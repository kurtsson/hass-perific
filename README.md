# hass-perific

Home Assistant integration for the **Perific One** energy monitor.

Reads grid import and export from your electricity meter's HAN port via the Enegic cloud API, and
feeds them to the Energy dashboard and long-term statistics.

> **Early days.** Version 0.5.0, running against one household's meter. The sensors and their typing
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
integration polls that cloud API and publishes ten sensors for the meter: live import and export
power, per-phase current and voltage, and two diagnostics. They feed the Energy dashboard, history
and automations like anything else in Home Assistant.

Energy is not among them, and that is the point. Rather than accumulating it from whatever Home
Assistant happened to be awake for, the integration imports the hourly series straight from Enegic's
own record — back to the day the device was registered, and topped up every hour. An outage leaves
no hole and no catch-up spike. See [Energy history](#energy-history).

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
| Power imported | W | ✅ | Live. Use as *grid consumption* under the dashboard's two-sensor power mode. |
| Power exported | W | ✅ | Live. The matching *return to grid* sensor. |
| Current L1–L3 | A | ✅ | Signed: negative means that phase is exporting. |
| Voltage L1–L3 | V | ✅ | Diagnostic. |
| Last packet | — | ✅ | Diagnostic. Timestamp of the newest packet the meter reported. |
| Status | — | ✅ | Diagnostic. `ok`, `stale`, `no_data` or `offline`. |

Every sensor above goes *unavailable* when its reading is missing, which tells you something is
wrong but not what. **Status** is the exception: it stays readable through a failed poll, and is
there to answer which. `offline` means Home Assistant could not reach the API at all; `no_data`
means the poll succeeded but the meter was not in the response; `stale` means the meter's newest
packet is more than five minutes old, which usually points at its Wi-Fi rather than at anything
here. Pair it with **Last packet** to see how long the data has been standing still.

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

- Home Assistant 2026.5.1 or newer — the floor CI runs the whole suite against on every change
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

**Your password is not stored.** It is exchanged once for an API token, and only the token and your
username are written to the config entry. Tokens are valid for about a year; when one expires — or
if the API rejects it sooner — Home Assistant raises its normal reauthentication prompt and asks for
the password again to mint a replacement. Upgrading from an earlier version migrates the entry
automatically: the stored password is spent once and then removed.

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
username, your API token and the device's MAC address are removed. The token's expiry date is left
in, because it is what explains an unexpected reauthentication prompt.

### Energy dashboard

Under **Settings → Dashboards → Energy**, open the grid connection to get *Configure grid
connection*, and fill it in like this:

| Field in the dialog | What to pick | Where it comes from |
|---|---|---|
| Energy imported from grid | **Imported electricity** | the imported history |
| Energy exported to grid | **Exported electricity** | the imported history |
| Type of power measurement | **Two sensors** | |
| → Power imported from grid | **Power imported** | the live sensor |
| → Power exported to grid | **Power exported** | the live sensor |

The two energy entries carry a **chart icon** and say **Perific** underneath, because they are
imported statistics rather than entities. They are the only energy options this integration offers —
there is no sensor version to confuse them with.

The power half is a different matter: **pick the ordinary sensors there.** Power is a live reading
that nothing can reconstruct after the fact, so there is no history version of it and the picker
will not offer one. Leave it on *No power sensor* and the dashboard still works, just without the
live view. **Two sensors** is the mode that suits this integration: the meter accounts per phase, so
import and export can both be non-zero at the same instant, and the other modes assume a single
sensor that is positive one way and negative the other. Home Assistant builds its own helper sensor
from the pair and says so in the dialog.

The same dialog holds the cost fields, and those need a word of their own — see [Cost](#cost)
below.

### Energy history

The hourly energy figures do not come from polling. Home Assistant only records statistics for the
hours it was running, so a restart, an outage or a rejected token leaves a hole — and because the
meter's registers are cumulative, the missing energy is not lost but dumped into whichever hour
collection resumed, as one implausible spike.

Instead, this integration reads the history Enegic already keeps, at one point per minute, and
writes it straight into long-term statistics:

- **On first setup** it imports everything back to the day your device was registered, which is
  usually well before you installed Home Assistant. Expect the Energy dashboard to have history the
  moment you finish configuring it.
- **Every hour after that**, a few minutes past, it imports whatever is new. Importing is a
  replace rather than an add, so a run that overlaps what it already wrote changes nothing.
- **An outage repairs itself.** The next successful run simply covers a wider span. There is
  nothing to detect and nothing to trigger.

Hours the vendor has no data for stay empty rather than being interpolated. Nothing here invents a
reading the meter never reported.

One consequence worth knowing: these series have **no entity**. They appear in the Energy dashboard
and under **Developer Tools → Statistics**, and nowhere else — not in History, and not as something
you can put on a card or use in an automation. There are deliberately no cumulative-energy sensors
either, because a second copy of the same series under a near-identical name is worse than no copy
at all. The power, current and voltage sensors are still ordinary entities and behave normally.

### Cost

Home Assistant normally works out cost itself, from the energy entity and a price entity. It
cannot here, and this is not an oversight on either side: `energy/data.py` **rejects** a price
entity outright when the energy source is an imported statistic, and directs you at the dashboard's
`stat_cost` field instead. So the integration works the price out and fills that field's series.

It is off until you give it a price entity, under the integration's **Configure** button.

**Buying and selling are priced differently**, and that is the part worth reading twice:

| | Formula |
|---|---|
| What you buy | `(spot + supplier markup + energy tax) × VAT` |
| What you sell | `spot + export premium` |

Export carries no energy tax and no VAT, because a household selling surplus charges neither. The
markup, tax and VAT fields apply *only* to what you buy, even though both sides read the same spot
price entity by default.

A Swedish worked example, for one hour in SE3:

| | öre/kWh |
|---|---|
| Nord Pool spot, excluding VAT | 139.37 |
| Supplier markup (påslag) | 5.00 |
| Energy tax (energiskatt), excluding VAT | 42.80 |
| **Subtotal, excluding VAT** | **187.17** |
| **× 1.25 VAT** | **233.96** — about 2.34 kr/kWh |

Entered as `0.05`, `0.4280` and `25`, since the price entity reports SEK/kWh rather than öre.

**The energy tax field is the one that catches people.** The published Swedish figure of 53.50
öre/kWh *includes* VAT. Enter 42.80, or VAT gets applied to it twice.

Two things this cannot be:

- **It is not your bill.** Fixed monthly charges — grid subscription, supplier fee — are not per
  kWh and have nowhere to go in this model. What you get is an accurate *variable* cost.
- **It starts later than your energy history.** Prices are read from the price entity's own
  recorded statistics, so cost begins when Home Assistant first saw that sensor. Energy imported
  from before then stays uncosted rather than being priced at a guess.
- **It trails the energy by an hour.** An hour is costed once the price entity has a *complete*
  hourly average for it, which is only true once the hour is over. The newest hour of energy is
  therefore always uncosted for a while; the next run picks it up.

The series appear within the hour, and then have to be pointed at from the Energy dashboard — this
does not happen by itself. Reopen *Configure grid connection*, and for each of the two sources:

| Field in the dialog | What to pick |
|---|---|
| Use an entity tracking the total costs | **Imported electricity cost** |
| …and on the return-to-grid source | **Exported electricity compensation** |

**The other cost options are greyed out, and that is expected.** *Use an entity with current price*
and *Use a static price* are refused outright next to an imported statistic; the total-cost field is
the only one Home Assistant will accept, and filling it makes the configuration valid again.

Both entries carry a chart icon and say **Perific** underneath, like the energy ones — they are
statistics, not entities, so they will not show up anywhere you can pick an entity.

To force a rebuild — after a long outage, or if you want to re-read a period — call
**`perific.import_history`**. With no arguments it continues from wherever the last import stopped;
give it a `start` and it re-reads from there, replacing what is stored:

```yaml
action: perific.import_history
data:
  start: "2026-08-29T17:00:00+02:00"
```

How far back Enegic retains is not yet known — this device has not been in service long enough for
a limit to show. It has no bearing on ordinary operation, which only ever reads forward.

## Development

The Python environment is managed by [uv](https://docs.astral.sh/uv/), and `uv.lock` pins every
dependency — including Home Assistant itself, through
`pytest-homeassistant-custom-component`. Install uv once (`brew install uv`), then:

```bash
uv sync --locked --group dev               # exactly what CI installs
uv run pre-commit install                  # lint, types and tests before every commit

uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run basedpyright                        # types; also what the editor runs
npx prettier --check .                     # JSON — ruff doesn't cover it
```

CI also runs the suite against the oldest Home Assistant the integration claims to support, which is
the `minimum-homeassistant` dependency group. Run it locally the same way:

```bash
uv run --isolated --locked --only-group minimum-homeassistant python -m pytest tests
```

The floor is declared in three places that must agree — `hacs.json`, that dependency group, and the
README's requirements — and `tests/test_release_metadata.py` fails if `hacs.json` and the group
drift apart. The same file checks that `pyproject.toml` and `manifest.json` carry the same version,
and that every translation covers the same keys as `strings.json`.

```bash
docker compose up -d                       # Home Assistant on http://localhost:8123
./scripts/dev-sync.sh                      # after editing the integration: copy in, restart
docker compose logs -f homeassistant       # debug logging is on for this component
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

- **Energy never becomes a sensor.** The other integrations publish cumulative counters and let
  Home Assistant build the hourly series from whatever it managed to poll, so a restart or an
  outage leaves a hole and the missing energy reappears as one spike in the hour collection
  resumed. Here the hourly series is read from the vendor's own record instead, which makes the
  hole impossible rather than repairable. A net sensor is doubly wrong: net can decrease, and a
  `TOTAL_INCREASING` counter reads every decrease as a meter reset.
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
