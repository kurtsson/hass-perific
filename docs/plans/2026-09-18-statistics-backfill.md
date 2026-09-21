# Backfilling statistics after a polling gap

Written 2026-09-18 while the evidence was fresh. **Both gates were cleared on 2026-09-21** — see
"Gates" below.

**Superseded as an approach, not as evidence.** Repairing gaps treats a symptom: the hole exists
only because the energy series is derived from polling continuity. Reading the vendor's own record
instead means it cannot arise. `docs/plans/2026-09-21-energy-history-import.md` is the plan that
gets built; everything below about the outages, the endpoint and the arithmetic still holds and is
what that plan argues from.

## The problem

When polling stops, the hourly statistics series gets a hole. Both known causes stop it for an
unbounded time rather than for one poll:

- **A rejected token.** `DataUpdateCoordinator` guards its reschedule with `if not auth_failed`, so
  `ConfigEntryAuthFailed` ends polling until reauth completes or the entry reloads. Observed on the
  live instance on 2026-09-17: 401s at 17:54 and again at 06:48 the next morning, on a token stored
  valid until 2027. Counting rejections before escalating narrows the window but does not close it —
  a genuinely dead token still waits for a human.
- **Home Assistant being down** for any other reason.

## What is actually lost

Less than it looks, and this is the reason to think before building anything.

`hwi` and `hwo` are cumulative registers, not rates. When polling resumes the counter has already
advanced by the whole gap's energy, and Home Assistant's `TOTAL_INCREASING` handling folds that
entire delta into the hour the reading landed in. So:

| | Effect of a gap |
|---|---|
| Lifetime total | Intact. Nothing is lost. |
| Year-over-year comparison | Intact. This is the project's actual goal. |
| Hours inside the gap | Read zero. |
| The hour collection resumed | One spike holding the whole gap. |

A gap shorter than an hour therefore costs nothing at all — the delta lands in the hour it belonged
to anyway. **Only gaps spanning hour boundaries are worth repairing.** Measure real gap lengths
before building this; if the auth fix keeps them under an hour, this work has no value.

## Gates

Both were open when this was written. Both are now closed, and both closed in favour of building.

**Are the gaps long enough to matter? Yes.** `scripts/measure_gaps.py` reads the hourly rows off the
real instance and reports the runs with no row. Over the five days the integration had then been
running, two gaps spanned hour boundaries:

| Gap | Lost | Resumption hour |
|---|---|---|
| 2026-09-17 18:00 → 2026-09-18 02:00 CEST | 9 h | 26.993 kWh in one hour, 43× the median |
| 2026-09-18 07:00 CEST | 1 h | 2.163 kWh, 3× the median |

Both line up with the 401s recorded above. Ten hours lost in five days, and a single hour holding
27 kWh, is comfortably past "not worth repairing".

**Does `/getphasedata` exist? Yes, and it is better than hoped.** `scripts/probe_api.py --phasedata`
established the call and `docs/api/enegic.md` now documents it. It is a **PUT**, not the documented
POST; it takes `itemId` and `startTime` (`endTime` optional); it returns **one point per minute**
rather than hourly buckets; and each point carries **`hwi` and `hwo`**, the same cumulative
registers the live packets do. Nothing has to be synthesised.

Two things found along the way that the implementation has to respect:

- **Timestamps are asymmetric.** `startTime` is read as UTC; the returned `ts` is in the item's own
  timezone. Getting this wrong books energy two hours from where it belongs, silently.
- **A wrong parameter name returns `200 []`, not an error.** Nancy ignores unknown fields. Any code
  calling this must treat an empty series as suspect rather than as "nothing happened then".

## Candidate mechanism

Two halves, both now verified.

**Reading history — verified.** `PUT /getphasedata`, documented in `docs/api/enegic.md` from the
probe rather than from community sources. Minute resolution, cumulative registers, everything back
to the device's registration.

**Writing history — verified mechanism, not yet used here.** Home Assistant exposes
`homeassistant.components.recorder.statistics.async_import_statistics`, which writes hourly
statistics rows retroactively for a `statistic_id`. This is how integrations with a cloud history
endpoint (Tibber among them) fill gaps. It writes the `statistics` table directly, which is the
never-purged one this project exists to accumulate.

## Open questions

1. ~~Does `/getphasedata` exist, and what does its request body look like?~~ **Yes.**
   `PUT`, `{itemId, startTime, endTime?}`, ISO dates read as UTC. See `docs/api/enegic.md`.
2. ~~What granularity does it return?~~ **One point per minute**, finer than `PhaseHour`.
3. ~~Cumulative registers or per-period sums?~~ **Cumulative.** `hwi` / `hwo` on every point, so
   `state` maps directly and `sum` follows from the deltas.
4. **How far back does the free tier retain?** Still open, and not answerable yet: the account
   serves everything back to the device's registration on 2026-08-29, which is younger than any
   plausible limit. Re-probe once the device is a few months old. Not blocking — a backfill needs
   to reach back hours, not months.
5. How are gaps detected — compare the counter delta against elapsed time on the first successful
   poll after a failure, or ask the recorder which hours are missing? `measure_gaps.py` does the
   latter over the websocket API; in-process the recorder can be asked directly.
6. What happens to a spike already written into the resumption hour? Re-importing that hour has to
   correct it, not add to it. Still open, and now the main design question — the 9-hour gap put
   27 kWh into one hour, so getting this wrong is worse than the gap.
7. New: which timezone does a rewritten hour belong to? `/getphasedata` labels points in the item's
   timezone and Home Assistant's statistics are UTC-keyed. This has to be converted once, in one
   place, with a test.

## Do not

- Do not synthesise readings. Interpolating a flat average across the gap would write numbers the
  meter never reported into the one table this project treats as ground truth. Either the API can
  say what happened, or the hours stay empty and honest. The probe makes this moot — the API does
  say what happened, to the minute.
- Do not trust an empty `/getphasedata` response. A wrong parameter name returns `200 []`, which is
  indistinguishable from a genuinely empty range unless the code checks.

## Learnings worth keeping regardless

- `ConfigEntryAuthFailed` stops the coordinator; `UpdateFailed` does not. The choice between them is
  about whether collection continues, not about which log line appears.
- The coordinator logs a failure only on the transition out of success, so an occurrence count in
  the Home Assistant log is a count of *outages*, not of failed requests.
- Cumulative counters are forgiving of gaps in a way that rate measurements are not. The power and
  current sensors lose their gap outright; the energy sensors do not.
