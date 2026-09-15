# Enegic API reference

Base URL: `https://api.enegic.com`

This API is **undocumented and reverse-engineered**. Enegic publishes no developer documentation and
offers no partner programme that we could find. Everything here comes from reading two community
integrations, and is marked with how much it can be trusted.

A real Perific One has since been probed. `docs/device-notes.md` holds those observations and
**takes precedence over anything marked Documented here.**

Confidence levels used below:

- **Confirmed** — two independent sources agree, or we've observed it ourselves.
- **Documented** — stated in `toshi38`'s `PERIFIC_API_DOCUMENTATION.md`. That file is
  self-described as AI-generated and unverified, but its embedded sample JSON looks like genuine
  captured output.
- **Unverified** — a single claim we have no corroboration for.

## Authentication

A real credential-based login exists, so there is no need to scrape a session token out of browser
developer tools (which is what `toshi38`'s README instructs).

### `PUT /createtoken` — **Confirmed**

```json
{ "username": "...", "password": "..." }
```

Response:

```json
{
  "TokenInfo": {
    "Token": "<token>",
    "Created": "2025-04-21T07:04:40.8966467Z",
    "ValidTo": "2026-04-21T07:04:40.8966468Z"
  }
}
```

`ValidTo` is exactly **365 days** after `Created`. Timestamps are ISO-8601 UTC with seven fractional
digits — more than `datetime.fromisoformat` accepts, so truncate to six before parsing.

The response also carries a large `User` object alongside `TokenInfo`. The integration needs nothing
from it.

Every other endpoint authenticates with a custom header:

```
X-Authorization: <token>
Content-Type: application/json
```

Note it is `X-Authorization`, **not** `Authorization`, and the token is sent bare — no `Bearer`
prefix.

### `PUT /refreshtoken` — **Documented**

Returns the same `TokenInfo` shape. **Whether it requires a still-valid token is unverified.** We
deliberately don't depend on it: re-minting through `/createtoken` always works, and the integration
leans on HA's reauth flow for renewal instead.

## Endpoints

| Endpoint | Method | Confidence | Purpose |
|---|---|---|---|
| `/createtoken` | PUT | Confirmed | Mint a token from username + password |
| `/refreshtoken` | PUT | Documented | Renew a token |
| `/getaccountoverview` | GET | Confirmed | List the devices ("items") on the account |
| `/getlatestpackets` | PUT | Confirmed | Current readings per item |
| `/getphasedata` | POST | Documented | Historical, date-ranged time series |
| `/getuserinfo` | GET | Documented | Profile information |
| `/isactivated` | PUT | Documented | Activation status |
| `/getitemuserparameters` | PUT | Documented | Per-device settings |
| `/getreporterssettingsforuser` | GET | Confirmed | EV-charger reporter settings |

Note that `/getlatestpackets` is a **PUT** despite being a read, and `/getaccountoverview` is a
**GET**. The verbs aren't consistent; don't infer them.

`/getreporterssettingsforuser` returns the load-balancing settings — `AllowedCurrent`,
`MainsFuseLevel`, `SafeModeCurrent` and `Mode` — as used by `PetrolHead2/perific-meter`. It returns
data on an account with EV-charger items. Out of scope: that balancing runs in Enegic hardware and
does not need Home Assistant.

Standard status codes: `200`, `400`, `401` (invalid token), `404`, `500`.

### `GET /getaccountoverview`

```json
{
  "Items": [
    {
      "ItemId": 12345,
      "Name": "...",
      "SystemName": "...",
      "ItemCategory": "...",
      "ItemType": "...",
      "ItemSubType": "...",
      "MacAddress": "...",
      "TimeZone": "...",
      "CreationTime": "..."
    }
  ]
}
```

`ItemId` is the stable per-device identifier and is what entity unique IDs are built from. It is a
millisecond epoch matching the item's `CreationTime`.

The real response also has top-level `Chargers` and `User` keys, and each item carries considerably
more than the fields above — `ItemState`, `DeletionTime`, `LatestPacketFromItem`,
`ActualItemUserParameters`, `UserSettings` and others. `docs/device-notes.md` enumerates them.

**Answered:** an account *does* contain non-meter items — EV chargers and empty stubs were both
observed. The meter is `ItemCategory: LocalPhysical` / `ItemType: Phase`. Unset enum fields come
back as the **string** `"None"` rather than `null`.

### `PUT /getlatestpackets`

Returns an array, one element per item:

```json
[
  {
    "ItemId": 12345,
    "LatestPackets": {
      "PhaseRealTime": { ... },
      "PhaseMinute":   { ... },
      "PhaseHour":     { ... },
      "PhaseDay":      { ... }
    }
  }
]
```

Each packet:

```json
{
  "hdr": 1, "iid": 12345, "ts": 1750000000000,
  "seqno": 42, "it": "...", "pv": 3,
  "fw": "...", "rssi": -67,
  "data": { ... }
}
```

| Bucket | Meaning | Cadence — **Confirmed** |
|---|---|---|
| `PhaseRealTime` | Most recent reading | ~10 s |
| `PhaseMinute` | Minute-averaged | 60 s, `ts` on the minute boundary |
| `PhaseHour` | Hour-averaged | re-served unchanged between polls |
| `PhaseDay` | Day-averaged, includes min/max | re-served unchanged between polls |

`pv` is the **packet version**. v2 is the clamp-sensor format (assumes a nominal 230 V rather than
measuring); v3 is the HAN-port format with real measurements. The `data` field sets differ between
them.

**The field set also differs per bucket, and this matters.** `PhaseRealTime` carries no energy
registers and only the hour and day buckets carry `hwpi` / `hwpo`. The per-bucket table is in
`docs/device-notes.md`; read it before choosing which bucket a sensor reads.

**Field names also differ by device.** `PetrolHead2/perific-meter` models its real-time packets as
`iavg` / `imin` / `imax` / `qmax` — no `h` prefix, no `hwi`, and energy derived from `qmax`, a raw
per-phase counter it documents as ~0.184 J per unit. Our device reports `hiavg` / `huavg` / `hwi`
instead. The `h` prefix plausibly marks HAN-sourced measurements against clamp-measured ones. This is
the concrete reason the client parses tolerantly rather than validating a fixed schema.

## `data` field reference

| Field | Meaning | Unit | Confidence |
|---|---|---|---|
| `hwi` | Total energy **imported** — cumulative | kWh | Confirmed |
| `hwo` | Total energy **exported** — cumulative | kWh | Confirmed |
| `hwpi` / `hwpo` | Energy imported / exported per phase, over the bucket's own period — **not power** | kWh | Observed, inferred |
| `hiavg` | Average current, per phase | A | Confirmed |
| `huavg` | Average voltage, per phase | V | Confirmed |
| `himin` / `himax` | Min / max current, per phase | A | Confirmed |
| `dv` | Data version — **not** a measurement | — | Confirmed (`2` while `pv` was `3`) |
| `iavg` / `uavg` | Non-`h`-prefixed current / voltage variants | A / V | Documented |

Sign convention on `hiavg`: **positive is import, negative is export.** This is the *opposite* of
what `toshi38`'s documentation claims. The correction is arithmetic, not opinion — the positive
phases account precisely for the rise in `hwi` and the negative one for the rise in `hwo`. The
working is in `docs/device-notes.md`.

Both registers can rise within the same minute, because the meter accounts per phase and phases can
run in opposite directions. Import and export are not mutually exclusive.

### Are `hwi` / `hwo` cumulative? — **Confirmed: yes**

Measured on a real v3 HAN device: two `PhaseMinute` packets a minute apart moved `hwi` from
248718.155 to 248718.222. Six-figure registers moving by hundredths of a kWh. `TOTAL_INCREASING` is
correct, and the deltas reconcile with current × voltage to under half a percent, which also pins
the unit as kWh. Working in `docs/device-notes.md`.

### Power

**There is no instantaneous power field.** `hwpi` / `hwpo` appear only in the hour and day buckets,
and their magnitudes are inconsistent with per-phase kW — they look like per-phase energy over the
bucket period instead.

Do **not** compute power as `Σ |hiavg| × huavg` either, which is what `toshi38`'s documentation
suggests. That yields *apparent* power in VA, not real power in W — it ignores the power factor, and
labelling it `device_class: power` with unit W would be quietly wrong.

What's left is deriving power from consecutive `hwi` / `hwo` readings: real power, averaged over the
poll interval. See `docs/device-notes.md`.

## Rate limits — **Unverified and contradictory**

`toshi38`'s two documents disagree with each other:

- `PERIFIC_API_DOCUMENTATION.md`: "The API doesn't appear to have strict rate limiting", with a
  recommendation to poll `/getlatestpackets` every 30–60 s.
- `README.md`: "The API has rate limits (1000 requests/hour, 10/second per endpoint)."

Both are from the self-declared AI-generated documentation, so neither figure is trustworthy. Until
measured, poll slowly — the integration uses 5 minutes (~288 calls/day), which is far under even the
stricter claim. A `429` is handled distinctly so a real limit degrades gracefully instead of
breaking setup.

Circumstantial evidence that the ceiling is not tight: `abelgladstone/homeassistant-perific` polls
every **10 seconds** and `PetrolHead2/perific-meter` every 30, apparently without trouble. Not a
reason to poll faster — nothing is gained — but it does suggest a `429` is unlikely at 5 minutes.

## Sources

- [`Pokeyo-AB/homeassistant-perific`](https://github.com/Pokeyo-AB/homeassistant-perific) — an
  independently written client; the corroborating source for auth and `hwi`.
- [`toshi38/homeassistant-perific`](https://github.com/toshi38/homeassistant-perific) — the fuller
  endpoint and field reference. Self-described as AI-generated; treat claims as hypotheses.

Both are checked out under `.reference/` (gitignored) for reading.
