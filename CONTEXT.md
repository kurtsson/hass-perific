# CONTEXT.md

Background for this project: what the hardware is, what problem it solves, and which questions are
already settled.

## The goal

Get grid energy data from a Perific One into Home Assistant, and keep it long enough to compare one
year against another. The Perific account's own retention is too short for that on the free tier.

## The device

**Perific One** — reads the electricity meter's HAN port. Cloud-connected; data reaches it via
`api.enegic.com` (Enegic is the backend; Perific is the device brand). Home Assistant polls that
cloud API rather than the device, which never speaks to the local network.

Details of the particular installation this is developed against — the Home Assistant deployment,
what else is on the account, local paths — live in `LOCAL.md`, which is gitignored. Everything in
this repository is written to be publishable without scrubbing; anything instance-specific belongs
there instead.

## Where development happens

**A local Home Assistant container.** `docker compose up` brings up HA on `localhost:8123` against a
throwaway config in `dev/config/`. `scripts/dev-sync.sh` copies the component in and restarts it;
the component is deliberately **not** bind-mounted, because nesting a mount inside the `/config`
mount empties itself on Docker Desktop for Mac while the container runs. The image is pinned to the
same version as the real instance so local results are representative.

The real instance is the target for final verification only — the one thing a container can't prove
is long-term statistics accumulating against real data over days.

## Why HA's long-term statistics are the answer

The retention requirement is satisfied by Home Assistant itself, for free. Verified by reading
`homeassistant/components/recorder/purge.py`: the periodic purge deletes from `states`, `events`,
`statistics_short_term` and `statistics_runs` — and never from the long-term `statistics` table.

There are two statistics tables, and only one of them survives: `statistics_short_term` holds
5-minute aggregates and is purged, while `statistics` holds hourly aggregates and is not. (Core
still emits exactly `EVENT_RECORDER_5MIN_STATISTICS_GENERATED` and
`EVENT_RECORDER_HOURLY_STATISTICS_GENERATED` — no other granularity exists as of 2026.9.)

So hourly statistics persist indefinitely while raw state history is purged after ~10 days. No
InfluxDB, no second system to maintain.

None of this depends on the recorder's storage backend. SQLite is the default on every install
method, and `recorder: db_url:` can point it at PostgreSQL or MariaDB instead; the recorder goes
through SQLAlchemy either way, so the tables, the `state_class` rules and the never-purged
`statistics` table behave identically. Two consequences are worth knowing for a project about
multi-year retention: an external database starts empty rather than migrating what SQLite holds,
and it falls outside Home Assistant's own backups — the history no longer sits in the config
directory, so it needs its own dump.

The catch: HA only records statistics for sensors whose `state_class` and unit it accepts, and it
declines silently — a log warning, no error. That's why sensor typing is the one thing this project
has to get right first time, and why it's called out in `AGENTS.md`.

Polling frequency barely matters *for this goal*. Even five-minute polling puts multiple samples in
every short-term bucket and dozens in every hourly one, so polling faster buys nothing for
year-over-year comparison — it only spends rate-limit budget we haven't measured. The interval is
configurable and defaults to 60 seconds for the sake of the live power and current sensors, which do
get fresher; the retention goal would be equally well served by a much slower poll.

## What the HAN port can and cannot see

The HAN port publishes what the *meter* sees at the grid connection point: import and export power,
per-phase voltage and current, and cumulative import/export energy registers.

It **cannot** see solar production. Production and self-consumption are indistinguishable from the
meter's position — when the panels produce 4 kW and the house draws 3 kW, the meter observes 1 kW of
export and has no knowledge of the other 3 kW. The Perific app reflects this: it shows surplus, not
generation.

This is settled, not an open question. Solar production comes from the inverter.

A **Perific Monitor with solar clamps** could measure production directly — a clamp on the PV
circuit sees generation regardless of what the meter thinks. Different device, different packet
format, and out of scope here.

## Why this is a new integration rather than a fork

Four community integrations exist, and essentially everything known about the API here came from
them. `README.md`'s Prior art section credits what each one contributed. They were read closely;
none was adopted as a base, for reasons of fit.

**Authentication.** `toshi38`'s setup asks the user to scrape a session token out of browser
developer tools. Its own documentation describes a real login endpoint — `PUT /createtoken`,
username and password, a token valid about a year — which turned out to work. That single discovery
is what makes an ordinary config flow and HA's reauth possible here, and it is the most valuable
thing to come out of reading any of them.

**Shape.** `Pokeyo-AB` is the closest in structure, with a coordinator/entity/hub split, tests and
translations. Building on it would still have meant rearranging the repository for HACS and reworking
the manifest and setup path — most of the cost of starting fresh, without the freedom to type the
sensors from scratch.

**Sensor typing, which is the whole point of this project.** The existing integrations publish a
net-energy sensor as `TOTAL_INCREASING`. Net can fall, and HA reads a fall in such a counter as a
meter reset, which puts spurious spikes in the long-term series — precisely the outcome this
repository exists to avoid. Hence no net sensor at all here, and typing covered by tests.

**Power.** The two forks of `Pokeyo` publish current × voltage as `device_class: power` in watts,
and one uses a hardcoded 230 V rather than the voltage the device reports. Strictly that product is
apparent power in VA. This integration publishes the same product — the vendor's own app computes
it, and it reconciles with the energy registers to within a few percent — but as a directional pair,
using the reported voltage, with the approximation stated in the README rather than left implicit.

**Field names vary by device.** `PetrolHead2`'s device reports a different field set from this one.
That is why the client parses tolerantly instead of validating against a fixed schema.

All four are checked out under `.reference/` (gitignored) so they can be read offline, alongside
`custom-components/zaptec` as the pattern reference. `AGENTS.md` says what each is good for. What
has been extracted from them lives in `docs/api/enegic.md` with a confidence level per claim —
prefer that over re-reading the sources.

## Distribution stance

Development is private. HACS cannot read private repositories at all, so during development the
integration is installed by deploy script rather than through HACS. If it proves itself, the
repository goes public and is added as a HACS custom repository.

**Home Assistant core is not the target.** Core forbids vendored API clients (it requires a
published PyPI library), expects 100% config-flow test coverage and >95% module coverage, needs a
`home-assistant/brands` pull request, and reviews take months with no published turnaround. The API
here is also undocumented and reverse-engineered, which makes acceptance a maintainer judgement call
rather than a checklist.

None of that rules core out later. Following current HA patterns — `entry.runtime_data`, a typed
config entry, a properly separated client — keeps the path open at no extra cost. But nothing should
be done *for* core at the expense of shipping.

## Open questions

The probe (`scripts/probe_api.py`) settled several of these against the real device; the findings are
in `docs/device-notes.md`.

- ~~Are `hwi` / `hwo` cumulative or interval deltas?~~ **Cumulative kWh registers.** Confirmed by
  two samples a minute apart, cross-checked against current × voltage. `TOTAL_INCREASING` is right.
- ~~Can the account contain non-meter items?~~ **Yes.** An account can hold EV chargers and stub
  items alongside the meter, so the integration filters on `LocalPhysical` / `Phase`.
- **What are the real rate limits?** The two community sources contradict each other ("no strict
  rate limiting" versus "1000/hour, 10/second") and both are self-declared AI-generated docs.
  Until measured, poll slowly.
- **Does `/refreshtoken` require a still-valid token?** Unverified. Not relied upon — re-minting via
  `/createtoken` always works.
- ~~Where does power come from?~~ **`Σ hiavg × huavg` from `PhaseRealTime`.** Not `hwpi` / `hwpo`,
  which are per-phase kWh for the bucket period and absent from the live buckets. Confirmed against
  the vendor app, which displays the same product.
