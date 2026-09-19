"""BlueZ transport that puts an unadopted shade on the air.

Registers a GATT application and an LE advertisement with `bluetoothd`
over D-Bus, routes every write to `ShadeResponder`, and returns as soon
as a home key arrives.

Imported lazily, never at module scope: `dbus_fast` installs only on
Linux, where `bluetooth_adapters` and `habluetooth` already pull it in,
so this costs the integration no new dependency. `keycapture` itself
stays importable anywhere -- see `async_capture_support` for the checks
that decide whether this module is worth loading at all.

The advertisement carries `home_id = 0`, which is what marks a shade as
never adopted and is why the peer talks to it in plaintext.
"""

# dbus_fast takes each D-Bus signature from the annotation itself ("s",
# "ay", "a{sv}"), which every type checker reads as a forward reference to
# a type that does not exist. There is no other way to declare an interface
# with this library, so the two rules are off for this file alone.
# ruff: noqa: F821, F722
#
# No `from __future__ import annotations` here on purpose: dbus_fast reads
# the D-Bus signature out of each annotation, and PEP 563 would store the
# source text (`"'s'"`, quotes included) instead of the string itself.
import asyncio
import contextlib
from typing import Any, Final

from dbus_fast import BusType, PropertyAccess, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, dbus_property, method

from .const import LOGGER, MFCT_ID
from .keycapture import (
    EMU_FW_REV,
    EMU_HW_REV,
    EMU_SERIAL,
    EMU_SW_REV,
    EMU_TYPE_ID,
    ShadeResponder,
)

# The GATT identity presented. Byte-for-byte what api.py derives through
# normalize_uuid_str(); spelled out here so this module needs neither bleak
# nor Home Assistant and can be exercised on a bare Linux host.
UUID_COV_SERVICE: Final[str] = "0000fdc1-0000-1000-8000-00805f9b34fb"
UUID_DEV_SERVICE: Final[str] = "0000180a-0000-1000-8000-00805f9b34fb"
UUID_TX: Final[str] = "cafe1001-c0ff-ee01-8000-a110ca7ab1e0"

BLUEZ: Final[str] = "org.bluez"
GATT_MANAGER: Final[str] = "org.bluez.GattManager1"
ADV_MANAGER: Final[str] = "org.bluez.LEAdvertisingManager1"
GATT_SERVICE: Final[str] = "org.bluez.GattService1"
GATT_CHARACTERISTIC: Final[str] = "org.bluez.GattCharacteristic1"
ADVERTISEMENT: Final[str] = "org.bluez.LEAdvertisement1"
OBJECT_MANAGER: Final[str] = "org.freedesktop.DBus.ObjectManager"

ROOT: Final[str] = "/org/hdpv/keycapture"
COVER_SERVICE_PATH: Final[str] = f"{ROOT}/service0"
COVER_CHAR_PATH: Final[str] = f"{COVER_SERVICE_PATH}/char0"
DEV_SERVICE_PATH: Final[str] = f"{ROOT}/service1"
ADVERTISEMENT_PATH: Final[str] = f"{ROOT}/advertisement0"

# The name the PowerView app offers to adopt.
EMU_NAME: Final[str] = "myPVcover"

# Manufacturer payload, 9 bytes as the integration's own decoder expects
# (dec_manufacturer_data in api.py). Bytes 0-1 are the home_id, and zero
# there is what says "never adopted".
ADVERT_PAYLOAD: Final[bytes] = bytes(
    [0x00, 0x00, EMU_TYPE_ID, 0x00, 0x00, 0x00, 0x00, 0x00, 0xA2]
)

# Device Information characteristics, UUIDs as GATT_DEV_INFO in
# scripts/shade_report.py names them. Values match what the product-info
# reply reports, because real firmware agrees across the two.
DEVICE_INFO: Final[tuple[tuple[str, bytes], ...]] = (
    ("00002a29-0000-1000-8000-00805f9b34fb", b"Hunter Douglas"),
    ("00002a24-0000-1000-8000-00805f9b34fb", str(EMU_TYPE_ID).encode()),
    ("00002a25-0000-1000-8000-00805f9b34fb", EMU_SERIAL[::-1].hex().upper().encode()),
    ("00002a27-0000-1000-8000-00805f9b34fb", str(EMU_HW_REV).encode()),
    ("00002a26-0000-1000-8000-00805f9b34fb", str(EMU_FW_REV).encode()),
    ("00002a28-0000-1000-8000-00805f9b34fb", str(EMU_SW_REV).encode()),
)


class _Service(ServiceInterface):
    """A primary GATT service."""

    def __init__(self, uuid: str) -> None:
        """Create a primary service advertising uuid."""
        super().__init__(GATT_SERVICE)
        self._uuid = uuid

    @dbus_property(access=PropertyAccess.READ)
    def UUID(self) -> "s":
        """Return the service UUID."""
        return self._uuid

    @dbus_property(access=PropertyAccess.READ)
    def Primary(self) -> "b":
        """Return whether this is a primary service."""
        return True


class _StaticCharacteristic(ServiceInterface):
    """Read-only characteristic serving a fixed value."""

    def __init__(self, uuid: str, service_path: str, value: bytes) -> None:
        """Create a read-only characteristic under service_path."""
        super().__init__(GATT_CHARACTERISTIC)
        self._uuid = uuid
        self._service_path = service_path
        self._value = value

    @dbus_property(access=PropertyAccess.READ)
    def UUID(self) -> "s":
        """Return the characteristic UUID."""
        return self._uuid

    @dbus_property(access=PropertyAccess.READ)
    def Service(self) -> "o":
        """Return the owning service's object path."""
        return self._service_path

    @dbus_property(access=PropertyAccess.READ)
    def Flags(self) -> "as":
        """Return the characteristic's GATT flags."""
        return ["read"]

    @method()
    def ReadValue(self, options: "a{sv}") -> "ay":
        """Serve the fixed value."""
        return self._value


class _CoverCharacteristic(ServiceInterface):
    """The characteristic the PowerView protocol runs over."""

    def __init__(self, responder: ShadeResponder, captured: asyncio.Event) -> None:
        """Route writes through responder, setting captured on a key."""
        super().__init__(GATT_CHARACTERISTIC)
        self._responder = responder
        self._captured = captured
        self._notifying = False
        self._value = b""

    @dbus_property(access=PropertyAccess.READ)
    def UUID(self) -> "s":
        """Return the characteristic UUID."""
        return UUID_TX

    @dbus_property(access=PropertyAccess.READ)
    def Service(self) -> "o":
        """Return the owning service's object path."""
        return COVER_SERVICE_PATH

    @dbus_property(access=PropertyAccess.READ)
    def Flags(self) -> "as":
        """Return the characteristic's GATT flags."""
        return ["notify", "write", "write-without-response"]

    @dbus_property(access=PropertyAccess.READ)
    def Value(self) -> "ay":
        """Return the last value notified."""
        return self._value

    @method()
    def StartNotify(self) -> None:
        """Record that the peer subscribed to notifications."""
        self._notifying = True

    @method()
    def StopNotify(self) -> None:
        """Record that the peer unsubscribed."""
        self._notifying = False

    @method()
    def WriteValue(self, value: "ay", options: "a{sv}") -> None:
        """Run a written frame through the responder and notify the reply."""
        reply = self._responder.handle(bytes(value))
        if reply is None:
            return
        self._value = reply
        if self._notifying:
            self.emit_properties_changed({"Value": reply})
        if self._responder.home_key:
            self._captured.set()


class _Advertisement(ServiceInterface):
    """The LE advertisement BlueZ broadcasts on our behalf."""

    def __init__(self) -> None:
        """Create the advertisement."""
        super().__init__(ADVERTISEMENT)

    @dbus_property(access=PropertyAccess.READ)
    def Type(self) -> "s":
        """Return the advertisement type."""
        return "peripheral"

    @dbus_property(access=PropertyAccess.READ)
    def ServiceUUIDs(self) -> "as":
        """Return the service UUIDs to advertise."""
        return [UUID_COV_SERVICE]

    @dbus_property(access=PropertyAccess.READ)
    def ManufacturerData(self) -> "a{qv}":
        """Return the Hunter Douglas manufacturer payload."""
        return {MFCT_ID: Variant("ay", ADVERT_PAYLOAD)}

    @dbus_property(access=PropertyAccess.READ)
    def LocalName(self) -> "s":
        """Return the name the app will offer to adopt."""
        return EMU_NAME

    @method()
    def Release(self) -> None:
        """Handle BlueZ dropping the advertisement."""
        LOGGER.debug("keycapture: advertisement released by BlueZ")


class _Application(ServiceInterface):
    """Root object BlueZ enumerates to find our services."""

    def __init__(self, managed: dict[str, dict[str, dict[str, Variant]]]) -> None:
        """Expose the prepared object tree."""
        super().__init__(OBJECT_MANAGER)
        self._managed = managed

    @method()
    def GetManagedObjects(self) -> "a{oa{sa{sv}}}":
        """Enumerate every service and characteristic for BlueZ."""
        return self._managed


def _managed_objects() -> dict[str, dict[str, dict[str, Variant]]]:
    """Describe the object tree in the shape ObjectManager returns."""
    tree: dict[str, dict[str, dict[str, Variant]]] = {
        COVER_SERVICE_PATH: {
            GATT_SERVICE: {
                "UUID": Variant("s", UUID_COV_SERVICE),
                "Primary": Variant("b", True),
            }
        },
        COVER_CHAR_PATH: {
            GATT_CHARACTERISTIC: {
                "Service": Variant("o", COVER_SERVICE_PATH),
                "UUID": Variant("s", UUID_TX),
                "Flags": Variant(
                    "as", ["notify", "write", "write-without-response"]
                ),
            }
        },
        DEV_SERVICE_PATH: {
            GATT_SERVICE: {
                "UUID": Variant("s", UUID_DEV_SERVICE),
                "Primary": Variant("b", True),
            }
        },
    }
    for index, (uuid, _value) in enumerate(DEVICE_INFO):
        tree[f"{DEV_SERVICE_PATH}/char{index}"] = {
            GATT_CHARACTERISTIC: {
                "Service": Variant("o", DEV_SERVICE_PATH),
                "UUID": Variant("s", uuid),
                "Flags": Variant("as", ["read"]),
            }
        }
    return tree


async def async_capture_key(adapter: str, timeout: float) -> tuple[bytes, bool]:
    """Advertise as an unadopted shade until a home key is written.

    Returns the captured key and whether anything addressed us with a key
    we did not hold -- an empty key with that flag set means the identity
    is already registered in the user's home, which is a different problem
    from nothing ever connecting.
    """
    responder = ShadeResponder()
    captured = asyncio.Event()
    exported: list[str] = []

    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        bus.export(ROOT, _Application(_managed_objects()))
        bus.export(COVER_SERVICE_PATH, _Service(UUID_COV_SERVICE))
        bus.export(COVER_CHAR_PATH, _CoverCharacteristic(responder, captured))
        bus.export(DEV_SERVICE_PATH, _Service(UUID_DEV_SERVICE))
        for index, (uuid, value) in enumerate(DEVICE_INFO):
            bus.export(
                f"{DEV_SERVICE_PATH}/char{index}",
                _StaticCharacteristic(uuid, DEV_SERVICE_PATH, value),
            )
        bus.export(ADVERTISEMENT_PATH, _Advertisement())
        exported = [
            ROOT,
            COVER_SERVICE_PATH,
            COVER_CHAR_PATH,
            DEV_SERVICE_PATH,
            ADVERTISEMENT_PATH,
            *(f"{DEV_SERVICE_PATH}/char{i}" for i in range(len(DEVICE_INFO))),
        ]

        path = f"/org/bluez/{adapter}"
        introspection = await bus.introspect(BLUEZ, path)
        proxy = bus.get_proxy_object(BLUEZ, path, introspection)
        gatt: Any = proxy.get_interface(GATT_MANAGER)
        advertising: Any = proxy.get_interface(ADV_MANAGER)

        await gatt.call_register_application(ROOT, {})
        LOGGER.debug("keycapture: GATT application registered on %s", path)
        await advertising.call_register_advertisement(ADVERTISEMENT_PATH, {})
        LOGGER.info("keycapture: advertising as %s on %s", EMU_NAME, path)

        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(captured.wait(), timeout)

        with contextlib.suppress(Exception):
            await advertising.call_unregister_advertisement(ADVERTISEMENT_PATH)
        with contextlib.suppress(Exception):
            await gatt.call_unregister_application(ROOT)
    finally:
        for path_ in exported:
            with contextlib.suppress(Exception):
                bus.unexport(path_)
        bus.disconnect()

    return responder.home_key, responder.saw_foreign_traffic
