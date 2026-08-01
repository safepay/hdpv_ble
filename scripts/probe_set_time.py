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

Phase 2 (--dow-sweep) holds the date fixed and sweeps that trailing byte
across 0..15.  Result on the same shade: 1..7 accepted, 0 and 8..15
refused with 0x80.  So the firmware bounds-checks the byte and never
validates it against the date -- a wrong-but-in-range value is stored
silently.  It also rules out Python's weekday(), which returns 0 on
Mondays and would be refused outright; the field is 1-based.

Which code means which day therefore cannot be answered by sweeping,
because every day of the week accepts the whole range.  Phase 3
(--read-time) tries 0xFF67, "get shade time" -- an opcode the emulator
carries only as a commented-out case, so whether anything answers it is
the first question.  If the shade returns a weekday it derived from the
date rather than the one last written, that settles the mapping;
otherwise it takes a btsnoop capture of the vendor app.

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

# Present in the emulator only as a commented-out case, so the number is
# known but nothing has ever answered it. --read-time finds out.
CMD_GET_TIME = 0xFF67

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
    lo, hi = min(codes), max(codes)
    print(
        f"{len(codes)} codes accepted ({', '.join(str(c) for c in codes)}), so "
        f"the shade only bounds-checks\n"
        f"  this byte against {lo}..{hi}. It never verifies the weekday "
        f"against the date, so a\n"
        f"  wrong-but-in-range value is stored silently.\n\n"
        f"  Send {lo}..{hi} only -- anything outside is refused, whatever the "
        f"real date is.\n"
        f"  Which code means {weekday} is NOT determinable by re-running: a "
        f"bounds check\n"
        f"  accepts the whole range on every day of the week. Settle it with "
        f"--read-time\n"
        f"  (does the shade recompute the weekday?) or a btsnoop capture of "
        f"the vendor app."
    )


def _decode_time(body: bytes) -> None:
    """Print an 8-byte set-time body, and what its date implies."""
    year = int.from_bytes(body[0:2], "little")
    month, day, hour, minute, second, dow = body[2:8]
    print(
        f"  decoded: {year:04d}-{month:02d}-{day:02d} "
        f"{hour:02d}:{minute:02d}:{second:02d}, weekday byte {dow}"
    )
    try:
        real = datetime(year, month, day)
    except ValueError as ex:
        print(f"  that date is not valid ({ex}), so the fields are misread.")
        return
    print(
        f"  {real:%Y-%m-%d} is a {real:%A} — isoweekday {real.isoweekday()}, "
        f"weekday {real.weekday()}."
    )
    print(
        f"  If {dow} differs from the byte last written, the shade derives "
        f"the weekday itself\n"
        f"  and {dow} is this firmware's code for {real:%A}. If it matches, "
        f"the shade only stores\n"
        f"  what it is given and the mapping needs a vendor-app capture."
    )


async def _read_time(api: PowerViewClient) -> int:
    """Try 0xFF67, "get shade time", and decode whatever comes back.

    The emulator has this opcode commented out and never implemented, so
    whether real firmware answers at all is itself the first question.
    """
    try:
        reply = await api.query(CMD_GET_TIME, b"")
    except (TimeoutError, ValueError) as ex:
        print(f"0xFF67 did not answer: {ex}")
        return 1

    print(f"0xFF67 replied with {len(reply)} byte(s): {reply.hex(' ')}")
    if len(reply) == 1:
        code, note = _describe(reply)
        shown = f"0x{code:02X} {note}" if code is not None else note
        print(f"  status only ({shown}) — this firmware has no get-time.")
        return 1
    # Read replies in this protocol lead with a status byte, as 0xFFDE does.
    if reply[0]:
        print(f"  leading status 0x{reply[0]:02X} — rejected.")
        return 1
    body = reply[1:]
    if len(body) < 8:
        print(f"  {len(body)} payload bytes, too few for the set-time layout.")
        return 0
    _decode_time(body)
    return 0


async def _probe(ble_name: str, hub: str, scan_timeout: float, mode: str) -> int:
    home_key = _fetch_home_key(hub)
    if home_key is None:
        return 1

    print(f"Scanning up to {scan_timeout:.0f}s for {ble_name}...")
    seen = await find_shades({ble_name}, scan_timeout)
    dev, _adv = seen.get(ble_name, (None, None))
    if dev is None:
        print(f"  {ble_name} not seen on air. Aborting.")
        return 1

    if mode == "read":
        async with PowerViewClient(dev, home_key) as api:
            return await _read_time(api)

    dow_sweep = mode == "dow"
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
        final = datetime.now().replace(microsecond=0)
        if dow_sweep and any(i == final.isoweekday() for i, _, _ in accepted):
            # Index equals the weekday code here, and accepted[0] would be
            # the lowest accepted code -- right only if the firmware counts
            # Monday=1. isoweekday() is at least a defensible guess.
            idx = final.isoweekday()
            label = f"dow {idx} (isoweekday)"
        else:
            idx, label, _ = accepted[0]
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
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dow-sweep",
        action="store_true",
        help=(
            "Phase 2: hold the time fixed and sweep the trailing "
            "day-of-week byte across 0-15"
        ),
    )
    mode.add_argument(
        "--read-time",
        action="store_true",
        help="Phase 3: read the clock back with 0xFF67 and decode it",
    )
    args = parser.parse_args()
    if args.read_time:
        chosen = "read"
    elif args.dow_sweep:
        chosen = "dow"
    else:
        chosen = "sweep"
    return asyncio.run(_probe(args.ble_name, args.hub, args.scan_timeout, chosen))


if __name__ == "__main__":
    raise SystemExit(main())
