# PowerView shade emulator — Linux port

A port of [`../PV_BLE_cover`](../PV_BLE_cover) that runs directly on a
Linux host's own Bluetooth adapter through BlueZ, instead of on an
ESP32. Same purpose and same procedure: adopting it into your home
with the PowerView app makes the app hand over the home key, which
this prints to the console — see [Getting the home
key](../../README.md#getting-the-home-key) in the main README.

Protocol, message IDs and static responses are ported byte-for-byte
from `../PV_BLE_cover/PV_BLE_cover.ino`. wolfSSL's AES-128-CTR is
replaced with Python's `cryptography` package: CTR-mode keystream
depends only on key, nonce and plaintext, not the implementation, so
output is identical and the emulator stays interoperable with the real
PowerView app.

## Requirements

- A Linux host with a BLE-capable Bluetooth adapter and BlueZ ≥ 5.50
  (`bluetoothd` running; tested on BlueZ 5.72).
- A Bluetooth 5.0+ adapter with LE Extended Advertising support. The
  advertisement (flags, 128-bit service UUID, manufacturer data and local
  name) comes to 45 bytes, over the 31-byte legacy advertising limit, so it
  only fits under extended advertising — this is what the `btmon` hint below
  is actually showing. A legacy-only 4.x adapter fails `RegisterAdvertisement`
  outright.
- `python3-dbus` and `python3-gi` (PyGObject) — install via your
  distro's package manager, e.g. `sudo apt install python3-dbus
  python3-gi`. These wrap system D-Bus/GLib libraries and generally
  don't install cleanly from PyPI.
- The `cryptography` Python package (`pip install cryptography`).
- Root, or a polkit rule granting your user `org.bluez.*` — registering
  a GATT application and an LE advertisement are privileged BlueZ
  operations.

## Usage

```sh
sudo python3 shade_emulator.py -v
```

Pass `--adapter hci1` to pick an adapter other than the first one BlueZ
reports as GATT/advertising-capable. `-v` logs every decoded message;
without it, only key events (registration, the extracted home key) are
logged.

Add the shade **myPVcover** to your home in the PowerView app. The
console prints a line like:

```text
home key installed - paste into Home Assistant's home_key field: 0123456789abcdef0123456789abcdef
```

Paste that into the integration's config flow, then delete the
emulated shade from the app.

## What's ported vs. what isn't

- The full command set from the `.ino` sketch's `decode()` — product
  info, position/scene/time/config writes, HW diagnostics, power
  status, identify, factory reset and the `0xFB02` home-key exchange —
  is implemented in `shade_emulator.py`'s `ShadeProtocol`.
- The advertisement matches the sketch's manufacturer data: company ID
  2073 (Hunter Douglas), same 9-byte payload keyed by `TYP_ID`.
- `ble_peripheral.py` is a small, sketch-independent BlueZ D-Bus
  GATT-server/advertising framework (the Linux equivalent of the
  `BLEDevice`/`BLEServer`/`BLECharacteristic` classes from the ESP32
  Arduino BLE library) — nothing PowerView-specific lives there.
- Not ported: nothing hardware-specific was left to port. There's no
  serial console, LED or `loop()` polling here — BlueZ drives
  everything through D-Bus callbacks and GLib's mainloop.

## Verifying it's advertising

Since scanning and advertising on a single local adapter can't always
see each other, the most reliable local check is BlueZ's own view of
what it told the controller to do:

```sh
sudo btmon &
sudo python3 shade_emulator.py -v &
# btmon output should show an LE Set (Extended) Advertising Data command
# containing "Company: Hunter Douglas Inc (2073)" and local name "myPVcover"
```

A second Bluetooth-capable device (phone, another PC) scanning nearby
will see `myPVcover` normally.

## License

**This directory is GPLv2, not Apache 2.0 like the rest of the
repository** — not because it links wolfSSL (this port doesn't), but
because it's a derivative of [`../PV_BLE_cover`](../PV_BLE_cover)'s
`PV_BLE_cover.ino` (`shade_emulator.py`) and of BlueZ's own GPLv2-licensed
`test/example-gatt-server`/`test/example-advertisement` scripts
(`ble_peripheral.py`). See [`../README.md`](../README.md#license).
