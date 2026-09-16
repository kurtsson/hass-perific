# Build plan — 2026-09-15

Milestones, each ending somewhere we can stop. Background is in `CONTEXT.md`, design rationale in
`docs/specs/perific-integration.md`, API detail in `docs/api/enegic.md`.

## M0 — Project conventions ✅

`AGENTS.md` (how code is written, where things live, the rules that fail silently) and `CONTEXT.md`
(hardware, problem, settled questions).

## M1 — Project structure ✅

Directory skeleton, `README.md`, `LICENSE` (MIT), `.gitignore`, `hacs.json`, and:

- **`pyproject.toml`** — ruff 0.16.7 for lint and format, with a rule selection overlapping HA
  core's so patterns copied from upstream integrations pass unchanged; mypy 2.3.1 scoped to
  `api/` only; pytest configured with `asyncio_mode = "auto"`.
  `pytest-homeassistant-custom-component==0.13.365` pins `homeassistant==2026.9.2` exactly, which
  matches the deployment target. HA 2026.9.2 requires Python ≥ 3.14.2, hence
  `target-version = "py314"`.
- **`docker-compose.yml` + `dev/config/`** — local HA on `localhost:8123`, image pinned to
  2026.9.2, `custom_components/perific/` bind-mounted read-only so a running HA can't modify the
  source. This is the primary test target.
- **Docs moved into the repo** — this plan, the design spec, and the API reference.
- **`.reference/`** — shallow clones of `Pokeyo-AB/homeassistant-perific`,
  `toshi38/homeassistant-perific` and `custom-components/zaptec` for reading. Gitignored.

The pinned versions in `pyproject.toml` and `docker-compose.yml` move together. If one changes and
the others don't, tests stop being representative of the deployment.

## M2 — Probe ✅

`scripts/probe_api.py` — standard library only, so it runs under bare `python3` with no virtualenv
and no install. Loads credentials from `.env` itself; it is run by hand, and the credentials never
enter an agent session.

It must answer, writing to `scripts/probe_out/` (gitignored) — `raw/` verbatim, `redacted/` with the
token, MAC address, item IDs and device names replaced, and `summary.txt` with the findings:

1. `/createtoken` — the real validity window and exact timestamp format.
2. `/getaccountoverview` — how many items, and their `ItemCategory` / `ItemType` / `ItemSubType`.
   Determines the category filter and whether non-meter items exist.
3. `/getlatestpackets` — every `data` key in every bucket, and the reported `pv`.
4. **The same call again ~60 s later.** Comparing `hwi` / `hwo` between the two confirms they're
   cumulative registers rather than interval deltas. The documented sample data says cumulative;
   this verifies it for a real v3 HAN device.

Also worth capturing: the actual packet cadence from `ts` / `seqno` deltas, so the poll interval can
be sanity-checked against how often the device even reports.

Ends with `docs/device-notes.md`. Redacted output becomes `tests/fixtures/`, keeping the two
60-seconds-apart captures as separate fixtures — they're what any monotonicity handling gets tested
against.

**Outcome.** `hwi` / `hwo` are cumulative kWh registers, so `TOTAL_INCREASING` stands and M4 is
unblocked. Three corrections fell out of it, all recorded in `docs/device-notes.md`: the energy
registers live in `PhaseMinute` and not `PhaseRealTime`, `hwpi` / `hwpo` are neither power nor
available in the live buckets so the M6 power sensor has to be derived, and the documented `hiavg`
sign convention is inverted. `--redact-only` re-runs redaction over saved captures, so fixing a
redaction gap costs no API call.

## M3 — Vendored client + tests ✅

`api/client.py`, `api/models.py`, `api/exceptions.py`. Async, injected `aiohttp` session, no
`homeassistant` imports, frozen dataclasses with tolerant parsing. Exceptions:
`PerificAuthError`, `PerificConnectionError`, `PerificRateLimitError`, over a `PerificError` base.

`pytest` against the M2 fixtures, including missing and unknown fields — the tolerant-parsing
behaviour is a design choice and should be pinned by tests. No HA runtime needed for this milestone.

**Outcome.** 76 tests, clean under `ruff` and under `mypy --strict`. The client tests run against a
real aiohttp server on 127.0.0.1 rather than a mocked session, which is what lets them assert the
two wire details that would otherwise only fail in production: the bare `X-Authorization` header,
and `Content-Length: 0` on the bodyless PUT. Binding that listener needs the `socket_enabled`
fixture, because `pytest-homeassistant-custom-component` blocks socket creation outright.

The fixture pair taken 60 s apart is now load-bearing: `TestCaptureComparison` asserts the register
rise, that both directions can rise in the same minute, the `PhaseMinute` seqno advancing by one,
and the day bucket being re-served byte-identical. If those fail against regenerated fixtures, the
sensor typing needs revisiting before the code does.

## M4 — Minimal integration, one sensor ✅

`__init__.py`, `manifest.json`, `const.py`, `config_flow.py` (user + reauth), `coordinator.py`,
`sensor.py`, `strings.json`, `translations/en.json`.

Just `energy_import`. Coordinator at 5 minutes with `always_update=False`.

Typing, error semantics and identity are specified in `docs/specs/perific-integration.md` — follow
it rather than re-deciding.

**Outcome.** 110 tests, clean under `ruff` and `mypy --strict`. Every row of the spec's failure
table is now a test, including the one that matters most — a 401 during a poll raising
`ConfigEntryAuthFailed` unwrapped, so reauth actually fires rather than the integration going quiet.
`UpdateFailed` turned out to accept a `retry_after`, which the coordinator honours for the next
interval, so a `429` degrades into a longer wait instead of a tight retry loop.

Two things the base classes forced. `DataUpdateCoordinator[dict[int, ItemPackets]]` and
`CoordinatorEntity[PerificCoordinator]` are evaluated when the class statement runs, so those two
names must be imported at runtime — `from __future__ import annotations` does not reach a base
class. And `enable_custom_integrations` depends on the `hass` fixture, so it is autouse per HA test
module rather than globally; making it session-wide would drag a Home Assistant instance into the
pure client tests.

The entity lands as `sensor.perific_device_4_energy_imported`, from `has_entity_name` plus the
device name. The tests look it up by unique ID instead of hardcoding that.

## M5 — Verify locally, then deploy ✅ (bar the 48-hour check)

First in the local container: complete the config flow against the real account, watch several poll
cycles at debug level. Everything except statistics accumulating over days can be proven here.

Then `scripts/deploy.sh` to the real instance — no bare rsync:

1. `tar` the component.
2. `scp` it across.
3. Extract beside the live directory, then swap in with `mv` — a failed transfer must never leave a
   half-written component for HA to import.
4. Restart via the REST API (`POST /api/services/homeassistant/restart`, long-lived token from
   `.env`). No SSH needed for the restart itself, and it works the same against a containerised
   instance — no `docker compose restart` required. The address and port to use are in `LOCAL.md`;
   don't assume 8123.
5. Poll `/api/states` until the expected entity appears; exit non-zero with the relevant log lines
   if it doesn't.

Manual verification: units and `state_class` in Developer Tools → States; a statistics graph on the
entity (absence means the typing was rejected and the recorder logged why); no issues in Developer
Tools → Statistics; selectable in the Energy dashboard; still accumulating 48 hours later.

**Container half, done.** Home Assistant 2026.9.2 loads the integration, and the config flow was
driven through the REST API: the form renders, and deliberately wrong credentials come back from the
real Enegic API as `invalid_auth` on the re-rendered form. Two things only a real runtime could have
shown. The password field was a plain `str`, which renders as a visible text box — it now uses a
`TextSelector` in password mode, verified as `"type": "password"` in the served schema. And the
local container's bind mount was unusable: mounting the component at
`/config/custom_components/perific` nests a mount inside the `/config` mount, which on Docker
Desktop for Mac empties itself while the container runs — `RestartCount` 0, host files intact,
directory empty inside. The component is now copied in by `scripts/dev-sync.sh` and the nested
mount is gone; verified stable across three consecutive restarts and an idle period.

Completing the flow with real credentials, and watching poll cycles at debug level, still needs
doing by hand — credentials never enter an agent session.

**Statistics, proven in the container.** The recorder registered
`sensor.onerj12_inkopt_elektricitet` in `statistics_meta` with `has_sum = 1`, `has_mean` unset and
unit `kWh`, and `statistics_short_term` shows `sum` accumulating off the counter — 0 → 0.036 → 0.099
→ 0.161 kWh across four five-minute rows. So the typing is accepted rather than silently declined,
which is the failure this milestone exists to rule out. Querying `dev/config/home-assistant_v2.db`
directly is the cheapest way to check it; the entity's own log lines say nothing either way.

The hourly `statistics` table — the one purging never touches, and therefore the one the whole
year-over-year goal rests on — compiled its first row at the following hour boundary: 0.324 kWh
imported. Export compiled 0.0 kWh, correctly, because the capture hour was after dark; the export
sensor's statistics path has not yet been exercised with a non-zero delta.

`energy_export` was added before the deploy rather than after it, so that the real instance is
touched once instead of twice. Both sensors read correctly against the live account:
248726.347 kWh imported, 18058.474 kWh exported, both `energy` / `total_increasing` / kWh.

**The rest of M6's sensors came forward for the same reason.** `power_import`, `power_export`,
`current_l1..3`, `voltage_l1..3` and the options flow are all in, so the first deploy carries the
whole set. The push for this was live power, which the vendor app shows and the integration did not.

A capture taken beside the app settled what the plan had left open. `hwpi` / `hwpo` are per-phase
kWh for the bucket period, not power — `sum(hwpi)` 16.343 against the app's 16.3 — which retires the
`--wait 3700` probe M2 proposed. And `Σ hiavg × huavg` is precisely what the app displays, 873 W
computed against 0.87 kW shown, which reverses the spec's rejection of it. `docs/device-notes.md`
carries both, and `docs/specs/perific-integration.md` records why the decision changed.

**Deploy half.** The first run broke the instance, which is exactly why the plan called for treating
it as part of the milestone rather than a formality. Two bugs, both in the script:

- **The backup was staged inside `custom_components/`.** Home Assistant's `_get_custom_components`
  iterates every directory there with no filtering, so `.perific-old` was imported as
  `custom_components..perific-old`, whose parent package is the empty-named `custom_components.` —
  `ModuleNotFoundError: No module named 'custom_components.'`. Both directories also carry the same
  `"domain": "perific"`, so they collide in the `{integration.domain: integration}` dict and the
  broken one can win. The new version now unpacks into a holding directory whose manifest sits one
  level deeper — `resolve_from_root` skips a directory with no `manifest.json` before importing
  anything — and moves into place with a rename inside `custom_components/`, which keeps the swap
  atomic. The script refuses to finish if it finds a dotted directory left behind.
- **The backup could not be written where the second attempt put it.** `$HA_CONFIG_DIR` is not
  writable by the SSH user on this instance, though `custom_components/` inside it is. The previous
  version goes to `$HOME/.perific-deploy` instead, overridable with `HA_DEPLOY_DIR`.
- **Verification matched an entity ID that can never exist.** The default was `energy_import`, but
  entity IDs are built from *translated* names, so on this Swedish instance the sensor is
  `sensor.onerj12_inkopt_elektricitet`. The check would always have timed out and rolled back a
  good deploy. Success is now the config entry reaching `loaded`, read from
  `/api/config/config_entries/entry?domain=perific`, which no language affects.

- **Cleanup could fail a deploy that had already succeeded.** `rmdir` on the holding directory ran
  after the rename, found it non-empty and aborted under `set -e`, leaving the new component in
  place with Home Assistant never restarted — the worst moment to stop. It is now `rm -rf || true`,
  and packaging passes `--no-xattrs` so macOS provenance attributes stop reaching GNU tar's pax
  parser on the far end.

The deploy succeeds now, and the config entry reports `loaded`. None of these four would have
surfaced without running it against a real instance: the container shares neither the permission
model, the language, nor the tar implementation. The rollback path has still never been exercised —
every failure came before the verification loop.

Worth keeping in mind for anything that touches a deployed component: Home Assistant runs as root
and writes `__pycache__` into it, and unlinking a file needs write permission on its *parent*
directory. So the SSH user cannot delete those, and the script moves directories aside rather than
removing them.

## M6 — Second pass

The monotonicity guard, `diagnostics.py`, CI (hassfest + HACS validation), and going public as a
HACS custom repository.

`energy_export`, both power sensors, the per-phase sensors and the options flow all moved into M5 —
see that milestone.

### The integration logo needs a brands PR

`icons.json` covers **entity** icons and ships inside the integration. The logo beside the
integration's own name is a different mechanism: the frontend fetches it from
`brands.home-assistant.io/_/perific/icon.png`, and when that 404s the UI renders an "icon not
available" placeholder. Nothing a custom component ships can override it — confirmed by grepping the
frontend bundle, which builds those URLs directly.

Fixing it means a pull request to [`home-assistant/brands`](https://github.com/home-assistant/brands)
adding `custom_integrations/perific/`:

| File | Requirement |
|---|---|
| `icon.png` | 256×256, square |
| `icon@2x.png` | 512×512, square |
| `logo.png` | shortest side 128–512 px, landscape preferred |
| `logo@2x.png` | shortest side 256–512 px |

PNG only, lossless, transparency encouraged, trimmed to minimum empty space. Optional `dark_*`
variants. **Custom integrations must not use Home Assistant branded imagery** — the brands repo
rejects anything that could imply this is an official integration.

Sequencing: the brands repository is public, so this lands after the repository goes public rather
than before. Artwork is a design decision, not a code one.

## Risks

| Risk | Mitigation |
|---|---|
| `hwi` / `hwo` turn out to be interval values | M2 confirms before any sensor is written |
| Rate limits unknown | 5-minute polling; `429` handled distinctly |
| Undocumented API changes without notice | Vendored client confines it to one module; fixtures surface breakage in tests |
| Stale packets inject false spikes into statistics | Guard designed, deferred to M6, promoted if seen during M5 |
| Deploy leaves a broken component and HA won't start | Atomic swap plus a post-restart entity check that fails loudly |

## Done when

`scripts/deploy.sh` puts the integration on the real instance, it configures from the UI with
username and password, `energy_import` appears with the correct unit and a statistics graph, it's
selectable in the Energy dashboard, and hourly statistics are still accumulating 48 hours later.

All of that holds except the last. The deploy runs clean and the config entry reports `loaded`; the
Energy dashboard shows import and export under *Configure grid connection*, and the power pair is
accepted by its two-sensor mode, which builds its own net-power helper from them. What remains is
only the wait: hourly statistics still accumulating two days on. Configuring it is now documented in
`README.md`, since the dialog's two-sensor mode is the part that is not obvious.
