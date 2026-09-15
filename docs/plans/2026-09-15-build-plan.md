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

## M5 — Verify locally, then deploy

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

**Status.** `scripts/deploy.sh` is written and syntax-checked but has never been run, so treat its
first run as part of the milestone rather than a formality. It adds a rollback the plan didn't
originally call for: if no matching entity reappears within `RESTART_TIMEOUT`, it puts the previous
version back and restarts again, so a bad deploy can't leave the instance without a working
component. Both halves of the milestone are still outstanding — the container run and the real
deploy — and `LOCAL.md` lists the two `.env` values still to fill in.

## M6 — Second pass

`energy_export` and `power`. Then, as separate decisions: per-phase current and voltage sensors, an
options flow for the poll interval, the monotonicity guard, `diagnostics.py`, `icons.json`, CI
(hassfest + HACS validation), and going public as a HACS custom repository.

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
