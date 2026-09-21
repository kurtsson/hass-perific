# AGENTS.md

Home Assistant custom integration for the Perific / Enegic energy monitor. Pulls grid import/export
into HA so it lands in long-term statistics.

## Where things are

| Path | What |
|---|---|
| `custom_components/perific/` | The integration. Exactly one directory under `custom_components/` — HACS requires this. |
| `custom_components/perific/api/` | Vendored Enegic API client. Standalone by design; see conventions. |
| `CONTEXT.md` | Domain background: the hardware, what the HAN port can see, which questions are settled. |
| `LOCAL.md` | The specific installation this is developed against. **Gitignored**; read it if present. |
| `docs/specs/` | Design specs — the reasoning behind the architecture. |
| `docs/plans/` | Implementation plans, dated. |
| `docs/api/enegic.md` | API reference. The endpoint and field documentation lives here, not in code comments. |
| `docs/device-notes.md` | Confirmed field meanings and units, from the probe. |
| `scripts/probe_api.py` | API probe. Standard library only, so it runs under bare `python3`. Loads `.env` itself. `--phasedata` probes the historical endpoint instead. |
| `scripts/measure_gaps.py` | Reports the hours missing from the real instance's long-term statistics. Read-only; needs the venv for `aiohttp`, so run it with `uv run`. |
| `scripts/dev-sync.sh` | Copies the component into the local container and restarts it. Run after every edit. |
| `scripts/deploy.sh` | Ships the component to the real instance and verifies it came back. Final verification only. |
| `tests/fixtures/` | Redacted real API responses. |
| `docker-compose.yml` | Local HA for testing. Image pinned to the real instance's version. |
| `dev/config/` | Throwaway HA configuration for the local container. Not the real instance's config. |
| `.reference/` | Shallow clones of six upstream projects, for reading. Gitignored — never edit, never import. |

Start with `CONTEXT.md` for the domain, then `docs/specs/` for why the code is shaped the way it is.

## How code is written here

**Async throughout.** `aiohttp` with an injected session (`async_get_clientsession(hass)`), never a
client-created session.

**Plain frozen dataclasses, not pydantic.** Parse with `.get()` and defaults, tolerating unknown and
missing fields. The API has multiple packet-format versions and fields come and go; a strict
validator fails the whole poll where we want one sensor to go unavailable. It also avoids a pydantic
pin that collides with other custom integrations in the same HA environment.

**`custom_components/perific/api/` imports nothing from `homeassistant.*`** and raises only its
own exception types. That's what keeps it liftable into a standalone library later — contained,
mechanical work rather than untangling. Don't reach into HA from it, and don't let HA types leak
into its signatures.

**`entry.runtime_data` with a typed `ConfigEntry` alias.** Not `hass.data[DOMAIN]` — that's the
pre-2024.6 pattern.

**Comments are for constraints, not narration.** The API is documented in `docs/api/enegic.md`;
don't restate it above each call. Comment the things a reader would otherwise undo.

## Reference checkouts

Six upstream projects are cloned into `.reference/` so they can be read without network access.
They are gitignored. Read them; never edit them, never import from them, never copy a file wholesale
without checking it against `docs/specs/`.

| Path | What it's for |
|---|---|
| `.reference/zaptec` | **The pattern reference.** [`custom-components/zaptec`](https://github.com/custom-components/zaptec) — a cloud-polling HACS integration already using `entry.runtime_data`, a typed config entry, a full user/reauth/reconfigure flow, and an `EntityDescription` subclass carrying the entity class for data-driven construction. Copy its shapes. |
| `.reference/homeassistant-perific-toshi38` | **The API reference source.** [`toshi38/homeassistant-perific`](https://github.com/toshi38/homeassistant-perific) — `PERIFIC_API_DOCUMENTATION.md` is where the endpoint list and `data` field meanings come from, and `mock_data.py` has sample payloads. The author states the repo was AI-generated and unverified, so treat its claims as hypotheses, not facts. Its own integration code follows outdated patterns. |
| `.reference/homeassistant-perific-pokeyo-ab` | **The corroborating source.** [`Pokeyo-AB/homeassistant-perific`](https://github.com/Pokeyo-AB/homeassistant-perific) — an independently written client. Where it agrees with `toshi38` on a field or endpoint, that claim is trustworthy; `perific/client.py` is the file worth reading. |
| `.reference/perific-meter-petrolhead` | **A different device variant.** [`PetrolHead2/perific-meter`](https://github.com/PetrolHead2/perific-meter) — a Pokeyo fork, the most recently maintained of the four. Its packets carry `iavg` / `imin` / `imax` / `qmax` where ours carry `hiavg` / `huavg`, which is the best evidence that field names vary by device. Also the only one using `/getreporterssettingsforuser`. `FULL.md` is a field-by-field sensor plan. |
| `.reference/homeassistant-perific-abelgladstone` | **The other fork.** [`abelgladstone/homeassistant-perific`](https://github.com/abelgladstone/homeassistant-perific) — a Pokeyo fork with unit tests. Independently reads energy from `PhaseMinute` and everything else from `PhaseRealTime`, the same split our probe forced. |
| `.reference/hass-plejd` | **The structural reference.** [`thomasloven/hass-plejd`](https://github.com/thomasloven/hass-plejd) — read it for module layout: a single domain-object module owning the connection, a `plejd_entity.py` base-entity module keeping the platform files thin, a declarative `diagnostics.py` redaction tree, and two-line hassfest/HACS workflows. Its wiring is *older* than ours in places — `hass.data[DOMAIN]` rather than `entry.runtime_data`, platforms forwarded before the connection is validated, an unpinned `pydantic` in `requirements`, and diagnostic sensors that set `_attr_unit_of_measurement` and override `state`, both of which HA core annotates as "subclasses of SensorEntity should not set this". Copy the layout, not the wiring. |

What has already been extracted from them lives in `docs/api/enegic.md`, with a confidence level on
every claim. Prefer that file over re-reading the sources, and update it if you learn something new.

To refresh them:

```
git clone --depth 1 https://github.com/custom-components/zaptec.git .reference/zaptec
git clone --depth 1 https://github.com/toshi38/homeassistant-perific.git .reference/homeassistant-perific-toshi38
git clone --depth 1 https://github.com/Pokeyo-AB/homeassistant-perific.git .reference/homeassistant-perific-pokeyo-ab
git clone --depth 1 https://github.com/PetrolHead2/perific-meter.git .reference/perific-meter-petrolhead
git clone --depth 1 https://github.com/abelgladstone/homeassistant-perific.git .reference/homeassistant-perific-abelgladstone
git clone --depth 1 https://github.com/thomasloven/hass-plejd.git .reference/hass-plejd
```

## Rules that fail silently if broken

These don't raise. They just quietly produce a broken integration.

**Sensor typing.** Long-term statistics are the point of this project, and HA declines to record
them — with only a log warning — if the typing is wrong.

- `TOTAL_INCREASING` for monotonic counters only. Any decrease is read as a meter reset.
- `TOTAL` for anything that can legitimately decrease.
- `MEASUREMENT` for instantaneous readings.
- The unit must be valid for the `device_class`: energy in kWh, power in W.

A missing statistics graph on an entity means the typing was rejected. Read the log rather than
guessing.

**No energy sensors at all, net or otherwise.** The hourly energy series is imported into long-term
statistics from `/getphasedata` — see `history.py` — so an entity accumulating the same registers
from polling would build a second, gappier copy of it under a name a user cannot tell apart in the
statistics picker. Net would be worse still: it isn't monotonic, so it could never be
`TOTAL_INCREASING` at all, and the Energy dashboard derives net from import and export itself. See
`CONTEXT.md` — both community reference integrations get this wrong.

**The `energy_import` / `energy_export` strings stay in `strings.json` and the translations even
though no entity uses them.** They are what names the imported statistics, which have no entity to
take a translated name from; `history.async_register_names` reads them by those exact keys. Deleting
them leaves the Energy dashboard showing English names beside Swedish sensors.

**Raise `ConfigEntryAuthFailed` directly**, never wrapped in `UpdateFailed` — but only once a
rejection has repeated. Wrapped forever, HA's reauth flow never triggers and the integration just
stops updating. Raised on the first 401, it is just as bad in the other direction: the coordinator
does not reschedule after `ConfigEntryAuthFailed` (`update_coordinator.py`, the `if not auth_failed`
guard around `_schedule_refresh`), so one bad answer from the API ends collection until a human
answers the prompt. Count consecutive rejections and escalate on the third.

**Poll slowly.** The API's rate limits are unverified and the community sources contradict each
other. Faster polling buys nothing — statistics are hourly buckets.

## Environment

**uv** owns the Python environment. `uv.lock` pins everything, Home Assistant included, and it is
committed — `uv lock --check` runs in CI and in pre-commit, so an unlocked change fails before it
lands. Run tools through `uv run` so they are the pinned ones rather than whatever is on `PATH`.

```
uv sync --locked --group dev    # exactly what CI installs
uv run pre-commit install       # once per clone
uv run pre-commit run --all-files
```

Two dependency groups, declared as conflicting so uv resolves both: `dev` pins the deployment
target, and `minimum-homeassistant` pins the floor promised in `hacs.json`. Changing the floor means
changing both, plus the README — `tests/test_release_metadata.py` fails if `hacs.json` and the group
disagree.

## Linting and formatting

**ruff** does both for Python, configured in `pyproject.toml`. Nothing else — no black, flake8 or
isort.

```
uv run ruff check .            # lint
uv run ruff check --fix .      # lint and autofix
uv run ruff format .           # format
```

`select = ["ALL"]` with an explicit ignore list, so new ruff rules arrive by default rather than
having to be opted into one at a time. Every entry in `ignore` and `per-file-ignores` carries the
reason it is there; add the reason with the rule or don't add the rule. When an upstream Home
Assistant idiom trips a rule, prefer a per-file ignore over rewriting the idiom — the point is that
code copied from core passes unchanged.

**prettier** covers JSON, which ruff does not read at all. A manifest, `strings.json` or a
translation that doesn't parse is invisible to every other check here — Home Assistant just drops
it and carries on.

```
npx prettier --check .  # verify
npx prettier --write .  # format
```

It needs no `package.json`; `npx` fetches it. Prettier honours `.gitignore`, so `.reference/`,
`.venv/` and `scripts/probe_out/` are already out of scope. `.prettierignore` adds the fixtures
(verbatim captures — reformatting would lose the shape the server sent) and Markdown (prose is
wrapped by hand).

`.editorconfig` carries the indent conventions. Prettier reads `indent_size` from it, so the two
have to agree: JSON stays at 2 spaces.

**basedpyright** type-checks the integration and the tests; the configuration is in
`pyproject.toml`:

```
uv run basedpyright
```

It replaced mypy, for one reason: mypy has no equivalent of `reportTypedDictNotRequiredAccess` or
`reportOptionalMemberAccess`, so it was silent on the Home Assistant result types that make up most
of this code's surface — and it only ever looked at `api/`. basedpyright is also what the editor
runs, so an editor warning and a CI failure are now the same thing.

Three scopes, because one strictness does not suit all of it:

- **`custom_components/perific/api/`** is checked hardest. It is standalone and fully typed, and a
  mistake there surfaces as a silently `None` sensor rather than an error.
- **The HA-facing modules** run at the default. `available` and `entity_description` override
  cached properties on `Entity`, which every integration does and which trips
  `reportIncompatibleVariableOverride`; those two rules are off.
- **`tests/`** additionally turns off `reportTypedDictNotRequiredAccess`. `ConfigFlowResult` and
  `StatisticData` mark most keys `NotRequired`, and in a test the `KeyError` from a missing one *is*
  the assertion.

Two traps, both of which cost real time to find:

- **`typeCheckingMode` is not valid inside an `executionEnvironment`.** It is ignored with only a
  passing note on stderr, so `api/` looked strict while being checked at the default. The rules are
  named individually instead. After changing them, confirm they still bite by adding a throwaway
  untyped function to `api/` and watching it fail.
- **An `executionEnvironment` resolves imports from its own root**, so `tests/` has to name the repo
  root in `extraPaths` — the same path `pythonpath` gives pytest.

The pre-commit hooks run all of them, plus the test suite, and they call the same `uv run` commands
CI does so a hook can never disagree with CI about which tool it used.

## Tests

**pytest**, plus **`pytest-homeassistant-custom-component`** for anything touching HA. Pin that
package to the HA version in `docker-compose.yml` — its fixtures track core, and a mismatch produces
confusing failures that look like bugs in our code.

```
uv run pytest                            # everything
uv run pytest tests/test_api_client.py   # client only, no HA runtime

uv run --isolated --locked --only-group minimum-homeassistant python -m pytest tests
```

The last one is the oldest supported Home Assistant. It is not redundant with the first: the two
HA versions disagree about parts of the helper surface, and it has already caught a registry call
that was deprecated at the top of the range and absent at the bottom.

Conventions:

- **Fixtures in `tests/fixtures/` are redacted real API responses**, captured by
  `scripts/probe_api.py`. Never hand-write a response shape — the API is undocumented and a guessed
  fixture tests our guess rather than the API. Regenerate from the probe when the API changes.
- Keep **two `getlatestpackets` captures taken ~60 s apart**. They're what prove a counter actually
  increases, and what any monotonicity handling has to be tested against.
- The `enable_custom_integrations` fixture is required for HA to load the component in tests; it
  belongs in `conftest.py`.
- `asyncio_mode = "auto"` in `pyproject.toml`, so tests don't each need a marker.

What's worth testing, in priority order:

1. **Sensor typing** — assert `device_class`, `state_class` and unit on every sensor. These fail
   silently in production; a test is the only place the mistake surfaces loudly.
2. **Client parsing against fixtures**, including missing and unknown fields — the tolerant-parsing
   behaviour is a deliberate design choice and should be pinned by tests.
3. **Config flow**, every path: success, invalid auth, cannot connect, unexpected error, duplicate
   entry.
4. **Coordinator** — `UpdateFailed` on connection errors, and that a 401 raises
   `ConfigEntryAuthFailed` so reauth triggers. Don't wait a year for token expiry to discover the
   reauth flow is broken.

No coverage gate. But the config flow is where the bugs live, so cover all of its branches.

## Scope

Grid import/export only. Solar production is **not** available from the HAN port and is out of
scope; `CONTEXT.md` explains why, so it doesn't get re-litigated.

## Running things

**Local Home Assistant, via `docker compose up`** — this is the normal target. HA on
`http://localhost:8123`, with `dev/config/` holding a throwaway configuration. The image is pinned
to the same HA version as the real instance, so what passes locally is representative.

A custom integration is imported into HA's Python process, so there is **no hot reload**. The
component is also not bind-mounted, so every edit needs a copy as well as a restart — both are
`scripts/dev-sync.sh`.

```
docker compose up -d                       # start local HA
./scripts/dev-sync.sh                      # after editing the component: copy, then restart
docker compose logs -f homeassistant       # watch coordinator polls at debug level
```

**The component is copied in, not mounted, and that is deliberate.** Bind-mounting it at
`/config/custom_components/perific` nests a mount inside the `/config` mount, and on Docker Desktop
for Mac the inner mount silently empties — not only across `docker compose restart` but while the
container sits running, with `RestartCount` still 0 and the host files untouched. HA then reports
`Cannot find integration perific`, or `No module named 'custom_components.perific.config_flow'` if
it emptied after startup. Both read like broken code rather than a broken mount, so if the
integration vanishes, check what HA can actually see before debugging anything else:

```
docker compose exec homeassistant ls /config/custom_components/perific
```

**Deploying to the real instance** is `scripts/deploy.sh`, and it is for final verification only —
long-term statistics accumulating against real data is the one thing the container can't prove.
That instance runs a household. Never iterate against it. Its deployment specifics — how it is run,
what the deploy script has to talk to — are in `LOCAL.md`.

## Working agreements

- **Never commit, push, or open PRs.** Leave changes uncommitted and say where they are.
- **`.env` is never read into an agent session and never committed.** Scripts load it themselves.
- **Write every tracked file as if the repository were already public.** It is private now and
  intended to go public later, so nothing should need scrubbing first. Anything about the specific
  installation — how Home Assistant is deployed, what else is on the account, host paths, the rest
  of the house — goes in the gitignored `LOCAL.md`, and tracked files refer to it instead. Write
  findings in terms of what the *device* or *API* does, not what this account happens to contain.
- Build in small milestones that each end somewhere we can stop.
- Run the tests and the linter after anything non-trivial.
