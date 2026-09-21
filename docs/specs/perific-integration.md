# Design: Perific integration

Design decisions and why they were made. For domain background see `CONTEXT.md`; for the API see
`docs/api/enegic.md`.

## Shape

Three layers, deliberately separable:

```
api/               HTTP, auth, response parsing. Knows nothing about Home Assistant.
coordinator.py     Polls the client on a timer, owns failure semantics.
entity.py          Base entity: device_info, unique_id, availability.
sensor.py          Entities built from declarative descriptions over coordinator data.
```

Data flows one way: `config_flow` captures credentials → `__init__` builds the client and
coordinator → coordinator polls → entities read `coordinator.data`. Nothing writes back; this
integration is read-only.

`entity.py` exists so that identity and availability are written once rather than per platform.
`thomasloven/hass-plejd` is the layout reference here (`AGENTS.md` says what to take from it and
what not to); the shape pays off as soon as a second platform appears, and per-phase sensors in M6
are that second platform.

`__init__.py` validates the connection *before* `async_forward_entry_setups`. Forwarding first
means `ConfigEntryNotReady` is raised with platforms already half-set-up, which is the order plejd
uses and the wrong way round.

## Decisions

### The API client is vendored, not published to PyPI

`custom_components/perific/api/` ships inside the integration. HACS permits this; HA core does
not — core requires an external library on PyPI with source distributions.

Publishing now would mean cutting a release, bumping a pin in `manifest.json`, and re-running
validation for every change to the response models — and those models will churn while the packet
format is still being learned. Vendoring makes that a plain commit.

The cost of keeping the option open is one rule: **`api/` imports nothing from `homeassistant.*` and
raises only its own exception types.** With that held, extracting it later means publishing the
package and rewriting a handful of import lines in the integration — contained, mechanical work.
`custom-components/zaptec` uses the same arrangement.

### Plain dataclasses, not pydantic

Both community integrations use pydantic. Here it's the wrong tool for two reasons.

The API has multiple packet-format versions (`pv` 2 for clamp sensors, 3 for HAN) whose `data` field
sets differ, and fields are absent from some buckets. Pydantic's default is to reject on
missing or unexpected fields, which would fail an entire poll — for every device on the account —
where the correct behaviour is one sensor going unavailable.

Second, pydantic needs a version pin in `manifest.json`, and those pins collide between custom
integrations sharing one Home Assistant environment. `zaptec` pins `pydantic>=2.11.7,<2.13`; a
second integration with an incompatible range is a support problem for the user and nothing we can
fix from here.

So: frozen dataclasses with `from_api()` classmethods that parse with `.get()` and defaults.
Unparseable fields become `None` and the affected entity reports unavailable.

### `entry.runtime_data` with a typed config entry

```python
type PerificConfigEntry = ConfigEntry[PerificCoordinator]
```

Rather than `hass.data[DOMAIN][entry.entry_id]`, which is the pre-2024.6 convention. Of the
integrations surveyed on a real HA install, only `zaptec` had migrated — but it's the documented
current pattern, gives static typing on the runtime object, and is cleaned up automatically on
unload.

The coordinator *is* the runtime data rather than being wrapped in a container object. It already
owns the client and the discovered meters, and a wrapper holding a single field earns nothing.

### Authentication leans on HA's reauth flow

The config flow takes a username and password, calls `PUT /createtoken`, and stores the token and
its expiry on the config entry. **The password is not stored.** Tokens last about a year.

The alternative — keeping the password and re-minting on every setup — was what the first version
did. It is simpler, but it puts a reusable account credential in `.storage` in plaintext for the
life of the integration, and spends a login on every restart, reload and options change against an
API whose rate limits are unmeasured. A token is narrower in blast radius, revocable, and already
has somewhere to go when it stops working.

Expiry is checked locally before the first request, so a year-old token prompts for a password
instead of spending a request on a certain 401.

Deliberately **not** implemented: re-minting inside an expiry margin. That would require keeping the
password, which is the thing being avoided. For the annual expiry HA already has the right
mechanism — raise `ConfigEntryAuthFailed` and the UI prompts for the password once.

Entries created by the first version are migrated on load: the stored password is spent once for a
token and then removed. A rejected password still migrates, into a reauth prompt; a network failure
does not, so the password survives for the retry rather than forcing a reauth nobody needed.

The constraint that makes this work: **`ConfigEntryAuthFailed` must be raised directly, never
wrapped in `UpdateFailed`.** Wrapped, the coordinator treats it as a transient failure, reauth never
triggers, and the integration silently stops updating.

The symmetric mistake costs more. `DataUpdateCoordinator` guards its reschedule with
`if not auth_failed`, so `ConfigEntryAuthFailed` does not merely fail one poll — it ends polling
until reauth completes or the entry reloads. A live instance produced two 401s thirteen hours apart
on a token valid for another year, and each one stopped collection outright. So rejections are
counted, and only the third consecutive one escalates; any success resets the count. Setup is the
exception and escalates immediately, because a failed setup is retried with a fresh coordinator and
a counter there could never reach its threshold.

`/refreshtoken` exists but its prerequisites are unverified, so nothing depends on it. Re-minting via
`/createtoken` always works.

### Polling every five minutes

Rate limits are unknown and the two available sources contradict each other (see
`docs/api/enegic.md`). Five minutes is ~288 requests/day, far below even the stricter claim.

Nothing is lost. Home Assistant aggregates into 5-minute short-term statistics and hourly long-term
statistics; the long-term table is what this project exists for, and it cannot tell the difference
between 5-second and 5-minute polling. Faster polling would spend an unmeasured budget for no gain.

### Sensor typing is the load-bearing decision

The whole point of the integration is long-term statistics, and Home Assistant decides whether to
record them from the sensor's `state_class` and unit. When the combination is invalid it declines
and logs a warning — the integration otherwise appears to work, with entities updating normally and
no history accumulating.

| Sensor | `device_class` | `state_class` | Unit | Source |
|---|---|---|---|---|
| `energy_import` | `ENERGY` | `TOTAL_INCREASING` | kWh | `hwi`, `PhaseMinute` |
| `energy_export` | `ENERGY` | `TOTAL_INCREASING` | kWh | `hwo`, `PhaseMinute` |
| `power_import` | `POWER` | `MEASUREMENT` | W | `Σ` positive `hiavg × huavg`, `PhaseRealTime` |
| `power_export` | `POWER` | `MEASUREMENT` | W | `Σ` negative phases, unsigned |
| `current_l1..3` | `CURRENT` | `MEASUREMENT` | A | `hiavg`, `PhaseRealTime`, sign kept |
| `voltage_l1..3` | `VOLTAGE` | `MEASUREMENT` | V | `huavg`, `PhaseRealTime`, diagnostic |

`TOTAL_INCREASING` is confirmed correct for `hwi` / `hwo`: they are cumulative kWh registers, probed
on a real device (`docs/device-notes.md`). It also carries the useful property that HA interprets a
decrease as a meter reset rather than negative consumption.

Both energy sensors read the **`PhaseMinute`** bucket. `PhaseRealTime` carries no energy registers
at all, so it is not an option, and the hour and day buckets are stale by design.

That property is also a hazard: a cloud API re-serving a stale or duplicate packet produces a
one-sample dip, which HA reads as a reset and which books the whole register as one period's
consumption. Statistics rows are laborious to correct after the fact, and they are the thing this
integration exists to build, so the guard is built rather than deferred: the asymmetry between
losing one held sample and corrupting the series favours holding.

It triggers only on a **fall**. A flat register is normal — it stays flat for hours whenever the
house is exporting, since nothing is flowing the other way — so only a decrease is treated as
suspect. The first fall below the running baseline is held and logged; if the next poll is back
above the baseline, the held reading is discarded and never reaches the recorder. If the next poll
is *also* below it, the fall is accepted, because a replaced meter genuinely does start again. The
second reading is not required to be lower than the first: a new meter counts upwards from its own
base.

The baseline survives an unavailable period deliberately. Were it cleared, the first packet after
any gap would be accepted unchallenged, which is exactly when a stale one is most likely.

The guard applies only to `TOTAL_INCREASING`. Power, current and voltage are measurements and fall
freely by nature.

### Power has no direct source

`hwpi` / `hwpo` are not power. They appear only in the hour and day buckets, and they are per-phase
kWh for the bucket period — confirmed against the vendor app in `docs/device-notes.md`.

Power is therefore **`hiavg × huavg` per phase, from `PhaseRealTime`**, with positive phases summed
into `power_import` and negative phases into `power_export`. Two pieces of evidence decided it. The
vendor's own app displays exactly this product: 873 W computed against 0.87 kW shown, from a capture
taken beside it. And against the energy registers as ground truth it agrees to within about two
percent, in both directions at once.

This publishes *apparent* power under `device_class: power`, which is a real inaccuracy under a poor
power factor — the sensor reads slightly high, never low. The alternative, energy delta over time
delta from consecutive `PhaseMinute` readings, measures something else: an average across the poll
interval rather than an instantaneous value. It cannot answer "what is the house drawing right now",
which is the question these sensors exist for.

Splitting by sign rather than taking `Σ |hiavg| × huavg` is load-bearing. The meter accounts per
phase, so one phase can export while another imports; an absolute-value sum reports the total as
consumption, and a signed net reports neither figure. The Energy dashboard wants the directional
pair anyway — `PowerConfig`'s two-sensor mode takes `stat_rate_from` and `stat_rate_to`, both
positive.

### The poll interval is configurable

The energy registers advance once a minute, so the 60 s default already reads them at full
resolution. `PhaseRealTime` moves every ~10 s, so a shorter interval buys fresher power, current and
voltage and nothing else. The floor is 15 s rather than the device's own cadence because the API's
rate limits are unmeasured.

Changing the option reloads the entry. Assigning `update_interval` on a live coordinator stores the
value without rescheduling the pending refresh, so the change would not take effect until after the
next poll.

### No net-energy sensor

Net (import − export) can decrease, so `TOTAL_INCREASING` is invalid for it — every downward move
would register as a meter reset. `TOTAL` would be correct, but the sensor isn't needed: the Energy
dashboard derives net from the import and export series itself.

Both community integrations ship a net sensor as `TOTAL_INCREASING`. Not shipping one at all is the
simplest way to not inherit that.

### Identity: one entry per account, one device per item

- Config entry `unique_id` = the account username, lowercased. One entry per Perific account;
  adding the same account twice aborts.
- Each tracked item from `/getaccountoverview` becomes a Home Assistant device, identified by
  `ItemId`.
- Entity `unique_id` = `f"{item_id}_{description.key}"`.

`ItemId` is the only stable per-device identifier the API offers — a millisecond epoch equal to the
item's creation time. `Name` is user-editable and must not be used for identity.

Accounts **do** contain non-meter items — EV chargers and stubs were both observed. The filter is
`ItemCategory == "LocalPhysical"` and `ItemType == "Phase"`, and anything else is logged and skipped
rather than turned into a device. Note that unset enum fields arrive as the string `"None"`, so a
filter comparing against Python's `None` would let the stub through.

### Distribution: HACS, not Home Assistant core

Core is not the target. It forbids vendored clients, expects 100% config-flow test coverage and
>95% module coverage, requires a `home-assistant/brands` pull request and a code-owner commitment,
and reviews take months with no published turnaround. The API being undocumented and
reverse-engineered makes acceptance a maintainer judgement call rather than a checklist.

Following current HA patterns keeps the path open at no cost, and starting in HACS and moving to
core later is a well-trodden route. But nothing is done *for* core at the expense of shipping.

Development is private, which means HACS can't be used to install it — HACS cannot read private
repositories at all. Hence a deploy script.

## Failure semantics

| Condition | Raised | Effect |
|---|---|---|
| Missing or expired token at setup | `ConfigEntryAuthFailed` | Entry enters reauth before any request is made |
| Token rejected at setup | `ConfigEntryAuthFailed` | Entry enters reauth; UI prompts for password |
| API unreachable at setup | `ConfigEntryNotReady` | HA retries setup with backoff |
| No supported items on the account | `ConfigEntryNotReady` | Retried; a real account shouldn't hit this |
| 401 during a poll | `ConfigEntryAuthFailed` — **unwrapped** | Reauth triggers |
| Connection error during a poll | `UpdateFailed` | Entities unavailable; coordinator retries |
| 429 during a poll | `UpdateFailed` with retry hint, plus a Repairs issue | Backs off instead of hammering, and says so where a user will see it |
| A field missing from a packet | nothing | That one entity reports unavailable |

The Repairs issue on `429` exists because `DataUpdateCoordinator` logs a failure only on the
transition out of success. Sustained throttling therefore leaves a single `ERROR` line and nothing
in the interface, while every sensor sits unavailable. Since the poll interval is user-configurable,
this is a failure a user can cause and can fix, so it needs a surface a user actually visits. The
issue names the interval in force and withdraws itself on the next successful poll.

## Open questions

Answered by the probe, in `docs/device-notes.md`: `hwi` / `hwo` are cumulative kWh registers, and an
account does contain non-meter items. Still open:

1. **What are the real rate limits?** Four requests drew no `429`, which proves nothing.
2. **Are `hwpi` / `hwpo` per-phase energy over the bucket period?** Inferred, not confirmed.
   `docs/device-notes.md` gives the test. Only affects M6.
3. **Does `/refreshtoken` need a valid token?** Not depended on either way.
4. **Does the sign convention hold under heavy export?** The correction to positive-is-import was
   measured during a single steady minute of net import.
