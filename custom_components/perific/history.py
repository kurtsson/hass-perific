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
from itertools import pairwise
from typing import TYPE_CHECKING, Any, Final
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
    get_metadata,
    statistics_during_period,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.helpers.recorder import get_instance
from homeassistant.helpers.translation import async_get_translations
from homeassistant.util import dt as dt_util

from .api import PerificError
from .const import (
    CONF_ENERGY_TAX,
    CONF_EXPORT_PREMIUM,
    CONF_EXPORT_PRICE_ENTITY,
    CONF_PRICE_ENTITY,
    CONF_PRICE_MARKUP,
    CONF_SOLAR_STATISTIC,
    CONF_VAT_PERCENT,
    COST_KEYS,
    COST_NAMES,
    DOMAIN,
    HISTORY_CHUNK,
    HISTORY_MAX_CHUNKS,
    HISTORY_MIN_WINDOW,
    HISTORY_NAMES,
    HISTORY_REGISTERS,
    SOLAR_AVOIDED_COST,
    SOLAR_NAMES,
    SOLAR_REVENUE,
    SOLAR_SELF_CONSUMED,
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

# The fewest stored hours that can produce a cost: one to difference against.
_PAIR = 2

# Inverter integrations differ on scale; SolarEdge reports Wh.
_TO_KWH: Final = {"Wh": 0.001, "kWh": 1.0, "MWh": 1000.0}


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


async def async_register_names(hass: HomeAssistant) -> dict[str, str]:
    """Read the registers' display names, in the instance's own language.

    An external statistic has no entity, so Home Assistant shows the plain
    string stored beside it and offers no way to translate that. These names are
    therefore read straight out of the integration's own translation files, by
    the same keys the sensors would have used. Metadata is rewritten on every
    import, so a change of language is picked up within the hour.
    """
    translations = await async_get_translations(
        hass, hass.config.language, "entity", {DOMAIN}
    )
    return {
        key: translations.get(f"component.{DOMAIN}.entity.sensor.{key}.name") or default
        for key, default in (HISTORY_NAMES | COST_NAMES | SOLAR_NAMES).items()
    }


def statistic_metadata(meter: Item, key: str, name: str) -> StatisticMetaData:
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
        name=f"{device} {name}",
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
    """Where the last run left off, as the last written row states it.

    Both halves are kept rather than just their difference: the energy series
    needs the ``offset``, and the cost series needs ``total`` to carry on
    accumulating from.
    """

    state: float
    total: float
    after: datetime | None

    @property
    def offset(self) -> float:
        """The constant between a row's register and its cumulative sum.

        Reading it back off our own last row is what makes a re-import of an
        hour reproduce the value already stored.
        """
        return self.total - self.state


@dataclass(frozen=True, slots=True)
class Tariff:
    """What one kWh actually costs on top of the spot price.

    Spot alone is not what anyone pays. The Swedish shape is spot plus the
    supplier's markup plus energy tax, with VAT applied to the sum; the same
    three terms describe most metered tariffs. Selling is the same arithmetic
    with the tax and VAT left at zero, since a household exporting surplus
    charges neither.
    """

    markup: float = 0.0
    tax: float = 0.0
    vat: float = 0.0

    def price(self, spot: float) -> float:
        """Deliver the price of one kWh at this spot price."""
        return (spot + self.markup + self.tax) * (1.0 + self.vat)


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


def cost_statistic_id(item_id: int, key: str) -> str:
    """Build the external statistic id carrying one register's cost."""
    return f"{DOMAIN}:{item_id}_{COST_KEYS[key]}"


def cost_metadata(meter: Item, key: str, name: str, currency: str) -> StatisticMetaData:
    """Describe one register's cost series.

    ``unit_class`` is None: money has no unit converter, which is also how Home
    Assistant's own cost statistics are stored.
    """
    device = meter.name or meter.system_name or "Perific"
    return StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=f"{device} {name}",
        source=DOMAIN,
        statistic_id=cost_statistic_id(meter.item_id, key),
        unit_of_measurement=currency,
        unit_class=None,
    )


def solar_metadata(
    meter: Item, key: str, name: str, unit: str | None
) -> StatisticMetaData:
    """Describe one of the solar economy series.

    ``unit`` carries the currency for the money series and None for the energy
    one, which takes kWh and the energy unit class so the recorder can convert
    it like any other energy statistic.
    """
    device = meter.name or meter.system_name or "Perific"
    energy = unit is None
    return StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=f"{device} {name}",
        source=DOMAIN,
        statistic_id=statistic_id(meter.item_id, key),
        unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR if energy else unit,
        unit_class=ENERGY_UNIT_CLASS if energy else None,
    )


async def async_statistic_unit(hass: HomeAssistant, statistic_id: str) -> str | None:
    """Read the unit a statistic is stored in."""

    def read() -> Any:
        return get_metadata(hass, statistic_ids={statistic_id})

    result = await get_instance(hass).async_add_executor_job(read)
    found = result.get(statistic_id)
    return found[1].get("unit_of_measurement") if found else None


def hourly_deltas(totals: dict[datetime, float]) -> dict[datetime, float]:
    """Turn cumulative sums into what each hour itself contributed.

    The first hour produces nothing: it is the one the previous run already
    accounted for, or the start of the series, and either way what happened
    during it is unknown.
    """
    return {
        hour: totals[hour] - totals[previous]
        for previous, hour in pairwise(sorted(totals))
    }


def solar_rows(
    produced: dict[datetime, float],
    exported: dict[datetime, float],
    buying: dict[datetime, float],
    selling: dict[datetime, float],
    running: dict[str, float],
) -> dict[str, list[StatisticData]]:
    """Accumulate what the panels were worth, hour by hour.

    Self-consumption is what was produced and not exported. It is floored at
    zero because production and export are read from two different meters whose
    clocks and sampling differ: an hour can compute slightly negative from
    timing alone, and that is measurement skew rather than energy.

    Revenue recomputes the export half rather than reading the compensation
    series, so that it always equals its own two parts.

    ``buying`` and ``selling`` are already what a kWh is worth each hour, tariff
    applied. A negative one needs no special case: the hour's contribution goes
    negative and the running total falls, which is what paying to export
    actually does.
    """
    rows: dict[str, list[StatisticData]] = {key: [] for key in running}
    totals = dict(running)
    for hour in sorted(produced.keys() & exported.keys()):
        buy, sell = buying.get(hour), selling.get(hour)
        if buy is None or sell is None:
            continue
        used = max(produced[hour] - exported[hour], 0.0)
        totals[SOLAR_SELF_CONSUMED] += used
        totals[SOLAR_AVOIDED_COST] += used * buy
        totals[SOLAR_REVENUE] += used * buy + exported[hour] * sell
        for key, total in totals.items():
            rows[key].append(StatisticData(start=hour, state=total, sum=total))
    return rows


async def async_hourly_prices(
    hass: HomeAssistant, statistic_id: str, window: tuple[datetime, datetime]
) -> dict[datetime, float]:
    """Read the spot price for each hour in the window.

    Prices come from the entity's own recorded statistics, so this reaches only
    as far back as the recorder saw that sensor.

    A price is a measurement, so the recorder keeps a mean for it rather than a
    sum, and the mean over an hour is exactly the hour's price.
    """
    start, end = window

    def read() -> Any:
        return statistics_during_period(
            hass, start, end, {statistic_id}, "hour", None, {"mean"}
        )

    result = await get_instance(hass).async_add_executor_job(read)

    prices: dict[datetime, float] = {}
    for row in result.get(statistic_id) or []:
        mean = row.get("mean")
        if not isinstance(mean, (int, float)):
            continue
        when = datetime.fromtimestamp(float(row["start"]), UTC)
        prices[when.replace(minute=0, second=0, microsecond=0)] = float(mean)
    return prices


async def async_stored_hours(
    hass: HomeAssistant, statistic_id: str, window: tuple[datetime, datetime]
) -> dict[datetime, float]:
    """Read the cumulative sums already written for a statistic, by hour.

    Cost is worked out from what the energy series says rather than from the
    readings that produced it, so that it can walk at its own pace: an hour of
    energy fetched weeks ago is costed the same way as one fetched a minute ago.
    """
    start, end = window

    def read() -> Any:
        return statistics_during_period(
            hass, start, end, {statistic_id}, "hour", None, {"sum"}
        )

    result = await get_instance(hass).async_add_executor_job(read)

    hours: dict[datetime, float] = {}
    for row in result.get(statistic_id) or []:
        total = row.get("sum")
        if not isinstance(total, (int, float)):
            continue
        when = datetime.fromtimestamp(float(row["start"]), UTC)
        hours[when.replace(minute=0, second=0, microsecond=0)] = float(total)
    return hours


def cost_rows(
    registers: dict[datetime, float],
    prices: dict[datetime, float],
    tariff: Tariff,
    running: float,
) -> list[StatisticData]:
    """Accumulate the cost of each hour's consumption.

    An hour costs its own consumption, so it needs the register before it as
    well as its own — which is why the first hour of a batch produces nothing.
    That hour is either the one the previous run already costed, or the very
    first hour of the series, whose consumption is unknown either way.

    An hour with no recorded price is skipped rather than costed at zero. Its
    energy then goes uncosted, which undercounts; pricing it at nothing would
    do the same while looking deliberate.
    """
    rows: list[StatisticData] = []
    for previous, hour in pairwise(sorted(registers)):
        spot = prices.get(hour)
        if spot is None:
            continue
        running += (registers[hour] - registers[previous]) * tariff.price(spot)
        rows.append(StatisticData(start=hour, state=running, sum=running))
    return rows


async def async_resume_point(
    hass: HomeAssistant, statistic_id: str, *, rewind: int = 0
) -> Resume | None:
    """Read back where the last run stopped, off its own last row.

    ``rewind`` resumes that many rows earlier, so the caller rewrites hours it
    has already written. A cost is worked out from the register as it stood at
    the time, but an hour that is still running keeps growing afterwards — and
    the next hour is differenced against its *final* register, so whatever
    arrived late is never counted anywhere. Recomputing the last hour is what
    collects it, and is safe because rows are keyed on their start.

    Runs on the recorder's executor: ``get_last_statistics`` opens a database
    session and must not be called from the event loop.
    """

    def read() -> Any:
        return get_last_statistics(
            hass, rewind + 1, statistic_id, convert_units=True, types={"state", "sum"}
        )

    result = await get_instance(hass).async_add_executor_job(read)
    # Oldest first, so the one to resume from is the first: asking for one more
    # row than the rewind is what puts it there.
    rows = sorted(result.get(statistic_id) or [], key=lambda row: row["start"])
    if len(rows) <= rewind:
        # Too short to rewind into, so there is no earlier total to carry on
        # from. Starting the series again costs one pass over a series this
        # small, and leaves no hour stuck at a partial value.
        return None

    row = rows[0]
    state, total = row.get("state"), row.get("sum")
    if not isinstance(state, (int, float)) or not isinstance(total, (int, float)):
        return None

    # `start` is epoch seconds here. The websocket API hands the same field out
    # in milliseconds, which is the easy way to be an hour or fifty years out.
    when = datetime.fromtimestamp(float(row["start"]), UTC)
    return Resume(
        state=float(state),
        total=float(total),
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
        names = await async_register_names(self.hass)

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

            written += await self._async_walk(meter, zone, cursor, resumes, names)

            # After the energy, and from the stored series rather than from
            # this run's window: cost has its own starting point and is usually
            # further behind, because prices only reach back as far as the
            # recorder saw the price entity.
            for key in HISTORY_REGISTERS:
                written += await self._async_import_cost(meter, key, names)

            # After the cost: self-consumption is priced the same way, and
            # differencing it needs the export series this run just extended.
            written += await self._async_import_solar(meter, names)

        return written

    async def _async_walk(
        self,
        meter: Item,
        zone: str,
        cursor: datetime,
        resumes: dict[str, Resume | None],
        names: dict[str, str],
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
                    first = registers[min(registers)]
                    resume = Resume(state=first, total=0.0, after=None)
                    resumes[key] = resume

                rows = statistic_rows(registers, resume)
                async_add_external_statistics(
                    self.hass, statistic_metadata(meter, key, names[key]), rows
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

    def _tariff(self, key: str) -> tuple[str | None, Tariff]:
        """Resolve the price entity and tariff for one register.

        Export carries neither energy tax nor VAT: a household selling surplus
        charges neither, so only the contract's premium sits on top of spot.
        """
        options = self.entry.options
        if key == "energy_export":
            entity_id = options.get(CONF_EXPORT_PRICE_ENTITY) or options.get(
                CONF_PRICE_ENTITY
            )
            return entity_id, Tariff(
                markup=float(options.get(CONF_EXPORT_PREMIUM, 0.0))
            )
        return options.get(CONF_PRICE_ENTITY), Tariff(
            markup=float(options.get(CONF_PRICE_MARKUP, 0.0)),
            tax=float(options.get(CONF_ENERGY_TAX, 0.0)),
            vat=float(options.get(CONF_VAT_PERCENT, 0.0)) / 100.0,
        )

    async def _async_import_cost(
        self, meter: Item, key: str, names: dict[str, str]
    ) -> int:
        """Cost every stored hour not costed yet, if a price entity is set.

        Home Assistant will not compute this itself: `energy/data.py` rejects a
        price entity outright when the energy source is an external statistic,
        and directs you to `stat_cost` instead. This is what fills it.

        Driven by the stored energy series rather than by whatever this run
        fetched. Costing an hour needs the hour before it to difference
        against, and a steady-state fetch covers a single hour — so tying the
        two together would mean no cost was ever written, and none of the
        history already stored could be reached.
        """
        price_entity, tariff = self._tariff(key)
        if not price_entity:
            _LOGGER.debug("No price entity configured for %s; not costing it", key)
            return 0

        cost_id = cost_statistic_id(meter.item_id, key)
        resume = await async_resume_point(self.hass, cost_id, rewind=1)
        # Start *at* the last costed hour, not before it. That hour becomes the
        # predecessor the next one is differenced against, and is not itself
        # rewritten — `resume.total` already counts it, so re-costing it would
        # add the same hour twice on every run. From the epoch when there is
        # nothing yet, which walks the whole stored series once.
        start = resume.after if resume and resume.after else registered_at(0)
        window = (start, dt_util.utcnow())

        sums = await async_stored_hours(
            self.hass, statistic_id(meter.item_id, key), window
        )
        if len(sums) < _PAIR:
            # One hour cannot be differenced against anything.
            _LOGGER.debug(
                "Only %d stored hour(s) of %s since %s; nothing to cost yet",
                len(sums),
                key,
                start.isoformat(),
            )
            return 0

        prices = await async_hourly_prices(self.hass, price_entity, window)
        if not prices:
            # The recorder only holds prices from when it first saw the entity,
            # so energy older than that is genuinely uncostable.
            _LOGGER.debug(
                "No recorded prices from %s since %s; cannot cost %s",
                price_entity,
                start.isoformat(),
                key,
            )
            return 0

        rows = cost_rows(sums, prices, tariff, resume.total if resume else 0.0)
        if not rows:
            _LOGGER.debug(
                "%d stored hour(s) and %d price(s) for %s, but no hour has both",
                len(sums),
                len(prices),
                key,
            )
            return 0

        cost_key = COST_KEYS[key]
        async_add_external_statistics(
            self.hass,
            cost_metadata(
                meter, key, names[cost_key], self.hass.config.currency or "EUR"
            ),
            rows,
        )
        _LOGGER.debug(
            "Costed %d hour(s) of %s, %s .. %s",
            len(rows),
            cost_id,
            rows[0]["start"].isoformat(),
            rows[-1]["start"].isoformat(),
        )
        return len(rows)

    async def _async_import_solar(self, meter: Item, names: dict[str, str]) -> int:
        """Value the solar against the grid it displaced, if one is configured.

        What the panels are worth is mostly invisible to the meter: the surplus
        that gets exported crosses it and is already priced, but the far larger
        share consumed on site never reaches it at all. That half is production
        minus export, and it is worth whatever buying it would have cost.

        Production comes from another integration's statistic, so this reads it
        rather than measuring anything — the HAN port cannot see the panels.
        """
        options = self.entry.options
        solar_id = options.get(CONF_SOLAR_STATISTIC)
        price_entity = options.get(CONF_PRICE_ENTITY)
        if not solar_id or not price_entity:
            _LOGGER.debug("No solar statistic or no price entity; not valuing solar")
            return 0

        unit = await async_statistic_unit(self.hass, solar_id)
        factor = _TO_KWH.get(unit or "")
        if factor is None:
            _LOGGER.warning(
                "Solar statistic %s is in %r, which is not an energy unit this "
                "can convert; expected one of %s",
                solar_id,
                unit,
                ", ".join(_TO_KWH),
            )
            return 0

        resume = await async_resume_point(
            self.hass, statistic_id(meter.item_id, SOLAR_REVENUE), rewind=1
        )
        start = resume.after if resume and resume.after else registered_at(0)
        window = (start, dt_util.utcnow())

        produced = await async_stored_hours(self.hass, solar_id, window)
        exported = await async_stored_hours(
            self.hass, statistic_id(meter.item_id, "energy_export"), window
        )
        if len(produced) < _PAIR or len(exported) < _PAIR:
            _LOGGER.debug(
                "Solar has %d stored hour(s) and export %d since %s; nothing to value",
                len(produced),
                len(exported),
                start.isoformat(),
            )
            return 0

        prices = await async_hourly_prices(self.hass, price_entity, window)
        export_entity = options.get(CONF_EXPORT_PRICE_ENTITY)
        selling_prices = (
            await async_hourly_prices(self.hass, export_entity, window)
            if export_entity
            else prices
        )
        if not prices:
            _LOGGER.debug("No recorded prices since %s; cannot value solar", start)
            return 0

        running: dict[str, float] = dict.fromkeys(
            (SOLAR_SELF_CONSUMED, SOLAR_AVOIDED_COST, SOLAR_REVENUE), 0.0
        )
        for key in running:
            point = await async_resume_point(
                self.hass, statistic_id(meter.item_id, key), rewind=1
            )
            running[key] = point.total if point else 0.0

        buying_tariff = self._tariff("energy_import")[1]
        selling_tariff = self._tariff("energy_export")[1]
        rows = solar_rows(
            {hour: kwh * factor for hour, kwh in hourly_deltas(produced).items()},
            hourly_deltas(exported),
            {hour: buying_tariff.price(spot) for hour, spot in prices.items()},
            {hour: selling_tariff.price(spot) for hour, spot in selling_prices.items()},
            running,
        )
        if not rows[SOLAR_REVENUE]:
            _LOGGER.debug(
                "No hour has solar, export and a price together: %d solar hour(s) "
                "since %s, %d export, %d price(s), %d in common",
                len(produced),
                start.isoformat(),
                len(exported),
                len(prices),
                len(hourly_deltas(produced).keys() & hourly_deltas(exported).keys()),
            )
            return 0

        currency = self.hass.config.currency or "EUR"
        for key, series in rows.items():
            async_add_external_statistics(
                self.hass,
                solar_metadata(
                    meter,
                    key,
                    names[key],
                    None if key == SOLAR_SELF_CONSUMED else currency,
                ),
                series,
            )
        _LOGGER.debug(
            "Valued %d hour(s) of solar, %s .. %s",
            len(rows[SOLAR_REVENUE]),
            rows[SOLAR_REVENUE][0]["start"].isoformat(),
            rows[SOLAR_REVENUE][-1]["start"].isoformat(),
        )
        return len(rows[SOLAR_REVENUE])
