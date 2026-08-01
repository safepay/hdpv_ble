# Hunter Douglas PowerView BLE for Home Assistant

[![GitHub Release](https://img.shields.io/github/v/release/safepay/hdpv_ble)](https://github.com/safepay/hdpv_ble/releases)
[![License](https://img.shields.io/github/license/safepay/hdpv_ble)](https://github.com/safepay/hdpv_ble/blob/main/LICENSE)
[![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2024.8.0+-blue.svg)](https://www.home-assistant.io/)

Control Hunter Douglas PowerView shades from Home Assistant over Bluetooth LE.
No cloud account, no vendor app in the loop, and no dependency on Hunter
Douglas's servers — Home Assistant talks to each shade directly.

A PowerView G3 hub is optional. If you have one, the integration will use it for
the handful of details the shades don't report reliably over the air.

## Requirements

- Home Assistant 2024.8.0 or newer.
- A Bluetooth adapter on your Home Assistant host, or one or more
  [ESPHome Bluetooth proxies](https://esphome.io/components/bluetooth_proxy)
  within range of the shades.
- Your home's **home key** — a 32-character hex string shared by every shade in
  the home. See [Getting the home key](#getting-the-home-key).
- Optionally, a PowerView G3 hub reachable over HTTP.

## Installation

### Via HACS

1. Open HACS in Home Assistant.
2. Click the three dots in the top right and choose **Custom repositories**.
3. Add `https://github.com/safepay/hdpv_ble` with category **Integration**.
4. Click **Install**, then restart Home Assistant.

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=safepay&repository=hdpv_ble&category=Integration)

### Manual

1. Open the directory holding your `configuration.yaml`.
2. Create `custom_components/hunterdouglas_powerview_ble/` if it doesn't exist.
3. Copy everything from `custom_components/hunterdouglas_powerview_ble/` in this
   repository into it.
4. Restart Home Assistant.

## Upgrading from the original integration

> [!WARNING]
> **Existing configuration does not carry over, and there is no migration path.**

This integration shares the domain `hunterdouglas_powerview_ble` with
[patman15/hdpv_ble](https://github.com/patman15/hdpv_ble), so the two cannot be
installed at the same time. It is not a drop-in replacement:

- The config entry format changed from version 1 to version 2. The original
  stored per-shade manufacturer data and read the encryption key from a constant
  compiled into `const.py`; this version stores the home key and optional hub URL
  with the entry, and discovers shades itself.
- No migration handler is provided, so Home Assistant will fail to load entries
  created by the original rather than converting them.

To move across:

1. Go to **Settings → Devices & Services** and delete every existing
   "Hunter Douglas PowerView (BLE)" entry.
2. Remove `patman15/hdpv_ble` in HACS.
3. Install this repository, restart, and set it up fresh.

Entity IDs, recorder history and long-term statistics from the original do not
survive this. Automations, scripts and dashboards referencing the old entity IDs
will need updating — check them before you delete anything.

## Setup

Shades are discovered automatically over Bluetooth, and a G3 hub is discovered
over zeroconf. When Home Assistant finds one, it asks for:

- **HomeKey** — required. 32 hex characters, e.g.
  `0102030405060708090a0b0c0d0e0f10`, or the `\xNN` escaped form.
- **PowerView hub URL** — optional, e.g. `http://192.168.1.50`.

Both are stored with the config entry and persist across updates. Use
**Configure** on the integration to change them later.

### Getting the home key

Every shade in a home shares one key. Three ways to obtain it:

1. **Adopt an emulated shade.** The [shade emulator](/emu/PV_BLE_cover) runs on
   an ESP32 (≥ 2 MiB flash, ≥ 128 KiB RAM) such as an
   [Adafruit QT Py ESP32-S3](https://www.adafruit.com/product/5426). Flash it,
   connect over serial, then add the shade `myPVcover` to your home in the
   PowerView app. The log prints `set shade key: \xx\xx...`. Copy it, then delete
   the emulated shade from the app.
2. **Extract it from a gateway.** [`scripts/extract_gateway3_homekey.py`](scripts/extract_gateway3_homekey.py)
   pulls the key from a working PowerView gateway.
3. **Recover it from the app.** See [this community forum post](https://community.home-assistant.io/t/hunter-douglas-powerview-gen-3-integration/424836/228).

### Connecting a hub

The hub is optional, but worth adding if you have one. With a hub URL set, the
integration reads two things over HTTP instead of Bluetooth:

- **Whether a shade is battery powered.** Shades do not report this reliably
  over the air, which previously caused battery entities to appear on hardwired
  shades and vice versa.
- **Friendly names.** Names follow whatever you have set in the PowerView app,
  and persist if the hub later goes offline.

## Entities

Each shade becomes one device. What it gets depends on its capabilities:

Platform | Entity | Notes
-- | -- | --
`cover` | Shade | Position, and tilt where supported. 100% is open
`cover` | Top rail / Bottom rail | Top-down/bottom-up shades only — one entity per rail
`number` | Velocity | Movement speed, 0–100. Configuration entity
`button` | Identify | Flashes the LED and beeps three times
`sensor` | Battery | 100% (full), 50%, 20%, 0% (empty)
`sensor` | Signal strength | BLE RSSI, in dBm. Diagnostic entity
`binary_sensor` | Charging | On while the battery is charging
`binary_sensor` | Clock reset required | Diagnostic, `problem` device class
`binary_sensor` | Mode reset required | Diagnostic, `problem` device class

## Supported shades

The type ID is shown in the PowerView app under *product info → type ID*.

Behaviour | Type IDs
-- | --
Position only | 1, 4, 5, 6, 19, 26, 27, 28, 31, 32, 38, 42, 49, 52, 53, 57, 65, 69, 70, 71, 79, 84, 95
Position and tilt | 51, 54, 55, 56, 62, 103
Tilt only | 39, 40, 66
Tilt when fully closed | 18, 23, 43, 44, 72
Top-down, single rail | 7, 10
Top-down/bottom-up, dual rail | 8, 9, 33, 47

Shades with two overlapping fabrics (9, 38, 65, 79, 95) are driven as a single
fabric — the second sheer or blackout layer is not exposed yet. On those types
the tilt of type 38 and the backlight of type 95 are also unavailable.

If your shade isn't listed, open an issue with its type ID and a diagnostics
download — [`scripts/shade_report.py`](scripts/shade_report.py) dumps the raw
bytes needed to work out a new type.

## How it works

Worth knowing, because it explains some of the behaviour:

- **Shade state arrives from Bluetooth advertisements**, not polling. Home
  Assistant listens rather than asking, so there is no configurable scan
  interval and no battery cost to reading state.
- **Out of range reports unknown, not stale.** If a shade stops advertising, its
  position, tilt and battery become unknown rather than silently holding the
  last value. Templates and automations should handle `unknown`.
- **Commands are sent over a direct connection**, so a shade must be reachable
  by an adapter or proxy at the moment you move it.

## Troubleshooting

For anything non-obvious:

1. Enable debug logging for the integration.
2. Reproduce the problem.
3. Disable the log — Home Assistant will offer the file for download.
4. From the shade's device page, choose **⋮ → Download diagnostics**.
5. [Open an issue](https://github.com/safepay/hdpv_ble/issues/new?assignees=&labels=Bug&projects=&template=bug.yml)
   describing what happened, and attach both files.

Diagnostics include the decoded shade state and capability flags with the home
key redacted, which is usually enough to identify a problem without a round trip.

## Known issues

<details><summary>Shade inoperable after charging</summary>

Shades appear to need re-initialising after a charge. The cause is not yet
understood; operating the shade once from the vendor app clears it.
</details>

## Credits

Originally written by [@patman15](https://github.com/patman15), with thanks to
[@mannkind](https://github.com/mannkind) and [@rspaargaren](https://github.com/rspaargaren).
This fork has diverged substantially — the config-flow home key, hub support,
the capability model, dual-rail shades and diagnostics are all new.

Licensed under the Apache License 2.0. See [LICENSE](LICENSE). One exception:
the ESP32 shade emulator in [`emu/`](emu/) is GPLv2, because it links wolfSSL.
It is a development tool and is not part of the integration HACS installs — see
[`emu/README.md`](emu/README.md).
