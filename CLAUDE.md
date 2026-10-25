# Hunter Douglas PowerView BLE (`hdpv_ble`)

A Home Assistant custom integration, distributed via HACS, that controls Hunter Douglas PowerView shades directly over Bluetooth LE — optionally reading supplementary data from a PowerView G3 hub over HTTP.

This file is tracked, so edits to it ship in a pull request like any other change. `.claude/` is gitignored globally — anything there is local-only and never committed.

## Provenance

A hard fork of [`patman15/hdpv_ble`](https://github.com/patman15/hdpv_ble), detached on 2026-08-01. Upstream's last commit to `main` was 2026-01-01 and this repo is a strict superset of it, so the `upstream` remote is **reference only**, never merged — `git diff upstream/main -- <path>` and `git show upstream/main:<path>` show the original.

Apache 2.0. Keep the `@author: patman15` headers, the LICENSE and the README's attribution note intact — §4(b) requires modified files to carry notice of the change.

## Git conventions

- Never commit directly to `main`. Branch first, then merge via pull request.
- Branch names: `<type>/<kebab-case-summary>`, e.g. `fix/tilt-on-closed-position`.
- Commit subjects use conventional prefixes: `fix:`, `feat:`, `chore:`.
- Write a body when the *why* isn't obvious from the subject; skip it for trivia.

**PR descriptions and commit bodies are public — keep them short and factual:** what changed, how to use it, anything a user must know. They read as a changelog entry, not a report. Don't narrate an investigation, list problems found along the way, or raise unresolved questions — those go to the maintainer or an issue. A caveat that must live in the repo belongs in a brief comment at the line.

## Layout

```text
custom_components/  # the integration itself, broken out below
emu/                # ESP32 shade emulator — GPLv2, see Gotchas
img/                # brand asset sources
scripts/            # standalone maintainer tools, not shipped to users
```

`scripts/` holds the two home-key recovery routes the README points users at — `extract_gateway3_homekey.py` for a G3 gateway and `extract_homekey_waydroid.sh` for the Android app under Waydroid. The shell script was contributed and is not maintainer-tested; its header says so.

`custom_components/hunterdouglas_powerview_ble/` holds the integration. Entity platforms are named for what they do; the rest are `__init__.py` (setup, unload, hub fetch, shade discovery dispatch), `api.py` (the `PowerViewBLE` transport, `ShadeCmd`, `ShadeCapability`) and `coordinator.py` (`PVCoordinator`). `translations/` holds `en` only.

There is **no test suite and no test config** — the dead `[tool.pytest.ini_options]` block and `requirements_test.txt` were removed on 2026-08-01 as unreferenced. Adding tests means bringing the config back too.

## How it works

- **Config flow only** — no YAML. Discovery is by BLE service UUID plus manufacturer ID `2073`, and by zeroconf for the G3 hub.
- `PVCoordinator` extends `PassiveBluetoothDataUpdateCoordinator`: shade state arrives from **advertisements**, not polling, and every entity extends `PassiveBluetoothCoordinatorEntity`. A shade out of Bluetooth range reports unknown rather than stale — preserve that.
- **The home key is entered in the config flow** and stored in `entry.data` under `CONF_HOME_KEY`. It is *not* hardcoded in `const.py` and *not* lost on update. This is the main divergence from upstream; any doc that says otherwise is stale.
- **There is no upgrade path from upstream and the README says so.** The config flow is `VERSION = 2` against upstream's `1`, the entry data shape differs, and no `async_migrate_entry` exists, so Home Assistant fails upstream entries rather than converting them. Bumping `VERSION` again without a handler breaks *this* fork's own users the same way; adding one means updating the README's "Upgrading from the original integration" section too.
- `CONF_HUB_URL` is optional and supplies three things over HTTP: the home key during the config flow, each shade's friendly name (cached in `entry.data` under `CONF_FRIENDLY_NAMES`), and each shade's `powerType` (cached under `CONF_POWER_TYPES`).
- **Power source is detected; battery entities are disabled, not withheld.** `_async_setup_shade` resolves it from the hub record, the `CONF_POWER_TYPES` cache, then a one-off `0xFFDE` GATT probe (a failed probe is left uncached, so the next restart retries). `PVCoordinator.battery_powered` is biased to yes — only `POWER_TYPE_HARDWIRED` answers no — so an unreadable shade keeps its battery entities and a hardwired one just gets them disabled by default; a wrong answer costs one toggle, not a missing sensor. Added in `548b53c`, closing issue #23.
- `cover.py` is a subclass ladder under `PowerViewCoverBase`, with a lift, a tilt, a dual-rail TDBU and a dual-fabric Duolite branch. `_add_entities` picks from the type ID's `ShadeCapability`, and one shade can yield several entities — TDBU two, Duolite three. Adding a shade type means picking the right subclass, not adding branching to the base class.

## Gotchas

- **The maintainer owns hardwired shades only and cannot test battery behaviour.** Anything touching battery SoC or the charging binary sensor is unverifiable locally — reason carefully from the protocol and say plainly in the PR that it is untested. The hardwired half of power-type detection *is* corroborated — six shades read `1` over GATT while their hub independently reported `powerType=1` — which is why only those codes are acted on.
- **`emu/` is GPLv2, not Apache 2.0.** The sketch links wolfSSL, whose license reaches the combined work, and `user_settings.h` is wolfSSL's own GPLv2 template. This is deliberate, documented in `emu/README.md`, and **must not be "fixed"** by relicensing to match the root LICENSE — patman15 and Dustin Brewer both hold copyright there. `emu/PV_BLE_cover/` is still byte-identical to upstream; `emu/README.md` and the `emu/linux/` BlueZ port are fork-only additions. Either way, nothing under `emu/` changes without hardware to test on.
- The version lives in **one** place: `version` in `manifest.json`. (The sibling `ha_google_weather` repo keeps a second copy in `const.py`; this one does not — don't port that half of the release workflow back in.)
- "BMS" wording inherited from patman15's unrelated `BMS_BLE-HA` project was purged from `coordinator.py` and `sensor.py` on 2026-08-01. Watch for it creeping back in from upstream diffs.
- Attribute names under `# attributes (do not change)` in `const.py` are part of the user-visible entity API — renaming them breaks templates and automations.

## Versioning and releases

Don't edit the version by hand and don't create tags manually — run the **Release** workflow (`.github/workflows/release.yml`), which bumps `manifest.json`, tags the commit and creates the GitHub release that HACS reads. See `.github/RELEASING.md`.

Versions are plain `X.Y.Z`. HACS shows release notes in Home Assistant's update dialog cumulatively, across every release between the user's installed version and the latest, so notes need to read well standalone.

## Validation

Lint, codespell, hassfest and HACS validation run **on pull requests only** (`.github/workflows/`) — nothing reaches `main` except through a PR, and the PR run already checks the merge result. Each is also `workflow_dispatch`, so it can be run against `main` before cutting a release. All must be green first.

Lint and hassfest carry `paths` filters and are skipped by documentation-only changes; codespell and HACS validation have none. Don't make the *filtered* checks required in branch protection — a required check skipped by a paths filter blocks the PR indefinitely. Codespell and HACS validation always run, so they are safe to require. For the same reason, **a check that reads the whole repo must not live in a filtered workflow**: `codespell` was moved out of `lint.yml` because behind lint's `**.py` filter a typo in a `.md` or `.sh` couldn't fail a PR.

Run locally before every commit — this is a recurring source of CI failures:

```sh
ruff check .
mypy .
codespell -L hass
```

Minimum supported Home Assistant is `2024.11.0`, declared in `hacs.json`; `requirements.txt` pins the version CI develops against.

`manifest.json` is validated only by hassfest, which runs in CI, not locally — a bad manifest change isn't caught before you push. The `categories` values are an enum hassfest checks against that **pinned** version, so a value current core accepts (`blinds_curtains`, say) still fails when it postdates the pin. `categories` is optional for custom integrations and only clears a HACS warning, so don't add it — or any manifest field — on spec; confirm it validates first.

The brand assets ship **inside the integration**, at `custom_components/hunterdouglas_powerview_ble/brand/` (`icon.png`, `icon@2x.png`, `logo.png`, `logo@2x.png`, copied from the sources in `img/`). HACS's brands check looks there before `home-assistant/brands`, so validation passes **without** an `ignore: brands`. The domain is **not** in [home-assistant/brands](https://github.com/home-assistant/brands), and Home Assistant only serves a local `brand/` folder from `2026.3.0` on, so users below that get no icon until the domain is registered there — that registration is still worth doing. If the brands check fails, restore the `brand/` folder, or register the domain, rather than adding an `ignore: brands`.
