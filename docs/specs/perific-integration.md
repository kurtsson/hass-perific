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

The config flow takes a username and password, calls `PUT /createtoken`, and holds the token in
memory. Tokens last about a year.

Deliberately **not** implemented: persisting the token to disk, and re-minting inside an expiry
margin. A fresh login on each HA restart costs one request and removes a whole class of
stale-credential-on-disk bugs. For the annual expiry, HA already has the right mechanism — raise
`ConfigEntryAuthFailed` and the UI prompts for the password.

The constraint that makes this work: **`ConfigEntryAuthFailed` must be raised directly, never
wrapped in `UpdateFailed`.** Wrapped, the coordinator treats it as a transient failure, reauth never
triggers, and the integration silently stops updating.

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
| `power` | `POWER` | `MEASUREMENT` | W | derived — see below |

`TOTAL_INCREASING` is confirmed correct for `hwi` / `hwo`: they are cumulative kWh registers, probed
on a real device (`docs/device-notes.md`). It also carries the useful property that HA interprets a
decrease as a meter reset rather than negative consumption.

Both energy sensors read the **`PhaseMinute`** bucket. `PhaseRealTime` carries no energy registers
at all, so it is not an option, and the hour and day buckets are stale by design.

That property is also a hazard: a cloud API re-serving a stale or duplicate packet produces a
one-sample dip, which HA reads as a reset and which corrupts the series. Statistics rows are
laborious to correct after the fact. A guard — suppress a single-sample regression, accept it only
when a second reading confirms — is designed but deferred until the behaviour is actually observed,
rather than built speculatively.

### Power has no direct source

The obvious candidate, `hwpi` / `hwpo` summed and converted from kW to W, does not work. Those
fields appear only in the hour and day buckets, and their magnitudes contradict per-phase kW anyway
— a day value of 13.819 would be 59 A on a phase whose measured maximum that day was 17.1 A.

No instantaneous power field exists. Power is therefore **derived from consecutive `hwi` / `hwo`
readings** — energy delta over time delta, giving real power averaged across the poll interval. That
needs the previous reading kept in the coordinator, which is why power is an M6 sensor rather than
part of the minimal integration.

The rejected alternative remains rejected: `Σ |hiavg| × huavg` produces apparent power in VA, and
publishing it as `device_class: power` in W would be quietly wrong by the power factor. That it
happened to match within 0.3% during the probe only means the power factor was near 1 for that
minute's load.

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
| Bad credentials at setup | `ConfigEntryAuthFailed` | Entry enters reauth; UI prompts for password |
| API unreachable at setup | `ConfigEntryNotReady` | HA retries setup with backoff |
| No supported items on the account | `ConfigEntryNotReady` | Retried; a real account shouldn't hit this |
| 401 during a poll | `ConfigEntryAuthFailed` — **unwrapped** | Reauth triggers |
| Connection error during a poll | `UpdateFailed` | Entities unavailable; coordinator retries |
| 429 during a poll | `UpdateFailed` with retry hint | Backs off instead of hammering |
| A field missing from a packet | nothing | That one entity reports unavailable |

## Open questions

Answered by the probe, in `docs/device-notes.md`: `hwi` / `hwo` are cumulative kWh registers, and an
account does contain non-meter items. Still open:

1. **What are the real rate limits?** Four requests drew no `429`, which proves nothing.
2. **Are `hwpi` / `hwpo` per-phase energy over the bucket period?** Inferred, not confirmed.
   `docs/device-notes.md` gives the test. Only affects M6.
3. **Does `/refreshtoken` need a valid token?** Not depended on either way.
4. **Does the sign convention hold under heavy export?** The correction to positive-is-import was
   measured during a single steady minute of net import.
