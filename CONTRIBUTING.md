# Contributing

## What helps most

Every shade the maintainer owns is hardwired, so anything beyond that is reasoned
from the protocol rather than tested. **Data from other shades is the most
valuable contribution there is.** Two open questions need nothing but a
diagnostics download from the right shade:

- [#23](https://github.com/safepay/hdpv_ble/issues/23) — battery shades. The
  power source isn't detected, so every shade gets battery entities whether it
  has a battery or not.
- [#13](https://github.com/safepay/hdpv_ble/issues/13) — dual-fabric (Duolite)
  shades. The front/rear fabric mapping is unconfirmed.

## Reporting a bug

Enable debug logging, reproduce the problem, then disable the log to download it.
Add a diagnostics download from the shade's device page (**⋮ → Download
diagnostics**) and [open an issue](https://github.com/safepay/hdpv_ble/issues/new?template=bug.yml)
with both files.

Note that PRs submitted WITHOUT logs and diagnostics will not be considered.

Diagnostics carry the raw advertisement bytes and GATT replies, with the home
key, home ID and serial numbers redacted; the shade's own name is not. What they
can't capture is anything physical — whether the shade runs on a battery, which
fabric moved, which way it went. Put that in the issue text. For an unsupported
shade, include its type ID from *product info → type ID* in the PowerView app.

## Adding or changing shade support

Which entities a shade gets is chosen from its type ID alone, and nothing in CI
can check that choice: there is no test suite, and the integration is passive, so
a shade cannot be asked what it is. A wrong entry ships a control with nothing
behind it, which is worse than leaving the type unsupported.

A change to `SHADE_TYPE` or `SHADE_CAPABILITIES` therefore needs one of:

- A diagnostics download from that type, and a line saying what physically moved
  when you operated the shade — diagnostics can't capture that part.
- A source that classifies the type independently: [`aiopvapi`](https://github.com/sander76/aio-powerview-api),
  which the capability table otherwise mirrors, or openHAB's database, where
  types 39 and 103 came from.

A product name is not evidence. Type 10 had its position reported and driven
inverted for a while because it is called SkyLift.

AI-assisted pull requests are fine, but the hardware claim has to be yours. If
you haven't operated the shade yourself, say so — an unverified mapping is still
worth an issue, and it will be taken on those terms.

## Development

Python 3.13.2+, then `pip install -r requirements.txt`. Run all three before
every commit; CI runs them on pull requests:

```sh
ruff check .
mypy .
codespell -L hass
```

There is no test suite — changes are verified against real shades. If you add
tests, the pytest configuration has to come back with them.

## Pull requests

- Branch from `main`; it takes no direct commits. Names are
  `<type>/<kebab-case-summary>`, e.g. `fix/tilt-on-closed-position`.
- Commit subjects use `fix:`, `feat:` or `chore:`.
- Descriptions read as a changelog entry — short and factual. HACS shows them in
  Home Assistant's update dialog.
- Lint, hassfest and HACS validation must be green.
- Don't edit `version` in `manifest.json` or create tags; the Release workflow
  does that.

## Licensing

Contributions fall under the [Apache License 2.0](LICENSE) that covers the
project. Leave the `@author: patman15` headers intact — section 4(b) requires
modified files to carry notice of the change. One exception to the licence:
`emu/` is GPLv2, because the ESP32 sketch links wolfSSL. That's deliberate — see
[`emu/README.md`](emu/README.md).
