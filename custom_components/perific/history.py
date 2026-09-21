"""Importing the energy series from the vendor's own record.

Energy is not accumulated from polling here. ``/getphasedata`` holds the
cumulative registers at minute resolution, so the hourly series is a copy of
that record and does not depend on Home Assistant having been awake. The
reasoning is in ``docs/plans/2026-09-21-energy-history-import.md``; the endpoint
is in ``docs/api/enegic.md``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from homeassistant.components.recorder.const import DOMAIN as RECORDER_DOMAIN
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.recorder import get_instance
from homeassistant.util import dt as dt_util

from .api import PerificError
from .const import (
    DOMAIN,
    HISTORY_CHUNK,
    HISTORY_MAX_CHUNKS,
    HISTORY_MIN_WINDOW,
    HISTORY_NAMES,
    HISTORY_REGISTERS,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .api import Item, PhaseData, PhasePoint
    from .coordinator import PerificConfigEntry

_LOGGER = logging.getLogger(__name__)

HOUR = timedelta(hours=1)

# kWh's unit class, as STATISTIC_UNIT_TO_UNIT_CONVERTER reports it on both the
# deployment target and the floor in hacs.json.
ENERGY_UNIT_CLASS = "energy"


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
    """Reduce minute points to the register as it stood at the end of each hour.

    Home Assistant's own hourly row records the last state within the hour, so
    the last point in the hour is the value to match.
    """
    registers: dict[datetime, float] = {}
    for when, data in sorted(localised, key=lambda pair: pair[0]):
        value = getattr(data, register, None)
        if not isinstance(value, (int, float)):
            continue
        registers[when.replace(minute=0, second=0, microsecond=0)] = float(value)
    return registers


def statistic_id(item_id: int, key: str) -> str:
    """Build the external statistic id for one meter's register.

    External, not an entity id: the recorder compiles statistics for any entity
    carrying a state_class and would then co-write this series, while dropping
    the state_class instead raises a `state_class_removed` repair issue.
    """
    return f"{DOMAIN}:{item_id}_{key}"


def register_name(hass: HomeAssistant, meter: Item, key: str) -> str:
    """Name the register, translated where that is possible.

    An external statistic has no entity, so Home Assistant shows the plain
    string stored beside it and offers no way to translate that. Borrowing the
    matching sensor's already-resolved name is what keeps the two from sitting
    in different languages in the same picker. Metadata is rewritten on every
    import, so a rename or a language change is picked up within the hour.

    Falls back to English once those sensors are retired, which is the point at
    which nothing translated is left to borrow.
    """
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{meter.item_id}_{key}")
    entry = registry.async_get(entity_id) if entity_id else None
    if entry and (name := entry.name or entry.original_name):
        return str(name)
    return HISTORY_NAMES[key]


def statistic_metadata(hass: HomeAssistant, meter: Item, key: str) -> StatisticMetaData:
    """Describe one register's series to the recorder.

    ``mean_type`` and ``unit_class`` are passed explicitly: they are accepted at
    the supported floor and become mandatory in 2026.11, so this is the one
    spelling that works across the whole range without a deprecation warning.

    The deprecated ``has_mean`` is deliberately absent. It is only consulted
    when ``mean_type`` is missing, and the column it used to fill is derived
    from ``mean_type`` now.
    """
    device = meter.name or meter.system_name or "Perific"
    return StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=f"{device} {register_name(hass, meter, key)}",
        source=DOMAIN,
        statistic_id=statistic_id(meter.item_id, key),
        unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        unit_class=ENERGY_UNIT_CLASS,
    )


def registered_at(item_id: int) -> datetime:
    """Decode when the device was registered from its id.

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


async def async_resume_point(hass: HomeAssistant, statistic_id: str) -> Resume | None:
    """Read back where the last run stopped, off its own last row.

    Runs on the recorder's executor: ``get_last_statistics`` opens a database
    session and must not be called from the event loop.
    """

    def read() -> Any:
        return get_last_statistics(
            hass, 1, statistic_id, convert_units=True, types={"state", "sum"}
        )

    result = await get_instance(hass).async_add_executor_job(read)
    rows = result.get(statistic_id) or []
    if not rows:
        return None

    row = rows[0]
    state, total = row.get("state"), row.get("sum")
    if not isinstance(state, (int, float)) or not isinstance(total, (int, float)):
        return None

    # `start` is epoch seconds here. The websocket API hands the same field out
    # in milliseconds, which is the easy way to be an hour or fifty years out.
    when = datetime.fromtimestamp(float(row["start"]), UTC)
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

        Driven by a timer, so a failure has to stay contained: the next run is
        an hour away and re-reads the same window.
        """
        if RECORDER_DOMAIN not in self.hass.config.components:
            # The recorder is optional and can be left out of a configuration.
            # `after_dependencies` in the manifest only orders setup when it is
            # present, so this has to hold for the case where it never is.
            _LOGGER.debug("No recorder on this instance; nothing to import into")
            return 0
        try:
            return await self.async_import_since(None)
        except PerificError, ValueError:
            _LOGGER.exception("History import failed")
            return 0

    async def async_import_since(self, start: datetime | None) -> int:
        """Import from ``start``, or from wherever the series left off."""
        written = 0

        for meter in self.entry.runtime_data.meters:
            zone = meter.time_zone or str(self.hass.config.time_zone)
            resumes = {
                key: await async_resume_point(
                    self.hass, statistic_id(meter.item_id, key)
                )
                for key in HISTORY_REGISTERS
            }

            if start is not None:
                # A forced rebuild keeps whatever offset the series already has,
                # so replaced rows land on the same scale as those around them.
                cursor = start
            else:
                # Both registers are read from one response, so the cursor is the
                # older of the two resume points. Re-importing an hour is a no-op,
                # and the last written hour is refetched deliberately: it may have
                # been incomplete when it was written.
                already = [
                    resume.after
                    for resume in resumes.values()
                    if resume is not None and resume.after is not None
                ]
                cursor = (
                    min(already)
                    if len(already) == len(HISTORY_REGISTERS)
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

        # Fixed for the whole walk. Re-reading the clock each time round leaves
        # the last chunk ending a few hundred milliseconds before the next
        # reading of it, and the sub-second window that follows is sent as
        # startTime == endTime, which the API answers 400.
        now = dt_util.utcnow()

        for _ in range(HISTORY_MAX_CHUNKS):
            if now - cursor < HISTORY_MIN_WINDOW:
                # Steady state exits here after one chunk; the cap above only
                # bounds a catch-up. Points are a minute apart, so a shorter
                # window could not hold one anyway.
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
                    self.hass, statistic_metadata(self.hass, meter, key), rows
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
