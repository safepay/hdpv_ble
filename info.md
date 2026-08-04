# Hunter Douglas PowerView BLE

Control Hunter Douglas PowerView shades from Home Assistant over Bluetooth LE.
No cloud account, no vendor app in the loop — Home Assistant talks to each shade
directly. A PowerView G3 hub is optional.

## Before you install

- **Home Assistant 2024.8.0 or newer.**
- **A Bluetooth adapter on your Home Assistant host**, or one or more
  [ESPHome Bluetooth proxies](https://esphome.io/components/bluetooth_proxy)
  within range of the shades.
- **Your home key** — a 32-character hex string shared by every shade in the
  home. Setup asks for it and cannot proceed without it.

If you have a G3 hub, the setup form fetches the home key for you. If you don't,
get it before you start — see
[Getting the home key](https://github.com/safepay/hdpv_ble#getting-the-home-key)
for the three ways to obtain one.

## Upgrading from the original integration

> **Existing configuration does not carry over, and there is no migration path.**

This integration shares the domain `hunterdouglas_powerview_ble` with
[patman15/hdpv_ble](https://github.com/patman15/hdpv_ble), so **the two cannot be
installed at the same time**, and Home Assistant will fail to load config entries
created by the original.

Do this first, before removing anything: open
`custom_components/hunterdouglas_powerview_ble/const.py` in your existing install
and copy the value of `HOME_KEY` — that is the key setup will ask you for, and
uninstalling deletes the file. Then delete the old config entries, remove
`patman15/hdpv_ble` in HACS, and install this one.

Entity IDs, history and statistics do not survive the move, so automations and
dashboards referencing the old entity IDs need updating.

## After installing

1. **Restart Home Assistant.**
2. Shades are discovered automatically over Bluetooth; a G3 hub is discovered
   over zeroconf. No YAML.
3. On the dialog that appears, fill in:
   - **HomeKey** — required. 32 hex characters, e.g.
     `0102030405060708090a0b0c0d0e0f10`, or the `\xNN` escaped form.
   - **PowerView hub URL** — optional, e.g. `http://192.168.1.50`.

Both are stored with the config entry and persist across updates. Use
**Configure** on the integration to change them later.

## Worth knowing

- **The hub is optional and only adds two things:** the home key during setup,
  and shade names as set in the PowerView app. Every command and all shade state
  is Bluetooth, hub or no hub.
- **Shade state arrives from Bluetooth advertisements**, so there is no scan
  interval to configure. A shade out of range reports `unknown` rather than a
  stale value — handle that in templates and automations.
- **Battery and Charging entities are created for every shade**, mains-wired or
  not, because the shades don't report their power source reliably. Hide them
  from the device page if you don't want them.
- **Dual-fabric (Duolite) shades are experimental** — types 38, 65, 79 and 95.

Check your shade's type ID against the
[supported shades table](https://github.com/safepay/hdpv_ble#supported-shades);
the PowerView app shows it under *product info → type ID*. If yours isn't listed,
open an issue with the type ID and a diagnostics download.

## Reporting a problem, or contributing

Every shade the maintainer owns is hardwired, so reports from other shades are
what move this integration forward — battery shades and dual-fabric (Duolite)
shades especially.

**Read
[CONTRIBUTING.md](https://github.com/safepay/hdpv_ble/blob/main/CONTRIBUTING.md)
before opening an issue or a pull request.** Its requirements are not optional: a
report needs a debug log and a diagnostics download, pull requests submitted
without them will not be considered, and a change to which entities a shade type
gets needs evidence from the shade itself.

Full documentation: [safepay/hdpv_ble](https://github.com/safepay/hdpv_ble)
