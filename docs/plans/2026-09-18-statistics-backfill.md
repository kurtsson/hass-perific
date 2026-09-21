# Backfilling statistics after a polling gap

Follow-up work, not started. Written 2026-09-18 while the evidence was fresh.

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

## Candidate mechanism

Two halves, one verified and one not.

**Reading history — unverified.** `docs/api/enegic.md` lists `POST /getphasedata` as "Historical,
date-ranged time series", which came from community sources and has never been called by this
project. Everything depends on it: whether it exists, whether it takes a date range, what bucket
granularity it returns, whether it needs anything beyond the normal token. Verify with
`scripts/probe_api.py` against a real account before designing anything on top of it.

**Writing history — verified mechanism, not yet used here.** Home Assistant exposes
`homeassistant.components.recorder.statistics.async_import_statistics`, which writes hourly
statistics rows retroactively for a `statistic_id`. This is how integrations with a cloud history
endpoint (Tibber among them) fill gaps. It writes the `statistics` table directly, which is the
never-purged one this project exists to accumulate.

## Open questions

1. Does `/getphasedata` exist, and what does its request body look like?
2. What granularity does it return — the `PhaseHour` bucket, or something finer?
3. Does it return the cumulative registers or per-period sums? `async_import_statistics` wants both
   `sum` and `state`, and the mapping differs depending on which the endpoint gives.
4. How far back does the free tier retain? The retention limit is the reason this project exists, so
   the backfill window is probably short.
5. How are gaps detected — compare the counter delta against elapsed time on the first successful
   poll after a failure, or ask the recorder which hours are missing?
6. What happens to a spike already written into the resumption hour? Re-importing that hour has to
   correct it, not add to it.

## Do not

- Do not synthesise readings. Interpolating a flat average across the gap would write numbers the
  meter never reported into the one table this project treats as ground truth. Either the API can
  say what happened, or the hours stay empty and honest.
- Do not start this before the gaps are measured. See above.

## Learnings worth keeping regardless

- `ConfigEntryAuthFailed` stops the coordinator; `UpdateFailed` does not. The choice between them is
  about whether collection continues, not about which log line appears.
- The coordinator logs a failure only on the transition out of success, so an occurrence count in
  the Home Assistant log is a count of *outages*, not of failed requests.
- Cumulative counters are forgiving of gaps in a way that rate measurements are not. The power and
  current sensors lose their gap outright; the energy sensors do not.
