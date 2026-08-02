"""Hunter Douglas PowerView BLE API."""

import asyncio
from dataclasses import dataclass
from datetime import time as dt_time
from enum import Enum
import time
from typing import Final, NamedTuple

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak.uuids import normalize_uuid_str
from bleak_retry_connector import establish_connection
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.base import (
    AEADDecryptionContext,
    AEADEncryptionContext,
)

from homeassistant.components.cover import (
    ATTR_CURRENT_POSITION,
    ATTR_CURRENT_TILT_POSITION,
)
from homeassistant.util import dt as dt_util

from .const import LOGGER, TIMEOUT

UUID_COV_SERVICE: Final[str] = normalize_uuid_str("fdc1")
UUID_TX: Final[str] = "cafe1001-c0ff-ee01-8000-a110ca7ab1e0"
UUID_DEV_SERVICE: Final[str] = normalize_uuid_str("180a")

ATTR_ACTIVITY: Final[str] = "activity"


# Type IDs and their product names follow aiopvapi's `resources/shade.py`, the
# library behind Home Assistant's official (hub-based) hunterdouglas_powerview
# integration. Types 39 and 103 are additions from openHAB's database that
# aiopvapi does not carry.
SHADE_TYPE: Final[dict[int, str]] = {
    # up down only
    1: "Designer Roller",
    4: "Roman",
    5: "Bottom Up",
    6: "Duette",
    10: "Duette and Applause SkyLift",
    19: "Provenance Woven Wood",
    26: "Skyline Panel, Left Stack",
    27: "Skyline Panel, Right Stack",
    28: "Skyline Panel, Split Stack",
    31: "Vignette",
    32: "Vignette",
    42: "M25T Roller Blind",
    49: "AC Roller",
    52: "Banded Shades",
    53: "Sonnette",
    57: "Carole Roman Shades",
    69: "Curtain, Left Stack",
    70: "Curtain, Right Stack",
    71: "Curtain, Split Stack",
    84: "Vignette",
    # top down (single rail, inverted position)
    7: "Top Down",
    # top down bottom up (dual rail)
    8: "Duette, Top Down Bottom Up",
    9: "Duette DuoLite, Top Down Bottom Up",
    33: "Duette Architella, Top Down Bottom Up",
    47: "Pleated, Top Down Bottom Up",
    # tilt only (no position movement)
    39: "Parkland",
    40: "Everwood Alternative Wood Blinds",
    66: "Palm Beach Shutters",
    # tilt on closed
    18: "Pirouette",
    23: "Silhouette",
    43: "Facette",
    44: "Twist",
    72: "Silhouette",
    # tilt anywhere (position + tilt)
    51: "Venetian, Tilt Anywhere",
    54: "Vertical Slats, Left Stack",
    55: "Vertical Slats, Right Stack",
    56: "Vertical Slats, Split Stack",
    62: "Venetian, Tilt Anywhere",
    103: "Designer Banded, Tilt Anywhere",
    # duolite (dual overlapping fabrics)
    38: "Silhouette Duolite",
    65: "Vignette Duolite",
    79: "Duolite Lift",
    95: "Aura Illuminated, Roller",
}


class ShadeCapability(NamedTuple):
    """Capability flags for a shade type."""

    has_tilt: bool = False
    tilt_only: bool = False
    is_tilt_on_closed: bool = False  # tilt only available when fully closed
    is_top_down: bool = False  # position logic is inverted (type 7 only)
    is_tdbu: bool = False  # dual-rail Top Down Bottom Up (needs two entities)
    is_duolite: bool = False  # dual-fabric sheer+opaque (needs three entities)
    has_vane: bool = False  # Duette/Applause privacy vanes controlled via pos2, exposed as tilt


# Capabilities mirror the aiopvapi class each type is registered under, so a
# type behaves the same here as it does over the hub API. Types absent from
# this table fall back to plain up/down, which is aiopvapi's capability 0.
# Duette/Applause with privacy vanes (pos2) are exposed as tilt in HA - PR#33 from patman15/hdpv_ble
SHADE_CAPABILITIES: Final[dict[int, ShadeCapability]] = {
    # duette / applause with vanes (privacy) - controlled via pos2, exposed as tilt
    6: ShadeCapability(has_vane=True),
    10: ShadeCapability(has_vane=True),
    # tilt anywhere (position + tilt)
    51: ShadeCapability(has_tilt=True),
    54: ShadeCapability(has_tilt=True),
    55: ShadeCapability(has_tilt=True),
    56: ShadeCapability(has_tilt=True),
    62: ShadeCapability(has_tilt=True),
    103: ShadeCapability(has_tilt=True),
    # tilt only (no position movement)
    39: ShadeCapability(has_tilt=True, tilt_only=True),
    40: ShadeCapability(has_tilt=True, tilt_only=True),
    66: ShadeCapability(has_tilt=True, tilt_only=True),
    # tilt on closed (tilt only available at fully closed position)
    18: ShadeCapability(has_tilt=True, is_tilt_on_closed=True),
    23: ShadeCapability(has_tilt=True, is_tilt_on_closed=True),
    43: ShadeCapability(has_tilt=True, is_tilt_on_closed=True),
    44: ShadeCapability(has_tilt=True, is_tilt_on_closed=True),
    72: ShadeCapability(has_tilt=True, is_tilt_on_closed=True),
    # top-down only (single rail, inverted position). Type 7 is the only
    # inverted single-rail type; type 10 (SkyLift) was previously listed here
    # on the strength of its name, but aiopvapi registers it as a plain
    # bottom-up shade, so it now falls through to the default.
    7: ShadeCapability(is_top_down=True),
    # dual-rail top-down/bottom-up (two independent rails → two entities).
    # Type 9 is named DuoLite but aiopvapi registers it as plain TDBU, and the
    # two-rail path is the one confirmed on hardware -- so no is_duolite here.
    8: ShadeCapability(is_tdbu=True),
    9: ShadeCapability(is_tdbu=True),
    33: ShadeCapability(is_tdbu=True),
    47: ShadeCapability(is_tdbu=True),
    # duolite (dual overlapping fabrics → three entities). Type 38 also tilts;
    # aiopvapi registers it under a tilting class, unlike the others.
    38: ShadeCapability(has_tilt=True, is_duolite=True),
    65: ShadeCapability(is_duolite=True),
    79: ShadeCapability(is_duolite=True),
    95: ShadeCapability(is_duolite=True),
}

_DEFAULT_CAPABILITY: Final[ShadeCapability] = ShadeCapability()


def get_shade_capabilities(type_id: int | None) -> ShadeCapability:
    """Return shade capabilities for a given type_id."""
    if type_id is None:
        return _DEFAULT_CAPABILITY
    return SHADE_CAPABILITIES.get(type_id, _DEFAULT_CAPABILITY)


OPEN_POSITION: Final[int] = 100
CLOSED_POSITION: Final[int] = 0

# Wire sentinel meaning "leave this axis where it is". Sent verbatim -- for
# pos1/pos2 that means skipping the *100 fixed-point scaling a real lift
# position would get; pos3 and tilt are unscaled either way.
KEEP_POSITION: Final[int] = 0x8000


class ShadeMove(NamedTuple):
    """A movement request in device coordinates.

    Cover entities translate their Home Assistant facing target into one of
    these, so the axis inversion and rail interlocks of each shade type stay
    in that type's subclass rather than leaking into the transport. Fields are
    in the wire order of ``PowerViewBLE.set_position``.
    """

    pos1: int
    pos2: int = KEEP_POSITION
    pos3: int = KEEP_POSITION
    tilt: int = KEEP_POSITION


POWER_LEVELS: Final[dict[int, int]] = {
    3: 100,  # 3 = 100% to 51% power remaining (also reported by hardwired)
    2: 50,  # 2 = 50% to 21% power remaining
    1: 20,  # 1 = 20% or less power remaining
    0: 0,  # 0 = No power remaining
}


class ShadeCmd(Enum):
    """The PowerView cover commands."""

    SET_POSITION = 0x01F7
    STOP = 0xB8F7
    ACTIVATE_SCENE = 0xBAF7
    IDENTIFY = 0x11F7
    POWER_STATUS = 0xDEFF
    # Values are the emulator's `(serviceID << 8) | cmdID` constants
    # byte-swapped, because _transact writes them little-endian: the
    # emulator's 0xFF77 "set shade time" is 0x77FF here.
    SET_TIME = 0x77FF
    SET_SOLAR = 0x87FF


@dataclass
class PVDeviceInfo:
    """Dataclass holding available PowerView device information."""

    manufacturer: str = ""
    model: str = ""
    serial_nr: str = ""
    hw_rev: str = ""
    fw_rev: str = ""
    sw_rev: str = ""
    battery_level: int = 0


class PowerViewBLE:
    """Class to handle connection to PowerView remote device."""

    # A G3 gateway refreshes every shade's clock at least daily. Match that,
    # so a clock drifting slowly without ever raising `reset_clock` still
    # gets corrected -- on an install with no gateway nothing else would.
    _CLOCK_REFRESH_S: Final[float] = 24 * 3600

    def __init__(self, ble_device: BLEDevice, home_key: bytes = b"") -> None:
        """Initialize device API via Bluetooth."""
        self._ble_device: BLEDevice = ble_device
        self.name: Final[str] = self._ble_device.name or "unknown"
        self._seqcnt: int = 1
        self._client: BleakClient = BleakClient(self._ble_device)
        self._data_event = asyncio.Event()
        self._data: bytes = b""
        self._info: PVDeviceInfo = PVDeviceInfo()
        # The three the coordinator writes from each advertisement, so they
        # are plain attributes rather than pass-through properties.
        #
        # Whether communication with this shade is encrypted.
        self.encrypted: bool = False
        # None until an advertisement has been decoded. Unknown counts as
        # "needs setting": a shade we cannot currently hear may have
        # rebooted unseen, and a redundant push is cheaper than a shade
        # sitting with a dead clock because we assumed the best.
        self.clock_reset: bool | None = None
        # Today's sunrise and sunset, or None where the sun does not do
        # both. Supplied by the coordinator, which is the side with a hass.
        self.solar: tuple[dt_time, dt_time] | None = None
        self._last_time_set: float = 0.0
        self._cmd_lock: Final = asyncio.Lock()
        # The pending command and the disconnect behaviour it wants. The two
        # travel together because the coroutine that ends up sending a
        # command is often not the caller that asked for it.
        self._cmd_next: tuple[tuple[ShadeCmd, bytes], bool]
        self._cipher: Final[Cipher | None] = (
            Cipher(algorithms.AES(home_key), modes.CTR(bytes(16)))
            if len(home_key) == 16
            else None
        )

    async def _wait_event(self) -> None:
        await self._data_event.wait()
        self._data_event.clear()

    def set_ble_device(self, ble_device: BLEDevice) -> None:
        """Update the BLE device reference (e.g. when proxy details change)."""
        self._ble_device = ble_device

    @property
    def has_key(self) -> bool:
        """Return True if a valid homekey was provided."""
        return self._cipher is not None

    @property
    def info(self) -> PVDeviceInfo:
        """Return device information, e.g. SW version."""
        return self._info

    @property
    def is_connected(self) -> bool:
        """Return whether remote device is connected."""
        return self._client.is_connected

    async def _transact(self, cmd: tuple[ShadeCmd, bytes]) -> int:
        # Assumes _cmd_lock is held and _connect() has run. Writes a framed
        # (optionally encrypted) request and waits for the device's reply;
        # caller inspects/validates self._data afterwards. Returns the seq
        # number used so callers can verify the echo.
        tx_data: bytes = bytes(
            int.to_bytes(cmd[0].value, 2, byteorder="little")
            + bytes([self._seqcnt, len(cmd[1])])
            + cmd[1]
        )
        LOGGER.debug("sending cmd: %s", tx_data.hex(" "))
        if self._cipher is not None and self.encrypted:
            enc: AEADEncryptionContext = self._cipher.encryptor()
            tx_data = enc.update(tx_data) + enc.finalize()
            LOGGER.debug("  encrypted: %s", tx_data.hex(" "))
        self._data_event.clear()
        await self._client.write_gatt_char(UUID_TX, tx_data, False)
        seq = self._seqcnt
        self._seqcnt += 1
        await asyncio.wait_for(self._wait_event(), timeout=TIMEOUT)
        return seq

    # general cmd: uint16_t cmd, uint8_t seqID, uint8_t data_len
    async def _cmd(self, cmd: tuple[ShadeCmd, bytes], disconnect: bool = True) -> None:
        # Commands coalesce rather than queue: one arriving while another is
        # in flight replaces the pending one, so dragging a slider does not
        # put every intermediate position on the wire.
        self._cmd_next = (cmd, disconnect)
        if self._cmd_lock.locked():
            LOGGER.debug("%s: device busy, queuing %s command", self.name, cmd[0])
            return

        # Whoever holds the lock owns whatever ends up in _cmd_next, so keep
        # going until nothing new has arrived. Reading it once was not
        # enough: anything landing after that read was stored and then
        # silently never sent, and the window is not small -- it spans the
        # transact and, for a command that disconnects, the seconds that
        # takes.
        while True:
            async with self._cmd_lock:
                try:
                    await self._connect()
                    # Read after connecting, so a command arriving while the
                    # link is coming up still replaces this one instead of
                    # being sent after it.
                    pending = self._cmd_next
                    cmd_run, disconnect_run = pending
                    try:
                        seq = await self._transact(cmd_run)
                        self._verify_ack_reply(self._data, seq, cmd_run[0])
                    except TimeoutError as ex:
                        raise TimeoutError("Device did not send confirmation.") from ex
                    finally:
                        if disconnect_run:
                            await self._client.disconnect()  # device disconnects itself
                except Exception as ex:
                    LOGGER.error("Error: %s - %s", type(ex).__name__, ex)
                    raise
            # Safe to test outside the lock: releasing it does not yield to
            # the loop, so nothing can slip in between the release and here.
            # A concurrent caller either queued while we held the lock, which
            # this catches, or arrives afterwards and drives its own loop.
            if self._cmd_next is pending:
                return
            LOGGER.debug(
                "%s: sending %s that arrived while busy",
                self.name,
                self._cmd_next[0][0],
            )

    async def _query(self, cmd: tuple[ShadeCmd, bytes]) -> bytes:
        """Send a read-type opcode and return its payload bytes."""
        async with self._cmd_lock:
            await self._connect()
            try:
                try:
                    seq = await self._transact(cmd)
                except TimeoutError as ex:
                    raise TimeoutError("Device did not send response.") from ex
                if not self._verify_header(self._data, seq, cmd[0]):
                    raise BleakError("Malformed query response header")
                length = int(self._data[3])
                return bytes(self._data[4 : 4 + length])
            finally:
                await self._client.disconnect()

    @staticmethod
    def dec_manufacturer_data(data: bytearray) -> dict[str, float | int | bool]:
        """Decode manufacturer data from BLE advertisement V2."""
        if len(data) != 9:
            LOGGER.debug("not a V2 record!")
            return {}
        # data[3] lower 2 bits are status flags; pos is in bits 2-7 of data[3]
        # and bits 0-3 of data[4].  Read flags before extracting position so
        # the masking below doesn't accidentally overwrite them.
        flags: Final[int] = data[3] & 0x3
        # Mask pos2 bits (upper nibble of data[4]) out before forming the
        # 10-bit position value, otherwise a non-zero top-rail position on
        # TDBU shades contaminates the bottom-rail reading.
        pos: Final[int] = ((data[4] & 0x0F) << 6) | ((data[3] >> 2) & 0x3F)
        pos2: Final[int] = (int(data[5]) << 4) + (int(data[4]) >> 4)
        return {
            ATTR_CURRENT_POSITION: pos / 10,
            # normalized to the same 0-100 percent scale as ATTR_CURRENT_POSITION
            # (pos2 is also a 10-bit field after the >>2, same width as pos1) --
            # inferred by symmetry, not yet confirmed against real TDBU hardware
            "position2": (pos2 >> 2) / 10,
            "position3": int(data[6]),
            ATTR_CURRENT_TILT_POSITION: int(data[7]),
            "home_id": int.from_bytes(data[0:2], byteorder="little"),
            "type_id": int(data[2]),
            "is_opening": bool(flags == 0x2),
            "is_closing": bool(flags == 0x1),
            "battery_charging": bool(flags == 0x3),  # observed
            "battery_level": POWER_LEVELS[(data[8] >> 6)],
            "reset_mode": bool(data[8] & 0x1),
            "reset_clock": bool(data[8] & 0x2),
        }

    # position cmd: uint16_t pos1, uint16_t pos2, uint16_t pos3, uint16_t tilt, uint8_t velocity
    async def set_position(
        self,
        pos1: int,
        pos2: int = KEEP_POSITION,
        pos3: int = KEEP_POSITION,
        tilt: int = KEEP_POSITION,
        velocity: int = 0x0,
        disconnect: bool = True,
    ) -> None:
        """Set position of device."""
        LOGGER.debug(
            "%s setting position to %i/%i/%i, tilt %i, velocity %s",
            self.name,
            pos1,
            pos2,
            pos3,
            tilt,
            velocity,
        )
        # pos2 is another lift-rail position, like pos1 -- not a rotation like
        # tilt -- so it gets the same *100 fixed-point wire encoding as pos1.
        # KEEP_POSITION is the device's "leave unchanged" sentinel and must
        # pass through unmultiplied.
        pos2_wire = pos2 if pos2 == KEEP_POSITION else pos2 * 100
        await self._cmd(
            (
                ShadeCmd.SET_POSITION,
                int.to_bytes(pos1 * 100, 2, byteorder="little")
                + int.to_bytes(pos2_wire, 2, byteorder="little")
                + int.to_bytes(pos3, 2, byteorder="little")
                + int.to_bytes(tilt, 2, byteorder="little")
                + int.to_bytes(velocity, 1),
            ),
            disconnect,
        )

    async def stop(self) -> None:
        """Stop device movement."""
        LOGGER.debug("%s stop", self.name)
        await self._cmd((ShadeCmd.STOP, b""))

    # uint8_t scene#, uint8_t unknown
    # open: scene 2
    # close: scene 3
    async def activate_scene(self, idx: int) -> None:
        """Activate stored scene."""
        LOGGER.debug("%s set scene #%i", self.name, idx)
        await self._cmd(
            (
                ShadeCmd.ACTIVATE_SCENE,
                int.to_bytes(idx, 1, byteorder="little") + bytes([0xA2]),
            ),
        )

    async def identify(self, beeps: int = 0x3) -> None:
        """Identify device."""
        LOGGER.debug("%s identify (%i)", self.name, beeps)
        await self._cmd((ShadeCmd.IDENTIFY, bytes([min(beeps, 0xFF)])))

    def _verify_header(self, data: bytes, seq_nr: int, cmd: ShadeCmd) -> bool:
        """Verify common header fields (length, echoed opcode, seq match)."""
        if len(data) < 4:
            LOGGER.error("Response message too short")
            return False
        if int.from_bytes(data[0:2], byteorder="little") != cmd.value & 0xFFEF:
            LOGGER.warning("Response to wrong command")
            return False
        if int(data[2]) != seq_nr:
            LOGGER.warning(
                "Response sequence id %i wrong, expected %d", int(data[2]), seq_nr
            )
            return False
        return True

    def _verify_ack_reply(self, data: bytes, seq_nr: int, cmd: ShadeCmd) -> bool:
        """Verify an ack-only reply (1-byte status payload, 0 == success)."""
        if not self._verify_header(data, seq_nr, cmd):
            return False
        if int(data[3]) != 1:
            LOGGER.error("Wrong response data length")
            return False
        if int(data[4] != 0):
            LOGGER.error("Command %X returned error #%d", cmd.value, int(data[4]))
            return False
        return True

    async def query_dev_info(self) -> dict[str, str]:
        """Return detailed device information."""
        data: dict[str, str] = {}
        uuids: Final[dict[str, str]] = {
            "manufacturer": "2a29",
            "model": "2a24",
            "serial_nr": "2a25",
            "hw_rev": "2a27",
            "fw_rev": "2a26",
            "sw_rev": "2a28",
        }

        async with self._cmd_lock:
            try:
                await self._connect()

                for key, uuid in uuids.items():
                    LOGGER.debug("querying %s(%s)", key, uuid)
                    data[key] = (
                        (await self._client.read_gatt_char(normalize_uuid_str(uuid)))
                        .copy()
                        .decode("UTF-8")
                    )
            except BleakError as ex:
                LOGGER.debug("%s: querying failed: %s", self.name, ex)
                raise
            finally:
                await self.disconnect()
        LOGGER.debug("%s device data: %s", self.name, data)
        return data.copy()

    async def query_power_status(self) -> bytes:
        """Return the raw 0xFFDE power-status reply, uninterpreted.

        Deliberately returns bytes rather than a decoded power source. The
        encoding is not established: byte 0 was previously read as a power-type
        enum, but every sample behind that reading came from hardwired shades,
        and acting on it misclassified battery shades as hardwired. Until a
        confirmed battery-shade sample exists, this is reported, never acted on.
        """
        return await self._query((ShadeCmd.POWER_STATUS, b""))

    def _on_disconnect(self, client: BleakClient) -> None:
        """Disconnect callback function."""

        LOGGER.debug("Disconnected from %s", client.address)

    def _notification_handler(self, _sender, data: bytearray) -> None:
        LOGGER.debug("%s received BLE data: %s", self.name, data.hex(" "))
        self._data = bytes(data)
        if self._cipher is not None and self.encrypted:
            dec: AEADDecryptionContext = self._cipher.decryptor()
            self._data = bytes(dec.update(bytes(data)) + dec.finalize())
            LOGGER.debug(
                "%s %s",
                "decoded data: ".rjust(19 + len(self.name)),
                self._data.hex(" "),
            )

        self._data_event.set()

    def _clock_due(self) -> bool:
        """Return whether this connection should carry a clock update.

        The shade raises `reset_clock` when it has lost the time, so an
        advertisement saying otherwise means there is nothing urgent to do
        and the connection can get on with the command it was opened for.

        A clock that merely drifts never raises that flag, though, so the
        flag alone is not enough: fall back to a daily refresh, which is
        what a G3 gateway does and what an install without one would
        otherwise never get.
        """
        if self.clock_reset is not False:
            return True  # asked for it, or no advertisement decoded yet
        if self._last_time_set == 0.0:
            return True  # nothing set this session, so establish a baseline
        return time.monotonic() - self._last_time_set >= self._CLOCK_REFRESH_S

    async def _set_time(self) -> None:
        """Push the current local time to the shade. Best effort, never raises.

        A shade that loses power stops its clock and comes back not knowing
        the time, so its stored schedules stay dormant until something tells
        it -- it advertises this as the `reset_clock` flag. Hunter Douglas
        say the vendor app pushes the time when it next connects and a G3
        gateway does so at least daily, which is what "operate the shade once
        from the app" was really fixing. See _clock_due for when we send.

        Failure is not the caller's problem: this is an unsolicited extra, so
        it must never take down the command the caller actually asked for.
        """
        if self.encrypted and self._cipher is None:
            return  # would put plaintext on the wire; the shade would ignore it
        if not self._clock_due():
            LOGGER.debug("%s: clock still current, not resending", self.name)
            return
        now = dt_util.now()
        # 8 bytes: year LE uint16, month, day, hour, minute, second, weekday.
        # The emulator documents only the first seven and never validates the
        # length, so the missing field went unnoticed there; fw_rev=22 rejects
        # every other length with status 0x04.
        #
        # The weekday is ISO 8601, Monday=1..Sunday=7 -- isoweekday(), not
        # weekday(). Sweeping the byte 0-15 got 1-7 accepted and everything
        # else refused with 0x80, so the shade bounds-checks it and never
        # derives it from the date; weekday() is 0 on Mondays and would have
        # been refused one day in seven. Monday=1 was then read off shades
        # whose clocks only the G3 gateway had ever set, with the
        # integration stopped: accurate to the second, and 7 on a Sunday.
        payload: Final[bytes] = int.to_bytes(now.year, 2, byteorder="little") + bytes(
            [now.month, now.day, now.hour, now.minute, now.second, now.isoweekday()]
        )
        if not await self._send_quiet(ShadeCmd.SET_TIME, payload):
            return
        # Track the successful push only. Clearing the cached flag too keeps
        # a reconnect that lands before the next advertisement from sending
        # a second update it does not need.
        self._last_time_set = time.monotonic()
        self.clock_reset = False
        LOGGER.debug("%s: clock set to %s", self.name, now.isoformat())
        await self._set_solar()

    async def _set_solar(self) -> None:
        """Push today's sunrise and sunset. Best effort, never raises.

        Solar-tied schedules ("close at sunset") are driven from these, and
        the payload carries no date -- probing every length 2..16 found only
        6 accepted, three bytes of sunrise then three of sunset -- so the
        shade applies whatever it was last told to today, and the values go
        stale as the days shift. Riding along with the clock update gives
        the refresh for free: same daily cadence, same recovery after a
        power loss, one extra round trip on the connections that already
        carry a clock write.
        """
        if self.solar is None:
            return
        sunrise, sunset = self.solar
        payload: Final[bytes] = bytes(
            [
                sunrise.hour,
                sunrise.minute,
                sunrise.second,
                sunset.hour,
                sunset.minute,
                sunset.second,
            ]
        )
        if not await self._send_quiet(ShadeCmd.SET_SOLAR, payload):
            return
        LOGGER.debug(
            "%s: sunrise %s, sunset %s",
            self.name,
            sunrise.isoformat(),
            sunset.isoformat(),
        )

    async def _send_quiet(self, cmd: ShadeCmd, payload: bytes) -> bool:
        """Send a housekeeping command and report whether the shade took it.

        Swallows transport failures rather than propagating them; see
        _set_time for why these must never reach the caller.
        """
        try:
            seq = await self._transact((cmd, payload))
        except (BleakError, TimeoutError) as ex:
            LOGGER.debug("%s: %s failed: %s", self.name, cmd.name, ex)
            return False
        return self._ack_ok(seq, cmd)

    def _ack_ok(self, seq: int, cmd: ShadeCmd) -> bool:
        """Return whether an ack reply reports success, logging only at debug.

        Deliberately not _verify_ack_reply, which logs at error level. These
        are unsolicited housekeeping commands nobody asked for, so a shade
        that refuses one must not fill the log on every connect. A reply that
        is not recognisable as an ack for this command counts as a failure.

        Known statuses are 0x04 "invalid length" and 0x80 "invalid field
        value" -- PV_ERROR_CODES in scripts/shade_report.py. Neither is
        expected from the payloads sent here, so a shade answering that way
        runs firmware wanting a different shape and is worth a bug report.
        """
        ack: Final[bytes] = int.to_bytes(cmd.value & 0xFFEF, 2, byteorder="little")
        if not (
            len(self._data) > 4 and self._data[0:2] == ack and self._data[2] == seq
        ):
            LOGGER.debug(
                "%s: unrecognised reply to %s: %s",
                self.name,
                cmd.name,
                self._data.hex(" "),
            )
            return False
        status: Final[int] = self._data[4]
        if status:
            LOGGER.debug(
                "%s: shade rejected %s, status 0x%02X", self.name, cmd.name, status
            )
            return False
        return True

    async def _connect(self) -> None:
        """Connect to the device and setup notification if not connected."""

        LOGGER.debug("Connecting %s", self.name)

        if self.is_connected:
            LOGGER.debug("%s already connected", self.name)
            return

        start: float = time.time()
        self._client = await establish_connection(
            BleakClient,
            self._ble_device,
            self.name,
            disconnected_callback=self._on_disconnect,
            ble_device_callback=lambda: self._ble_device,
            services=[
                UUID_COV_SERVICE,
                UUID_DEV_SERVICE,
            ],
        )
        await self._client.start_notify(UUID_TX, self._notification_handler)

        LOGGER.debug("\tconnect took %is", time.time() - start)

        # Only on a fresh connection -- the early return above means a shade
        # that is already connected does not get a second push.
        await self._set_time()

    async def disconnect(self) -> None:
        """Disconnect the device and stop notifications."""

        if self.is_connected:
            LOGGER.debug("Disconnecting device %s", self.name)
            try:
                self._data_event.clear()
                await self._client.disconnect()
            except BleakError:
                LOGGER.warning("Disconnect failed!")
