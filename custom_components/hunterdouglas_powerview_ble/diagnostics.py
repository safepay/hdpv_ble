"""Diagnostics for the Hunter Douglas PowerView (BLE) integration.

Dumps every signal the integration holds for each shade, decoded as little as
possible: the advertisement bytes, the shade's replies to its GATT queries, the
hub's record, the entities the shade produced, plus device, capability and
connectivity state. It is meant to be
attached to a bug report, whatever the bug is — position and tilt, range and
connectivity, encryption and control failures, the reset/reinit flags, or a
shade's power source.

Raw values are reported *alongside* the integration's interpretation of them,
never instead of it, because the two have disagreed before: byte 0 of the 0xFFDE
reply was taken for a power-source enum when it is really the protocol's status
code. It reads 0 on every successful reply, so the integration concluded that
every shade was mains-powered and stripped the battery sensors off the ones that
were not. A decoded field can be wrong; the bytes it came from cannot.
"""

import asyncio
from typing import Any, Final

import aiohttp
from bleak.exc import BleakError

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.components.cover import CoverEntityFeature
from homeassistant.const import ATTR_SUPPORTED_FEATURES, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.redact import async_redact_data

from . import ConfigEntryType
from .api import V2_RECORD_LEN, ShadeCmd
from .const import CONF_HOME_KEY, CONF_HUB_URL, DOMAIN, MFCT_ID
from .coordinator import PVCoordinator

# home_key is the AES key that drives the shades, home_id identifies the
# PowerView network, and a serial number identifies the owner's hardware. None
# is needed to diagnose anything, and this file gets attached to public issues.
# Each key is listed in both spellings: the shade reports snake_case over GATT
# and the hub reports camelCase in its JSON, so redacting one spelling alone
# silently leaks the same value from the other source.
TO_REDACT: Final[set[str]] = {
    CONF_HOME_KEY,
    "homeId",
    "homeKey",
    "home_id",
    "serialNumber",
    "serial_nr",
    "serial_number",
}

# Every GATT query costs a BLE connection. Opening one per shade at once is the
# adapter contention that made the old startup-time queries unreliable, so keep
# the fan-out bounded even though a user has to click a button to get here.
_MAX_PARALLEL_QUERIES: Final[int] = 3
_QUERY_TIMEOUT_S: Final[float] = 20.0
_HUB_TIMEOUT_S: Final[float] = 10.0

_NOTE: Final[str] = (
    "Raw, uninterpreted values for every shade, for attaching to a GitHub "
    "issue. Some things the integration cannot know and must be stated in the "
    "issue text — notably whether a shade runs on a battery or is wired to "
    "mains, which it does not detect."
)


async def _async_hub_shades(
    hass: HomeAssistant, hub_url: str
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Return the hub's status and its raw /home/shades records, by BLE name.

    Records pass through verbatim. Establishing what the hub actually sends is
    half the value here, so nothing is mapped, renamed or filtered.
    """
    if not hub_url:
        return {"configured": False}, {}

    session = async_get_clientsession(hass)
    timeout = aiohttp.ClientTimeout(total=_HUB_TIMEOUT_S)
    try:
        async with session.get(f"{hub_url}/home/shades", timeout=timeout) as resp:
            resp.raise_for_status()
            shades = await resp.json(content_type=None)
    except (TimeoutError, aiohttp.ClientError, ValueError) as ex:
        # A .local hub URL resolves over mDNS, which is flaky from inside a
        # container at boot; the integration swallows that failure silently, so
        # surface it here.
        return {
            "configured": True,
            "url": hub_url,
            "reachable": False,
            "error": f"{type(ex).__name__}: {ex}",
        }, {}

    records: dict[str, dict[str, Any]] = {
        shade["bleName"]: shade for shade in shades or [] if shade.get("bleName")
    }
    status: dict[str, Any] = {
        "configured": True,
        "url": hub_url,
        "reachable": True,
        "shade_count": len(records),
        # Hub records are joined to shades by an exact name match, so a shade
        # whose advertised name differs from the hub's bleName silently loses
        # its hub data. List the names to make such a near-miss visible.
        "ble_names": sorted(records),
    }
    return status, records


def _device(coord: PVCoordinator) -> dict[str, Any]:
    """Return the shade's identity: type, model and firmware revisions."""
    return {
        "type_id": coord.type_id,
        **async_redact_data(dict(coord.dev_details), TO_REDACT),
    }


def _capabilities(coord: PVCoordinator) -> dict[str, bool]:
    """Return what the integration believes this shade type can do.

    Derived from type_id alone, so a shade whose entities look wrong (a missing
    tilt, a phantom second rail) is usually a wrong or unmapped type_id here.
    """
    caps = coord.shade_capabilities
    return {
        "has_tilt": caps.has_tilt,
        "tilt_only": caps.tilt_only,
        "is_tilt_on_closed": caps.is_tilt_on_closed,
        "is_top_down": caps.is_top_down,
        "is_tdbu": caps.is_tdbu,
        "is_duolite": caps.is_duolite,
    }


def _entities(hass: HomeAssistant, coord: PVCoordinator) -> list[dict[str, Any]]:
    """Return the entities this shade produced, and what they currently report.

    Every other block here describes what the integration believes about a
    shade. This one describes what came out the other end, because "the
    controls disappeared" has three quite different causes that are otherwise
    indistinguishable: an entity never created because the type ID picked the
    wrong `cover.py` subclass, one created and then disabled or hidden in the
    registry, and one present and enabled that offers no features at all --
    which is how an encrypted shade with no home key looks from the outside.

    An empty list is itself the first of those answers.
    """
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, coord.address)})
    if device is None:
        return []

    registry = er.async_get(hass)
    entities: list[dict[str, Any]] = []
    for entry in er.async_entries_for_device(
        registry, device.id, include_disabled_entities=True
    ):
        state = hass.states.get(entry.entity_id)
        record: dict[str, Any] = {
            "entity_id": entry.entity_id,
            "unique_id": entry.unique_id,
            "domain": entry.domain,
            # StrEnum members, and this ends up as JSON.
            "disabled_by": entry.disabled_by.value if entry.disabled_by else None,
            "hidden_by": entry.hidden_by.value if entry.hidden_by else None,
            # None where the entity is in the registry but has no state object,
            # which is what a disabled entity looks like.
            "state": state.state if state else None,
        }
        features = state.attributes.get(ATTR_SUPPORTED_FEATURES) if state else None
        if features is not None:
            # Stored as the live IntFlag, not an int.
            record["supported_features"] = int(features)
            if entry.domain == Platform.COVER:
                # The reason this block exists: a cover reporting no features
                # renders as a shade with a position but no buttons and no
                # slider, and nothing else in this file would say so.
                record["supported_feature_names"] = [
                    flag.name for flag in CoverEntityFeature if flag & features
                ]
        entities.append(record)
    return entities


def _connectivity(
    coord: PVCoordinator, service_info: BluetoothServiceInfoBleak | None
) -> dict[str, Any]:
    """Return radio and encryption state, for range and control failures."""
    return {
        "present": coord.device_present,
        "rssi": service_info.rssi if service_info else None,
        # Position, tilt and battery ride only on V2 adverts; once those stop
        # arriving the retained sample is stale and the entities read unknown.
        "advert_stale": not coord.data_available,
        "encrypted": coord.api.encrypted,
        "home_key_configured": coord.api.has_key,
    }


def _advertisement(
    coord: PVCoordinator, service_info: BluetoothServiceInfoBleak | None
) -> dict[str, Any]:
    """Return the last advertisement: raw bytes and the decoded sample."""
    if service_info is None:
        return {"error": "no advertisement seen for this shade"}

    raw = bytes(service_info.manufacturer_data.get(MFCT_ID, b""))
    return {
        "raw": raw.hex(" "),
        "length": len(raw),
        "is_v2_record": len(raw) == V2_RECORD_LEN,
        # The top two bits of byte 8 drive the battery sensor via a
        # 3/2/1/0 -> 100/50/20/0 % table. Report the code itself: hardwired
        # shades also sit at 3, so the decoded 100% below means little on its
        # own.
        "power_level_code": raw[8] >> 6 if len(raw) == V2_RECORD_LEN else None,
        "decoded": async_redact_data(dict(coord.data), TO_REDACT),
        "velocity": coord.velocity,
    }


async def _async_queries(
    coord: PVCoordinator, sem: asyncio.Semaphore
) -> dict[str, Any]:
    """Read the shade's GATT queries, reported byte-for-byte.

    Failure is data too, not an error to raise: a shade that cannot be reached
    still belongs in the report, so the reason is recorded and the dump goes on.
    Keyed by opcode so further queries can be added without reshaping the file.
    """
    if coord.api.encrypted and not coord.api.has_key:
        reason = {"error": "shade is encrypted and no home key is configured"}
        return {ShadeCmd.POWER_STATUS.name: reason}

    async with sem:
        try:
            payload = await asyncio.wait_for(
                coord.api.query_power_status(), timeout=_QUERY_TIMEOUT_S
            )
            result: dict[str, Any] = {
                "opcode": f"0x{ShadeCmd.POWER_STATUS.value:04X}",
                "raw": payload.hex(" "),
                "length": len(payload),
                "bytes": {f"b{idx}": value for idx, value in enumerate(payload)},
            }
        except (BleakError, TimeoutError) as ex:
            result = {
                "opcode": f"0x{ShadeCmd.POWER_STATUS.value:04X}",
                "error": f"{type(ex).__name__}: {ex}",
            }

    return {ShadeCmd.POWER_STATUS.name: result}


async def _async_shade(
    hass: HomeAssistant,
    coord: PVCoordinator,
    hub_records: dict[str, dict[str, Any]],
    sem: asyncio.Semaphore,
) -> dict[str, Any]:
    """Collect every signal the integration holds for one shade."""
    service_info = bluetooth.async_last_service_info(
        hass, coord.address, connectable=True
    )
    ble_name = coord.api.name
    record = hub_records.get(ble_name)
    return {
        "name": coord.friendly_name,
        "ble_name": ble_name,
        "address": coord.address,
        "device": _device(coord),
        "capabilities": _capabilities(coord),
        "entities": _entities(hass, coord),
        "connectivity": _connectivity(coord, service_info),
        "advertisement": _advertisement(coord, service_info),
        "queries": await _async_queries(coord, sem),
        "hub_matched": record is not None,
        "hub_record": async_redact_data(record, TO_REDACT) if record else None,
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntryType
) -> dict[str, Any]:
    """Return diagnostics for every shade in the config entry."""
    hub_status, hub_records = await _async_hub_shades(
        hass, entry.data.get(CONF_HUB_URL, "")
    )
    sem = asyncio.Semaphore(_MAX_PARALLEL_QUERIES)
    shades = await asyncio.gather(
        *(
            _async_shade(hass, coord, hub_records, sem)
            for coord in entry.runtime_data.values()
        )
    )
    return {
        "note": _NOTE,
        "config_entry": async_redact_data(dict(entry.data), TO_REDACT),
        "hub": hub_status,
        "shades": shades,
    }


async def async_get_device_diagnostics(
    hass: HomeAssistant, entry: ConfigEntryType, device: dr.DeviceEntry
) -> dict[str, Any]:
    """Return diagnostics for a single shade."""
    addresses = {ident[1] for ident in device.identifiers if ident[0] == DOMAIN}
    coord = next(
        (coord for addr, coord in entry.runtime_data.items() if addr in addresses),
        None,
    )
    if coord is None:
        return {"error": "no coordinator is running for this device"}

    hub_status, hub_records = await _async_hub_shades(
        hass, entry.data.get(CONF_HUB_URL, "")
    )
    return {
        "note": _NOTE,
        # Carried here as well as in the config-entry dump: the README sends
        # reporters to the shade's device page, so this is the one that
        # actually gets attached to issues, and whether a home key is
        # configured at all is usually the first thing worth knowing.
        "config_entry": async_redact_data(dict(entry.data), TO_REDACT),
        "hub": hub_status,
        "shade": await _async_shade(hass, coord, hub_records, asyncio.Semaphore(1)),
    }
