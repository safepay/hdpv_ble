# Hunter Douglas PowerView BLE for Home Assistant

[![GitHub Release](https://img.shields.io/github/v/release/safepay/hdpv_ble)](https://github.com/safepay/hdpv_ble/releases)
[![License](https://img.shields.io/github/license/safepay/hdpv_ble)](https://github.com/safepay/hdpv_ble/blob/main/LICENSE)
[![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2024.8.0+-blue.svg)](https://www.home-assistant.io/)

A Home Assistant integration that controls Hunter Douglas PowerView shades
directly over Bluetooth LE, with no cloud account and no vendor app in the loop.

## Features

- Zero configuration — shades are discovered over Bluetooth
- Home key entered through the config flow and stored with the config entry
- Position **and tilt** control, including top-down and tilt-only shades
- Supports [ESPHome Bluetooth proxy](https://esphome.io/components/bluetooth_proxy)
- Optional PowerView G3 hub link for data the shades don't report reliably over the air
- Downloadable diagnostics for every shade

### Supported Devices

Type\* | Description
-- | --
1 | Designer Roller
4 | Roman
5 | Bottom Up
6 | Duette
10 | Duette and Applause SkyLift
19 | Provenance Woven Wood
31, 32, 84 | Vignette
39 | Parkland
42 | M25T Roller Blind
49 | AC Roller
52 | Banded Shades
53 | Sonnette

\*) Type can be found in the PowerView app under *product info*, *type ID*

### Provided Entities

Platform | Entity | Details
-- | -- | --
`cover` | Shade | Position and, where the shade supports it, tilt. 100% is open
`number` | Velocity | Movement speed, 0–100 (configuration entity)
`button` | Identify | Identifies the shade by LED and 3 beeps
`sensor` | Battery | State of charge: 100% (full), 50%, 20%, 0% (empty)
`sensor` | Signal strength | BLE RSSI in dBm
`binary_sensor` | Charging | On while the battery is charging
`binary_sensor` | Clock reset required | Flags a shade whose clock needs resetting
`binary_sensor` | Mode reset required | Flags a shade whose mode needs resetting

Position, tilt and battery report *unknown* rather than a stale value when a
shade goes out of Bluetooth range.

## Installation

### Via HACS (recommended)

1. Open HACS in Home Assistant.
2. Click the three dots in the top right corner and select **Custom repositories**.
3. Add this repository URL: `https://github.com/safepay/hdpv_ble`
4. Select **Integration** as the category.
5. Click **Install**, then restart Home Assistant.

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=safepay&repository=hdpv_ble&category=Integration)

> [!IMPORTANT]
> This integration uses the same domain as the upstream project, so the two
> cannot be installed side by side. Remove `patman15/hdpv_ble` in HACS first.
> Because the domain matches, your existing shades, entity IDs, history and
> automations carry over — there is no need to re-add anything.

### Manual

1. Open the directory for your HA configuration (where `configuration.yaml` lives).
2. Create a `custom_components` directory there if you don't have one.
3. Inside it, create a folder called `hunterdouglas_powerview_ble`.
4. Download *all* the files from `custom_components/hunterdouglas_powerview_ble/`
   in this repository into that folder.
5. Restart Home Assistant.
6. Go to **Settings → Devices & Services → Add Integration** and search for
   "Hunter Douglas PowerView (BLE)".

## Configuration

Shades are discovered automatically. When one is found, Home Assistant asks for:

- **HomeKey** — 32 hex characters (e.g. `0102030405060708090a0b0c0d0e0f10`) or
  the `\xNN` escaped form. Required. See [Obtaining the home key](#obtaining-the-home-key).
- **PowerView hub URL** — optional, e.g. `http://192.168.1.50`. When set, the
  integration reads battery-powered status and friendly names from a G3 hub over
  HTTP, because the shades do not report those reliably over Bluetooth.

Both are stored with the config entry and persist across updates. To change
them later, use **Configure** on the integration.

### Obtaining the home key

Every shade in a home shares one key. There are three ways to get it:

1. **Adopt a shade emulator.** The [shade emulator](/emu/PV_BLE_cover) works with
   the Arduino IDE and an ESP32 (≥ 2 MiB flash, ≥ 128 KiB RAM), e.g. an
   [Adafruit QT Py ESP32-S3](https://www.adafruit.com/product/5426). Flash it,
   connect over serial, then add the shade `myPVcover` to your home in the
   PowerView app. You will see `set shade key: \xx\xx...` in the log. Copy that
   key, then delete the emulated shade from the app.
2. **Extract it from a gateway.** [This script](scripts/extract_gateway3_homekey.py)
   pulls the key from a working PowerView gateway.
3. **Grab it from the app.** See [this post](https://community.home-assistant.io/t/hunter-douglas-powerview-gen-3-integration/424836/228)
   in the Home Assistant community forum.

## Known Issues

<details><summary>Shade inoperable after charging</summary>
Shades appear to need some re-initialisation after charging. The cause is
currently unknown; as a workaround, operate the shade once using the vendor app.
</details>

## Troubleshooting

If you hit something serious:

1. Enable debug logging for the integration.
2. Reproduce the issue.
3. Disable the log — Home Assistant will prompt you to download it.
4. Download diagnostics for the affected shade from its device page
   (**⋮ → Download diagnostics**).
5. [Open an issue](https://github.com/safepay/hdpv_ble/issues/new?assignees=&labels=Bug&projects=&template=bug.yml)
   with a good description of what happened, and attach both files.

## Outlook

- Add tests
- Allow parallel usage with the PowerView app as a "remote"
- Support further device types

## Credits

Originally written by [@patman15](https://github.com/patman15), with thanks to
[@mannkind](https://github.com/mannkind). This fork has been substantially
modified from the original — most notably the config-flow home key, hub support,
tilt handling and diagnostics.

Licensed under the Apache License 2.0; see [LICENSE](LICENSE).
