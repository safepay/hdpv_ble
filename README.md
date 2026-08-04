# Hunter Douglas PowerView BLE for Home Assistant

[![GitHub Release](https://img.shields.io/github/v/release/safepay/hdpv_ble)](https://github.com/safepay/hdpv_ble/releases)
[![License](https://img.shields.io/github/license/safepay/hdpv_ble)](https://github.com/safepay/hdpv_ble/blob/main/LICENSE)
[![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2024.8.0+-blue.svg)](https://www.home-assistant.io/)

Control Hunter Douglas PowerView shades from Home Assistant over Bluetooth LE.
No cloud account, no vendor app in the loop, and no dependency on Hunter
Douglas's servers — Home Assistant talks to each shade directly.

A PowerView G3 hub is optional. If you have one, it saves you finding the home
key by hand and names your shades the way the PowerView app does.

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

You do, however, already have the one thing setup asks for. The original's home
key was compiled in, so it is sitting in your installed copy — copy it out before
you uninstall anything, and setting up again is just pasting it back.

To move across:

1. **Note down your home key.** Open
   `custom_components/hunterdouglas_powerview_ble/const.py` in your existing
   install and copy the value of `HOME_KEY`, e.g.
   `\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b\x0c\x0d\x0e\x0f\x10`. Keep the
   escapes — the setup form takes that form as-is. Removing the integration
   deletes this file, so do it first.
2. Go to **Settings → Devices & Services** and delete every existing
   "Hunter Douglas PowerView (BLE)" entry.
3. Remove `patman15/hdpv_ble` in HACS.
4. Install this repository and restart Home Assistant.
5. Your shades are rediscovered over Bluetooth. Paste the home key into the
   **HomeKey** field on the dialog that appears.

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

Every shade in a home shares one key. If you have a G3 hub, the setup form will
fetch it for you — skip to [Connecting a hub](#connecting-a-hub). Otherwise there
are three ways to obtain it:

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

The hub is optional, but worth adding if you have one. It contributes two things
over HTTP:

- **The home key**, during setup. The setup form offers to fetch it from the hub,
  which is easier than any of the methods above.
- **Friendly names.** Names follow whatever you have set in the PowerView app.
  They are cached with the config entry, so they survive the hub going offline.

Everything else — shade state, position, tilt and every command — is Bluetooth,
whether a hub is configured or not. A shade out of Bluetooth range is unavailable
even if the hub can still see it.

## Entities

Each shade becomes one device. What it gets depends on its capabilities:

Platform | Entity | Notes
-- | -- | --
`cover` | Shade | Position, and tilt where supported. 100% is open
`cover` | Top rail / Bottom rail | Top-down/bottom-up shades only — one entity per rail
`cover` | Combined / Front / Rear | Dual-fabric shades only — see [Dual-fabric shades](#dual-fabric-shades)
`number` | Velocity | Movement speed, 0–100. Configuration entity
`button` | Identify | Flashes the LED and beeps three times
`sensor` | Battery | 100% (full), 50%, 20%, 0% (empty). Created for every shade — see [Known issues](#known-issues)
`sensor` | Signal strength | BLE RSSI, in dBm. Diagnostic entity
`binary_sensor` | Charging | On while the battery is charging. Created for every shade
`binary_sensor` | Clock reset required | Diagnostic, `problem` device class
`binary_sensor` | Mode reset required | Diagnostic, `problem` device class

## Supported shades

The type ID is shown in the PowerView app under *product info → type ID*.

Behaviour | Type IDs
-- | --
Position only | 1, 4, 5, 6, 10, 19, 26, 27, 28, 31, 32, 42, 49, 52, 53, 57, 69, 70, 71, 84
Position and tilt | 51, 54, 55, 56, 62, 103
Tilt only | 39, 40, 66
Tilt when fully closed | 18, 23, 43, 44, 72
Top-down, single rail | 7
Top-down/bottom-up, dual rail | 8, 9, 33, 47
Dual fabric, front sheer and rear opaque | 38, 65, 79, 95

Which behaviour each type gets follows [`aiopvapi`](https://github.com/sander76/aio-powerview-api),
the library behind Home Assistant's official hub-based PowerView integration, so
a shade behaves the same here as it does over the hub.

If your shade isn't listed, open an issue with its type ID and a diagnostics
download. [`scripts/shade_report.py`](scripts/shade_report.py) dumps the same raw
bytes without your shade's name or serial number, if you have a G3 hub for it to
read the home key from.

### Dual-fabric shades

> [!NOTE]
> **Experimental.** These entities were written from the hub API's model of
> these shades rather than from a Duolite shade on the bench. If yours moves the
> wrong fabric, moves the wrong way, or doesn't move at all, please open an issue
> with a diagnostics download — that is enough to correct the mapping.

Types 38, 65, 79 and 95 hang a sheer fabric and an opaque fabric on one motor.
They get three cover entities:

Entity | Controls
-- | --
Combined | Both fabrics on one 0–100 scale: 0–50 moves the rear opaque fabric, 51–100 moves the front sheer. Use this one unless you need the fabrics apart
Front | The front sheer fabric alone
Rear | The rear opaque fabric alone

Type 38 (Silhouette Duolite) also tilts, on its combined entity. The backlight of
type 95 (Aura Illuminated) is still not exposed.

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
Both files are required — see [CONTRIBUTING.md](CONTRIBUTING.md) for what else to
include, particularly the physical detail diagnostics can't capture.

## Known issues

<details><summary>Battery entities appear on hardwired shades</summary>

Every shade gets a `Battery` sensor and a `Charging` binary sensor, mains-wired
or not. On a hardwired shade the battery sits at a constant 100%.

The power source is not something the shades report reliably: the advertisement's
power level is two bits wide and its top code means "100% to 51% remaining" *or*
"hardwired", with no way to tell them apart. An earlier attempt to detect it
misread a status byte as a power-type enum and stripped the battery sensors off
real battery shades, so it was removed rather than left to guess.

Hide the entities you don't want from the shade's device page. Resolving it
properly needs data from a battery-powered shade — see
[#23](https://github.com/safepay/hdpv_ble/issues/23) if you have one.
</details>

<details><summary>Schedules stop after a shade loses power</summary>

A shade that loses power — a battery wand pulled for charging, or mains
interrupted — restarts without a valid clock, so schedules stored on the shade
stop running. It reports this as the `Clock reset required` diagnostic sensor.
The shade still takes movement commands normally; only its own timed behaviour
is affected.

Operating the shade once from the vendor app clears it. Doing the same directly
from Home Assistant is not yet possible — see
[#5](https://github.com/safepay/hdpv_ble/issues/5).
</details>

## Contributing

Every shade the maintainer owns is hardwired, so data from other shades is the
most useful contribution there is — battery-powered shades
([#23](https://github.com/safepay/hdpv_ble/issues/23)) and dual-fabric shades
([#13](https://github.com/safepay/hdpv_ble/issues/13)) especially, both of which
need nothing more than a diagnostics download to settle.

Read [CONTRIBUTING.md](CONTRIBUTING.md) before opening an issue or a pull
request. Reports and pull requests without a debug log and a diagnostics download
will not be considered, and a change to which entities a shade type gets needs
evidence from the shade itself — a product name is not evidence.

## Credits

Originally written by [@patman15](https://github.com/patman15), with thanks to
[@mannkind](https://github.com/mannkind) and [@rspaargaren](https://github.com/rspaargaren).
This fork has diverged substantially — the config-flow home key, hub support,
the capability model, dual-rail shades and diagnostics are all new.

Licensed under the Apache License 2.0. See [LICENSE](LICENSE). One exception:
the ESP32 shade emulator in [`emu/`](emu/) is GPLv2, because it links wolfSSL.
It is a development tool and is not part of the integration HACS installs — see
[`emu/README.md`](emu/README.md).
