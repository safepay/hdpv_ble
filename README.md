# Hunter Douglas PowerView BLE for Home Assistant

[![GitHub Release](https://img.shields.io/github/v/release/safepay/hdpv_ble)](https://github.com/safepay/hdpv_ble/releases)
[![License](https://img.shields.io/github/license/safepay/hdpv_ble)](https://github.com/safepay/hdpv_ble/blob/main/LICENSE)
[![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2024.11.0+-blue.svg)](https://www.home-assistant.io/)

Control Hunter Douglas PowerView shades from Home Assistant over Bluetooth LE.
No cloud account, no vendor app, no dependency on Hunter Douglas's servers.

A PowerView G3 hub is optional: it supplies the home key and names your shades
the way the PowerView app does.

## Requirements

- Home Assistant 2024.11.0 or newer.
- A Bluetooth adapter on your Home Assistant host, or
  [ESPHome Bluetooth proxies](https://esphome.io/components/bluetooth_proxy)
  within range of the shades.
- Your home's **home key**, but only if your shades have been adopted into the
  PowerView app or a hub — see [Do you need a home key?](#do-you-need-a-home-key)

## Installation

### Via HACS

1. In HACS, click the three dots and choose **Custom repositories**.
2. Add `https://github.com/safepay/hdpv_ble` with category **Integration**.
3. Click **Install**, then restart Home Assistant.

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=safepay&repository=hdpv_ble&category=Integration)

### Manual

Copy `custom_components/hunterdouglas_powerview_ble/` from this repository into
the `custom_components/` directory beside your `configuration.yaml`, then
restart Home Assistant.

### Coming from patman15/hdpv_ble

> [!WARNING]
> Existing configuration does not carry over, and there is no migration path.

<details><summary>What to do</summary>

This integration shares the domain `hunterdouglas_powerview_ble` with
[patman15/hdpv_ble](https://github.com/patman15/hdpv_ble), so the two cannot be
installed together, and the config entry format changed from version 1 to
version 2. No migration handler exists, so Home Assistant fails entries created
by the original rather than converting them.

You already have the one thing setup asks for, though: the original compiled
the home key into its source, so it is sitting in your installed copy.

1. **Note down your home key first** — removing the integration deletes it.
   Copy `HOME_KEY` from
   `custom_components/hunterdouglas_powerview_ble/const.py`, e.g.
   `\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b\x0c\x0d\x0e\x0f\x10`. Keep the
   escapes; the setup form takes that form as-is.
2. Delete every existing "Hunter Douglas PowerView (BLE)" entry under
   **Settings → Devices & Services**.
3. Remove `patman15/hdpv_ble` in HACS.
4. Install this repository and restart Home Assistant.
5. Paste the home key into the **HomeKey** field when your rediscovered shades
   prompt for it.

Entity IDs, recorder history and long-term statistics do not survive this.
Update automations, scripts and dashboards referencing the old entity IDs.
</details>

## Setup

Shades are discovered over Bluetooth and a G3 hub over zeroconf. When Home
Assistant finds one, the setup form asks for a **HomeKey** and, optionally, a
**PowerView hub URL** such as `http://192.168.1.50`.

Both are stored with the config entry and persist across updates. Change them
later with **⋮ → Reconfigure**; shades keep their entity IDs and settings.

### Do you need a home key?

Only shades that have been adopted are encrypted, and only encrypted shades
need a key. Find your situation:

Your shades | What to do
-- | --
Never adopted — new, or factory reset | **No key needed.** Choose **Skip** under *Key source*; the shades talk unencrypted
Adopted, and you have a G3 gateway | The setup form fetches the key from the gateway for you
Adopted in the PowerView app, no gateway | You have to retrieve the key — see [Getting the home key](#getting-the-home-key)

If you are unsure, choose **Skip**. Shades that turn out to need a key appear
without controls, and a key can be added later through **⋮ → Reconfigure**
without losing anything.

When you do enter one, it is 32 hex characters — either
`0102030405060708090a0b0c0d0e0f10` or the `\xNN` escaped form. Every shade in a
home shares the same key.

### Getting the home key

Only needed for adopted shades that no gateway can supply the key for.

If you have a gateway but the setup form cannot reach it,
[`scripts/extract_gateway3_homekey.py`](scripts/extract_gateway3_homekey.py)
pulls the same key over HTTP: plain Python, any OS, no hardware.

Otherwise the key has to come from a shade or from the PowerView app. Least
effort first:

1. **Emulate a shade on Linux.** [`emu/linux`](/emu/linux) makes a Linux host's
   own Bluetooth adapter pretend to be a shade; adding `myPVcover` to your home
   in the PowerView app hands the key over. Booting a live USB stick is enough.
2. **Emulate a shade on an ESP32.** The [same emulator](/emu/PV_BLE_cover) as a
   sketch, for an ESP32 with at least 2 MiB flash and 128 KiB RAM such as an
   [Adafruit QT Py ESP32-S3](https://www.adafruit.com/product/5426). Flash it,
   adopt `myPVcover` as above, and the serial log prints
   `set shade key: \xx\xx...`. Worth it only if you have the board already.
3. **Read the PowerView app's database.**
   [`scripts/extract_homekey_waydroid.sh`](scripts/extract_homekey_waydroid.sh)
   walks through installing Waydroid, sideloading the app and reading the key
   out; tested on Ubuntu 24.04 only. This
   [community forum post](https://community.home-assistant.io/t/hunter-douglas-powerview-gen-3-integration/424836/228)
   describes the manual route.

Delete the emulated shade from the app once you have the key — routes 1 and 2
leave it registered in your home otherwise.

### Connecting a hub

The hub is optional. Over HTTP it contributes two things:

- **The home key**, during setup, which is easier than any route above.
- **Friendly names**, following whatever you have set in the PowerView app.
  They are cached with the config entry and survive the hub going offline.

Everything else — shade state, position, tilt and every command — is Bluetooth
either way. A shade out of Bluetooth range is unavailable even if the hub can
still see it.

## Entities

Each shade becomes one device. What it gets depends on its capabilities:

Platform | Entity | Notes
-- | -- | --
`cover` | Shade | Position, and tilt where supported. 100% is open
`cover` | Top rail / Bottom rail | Top-down/bottom-up shades only — one entity per rail
`cover` | Combined / Front / Rear | Dual-fabric shades only — see [Dual-fabric shades](#dual-fabric-shades)
`number` | Velocity | Movement speed as a percentage: 10 is slowest, 100 fastest, 0 leaves it to the shade. Configuration entity
`button` | Identify | Flashes the LED and beeps three times
`sensor` | Battery | 100% (full), 50%, 20%, 0% (empty). Disabled on shades detected as hardwired
`sensor` | Signal strength | BLE RSSI, in dBm. Diagnostic entity
`binary_sensor` | Charging | On while the battery is charging. Disabled on shades detected as hardwired
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

Which behaviour each type gets follows
[`aiopvapi`](https://github.com/sander76/aio-powerview-api), the library behind
Home Assistant's official hub-based PowerView integration, so a shade behaves
the same here as it does over the hub.

If your shade isn't listed, open an issue with its type ID and a diagnostics
download. [`scripts/shade_report.py`](scripts/shade_report.py) dumps the same
raw bytes without your shade's name or serial number, if you have a G3 hub for
it to read the home key from.

### Dual-fabric shades

> [!NOTE]
> **Experimental.** These entities were written from the hub API's model rather
> than from a Duolite shade on the bench. If yours moves the wrong fabric, moves
> the wrong way, or doesn't move at all, open an issue with a diagnostics
> download — that is enough to correct the mapping.

Types 38, 65, 79 and 95 hang a sheer fabric and an opaque fabric on one motor,
and get three cover entities:

Entity | Controls
-- | --
Combined | Both fabrics on one 0–100 scale: 0–50 moves the rear opaque fabric, 51–100 the front sheer. Use this unless you need the fabrics apart
Front | The front sheer fabric alone
Rear | The rear opaque fabric alone

Type 38 (Silhouette Duolite) also tilts, on its combined entity. The backlight
of type 95 (Aura Illuminated) is not exposed.

## How it works

- **Shade state arrives from Bluetooth advertisements**, not polling, so there
  is no scan interval to configure and no battery cost to reading state.
- **Out of range reports unknown, not stale.** A shade that stops advertising
  reports its position, tilt and battery as unknown rather than silently
  holding the last value, so templates and automations should handle `unknown`.
- **Commands go over a direct connection**, so a shade must be reachable by an
  adapter or proxy at the moment you move it.

## Troubleshooting

**Shades appear but have no controls.** The shade is encrypted and no valid home
key is configured, so the cover entity reports its position but offers nothing
to drive it with. Diagnostics show `"encrypted": true` alongside
`"home_key_configured": false`. Fix it with **⋮ → Reconfigure**. If a key fetch
fails against a `.local` hub URL, try the hub's IP address — mDNS often does
not resolve from inside a container.

For anything else:

1. Enable debug logging for the integration.
2. Reproduce the problem.
3. Disable the log — Home Assistant will offer the file for download.
4. From the shade's device page, choose **⋮ → Download diagnostics**.
5. [Open an issue](https://github.com/safepay/hdpv_ble/issues/new?assignees=&labels=Bug&projects=&template=bug.yml)
   describing what happened, and attach both files.

Both files are required. Diagnostics carry the decoded shade state and
capability flags with the home key redacted, which is usually enough to
identify a problem without a round trip. [CONTRIBUTING.md](CONTRIBUTING.md)
lists what else to include, particularly the physical detail diagnostics can't
capture.

## Known issues

<details><summary>Battery entities on hardwired shades</summary>

A mains-wired shade gets its `Battery` and `Charging` entities created
**disabled** rather than withheld, so a shade we misread costs you one toggle
on its device page instead of a missing sensor. The power source comes from the
hub's `powerType`, or from one Bluetooth query per shade; anything unreadable
keeps its battery entities, as do all type 10 shades.

Entities that already exist are never changed — remove and re-add a shade to
re-evaluate it. See [#23](https://github.com/safepay/hdpv_ble/issues/23).
</details>

<details><summary>Schedules stop after a shade loses power</summary>

A shade that loses power — a battery wand pulled for charging, or mains
interrupted — restarts without a valid clock, so schedules stored on the shade
stop running, and it reports this as the `Clock reset required` diagnostic
sensor. Movement commands still work; only the shade's own timed behaviour is
affected.

Operating the shade once from the vendor app clears it. Doing the same from
Home Assistant is not yet possible — see
[#5](https://github.com/safepay/hdpv_ble/issues/5).
</details>

## Contributing

Every shade the maintainer owns is hardwired, so data from other shades is the
most useful contribution there is — battery-powered shades
([#23](https://github.com/safepay/hdpv_ble/issues/23)) and dual-fabric shades
([#13](https://github.com/safepay/hdpv_ble/issues/13)) especially, both of which
need nothing more than a diagnostics download to settle.

Read [CONTRIBUTING.md](CONTRIBUTING.md) first. Reports and pull requests
without a debug log and a diagnostics download will not be considered, and
changing which entities a shade type gets needs evidence from the shade itself
— a product name is not evidence.

## Credits

Originally written by [@patman15](https://github.com/patman15), with thanks to
[@mannkind](https://github.com/mannkind) and
[@rspaargaren](https://github.com/rspaargaren). This fork has diverged
substantially — the config-flow home key, hub support, the capability model,
dual-rail shades and diagnostics are all new.

Licensed under the Apache License 2.0 — see [LICENSE](LICENSE). The shade
emulator in [`emu/`](emu/) is the exception: GPLv2, as a derivative of the
ESP32 sketch and, for the Linux port, of BlueZ's example scripts. It is a
development tool, not part of what HACS installs — see
[`emu/README.md`](emu/README.md).
