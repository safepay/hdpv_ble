# Hunter Douglas PowerView BLE (`hdpv_ble`)

A Home Assistant custom integration, distributed via HACS, that controls Hunter
Douglas PowerView shades directly over Bluetooth LE — optionally reading
supplementary data from a PowerView G3 hub over HTTP.

This file is tracked, so edits to it ship in a pull request like any other
change. `.claude/` is excluded by a global gitignore rule — anything placed
there is local-only and never reaches a commit.

## Provenance

A hard fork of [`patman15/hdpv_ble`](https://github.com/patman15/hdpv_ble),
detached on 2026-08-01. Upstream's last commit to `main` was 2026-01-01 and
this repo is a strict superset of it, so the `upstream` remote is **reference
only** and is never merged — `git diff upstream/main -- <path>` and
`git show upstream/main:<path>` show the original.

Apache 2.0. Keep the `@author: patman15` headers, the LICENSE and the README's
attribution note intact — §4(b) requires modified files to carry notice of the
change.

## Git conventions

**Never add a Claude signature to commits or pull requests.** Specifically, do
not append `Co-Authored-By: Claude ...`, `🤖 Generated with [Claude Code]`, or
any equivalent attribution trailer or footer. Commit messages and PR bodies are
the maintainer's own.

- Never commit directly to `main`. Branch first, then merge via pull request.
- Branch names: `<type>/<kebab-case-summary>`, e.g. `fix/tilt-on-closed-position`.
- Commit subjects use conventional prefixes: `fix:`, `feat:`, `chore:`.
- Write a body when the *why* isn't obvious from the subject; skip it for trivia.

**PR descriptions and commit bodies are public. Keep them short and factual** —
what changed, how to use it, anything a user must know. They read as a changelog
entry, not as a report. Do not narrate an investigation, list problems found
along the way, or raise unresolved issues awaiting a decision. Those go to the
maintainer directly or into an issue. If a caveat has to live in the repo, a
brief comment at the relevant line is the right size.

## Layout

```text
custom_components/  # the integration itself, broken out below
emu/                # ESP32 shade emulator — GPLv2, see Gotchas
img/                # brand asset sources
scripts/            # standalone maintainer tools, not shipped to users
```

`scripts/` holds the two home-key recovery routes the README points users at —
`extract_gateway3_homekey.py` for a G3 gateway and `extract_homekey_waydroid.sh`
for the Android app under Waydroid. The shell script was contributed and is not
maintainer-tested; its header says so.

`custom_components/hunterdouglas_powerview_ble/` holds the integration. Entity
platforms are named for what they do; the rest are `__init__.py` (setup,
unload, hub fetch, shade discovery dispatch), `api.py` (the `PowerViewBLE`
transport, `ShadeCmd`, `ShadeCapability`) and `coordinator.py`
(`PVCoordinator`). `translations/` holds `en` only.

There is **no test suite**, and no test config either — the dead
`[tool.pytest.ini_options]` block and `requirements_test.txt` were removed on
2026-08-01 because nothing referenced them. If you add tests, the config has to
come back with them.

## How it works

- **Config flow only** — no YAML. Discovery is by BLE service UUID plus
  manufacturer ID `2073`, and by zeroconf for the G3 hub.
- `PVCoordinator` extends `PassiveBluetoothDataUpdateCoordinator`: shade state
  arrives from **advertisements**, not polling, and every entity extends
  `PassiveBluetoothCoordinatorEntity`. A shade out of Bluetooth range reports
  unknown rather than stale — preserve that.
- **The home key is entered in the config flow** and stored in `entry.data`
  under `CONF_HOME_KEY`. It is *not* hardcoded in `const.py` and *not* lost on
  update. This is the main divergence from upstream; any doc that says
  otherwise is stale.
- **There is no upgrade path from upstream and the README says so.** The config
  flow is `VERSION = 2` against upstream's `1`, the entry data shape differs,
  and no `async_migrate_entry` exists, so Home Assistant fails upstream entries
  rather than converting them. Bumping `VERSION` again without a handler breaks
  *this* fork's own users the same way; adding one means updating the README's
  "Upgrading from the original integration" section too.
- `CONF_HUB_URL` is optional and supplies three things over HTTP: the home key
  during the config flow, each shade's friendly name (cached in `entry.data`
  under `CONF_FRIENDLY_NAMES`), and each shade's `powerType` (cached under
  `CONF_POWER_TYPES`).
- **The power source is detected, and battery entities are disabled rather
  than withheld.** `_async_setup_shade` resolves it from the hub's record, the
  `CONF_POWER_TYPES` cache, then a one-off `0xFFDE` GATT probe (a failed probe
  is left uncached so the next restart retries).
  `PVCoordinator.battery_powered` is biased towards yes: only
  `POWER_TYPE_HARDWIRED` answers no, so an unreadable shade keeps its battery
  entities and a hardwired one merely gets them disabled by default — a wrong
  answer costs one toggle, not a missing sensor. Added in `548b53c`, closing
  issue #23.
- `cover.py` is a subclass ladder under `PowerViewCoverBase`, with a lift, a
  tilt, a dual-rail TDBU and a dual-fabric Duolite branch. `_add_entities`
  picks from the type ID's `ShadeCapability`, and one shade can yield several
  entities — TDBU two, Duolite three. Adding a shade type means picking the
  right subclass, not adding branching to the base class.

## Gotchas

- **The maintainer owns hardwired shades only and cannot test battery
  behaviour.** Anything touching battery SoC or the charging binary sensor is
  unverifiable locally — reason carefully from the protocol and say plainly in
  the PR that it is untested. The hardwired half of power-type detection *is*
  corroborated — six shades read `1` over GATT while their hub independently
  reported `powerType=1` — which is why only those codes are acted on.
- **`emu/` is GPLv2, not Apache 2.0.** The sketch links wolfSSL, whose license
  reaches the combined work, and `user_settings.h` is wolfSSL's own GPLv2
  template. This is deliberate, documented in `emu/README.md`, and **must not be
  "fixed"** by relicensing to match the root LICENSE — patman15 and Dustin
  Brewer both hold copyright there. `emu/` is also byte-identical to upstream,
  so leave it that way unless there is hardware to test a change on.
- The version lives in **one** place: `version` in `manifest.json`. (The
  sibling `ha_google_weather` repo keeps a second copy in `const.py`; this one
  does not — don't port that half of the release workflow back in.)
- "BMS" wording inherited from patman15's unrelated `BMS_BLE-HA` project was
  purged from `coordinator.py` and `sensor.py` on 2026-08-01. Watch for more of
  it creeping back in from upstream diffs.
- Attribute names under `# attributes (do not change)` in `const.py` are part
  of the user-visible entity API — renaming them breaks templates and
  automations.

## Versioning and releases

Don't edit the version by hand and don't create tags manually — run the
**Release** workflow (`.github/workflows/release.yml`), which bumps
`manifest.json`, tags the commit and creates the GitHub release that HACS reads.
See `.github/RELEASING.md`.

Versions are plain `X.Y.Z`. HACS shows release notes in Home Assistant's update
dialog cumulatively, across every release between the user's installed version
and the latest, so notes need to read well standalone.

## Validation

Lint, codespell, hassfest and HACS validation run **on pull requests only**
(`.github/workflows/`) — nothing reaches `main` except through a PR, and the PR
run already checks the merge result. Each is also `workflow_dispatch`, so it
can be run against `main` before cutting a release. All must be green first.

Lint and hassfest carry `paths` filters and are skipped by documentation-only
changes; codespell and HACS validation have none. Do not make the *filtered*
checks required in branch protection — a required check that is skipped by a
paths filter blocks the PR indefinitely. Codespell and HACS validation always
run, so they are safe to require.

**A check that reads the whole repository must not live in a filtered
workflow.** `codespell` was moved out of `lint.yml` for exactly that reason: it
scans every text file, but behind lint's `**.py` filter a typo in a `.md` or
`.sh` could not fail a PR.

Run locally before every commit — this is a recurring source of CI failures:

```sh
ruff check .
mypy .
codespell -L hass
```

Minimum supported Home Assistant is `2024.8.0`, declared in `hacs.json`;
`requirements.txt` pins the version CI develops against.

The integration's brand assets are already registered in
[home-assistant/brands](https://github.com/home-assistant/brands) under
`custom_integrations/hunterdouglas_powerview_ble`, so HACS validation runs
**without** an `ignore: brands` — unlike the sibling repo. If the brands check
starts failing, fix it rather than ignoring it. The source PNGs live in `img/`.
