"""Platform for sensor integration."""

from datetime import timedelta
from typing import Any

from bleak.exc import BleakError

from homeassistant.components.bluetooth.passive_update_coordinator import (
    PassiveBluetoothCoordinatorEntity,
)
from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.components.sensor.const import SensorDeviceClass, SensorStateClass
from homeassistant.const import (
    ATTR_BATTERY_CHARGING,
    ATTR_BATTERY_LEVEL,
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ConfigEntryType, async_setup_shade_platform
from .api import decode_power_type
from .const import ATTR_POWER_STATUS, ATTR_RSSI, DOMAIN, LOGGER
from .coordinator import PVCoordinator

# The raw power-status sensor is the one entity here that polls over GATT
# instead of riding advertisements. Poll it slowly -- a shade that reports its
# own battery is usually on battery, and an active connection costs more than a
# passive listen -- and serialize the probes so several shades don't open BLE
# connections at once. Neither constant touches the advert-derived sensors,
# which keep should_poll False.
SCAN_INTERVAL = timedelta(hours=2)
PARALLEL_UPDATES = 1

SENSOR_TYPES: list[SensorEntityDescription] = [
    SensorEntityDescription(
        key=ATTR_BATTERY_LEVEL,
        translation_key=ATTR_BATTERY_LEVEL,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.BATTERY,
    ),
    SensorEntityDescription(
        key=ATTR_RSSI,
        translation_key=ATTR_RSSI,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
]


def _add_entities(
    coordinator: PVCoordinator, async_add_entities: AddEntitiesCallback
) -> None:
    """Create sensor entities for a single shade coordinator."""
    stem = coordinator.unique_id_stem
    entities: list[SensorEntity] = [
        PVSensor(coordinator, descr, stem) for descr in SENSOR_TYPES
    ]
    entities.append(PVPowerStatusSensor(coordinator, stem))
    async_add_entities(entities)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntryType,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add sensors for passed config_entry in Home Assistant."""
    async_setup_shade_platform(hass, config_entry, async_add_entities, _add_entities)


class PVSensor(PassiveBluetoothCoordinatorEntity[PVCoordinator], SensorEntity):  # type: ignore[reportIncompatibleMethodOverride]
    """The generic shade sensor implementation."""

    _attr_has_entity_name = True

    def __init__(
        self, pv_dev: PVCoordinator, descr: SensorEntityDescription, unique_id: str
    ) -> None:
        """Initialize the shade sensor."""
        self._attr_unique_id = f"{DOMAIN}-{unique_id}-{descr.key}"
        self._attr_device_info = pv_dev.device_info
        self.entity_description = descr
        if descr.key == ATTR_BATTERY_LEVEL and not pv_dev.battery_powered:
            # Disabled rather than withheld: the power type is read from one
            # GATT byte and can be wrong, so the owner of a shade we called
            # mains-powered can simply switch its battery sensor back on.
            # Only ever set for the battery key -- RSSI carries its own
            # disabled-by-default flag on the description.
            self._attr_entity_registry_enabled_default = False
        super().__init__(pv_dev)

    @property
    def native_value(self) -> int | float | None:  # type: ignore[reportIncompatibleVariableOverride]
        """Return the sensor value.

        RSSI refreshes on every advert, so it is always reported. V2-derived
        values (e.g. battery) read as unknown once the advert data is stale.
        """
        if (
            self.entity_description.key != ATTR_RSSI
            and not self.coordinator.data_available
        ):
            return None
        return self.coordinator.data.get(self.entity_description.key)


class PVPowerStatusSensor(
    PassiveBluetoothCoordinatorEntity[PVCoordinator], SensorEntity
):  # type: ignore[reportIncompatibleMethodOverride]
    """The raw 0xFFDE power-status reply, polled over GATT.

    Disabled by default and diagnostic. Unlike every other entity here it is
    not advert-derived: it opens a GATT connection on SCAN_INTERVAL and reports
    the reply byte-for-byte, so battery-shade owners can record it across a
    discharge while the encoding is still being worked out (issue #75). A
    probe that fails reads unknown, as an out-of-range shade does.
    """

    _attr_has_entity_name = True
    _attr_translation_key = ATTR_POWER_STATUS
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coord: PVCoordinator, unique_id: str) -> None:
        """Initialize the raw power-status sensor."""
        self._attr_unique_id = f"{DOMAIN}-{unique_id}-{ATTR_POWER_STATUS}"
        self._attr_device_info = coord.device_info
        self._payload: bytes | None = None
        super().__init__(coord)

    @property
    def should_poll(self) -> bool:  # type: ignore[reportIncompatibleVariableOverride]
        """Poll this one entity; the rest of the integration stays passive."""
        return True

    async def async_update(self) -> None:
        """Read the raw 0xFFDE reply, caching None when it can't be read."""
        api = self.coordinator.api
        if api.encrypted and not api.has_key:
            self._payload = None
            return
        try:
            self._payload = await api.query_power_status()
        except (BleakError, TimeoutError) as ex:
            LOGGER.debug(
                "%s: power status poll failed: %s", self.coordinator.friendly_name, ex
            )
            self._payload = None

    @property
    def native_value(self) -> str | None:  # type: ignore[reportIncompatibleVariableOverride]
        """Return the reply as space-separated hex, or None if unread."""
        return self._payload.hex(" ") if self._payload else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Break the reply into per-byte values plus a decoded power type.

        The state string already carries every byte; these are for reading the
        changing bytes (b2/b3 on the type 8 sample in issue #75) off the
        attributes without re-splitting the hex.
        """
        if not self._payload:
            return None
        attrs: dict[str, Any] = {
            "length": len(self._payload),
            **{f"b{idx}": value for idx, value in enumerate(self._payload)},
        }
        if (power_type := decode_power_type(self._payload)) is not None:
            attrs["decoded_power_type"] = power_type
        # The advertised battery level sits beside the raw bytes so one recorder
        # row pins b2/b3 to the code the shade was reporting at the same moment
        # -- the correlation issue #75 needs (is b2 near 50 when the advert drops
        # to code 2?) without time-joining two entities' histories. Only while
        # the advert is fresh; a stale sample would mislabel the reply.
        if self.coordinator.data_available:
            data = self.coordinator.data
            if (battery := data.get(ATTR_BATTERY_LEVEL)) is not None:
                attrs["advertised_battery"] = battery
            attrs[ATTR_BATTERY_CHARGING] = bool(data.get(ATTR_BATTERY_CHARGING))
        return attrs
