#!/usr/bin/env python3
"""Emulate a Hunter Douglas PowerView shade as a BLE peripheral on Linux.

A port of ../PV_BLE_cover/PV_BLE_cover.ino that runs on the host's own
Bluetooth adapter through BlueZ's D-Bus GATT-server API, instead of on
an ESP32. Same purpose: adopting it into your home with the PowerView
app makes the app hand over the home key, which this prints to the
console. See ../README.md and the main README's "Getting the home key"
section.

Protocol and constants ported 1:1 from the .ino sketch. wolfSSL's
AES-128-CTR is replaced with the `cryptography` package: CTR-mode
output depends only on key, nonce and plaintext, not the
implementation, so this is bit-for-bit interchangeable with the
original and stays interoperable with the real PowerView app.

AUTHOR: patman15 (original ESP32 sketch, ../PV_BLE_cover/PV_BLE_cover.ino)
LICENSE: GPLv2, see ../README.md
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import struct
import sys

from ble_peripheral import (
    BLUEZ_SERVICE_NAME,
    GATT_MANAGER_IFACE,
    LE_ADVERTISING_MANAGER_IFACE,
    Advertisement,
    Application,
    Characteristic,
    Service,
    find_adapter,
)
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
import dbus
import dbus.exceptions
import dbus.mainloop.glib
from gi.repository import GLib

LOGGER = logging.getLogger("shade_emulator")

NAME = "myPVcover"
SW_VERSION = 391
SERIAL_NR = "01234567890ABCDEF"
TYP_ID = 42
MODEL_ID = 224
FW_REVISION = 27
HW_REVISION = 171103
BATTERY_LEVEL = 42
MFCT_ID = 2073  # Hunter Douglas BLE company identifier

COVER_SERVICE_UUID = "0000fdc1-0000-1000-8000-00805f9b34fb"
COVER_CHAR_UUID = "cafe1001-c0ff-ee01-8000-a110ca7ab1e0"
XXX_CHAR_UUID = "cafe1002-c0ff-ee01-8000-a110ca7ab1e0"

FW_SERVICE_UUID = "cafe8000-c0ff-ee01-8000-a110ca7ab1e0"
FW_CHAR_UUID = "cafe8003-c0ff-ee01-8000-a110ca7ab1e0"

DEV_SERVICE_UUID = "0000180a-0000-1000-8000-00805f9b34fb"
SER_CHAR_UUID = "00002a25-0000-1000-8000-00805f9b34fb"
MAN_CHAR_UUID = "00002a29-0000-1000-8000-00805f9b34fb"
MOD_CHAR_UUID = "00002a24-0000-1000-8000-00805f9b34fb"
FWR_CHAR_UUID = "00002a26-0000-1000-8000-00805f9b34fb"
HWR_CHAR_UUID = "00002a27-0000-1000-8000-00805f9b34fb"
SWR_CHAR_UUID = "00002a28-0000-1000-8000-00805f9b34fb"

BAT_SERVICE_UUID = "0000180f-0000-1000-8000-00805f9b34fb"
BAT_CHAR_UUID = "00002a19-0000-1000-8000-00805f9b34fb"

ZERO_KEY = b"\x00" * 16

# Static responses, verbatim from PV_BLE_cover.ino.
RET_F1DD = bytes(  # product info
    [0x00, 0x04, 0x01, 0x00, 0x00, 0x00, 0x87, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]
)
RET_FFDD = bytes([  # HW diagnostics
    0x00, 0x05, 0xD1, 0xA2, 0x9A, 0x42, 0x59, 0x5D, 0x5C, 0x52, 0x1B, 0x00, 0x00, 0x00,
    SW_VERSION & 0xFF, (SW_VERSION >> 8) & 0xFF, 0x00, 0x00,
    0x5F, 0x9C, 0x02, 0x00, 0x5F, 0x9C, 0x02, 0x00,
    TYP_ID & 0xFF, MODEL_ID & 0xFF, 0x08,
])
RET_FFDE = bytes([0x08, 0x00, 0x02, 0x26, 0x72, 0x01, 0x59, 0x01, 0x00])  # power status
RET_FA5B = bytes(  # get scene
    [0x00, 0x0A, 0xA2, 0x88, 0x13, 0x00, 0x80, 0x00, 0x80, 0x00, 0x80, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]
)
RET_FA5A = bytes([0x00, 0x02, 0xB0])  # set scene


class ShadeProtocol:
    """Message framing, AES-128-CTR home-key crypto and command handling.

    Ports decode()/set_response() from the .ino sketch. The home key
    starts zeroed (unencrypted traffic) and gets installed by an
    0xFB02 "set shade key" write, exactly like the real shade.
    """

    def __init__(self) -> None:
        """Start with the home key zeroed, i.e. traffic unencrypted."""
        self.home_key = ZERO_KEY

    def _crypt(self, data: bytes) -> bytes:
        """AES-128-CTR with the counter reset to zero for every message."""
        encryptor = Cipher(algorithms.AES(self.home_key), modes.CTR(ZERO_KEY)).encryptor()
        return encryptor.update(data) + encryptor.finalize()

    def _set_response(self, request: bytes, data: bytes | None = None) -> bytes:
        payload = data if data is not None else b"\x00"
        response = bytes([request[0] & 0xEF, request[1], request[2], len(payload)]) + payload
        LOGGER.debug("ret value (%d): %s", len(response), response.hex(" "))
        if self.home_key != ZERO_KEY:
            response = self._crypt(response)
            LOGGER.debug("encrypted (%d): %s", len(response), response.hex(" "))
        return response

    def decode(self, data_raw: bytes) -> bytes | None:
        """Handle one write to the cover characteristic.

        Returns the bytes to notify back, or None if this command
        doesn't answer.
        """
        LOGGER.debug("BLE data: %s", data_raw.hex(" "))
        if len(data_raw) < 4:
            return None

        if self.home_key != ZERO_KEY:
            data_dec = self._crypt(data_raw)
            LOGGER.debug("decrypted: %s", data_dec.hex(" "))
        else:
            data_dec = data_raw

        service_id, cmd_id, sequence, data_len = data_dec[0], data_dec[1], data_dec[2], data_dec[3]
        body = data_dec[4 : 4 + data_len]
        LOGGER.info(
            "message: SRV: %02x, CMD %02x, SEQ %i, LEN %i", service_id, cmd_id, sequence, data_len
        )

        command = (service_id << 8) | cmd_id
        return self._handle(command, data_dec, data_raw, body, data_len)

    def _handle(  # noqa: C901, PLR0911, PLR0912
        self, command: int, data_dec: bytes, data_raw: bytes, body: bytes, data_len: int
    ) -> bytes | None:
        if command == 0xF1DD:
            LOGGER.info("get product info.")
            return self._set_response(data_dec, RET_F1DD)
        if command == 0xF701:
            struct_bytes = body[:9].ljust(9, b"\x00")
            pos1, pos2, pos3, tilt, velocity = struct.unpack("<HHHHB", struct_bytes)
            LOGGER.info(
                "set position: pos1 %.2f%%, pos2 %d, pos3 %d, tilt %d, velocity %d",
                pos1 / 100.0, pos2, pos3, tilt, velocity,
            )
            return None
        if command == 0xF711:
            LOGGER.info("identify: %d times", body[0] if body else 0)
            return self._set_response(data_dec)
        if command == 0xF7B8:
            LOGGER.info("stop.")
            return None
        if command == 0xF7BA:
            LOGGER.info("activate scene #%d", body[0] if body else 0)
            return None
        if command == 0xFA5A:
            LOGGER.info("set scene #%d", body[0] if body else 0)
            return self._set_response(data_dec, RET_FA5A)
        if command == 0xFA5B:
            LOGGER.info("get scene #%d", body[0] if body else 0)
            return self._set_response(data_dec, RET_FA5B)
        if command == 0xFAEA:
            LOGGER.info("reset scene automations:")
            return self._set_response(data_dec)
        if command == 0xFB02:
            return self._set_shade_key(data_dec, data_raw, data_len)
        if command == 0xFF77:
            b = body[:7].ljust(7, b"\x00")
            year = b[0] | (b[1] << 8)
            LOGGER.info(
                "set time: %d-%d-%d %d:%d:%d",
                year, b[2], b[3], b[4], b[5], b[6],
            )
            return self._set_response(data_dec)
        if command == 0xFF87:
            b = body[:6].ljust(6, b"\x00")
            LOGGER.info(
                "set sunrise %d:%d:%d, sunset %d:%d:%d",
                b[0], b[1], b[2], b[3], b[4], b[5],
            )
            return self._set_response(data_dec)
        if command == 0xFFD7:
            b = body[:2].ljust(2, b"\x00")
            LOGGER.info(
                "set shade configuration: 0x%02X, status LED: %s",
                b[0], "on" if b[1] else "off",
            )
            return self._set_response(data_dec)
        if command == 0xFFDD:
            LOGGER.info("get HW diagnostics.")
            return self._set_response(data_dec, RET_FFDD)
        if command == 0xFFDE:
            LOGGER.info("get power status.")
            return self._set_response(data_dec, RET_FFDE)
        if command == 0xFFDF:
            LOGGER.info("set power type: %d", body[0] if body else 0)
            return self._set_response(data_dec)
        if command == 0xFFEE:
            LOGGER.info("factory reset.")
            return self._set_response(data_dec)

        LOGGER.warning("unknown message 0x%04X (try ACK)", command)
        return self._set_response(data_dec)

    def _set_shade_key(self, data_dec: bytes, data_raw: bytes, data_len: int) -> bytes:
        LOGGER.info("set shade key: %s", "".join(f"\\x{b:02X}" for b in data_raw[4:]))
        # ack before installing the key, so the ack itself goes out unencrypted,
        # exactly like the .ino sketch.
        response = self._set_response(data_dec)
        if data_len == 16:
            self.home_key = data_raw[4:20]
            LOGGER.info(
                "home key installed - paste into Home Assistant's home_key field: %s",
                self.home_key.hex(),
            )
        return response


class GenericCharacteristic(Characteristic):
    """Read/write characteristic that only logs (ports genericCallbacks)."""

    def __init__(
        self, bus: dbus.Bus, index: int, uuid: str, flags: list[str], service: Service, *, label: str
    ) -> None:
        """Create a characteristic that logs reads/writes under label."""
        super().__init__(bus, index, uuid, flags, service)
        self.label = label

    def read_value(self, options: dict) -> list[int]:
        """Log and return the characteristic's current value."""
        LOGGER.info("%s read", self.label)
        return self.value

    def write_value(self, value: bytes, options: dict) -> None:
        """Log the write and store it verbatim."""
        LOGGER.info("%s write: %s", self.label, value.hex(" "))
        self.value = list(value)


class StaticCharacteristic(Characteristic):
    """Read-only characteristic serving a fixed value (device info, battery)."""

    def __init__(
        self, bus: dbus.Bus, index: int, uuid: str, flags: list[str], service: Service, *, value: bytes
    ) -> None:
        """Create a read-only characteristic serving value."""
        super().__init__(bus, index, uuid, flags, service)
        self.value = list(value)


class CoverCharacteristic(Characteristic):
    """The cover characteristic: every write runs the PowerView protocol."""

    def __init__(self, bus: dbus.Bus, index: int, service: Service, protocol: ShadeProtocol) -> None:
        """Create the cover characteristic, wired up to protocol."""
        super().__init__(
            bus, index, COVER_CHAR_UUID, ["notify", "write", "write-without-response"], service
        )
        self.protocol = protocol

    def write_value(self, value: bytes, options: dict) -> None:
        """Run the write through the protocol and notify back any response."""
        response = self.protocol.decode(value)
        if response:
            self.notify(response)


def build_application(bus: dbus.Bus, protocol: ShadeProtocol) -> Application:
    """Assemble the GATT application: cover, FW, battery and device-info services."""
    app = Application(bus)

    cover_service = Service(bus, 0, COVER_SERVICE_UUID)
    cover_service.add_characteristic(CoverCharacteristic(bus, 0, cover_service, protocol))
    cover_service.add_characteristic(
        GenericCharacteristic(
            bus, 1, XXX_CHAR_UUID, ["notify", "write", "write-without-response"], cover_service, label="unknown"
        )
    )
    app.add_service(cover_service)

    fw_service = Service(bus, 1, FW_SERVICE_UUID)
    fw_service.add_characteristic(
        GenericCharacteristic(
            bus, 0, FW_CHAR_UUID, ["read", "write", "write-without-response"], fw_service, label="firmware"
        )
    )
    app.add_service(fw_service)

    bat_service = Service(bus, 2, BAT_SERVICE_UUID)
    bat_service.add_characteristic(
        StaticCharacteristic(bus, 0, BAT_CHAR_UUID, ["read"], bat_service, value=bytes([BATTERY_LEVEL]))
    )
    app.add_service(bat_service)

    dev_service = Service(bus, 3, DEV_SERVICE_UUID)
    for index, (uuid, value) in enumerate([
        (SWR_CHAR_UUID, str(SW_VERSION)),
        (SER_CHAR_UUID, SERIAL_NR),
        (MAN_CHAR_UUID, "Hunter Douglas"),
        (MOD_CHAR_UUID, str(TYP_ID)),
        (FWR_CHAR_UUID, str(FW_REVISION)),
        (HWR_CHAR_UUID, str(HW_REVISION)),
    ]):
        dev_service.add_characteristic(
            StaticCharacteristic(bus, index, uuid, ["read"], dev_service, value=value.encode())
        )
    app.add_service(dev_service)

    return app


def build_advertisement(bus: dbus.Bus) -> Advertisement:
    """Assemble the LE advertisement: local name, service UUID and manufacturer data.

    The manufacturer payload matches the .ino sketch's adv[] byte for
    byte: company ID 2073 (Hunter Douglas) then [00 00 TYP_ID 00 00 00
    00 00 A2].
    """
    advertisement = Advertisement(bus, 0)
    advertisement.local_name = NAME
    advertisement.add_service_uuid(COVER_SERVICE_UUID)
    advertisement.add_manufacturer_data(
        MFCT_ID, bytes([0x00, 0x00, TYP_ID & 0xFF, 0x00, 0x00, 0x00, 0x00, 0x00, 0xA2])
    )
    return advertisement


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--adapter", help="hci adapter name, e.g. hci0 (default: first usable)")
    parser.add_argument(
        "-v", "--verbose", action="count", default=0, help="-v for debug logging of every message"
    )
    return parser.parse_args()


def main() -> int:
    """Register the GATT application and advertisement, then run until Ctrl+C."""
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        stream=sys.stdout,
    )

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()

    try:
        gatt_adapter_path = find_adapter(bus, GATT_MANAGER_IFACE, args.adapter)
        ad_adapter_path = find_adapter(bus, LE_ADVERTISING_MANAGER_IFACE, args.adapter)
    except RuntimeError:
        LOGGER.exception("no usable Bluetooth adapter")
        return 1

    adapter_props = dbus.Interface(
        bus.get_object(BLUEZ_SERVICE_NAME, gatt_adapter_path), "org.freedesktop.DBus.Properties"
    )
    if not adapter_props.Get("org.bluez.Adapter1", "Powered"):
        LOGGER.info("powering on %s", gatt_adapter_path)
        adapter_props.Set("org.bluez.Adapter1", "Powered", True)

    service_manager = dbus.Interface(
        bus.get_object(BLUEZ_SERVICE_NAME, gatt_adapter_path), GATT_MANAGER_IFACE
    )
    ad_manager = dbus.Interface(
        bus.get_object(BLUEZ_SERVICE_NAME, ad_adapter_path), LE_ADVERTISING_MANAGER_IFACE
    )

    protocol = ShadeProtocol()
    app = build_application(bus, protocol)
    advertisement = build_advertisement(bus)

    mainloop = GLib.MainLoop()
    registration_failed = False

    def register_app_error_cb(error: Exception) -> None:
        nonlocal registration_failed
        LOGGER.error("failed to register GATT application: %s", error)
        registration_failed = True
        mainloop.quit()

    def register_ad_error_cb(error: Exception) -> None:
        nonlocal registration_failed
        LOGGER.error("failed to register advertisement: %s", error)
        registration_failed = True
        mainloop.quit()

    service_manager.RegisterApplication(
        app.get_path(),
        {},
        reply_handler=lambda: LOGGER.info("GATT application registered on %s", gatt_adapter_path),
        error_handler=register_app_error_cb,
    )
    ad_manager.RegisterAdvertisement(
        advertisement.get_path(),
        {},
        reply_handler=lambda: LOGGER.info("advertising as %r on %s", NAME, ad_adapter_path),
        error_handler=register_ad_error_cb,
    )

    LOGGER.info(
        "%s ready. Add it to your home in the PowerView app, then Ctrl+C to stop.", NAME
    )
    try:
        mainloop.run()
    except KeyboardInterrupt:
        LOGGER.info("shutting down")
    finally:
        with contextlib.suppress(dbus.exceptions.DBusException):
            ad_manager.UnregisterAdvertisement(advertisement.get_path())
        with contextlib.suppress(dbus.exceptions.DBusException):
            service_manager.UnregisterApplication(app.get_path())

    return 1 if registration_failed else 0


if __name__ == "__main__":
    sys.exit(main())
