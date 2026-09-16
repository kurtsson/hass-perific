# hass-perific

Home Assistant integration for the **Perific One** energy monitor.

Reads grid import and export from your electricity meter's HAN port via the Enegic cloud API, and
feeds them to the Energy dashboard and long-term statistics.

> **Early days.** Version 0.2.0, running against one household's meter. The sensors and their typing
> are verified, but the API is undocumented and this has been exercised on a single device — expect
> rough edges, and please open an issue if you hit one.

## Why this exists

I'm Martin Kurtsson ([@kurtsson](https://github.com/kurtsson)), and I wrote this for my own house.

The Perific app is good at showing what the meter is doing right now. What it doesn't do is keep the
history — on a free account the detail ages out, so a question like *"how did this October compare
with last October?"* isn't one I could answer.

Home Assistant can, and the mechanism is worth knowing about before you commit to it. The recorder
purges detailed state history after a few days, but the hourly `statistics` table it compiles
alongside is never purged — it is designed to be kept indefinitely. So once the meter is feeding
Home Assistant, every hour of every year stays queryable, on hardware you control and in a database
you can back up. Long-term retention is the point of this integration; the live readings are a
by-product of getting there.

That only works if the sensors are typed correctly. Home Assistant decides whether to record
statistics from a sensor's `device_class`, `state_class` and unit, and when the combination is wrong
it declines and logs a warning — the integration appears to work, entities update, and no history
accumulates. Getting that right, and proving it against a real device, is most of what this project
is.

## What it does

The Perific One reads your electricity meter's HAN port and reports to Enegic's cloud. This
integration polls that cloud API and publishes ten sensors for the meter: cumulative import and
export energy, live import and export power, and per-phase current and voltage. They feed the Energy
dashboard, long-term statistics, history and automations like anything else in Home Assistant.

What it can't do:

- **No solar production.** The HAN port only sees the grid connection point, so you get net import
  and export, not what your panels produced. Use your inverter's own integration for that.
- **No local access.** The device pushes to Enegic's servers, so everything here goes through their
  cloud. There is no LAN API to talk to, and the readings are therefore only as fresh as the poll
  interval allows.
- **Not fast enough for load balancing.** The device reports roughly every 10 seconds, but polling
  a cloud API that hard isn't reasonable and its rate limits are undocumented. Treat this as
  monitoring and history, not as a control loop.

This is an unofficial, personal project. It is not affiliated with, endorsed by, or supported by
Perific or Enegic, and it relies on an undocumented API that they are free to change.

## Sensors

| Sensor | Unit | Status | Notes |
|---|---|---|---|
| Energy imported | kWh | ✅ | Cumulative. Use as *grid consumption* in the Energy dashboard. |
| Energy exported | kWh | ✅ | Cumulative. Use as *return to grid*. |
| Power imported | W | ✅ | Live. Use as *grid consumption* under the dashboard's two-sensor power mode. |
| Power exported | W | ✅ | Live. The matching *return to grid* sensor. |
| Current L1–L3 | A | ✅ | Signed: negative means that phase is exporting. |
| Voltage L1–L3 | V | ✅ | Diagnostic. |

Import and export are separate sensors rather than one signed value because the meter accounts per
phase, so both can be non-zero at the same moment — one phase exporting while another imports. A net
figure would hide that, and Home Assistant's Energy dashboard asks for the pair anyway.

The API publishes no power field. The power sensors are `Σ (current × voltage)` across the phases,
which is what the Perific app itself displays; it reconciles with the energy registers to within a
few percent. Strictly it is apparent power, so expect it to read slightly high under a poor power
factor.

There is deliberately **no net-energy sensor**. Net can decrease, so it cannot be a
`TOTAL_INCREASING` counter without every downward move reading as a meter reset; the Energy
dashboard derives net from import and export itself.

## Requirements

- Home Assistant 2026.9.2 or newer
- A Perific account (username and password)

## Installation

Through [HACS](https://hacs.xyz), as a custom repository:

1. HACS → ⋮ → **Custom repositories**
2. Repository `kurtsson/hass-perific`, type **Integration**
3. Add, then install **Perific** from the HACS list
4. Restart Home Assistant
5. **Settings → Devices & Services → Add Integration → Perific**

Or by hand: copy `custom_components/perific/` into your Home Assistant `config/custom_components/`
and restart.

Home Assistant will warn that this is a custom integration it has not tested. That warning is
expected for anything installed outside core.

## Configuration

Add the integration from **Settings → Devices & Services → Add Integration**, then enter your
Perific username and password. Nothing goes in `configuration.yaml`.

Tokens are valid for about a year. When one expires, Home Assistant prompts you to sign in again.

**Poll interval.** The default is 60 seconds, changeable under the integration's **Configure**
button. The energy registers only advance once a minute, so polling faster buys nothing there; the
real-time bucket the power and current sensors read moves every ~10 seconds, so a shorter interval
does make those fresher. The API's rate limits are undocumented and unmeasured, which is why the
floor is 15 seconds rather than the device's own 10 — lower it gradually.

If you do go too fast, the API answers `429` and the sensors go unavailable. Home Assistant logs
that once and then stays quiet, which is easy to miss, so the integration also raises an issue under
**Settings → Repairs** naming the current interval. It withdraws itself as soon as a poll succeeds.

### Reporting a problem

The integration's **⋮ → Download diagnostics** gives a redacted dump: the config entry, the
coordinator's health, the meters, and the last raw packets received. The packets are the useful
part — nearly every failure here is the API returning a shape the parser didn't expect. Your
username, password and the device's MAC address are removed.

### Energy dashboard

Under **Settings → Dashboards → Energy**, open the grid connection to get *Configure grid
connection*, and fill it in like this:

| Field in the dialog | Sensor to pick |
|---|---|
| Energy imported from grid | **Energy imported** |
| Energy exported to grid | **Energy exported** |
| Type of power measurement | **Two sensors** |
| → Power imported from grid | **Power imported** |
| → Power exported to grid | **Power exported** |

The power half is optional — leave it on *No power sensor* and the dashboard still works, just
without the live view. Choosing **Two sensors** is what suits this integration: the meter accounts
per phase, so import and export can both be non-zero at the same instant, and the other modes assume
a single sensor that is positive one way and negative the other. Home Assistant creates its own
helper sensor from the pair and says so in the dialog.

Entity names follow your Home Assistant language, so on a Swedish instance these appear as *Inköpt
elektricitet*, *Såld elektricitet*, *Inköpt effekt* and *Såld effekt*.

Cost tracking is independent of this integration — point it at whatever price entity you already
have, such as a Nord Pool sensor.

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
Home Assistant through its REST API, and rolls back if the integration doesn't load again. It is
for final verification against a real instance — long-term statistics accumulating over days is the
one thing the local container can't prove. Never iterate against a live instance with it.

How the swap works is not incidental. Home Assistant reads `<dir>/manifest.json` for **every**
directory in `custom_components/`, including ones whose names begin with a dot — and such a name
resolves to the empty module `custom_components.`, which fails the scan for every custom integration
on the instance. So the new version is unpacked into a holding directory whose manifest sits one
level deeper, where the scan skips it, and moved into place with a rename inside the same directory
so the live component is never half-written. The script refuses to finish if it finds a dotted
directory left behind.

The previous version is kept in `$HOME/.perific-deploy` on the remote, not under the config
directory, which on a container install is often not writable by the SSH user even when
`custom_components/` is. Override with `HA_DEPLOY_DIR`.

It needs four more keys in the same `.env`:

```
HA_URL=http://homeassistant.local     # through whatever fronts it; not necessarily :8123
HA_TOKEN=...                          # Profile -> Long-lived access tokens
HA_SSH=user@homeassistant.local
HA_CONFIG_DIR=/path/to/ha/config      # the host path bind-mounted to /config
```

Success is judged by the config entry reaching the `loaded` state, which is language-independent.
Entity IDs are not: they are built from translated names, so on a Swedish instance the import sensor
is `sensor.<device>_inkopt_elektricitet`, with no `energy_import` anywhere in it.

Optionally `HA_DEPLOY_DIR` (default `$HOME/.perific-deploy` on the remote), `HA_VERIFY_ENTITY`, an
extra substring that must appear in some entity ID (empty by default), and `RESTART_TIMEOUT`
(default 180 seconds).

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
- **Power is computed, and split by direction.** There is no instantaneous power field in the API.
  Other integrations sum `|current| × voltage` into a single figure, one of them against a hardcoded
  230 V. Here the per-phase products keep their sign and are split into an import and an export
  sensor, using the voltage the device reports — because the meter accounts per phase, so both
  directions can be live at once. The approximation is the same either way and is stated above:
  it is apparent power.
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

## Licence

MIT
