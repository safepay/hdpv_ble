# PowerView shade emulator — Linux port

Makes a Linux host's own Bluetooth adapter impersonate a PowerView shade. Adopt
it into your home with the PowerView app and the app hands over your home key,
which the emulator prints to the console.

Nothing has to be installed permanently — booting a live USB stick and using
the machine's own Bluetooth adapter is enough. For the ESP32 version of the
same trick, see [`../PV_BLE_cover`](../PV_BLE_cover); for how it fits into
setup, see [Getting the home key](../../README.md#getting-the-home-key).

## Quick start

1. Install the dependencies (Debian/Ubuntu shown; see
   [Requirements](#requirements) if your distro differs):

   ```sh
   sudo apt install python3-dbus python3-gi python3-cryptography
   ```

2. Start the emulator:

   ```sh
   sudo python3 shade_emulator.py -v
   ```

3. In the PowerView app, add a shade to your home. It appears as
   **myPVcover**.

4. The console prints your home key:

   ```text
   home key installed - paste into Home Assistant's home_key field: 0123456789abcdef0123456789abcdef
   ```

5. Paste those 32 hex characters into the **HomeKey** field in the
   integration's config flow.

6. Delete **myPVcover** from your home in the PowerView app.

> [!IMPORTANT]
> Step 6 is not optional if you intend to run the emulator again. While
> myPVcover is still adopted, the app talks to it encrypted with the key it
> installed, and the emulator has no way to answer.

## Requirements

- A Linux host with a BLE-capable Bluetooth adapter and BlueZ ≥ 5.50, with
  `bluetoothd` running. Tested on BlueZ 5.72.
- `python3-dbus`, `python3-gi` (PyGObject) and `python3-cryptography`, from
  your distro's packages. The first two wrap system D-Bus/GLib libraries and
  rarely install cleanly from PyPI; on Debian 12 and derivatives `pip` refuses
  to touch the system Python at all (PEP 668), and since the script needs root
  a virtualenv is awkward.
- Root, or a polkit rule granting your user `org.bluez.*` — registering a GATT
  application and an LE advertisement are both privileged BlueZ operations.

## Options

`-v` logs every decoded message. Without it, only key events — registration and
the extracted home key — are logged.

`--adapter hci1` picks an adapter other than the first one BlueZ reports as
GATT/advertising-capable. A host with both a built-in radio and a dongle has
two and the numbering is not guaranteed, so check `hciconfig -a` first.

## Checking that it's advertising

A single adapter scanning and advertising at once can't reliably see itself, so
the dependable local check is BlueZ's own view of what it told the controller
to do:

```sh
sudo btmon &
sudo python3 shade_emulator.py -v &
```

`btmon` should show an LE Set Advertising Data command containing
`Company: Hunter Douglas Inc (2073)` and the local name `myPVcover`. Any second
Bluetooth device nearby — a phone, another PC — will also see `myPVcover` by
scanning normally.

## How the port works

Protocol, message IDs and static responses are ported byte-for-byte from
[`../PV_BLE_cover/PV_BLE_cover.ino`](../PV_BLE_cover/PV_BLE_cover.ino).
wolfSSL's AES-128-CTR is replaced with Python's `cryptography` package: a
CTR-mode keystream depends only on key, nonce and plaintext, not on the
implementation, so the output is identical and the emulator stays
interoperable with the real PowerView app.

- `shade_emulator.py`'s `ShadeProtocol` implements the full command set from
  the sketch's `decode()` — product info, position/scene/time/config writes,
  HW diagnostics, power status, identify, factory reset and the `0xFB02`
  home-key exchange.
- The advertisement matches the sketch's manufacturer data: company ID 2073
  (Hunter Douglas), same 9-byte payload keyed by `TYP_ID`.
- `ble_peripheral.py` is a small, sketch-independent BlueZ D-Bus GATT-server
  and advertising framework — the Linux equivalent of the
  `BLEDevice`/`BLEServer`/`BLECharacteristic` classes from the ESP32 Arduino
  BLE library. Nothing PowerView-specific lives there.
- Nothing hardware-specific was left unported. There is no serial console, LED
  or `loop()` polling here; BlueZ drives everything through D-Bus callbacks and
  GLib's mainloop.

## License

**This directory is GPLv2, not Apache 2.0 like the rest of the repository** —
not because it links wolfSSL (this port doesn't), but because it derives from
[`../PV_BLE_cover`](../PV_BLE_cover)'s `PV_BLE_cover.ino` (`shade_emulator.py`)
and from BlueZ's own GPLv2-licensed `test/example-gatt-server` and
`test/example-advertisement` scripts (`ble_peripheral.py`). See
[`../README.md`](../README.md#license).
