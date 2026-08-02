"""Hunter Douglas Powerview cover."""

from math import ceil
from typing import Any

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
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ConfigEntryType, async_setup_shade_platform
from .api import CLOSED_POSITION, OPEN_POSITION, ShadeMove
from .const import DOMAIN, LOGGER
from .coordinator import PVCoordinator

# Position at which the duolite front sheer hands over to the rear opaque
# fabric. One motor drives both, so Home Assistant's single 0-100 scale is
# split in half: below this the rear fabric moves, above it the front does.
DUOLITE_MIDPOINT = 50


def _add_entities(
    coordinator: PVCoordinator, async_add_entities: AddEntitiesCallback
) -> None:
    """Create cover entities for a single shade coordinator."""
    caps = coordinator.shade_capabilities

    if caps.tilt_only:
        entities: list[PowerViewCoverBase] = [PowerViewCoverTiltOnly(coordinator)]
    elif caps.is_duolite:
        entities = [
            PowerViewCoverDuoliteCombinedTilt(coordinator)
            if caps.has_tilt
            else PowerViewCoverDuoliteCombined(coordinator),
            PowerViewCoverDuoliteFront(coordinator),
            PowerViewCoverDuoliteRear(coordinator),
        ]
    elif caps.is_tilt_on_closed:
        entities = [PowerViewCoverTiltOnClosed(coordinator)]
    elif caps.has_vane:
        entities = [PowerViewCoverDuette(coordinator)]
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


class PowerViewCoverBase(PassiveBluetoothCoordinatorEntity[PVCoordinator], CoverEntity):  # type: ignore[reportIncompatibleVariableOverride]
    """Shared behaviour for every PowerView cover entity.

    Deliberately sets no ``_attr_name``: Home Assistant resolves a name from
    ``hasattr(self, "_attr_name")`` first and never reaches the translation
    key if any class in the chain has set one. Shades represented by a single
    entity take their name from the device via ``PowerViewCover``; shades that
    fan out into several entities name each one with a translation key.
    """

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
        LOGGER.debug("%s: init() %s", coordinator.name, type(self).__name__)
        self._coord: PVCoordinator = coordinator
        self._attr_device_info = self._coord.device_info
        self._attr_unique_id = (
            f"{DOMAIN}_{format_mac(self._coord.address)}_{CoverDeviceClass.SHADE}"
        )
        # Seeded from the entity's own view of position, so subclasses that
        # invert or remap the device axis start out consistent with what they
        # report rather than with the raw reading.
        self._target_position: int | None = self.current_cover_position
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

    def _reset_target_position(self) -> None:
        self._target_position = None

    def _fresh_position(self, key: str) -> int | None:
        """Return the rounded ``key`` position, or None if stale/absent."""
        if not self._coord.data_available:
            return None
        pos = self._coord.data.get(key)
        return round(pos) if pos is not None else None

    # --- movement hooks --------------------------------------------------
    #
    # Everything that differs between shade types lives in these callbacks,
    # so the send-and-report body below is written once. They may return
    # None: this is a passive integration, and a subclass that has to consult
    # another rail's position can find it simply isn't known yet, in which
    # case the move is abandoned rather than sent with a guess.

    @callback
    def _clamp_cover_limit(self, target: int) -> int | None:
        """Constrain a target so the shade can't be sent somewhere impossible."""
        return target

    @callback
    def _get_shade_move(self, target: int) -> ShadeMove | None:
        """Translate a Home Assistant facing target into a device-facing move."""
        return ShadeMove(pos1=target)

    @callback
    def _get_shade_tilt(self, target: int) -> ShadeMove | None:
        """Translate a Home Assistant facing tilt target into a device move.

        The lift axis has to be restated in every position command, and it is
        the *device* reading that belongs on the wire -- not this entity's
        possibly inverted or remapped view of it.
        """
        pos1 = self._fresh_position(ATTR_CURRENT_POSITION)
        return None if pos1 is None else ShadeMove(pos1=pos1, tilt=target)

    async def _async_send_move(self, move: ShadeMove, description: str) -> bool:
        """Send a move to the shade, returning whether it was accepted."""
        LOGGER.debug("%s: %s as %s", self.name, description, move)
        try:
            await self._coord.api.set_position(
                move.pos1,
                move.pos2,
                move.pos3,
                move.tilt,
                velocity=self._coord.velocity,
                # The shade drops the link itself once it has finished moving.
                # Holding it open until then is what lets is_opening/is_closing
                # report movement in between advertisements.
                disconnect=False,
            )
        except BleakError as err:
            LOGGER.error("Failed to %s for '%s': %s", description, self.name, err)
            return False
        self.async_write_ha_state()
        return True

    async def _async_move_to(self, target: float) -> None:
        """Move the shade to a Home Assistant facing position."""
        clamped = self._clamp_cover_limit(round(target))
        if clamped is None:
            return
        if self.current_cover_position == clamped and not (
            self.is_closing or self.is_opening
        ):
            return
        if (move := self._get_shade_move(clamped)) is None:
            return
        self._target_position = clamped
        if not await self._async_send_move(move, f"move to {clamped}%"):
            self._reset_target_position()

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Move the cover to a specific position."""
        if (target_position := kwargs.get(ATTR_POSITION)) is not None:
            await self._async_move_to(target_position)

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open the cover."""
        await self._async_move_to(OPEN_POSITION)

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close the cover."""
        await self._async_move_to(CLOSED_POSITION)

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Stop the cover."""
        LOGGER.debug("stop cover")
        try:
            await self._coord.api.stop()
            self._reset_target_position()
            self.async_write_ha_state()
        except BleakError as err:
            LOGGER.error("Failed to stop cover '%s': %s", self.name, err)


class PowerViewCover(PowerViewCoverBase):
    """A shade represented by one entity, named after its device."""

    _attr_name = None


class PowerViewCoverTiltBase(PowerViewCoverBase):
    """Tilt behaviour, without claiming a name.

    Kept separate from PowerViewCoverTilt so the Duolite combined entity can
    pick up tilt while still naming itself from its own translation key.
    """

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

    @property
    def current_cover_tilt_position(self) -> int | None:  # type: ignore[reportIncompatibleVariableOverride]
        """Return current tilt of cover.

        None is unknown
        """
        return self._fresh_position(ATTR_CURRENT_TILT_POSITION)

    async def async_set_cover_tilt_position(self, **kwargs: Any) -> None:
        """Move the tilt to a specific position."""
        if not isinstance(target_position := kwargs.get(ATTR_TILT_POSITION), int):
            return
        if self.current_cover_tilt_position == target_position:
            return
        if (move := self._get_shade_tilt(target_position)) is None:
            return
        await self._async_send_move(move, f"tilt to {target_position}%")

    async def async_stop_cover_tilt(self, **kwargs: Any) -> None:
        """Stop the cover."""
        await self.async_stop_cover(**kwargs)

    async def async_open_cover_tilt(self, **kwargs: Any) -> None:
        """Open the cover tilt."""
        await self.async_set_cover_tilt_position(
            **{**kwargs, ATTR_TILT_POSITION: OPEN_POSITION}
        )

    async def async_close_cover_tilt(self, **kwargs: Any) -> None:
        """Close the cover tilt."""
        await self.async_set_cover_tilt_position(
            **{**kwargs, ATTR_TILT_POSITION: CLOSED_POSITION}
        )


class PowerViewCoverTilt(PowerViewCoverTiltBase):
    """Representation of a PowerView shade with additional tilt functionality."""

    _attr_name = None


class PowerViewCoverTiltOnClosed(PowerViewCoverTilt):
    """Representation of a PowerView shade whose tilt is only available when closed.

    Examples: Pirouette (type 18), Twist (type 44).

    If a tilt command arrives while the shade is open, the shade is closed first
    so the tilt mechanism is engaged before the command is sent.
    """

    async def async_set_cover_tilt_position(self, **kwargs: Any) -> None:
        """Move the tilt to a specific position, closing first if needed."""
        if self.current_cover_position != CLOSED_POSITION:
            LOGGER.debug("tilt-on-closed: closing shade before tilting")
            await self._async_move_to(CLOSED_POSITION)
            return
        await super().async_set_cover_tilt_position(**kwargs)


class PowerViewCoverDuette(PowerViewCoverTiltBase):
    """Duette / Applause with privacy vanes controlled via pos2, exposed as tilt.

    Patman15 PR#33 (bob's fork) implementation:
    - vanes position is advertised as position2 (0-100 %)
    - HA represents vanes as a tilt slider for UX consistency
    - pos2 uses same *100 wire encoding as pos1 (not like tilt)

    Types 6 (Duette) and 10 (Duette and Applause SkyLift) confirmed;
    types 8/9/33 are TDBU in this fork and keep their dual-rail entities.
    """

    _attr_name = None

    @property
    def current_cover_tilt_position(self) -> int | None:  # type: ignore[reportIncompatibleVariableOverride]
        """Return vanes position as tilt (0-100 %)."""
        pos2 = self._fresh_position("position2")
        return round(pos2) if pos2 is not None else None

    @callback
    def _get_shade_tilt(self, target: int) -> ShadeMove | None:
        """Translate HA tilt target into a pos2 (vane) move, restating lift."""
        pos1 = self._fresh_position(ATTR_CURRENT_POSITION)
        if pos1 is None:
            return None
        # pos2 is a lift-rail like pos1, so it needs *100 encoding, not tilt's
        # unscaled encoding. ShadeMove carries the raw HA %; set_position() does
        # the *100 itself.
        return ShadeMove(pos1=pos1, pos2=target)



class PowerViewCoverTiltOnly(PowerViewCoverTilt):
    """Representation of a PowerView shade with tilt and no lift."""

    OPENCLOSED_THRESHOLD = 5

    _attr_device_class = CoverDeviceClass.BLIND
    _attr_supported_features = (
        CoverEntityFeature.OPEN_TILT
        | CoverEntityFeature.CLOSE_TILT
        | CoverEntityFeature.STOP_TILT
        | CoverEntityFeature.SET_TILT_POSITION
    )

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

    @callback
    def _get_shade_move(self, target: int) -> ShadeMove | None:
        """Invert the target back into the device's own axis."""
        return ShadeMove(pos1=OPEN_POSITION - target)


class PowerViewCoverTDBUBottom(PowerViewCoverBase):
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

    _attr_translation_key = "top_rail"

    def __init__(self, coordinator: PVCoordinator) -> None:
        """Initialize the rail."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._attr_unique_id}_bottom"

    @property
    def current_cover_position(self) -> int | None:  # type: ignore[reportIncompatibleVariableOverride]
        """Return current position, inverting the device axis."""
        pos = self._fresh_position(ATTR_CURRENT_POSITION)
        return OPEN_POSITION - pos if pos is not None else None

    @callback
    def _clamp_cover_limit(self, target: int) -> int | None:
        """Stop the top rail being pulled down past the bottom rail.

        That is a LOWER bound on the top rail (target >= bottom), not an upper
        bound. (Previously coded as min(target, 100-bottom), which is the wrong
        operator and was the cause of the top rail appearing "locked" once it
        reached 100-bottom.)
        """
        bottom_position = self._fresh_position("position2")
        return None if bottom_position is None else max(target, bottom_position)

    @callback
    def _get_shade_move(self, target: int) -> ShadeMove | None:
        """Invert the target back into the device's own axis."""
        return ShadeMove(pos1=OPEN_POSITION - target)


class PowerViewCoverTDBUTop(PowerViewCoverBase):
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

    _attr_translation_key = "bottom_rail"

    def __init__(self, coordinator: PVCoordinator) -> None:
        """Initialize the rail."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._attr_unique_id}_top"

    @property
    def current_cover_position(self) -> int | None:  # type: ignore[reportIncompatibleVariableOverride]
        """Return current position of the bottom rail."""
        return self._fresh_position("position2")

    @callback
    def _clamp_cover_limit(self, target: int) -> int | None:
        """Stop the bottom rail rising past the top rail.

        An UPPER bound (target <= top). PowerViewCoverTDBUBottom (the "Top
        rail" entity) inverts position1 to get its own HA-facing position
        (top_ha = 100 - raw_pos1), so this clamps against 100 - raw_pos1,
        not the raw reading directly.
        """
        top_rail_raw = self._fresh_position(ATTR_CURRENT_POSITION)
        if top_rail_raw is None:
            return None
        return min(target, OPEN_POSITION - top_rail_raw)

    @callback
    def _get_shade_move(self, target: int) -> ShadeMove | None:
        """Restate the top rail unchanged and drive position2 to the target."""
        top_rail_raw = self._fresh_position(ATTR_CURRENT_POSITION)
        if top_rail_raw is None:
            return None
        return ShadeMove(pos1=top_rail_raw, pos2=target)


class PowerViewCoverDuoliteBase(PowerViewCoverBase):
    """Shared plumbing for dual-fabric (Duolite) shades.

    EXPERIMENTAL -- see the Duolite section of the README. One motor drives a
    front sheer and a rear opaque fabric. The mapping used here mirrors the
    official hunterdouglas_powerview integration, where the front sheer is the
    primary position and the rear opaque is the secondary; over BLE those are
    position1 and position2. That correspondence is carried over from the hub
    API, not confirmed against a Duolite shade, so please open an issue with a
    diagnostics download if these entities misbehave.
    """

    @property
    def _front_position(self) -> int | None:
        """Return the front sheer fabric's device position."""
        return self._fresh_position(ATTR_CURRENT_POSITION)

    @property
    def _rear_position(self) -> int | None:
        """Return the rear opaque fabric's device position."""
        return self._fresh_position("position2")


class PowerViewCoverDuoliteCombined(PowerViewCoverDuoliteBase):
    """Both Duolite fabrics presented as one cover on a single 0-100 scale.

    Below the midpoint the rear opaque fabric moves across its full travel;
    above it the front sheer does. This is the entity most users will want.
    """

    _attr_translation_key = "combined"

    def __init__(self, coordinator: PVCoordinator) -> None:
        """Initialize the combined fabric entity."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._attr_unique_id}_combined"

    @property
    def current_cover_position(self) -> int | None:  # type: ignore[reportIncompatibleVariableOverride]
        """Fold both fabric positions onto one scale."""
        front, rear = self._front_position, self._rear_position
        if front is None or rear is None:
            return None
        if front == CLOSED_POSITION:
            return ceil(rear / 2)
        return ceil(front / 2) + DUOLITE_MIDPOINT

    @property
    def is_closed(self) -> bool:  # type: ignore[reportIncompatibleVariableOverride]
        """Return if the cover is closed; the rear opaque fabric decides."""
        return self._rear_position == CLOSED_POSITION

    @callback
    def _get_shade_move(self, target: int) -> ShadeMove | None:
        """Drive whichever fabric owns this half of the scale.

        The fabrics interlock: the front sheer has to be retracted before the
        rear opaque can move, and the rear has to be fully open before the
        front can. The hub API leaves the idle axis unset and lets the hub
        apply that itself, but every position command here carries both axes,
        so the idle one is stated at the position the interlock requires.
        Restating it at its current position instead would pin the fabric and
        leave the combined scale stuck in one half.
        """
        if target > DUOLITE_MIDPOINT:
            return ShadeMove(pos1=(target - DUOLITE_MIDPOINT) * 2, pos2=OPEN_POSITION)
        return ShadeMove(pos1=CLOSED_POSITION, pos2=target * 2)


class PowerViewCoverDuoliteCombinedTilt(
    PowerViewCoverTiltBase, PowerViewCoverDuoliteCombined
):
    """Combined Duolite entity for a shade whose front fabric also tilts.

    Type 38 (Silhouette Duolite) only. Tilt applies to the front sheer, which
    is the position1 axis the inherited tilt hook already restates. The tilt
    base comes first so its supported features win over the lift-only set.
    """


class PowerViewCoverDuoliteFront(PowerViewCoverDuoliteBase):
    """The front sheer fabric of a Duolite shade on its own."""

    _attr_translation_key = "front"

    def __init__(self, coordinator: PVCoordinator) -> None:
        """Initialize the front fabric entity."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._attr_unique_id}_front"


class PowerViewCoverDuoliteRear(PowerViewCoverDuoliteBase):
    """The rear opaque fabric of a Duolite shade on its own."""

    _attr_translation_key = "rear"

    def __init__(self, coordinator: PVCoordinator) -> None:
        """Initialize the rear fabric entity."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._attr_unique_id}_rear"

    @property
    def current_cover_position(self) -> int | None:  # type: ignore[reportIncompatibleVariableOverride]
        """Return the rear fabric's position."""
        return self._rear_position

    @callback
    def _get_shade_move(self, target: int) -> ShadeMove | None:
        """Restate the front fabric unchanged and drive position2."""
        front = self._front_position
        return None if front is None else ShadeMove(pos1=front, pos2=target)
