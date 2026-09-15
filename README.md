# hass-perific

Home Assistant integration for the **Perific One** energy monitor.

Reads grid import and export from your electricity meter's HAN port via the Enegic cloud API, and
feeds them to the Energy dashboard and long-term statistics.

> **Early development.** Not installable yet.

## Sensors

| Sensor | Unit | Status | Notes |
|---|---|---|---|
| Energy imported | kWh | ✅ | Cumulative. Use as *grid consumption* in the Energy dashboard. |
| Energy exported | kWh | planned | Cumulative. Use as *return to grid*. |
| Power | W | planned | Derived from consecutive energy readings — the API exposes no instantaneous power. |

There is deliberately **no net-energy sensor**. Net can decrease, so it cannot be a
`TOTAL_INCREASING` counter without every downward move reading as a meter reset; the Energy
dashboard derives net from import and export itself.

Solar production is not available — the HAN port only sees the grid connection point. Use your
inverter's own integration for that.

## Requirements

- Home Assistant 2026.9.2 or newer
- A Perific account (username and password)

## Installation

Not published yet. It will be installable as a HACS custom repository.

## Configuration

Add the integration from **Settings → Devices & Services → Add Integration**, then enter your
Perific username and password. Nothing goes in `configuration.yaml`.

Tokens are valid for about a year. When one expires, Home Assistant prompts you to sign in again.

## Development

```bash
docker compose up -d                       # Home Assistant on http://localhost:8123
./scripts/dev-sync.sh                      # after editing the integration: copy in, restart
docker compose logs -f homeassistant       # debug logging is on for this component

pytest
ruff check . && ruff format --check .
npx prettier --check .                     # JSON — ruff doesn't cover it
mypy custom_components/perific/api
```

There is no hot reload — Home Assistant imports the integration into its own process, so every
change needs a restart.

The API probe records what the Enegic API actually returns, into `scripts/probe_out/`. It needs no
virtualenv — standard library only:

```bash
python3 scripts/probe_api.py            # two samples 60s apart
python3 scripts/probe_api.py --wait 120
```

It reads credentials from a `.env` in the repository root:

```
PERIFIC_USERNAME=...
PERIFIC_PASSWORD=...
```

### Deploying

`scripts/deploy.sh` packages the component, ships it over SSH, swaps it in atomically, restarts
Home Assistant through its REST API, and rolls back if the entities don't come back. It is for
final verification against a real instance — long-term statistics accumulating over days is the one
thing the local container can't prove. Never iterate against a live instance with it.

It needs four more keys in the same `.env`:

```
HA_URL=http://homeassistant.local     # through whatever fronts it; not necessarily :8123
HA_TOKEN=...                          # Profile -> Long-lived access tokens
HA_SSH=user@homeassistant.local
HA_CONFIG_DIR=/path/to/ha/config      # the host path bind-mounted to /config
```

Optionally `HA_VERIFY_ENTITY` (default `energy_import`), matched against entity IDs to decide
whether the deploy worked, and `RESTART_TIMEOUT` (default 180 seconds).

## Documentation

| | |
|---|---|
| [`AGENTS.md`](AGENTS.md) | Conventions, project layout, tooling |
| [`CONTEXT.md`](CONTEXT.md) | Hardware, constraints, background |
| [`docs/api/enegic.md`](docs/api/enegic.md) | Enegic API reference |
| [`docs/specs/`](docs/specs/) | Design decisions |
| [`docs/plans/`](docs/plans/) | Implementation plans |

## Prior art

The Enegic API is undocumented. Everything this integration knows about it was reverse-engineered
first by other people, who published their work for free — this would have been a much longer
project without them.

### Perific / Enegic

| Project | What it contributed here |
|---|---|
| [`toshi38/homeassistant-perific`](https://github.com/toshi38/homeassistant-perific) | The fullest endpoint list and `data` field reference anywhere. The author notes the repository was AI-generated and asks readers to proof-read it, so its claims were treated as hypotheses and checked against a real device — but its sample payloads look like genuine captures and were the starting point for all of it. |
| [`Pokeyo-AB/homeassistant-perific`](https://github.com/Pokeyo-AB/homeassistant-perific) | An independently written client, which is what makes it valuable: where it agrees with `toshi38` on a field, that agreement is real corroboration rather than one document repeating itself. |
| [`PetrolHead2/perific-meter`](https://github.com/PetrolHead2/perific-meter) | Models packets as `iavg` / `imin` / `imax` / `qmax` where this device reports `hiavg` / `huavg` / `hwi`. That mismatch is the clearest evidence that **packet field names vary by device**, and the reason this client parses tolerantly instead of validating a fixed schema. |
| [`abelgladstone/homeassistant-perific`](https://github.com/abelgladstone/homeassistant-perific) | Reads energy from the `PhaseMinute` bucket and everything else from `PhaseRealTime` — the same split this integration arrived at independently after probing a device. Independent agreement on a bucket choice is worth a lot when the API is undocumented. |

The most valuable single discovery across all four: **a real login endpoint exists**
(`PUT /createtoken`, username and password, token valid about a year). No scraping a session token
out of browser developer tools, and renewal fits Home Assistant's own reauth flow.

What has been extracted from them, with a confidence level on every claim, is in
[`docs/api/enegic.md`](docs/api/enegic.md).

### Where this one differs

- **No net-energy sensor.** Net (import − export) can decrease, and a `TOTAL_INCREASING` sensor
  reads every decrease as a meter reset, which corrupts the long-term statistics series. The Energy
  dashboard derives net from import and export by itself, so there is nothing to gain from
  publishing it.
- **Power is derived, not read.** There is no instantaneous power field in the API. Computing
  `Σ |current| × voltage` gives *apparent* power in VA, which is wrong by the power factor and
  shouldn't carry `device_class: power` in watts. Power here comes from the change in the energy
  registers between polls instead.
- **Long-term statistics are the goal**, not a side effect — which is why sensor typing is covered
  by tests rather than left to be discovered from a missing history graph.

### Home Assistant patterns

Neither of these has anything to do with Perific; both were read for how a modern custom
integration is put together.

- [`custom-components/zaptec`](https://github.com/custom-components/zaptec) — `entry.runtime_data`,
  a typed config entry, a full user/reauth/reconfigure flow, and entity descriptions that carry
  their own entity class.
- [`thomasloven/hass-plejd`](https://github.com/thomasloven/hass-plejd) — module layout: a base
  entity module that keeps the platform files thin, and a declarative diagnostics redaction tree.

- [`Pokeyo-AB/homeassistant-perific`](https://github.com/Pokeyo-AB/homeassistant-perific)
- [`toshi38/homeassistant-perific`](https://github.com/toshi38/homeassistant-perific)

## Licence

MIT
