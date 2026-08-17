"""The Hunter Douglas PowerView (BLE) integration.

@author: patman15
@license: Apache-2.0 license
"""

import base64
from collections.abc import Callable

import aiohttp
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import (
    BluetoothCallbackMatcher,
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
    async_ble_device_from_address,
    async_discovered_service_info,
)
from homeassistant.components.bluetooth.const import DOMAIN as BLUETOOTH_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import UUID_COV_SERVICE as UUID, V2_RECORD_LEN
from .const import (
    CONF_FRIENDLY_NAMES,
    CONF_HUB_URL,
    DOMAIN,
    LOGGER,
    MFCT_ID,
    SIGNAL_NEW_SHADE,
)
from .coordinator import PVCoordinator, shade_id_for

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.COVER,
    Platform.NUMBER,
    Platform.SENSOR,
]

# Keyed by `shade_id`, not by Bluetooth address: a shade that changes address
# has to be recognised as one we already track rather than set up again.
type HubRuntimeData = dict[str, PVCoordinator]
type ConfigEntryType = ConfigEntry[HubRuntimeData]

type AddEntitiesFn = Callable[[PVCoordinator, AddEntitiesCallback], None]


def async_setup_shade_platform(
    hass: HomeAssistant,
    config_entry: ConfigEntryType,
    async_add_entities: AddEntitiesCallback,
    add_fn: AddEntitiesFn,
) -> None:
    """Set up a platform for all current and future shades."""
    for coordinator in config_entry.runtime_data.values():
        add_fn(coordinator, async_add_entities)

    @callback
    def _async_new_shade(coordinator: PVCoordinator) -> None:
        add_fn(coordinator, async_add_entities)

    config_entry.async_on_unload(
        async_dispatcher_connect(
            hass,
            SIGNAL_NEW_SHADE.format(entry_id=config_entry.entry_id),
            _async_new_shade,
        )
    )


async def _fetch_shade_names(hass: HomeAssistant, hub_url: str) -> dict[str, str]:
    """Fetch the shades' friendly names from the hub, keyed by BLE advert name.

    Returns empty dict on failure.
    """
    session = async_get_clientsession(hass)
    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with session.get(f"{hub_url}/home/shades", timeout=timeout) as resp:
            resp.raise_for_status()
            shades = await resp.json(content_type=None)
    except (TimeoutError, aiohttp.ClientError, ValueError):
        return {}

    names: dict[str, str] = {}
    for shade in shades or []:
        ble_name = shade.get("bleName", "")
        if not ble_name:
            continue
        name_b64 = shade.get("name", "")
        try:
            name = base64.b64decode(name_b64).decode("utf-8") if name_b64 else ble_name
        except Exception:  # noqa: BLE001
            name = ble_name
        names[ble_name] = name
    return names


def _persist_cache_entry(
    hass: HomeAssistant,
    entry: ConfigEntryType,
    key: str,
    shade_id: str,
    value: object,
    obsolete: str | None = None,
) -> None:
    """Read-modify-write a per-shade value into a cache stored on entry.data.

    HA replaces entry.data atomically, so concurrent setup tasks each pull
    the latest snapshot before merging — the no-op guard prevents writes
    that would just notify listeners with unchanged data.

    ``obsolete`` drops a key the value used to be filed under. These caches
    were keyed by Bluetooth address, and leaving those entries behind would
    grow the config entry by one dead record per address a shade ever had.
    """
    cache = dict(entry.data.get(key, {}))
    # None unless there really is a stale key to drop. A nameless shade files
    # under its address, so `obsolete` is its own key and must not count.
    stale_key: str | None = (
        obsolete
        if obsolete is not None and obsolete != shade_id and obsolete in cache
        else None
    )
    if cache.get(shade_id) == value and stale_key is None:
        return
    cache[shade_id] = value
    if stale_key is not None:
        del cache[stale_key]
    hass.config_entries.async_update_entry(entry, data={**entry.data, key: cache})


def _resolve_friendly_name(
    hass: HomeAssistant,
    entry: ConfigEntryType,
    service_info: BluetoothServiceInfoBleak,
    shade_id: str,
    hub_name: str | None,
) -> str:
    """Resolve a shade's friendly name (Shelly-style) and refresh the cache.

    Hub data wins; otherwise fall back to the cached value from a prior
    successful resolution; otherwise fall back to the BLE advert name.

    The address is still consulted for reads, because that is how the cache
    was keyed before shades were identified by name, and the write below
    moves the value across.
    """
    address = service_info.address
    cached_names: dict[str, str] = entry.data.get(CONF_FRIENDLY_NAMES, {})
    if hub_name is not None:
        friendly_name = hub_name
    elif shade_id in cached_names:
        friendly_name = cached_names[shade_id]
    elif address in cached_names:
        friendly_name = cached_names[address]
    else:
        friendly_name = service_info.name or address

    _persist_cache_entry(
        hass, entry, CONF_FRIENDLY_NAMES, shade_id, friendly_name, obsolete=address
    )
    return friendly_name


def _is_shade_advert(service_info: BluetoothServiceInfoBleak) -> bool:
    """Whether this advertisement really came from a PowerView shade.

    Passing Home Assistant's Bluetooth matcher is not enough. The matcher tests
    company ID 2073 and service UUID fdc1 against `BluetoothServiceInfoBleak`,
    which is a *union* of everything an address has ever advertised -- both
    habluetooth (`base_scanner.py`, service_uuids and manufacturer_data are
    merged into the previous record) and BlueZ's own Device1 properties
    accumulate that way and never forget. So the two conditions need not have
    arrived in the same packet, or in the same minute, and unrelated hardware
    has been adopted as a shade on the strength of it -- a freezer and a coffee
    machine in issue #42.

    The payload itself does not accumulate: manufacturer_data[2073] is whatever
    that company ID last carried. A shade always puts a V2 record there, so its
    length is the honest test.
    """
    return len(service_info.manufacturer_data.get(MFCT_ID, b"")) == V2_RECORD_LEN


def _migrate_legacy_identity(hass: HomeAssistant, address: str, shade_id: str) -> None:
    """Re-key an existing device and its entities off the Bluetooth address.

    Runs when a shade is first set up, which is the only moment both halves of
    the mapping are known: the registry holds the address a device was created
    under, and the advertisement in hand supplies the name to move it to.
    There is no `async_migrate_entry` equivalent, because nothing stored on the
    config entry says which address belonged to which shade.

    Deliberately conservative. A shade whose address has already changed at
    least once is not found by its current address, and its old device is left
    alone to be deleted by hand -- merging two registry entries would have to
    guess which of the two carries the customisation worth keeping.
    """
    if shade_id == address:
        return  # nameless shade: its identity is the address either way

    dev_reg = dr.async_get(hass)
    if dev_reg.async_get_device(identifiers={(DOMAIN, shade_id)}) is not None:
        return  # already migrated, or a duplicate device already holds the name

    device = dev_reg.async_get_device(identifiers={(DOMAIN, address)})
    if device is None:
        return  # not seen before, so it gets the new identity from the start

    LOGGER.info("Re-keying shade at %s onto its name %s", address, shade_id)

    # Entities first: the unique IDs have to be free before the device moves,
    # or a rejected entity would come back under a device that no longer
    # matches the address its unique ID was built from.
    ent_reg = er.async_get(hass)
    old_stem, new_stem = format_mac(address), format_mac(shade_id)
    for ent in er.async_entries_for_device(
        ent_reg, device.id, include_disabled_entities=True
    ):
        if old_stem not in ent.unique_id:
            continue
        new_unique_id = ent.unique_id.replace(old_stem, new_stem, 1)
        if ent_reg.async_get_entity_id(ent.domain, DOMAIN, new_unique_id):
            continue  # a duplicate device got there first; leave this one be
        ent_reg.async_update_entity(ent.entity_id, new_unique_id=new_unique_id)

    dev_reg.async_update_device(
        device.id,
        new_identifiers={(DOMAIN, shade_id), (BLUETOOTH_DOMAIN, address)},
    )


async def _async_setup_shade(
    hass: HomeAssistant,
    entry: ConfigEntryType,
    service_info: BluetoothServiceInfoBleak,
    shade_names: dict[str, str],
) -> None:
    """Create a coordinator for a newly discovered shade."""
    address = service_info.address

    if not _is_shade_advert(service_info):
        # Rechecked on every advertisement this address sends, so a shade first
        # heard mid-packet is adopted as soon as a whole record arrives.
        LOGGER.debug("%s: no PowerView V2 record, not a shade", address)
        return

    ble_device: BLEDevice | None = async_ble_device_from_address(
        hass=hass, address=address, connectable=True
    )
    if not ble_device:
        LOGGER.debug("BLE device %s not connectable, skipping", address)
        return

    shade_id = shade_id_for(ble_device)

    # A shade already tracked under this identity has changed address rather
    # than appeared. Everything Home Assistant holds for it stays; only the
    # radio subscriptions move. Setting it up again would instead have built a
    # second device whose entities collide on unique ID with the first.
    if (known := entry.runtime_data.get(shade_id)) is not None:
        known.async_retarget(ble_device)
        return

    _migrate_legacy_identity(hass, address, shade_id)

    friendly_name = _resolve_friendly_name(
        hass, entry, service_info, shade_id, shade_names.get(service_info.name)
    )

    coordinator = PVCoordinator(hass, ble_device, entry.data.copy(), friendly_name)

    entry.runtime_data[shade_id] = coordinator
    entry.async_on_unload(coordinator.async_start())

    # Populate dev_details before entity dispatch so the device registers with
    # firmware/serial on first creation — the HA registry doesn't re-read
    # DeviceInfo later. Failures are retried from the advertisement handler.
    try:
        await coordinator.query_dev_info()
    except (BleakError, TimeoutError):
        LOGGER.debug(
            "Initial device info query failed for %s (%s); will retry via adverts",
            friendly_name,
            address,
        )

    async_dispatcher_send(
        hass,
        SIGNAL_NEW_SHADE.format(entry_id=entry.entry_id),
        coordinator,
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntryType) -> bool:
    """Set up PowerView Home from a config entry."""
    LOGGER.debug("Setup of %s", repr(entry))

    entry.runtime_data = {}

    # Resolve shade friendly names from hub if available
    hub_url = entry.data.get(CONF_HUB_URL, "")
    shade_names: dict[str, str] = {}
    if hub_url:
        shade_names = await _fetch_shade_names(hass, hub_url)

    # Forward platforms first so dispatched entities have their setup ready
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Kick off shade setup for already-discovered BLE devices (non-blocking)
    for service_info in async_discovered_service_info(hass, connectable=True):
        if (
            MFCT_ID in service_info.manufacturer_data
            and UUID in service_info.service_uuids
        ):
            hass.async_create_task(
                _async_setup_shade(hass, entry, service_info, shade_names)
            )

    # Register for future BLE discoveries
    def _async_discovered_device(
        service_info: BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        # Both conditions are rechecked inside _async_setup_shade, which is the
        # chokepoint the startup sweep shares. Testing them here as well keeps a
        # rejected device from costing a task per advertisement, and a shade
        # advertises several a second while it is moving. The address is looked
        # up across the coordinators rather than in the keys, which are shade
        # IDs -- an address we already track needs nothing doing, and one we do
        # not may still belong to a shade that has moved to it.
        if _is_shade_advert(service_info) and not any(
            coord.address == service_info.address
            for coord in entry.runtime_data.values()
        ):
            hass.async_create_task(
                _async_setup_shade(hass, entry, service_info, shade_names)
            )

    entry.async_on_unload(
        bluetooth.async_register_callback(
            hass,
            _async_discovered_device,
            BluetoothCallbackMatcher(
                service_uuid=UUID,
                manufacturer_id=MFCT_ID,
            ),
            BluetoothScanningMode.ACTIVE,
        )
    )

    return True


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    entry: ConfigEntryType,
    device_entry: dr.DeviceEntry,
) -> bool:
    """Allow user-driven device removal; purge cached state for that shade."""
    shade_ids = {ident[1] for ident in device_entry.identifiers if ident[0] == DOMAIN}
    if not shade_ids:
        return True

    new_data = dict(entry.data)
    cache = dict(new_data.get(CONF_FRIENDLY_NAMES, {}))
    for shade_id in shade_ids:
        cache.pop(shade_id, None)
    new_data[CONF_FRIENDLY_NAMES] = cache
    hass.config_entries.async_update_entry(entry, data=new_data)

    for shade_id in shade_ids:
        coord = entry.runtime_data.pop(shade_id, None)
        if coord is not None:
            # _async_stop is a parent-class (DataUpdateCoordinator) convention;
            # entry.async_on_unload only fires on full entry unload, so we
            # invoke it directly here for per-device removal.
            coord._async_stop()  # noqa: SLF001

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntryType) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        entry.runtime_data.clear()

    LOGGER.debug("Unloaded config entry: %s, ok? %s!", entry.unique_id, str(unload_ok))
    return unload_ok
