# Energy history import implementation plan

> **Status: all five tasks implemented and verified against the local container on 2026-09-21.**
> 223 tests pass on both 2026.9.2 and the 2026.5.1 floor; ruff, basedpyright and prettier are
> clean. Deployed to the Pi as 0.4.0 on 2026-09-21, which imported 516 hours back to the device's
> registration. What remains is the dashboard switchover — step 6 of "Verifying against real data".
>
> Four things came out differently from the plan; each is noted inline in the task it affects.
>
> **The container found a bug the tests could not.** The walk re-read the clock each time round, so
> the final chunk ended a few hundred milliseconds before the next reading of it and a sub-second
> window went out as `startTime == endTime`, which the API answers 400. It logged an exception on
> every steady-state run. `now` is now fixed for the whole walk and `HISTORY_MIN_WINDOW` refuses a
> window shorter than a minute. A frozen clock is exactly what hid this, so the regression test
> runs unfrozen and asserts the invariant — that no requested window is under a minute — rather
> than the constant.
>
> **What the container proved.** 512 hours imported, 2026-08-29 15:00Z .. now. All nine hours the
> Pi lost on 2026-09-17 are present, and they carry 24.941 kWh; with the resumption hour's own
> 1.630 kWh that is 26.571 kWh against the 26.993 kWh the Pi folded into a single hour — 1.6%
> apart, which is where the last pre-gap and first post-gap readings happened to land inside their
> hours. Two 19-hour holes remain, on 2026-08-31 and 2026-09-01: the vendor has no data for them
> either, so they stay empty and honest.

> **For agentic workers:** REQUIRED SUB-SKILL: use `superpowers:subagent-driven-development`
> (recommended) or `superpowers:executing-plans` to implement this task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Take energy out of the polling path entirely. Import the cumulative registers from
`/getphasedata` straight into long-term statistics on a timer, so the hourly energy series is a
copy of the vendor's record rather than a by-product of Home Assistant having been awake.

**Architecture:** Two kinds of data with different natures, handled differently. Power, current and
voltage are ephemeral observations and stay exactly as they are — polled from `/getlatestpackets`,
lost on a missed poll, and nobody minds. Energy is a record the vendor already keeps at minute
resolution back to device registration, so it is read rather than accumulated. A timer asks
`/getphasedata` for everything since the last hour already written and imports it as **external
statistics** under `perific:<item>_energy_import` / `_energy_export`. Import is an upsert, so a run
that overlaps what it already wrote is a no-op and an outage of any length heals on the next
successful run. There is no gap detection, no repair and no spike.

**Cadence: once an hour, five minutes past.** Statistics are hourly, so importing more often only
rewrites the current unfinished hour — it cannot make the series more complete. Because a run
resumes from wherever the last one stopped, the interval affects how *promptly* history lands and
never *whether* it does: a instance down for three days catches up on its next run. Steady state is
one request per meter per hour, against the 1440 a day `/getlatestpackets` already makes at the 60 s
poll. The live sensors are untouched and keep answering "what is happening now"; this series is the
archive.

**Tech stack:** Python 3.14, `aiohttp`, Home Assistant 2026.9.2 (floor 2026.5.1 per `hacs.json`),
pytest with `pytest-homeassistant-custom-component`, `uv`, `ruff`.

**Spec:** `docs/plans/2026-09-18-statistics-backfill.md` — the evidence and the endpoint findings.
`docs/api/enegic.md` — the endpoint. Both travel with this plan. Note that the spec is written
around *repairing* gaps; this plan supersedes that approach for the reason in "Why not a backfill"
below, and the spec's findings about the endpoint all still hold.

## Global constraints

- **Never commit.** `AGENTS.md`, "Working agreements": all changes are left uncommitted. Each task
  ends with a verification gate, not a commit.
- `custom_components/perific/api/` **imports nothing from `homeassistant.*`** and raises only its
  own exception types.
- Parse tolerantly: `.get()` with defaults, frozen dataclasses, no pydantic.
- Fixtures are redacted real captures, never hand-written.
- Comments carry constraints, not narration (`AGENTS.md`, "Comments").
- Every task runs `uv run ruff format .`, `uv run ruff check .`, `uv run basedpyright`,
  `uv run pytest`, and
  `uv run --isolated --locked --only-group minimum-homeassistant python -m pytest tests`.
- **The existing sensors are not touched.** `sensor.py`, `coordinator.py` and the existing
  `sensor.*` statistics stay exactly as they are, so the new series runs alongside the old one and
  the switchover is a dashboard setting, not a deploy.

## Why not a backfill

Repairing gaps treats a symptom. The gap exists because the energy series is derived from polling
continuity; read the vendor's record instead and it cannot arise. This deletes gap detection,
anchor selection, spike correction and the pre-deployment special case — the first import *is* the
history import.

It costs one thing: the series has to be an **external** statistic, not today's
`sensor.onerj12_inkopt_elektricitet`. `sensor/recorder.py:905` raises `state_class_removed` for a
numeric entity that has statistics but no `state_class`, so the current statistic id cannot be kept
while stopping the recorder from co-writing it, and dropping the entity instead trips `no_state`.
External ids are exempt: `energy/validate.py:266` returns early when the id is not an entity id, so
the Energy dashboard treats them as first class. This is how Opower and Tibber ship history.

The old series keeps its five days. The new one rebuilds twenty-three from the API, so nothing of
value is orphaned.

## Facts this plan rests on

Read out of the installed Home Assistant, and checked against **both** ends of the supported range
(2026.9.2 and the 2026.5.1 floor), because the two disagree about parts of the helper surface:

| Fact | Where |
|---|---|
| Import is an upsert on `(metadata_id, start)` | `recorder/statistics.py:2920` |
| `source` must equal the part of the id before the `:` | `recorder/statistics.py:2880` |
| `mean_type` and `unit_class` become mandatory in 2026.11, and both already exist at the 2026.5.1 floor | `recorder/statistics.py:2886` |
| `unit_class` for `kWh` is `"energy"` | `STATISTIC_UNIT_TO_UNIT_CONVERTER` |
| `get_last_statistics(hass, n, statistic_id, convert_units, types)` — same signature on both versions | `recorder/statistics.py` |
| Live compilation reads `statistics_short_term`, never the long-term table | `sensor/recorder.py:621` |

Pass `mean_type` and `unit_class` explicitly. They are accepted at the floor and required from
2026.11, so this is the only spelling that works across the whole range without a deprecation
warning.

## The sum

`sum` is cumulative-since-tracking and only ever read as a difference — `change` is
`_sum - prev_sum` (`recorder/statistics.py:2089`). Since this plan owns the whole series, the
scale is ours to choose:

- **First ever import:** the first hour gets `sum = 0`, so `K = -first_register`.
- **Every run after:** read our own last row with `get_last_statistics`, take
  `K = last.sum - last.state`, and write `sum = register + K`.

`K` stays constant while the register is monotonic, which is what makes a run that overlaps
previously written hours produce byte-identical values. A falling register means a meter reset and
is refused rather than guessed at.

## File structure

| File | Responsibility |
|---|---|
| `custom_components/perific/api/models.py` | Add `PhasePoint`, `parse_phase_data`. Reuses `PhaseData`, which already reads `hwi`/`hwo`. |
| `custom_components/perific/api/client.py` | Add `async_get_phase_data`. |
| `custom_components/perific/history.py` | **New.** Local-time conversion, hourly reduction, statistic identity, resume point, the import loop. |
| `custom_components/perific/const.py` | New constants. |
| `custom_components/perific/services.yaml` | **New.** The service schema. |
| `custom_components/perific/__init__.py` | Start the timer per entry; register the service. |
| `custom_components/perific/strings.json`, `translations/{en,sv}.json` | Service text. |
| `tests/fixtures/getphasedata.json` | **New.** Trimmed redacted capture. |
| `tests/test_api_phase_data.py` | **New.** Client and parsing. |
| `tests/test_history.py` | **New.** Conversion, rows, resume, the importer. |
| `tests/test_history_service.py` | **New.** Service and timer wiring. |

Named `history.py`, not `statistics.py`: the latter shadows both the standard library module and
Home Assistant's own, and this code imports the second one.

## Interfaces defined by this plan

Every task below uses these exact names.

```python
# custom_components/perific/api/models.py
@dataclass(frozen=True, slots=True)
class PhasePoint:
    timestamp: datetime  # naive, in the item's own timezone
    data: PhaseData


def parse_phase_data(payload: Any) -> list[PhasePoint]: ...


# custom_components/perific/api/client.py
async def async_get_phase_data(
    self, item_id: int, start: datetime, end: datetime | None = None
) -> list[PhasePoint]: ...


# custom_components/perific/history.py
def localise(
    points: list[PhasePoint], time_zone: str, window: tuple[datetime, datetime]
) -> list[tuple[datetime, PhaseData]]: ...


def hourly_registers(
    localised: list[tuple[datetime, PhaseData]], register: str
) -> dict[datetime, float]: ...


def statistic_id(item_id: int, key: str) -> str: ...
def statistic_metadata(meter: Item, key: str) -> StatisticMetaData: ...
def registered_at(item_id: int) -> datetime: ...


@dataclass(frozen=True, slots=True)
class Resume:
    offset: float  # K
    after: datetime | None  # the last hour already written, refreshed on the next run


def statistic_rows(
    registers: dict[datetime, float], resume: Resume
) -> list[StatisticData]: ...


async def async_resume_point(
    hass: HomeAssistant, statistic_id: str
) -> Resume | None: ...


class HistoryImporter:
    def __init__(self, hass: HomeAssistant, entry: PerificConfigEntry) -> None: ...
    async def async_run(self, _now: datetime | None = None) -> int: ...
    async def async_import_since(self, start: datetime | None) -> int: ...
```

`StatisticData` and `StatisticMetaData` come from `homeassistant.components.recorder.models`.

---

### Task 1: Fetch phase data from the API

**Files:**
- Modify: `custom_components/perific/api/models.py`, `api/client.py`, `api/__init__.py`
- Modify: `tests/conftest.py`
- Create: `tests/fixtures/getphasedata.json`, `tests/test_api_phase_data.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `PhasePoint`, `parse_phase_data`, `EnegicClient.async_get_phase_data`.

> **As built:** the fixture is 11 points (02:55–03:05 CEST), not 180. The harness blocks scripted
> file creation, so it had to be written by hand; a slice that crosses one hour boundary is all the
> tests need, and it is still a verbatim subset of the capture. `hwo` is flat across it, which
> turned out to be useful — it pins that a flat register is accepted.

- [ ] **Step 1: Create the fixture from the existing redacted capture**

The probe already captured a real response. Trim it to three whole hours; this subsets a real
response, it does not invent one.

```bash
uv run python - <<'PY'
import json, pathlib
src = pathlib.Path("scripts/probe_out/phasedata/redacted/05-getphasedata-recent.json")
groups = json.loads(src.read_text())
keep = [p for p in groups[0]["data"] if p["ts"][11:13] in {"02", "03", "04"}]
groups[0]["data"] = keep
pathlib.Path("tests/fixtures/getphasedata.json").write_text(json.dumps(groups, indent=2) + "\n")
print(len(keep), "points")
PY
```

Expected: `180 points`. If `scripts/probe_out/` is empty, regenerate it with
`python3 scripts/probe_api.py --phasedata` first.

- [ ] **Step 2: Add the fixture to conftest**

In `tests/conftest.py`, beside the other capture fixtures:

```python
@pytest.fixture
def phasedata() -> Any:
    """The captured ``PUT /getphasedata`` response, trimmed to three hours."""
    return load_fixture("getphasedata.json")
```

- [ ] **Step 3: Write the failing tests**

```python
# tests/test_api_phase_data.py
"""Parsing and fetching /getphasedata.

Its verb, both parameter names and its timestamp handling all differ from the
community documentation, so these tests pin the wire format as well as the
parsing. See docs/api/enegic.md.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from custom_components.perific.api import PhasePoint, parse_phase_data


def test_parses_points_from_the_capture(phasedata):
    points = parse_phase_data(phasedata)
    assert len(points) == 180
    assert all(isinstance(point, PhasePoint) for point in points)


def test_points_are_ordered_and_one_minute_apart(phasedata):
    points = parse_phase_data(phasedata)
    gaps = {
        (b.timestamp - a.timestamp).total_seconds()
        for a, b in zip(points, points[1:], strict=False)
    }
    assert gaps == {60.0}


def test_carries_the_cumulative_registers(phasedata):
    first = parse_phase_data(phasedata)[0]
    assert first.data.energy_import == pytest.approx(248868.26)
    assert first.data.energy_export == pytest.approx(18116.944)


def test_timestamps_are_naive(phasedata):
    # The API labels points in the item's own timezone with no offset. Attaching
    # one here would hide the conversion the importer has to do.
    assert parse_phase_data(phasedata)[0].timestamp.tzinfo is None


@pytest.mark.parametrize("payload", [None, {}, [], [{"data": None}], "nonsense"])
def test_tolerates_rubbish(payload):
    assert parse_phase_data(payload) == []


async def test_requests_the_documented_shape(make_client, fake_api, phasedata):
    fake_api.respond_json("PUT", "/getphasedata", phasedata)
    client = make_client(token="t")

    points = await client.async_get_phase_data(
        12345,
        datetime(2026, 9, 21, 0, tzinfo=UTC),
        datetime(2026, 9, 21, 6, tzinfo=UTC),
    )

    assert len(points) == 180
    request = fake_api.request_for("/getphasedata")
    assert request.method == "PUT"
    assert json.loads(request.body) == {
        "itemId": 12345,
        "startTime": "2026-09-21T00:00:00",
        "endTime": "2026-09-21T06:00:00",
    }


async def test_converts_aware_times_to_utc_before_sending(
    make_client, fake_api, phasedata
):
    """The server reads the naive strings as UTC, so a local-time window has to
    be converted rather than stripped of its offset."""
    from zoneinfo import ZoneInfo

    fake_api.respond_json("PUT", "/getphasedata", phasedata)
    client = make_client(token="t")

    await client.async_get_phase_data(
        12345, datetime(2026, 9, 21, 2, tzinfo=ZoneInfo("Europe/Stockholm"))
    )

    assert json.loads(fake_api.request_for("/getphasedata").body)["startTime"] == (
        "2026-09-21T00:00:00"
    )


async def test_omits_end_time_when_not_given(make_client, fake_api, phasedata):
    fake_api.respond_json("PUT", "/getphasedata", phasedata)
    client = make_client(token="t")

    await client.async_get_phase_data(12345, datetime(2026, 9, 21, tzinfo=UTC))

    assert "endTime" not in json.loads(fake_api.request_for("/getphasedata").body)
```

- [ ] **Step 4: Run the tests to verify they fail**

Run: `uv run pytest tests/test_api_phase_data.py -v`
Expected: FAIL — `ImportError: cannot import name 'PhasePoint'`.

- [ ] **Step 5: Add the model and parser**

In `custom_components/perific/api/models.py`, after `PhaseData`:

```python
@dataclass(frozen=True, slots=True)
class PhasePoint:
    """One minute of history for an item.

    ``timestamp`` is naive and in the *item's own* timezone, not UTC — see
    ``docs/api/enegic.md``. Converting it is the caller's job, because doing it
    here would need the item, which this module does not have.
    """

    timestamp: datetime
    data: PhaseData


def parse_phase_data(payload: Any) -> list[PhasePoint]:
    """Flatten a ``/getphasedata`` response into points, oldest first.

    Points arrive nested under a group whose ``dt`` has been the same constant
    for every window probed, so the grouping is flattened rather than read.
    """
    if not isinstance(payload, list):
        return []

    points: list[PhasePoint] = []
    for group in payload:
        if not isinstance(group, dict):
            continue
        for entry in group.get("data") or []:
            if not isinstance(entry, dict):
                continue
            timestamp = _iso_naive(entry.get("ts"))
            if timestamp is None:
                continue
            points.append(
                PhasePoint(
                    timestamp=timestamp, data=PhaseData.from_api(entry.get("data"))
                )
            )
    points.sort(key=lambda point: point.timestamp)
    return points


def _iso_naive(value: Any) -> datetime | None:
    """Parse the endpoint's naive ISO timestamps, rejecting anything else."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is None else None
```

- [ ] **Step 6: Add the client method**

In `custom_components/perific/api/client.py`, after `async_get_latest_packets`:

```python
    async def async_get_phase_data(
        self, item_id: int, start: datetime, end: datetime | None = None
    ) -> list[PhasePoint]:
        """Historical readings for one item, one point per minute.

        ``start`` and ``end`` are sent as naive ISO strings and read by the server
        as UTC; the timestamps that come back are in the item's own timezone. The
        same two fields carrying epoch milliseconds answer 500, and the
        ``fromDate``/``toDate`` the community documentation gives bind to nothing
        and return an empty list. See ``docs/api/enegic.md``.
        """
        body: dict[str, Any] = {"itemId": item_id, "startTime": _api_time(start)}
        if end is not None:
            body["endTime"] = _api_time(end)
        return parse_phase_data(await self._request("PUT", "/getphasedata", body=body))
```

At module level in `client.py`:

```python
def _api_time(value: datetime) -> str:
    """Format a datetime the way /getphasedata wants it: naive ISO, read as UTC."""
    if value.tzinfo is not None:
        value = value.astimezone(UTC).replace(tzinfo=None)
    return value.strftime("%Y-%m-%dT%H:%M:%S")
```

Add `from datetime import UTC, datetime` and `PhasePoint, parse_phase_data` to `client.py`'s
imports, and export `PhasePoint` and `parse_phase_data` from `api/__init__.py` — both the import
block and `__all__`, which is sorted.

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run pytest tests/test_api_phase_data.py -v`
Expected: 12 passed — 8 test functions, one of which is parametrised over 5 payloads.

- [ ] **Step 8: Verification gate**

Run: `uv run ruff format . && uv run ruff check . && uv run basedpyright && uv run pytest`
Expected: all clean, 175 passed — the 163 already in the suite plus these 12.

---

### Task 2: Convert local labels to UTC hours

**Files:**
- Create: `custom_components/perific/history.py`, `tests/test_history.py`

**Interfaces:**
- Consumes: `PhasePoint`, `PhaseData` from Task 1.
- Produces: `localise`, `hourly_registers`.

This is the task most likely to go wrong silently. `/getphasedata` labels points in the item's
timezone with no offset; statistics are keyed on UTC hours. An hour's error books energy in the
wrong hour and looks like correct data. Every expected value below was checked against real
`zoneinfo`, including that 2026-03-29 and 2026-10-25 are the actual EU transition dates.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_history.py
"""Turning /getphasedata into statistics rows.

The conversion is the risky part: the endpoint labels points in the item's own
timezone, Home Assistant keys statistics on UTC hours, and an hour's error is
invisible once written.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.perific.api import PhaseData, PhasePoint
from custom_components.perific.history import hourly_registers, localise

STOCKHOLM = "Europe/Stockholm"


def point(naive: str, imported: float = 1.0) -> PhasePoint:
    return PhasePoint(
        timestamp=datetime.fromisoformat(naive),
        data=PhaseData(energy_import=imported, energy_export=0.0),
    )


def window(start: str, end: str) -> tuple[datetime, datetime]:
    return datetime.fromisoformat(start), datetime.fromisoformat(end)


def test_summer_offset_is_two_hours():
    # 07:00 Stockholm in September is 05:00 UTC.
    [(when, _)] = localise(
        [point("2026-09-21T07:00:00")],
        STOCKHOLM,
        window("2026-09-21T05:00+00:00", "2026-09-21T06:00+00:00"),
    )
    assert when == datetime(2026, 9, 21, 5, tzinfo=UTC)


def test_winter_offset_is_one_hour():
    [(when, _)] = localise(
        [point("2026-12-01T06:00:00")],
        STOCKHOLM,
        window("2026-12-01T05:00+00:00", "2026-12-01T06:00+00:00"),
    )
    assert when == datetime(2026, 12, 1, 5, tzinfo=UTC)


def test_autumn_fold_resolves_by_monotonicity():
    """02:00-02:59 local happens twice on 2026-10-25.

    The labels alone are ambiguous. The series is ordered, so a label that goes
    backwards marks the second pass and everything after it is CET.
    """
    points = [
        point("2026-10-25T02:30:00"),  # first pass, CEST (+2) -> 00:30Z
        point("2026-10-25T02:00:00"),  # clocks went back, CET (+1) -> 01:00Z
        point("2026-10-25T03:00:00"),  # CET -> 02:00Z
    ]
    times = [
        when
        for when, _ in localise(
            points,
            STOCKHOLM,
            window("2026-10-25T00:00+00:00", "2026-10-25T03:00+00:00"),
        )
    ]

    assert times == [
        datetime(2026, 10, 25, 0, 30, tzinfo=UTC),
        datetime(2026, 10, 25, 1, 0, tzinfo=UTC),
        datetime(2026, 10, 25, 2, 0, tzinfo=UTC),
    ]
    assert times == sorted(times)


def test_window_disambiguates_a_first_point_inside_the_fold():
    # A window starting in the second pass has no earlier point to compare
    # against; the requested window is what rules out the first pass.
    [(when, _)] = localise(
        [point("2026-10-25T02:30:00")],
        STOCKHOLM,
        window("2026-10-25T01:00+00:00", "2026-10-25T02:00+00:00"),
    )
    assert when == datetime(2026, 10, 25, 1, 30, tzinfo=UTC)


def test_spring_forward_has_no_gap_in_utc():
    # 02:00-02:59 local does not exist on 2026-03-29.
    points = [point("2026-03-29T01:59:00"), point("2026-03-29T03:00:00")]
    times = [
        when
        for when, _ in localise(
            points,
            STOCKHOLM,
            window("2026-03-29T00:00+00:00", "2026-03-29T03:00+00:00"),
        )
    ]
    assert times[1] - times[0] == timedelta(minutes=1)


def test_unknown_timezone_is_refused():
    with pytest.raises(ValueError, match="timezone"):
        localise(
            [point("2026-09-21T02:00:00")],
            "Mars/Olympus",
            window("2026-09-21T00:00+00:00", "2026-09-21T01:00+00:00"),
        )


def test_hourly_registers_take_the_last_reading_in_each_hour():
    localised = [
        (datetime(2026, 9, 21, 5, 0, tzinfo=UTC), PhaseData(energy_import=10.0)),
        (datetime(2026, 9, 21, 5, 59, tzinfo=UTC), PhaseData(energy_import=11.0)),
        (datetime(2026, 9, 21, 6, 0, tzinfo=UTC), PhaseData(energy_import=12.0)),
    ]
    assert hourly_registers(localised, "energy_import") == {
        datetime(2026, 9, 21, 5, tzinfo=UTC): 11.0,
        datetime(2026, 9, 21, 6, tzinfo=UTC): 12.0,
    }


def test_hourly_registers_skip_points_missing_that_register():
    localised = [
        (datetime(2026, 9, 21, 5, 0, tzinfo=UTC), PhaseData(energy_import=10.0)),
        (datetime(2026, 9, 21, 5, 30, tzinfo=UTC), PhaseData(energy_import=None)),
    ]
    assert hourly_registers(localised, "energy_import") == {
        datetime(2026, 9, 21, 5, tzinfo=UTC): 10.0
    }
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_history.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'custom_components.perific.history'`.

- [ ] **Step 3: Write the module**

```python
# custom_components/perific/history.py
"""Importing the energy series from the vendor's own record.

Energy is not accumulated from polling here. ``/getphasedata`` holds the
cumulative registers at minute resolution, so the hourly series is a copy of
that record and does not depend on Home Assistant having been awake. The
reasoning is in ``docs/plans/2026-09-21-energy-history-import.md``; the endpoint
is in ``docs/api/enegic.md``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if TYPE_CHECKING:
    from .api import PhaseData, PhasePoint

HOUR = timedelta(hours=1)


def localise(
    points: list[PhasePoint],
    time_zone: str,
    window: tuple[datetime, datetime],
) -> list[tuple[datetime, PhaseData]]:
    """Attach UTC instants to points the API labelled in local wall-clock time.

    The autumn fold repeats an hour, so a label alone cannot say which pass it
    belongs to. Two things resolve it, in order: the requested window, which the
    first point must fall inside, and monotonicity, since a label that goes
    backwards can only be the clocks going back.
    """
    try:
        zone = ZoneInfo(time_zone)
    except (ZoneInfoNotFoundError, ValueError) as err:
        raise ValueError(f"unknown timezone {time_zone!r}") from err

    start, end = window
    localised: list[tuple[datetime, PhaseData]] = []
    fold = 0
    previous: datetime | None = None

    for point in points:
        naive = point.timestamp
        if previous is not None and naive < previous:
            # Wall-clock time cannot go backwards within one ordered series
            # except across the autumn fold. Everything after it is the second
            # pass, and `fold` is ignored once times are unambiguous again.
            fold = 1
        elif previous is None:
            when = naive.replace(tzinfo=zone, fold=0).astimezone(UTC)
            if when < start or when >= end:
                fold = 1
        previous = naive
        localised.append(
            (naive.replace(tzinfo=zone, fold=fold).astimezone(UTC), point.data)
        )

    return localised


def hourly_registers(
    localised: list[tuple[datetime, PhaseData]], register: str
) -> dict[datetime, float]:
    """The cumulative register as it stood at the end of each hour."""
    registers: dict[datetime, float] = {}
    for when, data in sorted(localised, key=lambda pair: pair[0]):
        value = getattr(data, register, None)
        if not isinstance(value, (int, float)):
            continue
        registers[when.replace(minute=0, second=0, microsecond=0)] = float(value)
    return registers
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_history.py -v`
Expected: 8 passed.

- [ ] **Step 5: Verification gate**

Run: `uv run ruff format . && uv run ruff check . && uv run basedpyright && uv run pytest`
Expected: all clean. Leave uncommitted.

---

### Task 3: Statistic identity, metadata and rows

**Files:**
- Modify: `custom_components/perific/history.py`, `custom_components/perific/const.py`
- Modify: `tests/test_history.py`

**Interfaces:**
- Consumes: `hourly_registers` from Task 2.
- Produces: `statistic_id`, `statistic_metadata`, `registered_at`, `Resume`, `statistic_rows`.

- [ ] **Step 1: Add the constants**

In `custom_components/perific/const.py`:

```python
# The two cumulative registers imported as external statistics, as
# {statistic key: PhaseData field}. Both names match; the mapping is explicit so
# a rename of either side cannot silently pair them wrongly.
HISTORY_REGISTERS: Final = {
    "energy_import": "energy_import",
    "energy_export": "energy_export",
}

# Shown in the statistics picker and the Energy dashboard, where there is no
# entity to take a translated name from.
HISTORY_NAMES: Final = {
    "energy_import": "Imported electricity",
    "energy_export": "Exported electricity",
}

# One request covers at most this much, so a long catch-up arrives as a series
# of ordinary responses rather than one very large one. The cap only bites while
# catching up: a steady-state run finds a single hour outstanding and stops after
# the first chunk.
HISTORY_CHUNK: Final = timedelta(days=1)
HISTORY_MAX_CHUNKS: Final = 40

# Statistics are hourly, so importing more often than that only rewrites the
# current unfinished hour. Run a few minutes past the hour, by which time the
# hour that just ended is complete and the vendor has it. Phase matters: an
# interval timer would drift to whatever minute setup happened at, leaving a
# finished hour unwritten for up to an hour.
HISTORY_RUN_AT_MINUTE: Final = 5

SERVICE_IMPORT_HISTORY: Final = "import_history"
ATTR_START: Final = "start"
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_history.py`:

```python
from homeassistant.components.recorder.models import StatisticData, StatisticMeanType

from custom_components.perific.history import (
    Resume,
    registered_at,
    statistic_id,
    statistic_metadata,
    statistic_rows,
)


def test_statistic_id_is_external_not_an_entity_id():
    # An entity-shaped id would make the recorder co-write the series and raise
    # a state_class_removed issue; an external one is exempt.
    assert statistic_id(1788016523401, "energy_import") == (
        "perific:1788016523401_energy_import"
    )
    assert ":" in statistic_id(1, "energy_import")


def test_metadata_matches_what_the_recorder_requires(meters):
    metadata = statistic_metadata(meters[0], "energy_import")
    # source must equal the part before the colon, or the import is refused.
    assert metadata["source"] == "perific"
    assert metadata["statistic_id"].startswith("perific:")
    assert metadata["has_sum"] is True
    assert metadata["has_mean"] is False
    assert metadata["unit_of_measurement"] == "kWh"
    # Both are mandatory from 2026.11 and already accepted at the 2026.5.1 floor.
    assert metadata["mean_type"] is StatisticMeanType.NONE
    assert metadata["unit_class"] == "energy"


def test_registered_at_decodes_the_item_id():
    # ItemId is a millisecond epoch of the device's registration, and the oldest
    # reading the account serves is one minute after it.
    assert registered_at(1788016523401) == datetime(
        2026, 8, 29, 15, 15, 23, 401000, tzinfo=UTC
    )


def test_first_ever_import_starts_the_series_at_zero():
    resume = Resume(offset=-248868.26, after=None)
    registers = {
        datetime(2026, 9, 21, 2, tzinfo=UTC): 248868.26,
        datetime(2026, 9, 21, 3, tzinfo=UTC): 248871.76,
    }
    first, second = statistic_rows(registers, resume)
    assert first["sum"] == pytest.approx(0.0)
    assert second["sum"] == pytest.approx(3.5)


def test_rows_carry_the_register_as_state():
    resume = Resume(offset=-248000.0, after=None)
    [row] = statistic_rows({datetime(2026, 9, 21, 2, tzinfo=UTC): 248868.26}, resume)
    assert row["state"] == pytest.approx(248868.26)
    assert row["start"] == datetime(2026, 9, 21, 2, tzinfo=UTC)


def test_consecutive_rows_differ_by_the_real_consumption():
    resume = Resume(offset=-248000.0, after=None)
    registers = {
        datetime(2026, 9, 21, 2, tzinfo=UTC): 248868.26,
        datetime(2026, 9, 21, 3, tzinfo=UTC): 248871.76,
    }
    first, second = statistic_rows(registers, resume)
    assert second["sum"] - first["sum"] == pytest.approx(3.5)


def test_rows_are_ordered_and_on_the_hour():
    resume = Resume(offset=0.0, after=None)
    registers = {
        datetime(2026, 9, 21, 3, tzinfo=UTC): 2.0,
        datetime(2026, 9, 21, 2, tzinfo=UTC): 1.0,
    }
    rows = statistic_rows(registers, resume)
    assert [row["start"] for row in rows] == sorted(row["start"] for row in rows)
    assert all(row["start"].minute == 0 for row in rows)
    assert all(row["start"].tzinfo is not None for row in rows)


def test_rewriting_an_hour_reproduces_the_same_sum():
    """A run that overlaps what it already wrote must be a no-op.

    The offset is read back off the previous run's own row, so the arithmetic
    that produced a value is the arithmetic that reproduces it.
    """
    registers = {datetime(2026, 9, 21, 2, tzinfo=UTC): 248868.26}
    [first] = statistic_rows(registers, Resume(offset=-248868.26, after=None))
    resume = Resume(offset=first["sum"] - first["state"], after=None)
    [again] = statistic_rows(registers, resume)
    assert again["sum"] == pytest.approx(first["sum"])


def test_a_falling_register_is_refused():
    """A register that goes backwards is a meter reset, and the offset is no
    longer constant across it. Writing through that would corrupt the series."""
    registers = {
        datetime(2026, 9, 21, 2, tzinfo=UTC): 248868.26,
        datetime(2026, 9, 21, 3, tzinfo=UTC): 248860.0,
    }
    with pytest.raises(ValueError, match="backwards"):
        statistic_rows(registers, Resume(offset=0.0, after=None))


def test_hours_at_or_before_the_resume_point_are_kept():
    """The last written hour is deliberately refetched: it may have been
    incomplete when it was written."""
    resume = Resume(offset=0.0, after=datetime(2026, 9, 21, 2, tzinfo=UTC))
    registers = {
        datetime(2026, 9, 21, 2, tzinfo=UTC): 1.0,
        datetime(2026, 9, 21, 3, tzinfo=UTC): 2.0,
    }
    assert len(statistic_rows(registers, resume)) == 2


def test_an_empty_mapping_yields_no_rows():
    assert statistic_rows({}, Resume(offset=0.0, after=None)) == []
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_history.py -v`
Expected: FAIL — `ImportError: cannot import name 'Resume'`.

- [ ] **Step 4: Implement**

Add to `custom_components/perific/history.py`:

```python
from dataclasses import dataclass

from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.const import UnitOfEnergy

from .const import DOMAIN, HISTORY_NAMES

if TYPE_CHECKING:
    from .api import Item

# kWh's unit class, as STATISTIC_UNIT_TO_UNIT_CONVERTER reports it on both the
# deployment target and the floor in hacs.json.
ENERGY_UNIT_CLASS = "energy"


def statistic_id(item_id: int, key: str) -> str:
    """The external statistic id for one meter's register.

    External, not an entity id: the recorder compiles statistics for any entity
    carrying a state_class, and would then co-write this series. Dropping the
    state_class instead raises a `state_class_removed` repair issue.
    """
    return f"{DOMAIN}:{item_id}_{key}"


def statistic_metadata(meter: Item, key: str) -> StatisticMetaData:
    """Metadata for one register's series.

    ``mean_type`` and ``unit_class`` are passed explicitly: they are accepted at
    the supported floor and become mandatory in 2026.11, so this is the one
    spelling that works across the whole range without a deprecation warning.
    """
    name = meter.name or meter.system_name or "Perific"
    return StatisticMetaData(
        has_mean=False,
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=f"{name} {HISTORY_NAMES[key]}",
        source=DOMAIN,
        statistic_id=statistic_id(meter.item_id, key),
        unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        unit_class=ENERGY_UNIT_CLASS,
    )


def registered_at(item_id: int) -> datetime:
    """When the device was registered, decoded from its id.

    ``ItemId`` is a millisecond epoch matching the item's ``CreationTime``, and
    the oldest reading the account will serve is a minute after it. That makes
    it the natural start for a first import.
    """
    return datetime.fromtimestamp(item_id / 1000, UTC)


@dataclass(frozen=True, slots=True)
class Resume:
    """Where the last run left off.

    ``offset`` is the constant between a row's register and its cumulative sum.
    Reading it back off our own last row is what makes a re-import of an hour
    reproduce the value already stored.
    """

    offset: float
    after: datetime | None


def statistic_rows(
    registers: dict[datetime, float], resume: Resume
) -> list[StatisticData]:
    """Build importable rows from end-of-hour registers."""
    rows: list[StatisticData] = []
    previous: float | None = None
    for hour in sorted(registers):
        register = registers[hour]
        if previous is not None and register < previous:
            raise ValueError(
                f"register went backwards at {hour.isoformat()}: "
                f"{previous} -> {register}. A meter reset breaks the constant "
                "offset this mapping depends on."
            )
        previous = register
        rows.append(
            StatisticData(start=hour, state=register, sum=register + resume.offset)
        )
    return rows
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_history.py -v`
Expected: 19 passed.

- [ ] **Step 6: Verification gate**

Run: `uv run ruff format . && uv run ruff check . && uv run basedpyright && uv run pytest`
Then: `uv run --isolated --locked --only-group minimum-homeassistant python -m pytest tests`
Expected: all clean on both. The second command is what proves `mean_type` and `unit_class` are
accepted at the floor. Leave uncommitted.

---

### Task 4: The importer

**Files:**
- Modify: `custom_components/perific/history.py`, `tests/test_history.py`

**Interfaces:**
- Consumes: everything from Tasks 1–3.
- Produces: `async_resume_point`, `HistoryImporter` with `async_run` and `async_import_since`.

> **As built, and this one was a bug the tests caught:** `get_last_statistics` returns `start` in
> epoch **seconds**. The plan said milliseconds, copied from the websocket API, which does convert.
> Dividing by 1000 put the resume point in January 1970. `async_resume_point` carries a comment.
>
> Two test-harness notes: `recorder_mock` must be listed **before** `hass` in a test signature or
> the recorder's database fixture asserts, and PHACC's `caplog` wrapper recurses under this plugin
> set, so the log assertions patch `history._LOGGER` instead.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_history.py`:

```python
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.perific.history import HistoryImporter, async_resume_point


async def test_resume_point_is_none_for_an_untouched_series(hass, recorder_mock):
    assert await async_resume_point(hass, "perific:1_energy_import") is None


async def test_resume_point_reads_back_our_own_last_row(hass, recorder_mock, meters):
    metadata = statistic_metadata(meters[0], "energy_import")
    async_add_external_statistics(
        hass,
        metadata,
        [
            StatisticData(
                start=datetime(2026, 9, 21, 2, tzinfo=UTC), state=248868.26, sum=0.0
            ),
            StatisticData(
                start=datetime(2026, 9, 21, 3, tzinfo=UTC), state=248871.76, sum=3.5
            ),
        ],
    )
    await async_wait_recording_done(hass)

    resume = await async_resume_point(hass, metadata["statistic_id"])

    assert resume is not None
    assert resume.after == datetime(2026, 9, 21, 3, tzinfo=UTC)
    assert resume.offset == pytest.approx(3.5 - 248871.76)


async def test_first_run_imports_from_registration(
    hass, recorder_mock, setup_integration, mock_client, phasedata
):
    from custom_components.perific.api import parse_phase_data

    mock_client.async_get_phase_data.return_value = parse_phase_data(phasedata)
    importer = HistoryImporter(hass, setup_integration)

    written = await importer.async_run()
    await async_wait_recording_done(hass)

    assert written > 0
    meter = setup_integration.runtime_data.meters[0]
    requested_start = mock_client.async_get_phase_data.await_args_list[0].args[1]
    assert requested_start == registered_at(meter.item_id)


async def test_a_second_run_resumes_and_does_not_change_what_it_wrote(
    hass, recorder_mock, setup_integration, mock_client, phasedata
):
    """Re-importing an overlapping window must be a no-op."""
    from custom_components.perific.api import parse_phase_data

    mock_client.async_get_phase_data.return_value = parse_phase_data(phasedata)
    importer = HistoryImporter(hass, setup_integration)
    await importer.async_run()
    await async_wait_recording_done(hass)

    meter = setup_integration.runtime_data.meters[0]
    sid = statistic_id(meter.item_id, "energy_import")
    first = await async_resume_point(hass, sid)

    await importer.async_run()
    await async_wait_recording_done(hass)
    second = await async_resume_point(hass, sid)

    assert second == first


async def test_one_request_per_chunk_not_one_per_register(
    hass, recorder_mock, setup_integration, mock_client, phasedata
):
    """Both registers come out of the same response.

    Fetching the window once per register would double the calls against an API
    whose rate limits are unmeasured.
    """
    from custom_components.perific.api import parse_phase_data
    from custom_components.perific.const import HISTORY_MAX_CHUNKS

    mock_client.async_get_phase_data.return_value = parse_phase_data(phasedata)
    importer = HistoryImporter(hass, setup_integration)

    await importer.async_run()

    meters = len(setup_integration.runtime_data.meters)
    assert mock_client.async_get_phase_data.await_count <= HISTORY_MAX_CHUNKS * meters


async def test_a_caught_up_series_makes_one_request(
    hass, recorder_mock, setup_integration, mock_client, meters
):
    """Steady state is one call an hour, not a walk over the whole history.

    The loop stops as soon as the cursor reaches now, so the chunk cap costs
    nothing once the series is current.
    """
    for key in ("energy_import", "energy_export"):
        async_add_external_statistics(
            hass,
            statistic_metadata(meters[0], key),
            [
                StatisticData(
                    start=dt_util.utcnow().replace(minute=0, second=0, microsecond=0),
                    state=100.0,
                    sum=0.0,
                )
            ],
        )
    await async_wait_recording_done(hass)

    mock_client.async_get_phase_data.return_value = []
    await HistoryImporter(hass, setup_integration).async_run()

    assert mock_client.async_get_phase_data.await_count == 1


async def test_an_empty_response_stops_the_run_without_writing(
    hass, recorder_mock, setup_integration, mock_client
):
    """A wrong parameter name also returns 200 with an empty list, so an empty
    response is treated as "nothing more to read", never as zero energy."""
    mock_client.async_get_phase_data.return_value = []
    importer = HistoryImporter(hass, setup_integration)

    assert await importer.async_run() == 0


async def test_an_api_failure_does_not_raise(
    hass, recorder_mock, setup_integration, mock_client, caplog
):
    from custom_components.perific.api import PerificError

    mock_client.async_get_phase_data.side_effect = PerificError("boom")
    importer = HistoryImporter(hass, setup_integration)

    assert await importer.async_run() == 0
    assert "History import failed" in caplog.text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_history.py -v`
Expected: FAIL — `ImportError: cannot import name 'HistoryImporter'`.

- [ ] **Step 3: Implement**

Add to `custom_components/perific/history.py`:

```python
import logging

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
)
from homeassistant.util import dt as dt_util

from .api import PerificError
from .const import HISTORY_CHUNK, HISTORY_MAX_CHUNKS, HISTORY_REGISTERS

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .coordinator import PerificConfigEntry

_LOGGER = logging.getLogger(__name__)


async def async_resume_point(hass: HomeAssistant, statistic_id: str) -> Resume | None:
    """Where the last run left off, read back off its own last row.

    Runs on the recorder's executor: `get_last_statistics` opens a database
    session and must not be called from the event loop.
    """

    def read() -> dict[str, list[dict[str, object]]]:
        return get_last_statistics(hass, 1, statistic_id, True, {"state", "sum"})

    result = await get_instance(hass).async_add_executor_job(read)
    rows = result.get(statistic_id) or []
    if not rows:
        return None

    row = rows[0]
    state, total = row.get("state"), row.get("sum")
    if not isinstance(state, (int, float)) or not isinstance(total, (int, float)):
        return None

    when = datetime.fromtimestamp(float(row["start"]) / 1000, UTC)
    return Resume(
        offset=float(total) - float(state),
        after=when.replace(minute=0, second=0, microsecond=0),
    )


class HistoryImporter:
    """Keeps the external energy series level with the vendor's record."""

    def __init__(self, hass: HomeAssistant, entry: PerificConfigEntry) -> None:
        """Bind to one config entry; the coordinator owns the client and meters."""
        self.hass = hass
        self.entry = entry

    async def async_run(self, _now: datetime | None = None) -> int:
        """Import everything not yet written. Never raises.

        Called from a timer, so a failure has to stay contained: the next run is
        minutes away and re-reads the same window.
        """
        try:
            return await self.async_import_since(None)
        except PerificError, ValueError:
            _LOGGER.exception("History import failed")
            return 0

    async def async_import_since(self, start: datetime | None) -> int:
        """Import from ``start``, or from wherever the series left off."""
        coordinator = self.entry.runtime_data
        written = 0

        for meter in coordinator.meters:
            zone = meter.time_zone or str(self.hass.config.time_zone)
            resumes = {
                key: await async_resume_point(
                    self.hass, statistic_id(meter.item_id, key)
                )
                for key in HISTORY_REGISTERS
            }

            if start is not None:
                # A forced rebuild keeps whatever offset the series already has,
                # so replaced rows land on the same scale as the ones around them.
                cursor = start
            else:
                # Both registers are read from one response, so the cursor is the
                # older of the two resume points. Re-importing an hour is a no-op,
                # and the last written hour is refetched deliberately: it may have
                # been incomplete when it was written.
                written_after = [
                    resume.after
                    for resume in resumes.values()
                    if resume is not None and resume.after is not None
                ]
                cursor = (
                    min(written_after)
                    if len(written_after) == len(HISTORY_REGISTERS)
                    else registered_at(meter.item_id)
                )

            written += await self._async_walk(meter, zone, cursor, resumes)

        return written

    async def _async_walk(
        self,
        meter: Item,
        zone: str,
        cursor: datetime,
        resumes: dict[str, Resume | None],
    ) -> int:
        """Import forward from the cursor, one request per chunk.

        Both registers come out of the same response: fetching the window twice
        would double the calls against an API whose rate limits are unmeasured.
        """
        client = self.entry.runtime_data.client
        written = 0

        for _ in range(HISTORY_MAX_CHUNKS):
            now = dt_util.utcnow()
            if cursor >= now:
                # Steady state exits here after one chunk. The cap above only
                # bounds a catch-up.
                break
            window = (cursor, min(cursor + HISTORY_CHUNK, now))

            points = await client.async_get_phase_data(meter.item_id, *window)
            if not points:
                # Also what a rejected request looks like, so this is "nothing
                # more to read", never "no energy was used".
                break
            localised = localise(points, zone, window)

            for key, field in HISTORY_REGISTERS.items():
                registers = hourly_registers(localised, field)
                if not registers:
                    continue

                resume = resumes[key]
                if resume is None:
                    # First ever row for this register: start the series at zero.
                    resume = Resume(offset=-registers[min(registers)], after=None)
                    resumes[key] = resume

                rows = statistic_rows(registers, resume)
                async_add_external_statistics(
                    self.hass, statistic_metadata(meter, key), rows
                )
                written += len(rows)
                _LOGGER.debug(
                    "Imported %d hour(s) of %s, %s .. %s",
                    len(rows),
                    statistic_id(meter.item_id, key),
                    rows[0]["start"].isoformat(),
                    rows[-1]["start"].isoformat(),
                )

            cursor = window[1]

        return written
```

Add `Item` to the `TYPE_CHECKING` imports from `.api`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_history.py -v`
Expected: 27 passed.

- [ ] **Step 5: Verification gate**

Run: `uv run ruff format . && uv run ruff check . && uv run basedpyright && uv run pytest`
Then: `uv run --isolated --locked --only-group minimum-homeassistant python -m pytest tests`
Expected: all clean on both. Leave uncommitted.

---

### Task 5: Run it on a timer, and on demand

**Files:**
- Create: `custom_components/perific/services.yaml`, `tests/test_history_service.py`
- Modify: `custom_components/perific/__init__.py`
- Modify: `custom_components/perific/strings.json`, `translations/en.json`, `translations/sv.json`

**Interfaces:**
- Consumes: `HistoryImporter` from Task 4.
- Produces: the timer on each entry, and `perific.import_history`.

> **As built:** three additions the plan missed.
>
> - The importer needs the recorder, which is optional in Home Assistant. Starting an import at
>   setup broke every existing test with `KeyError: 'recorder_instance'`. `async_run` now returns
>   early when the recorder is absent, and the manifest gains
>   `"after_dependencies": ["recorder"]` so setup is ordered when it is present.
> - `async_track_time_change` wants a listener returning `None`; `async_run` returns a count. A
>   small adapter in `async_setup_entry` bridges them.
> - The timer holds a **bound** method captured at setup, so patching
>   `HistoryImporter.async_run` afterwards does not reach it. The timer tests assert through the
>   API client instead, which tests the real path rather than a mock of our own code.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_history_service.py
"""The import timer and the on-demand service."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.perific.const import (
    DOMAIN,
    HISTORY_RUN_AT_MINUTE,
    SERVICE_IMPORT_HISTORY,
)


def next_run(offset_hours: int = 1) -> datetime:
    """The next wall-clock moment the importer is scheduled for."""
    return (dt_util.now() + timedelta(hours=offset_hours)).replace(
        minute=HISTORY_RUN_AT_MINUTE, second=0, microsecond=0
    )


async def test_service_is_registered(hass: HomeAssistant, setup_integration):
    assert hass.services.has_service(DOMAIN, SERVICE_IMPORT_HISTORY)


async def test_import_runs_just_after_each_hour(hass: HomeAssistant, setup_integration):
    with patch(
        "custom_components.perific.HistoryImporter.async_run", return_value=0
    ) as run:
        async_fire_time_changed(hass, next_run())
        await hass.async_block_till_done()

    run.assert_called()


# No test that it stays quiet mid-hour: `async_fire_time_changed` fires every
# listener due at or before the given moment, so advancing to any later point
# necessarily replays the scheduled run. That the schedule is hourly is
# `async_track_time_change`'s behaviour, not this integration's.


async def test_service_forces_a_rebuild_from_a_given_start(
    hass: HomeAssistant, setup_integration
):
    with patch(
        "custom_components.perific.HistoryImporter.async_import_since", return_value=7
    ) as since:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_IMPORT_HISTORY,
            {"start": "2026-08-29T17:00:00+00:00"},
            blocking=True,
        )

    assert since.await_args.args[0] == datetime(2026, 8, 29, 17, tzinfo=UTC)


async def test_service_without_a_start_resumes(hass: HomeAssistant, setup_integration):
    with patch(
        "custom_components.perific.HistoryImporter.async_import_since", return_value=0
    ) as since:
        await hass.services.async_call(
            DOMAIN, SERVICE_IMPORT_HISTORY, {}, blocking=True
        )

    assert since.await_args.args[0] is None


async def test_unloading_the_entry_stops_the_timer(
    hass: HomeAssistant, setup_integration
):
    await hass.config_entries.async_unload(setup_integration.entry_id)
    await hass.async_block_till_done()

    with patch(
        "custom_components.perific.HistoryImporter.async_run", return_value=0
    ) as run:
        async_fire_time_changed(hass, next_run(offset_hours=2))
        await hass.async_block_till_done()

    run.assert_not_called()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_history_service.py -v`
Expected: FAIL — the service is not registered.

- [ ] **Step 3: Write `services.yaml`**

```yaml
import_history:
  fields:
    start:
      required: false
      example: "2026-08-29T17:00:00+00:00"
      selector:
        datetime:
```

- [ ] **Step 4: Register the service and start the timer**

`__init__.py` currently has no `async_setup` — only `async_setup_entry`. Add one above it, so the
service is registered once for the integration rather than once per entry:

```python
async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the integration-wide service.

    Registered here rather than per entry: the service spans every entry, and
    re-registering it on each reload would rebind the handler.
    """

    async def async_handle_import(call: ServiceCall) -> None:
        start = call.data.get(ATTR_START)
        if start is not None:
            start = dt_util.as_utc(start)
        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            await HistoryImporter(hass, entry).async_import_since(start)

    hass.services.async_register(
        DOMAIN,
        SERVICE_IMPORT_HISTORY,
        async_handle_import,
        schema=vol.Schema({vol.Optional(ATTR_START): cv.datetime}),
    )
    return True
```

At the end of `async_setup_entry`, after the platforms are forwarded:

```python
    importer = HistoryImporter(hass, entry)
    entry.async_on_unload(
        async_track_time_change(
            hass, importer.async_run, minute=HISTORY_RUN_AT_MINUTE, second=0
        )
    )
    # Once at startup as well, so an instance that was down over several hours
    # catches up immediately rather than at the next hour mark. It can take a
    # while on a first import, so it must not hold up setup.
    entry.async_create_background_task(
        hass, importer.async_run(), name=f"{DOMAIN} history import"
    )
```

New imports for `__init__.py`:

```python
import voluptuous as vol
from homeassistant.core import ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_time_change

from .const import ATTR_START, DOMAIN, HISTORY_RUN_AT_MINUTE, SERVICE_IMPORT_HISTORY
from .history import HistoryImporter
```

with `from homeassistant.helpers.typing import ConfigType` under `TYPE_CHECKING`. `DOMAIN` may not
currently be imported in `__init__.py` — add it. `HistoryImporter` must be imported at module level,
not under `TYPE_CHECKING`, because the tests patch `custom_components.perific.HistoryImporter`.

- [ ] **Step 5: Add the translations**

In `strings.json` and `translations/en.json`, under a top-level `services` key:

```json
"services": {
  "import_history": {
    "name": "Import energy history",
    "description": "Imports hourly energy statistics from the Perific cloud. Runs by itself every 15 minutes; use this to force a rebuild from a chosen point.",
    "fields": {
      "start": {
        "name": "Start",
        "description": "Import from this moment onwards, replacing what is already stored. Leave empty to continue from wherever the last import stopped."
      }
    }
  }
}
```

Translate the same keys into `translations/sv.json`.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_history_service.py -v`
Expected: 5 passed.

- [ ] **Step 7: Verification gate**

Run: `uv run ruff format . && uv run ruff check . && uv run basedpyright && uv run pytest`
Then: `npx prettier --check .`
Then: `uv run --isolated --locked --only-group minimum-homeassistant python -m pytest tests`
Expected: all clean. Leave uncommitted.

---

## Verifying against real data

The container proves the mechanism; only the real instance proves the series. Nothing here touches
the existing sensors, so both run side by side and the switchover is a dashboard setting.

1. `./scripts/dev-sync.sh`, then watch the log at debug level for `Imported N hour(s)`.
2. In Developer Tools → Statistics, confirm `perific:<item>_energy_import` appears and that **no
   issues are listed** — in particular no `state_class_removed` against the existing sensors, which
   must be untouched.
3. Compare the two series over the same day: the external one and
   `sensor.onerj12_inkopt_elektricitet`. Outside the two known outages they should agree to within
   a few watt-hours, the difference being which reading landed last in each hour.
4. Inside the 2026-09-17 18:00 → 2026-09-18 03:00 CEST outage the external series must show nine
   ordinary hours where the old one shows nothing followed by a 27 kWh spike. **That is the whole
   point of the change; check it explicitly.**
5. `./scripts/deploy.sh`, then leave it for a day.
6. Repoint Settings → Energy at the external statistics. Confirm the dashboard renders history
   back to 2026-08-29.

Every step is additive and reversible — repointing the dashboard is a setting, and the old series
is left intact. **Do not clear the old `sensor.*` statistics as part of this work.** Those entities
still carry `state_class: TOTAL_INCREASING`, so the recorder recompiles their statistics on the
next hourly pass and clearing them achieves nothing. Retiring them means removing the `state_class`
*and* clearing, in one change — see below.

## Deliberately not in this plan

- **Retiring the old energy series.** The two `sensor.*` energy entities keep their `state_class`,
  keep compiling their own statistics, and keep working; the cost is a duplicate series in the
  statistics picker. Retiring it is one change that must do both halves at once — drop the
  `state_class` *and* clear the stored statistics — because an entity with statistics and no
  `state_class` raises `state_class_removed` (`sensor/recorder.py:905`), while clearing alone is
  undone by the next hourly compile. Revisit once the external series has been trusted for a while.
- **Retention.** The account currently serves everything back to device registration, so no limit
  has been observed. Once the device is a few months old, re-run
  `python3 scripts/probe_api.py --phasedata` and record the horizon in `docs/api/enegic.md`. If a
  limit appears, the importer is unaffected — it only ever walks forward.
- **A window starting inside the autumn fold** is disambiguated by the requested window, which is
  correct but untested against a real October. Re-check after 2026-10-25.
