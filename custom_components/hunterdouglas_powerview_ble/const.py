"""Constants for the Hunter Douglas PowerView (BLE) integration."""

import logging
from typing import Final

DOMAIN: Final[str] = "hunterdouglas_powerview_ble"
LOGGER: Final = logging.getLogger(__package__)
MFCT_ID: Final[int] = 2073
TIMEOUT: Final[int] = 5
# Ceiling on one attempt to open the link and subscribe. A shade in a bad state
# was seen to hold a connect for ~25 s before failing, with the command lock
# held throughout, so an unbounded wait wedges every command to that shade.
# _connect retries once, so the worst-case lock hold is ~80 s: two attempts of
# this plus two TIMEOUT*2 teardowns.
CONNECT_TIMEOUT: Final[int] = 30

CONF_HOME_KEY: Final[str] = "home_key"
CONF_HUB_URL: Final[str] = "hub_url"
CONF_FRIENDLY_NAMES: Final[str] = "friendly_names"
# Probed once per shade over GATT and cached, so the extra connection is
# paid on first discovery rather than on every restart.
CONF_POWER_TYPES: Final[str] = "power_types"

# dispatcher signal for newly discovered shades (format with entry_id)
SIGNAL_NEW_SHADE: Final[str] = f"{DOMAIN}_new_shade_{{entry_id}}"

# attributes (do not change)
ATTR_RSSI: Final[str] = "rssi"
