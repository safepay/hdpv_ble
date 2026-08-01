#!/usr/bin/env python3
"""Probe the payload layout of 0xFF77, "set shade time".

The emulator documents a 7-byte payload (year LE uint16, month, day,
hour, minute, second) and its 0xFF77 branch acks whatever arrives without
ever checking msg.data_len -- so an incomplete layout was invisible to
whoever wrote it.  Real firmware validates.

Phase 1 (default) sweeps payload lengths and a few structural variants.
Against a hardwired Duette on fw_rev=22 it found:

    length != 8                     -> 0x04, invalid length
    length 8, trailing byte 0x00    -> 0x80, length accepted, value refused
    length 8, trailing byte 0x06    -> 0x00, accepted

So the payload is 8 bytes: the emulator's 7 plus a trailing day-of-week,
and the firmware validates length before content.

Phase 2 (--dow-sweep) pins down what that trailing byte means, which
phase 1 cannot: the run above was on a Sunday, where Python's weekday()
returns 6, and two readings both fit.  Either the field is Mon=0..Sun=6
and the shade cross-checks it against the date (so only one value is ever
valid for a given day), or it is a 1..7 range checked only for bounds (so
6 was accepted merely for being in range, and would have stored the wrong
weekday).  Sweeping the byte across 0..15 on a known date separates them:
exactly one acceptance means a cross-check, a contiguous run means a
bounds check.

UNLIKE shade_report.py, THIS SCRIPT WRITES.  It sends exactly one opcode,
0xFF77, and nothing else -- no move, no scene, no rekey, no power-type
change, no factory reset.  Setting a shade's clock is not destructive,
and a shade worth probing has an invalid clock already; a successful
probe finishes by writing the correct time.

Usage:
    python -m scripts.probe_set_time --ble-name DUE:7C82
    python -m scripts.probe_set_time --ble-name DUE:7C82 --dow-sweep
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

# Reply status byte; see PV_ERROR_CODES in shade_report.py.  0x80 appears
# only at the correct length with a refused field value, so the firmware
# checks length first and content second.
STATUS_LABELS: dict[int, str] = {
    0x00: "OK",
    0x04: "invalid length",
    0x80: "invalid value",
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


def _dow_candidates(now: datetime) -> list[tuple[str, bytes]]:
    """Return one 8-byte payload per candidate day-of-week code.

    Everything but the trailing byte is held at the real current time, so
    the only variable is the weekday code.  Read the result as:

      exactly one acceptance -> the shade cross-checks the code against
        the date, and the accepted value *is* this date's code
      a contiguous run       -> the shade only bounds-checks, so an
        in-range code can still store the wrong weekday
    """
    core = int.to_bytes(now.year, 2, "little") + bytes(
        [now.month, now.day, now.hour, now.minute, now.second]
    )
    return [
        (f"dow {code:>2} (0x{code:02X})", core + bytes([code])) for code in range(16)
    ]


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


def _interpret_dow(now: datetime, accepted: list[tuple[int, str, bytes]]) -> None:
    """Explain what a day-of-week sweep result implies about the field."""
    codes = [payload[-1] for _, _, payload in accepted]
    weekday = now.strftime("%A")
    if len(codes) == 1:
        code = codes[0]
        offset = (code - now.weekday()) % 7
        scheme = (
            "Python's weekday() exactly (Mon=0..Sun=6)"
            if offset == 0
            else f"weekday() shifted by +{offset} (Mon={offset % 7})"
        )
        print(
            f"Exactly one code accepted, so the shade cross-checks the "
            f"weekday against the date.\n"
            f"  {now:%Y-%m-%d} is a {weekday}; the shade wants 0x{code:02X} "
            f"({code}).\n"
            f"  That matches {scheme}."
        )
        return
    print(
        f"{len(codes)} codes accepted ({', '.join(str(c) for c in codes)}), so "
        f"the shade only bounds-checks this byte.\n"
        f"  It does NOT verify the weekday against the date, which means a "
        f"wrong-but-in-range\n"
        f"  value is stored silently. The correct code for {weekday} cannot be "
        f"read off this run\n"
        f"  alone -- re-run on a different weekday, or capture the vendor app."
    )


async def _probe(ble_name: str, hub: str, scan_timeout: float, dow_sweep: bool) -> int:
    home_key = _fetch_home_key(hub)
    if home_key is None:
        return 1

    print(f"Scanning up to {scan_timeout:.0f}s for {ble_name}...")
    seen = await find_shades({ble_name}, scan_timeout)
    dev, _adv = seen.get(ble_name, (None, None))
    if dev is None:
        print(f"  {ble_name} not seen on air. Aborting.")
        return 1

    build = _dow_candidates if dow_sweep else _candidates
    now = datetime.now().replace(microsecond=0)
    candidates = build(now)
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
        if dow_sweep:
            print()
            _interpret_dow(now, accepted)

        # The sweep set the clock to whatever the winning candidate carried,
        # which is now a little stale. Regenerate the same candidate from a
        # fresh timestamp -- by index, so no assumption is made about where
        # that candidate puts its fields -- and send it once more.
        idx, label, _ = accepted[0]
        final = datetime.now().replace(microsecond=0)
        payload = build(final)[idx][1]
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
    parser.add_argument(
        "--dow-sweep",
        action="store_true",
        help=(
            "Phase 2: hold the time fixed and sweep the trailing "
            "day-of-week byte across 0-15"
        ),
    )
    args = parser.parse_args()
    return asyncio.run(
        _probe(args.ble_name, args.hub, args.scan_timeout, args.dow_sweep)
    )


if __name__ == "__main__":
    raise SystemExit(main())
