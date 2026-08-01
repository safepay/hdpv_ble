#!/usr/bin/env python3
"""Probe the payload layout of 0xFF77, "set shade time".

Sending 0xFF77 with the 7-byte payload the emulator documents (year LE
uint16, month, day, hour, minute, second) gets status 0x04 back from a
hardwired Duette on fw_rev=22 -- "invalid length", per PV_ERROR_CODES in
shade_report.py.

The opcode itself is right: the shade echoes 0x77EF with a matching
sequence number and a well-formed 1-byte status, exactly as a successful
0x01F7 does, rather than failing to parse the request.  So it is the
payload that is wrong, and the emulator would never have caught that --
its 0xFF77 branch reads indices 4..10 and acks whatever arrives without
ever checking msg.data_len.

This sweeps candidate payloads and prints the status byte for each, to
find the shape the firmware actually accepts.

UNLIKE shade_report.py, THIS SCRIPT WRITES.  It sends exactly one opcode,
0xFF77, and nothing else -- no move, no scene, no rekey, no power-type
change, no factory reset.  Setting a shade's clock is not destructive,
and a shade worth probing has an invalid clock already; a successful
probe finishes by writing the correct time.

Usage:
    python -m scripts.probe_set_time --ble-name DUE:7C82
"""

from __future__ import annotations

from pathlib import Path
import sys

# Allow "python scripts/probe_set_time.py" to resolve the sibling package
# imports below: prepend the project root so `scripts.*` is importable
# regardless of CWD. Mirrors shade_report.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import asyncio
from datetime import datetime

from scripts.extract_gateway3_homekey import HUB, get_shade_key
from scripts.shade_report import (
    SCAN_TIMEOUT,
    PowerViewClient,
    fetch_shade_list,
    find_shades,
)

# Emulator naming: wire byte 0 = 0xFF, byte 1 = 0x77.  PowerViewClient.query
# packs big-endian, so this constant is used as-is (the integration's enum
# stores the byte-swapped 0x77FF and packs little-endian to the same wire).
CMD_SET_TIME = 0xFF77

# Reply status byte.  0x04 is the only non-zero code observed so far; see
# PV_ERROR_CODES in shade_report.py.
STATUS_LABELS: dict[int, str] = {
    0x00: "OK",
    0x04: "invalid length",
}

MAX_PAYLOAD = 16


def _candidates(now: datetime) -> list[tuple[str, bytes]]:
    """Return (label, payload) pairs to try, cheapest hypothesis first.

    Two families.  The length sweep finds the accepted size on the
    assumption that firmware validates length before content -- which the
    0x04 "invalid length" code suggests it does.  The variants then cover
    the case where a length is accepted but the field order differs from
    the emulator's.
    """
    year_le = int.to_bytes(now.year, 2, "little")
    core = year_le + bytes([now.month, now.day, now.hour, now.minute, now.second])
    # Correct time in the fields we know, zeros beyond them.  A zero tail is
    # enough to identify the right length even if those trailing fields mean
    # something; content gets pinned down once the length is known.
    padded = core + bytes(MAX_PAYLOAD)

    out: list[tuple[str, bytes]] = [
        (f"sweep len {n:>2}", padded[:n]) for n in range(4, MAX_PAYLOAD + 1)
    ]

    dow_mon0 = now.weekday()  # Monday = 0, as Python counts
    dow_sun0 = (now.weekday() + 1) % 7  # Sunday = 0, as most RTCs count
    yy = now.year % 100
    out += [
        ("var  +dow (Mon=0)", core + bytes([dow_mon0])),
        ("var  +dow (Sun=0)", core + bytes([dow_sun0])),
        ("var  year BE", int.to_bytes(now.year, 2, "big") + core[2:]),
        ("var  1-byte year", bytes([yy]) + core[2:]),
        (
            "var  1-byte year +dow",
            bytes([yy]) + core[2:] + bytes([dow_sun0]),
        ),
        ("var  dow first", bytes([dow_sun0]) + core),
    ]
    return out


def _describe(reply: bytes) -> tuple[int | None, str]:
    """Turn a reply payload into (status, human description)."""
    if len(reply) != 1:
        return None, f"unexpected {len(reply)}-byte payload: {reply.hex(' ')}"
    code = reply[0]
    return code, STATUS_LABELS.get(code, "unknown code")


def _fetch_home_key(hub: str) -> bytes | None:
    """Fetch the home-wide key, trying the shades the hub hears best first."""
    print(f"Fetching shade list from {hub}...")
    try:
        shades = fetch_shade_list(hub)
    except Exception as ex:  # noqa: BLE001
        print(f"  failed: {ex}")
        return None

    for shade in sorted(
        shades, key=lambda s: s.get("signalStrength", -100), reverse=True
    ):
        try:
            return get_shade_key(hub, shade["bleName"])
        except Exception as ex:  # noqa: BLE001
            print(f"  {shade['bleName']}: {ex} — trying next shade...")
    print("Could not fetch the homekey from any shade.")
    return None


async def _sweep(
    api: PowerViewClient, candidates: list[tuple[str, bytes]]
) -> list[tuple[int, str, bytes]]:
    """Send every candidate, print the shade's status, return the accepted."""
    accepted: list[tuple[int, str, bytes]] = []
    for idx, (label, payload) in enumerate(candidates):
        try:
            reply = await api.query(CMD_SET_TIME, payload)
        except (TimeoutError, ValueError) as ex:
            print(f"  {label:<22} {payload.hex(' '):<50} -- {ex}")
            continue
        code, note = _describe(reply)
        flag = "  <== ACCEPTED" if code == 0 else ""
        shown = f"0x{code:02X} {note}" if code is not None else note
        print(f"  {label:<22} {payload.hex(' '):<50} {shown}{flag}")
        if code == 0:
            accepted.append((idx, label, payload))
    return accepted


async def _probe(ble_name: str, hub: str, scan_timeout: float) -> int:
    home_key = _fetch_home_key(hub)
    if home_key is None:
        return 1

    print(f"Scanning up to {scan_timeout:.0f}s for {ble_name}...")
    seen = await find_shades({ble_name}, scan_timeout)
    dev, _adv = seen.get(ble_name, (None, None))
    if dev is None:
        print(f"  {ble_name} not seen on air. Aborting.")
        return 1

    now = datetime.now().replace(microsecond=0)
    candidates = _candidates(now)
    print(
        f"\nProbing {ble_name} with 0xFF77, reference time "
        f"{now:%Y-%m-%d %H:%M:%S} ({len(candidates)} candidates)\n"
    )
    print(f"  {'candidate':<22} {'payload':<50} status")
    print(f"  {'-' * 22} {'-' * 50} {'-' * 20}")

    async with PowerViewClient(dev, home_key) as api:
        accepted = await _sweep(api, candidates)

        print()
        if not accepted:
            print("No candidate was accepted. Every reply was a rejection.")
            print(
                "Next step is a btsnoop capture of the vendor app setting the "
                "time, to read the real payload off the wire."
            )
            return 1

        print(f"{len(accepted)} candidate(s) accepted:")
        for _, label, payload in accepted:
            print(f"  {label}: {payload.hex(' ')}")

        # The sweep set the clock to whatever the winning candidate carried,
        # which is now a little stale. Regenerate the same candidate from a
        # fresh timestamp -- by index, so no assumption is made about where
        # that candidate puts its fields -- and send it once more.
        idx, label, _ = accepted[0]
        final = datetime.now().replace(microsecond=0)
        payload = _candidates(final)[idx][1]
        code, note = _describe(await api.query(CMD_SET_TIME, payload))
        stamp = f"{final:%Y-%m-%d %H:%M:%S}"
        result = f"0x{code:02X} {note}" if code is not None else note
        print(f"\nRe-sent '{label}' with {stamp}: {result}")
    print(
        "\nWatch the shade's next advertisement: byte 8 bit 1 (reset_clock) "
        "should now be clear."
    )
    return 0


def main() -> int:
    """Parse CLI args and run the probe."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hub", default=HUB, help=f"Gateway URL (default: {HUB})")
    parser.add_argument(
        "--ble-name", required=True, help="BLE name of the shade, e.g. 'DUE:7C82'"
    )
    parser.add_argument(
        "--scan-timeout",
        type=float,
        default=SCAN_TIMEOUT,
        help=f"BLE scan timeout in seconds (default: {SCAN_TIMEOUT})",
    )
    args = parser.parse_args()
    return asyncio.run(_probe(args.ble_name, args.hub, args.scan_timeout))


if __name__ == "__main__":
    raise SystemExit(main())
