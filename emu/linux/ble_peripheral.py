"""Minimal BlueZ D-Bus GATT-server and LE-advertising framework.

A small, self-contained port of the pattern BlueZ ships as
``test/example-gatt-server`` and ``test/example-advertisement``: enough
GATT server and advertising boilerplate to stand up a BLE peripheral
against ``bluetoothd`` over D-Bus, with no extra Python dependencies
beyond ``dbus-python`` and ``PyGObject`` (both ship as system packages
on any BlueZ-based distro).

This module has no PowerView-specific knowledge; see shade_emulator.py
for that.

AUTHOR: BlueZ project authors (test/example-gatt-server, test/example-advertisement)
LICENSE: GPLv2, see ../README.md
"""

from __future__ import annotations

import logging

import dbus
import dbus.exceptions
import dbus.mainloop.glib
import dbus.service

LOGGER = logging.getLogger("ble_peripheral")

BLUEZ_SERVICE_NAME = "org.bluez"
DBUS_OM_IFACE = "org.freedesktop.DBus.ObjectManager"
DBUS_PROP_IFACE = "org.freedesktop.DBus.Properties"

GATT_MANAGER_IFACE = "org.bluez.GattManager1"
GATT_SERVICE_IFACE = "org.bluez.GattService1"
GATT_CHRC_IFACE = "org.bluez.GattCharacteristic1"

LE_ADVERTISING_MANAGER_IFACE = "org.bluez.LEAdvertisingManager1"
LE_ADVERTISEMENT_IFACE = "org.bluez.LEAdvertisement1"


class InvalidArgsException(dbus.exceptions.DBusException):
    """Raised for a GetAll() on an interface this object doesn't have."""

    _dbus_error_name = "org.freedesktop.DBus.Error.InvalidArgs"


def find_adapter(bus: dbus.Bus, manager_iface: str, preferred: str | None) -> str:
    """Return the object path of the first adapter exposing manager_iface."""
    remote_om = dbus.Interface(
        bus.get_object(BLUEZ_SERVICE_NAME, "/"), DBUS_OM_IFACE
    )
    objects = remote_om.GetManagedObjects()

    adapter_seen = False
    for path, interfaces in objects.items():
        if preferred is not None and not path.endswith("/" + preferred):
            continue
        if "org.bluez.Adapter1" in interfaces:
            adapter_seen = True
        if manager_iface in interfaces:
            return path

    if preferred is not None and not adapter_seen:
        msg = f"no such Bluetooth adapter: {preferred}"
    else:
        adapter = f" ({preferred})" if preferred else ""
        msg = f"no BlueZ adapter{adapter} exposes {manager_iface}"
    raise RuntimeError(msg)


class Application(dbus.service.Object):
    """Root GATT application object BlueZ enumerates via ObjectManager."""

    def __init__(self, bus: dbus.Bus) -> None:
        """Create the application object and export it on bus."""
        self.path = "/org/hdpv/emu"
        self.services: list[Service] = []
        dbus.service.Object.__init__(self, bus, self.path)

    def get_path(self) -> dbus.ObjectPath:
        """Return this application's D-Bus object path."""
        return dbus.ObjectPath(self.path)

    def add_service(self, service: Service) -> None:
        """Register a GATT service under this application."""
        self.services.append(service)

    @dbus.service.method(DBUS_OM_IFACE, out_signature="a{oa{sa{sv}}}")
    def GetManagedObjects(self):
        """Enumerate every service and characteristic for BlueZ."""
        response = {}
        for service in self.services:
            response[service.get_path()] = service.get_properties()
            for chrc in service.characteristics:
                response[chrc.get_path()] = chrc.get_properties()
        return response


class Service(dbus.service.Object):
    """A GATT primary service exposed over org.bluez.GattService1."""

    PATH_BASE = "/org/hdpv/emu/service"

    def __init__(self, bus: dbus.Bus, index: int, uuid: str) -> None:
        """Create service index with the given 128-bit uuid and export it on bus."""
        self.path = f"{self.PATH_BASE}{index}"
        self.uuid = uuid
        self.characteristics: list[Characteristic] = []
        dbus.service.Object.__init__(self, bus, self.path)

    def get_path(self) -> dbus.ObjectPath:
        """Return this service's D-Bus object path."""
        return dbus.ObjectPath(self.path)

    def add_characteristic(self, characteristic: Characteristic) -> None:
        """Register a characteristic under this service."""
        self.characteristics.append(characteristic)

    def get_properties(self) -> dict:
        """Return the org.bluez.GattService1 property dict."""
        return {
            GATT_SERVICE_IFACE: {
                "UUID": self.uuid,
                "Primary": True,
                "Characteristics": dbus.Array(
                    [c.get_path() for c in self.characteristics], signature="o"
                ),
            }
        }

    @dbus.service.method(DBUS_PROP_IFACE, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface: str):
        """Return this service's properties for the given interface."""
        if interface != GATT_SERVICE_IFACE:
            raise InvalidArgsException
        return self.get_properties()[GATT_SERVICE_IFACE]


class Characteristic(dbus.service.Object):
    """A GATT characteristic exposed over org.bluez.GattCharacteristic1.

    Subclasses override read_value()/write_value() for the interesting
    behaviour; BlueZ auto-creates the CCCD descriptor for a
    'notify'-flagged characteristic, so there is nothing to do here to
    get notifications subscribed to.
    """

    PATH_BASE = "/char"

    def __init__(
        self, bus: dbus.Bus, index: int, uuid: str, flags: list[str], service: Service
    ) -> None:
        """Create characteristic index of service with the given uuid and flags."""
        self.path = service.path + self.PATH_BASE + str(index)
        self.uuid = uuid
        self.service = service
        self.flags = flags
        self.notifying = False
        self.value: list[int] = []
        dbus.service.Object.__init__(self, bus, self.path)

    def get_path(self) -> dbus.ObjectPath:
        """Return this characteristic's D-Bus object path."""
        return dbus.ObjectPath(self.path)

    def get_properties(self) -> dict:
        """Return the org.bluez.GattCharacteristic1 property dict."""
        return {
            GATT_CHRC_IFACE: {
                "Service": self.service.get_path(),
                "UUID": self.uuid,
                "Flags": self.flags,
                "Notifying": self.notifying,
            }
        }

    @dbus.service.method(DBUS_PROP_IFACE, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface: str):
        """Return this characteristic's properties for the given interface."""
        if interface != GATT_CHRC_IFACE:
            raise InvalidArgsException
        return self.get_properties()[GATT_CHRC_IFACE]

    @dbus.service.method(GATT_CHRC_IFACE, in_signature="a{sv}", out_signature="ay")
    def ReadValue(self, options):
        """Handle a GATT read request; delegates to read_value()."""
        return dbus.Array(self.read_value(dict(options)), signature="y")

    @dbus.service.method(GATT_CHRC_IFACE, in_signature="aya{sv}")
    def WriteValue(self, value, options):
        """Handle a GATT write request; delegates to write_value()."""
        self.write_value(bytes(value), dict(options))

    @dbus.service.method(GATT_CHRC_IFACE)
    def StartNotify(self):
        """Handle a client subscribing to notifications."""
        self.notifying = True

    @dbus.service.method(GATT_CHRC_IFACE)
    def StopNotify(self):
        """Handle a client unsubscribing from notifications."""
        self.notifying = False

    @dbus.service.signal(DBUS_PROP_IFACE, signature="sa{sv}as")
    def PropertiesChanged(self, interface, changed, invalidated):
        """D-Bus signal BlueZ turns into a GATT notification."""

    def notify(self, value: bytes) -> None:
        """Push value out as a GATT notification, if a client subscribed."""
        self.value = list(value)
        if not self.notifying:
            LOGGER.debug("not notifying %s: no subscriber", self.uuid)
            return
        self.PropertiesChanged(
            GATT_CHRC_IFACE, {"Value": dbus.Array(self.value, signature="y")}, []
        )

    def read_value(self, options: dict) -> list[int]:
        """Return the bytes for a read request; override for static values."""
        return self.value

    def write_value(self, value: bytes, options: dict) -> None:
        """Handle written bytes; override to react to writes."""
        self.value = list(value)


class Advertisement(dbus.service.Object):
    """An LE advertisement exposed over org.bluez.LEAdvertisement1."""

    PATH_BASE = "/org/hdpv/emu/advertisement"

    def __init__(self, bus: dbus.Bus, index: int) -> None:
        """Create advertisement index and export it on bus."""
        self.path = f"{self.PATH_BASE}{index}"
        self.service_uuids: list[str] = []
        self.manufacturer_data = dbus.Dictionary({}, signature="qv")
        self.local_name: str | None = None
        dbus.service.Object.__init__(self, bus, self.path)

    def get_path(self) -> dbus.ObjectPath:
        """Return this advertisement's D-Bus object path."""
        return dbus.ObjectPath(self.path)

    def add_service_uuid(self, uuid: str) -> None:
        """Advertise a 128-bit service UUID."""
        self.service_uuids.append(uuid)

    def add_manufacturer_data(self, company_id: int, data: bytes) -> None:
        """Set the manufacturer-specific AD payload for company_id."""
        self.manufacturer_data[company_id] = dbus.Array(data, signature="y")

    def get_properties(self) -> dict:
        """Return the org.bluez.LEAdvertisement1 property dict."""
        properties: dict = {"Type": "peripheral"}
        if self.service_uuids:
            properties["ServiceUUIDs"] = dbus.Array(self.service_uuids, signature="s")
        if self.manufacturer_data:
            properties["ManufacturerData"] = self.manufacturer_data
        if self.local_name is not None:
            properties["LocalName"] = dbus.String(self.local_name)
        return {LE_ADVERTISEMENT_IFACE: properties}

    @dbus.service.method(DBUS_PROP_IFACE, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface: str):
        """Return this advertisement's properties for the given interface."""
        if interface != LE_ADVERTISEMENT_IFACE:
            raise InvalidArgsException
        return self.get_properties()[LE_ADVERTISEMENT_IFACE]

    @dbus.service.method(LE_ADVERTISEMENT_IFACE, in_signature="", out_signature="")
    def Release(self):
        """Handle BlueZ releasing this advertisement (e.g. adapter reset)."""
        LOGGER.info("advertisement released by BlueZ")
