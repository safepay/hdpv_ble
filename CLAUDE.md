# Hunter Douglas PowerView BLE (`hdpv_ble`)

A Home Assistant custom integration, distributed via HACS, that controls Hunter
Douglas PowerView shades directly over Bluetooth LE — optionally reading
supplementary data from a PowerView G3 hub over HTTP.

This file lives at the repository root and is tracked, so edits to it ship in a
pull request like any other change. `.claude/` is excluded by a global gitignore
rule; anything placed there is local-only and will never appear in a commit.

## Provenance

This is a hard fork of [`patman15/hdpv_ble`](https://github.com/patman15/hdpv_ble),
detached from the fork network on 2026-08-01 because upstream stopped merging
pull requests. Upstream's last commit to `main` was 2026-01-01 and this repo is
a strict superset of it — there are no divergent upstream commits to merge.

The `upstream` remote is kept for **reference only**, not for merging. When
reworking something, `git diff upstream/main -- <path>` and
`git show upstream/main:<path>` show the original implementation.

Apache 2.0. Keep the `@author: patman15` headers and the LICENSE intact, and
keep the attribution note in the README — §4(b) requires modified files to carry
notice of the change.

## Git conventions

**Never add a Claude signature to commits or pull requests.** Specifically, do
not append `Co-Authored-By: Claude ...`, `🤖 Generated with [Claude Code]`, or
any equivalent attribution trailer or footer. Commit messages and PR bodies
should read as if written by the maintainer.

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

```text
custom_components/hunterdouglas_powerview_ble/
├── __init__.py       # setup/unload, hub polling, shade discovery dispatch
├── api.py            # PowerViewBLE transport: ShadeCmd, ShadeCapability, PVDeviceInfo
├── config_flow.py    # ConfigFlow: home key entry, optional hub URL, discovery
├── const.py          # DOMAIN, CONF_* keys, MFCT_ID, SIGNAL_NEW_SHADE
├── coordinator.py    # PVCoordinator: PassiveBluetoothDataUpdateCoordinator
├── cover.py          # cover entities (see the subclass ladder below)
├── binary_sensor.py  # charging indicator
├── button.py         # identify shade
├── number.py         # velocity
├── sensor.py         # battery SoC, RSSI
├── diagnostics.py    # config-entry and device diagnostics download
└── strings.json      # source strings; translations/ holds en only
```

There is **no test suite**, and as of 2026-08-01 no test config either — the
dead `[tool.pytest.ini_options]` block and `requirements_test.txt` were removed
because nothing referenced them. Nothing enforces coverage. If you add tests,
the config has to come back with them.

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
  flow is `VERSION = 2` against upstream's `1`, the entry data shape is
  different (`home_key`/`hub_url` versus `manufacturer_data`), and no
  `async_migrate_entry` exists — so Home Assistant fails upstream entries rather
  than converting them. Users must delete their entries and set up fresh. If you
  ever add a migration handler, the README's "Upgrading from the original
  integration" section has to change with it. Bumping `VERSION` again without a
  handler breaks *this* fork's own users the same way.
- `CONF_HUB_URL` is optional and supplies exactly two things over HTTP: the home
  key during the config flow, and each shade's friendly name (cached in
  `entry.data` under `CONF_FRIENDLY_NAMES`). It once supplied `powerType` too;
  that was removed in `6dd9a74` along with the BLE power-source detection, so
  **battery entities are now created for every shade unconditionally** — see
  issue #23. Anything claiming the hub reports battery status is stale.
- `cover.py` is a subclass ladder — `PowerViewCover` → `PowerViewCoverTilt` →
  `PowerViewCoverTiltOnClosed`, plus `PowerViewCoverTopDown` and
  `PowerViewCoverTiltOnly`. Which one is instantiated depends on the type ID's
  `ShadeCapability`. Adding a shade type means picking the right subclass, not
  adding branching to the base class.

## Gotchas

- **The maintainer owns hardwired shades only and cannot test battery
  behaviour.** Anything touching battery SoC, the charging binary sensor, or the
  hub's battery-powered flag is unverifiable locally — reason carefully from the
  protocol and say plainly in the PR that it is untested.
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

Versions are plain `X.Y.Z`. Upstream never tagged a release, so there is no tag
history to collide with; this fork's first release starts from the normalised
`0.24.0` in the manifest.

Note that HACS shows release notes in Home Assistant's update dialog,
cumulatively across every release between the user's installed version and the
latest, so notes need to read well standalone.

## Validation

Lint, codespell, hassfest and HACS validation run **on pull requests only**
(`.github/workflows/lint.yml`, `codespell.yml`, `hassfest.yml`, `validate.yml`)
— there is no push trigger, because nothing reaches `main` except through a PR
and the PR run already checks the merge result. Each is also
`workflow_dispatch`, so it can be run against `main` on demand before cutting a
release. All must be green before a release is cut.

Lint and hassfest carry `paths` filters and are skipped by documentation-only
changes; codespell and HACS validation have none. Do not make the *filtered*
checks required in branch protection — a required check that is skipped by a
paths filter blocks the PR indefinitely. Codespell and HACS validation always
run, so they are safe to require.

**A check that reads the whole repository must not live in a filtered
workflow.** `codespell` was moved out of `lint.yml` for that reason: it scans
every text file, but sitting behind lint's `**.py` filter meant a typo in a
`.md` or `.sh` could not fail a PR, and would surface only on a manual run
against `main` before a release.

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
