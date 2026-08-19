"""Home Assistant coordinator for Hunter Douglas PowerView (BLE) integration."""

import asyncio
from datetime import date, time as dt_time
import time
from typing import Any, Final

from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth.const import DOMAIN as BLUETOOTH_DOMAIN
from homeassistant.components.bluetooth.passive_update_coordinator import (
    PassiveBluetoothDataUpdateCoordinator,
)
from homeassistant.const import SUN_EVENT_SUNRISE, SUN_EVENT_SUNSET
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import (
    CONNECTION_BLUETOOTH,
    DeviceInfo,
    format_mac,
)
from homeassistant.helpers.sun import get_astral_event_date
from homeassistant.util import dt as dt_util

from .api import (
    POWER_TYPE_HARDWIRED,
    SHADE_TYPE,
    PowerViewBLE,
    ShadeCapability,
    decode_power_type,
    get_shade_capabilities,
)
from .const import ATTR_RSSI, CONF_HOME_KEY, DOMAIN, LOGGER


def shade_id_for(ble_device: BLEDevice) -> str:
    """Return the identity a shade keeps when its Bluetooth address changes.

    The address is not it. Shades advertise from random static addresses,
    which the specification permits a device to regenerate at every power
    cycle, and one shade has been seen under two addresses at once. Because
    the device registry entry and every entity unique ID were built from the
    address, a shade that changed it became a second device with a second set
    of entities, and the `cover.*` that dashboards and automations point at
    went stale -- the "automation and dashboard failures" of issue #42.

    The advertised local name (`SKL:C71E`, `DUE:8964`) survives the change,
    and is already what a hub's own roster is keyed by, so identity hangs off
    that instead. A shade advertising no name falls back to the address and
    keeps exactly the identifiers and unique IDs it has today.
    """
    name = ble_device.name
    if name and name != ble_device.address:
        return name
    return ble_device.address


class PVCoordinator(PassiveBluetoothDataUpdateCoordinator):
    """Update coordinator for a single PowerView shade."""

    # Firmware isn't in the advert — pull it via GATT. Retry every minute
    # while empty, then re-check daily so firmware upgrades eventually surface.
    _DEV_INFO_REFRESH_S: Final[float] = 24 * 3600
    _DEV_INFO_RETRY_S: Final[float] = 60

    # Position/battery come only from V2 adverts. Past this many seconds without
    # HA hearing from the shade at all we stop reporting the retained sample as
    # current, so an out-of-range shade doesn't keep showing a stale position.
    _STALE_AFTER_S: Final[float] = 300.0

    def __init__(
        self,
        hass: HomeAssistant,
        ble_device: BLEDevice,
        data: dict[str, Any],
        friendly_name: str | None = None,
    ) -> None:
        """Initialize the shade data coordinator."""
        # A shade with no local name in its advertisement is not something we
        # have seen, but an assert here died inside a fire-and-forget setup
        # task and took the shade with it. The address is always available.
        self._friendly_name = friendly_name or ble_device.name or ble_device.address
        self._shade_id = shade_id_for(ble_device)
        home_key_hex: str = data.get(CONF_HOME_KEY, "")
        home_key: bytes = (
            bytes.fromhex(home_key_hex) if len(home_key_hex) == 32 else b""
        )
        self.api = PowerViewBLE(ble_device, home_key)
        self.data: dict[str, int | float | bool] = {}
        self._v2_seen: bool = False
        self._no_key_logged: bool = False
        self._manuf_dat = data.get("manufacturer_data")
        self.dev_details: dict[str, str] = {}
        self.velocity: int = 0
        self._last_dev_info_at: float = 0.0
        self._dev_info_task: asyncio.Task[None] | None = None
        self._solar_day: date | None = None
        self._solar: tuple[dt_time, dt_time] | None = None
        self._power_type: int | None = None

        LOGGER.debug(
            "Initializing coordinator for %s (%s)",
            self._friendly_name,
            ble_device.address,
        )
        super().__init__(
            hass,
            LOGGER,
            ble_device.address,
            bluetooth.BluetoothScanningMode.ACTIVE,
        )

    @property
    def friendly_name(self) -> str:
        """Return the shade's resolved friendly name."""
        return self._friendly_name

    @property
    def shade_id(self) -> str:
        """Return the address-independent identity; see `shade_id_for`."""
        return self._shade_id

    @property
    def unique_id_stem(self) -> str:
        """Return `shade_id` in the form entity unique IDs have always used.

        `format_mac` returns anything that is not an address unchanged, so a
        named shade contributes its name verbatim while a nameless one keeps
        the lower-cased address its existing unique IDs were built from.
        """
        return format_mac(self._shade_id)

    @callback
    def async_retarget(self, ble_device: BLEDevice) -> None:
        """Follow this shade to a Bluetooth address it has moved to.

        Everything that identifies the shade to Home Assistant is derived from
        `shade_id`, so the device, its entities and their history all stay put
        and only the radio subscriptions move.
        """
        self.api.set_ble_device(ble_device)
        if ble_device.address == self.address:
            return

        old_address = self.address
        LOGGER.info(
            "%s moved from %s to %s",
            self._friendly_name,
            old_address,
            ble_device.address,
        )
        # Deliberately the parent's stop, which unsubscribes and nothing more.
        # This class overrides `_async_stop` to also tear down the device-info
        # task, which is what entry unload wants and a retarget does not.
        super()._async_stop()
        self.address = ble_device.address
        self._async_start()
        # Any open link is to an address that no longer answers.
        self.hass.async_create_task(self.api.disconnect())

        reg = dr.async_get(self.hass)
        device = reg.async_get_device(identifiers={(DOMAIN, self._shade_id)})
        if device is not None:
            reg.async_update_device(
                device.id,
                new_identifiers={
                    (DOMAIN, self._shade_id),
                    (BLUETOOTH_DOMAIN, self.address),
                },
                new_connections={(CONNECTION_BLUETOOTH, self.address)},
            )

    @property
    def type_id(self) -> int | None:
        """Return the shade type ID from manufacturer data or live BLE data."""
        if self._manuf_dat:
            return int(bytes.fromhex(self._manuf_dat)[2])
        live = self.data.get("type_id")
        return int(live) if live is not None else None

    @property
    def shade_capabilities(self) -> ShadeCapability:
        """Return the shade capabilities based on type ID."""
        return get_shade_capabilities(self.type_id)

    @property
    def power_type(self) -> int | None:
        """Return the probed power type, or None if it is not known."""
        return self._power_type

    @power_type.setter
    def power_type(self, value: int | None) -> None:
        """Seed the power type from the cache, skipping the GATT probe."""
        self._power_type = value

    @property
    def battery_powered(self) -> bool:
        """Whether this shade should offer battery entities.

        Biased towards yes. Only a power type with outside corroboration --
        the hardwired codes, which six shades reported while their hub agreed
        -- answers no; an unreadable, unrecognised or never-probed shade keeps
        its battery entities. A spurious sensor on a mains shade is a smaller
        failure than a missing one on a battery shade, and the entities are
        disabled rather than withheld either way, so a wrong answer here is
        one the owner can undo.
        """
        return self._power_type not in POWER_TYPE_HARDWIRED

    async def query_power_type(self) -> int | None:
        """Probe the power type over GATT, returning None if it cannot be read.

        Never raises: this is an unsolicited extra on top of setup, and a
        shade too weak to answer must still get its entities.
        """
        try:
            payload = await self.api.query_power_status()
        except (BleakError, TimeoutError) as ex:
            LOGGER.debug("%s: power status query failed: %s", self.name, ex)
            return None
        self._power_type = decode_power_type(payload)
        LOGGER.debug(
            "%s: power status %s, power type %s",
            self.name,
            payload.hex(" "),
            self._power_type,
        )
        return self._power_type

    @property
    def data_available(self) -> bool:
        """Whether the retained V2 sample is recent enough to trust.

        Position, tilt and battery are only carried in V2 adverts, so an
        out-of-range shade must stop reporting its retained sample as current.

        Age is measured against Home Assistant's own last-seen time, not
        against when we were last handed an advertisement: habluetooth drops
        an advertisement identical to the previous one before any listener
        sees it, and a stationary shade repeats the same nine bytes
        indefinitely, so a perfectly healthy shade can go hours without
        dispatching a single event. Timing our own events instead declared
        every idle shade stale after five minutes.
        """
        if not self._v2_seen:
            return False  # nothing decoded yet, so there is no sample to age
        info = bluetooth.async_last_service_info(
            self.hass, self.address, connectable=True
        )
        # Same monotonic clock as time.monotonic(), just the coarse variant.
        return info is not None and time.monotonic() - info.time < self._STALE_AFTER_S

    def _warn_if_no_key(self) -> None:
        """Say once why an encrypted shade will not take commands.

        Nothing else reports it. The cover entities answer an encrypted shade
        with no key by returning an empty feature set, which renders as a shade
        with no controls at all and leaves nothing in the log to explain it.
        """
        if self._no_key_logged or not self.api.encrypted or self.api.has_key:
            return
        self._no_key_logged = True
        LOGGER.warning(
            "%s is encrypted and no home key is configured, so it cannot be "
            "controlled. Add the key from the PowerView Home integration's "
            "Reconfigure menu",
            self._friendly_name,
        )

    async def query_dev_info(self) -> None:
        """Fetch device info over GATT and push into the device registry.

        The HA device registry does not re-read DeviceInfo when the underlying
        data changes, so we update it explicitly when new details arrive.
        """
        LOGGER.debug("%s: querying device info", self.name)
        self._last_dev_info_at = time.monotonic()
        new = await self.api.query_dev_info()
        if new and new != self.dev_details:
            self.dev_details.update(new)
            self._push_device_registry_update()

    def _push_device_registry_update(self) -> None:
        reg = dr.async_get(self.hass)
        device = reg.async_get_device(identifiers={(DOMAIN, self._shade_id)})
        if device is None:
            return
        reg.async_update_device(
            device.id,
            sw_version=self.dev_details.get("sw_rev"),
            hw_version=self.dev_details.get("hw_rev"),
            serial_number=self.dev_details.get("serial_nr"),
        )

    async def _refresh_dev_info_safe(self) -> None:
        try:
            await self.query_dev_info()
        except (BleakError, TimeoutError) as ex:
            LOGGER.debug("%s: dev_info refresh failed: %s", self.name, ex)

    def _solar_times(self) -> tuple[dt_time, dt_time] | None:
        """Return today's sunrise and sunset as local wall-clock times.

        Cached per day. The shade's solar payload carries no date, so these
        only need recomputing when the date rolls over -- which matters
        because this runs on every advertisement, several a second while a
        shade is moving.

        None where the sun does not both rise and set, which astral reports
        as a missing event and which is a real case above the Arctic circle.
        """
        today = dt_util.now().date()
        if self._solar_day == today:
            return self._solar
        self._solar_day = today
        sunrise = get_astral_event_date(self.hass, SUN_EVENT_SUNRISE, today)
        sunset = get_astral_event_date(self.hass, SUN_EVENT_SUNSET, today)
        self._solar = (
            None
            if sunrise is None or sunset is None
            else (dt_util.as_local(sunrise).time(), dt_util.as_local(sunset).time())
        )
        return self._solar

    def _maybe_refresh_dev_info(self) -> None:
        """Schedule a background dev_info refresh if stale."""
        if self._dev_info_task is not None and not self._dev_info_task.done():
            return
        interval = (
            self._DEV_INFO_REFRESH_S
            if self.dev_details.get("sw_rev")
            else self._DEV_INFO_RETRY_S
        )
        if time.monotonic() - self._last_dev_info_at < interval:
            return
        self._dev_info_task = self.hass.async_create_background_task(
            self._refresh_dev_info_safe(), name=f"pvble_dev_info_{self.address}"
        )

    @property
    def device_info(self) -> DeviceInfo:
        """Return detailed device information for GUI."""
        LOGGER.debug("%s: device_info, %s", self._friendly_name, self.dev_details)
        return DeviceInfo(
            identifiers={
                (DOMAIN, self._shade_id),
                (BLUETOOTH_DOMAIN, self.address),
            },
            connections={(CONNECTION_BLUETOOTH, self.address)},
            name=self._friendly_name,
            configuration_url=None,
            manufacturer="Hunter Douglas",
            model=(
                str(SHADE_TYPE.get(self.type_id, "unknown"))
                if self.type_id is not None
                else None
            ),
            model_id=(str(self.type_id) if self.type_id is not None else None),
            serial_number=self.dev_details.get("serial_nr"),
            sw_version=self.dev_details.get("sw_rev"),
            hw_version=self.dev_details.get("hw_rev"),
        )

    @property
    def device_present(self) -> bool:
        """Check if a device is present."""
        return bluetooth.async_address_present(
            self.hass, self.address, connectable=True
        )

    def _async_stop(self) -> None:
        """Shutdown coordinator and any connection."""
        LOGGER.debug("%s: shutting down shade", self.name)
        if self._dev_info_task is not None and not self._dev_info_task.done():
            self._dev_info_task.cancel()
        self.hass.async_create_task(self.api.disconnect())
        super()._async_stop()

    @callback
    def _async_handle_bluetooth_event(
        self,
        service_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        """Handle a Bluetooth event."""

        LOGGER.debug("BLE event %s: %s", change, service_info.manufacturer_data)
        self.api.set_ble_device(service_info.device)

        # Merge onto the retained sample rather than replacing it: an advert
        # without a valid V2 record (wrong length / legacy format / 2073
        # absent) decodes to {} and must not wipe the last-known position,
        # tilt and battery state.
        new_data: dict[str, int | float | bool] = dict(self.data)
        new_data[ATTR_RSSI] = service_info.rssi
        if change == bluetooth.BluetoothChange.ADVERTISEMENT:
            decoded = self.api.dec_manufacturer_data(
                bytearray(service_info.manufacturer_data.get(2073, b""))
            )
            if decoded:
                new_data.update(decoded)
                self.api.encrypted = bool(decoded.get("home_id"))
                self._warn_if_no_key()
                # Drives whether the next connection carries a clock update,
                # and what solar times ride along with it.
                self.api.clock_reset = bool(decoded.get("reset_clock"))
                self.api.solar = self._solar_times()
                self._v2_seen = True
                self._maybe_refresh_dev_info()

        if new_data != self.data:
            self.data = new_data
            LOGGER.debug("data sample %s", self.data)
        # Handed on even when nothing changed: the super() call is the only
        # thing that marks the coordinator available again, so a shade coming
        # back with exactly the advertisement it left on must not be skipped.
        super()._async_handle_bluetooth_event(service_info, change)
