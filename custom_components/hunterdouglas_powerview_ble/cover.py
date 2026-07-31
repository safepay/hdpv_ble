"""Hunter Douglas Powerview cover."""

from typing import Any, Final

from bleak.exc import BleakError

from homeassistant.components.bluetooth.passive_update_coordinator import (
    PassiveBluetoothCoordinatorEntity,
)
from homeassistant.components.cover import (
    ATTR_CURRENT_POSITION,
    ATTR_CURRENT_TILT_POSITION,
    ATTR_POSITION,
    ATTR_TILT_POSITION,
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ConfigEntryType, async_setup_shade_platform
from .api import CLOSED_POSITION, OPEN_POSITION
from .const import DOMAIN, LOGGER
from .coordinator import PVCoordinator


def _add_entities(
    coordinator: PVCoordinator, async_add_entities: AddEntitiesCallback
) -> None:
    """Create cover entities for a single shade coordinator."""
    caps = coordinator.shade_capabilities

    if caps.tilt_only:
        entities: list[PowerViewCover] = [PowerViewCoverTiltOnly(coordinator)]
    elif caps.is_tilt_on_closed:
        entities = [PowerViewCoverTiltOnClosed(coordinator)]
    elif caps.has_tilt:
        entities = [PowerViewCoverTilt(coordinator)]
    elif caps.is_top_down:
        entities = [PowerViewCoverTopDown(coordinator)]
    elif caps.is_tdbu:
        entities = [
            PowerViewCoverTDBUBottom(coordinator),
            PowerViewCoverTDBUTop(coordinator),
        ]
    else:
        entities = [PowerViewCover(coordinator)]

    async_add_entities(entities)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntryType,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the cover platform."""
    async_setup_shade_platform(hass, config_entry, async_add_entities, _add_entities)


class PowerViewCover(PassiveBluetoothCoordinatorEntity[PVCoordinator], CoverEntity):  # type: ignore[reportIncompatibleVariableOverride]
    """Representation of a PowerView shade with Up/Down functionality only."""

    _attr_has_entity_name = True
    _attr_device_class = CoverDeviceClass.SHADE
    _attr_supported_features = (
        CoverEntityFeature.OPEN
        | CoverEntityFeature.CLOSE
        | CoverEntityFeature.SET_POSITION
        | CoverEntityFeature.STOP
    )

    def __init__(
        self,
        coordinator: PVCoordinator,
    ) -> None:
        """Initialize the shade."""
        LOGGER.debug("%s: init() PowerViewCover", coordinator.name)
        self._attr_name = None
        self._coord: PVCoordinator = coordinator
        self._attr_device_info = self._coord.device_info
        self._target_position: int | None = round(
            self._coord.data.get(ATTR_CURRENT_POSITION, OPEN_POSITION)
        )
        self._attr_unique_id = (
            f"{DOMAIN}_{format_mac(self._coord.address)}_{CoverDeviceClass.SHADE}"
        )
        super().__init__(coordinator)

    @property
    def is_opening(self) -> bool | None:  # type: ignore[reportIncompatibleVariableOverride]
        """Return if the cover is opening or not."""
        if not self._coord.data_available:
            return None
        return bool(self._coord.data.get("is_opening")) or (
            isinstance(self._target_position, int)
            and isinstance(self.current_cover_position, int)
            and self._target_position > self.current_cover_position
            and self._coord.api.is_connected
        )

    @property
    def is_closing(self) -> bool | None:  # type: ignore[reportIncompatibleVariableOverride]
        """Return if the cover is closing or not."""
        if not self._coord.data_available:
            return None
        return bool(self._coord.data.get("is_closing")) or (
            isinstance(self._target_position, int)
            and isinstance(self.current_cover_position, int)
            and self._target_position < self.current_cover_position
            and self._coord.api.is_connected
        )

    @property
    def is_closed(self) -> bool:  # type: ignore[reportIncompatibleVariableOverride]
        """Return if the cover is closed."""
        return self.current_cover_position == CLOSED_POSITION

    @property
    def supported_features(self) -> CoverEntityFeature:  # type: ignore[reportIncompatibleVariableOverride]
        """Flag supported features, disable control if encryption is needed."""
        if self._coord.data.get("home_id") and not self._coord.api.has_key:
            return CoverEntityFeature(0)

        return super().supported_features

    @property
    def current_cover_position(self) -> int | None:  # type: ignore[reportIncompatibleVariableOverride]
        """Return current position of cover.

        None is unknown, 0 is closed, 100 is fully open.
        """
        return self._fresh_position(ATTR_CURRENT_POSITION)

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Move the cover to a specific position."""
        target_position: Final = kwargs.get(ATTR_POSITION)
        if target_position is not None:
            LOGGER.debug("set cover to position %f", target_position)
            if self.current_cover_position == round(target_position) and not (
                self.is_closing or self.is_opening
            ):
                return
            self._target_position = round(target_position)
            try:
                await self._coord.api.set_position(
                    round(target_position),
                    velocity=self._coord.velocity,
                )
                self.async_write_ha_state()
            except BleakError as err:
                LOGGER.error(
                    "Failed to move cover '%s' to %f%%: %s",
                    self.name,
                    target_position,
                    err,
                )

    def _reset_target_position(self) -> None:
        self._target_position = None

    def _fresh_position(self, key: str) -> int | None:
        """Return the rounded ``key`` position, or None if stale/absent."""
        if not self._coord.data_available:
            return None
        pos = self._coord.data.get(key)
        return round(pos) if pos is not None else None

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open the cover."""
        LOGGER.debug("open cover")
        if self.current_cover_position == OPEN_POSITION:
            return
        try:
            self._target_position = OPEN_POSITION
            await self._coord.api.open(velocity=self._coord.velocity)
            self.async_write_ha_state()
        except BleakError as err:
            LOGGER.error("Failed to open cover '%s': %s", self.name, err)
            self._reset_target_position()

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close the cover tilt."""
        LOGGER.debug("close cover")
        if self.current_cover_position == CLOSED_POSITION:
            return
        try:
            self._target_position = CLOSED_POSITION
            await self._coord.api.close(velocity=self._coord.velocity)
            self.async_write_ha_state()
        except BleakError as err:
            LOGGER.error("Failed to close cover '%s': %s", self.name, err)
            self._reset_target_position()

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Stop the cover."""
        LOGGER.debug("stop cover")
        try:
            await self._coord.api.stop()
            self._reset_target_position()
            self.async_write_ha_state()
        except BleakError as err:
            LOGGER.error("Failed to stop cover '%s': %s", self.name, err)


class PowerViewCoverTilt(PowerViewCover):
    """Representation of a PowerView shade with additional tilt functionality."""

    _attr_supported_features = (
        CoverEntityFeature.OPEN
        | CoverEntityFeature.CLOSE
        | CoverEntityFeature.STOP
        | CoverEntityFeature.SET_POSITION
        | CoverEntityFeature.OPEN_TILT
        | CoverEntityFeature.CLOSE_TILT
        | CoverEntityFeature.STOP_TILT
        | CoverEntityFeature.SET_TILT_POSITION
    )

    def __init__(
        self,
        coordinator: PVCoordinator,
    ) -> None:
        """Initialize the shade with tilt."""
        LOGGER.debug("%s: init() PowerViewCoverTilt", coordinator.name)
        super().__init__(coordinator)

    @property
    def current_cover_tilt_position(self) -> int | None:  # type: ignore[reportIncompatibleVariableOverride]
        """Return current tilt of cover.

        None is unknown
        """
        return self._fresh_position(ATTR_CURRENT_TILT_POSITION)

    async def async_set_cover_tilt_position(self, **kwargs: Any) -> None:
        """Move the tilt to a specific position."""

        if isinstance(target_position := kwargs.get(ATTR_TILT_POSITION), int):
            LOGGER.debug("set cover tilt to position %i", target_position)
            if (
                self.current_cover_tilt_position == round(target_position)
                or self.current_cover_position is None
            ):
                return

            try:
                await self._coord.api.set_position(
                    self.current_cover_position,
                    tilt=target_position,
                    velocity=self._coord.velocity,
                )
                self.async_write_ha_state()
            except BleakError as err:
                LOGGER.error(
                    "Failed to tilt cover '%s' to %f%%: %s",
                    self.name,
                    target_position,
                    err,
                )

    async def async_stop_cover_tilt(self, **kwargs: Any) -> None:
        """Stop the cover."""
        await self.async_stop_cover(**kwargs)

    async def async_open_cover_tilt(self, **kwargs: Any) -> None:
        """Open the cover tilt."""
        LOGGER.debug("open cover tilt")
        _kwargs = {**kwargs, ATTR_TILT_POSITION: OPEN_POSITION}
        await self.async_set_cover_tilt_position(**_kwargs)

    async def async_close_cover_tilt(self, **kwargs: Any) -> None:
        """Close the cover tilt."""
        LOGGER.debug("close cover tilt")
        _kwargs = {**kwargs, ATTR_TILT_POSITION: CLOSED_POSITION}
        await self.async_set_cover_tilt_position(**_kwargs)


class PowerViewCoverTiltOnClosed(PowerViewCoverTilt):
    """Representation of a PowerView shade whose tilt is only available when closed.

    Examples: Bottom Up 90° (type 18), Twist (type 44).

    If a tilt command arrives while the shade is open, the shade is closed first
    so the tilt mechanism is engaged before the command is sent.
    """

    def __init__(self, coordinator: PVCoordinator) -> None:
        """Initialize the shade."""
        LOGGER.debug("%s: init() PowerViewCoverTiltOnClosed", coordinator.name)
        super().__init__(coordinator)

    async def async_set_cover_tilt_position(self, **kwargs: Any) -> None:
        """Move the tilt to a specific position, closing first if needed."""
        if self.current_cover_position != CLOSED_POSITION:
            LOGGER.debug("tilt-on-closed: closing shade before tilting")
            try:
                self._target_position = CLOSED_POSITION
                await self._coord.api.close(velocity=self._coord.velocity)
                self.async_write_ha_state()
            except BleakError as err:
                LOGGER.error(
                    "Failed to close cover '%s' before tilt: %s", self.name, err
                )
                self._reset_target_position()
            return
        await super().async_set_cover_tilt_position(**kwargs)


class PowerViewCoverTopDown(PowerViewCover):
    """Representation of a top-down PowerView shade.

    The device position axis is inverted: device 0 = open (fabric retracted),
    device 100 = closed (fabric fully extended). We translate at the boundary
    so HA's standard 0=closed / 100=open convention is preserved.
    """

    @property
    def current_cover_position(self) -> int | None:  # type: ignore[reportIncompatibleVariableOverride]
        """Return current position, inverting the device axis."""
        pos = self._fresh_position(ATTR_CURRENT_POSITION)
        return OPEN_POSITION - pos if pos is not None else None

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Move the cover to a specific position, inverting for the device."""
        target_position: Final = kwargs.get(ATTR_POSITION)
        if target_position is not None:
            inverted = OPEN_POSITION - round(target_position)
            LOGGER.debug(
                "set top-down cover to position %f (device %i)",
                target_position,
                inverted,
            )
            if self.current_cover_position == round(target_position) and not (
                self.is_closing or self.is_opening
            ):
                return
            self._target_position = round(target_position)
            try:
                await self._coord.api.set_position(
                    inverted,
                    velocity=self._coord.velocity,
                )
                self.async_write_ha_state()
            except BleakError as err:
                LOGGER.error(
                    "Failed to move cover '%s' to %f%%: %s",
                    self.name,
                    target_position,
                    err,
                )

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open the cover (send device position 0)."""
        LOGGER.debug("open top-down cover")
        if self.current_cover_position == OPEN_POSITION:
            return
        try:
            self._target_position = OPEN_POSITION
            await self._coord.api.set_position(
                CLOSED_POSITION, velocity=self._coord.velocity
            )
            self.async_write_ha_state()
        except BleakError as err:
            LOGGER.error("Failed to open cover '%s': %s", self.name, err)
            self._reset_target_position()

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close the cover (send device position 100)."""
        LOGGER.debug("close top-down cover")
        if self.current_cover_position == CLOSED_POSITION:
            return
        try:
            self._target_position = CLOSED_POSITION
            await self._coord.api.set_position(
                OPEN_POSITION, velocity=self._coord.velocity
            )
            self.async_write_ha_state()
        except BleakError as err:
            LOGGER.error("Failed to close cover '%s': %s", self.name, err)
            self._reset_target_position()


class PowerViewCoverTiltOnly(PowerViewCoverTilt):
    """Representation of a PowerView shade with additional tilt functionality."""

    OPENCLOSED_THRESHOLD = 5

    _attr_device_class = CoverDeviceClass.BLIND
    _attr_supported_features = (
        CoverEntityFeature.OPEN_TILT
        | CoverEntityFeature.CLOSE_TILT
        | CoverEntityFeature.STOP_TILT
        | CoverEntityFeature.SET_TILT_POSITION
    )

    def __init__(
        self,
        coordinator: PVCoordinator,
    ) -> None:
        """Initialize the shade with tilt only."""
        LOGGER.debug("%s: init() PowerViewCoverTiltOnly", coordinator.name)
        super().__init__(coordinator)

    @property
    def is_opening(self) -> bool | None:  # type: ignore[reportIncompatibleVariableOverride]
        """Return if the cover is opening or not."""
        return False

    @property
    def is_closing(self) -> bool | None:  # type: ignore[reportIncompatibleVariableOverride]
        """Return if the cover is closing or not."""
        return False

    @property
    def is_closed(self) -> bool:  # type: ignore[reportIncompatibleVariableOverride]
        """Return if the cover is closed."""
        return isinstance(self.current_cover_tilt_position, int) and (
            self.current_cover_tilt_position
            >= OPEN_POSITION - PowerViewCoverTiltOnly.OPENCLOSED_THRESHOLD
            or self.current_cover_tilt_position
            <= CLOSED_POSITION + PowerViewCoverTiltOnly.OPENCLOSED_THRESHOLD
        )


class PowerViewCoverTDBUBottom(PowerViewCover):
    """Rail driven by position1/primary of a dual-rail Top-Down/Bottom-Up shade.

    The official Hunter Douglas PowerView integration in home-assistant/core
    (hunterdouglas_powerview) names the primary-position rail "bottom" and
    the secondary-position rail "top", but that's over the WiFi hub API, not
    BLE -- on this BLE firmware it's confirmed reversed: this class (driven
    by position1) is physically the TOP rail. The class/unique_id keep the
    position1-based "Bottom" name for internal consistency; only the
    user-facing name is swapped below.

    Also confirmed: on this firmware position1 uses the same inverted
    raw-position convention as PowerViewCoverTopDown (device 0 = open/
    retracted, device 100 = closed/extended), opposite of the "Bottom rail"
    (position2) entity -- reported as the two rails moving in opposite
    directions for the same target percentage. Inverted at the boundary so
    HA's standard 0=closed/100=open is preserved, same as PowerViewCoverTopDown.
    """

    def __init__(self, coordinator: PVCoordinator) -> None:
        """Initialize the rail."""
        LOGGER.debug("%s: init() PowerViewCoverTDBUBottom", coordinator.name)
        super().__init__(coordinator)
        self._attr_name = "Top rail"
        self._attr_unique_id = f"{self._attr_unique_id}_bottom"

    @property
    def current_cover_position(self) -> int | None:  # type: ignore[reportIncompatibleVariableOverride]
        """Return current position, inverting the device axis."""
        pos = self._fresh_position(ATTR_CURRENT_POSITION)
        return OPEN_POSITION - pos if pos is not None else None

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Move the rail, inverting for the device, clamped against the other rail.

        The top rail can't be pulled down past the bottom rail's current
        position -- that's a LOWER bound on the top rail (target >= bottom),
        not an upper bound. (Previously coded as min(target, 100-bottom),
        which is the wrong operator and was the cause of the top rail
        appearing "locked" once it reached 100-bottom.)
        """
        target_position: Final = kwargs.get(ATTR_POSITION)
        if target_position is None:
            return
        bottom_position = self._fresh_position("position2")
        if bottom_position is None:
            return
        clamped = max(round(target_position), bottom_position)
        inverted = OPEN_POSITION - clamped
        LOGGER.debug(
            "set top-rail cover to position %f (device %i)", target_position, inverted
        )
        if self.current_cover_position == clamped and not (
            self.is_closing or self.is_opening
        ):
            return
        self._target_position = clamped
        try:
            await self._coord.api.set_position(
                inverted,
                velocity=self._coord.velocity,
            )
            self.async_write_ha_state()
        except BleakError as err:
            LOGGER.error(
                "Failed to move top-rail cover '%s' to %f%%: %s",
                self.name,
                target_position,
                err,
            )

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open the rail (send device position 0), clamped against the other rail."""
        LOGGER.debug("open top-rail cover")
        await self.async_set_cover_position(**{ATTR_POSITION: OPEN_POSITION})

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close the rail (send device position 100)."""
        LOGGER.debug("close top-rail cover")
        await self.async_set_cover_position(**{ATTR_POSITION: CLOSED_POSITION})


class PowerViewCoverTDBUTop(PowerViewCover):
    """Rail driven by position2/secondary of a dual-rail Top-Down/Bottom-Up shade.

    TDBU shades (type 8/9/33/47) have two independently-movable rails, but
    the API previously only ever exposed one cover entity, driven off
    position1. This drives the second rail via the "position2" field that
    dec_manufacturer_data() decodes but nothing else consumed.

    See PowerViewCoverTDBUBottom for naming caveats -- on this BLE firmware
    this class (driven by position2) is confirmed to be the physical BOTTOM
    rail, so only its user-facing name is swapped below. Move it in small
    steps from its current position first, watching the shade, before
    trusting a jump to 0 or 100.
    """

    def __init__(self, coordinator: PVCoordinator) -> None:
        """Initialize the rail."""
        LOGGER.debug("%s: init() PowerViewCoverTDBUTop", coordinator.name)
        super().__init__(coordinator)
        self._attr_name = "Bottom rail"
        self._attr_unique_id = f"{self._attr_unique_id}_top"
        self._target_position = self._fresh_position("position2") or OPEN_POSITION

    @property
    def current_cover_position(self) -> int | None:  # type: ignore[reportIncompatibleVariableOverride]
        """Return current position of the bottom rail."""
        return self._fresh_position("position2")

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Move the bottom rail, clamped so it can't pass through the top rail.

        The bottom rail can't rise past the top rail's current position --
        an UPPER bound (target <= top). PowerViewCoverTDBUBottom (the "Top
        rail" entity) inverts position1 to get its own HA-facing position
        (top_ha = 100 - raw_pos1), so this clamps against 100 - raw_pos1,
        not the raw reading directly.
        """
        target_position: Final = kwargs.get(ATTR_POSITION)
        if target_position is None:
            return
        top_rail_raw = self._fresh_position(ATTR_CURRENT_POSITION)
        if top_rail_raw is None:
            return
        clamped_target = min(round(target_position), OPEN_POSITION - top_rail_raw)
        LOGGER.debug("set bottom-rail cover to position %f", target_position)
        if self.current_cover_position == clamped_target and not (
            self.is_closing or self.is_opening
        ):
            return
        self._target_position = clamped_target
        try:
            await self._coord.api.set_position(
                top_rail_raw,
                pos2=clamped_target,
                velocity=self._coord.velocity,
            )
            self.async_write_ha_state()
        except BleakError as err:
            LOGGER.error(
                "Failed to move bottom-rail cover '%s' to %f%%: %s",
                self.name,
                target_position,
                err,
            )

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open the bottom rail, clamped against the top rail."""
        LOGGER.debug("open bottom-rail cover")
        await self.async_set_cover_position(**{ATTR_POSITION: OPEN_POSITION})

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close the bottom rail."""
        LOGGER.debug("close bottom-rail cover")
        await self.async_set_cover_position(**{ATTR_POSITION: CLOSED_POSITION})
