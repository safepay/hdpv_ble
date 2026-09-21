"""Capture a home key by impersonating a shade that has never been adopted.

A shade leaves the factory with a zeroed home key, so the write that
installs one arrives in the clear. Presenting an unadopted shade to the
PowerView app therefore yields the key for the whole home, which is what
the ESP32 and Linux emulators under `emu/` do out-of-process.

This module carries the half of that which needs no Bluetooth: framing a
reply and recognising the key write. The BlueZ transport that puts it on
the air lives separately, so the part that cannot be exercised without a
Linux host and a local adapter stays as small as possible.

Framing mirrors `PowerViewBLE` in api.py rather than either emulator --
those are GPLv2 and this ships under the repository's Apache licence.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
import contextlib
from dataclasses import dataclass
import sys
from typing import TYPE_CHECKING, Final

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .const import LOGGER

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

# Request layout is serviceID, cmdID, sequence, length, body -- the frame
# _transact() builds. A reply echoes the first three with 0x10 cleared from
# the serviceID, which is the same mask api.py applies as `cmd & 0xFFEF`.
HEADER_LEN: Final[int] = 4
RESPONSE_MASK: Final[int] = 0xEF
KEY_LEN: Final[int] = 16

# serviceID 0xFB, cmdID 0x02: the write that installs a home key.
SET_KEY_CMD: Final[tuple[int, int]] = (0xFB, 0x02)

# The name the emulated shade advertises under. Also what identifies it to
# the rest of the integration, which would otherwise discover its own
# advertisement and offer it as a shade to set up.
EMU_NAME: Final[str] = "myPVcover"

# Status byte of a bare acknowledgement. 0x00 is success; shades answer
# 0x04 for a bad length and 0x80 for a bad field (PV_ERROR_CODES in
# scripts/shade_report.py).
ACK_OK: Final[bytes] = b"\x00"

# serviceID 0xFF, cmdID 0xDD: product info, gated on a one-byte selector.
# The app opens an adoption with selector 5 and gives up if it is answered
# with a bare ack, so this is the one command that must carry real data.
PRODUCT_INFO_CMD: Final[tuple[int, int]] = (0xFF, 0xDD)
SELECTOR_SHORT: Final[int] = 0x04
SELECTOR_EXTENDED: Final[int] = 0x05
# Two-byte rejection a shade returns for a selector it does not implement.
REJECT_SELECTOR: Final[int] = 0x8C

# Identity this emulated shade reports. The values are ours; the layout they
# sit in is the one annotate_query() in scripts/shade_report.py documents
# against real fw_rev=22 hardware -- byte 1 echoes the selector, serial at
# 2-9, fw_rev 10-11, sw_rev 14-15, hw_rev 18-21, build id 22-25, type_id 26,
# model 27. Real firmware answers selector 5 in 28 bytes.
EMU_SERIAL: Final[bytes] = bytes.fromhex("0123456789abcdef")
EMU_FW_REV: Final[int] = 22
EMU_SW_REV: Final[int] = 391
EMU_HW_REV: Final[int] = 171103
EMU_BUILD_ID: Final[int] = 1015780
EMU_TYPE_ID: Final[int] = 42
EMU_MODEL_ID: Final[int] = 224


def _product_info(selector: int) -> bytes:
    """Return the product-info payload for one selector."""
    if selector == SELECTOR_EXTENDED:
        return (
            bytes([0x00, SELECTOR_EXTENDED])
            + EMU_SERIAL
            + EMU_FW_REV.to_bytes(2, "little")
            + bytes(2)
            + EMU_SW_REV.to_bytes(2, "little")
            + bytes(2)
            + EMU_HW_REV.to_bytes(4, "little")
            + EMU_BUILD_ID.to_bytes(4, "little")
            + bytes([EMU_TYPE_ID, EMU_MODEL_ID])
        )
    if selector == SELECTOR_SHORT:
        return (
            bytes([0x00, SELECTOR_SHORT])
            + (1).to_bytes(4, "little")
            + EMU_SW_REV.to_bytes(2, "little")
            + bytes(6)
        )
    return bytes([REJECT_SELECTOR, selector])


# serviceID 0xFA: scene storage. Adoption reads two scenes and writes two
# back; a bare ack to the read stops it, a bare ack to the write does not.
GET_SCENE_CMD: Final[tuple[int, int]] = (0xFA, 0x5B)
SET_SCENE_CMD: Final[tuple[int, int]] = (0xFA, 0x5A)
# Repeated locally rather than imported from api.py, which pulls in bleak and
# Home Assistant; this module stays importable with only cryptography.
KEEP_POSITION: Final[int] = 0x8000
# Half open, in the hundredths-of-a-percent the position struct uses.
SCENE_POSITION: Final[int] = 5000


def _scene(body: bytes) -> bytes:
    """Return the stored scene named by a get-scene request.

    Both bytes of the request are echoed back, then the position struct
    api.py sends -- pos1, pos2, pos3, tilt as little-endian u16, with the
    rails this shade does not have left at KEEP_POSITION.
    """
    return (
        bytes([0x00])
        + (body[:2] + bytes(2))[:2]
        + SCENE_POSITION.to_bytes(2, "little")
        + KEEP_POSITION.to_bytes(2, "little") * 3
        + bytes(7)
    )



@dataclass(frozen=True)
class CaptureSupport:
    """Whether this installation can advertise a shade of its own."""

    supported: bool
    reason: str | None = None
    adapter: str | None = None


async def async_capture_support(hass: HomeAssistant) -> CaptureSupport:
    """Report whether a home key can be captured on this installation.

    Checks only what is cheap and decisive, so the offer can be hidden
    rather than failing later: BlueZ is Linux-only, and a Bluetooth proxy
    can scan but never advertise, so an install with no local adapter
    cannot do this however modern its proxies are.

    Whether the adapter supports extended advertising is deliberately not
    checked here -- it needs the adapter's own details, and the payload is
    over the 31-byte legacy limit, so registration is the honest test.
    """
    # Imported here, not at module scope: this keeps the responder below
    # usable on a bare Linux host with only `cryptography` installed, which
    # is how the BlueZ transport gets exercised away from Home Assistant.
    from habluetooth import HaScanner  # noqa: PLC0415

    from homeassistant.components import bluetooth  # noqa: PLC0415

    if not sys.platform.startswith("linux"):
        return CaptureSupport(False, "capture_not_linux")

    local = [
        scanner
        for scanner in bluetooth.async_current_scanners(hass)
        if isinstance(scanner, HaScanner)
    ]
    if not local:
        return CaptureSupport(False, "capture_no_local_adapter")

    # Logged because the choice matters and is invisible otherwise: an
    # installation with more than one local adapter gets the first, which
    # may well be the one Home Assistant is busiest scanning on.
    LOGGER.debug(
        "keycapture: local adapters %s, using %s",
        [(s.adapter, s.source) for s in local],
        local[0].adapter,
    )
    adapter = local[0].adapter
    from .keycapture_bluez import async_adapter_ready  # noqa: PLC0415

    if not await async_adapter_ready(adapter):
        return CaptureSupport(False, "capture_adapter_unavailable")

    return CaptureSupport(True, adapter=adapter)


def async_is_own_advert(hass: HomeAssistant, address: str) -> bool:
    """Whether this advertisement is our own capture emulator.

    Matching on the name does not work: the name rides in the scan response,
    and a passively-scanning proxy reports the address instead. The address
    is decisive -- it is the local adapter's own, because the emulator
    advertises through it.
    """
    from habluetooth import HaScanner  # noqa: PLC0415

    from homeassistant.components import bluetooth  # noqa: PLC0415

    return any(
        isinstance(scanner, HaScanner)
        and scanner.source.upper() == address.upper()
        for scanner in bluetooth.async_current_scanners(hass)
    )


@contextlib.asynccontextmanager
async def async_quiet_adapter(
    hass: HomeAssistant, adapter: str
) -> AsyncIterator[None]:
    """Stop Home Assistant scanning on adapter for the duration.

    A controller has to divide its time between scanning as a central and
    advertising as a peripheral, and an installation with several shades
    keeps the scanner busy continuously. On a weaker adapter that leaves an
    incoming connection unable to complete: the shade is advertised and the
    app offers it, but adoption never starts.

    Reaching into Home Assistant's Bluetooth stack like this is not
    something an integration should do lightly, so it is confined to the
    capture window and always undone. Any Bluetooth proxy keeps scanning
    throughout; only this one adapter goes quiet.
    """
    from habluetooth import HaScanner  # noqa: PLC0415

    from homeassistant.components import bluetooth  # noqa: PLC0415

    scanner = next(
        (
            candidate
            for candidate in bluetooth.async_current_scanners(hass)
            if isinstance(candidate, HaScanner) and candidate.adapter == adapter
        ),
        None,
    )
    if scanner is None:
        yield
        return

    LOGGER.info("keycapture: pausing Home Assistant's scanner on %s", adapter)
    await scanner.async_stop()
    try:
        yield
    finally:
        LOGGER.info("keycapture: resuming scanning on %s", adapter)
        with contextlib.suppress(Exception):
            await scanner.async_start()


class ShadeResponder:
    """Answer writes as an unadopted shade would, keeping any key offered.

    Starts with no key, so traffic is plaintext in both directions. The
    key write is acknowledged *before* the key takes effect, because the
    other side does not know the shade has it yet and would be unable to
    read an encrypted reply.
    """

    def __init__(self) -> None:
        """Start unadopted, with no key and no encryption."""
        self._key: bytes = b""
        # Set when a frame arrives that cannot be plaintext. Distinguishes
        # "nothing connected" from "the identity is already taken", which
        # are different problems for whoever is trying to capture a key.
        self.saw_foreign_traffic: bool = False

    @property
    def home_key(self) -> bytes:
        """Return the captured key, or empty until one has been installed."""
        return self._key

    def _crypt(self, data: bytes) -> bytes:
        # AES-128-CTR with a zero nonce, a fresh context per message so the
        # counter restarts each time -- the construction api.py connects with.
        ctx = Cipher(algorithms.AES(self._key), modes.CTR(bytes(16))).encryptor()
        return ctx.update(data) + ctx.finalize()

    def handle(self, data: bytes) -> bytes | None:
        """Return the reply to one written frame, or None if unreadable.

        Captures the home key as a side effect when the frame is the write
        that installs one.
        """
        if len(data) < HEADER_LEN:
            LOGGER.debug("keycapture: runt frame (%d bytes)", len(data))
            return None

        plain = self._crypt(data) if self._key else data
        service_id, cmd_id, sequence, data_len = plain[:HEADER_LEN]

        # A frame whose length field disagrees with what arrived is not
        # plaintext: something is addressing this identity with a key we do
        # not hold, which means the shade being impersonated is already
        # adopted. Answering would put garbage on the wire, so say nothing
        # and let the caller report why nothing was captured.
        if data_len != len(plain) - HEADER_LEN:
            LOGGER.debug(
                "keycapture: implausible frame (len %d, got %d) -- peer is "
                "probably encrypting with a key we do not have",
                data_len,
                len(plain) - HEADER_LEN,
            )
            self.saw_foreign_traffic = True
            return None

        body = plain[HEADER_LEN : HEADER_LEN + data_len]
        LOGGER.debug(
            "keycapture: srv %02x cmd %02x seq %d len %d",
            service_id,
            cmd_id,
            sequence,
            data_len,
        )

        payload = ACK_OK
        if (service_id, cmd_id) == PRODUCT_INFO_CMD:
            payload = _product_info(body[0] if body else SELECTOR_EXTENDED)
        elif (service_id, cmd_id) == GET_SCENE_CMD:
            payload = _scene(body)
        elif (service_id, cmd_id) == SET_SCENE_CMD:
            payload = bytes([0x00]) + (body[:2] + bytes(2))[:2]

        reply = bytes(
            [service_id & RESPONSE_MASK, cmd_id, sequence, len(payload)]
        ) + payload
        # Encrypt against the key in force as the frame arrived, so the
        # acknowledgement below still goes out in the clear.
        if self._key:
            reply = self._crypt(reply)

        if (service_id, cmd_id) == SET_KEY_CMD:
            self._install_key(body, data_len)

        return reply

    def _install_key(self, body: bytes, data_len: int) -> None:
        if data_len != KEY_LEN or len(body) != KEY_LEN:
            LOGGER.warning(
                "keycapture: ignoring %d-byte home key, expected %d",
                len(body),
                KEY_LEN,
            )
            return
        self._key = bytes(body)
        LOGGER.info("keycapture: home key captured")
