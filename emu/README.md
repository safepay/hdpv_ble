# PowerView shade emulator

An ESP32 sketch that pretends to be a Hunter Douglas PowerView shade. Adopting
it into your home with the PowerView app makes the app hand over the home key,
which the emulator prints to the serial console.

This is the only way to obtain a home key without a G3 gateway. If you have a
gateway, [`scripts/extract_gateway3_homekey.py`](../scripts/extract_gateway3_homekey.py)
is easier. See [Getting the home key](../README.md#getting-the-home-key) in the
main README for the full procedure.

## Requirements

- An ESP32 with at least 2 MiB flash and 128 KiB RAM, such as an
  [Adafruit QT Py ESP32-S3](https://www.adafruit.com/product/5426)
- ESP32 board definitions 3.0.x (tested on 3.0.7)
- wolfSSL 5.7.x (tested on 5.7.6), installed through the Arduino library manager
- The device used for adopting the emulator MUST use the UK region. For example,
  on iPhones, go to Settings > General > Region & Language > Region and select
  United Kingdom. You can change the region back to your home region after adopting

`user_settings.h` is wolfSSL's configuration template and is included in the
build automatically; it does not need editing.

## License

**This directory is GPLv2, not Apache 2.0 like the rest of the repository.**
The sketch links wolfSSL, whose license covers the combined work, and
`user_settings.h` is wolfSSL's own GPLv2-licensed template. The emulator is a
development tool: HACS installs only `custom_components/`, so this code is
never distributed to users of the integration.

Do not relicense these files to match the root LICENSE. Several people hold
copyright in them.

## Note for anyone with hardware to test on

The sketch uses wolfSSL for one thing only: AES-128-CTR with a zero counter,
reset per message. mbedTLS ships with the ESP32 core and offers the same
primitive via `mbedtls_aes_crypt_ctr()`, which would remove the external
library dependency. It would not change the license of the sketch itself, and
nobody has tried it — the emulator runs on a first-time-setup path, so it wants
testing on real hardware before any such change is worth making.
