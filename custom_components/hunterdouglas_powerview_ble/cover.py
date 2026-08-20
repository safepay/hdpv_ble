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
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ConfigEntryType, async_setup_shade_platform
from .api import CLOSED_POSITION, KEEP_POSITION, OPEN_POSITION, ShadeMove
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
            f"{DOMAIN}_{self._coord.unique_id_stem}_{CoverDeviceClass.SHADE}"
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
        """Return the rounded ``key`` position, or None if stale/absent.

        For what this entity *reports*: a shade we have not heard from in a
        while must say unknown rather than keep publishing an old reading.
        """
        return self._last_position(key) if self._coord.data_available else None

    def _last_position(self, key: str) -> int | None:
        """Return the rounded ``key`` position however old, or None if absent.

        For what this entity *sends* when an axis still has to be restated
        (tilt-on-closed, duolite, and any path that cannot use KEEP on that
        axis). The last reading is the best value there is -- the shade does
        not move on its own. Refusing to build the command instead left
        tilt-only shades with no working control at all once their reading
        aged out. Dual-rail TDBU bottom moves use KEEP for the other axis
        instead, so they do not depend on this.
        """
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
        pos1 = self._last_position(ATTR_CURRENT_POSITION)
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
        requested = round(target)
        clamped = self._clamp_cover_limit(requested)
        if clamped is None:
            LOGGER.debug(
                "%s: dropping move to %i%%, cannot check the interlocking rail",
                self._coord.name,
                requested,
            )
            return
        if self.current_cover_position == clamped and not (
            self.is_closing or self.is_opening
        ):
            return
        if (move := self._get_shade_move(clamped)) is None:
            LOGGER.debug(
                "%s: dropping move to %i%%, no position to restate",
                self._coord.name,
                clamped,
            )
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
            LOGGER.debug(
                "%s: dropping tilt to %i%%, no lift position to restate",
                self._coord.name,
                target_position,
            )
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
    """Representation of a PowerView shade whose tilt only engages when closed.

    Pirouette (18), Silhouette (23, 72), Facette (43), Twist (44).

    The vanes only engage at the closed position, but that is a fact about the
    hardware rather than something the integration has to arrange: the tilt is
    sent as asked and the shade does what it can with it. aiopvapi registers
    these under ShadeBottomUpTiltOnClosed90/180, and neither it nor Home
    Assistant's own hub integration drives the lift axis before tilting --
    `PowerViewShadeWithTiltOnClosed` overrides position properties only.

    This class previously closed the shade first and returned, which sent the
    lift command and dropped the tilt target on the floor, so a single service
    call never tilted anything. Worse, `current_cover_position` is None until
    an advertisement decodes, and None != CLOSED_POSITION, so a tilt arriving
    before the first advert closed the shade instead of tilting it.
    """


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

    @callback
    def _get_shade_tilt(self, target: int) -> ShadeMove:
        """Build the move without consulting a lift axis this shade lacks.

        The inherited hook restates the lift position because most shades
        have one; here it is always 0 and carries no information. Every
        feature this entity exposes routes through this hook, so one that can
        decline would leave the shade with nothing but Stop -- hence the
        narrowed return type: this one never declines.
        """
        return ShadeMove(pos1=CLOSED_POSITION, tilt=target)

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
        """Drive position2; leave the top rail (pos1) unchanged via KEEP.

        Using KEEP (instead of restating the current top reading) lets a
        concurrent top-rail command coalesce into one SET_POSITION that
        carries both targets, instead of the bottom command overwriting the
        top's intended position.

        Pre-move clamps on each entity can still produce a crossed pair
        (e.g. top→20 + bottom→70 from 100/0). On type-8 Duette firmware that
        is fine: the shade refuses the cross and settles on the rail boundary
        (observed: requested 20/70 ended at HA 70/70, device pos1+pos2=100).
        """
        return ShadeMove(pos1=KEEP_POSITION, pos2=target)


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
        """Restate the front fabric unchanged and drive position2.

        Deliberately not ``_front_position``: that is the reporting view,
        which withholds a stale reading, and restating an axis wants the last
        reading there is.
        """
        front = self._last_position(ATTR_CURRENT_POSITION)
        return None if front is None else ShadeMove(pos1=front, pos2=target)
