# Device notes

What the API actually returns, captured from a real device by `scripts/probe_api.py` on 2026-09-15.
Everything here is **observed**, not documented. Where it contradicts `docs/api/enegic.md`'s
Documented claims, this file wins.

Device: Perific One, `ItemSubType: EM2One`, hardware 4.6, firmware 4.5.15, RJ12 to the meter's HAN
port, packet version `pv: 3`. Fixtures in `tests/fixtures/` are the redacted captures.

## Authentication

`PUT /createtoken` returns `TokenInfo` **and** a large `User` object.

| | |
|---|---|
| Token | 36 characters (GUID-shaped) |
| Validity | exactly 365 days |
| Timestamps | `2026-09-15T08:16:54.4029791Z` — UTC, **seven** fractional digits |

Seven digits is more precision than `datetime.fromisoformat` accepts; truncate to six before
parsing. `Created` and `ValidTo` differ only in the last digit, so don't treat sub-second precision
as meaningful.

`User` carries `UserId`, `StripeId`, `Domain` (the installing company), `MustChangePassword`, and a
`Capabilities` list of 36 entries, each duplicated — 72 in total. The capabilities are almost all
EV-charger integrations (`IntegrationZaptec`, `IntegrationEasee`, …); the two non-charger entries
are **`Solar`** and **`Pair`**. Nothing in `User` is needed by the integration.

## Account contents

`GET /getaccountoverview` returns `Items`, `Chargers` and `User`. **`Chargers` was `[]` on an
account that does own an EV charger** — the charger appears in `Items` instead. Don't read
`Chargers`.

The probed account held four items, of which **one** was the meter:

| `ItemCategory` | `ItemType` | `ItemSubType` | What it is |
|---|---|---|---|
| `LocalVirtual` | `Reporter` | `ZaptecReporter` | Charger load-balancing reporter |
| `LocalVirtual` | `Charger` | `ZaptecCharger` | EV charger |
| `"None"` | `"None"` | `"None"` | An empty stub item |
| `LocalPhysical` | `Phase` | `EM2One` | The Perific One |

So the answer to "can the account contain non-meter items" is **yes, definitively**. The filter is
`ItemCategory == "LocalPhysical"` and `ItemType == "Phase"`.

Two traps in that table:

- Unset enum fields are the **string** `"None"`, not JSON `null`. A filter written against Python's
  `None` silently fails to exclude them.
- `SystemName` is `null` on the charger items but a string elsewhere.

`Name` is user-editable and arrives in whatever language the owner typed. It is display text, never
identity.

Per-item fields beyond the documented set:

| Field | Observed | Use |
|---|---|---|
| `ItemState` | `"Active"` | Availability signal |
| `DeletionTime` | `null` | Non-null presumably means a deleted item; skip those |
| `FlashSeqNo` | `22120` | Equals the `PhaseMinute` packet's `seqno` |
| `LatestPacketFromItem` | a packet | See below — **not** a substitute for `/getlatestpackets` |
| `ActualItemUserParameters` | dict | Hardware detail: `FW`, `HW`, `HBaud: 115200`, `Mac`, `ServerUdpAddr: phase.device.enegic.com`, `ServerUdpPort: 65110`, and a `Cap` map (`RJ12: true`, `WiFi: true`, `LAN: false`) |
| `UserSettings` | dict | The app's own config: `voltage: 230` nominal, `fuselevel: 16`, and a node named `"One V2 HAN"` with the 4th phase inactive |
| `SystemSettings` | `{}` | Empty |
| `ClusterId` / `GroupId` / `SharedDeviceOwner` | `null` | — |

`ItemId` is a 13-digit millisecond epoch matching the item's own `CreationTime`. It is stable and
suitable for unique IDs. `Name` is user-editable and is not.

`LatestPacketFromItem` contains only the real-time packet — `dv`, `hiavg`, `huavg`, no energy
registers — so `/getlatestpackets` is still required. Its `hdr` is `1002` where `/getlatestpackets`
reports `1`.

## Packets

`PUT /getlatestpackets` returned **one** entry, for the meter only: items that don't report packets
are simply absent. Filter by category anyway rather than relying on that.

All four buckets were present, but **the field set differs per bucket**:

| Field | `PhaseRealTime` | `PhaseMinute` | `PhaseHour` | `PhaseDay` |
|---|---|---|---|---|
| `dv`, `hiavg`, `huavg` | ✅ | ✅ | ✅ | ✅ |
| `himin`, `himax` | — | ✅ | ✅ | ✅ |
| `hwi`, `hwo` | — | ✅ | ✅ | ✅ |
| `hwpi`, `hwpo` | — | — | ✅ | ✅ |

Two consequences that shape the integration:

- **Energy comes from `PhaseMinute`.** It is the freshest bucket carrying `hwi` / `hwo`;
  `PhaseRealTime` has no energy registers at all. `abelgladstone/homeassistant-perific` arrived at
  the same split independently — energy from `PhaseMinute`, everything else from `PhaseRealTime`.
- **There is no instantaneous power field anywhere.** `hwpi` / `hwpo` exist only in the hour and day
  buckets. See "Power" below.

Cadence, from `seqno` deltas across the two captures 60 s apart:

| Bucket | Behaviour |
|---|---|
| `PhaseRealTime` | `seqno` +6 in 60 s → roughly every 10 s |
| `PhaseMinute` | `seqno` +1, `ts` on the minute boundary |
| `PhaseHour` / `PhaseDay` | byte-identical packet re-served, `seqno` delta 0 |

Five-minute polling therefore skips four of five minute packets. Nothing is lost: the registers are
cumulative.

`dv` was `2` while `pv` was `3`, confirming `dv` is a data version and not a measurement.

## `hwi` / `hwo` are cumulative kWh registers — confirmed

The blocking question, and the answer is unambiguous.

`PhaseMinute`, one minute apart:

| | t0 (10:15) | t1 (10:16) | delta |
|---|---|---|---|
| `hwi` | 248718.155 | 248718.222 | +0.067 |
| `hwo` | 18048.557 | 18048.572 | +0.015 |

Six-figure values moving by hundredths. **`TOTAL_INCREASING` is correct**, and the unit is kWh — not
merely by assumption, but because the deltas reconcile with the measured current and voltage:

```
hiavg = [ 9.9, 7.5, -3.79 ] A      huavg = [ 231.2, 232.0, 236.7 ] V

positive phases:  9.9×231.2 + 7.5×232.0 = 4029 W  ->  0.0672 kWh in 60 s   (hwi: +0.067)
negative phase:              3.79×236.7 =  897 W  ->  0.0150 kWh in 60 s   (hwo: +0.015)
```

Both directions match to under half a percent.

### The documented sign convention is inverted

`docs/api/enegic.md` records, from `toshi38`, that negative `hiavg` means import. The arithmetic
above shows the opposite: **positive `hiavg` is import, negative is export.** The positive phases
account for the rise in `hwi` and the negative one for the rise in `hwo`, and `hwi` is import (it
reads 248,710 kWh against `hwo`'s 18,046, and `Pokeyo`'s client treats it as consumption).

Caveat: one minute of steady load. Worth re-checking at a midday export peak, where the signs on
most phases should flip.

### Both registers can rise in the same minute

They did here — `hwi` and `hwo` both increased, because the meter accounts per phase and directions
differed between phases. Don't treat import and export as mutually exclusive.

## `hwpi` / `hwpo` are per-phase kWh for the bucket period — confirmed

`docs/specs/perific-integration.md` originally specified the power sensor as `hwpi` / `hwpo` summed
and scaled from kW to W. They are not power.

A capture taken with the vendor app open beside it settles it. `PhaseDay` carried
`hwpi = [9.781, 5.312, 1.25]` and `hwpo = [2.766, 4.012, 5.198]`:

| | `sum()` | App |
|---|---|---|
| `hwpi` | 16.343 kWh | "bought yesterday" 16.3 |
| `hwpo` | 11.976 kWh | "sold yesterday" 12 |

Read as kW instead, L1 would have been 9.8 kW — 42 A at 233 V, against a 16 A fuse.

Two further things fall out of the same capture:

- **`ts` is the start of the period, and `hwi` / `hwo` are the register at its end.** The `PhaseDay`
  bucket served by `/getlatestpackets` is the last *completed* day, not the current one.
- **"Today" is a subtraction.** `hwi(PhaseMinute) − hwi(PhaseDay)` gave 10.180 kWh against the app's
  "bought today" 10.2, and the export pair gave 0.002 against 0. Nothing in the API reports a
  running daily total.

## Power is `Σ hiavg × huavg`, and that is what the app shows

No bucket carries a power field. The product of the per-phase signed current and voltage is what the
vendor's own client displays:

```
hiavg = [ 1.5, 1.5, 0.7 ] A     huavg = [ 235.8, 235.6, 237.3 ] V
                                ->  873 W     app: "buying right now 0.87 kW"
```

All three phases positive, and the app's "selling right now" read zero. Against the energy registers
as ground truth over a minute, the same product came within 1.0% on import and 2.1% on export while
both directions were active at once.

Strictly this is *apparent* power. The agreement says the power factor was near 1 in these captures,
not that it always is, so expect it to read slightly high under a reactive load. It is the only
instantaneous source available: deriving power from `PhaseMinute` register deltas would average over
the poll interval instead, which is a different measurement.

## Rate limits

Still unmeasured. The probe's requests drew no `429` and no throttling, which says nothing about the
real ceiling. The poll interval is configurable with a 15 s floor, below the 60 s default but above
the device's own ~10 s cadence; treat anything aggressive as unexplored.
