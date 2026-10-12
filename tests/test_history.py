"""Turning ``/getphasedata`` into statistics rows.

The conversion is the risky part: the endpoint labels points in the item's own
timezone, Home Assistant keys statistics on UTC hours, and an hour's error is
invisible once written. Every expected value here was checked against real
``zoneinfo``, including that 2026-03-29 and 2026-10-25 are the EU transition
dates.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.const import CONF_TOKEN
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.perific import history
from custom_components.perific.api import (
    PerificAuthError,
    PerificError,
    PhaseData,
    PhasePoint,
    parse_phase_data,
)
from custom_components.perific.const import (
    CONF_ENERGY_TAX,
    CONF_PRICE_ENTITY,
    CONF_PRICE_MARKUP,
    CONF_SOLAR_STATISTIC,
    CONF_VAT_PERCENT,
    COST_NAMES,
    DOMAIN,
    HISTORY_NAMES,
    HISTORY_REGISTERS,
    SOLAR_AVOIDED_COST,
    SOLAR_NAMES,
    SOLAR_REVENUE,
    SOLAR_SELF_CONSUMED,
)
from custom_components.perific.history import (
    MAX_KWH_PER_HOUR,
    HistoryImporter,
    Imported,
    Reading,
    Resume,
    Tariff,
    async_register_names,
    async_resume_point,
    async_stored_hours,
    classify_reading,
    cost_rows,
    cost_statistic_id,
    first_resume,
    hourly_deltas,
    hourly_registers,
    localise,
    registered_at,
    solar_rows,
    start_of_hour,
    statistic_id,
    statistic_metadata,
    statistic_rows,
)

from .conftest import RENEWED_TOKEN_INFO

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from unittest.mock import AsyncMock

    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.perific.api import Item

STOCKHOLM = "Europe/Stockholm"


def H(hour: int) -> datetime:  # noqa: N802
    """An hour counted from midnight on the capture's day, in UTC."""
    return datetime(2026, 9, 21, tzinfo=UTC) + timedelta(hours=hour)


def point(naive: str, imported: float = 1.0) -> PhasePoint:
    """A point labelled in wall-clock time, as the API writes them."""
    return PhasePoint(
        timestamp=datetime.fromisoformat(naive),
        data=PhaseData(energy_import=imported, energy_export=0.0),
    )


def window(start: str, end: str) -> tuple[datetime, datetime]:
    return datetime.fromisoformat(start), datetime.fromisoformat(end)


def sum_of(row: StatisticData) -> float:
    """A row's cumulative sum.

    ``sum`` and ``state`` are ``NotRequired`` on ``StatisticData``, so reading
    either by subscript is an error to a strict type checker even here, where
    the test wrote the row itself. Going through these narrows the type and
    asserts the key is present, which a subscript would only do at runtime.
    """
    value = row.get("sum")
    assert value is not None
    return value


def state_of(row: StatisticData) -> float:
    """A row's meter register."""
    value = row.get("state")
    assert value is not None
    return value


def name_of(metadata: StatisticMetaData) -> str:
    """A series' display name, which the metadata types as optional."""
    name = metadata["name"]
    assert name is not None
    return name


class TestLocalise:
    """Attaching UTC instants to wall-clock labels."""

    def test_summer_offset_is_two_hours(self) -> None:
        # 07:00 Stockholm in September is 05:00 UTC.
        [(when, _)] = localise(
            [point("2026-09-21T07:00:00")],
            STOCKHOLM,
            window("2026-09-21T05:00+00:00", "2026-09-21T06:00+00:00"),
        )
        assert when == datetime(2026, 9, 21, 5, tzinfo=UTC)

    def test_winter_offset_is_one_hour(self) -> None:
        [(when, _)] = localise(
            [point("2026-12-01T06:00:00")],
            STOCKHOLM,
            window("2026-12-01T05:00+00:00", "2026-12-01T06:00+00:00"),
        )
        assert when == datetime(2026, 12, 1, 5, tzinfo=UTC)

    def test_autumn_fold_resolves_by_monotonicity(self) -> None:
        """02:00-02:59 local happens twice on 2026-10-25.

        The labels alone are ambiguous. The series is ordered, so a label that
        goes backwards marks the second pass and everything after it is CET.
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

    def test_window_disambiguates_a_first_point_inside_the_fold(self) -> None:
        # A window starting in the second pass has no earlier point to compare
        # against; the requested window is what rules out the first pass.
        [(when, _)] = localise(
            [point("2026-10-25T02:30:00")],
            STOCKHOLM,
            window("2026-10-25T01:00+00:00", "2026-10-25T02:00+00:00"),
        )
        assert when == datetime(2026, 10, 25, 1, 30, tzinfo=UTC)

    def test_spring_forward_has_no_gap_in_utc(self) -> None:
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

    def test_unknown_timezone_is_refused(self) -> None:
        with pytest.raises(ValueError, match="timezone"):
            localise(
                [point("2026-09-21T02:00:00")],
                "Mars/Olympus",
                window("2026-09-21T00:00+00:00", "2026-09-21T01:00+00:00"),
            )

    def test_no_points_is_not_an_error(self) -> None:
        assert (
            localise(
                [],
                STOCKHOLM,
                window("2026-09-21T00:00+00:00", "2026-09-21T01:00+00:00"),
            )
            == []
        )

    def test_the_capture_converts_to_the_expected_hours(
        self, phasedata: object
    ) -> None:
        """The real capture, end to end: 02:55-03:05 CEST is 00:55-01:05 UTC."""
        points = parse_phase_data(phasedata)
        times = [
            when
            for when, _ in localise(
                points,
                STOCKHOLM,
                window("2026-09-21T00:00+00:00", "2026-09-21T02:00+00:00"),
            )
        ]
        assert times[0] == datetime(2026, 9, 21, 0, 55, tzinfo=UTC)
        assert times[-1] == datetime(2026, 9, 21, 1, 5, tzinfo=UTC)


class TestHourlyRegisters:
    """Reducing minute points to one register per hour."""

    def test_take_the_last_reading_in_each_hour(self) -> None:
        localised = [
            (datetime(2026, 9, 21, 5, 0, tzinfo=UTC), PhaseData(energy_import=10.0)),
            (datetime(2026, 9, 21, 5, 59, tzinfo=UTC), PhaseData(energy_import=11.0)),
            (datetime(2026, 9, 21, 6, 0, tzinfo=UTC), PhaseData(energy_import=12.0)),
        ]
        assert hourly_registers(localised, "energy_import") == {
            datetime(2026, 9, 21, 5, tzinfo=UTC): 11.0,
            datetime(2026, 9, 21, 6, tzinfo=UTC): 12.0,
        }

    def test_order_of_input_does_not_matter(self) -> None:
        localised = [
            (datetime(2026, 9, 21, 5, 59, tzinfo=UTC), PhaseData(energy_import=11.0)),
            (datetime(2026, 9, 21, 5, 0, tzinfo=UTC), PhaseData(energy_import=10.0)),
        ]
        assert hourly_registers(localised, "energy_import") == {
            datetime(2026, 9, 21, 5, tzinfo=UTC): 11.0
        }

    def test_skip_points_missing_that_register(self) -> None:
        localised = [
            (datetime(2026, 9, 21, 5, 0, tzinfo=UTC), PhaseData(energy_import=10.0)),
            (datetime(2026, 9, 21, 5, 30, tzinfo=UTC), PhaseData(energy_import=None)),
        ]
        assert hourly_registers(localised, "energy_import") == {
            datetime(2026, 9, 21, 5, tzinfo=UTC): 10.0
        }

    def test_no_points_yields_no_hours(self) -> None:
        assert hourly_registers([], "energy_import") == {}


class TestStatisticIdentity:
    """How the series names itself to the recorder."""

    def test_statistic_id_is_external_not_an_entity_id(self) -> None:
        # An entity-shaped id would make the recorder co-write the series and
        # raise a state_class_removed issue; an external one is exempt.
        assert (
            statistic_id(1788016523401, "energy_import")
            == "perific:1788016523401_energy_import"
        )

    async def test_metadata_matches_what_the_recorder_requires(
        self, hass: HomeAssistant, meters: list[Item]
    ) -> None:
        metadata = statistic_metadata(
            meters[0], "energy_import", "Imported electricity"
        )
        # source must equal the part before the colon, or the import is refused.
        assert metadata["source"] == "perific"
        assert metadata["statistic_id"].startswith("perific:")
        assert metadata["has_sum"] is True
        assert metadata["unit_of_measurement"] == "kWh"
        # Both are mandatory from 2026.11 and already accepted at the 2026.5.1
        # floor, so passing them explicitly is what works across the range.
        assert metadata["mean_type"] is StatisticMeanType.NONE
        assert metadata["unit_class"] == "energy"
        # Deprecated, and the only optional key in the TypedDict. It is read
        # solely as a fallback for a missing mean_type, which is never our case.
        assert "has_mean" not in metadata

    async def test_metadata_names_the_device_and_the_register(
        self, meters: list[Item]
    ) -> None:
        name = name_of(
            statistic_metadata(meters[0], "energy_import", "Imported electricity")
        )
        assert name.endswith("Imported electricity")
        assert name != "Imported electricity", "the device should be named too"


@pytest.mark.usefixtures("enable_custom_integrations")
class TestRegisterNames:
    """An external statistic has no entity, so its name is a stored string.

    Home Assistant offers no way to translate that, so the names are read out of
    this integration's own translation files by the keys the retired energy
    sensors used. Without it the picker shows English names beside the Swedish
    power sensors.

    ``enable_custom_integrations`` is required: without it Home Assistant cannot
    find the integration to load any translations from.
    """

    async def test_english_by_default(self, hass: HomeAssistant) -> None:
        assert await async_register_names(hass) == {
            "energy_import": "Imported electricity",
            "energy_export": "Exported electricity",
            "energy_import_cost": "Imported electricity cost",
            "energy_export_compensation": "Exported electricity compensation",
            "solar_self_consumed": "Solar used directly",
            "solar_avoided_cost": "Solar savings",
            "solar_revenue": "Solar revenue",
        }

    async def test_follows_the_instance_language(self, hass: HomeAssistant) -> None:
        await hass.config.async_update(language="sv")

        names = await async_register_names(hass)

        assert names["energy_import"] == "Inköpt elektricitet"
        assert names["energy_export"] == "Såld elektricitet"
        assert names["energy_import_cost"] == "Kostnad för inköpt elektricitet"

    async def test_falls_back_when_a_language_has_no_translation(
        self, hass: HomeAssistant
    ) -> None:
        await hass.config.async_update(language="fr")

        names = await async_register_names(hass)

        assert names == HISTORY_NAMES | COST_NAMES | SOLAR_NAMES

    def test_registered_at_decodes_the_item_id(self) -> None:
        # ItemId is a millisecond epoch of the device's registration, and the
        # oldest reading the account serves is one minute after it.
        assert registered_at(1788016523401) == datetime(
            2026, 8, 29, 15, 15, 23, 401000, tzinfo=UTC
        )


def readings(*values: float, first_hour: int = 0) -> dict[datetime, float]:
    """Consecutive hourly readings starting at ``H(first_hour)``."""
    return {H(first_hour + index): value for index, value in enumerate(values)}


def sums(imported: Imported) -> list[float]:
    return [sum_of(row) for row in imported.rows]


def starts(imported: Imported) -> list[datetime]:
    return [row["start"] for row in imported.rows]


STAYS = 3  # CONFIRM_READINGS, spelled out so the tests pin it


class TestClassifyReading:
    """One reading against the last good one."""

    @pytest.mark.parametrize(
        ("previous", "reading", "hours_since", "later", "expected"),
        [
            pytest.param(100, 101, 1, [], Reading.OK, id="normal-usage"),
            pytest.param(100, 100, 1, [], Reading.OK, id="no-usage"),
            pytest.param(0, 0, 1, [], Reading.OK, id="unused-export-register"),
            pytest.param(100, 0, 1, [0] * 30, Reading.GLITCH, id="zero-is-missing"),
            pytest.param(0.5, 0, 1, [], Reading.GLITCH, id="zero-after-small-value"),
            pytest.param(100, 50, 1, [101], Reading.GLITCH, id="drop-goes-back"),
            pytest.param(100, 50, 1, [100], Reading.GLITCH, id="drop-back-to-same"),
            pytest.param(100, 50, 1, [50, 51], Reading.WAIT, id="drop-too-soon"),
            pytest.param(
                100, 50, 1, [50, 51, 52], Reading.NEW_BASELINE, id="drop-lasts"
            ),
            pytest.param(
                100, 50, 1, [0, 0, 0], Reading.WAIT, id="zeros-do-not-confirm"
            ),
            pytest.param(
                100, 90, 1, [90] * (STAYS - 1), Reading.WAIT, id="shallow-too-soon"
            ),
            pytest.param(
                100, 90, 1, [90] * STAYS, Reading.NEW_BASELINE, id="shallow-lasts"
            ),
            pytest.param(
                100,
                90,
                1,
                [90, 90, 90, *[100] * STAYS],
                Reading.NEW_BASELINE,
                id="passing-old-level-later-is-not-going-back",
            ),
            pytest.param(
                100, 90, 1, [90, 90, 100], Reading.GLITCH, id="back-on-third-reading"
            ),
            pytest.param(
                100, 1000, 1, [101], Reading.GLITCH, id="spike-comes-back-down"
            ),
            pytest.param(
                100, 1000, 1, [1000] * (STAYS - 1), Reading.WAIT, id="jump-too-soon"
            ),
            pytest.param(
                100, 1000, 1, [1000] * STAYS, Reading.NEW_BASELINE, id="jump-lasts"
            ),
            pytest.param(
                100, 100 + MAX_KWH_PER_HOUR, 1, [], Reading.OK, id="at-the-limit"
            ),
            pytest.param(100, 184, 24, [], Reading.OK, id="gap-allows-more-usage"),
            pytest.param(
                100, 151, 1, [101], Reading.GLITCH, id="over-50-kwh-in-an-hour"
            ),
            pytest.param(100, 1000, None, [], Reading.OK, id="no-earlier-hour"),
        ],
    )
    def test_classify(
        self,
        previous: float,
        reading: float,
        hours_since: float | None,
        later: list[float],
        expected: Reading,
    ) -> None:
        assert classify_reading(previous, reading, hours_since, later) is expected


class TestStatisticRows:
    """Turning hourly readings into rows."""

    def test_a_new_series_starts_at_zero(self) -> None:
        values = readings(248868.26, 248871.76)
        imported = statistic_rows(values, first_resume(values))
        assert sums(imported) == pytest.approx([0.0, 3.5])

    def test_rows_carry_the_reading_as_state(self) -> None:
        imported = statistic_rows(readings(10.0, 12.0), first_resume(readings(10.0)))
        assert [state_of(row) for row in imported.rows] == [10.0, 12.0]
        assert starts(imported) == [H(0), H(1)]

    def test_rows_are_ordered_and_on_the_hour(self) -> None:
        imported = statistic_rows({H(3): 2.0, H(2): 1.0}, Resume(1.0, 0.0, None))
        assert starts(imported) == [H(2), H(3)]
        assert all(start.minute == 0 for start in starts(imported))
        assert all(start.tzinfo is not None for start in starts(imported))

    def test_rewriting_an_hour_reproduces_the_same_sum(self) -> None:
        """Every run fetches the last stored hour again; rewriting it is a no-op."""
        values = readings(248868.26, 248871.76, 248873.01)
        first = statistic_rows(values, first_resume(values))
        before_last = first.rows[-2]
        resume = Resume(
            state=state_of(before_last),
            total=sum_of(before_last),
            after=before_last["start"],
        )

        again = statistic_rows(values, resume)

        assert starts(again) == [H(2)]
        assert sums(again) == pytest.approx(sums(first)[-1:])

    def test_continues_from_the_stored_row(self) -> None:
        resume = Resume(state=100.0, total=40.0, after=H(0))
        imported = statistic_rows(readings(101.0, 103.0, first_hour=1), resume)
        assert sums(imported) == pytest.approx([41.0, 43.0])
        assert imported.resume == Resume(state=103.0, total=43.0, after=H(2))

    def test_hours_already_stored_are_ignored(self) -> None:
        resume = Resume(state=100.0, total=40.0, after=H(1))
        imported = statistic_rows(readings(1.0, 100.0, 101.0), resume)
        assert starts(imported) == [H(2)]

    def test_the_first_fetched_hour_is_checked_too(self) -> None:
        resume = Resume(state=100.0, total=40.0, after=H(0))
        imported = statistic_rows(readings(0.0, 101.0, first_hour=1), resume)
        assert starts(imported) == [H(2)]
        assert sums(imported) == pytest.approx([41.0])

    def test_one_zero_hour_is_skipped(self) -> None:
        """Seen in the vendor's record: 13616.16, then 0.0, then 13617."""
        values = readings(13615.0, 13616.16, 0.0, 13617.0)
        imported = statistic_rows(values, first_resume(values))
        assert starts(imported) == [H(0), H(1), H(3)]
        assert sums(imported) == pytest.approx([0.0, 1.16, 2.0])

    def test_a_long_outage_of_zeros_is_skipped(self) -> None:
        values = readings(13616.0, *[0.0] * 10, 13617.0)
        imported = statistic_rows(values, first_resume(values))
        assert sums(imported) == pytest.approx([0.0, 1.0])

    def test_a_drop_that_lasts_becomes_the_new_baseline(self) -> None:
        values = readings(100.0, 101.0, 1.0, 2.0, 3.0, 4.0)
        imported = statistic_rows(values, first_resume(values))
        # The swap itself is not usage; what the new meter counts after it is.
        assert sums(imported) == pytest.approx([0.0, 1.0, 1.0, 2.0, 3.0, 4.0])

    def test_a_lasting_small_correction_writes_every_hour(self) -> None:
        values = readings(100.0, 90.0, *[90.0 + n / 10 for n in range(1, STAYS + 1)])
        imported = statistic_rows(values, first_resume(values))
        assert len(imported.rows) == len(values)
        assert imported.undecided == {}
        assert sums(imported)[-1] == pytest.approx(STAYS / 10)

    def test_a_dip_that_recovers_only_after_the_wait_is_a_new_baseline(self) -> None:
        values = readings(100.0, 90.0, *[90.0] * STAYS, 101.0)
        imported = statistic_rows(values, first_resume(values))
        assert len(imported.rows) == len(values)
        assert sums(imported)[-1] == pytest.approx(11.0)

    def test_a_small_meter_swap_keeps_the_usage_after_it(self) -> None:
        """Usage after a shallow swap can pass the old level without undoing it."""
        values = readings(100.0, 62.0, *[62.0 + 2 * n for n in range(1, STAYS + 1)])
        imported = statistic_rows(values, first_resume(values))
        assert len(imported.rows) == len(values)
        assert sums(imported)[-1] == pytest.approx(2.0 * STAYS)

    def test_a_small_dip_that_recovers_is_skipped(self) -> None:
        values = readings(12693.083, 12666.474, 12680.0, 12694.0)
        imported = statistic_rows(values, first_resume(values))
        assert starts(imported) == [H(0), H(3)]
        assert sums(imported) == pytest.approx([0.0, 0.917])

    def test_a_spike_up_is_skipped(self) -> None:
        values = readings(100.0, 99999.0, 101.0)
        imported = statistic_rows(values, first_resume(values))
        assert starts(imported) == [H(0), H(2)]
        assert sums(imported) == pytest.approx([0.0, 1.0])

    def test_hours_that_cannot_be_judged_yet_are_handed_back(self) -> None:
        values = readings(100.0, 101.0, 50.0, 51.0)
        resume = first_resume(values)
        imported = statistic_rows(values, resume)
        assert starts(imported) == [H(0), H(1)]
        assert imported.undecided == {H(2): 50.0, H(3): 51.0}
        assert imported.resume == Resume(state=101.0, total=1.0, after=H(1))

    def test_handed_back_hours_are_judged_once_more_arrive(self) -> None:
        first = statistic_rows(readings(100.0, 50.0, 51.0), first_resume(readings(100)))
        later = first.undecided | readings(52.0, 53.0, first_hour=3)
        second = statistic_rows(later, first.resume)
        assert sums(second) == pytest.approx([0.0, 1.0, 2.0, 3.0])

    def test_a_flat_register_is_accepted(self) -> None:
        values = readings(18116.944, 18116.944)
        imported = statistic_rows(values, first_resume(values))
        assert sums(imported) == [0.0, 0.0]

    def test_no_readings_gives_no_rows(self) -> None:
        resume = Resume(state=1.0, total=0.0, after=H(0))
        imported = statistic_rows({}, resume)
        assert imported.rows == []
        assert imported.resume == resume


class TestTariff:
    """What a kWh costs once the bill's other terms are added."""

    def test_reproduces_the_worked_se3_example(self) -> None:
        """Spot 139.37 + påslag 5.00 + energiskatt 42.80 öre, then 25% VAT.

        The published energy tax of 53.50 öre/kWh includes VAT; 42.80 is the
        same figure without it, which is what this field takes.
        """
        tariff = Tariff(markup=0.05, tax=0.4280, vat=0.25)
        assert tariff.price(1.3937) == pytest.approx(2.3396, abs=5e-5)

    def test_spot_alone_when_nothing_is_configured(self) -> None:
        assert Tariff().price(1.3937) == pytest.approx(1.3937)

    def test_export_carries_no_tax_or_vat(self) -> None:
        """A household selling surplus charges neither."""
        assert Tariff(markup=0.02).price(1.0) == pytest.approx(1.02)


class TestCostRows:
    """Accumulating the cost of each hour's consumption."""

    def test_costs_each_hour_at_its_own_price(self) -> None:
        registers = {H(2): 100.0, H(3): 102.0, H(4): 103.0}
        prices = {H(3): 1.0, H(4): 2.0}

        rows = cost_rows(registers, prices, Tariff(), 0.0)

        # 2 kWh at 1.00, then 1 kWh at 2.00, accumulating.
        assert [sum_of(row) for row in rows] == [pytest.approx(2.0), pytest.approx(4.0)]

    def test_the_first_hour_of_a_batch_is_not_costed(self) -> None:
        """It has no predecessor, so its own consumption is unknown here."""
        registers = {H(2): 100.0, H(3): 102.0}
        rows = cost_rows(registers, {H(2): 1.0, H(3): 1.0}, Tariff(), 0.0)
        assert [row["start"] for row in rows] == [H(3)]

    def test_continues_from_the_previous_total(self) -> None:
        registers = {H(2): 100.0, H(3): 101.0}
        [row] = cost_rows(registers, {H(3): 1.0}, Tariff(), 7.5)
        assert sum_of(row) == pytest.approx(8.5)

    def test_an_hour_without_a_price_is_skipped(self) -> None:
        """Pricing it at zero would undercount while looking deliberate."""
        registers = {H(2): 100.0, H(3): 102.0, H(4): 103.0}
        rows = cost_rows(registers, {H(4): 2.0}, Tariff(), 0.0)
        assert [row["start"] for row in rows] == [H(4)]
        assert sum_of(rows[0]) == pytest.approx(2.0)

    def test_no_prices_at_all_yields_nothing(self) -> None:
        assert cost_rows({H(2): 1.0, H(3): 2.0}, {}, Tariff(), 0.0) == []


class TestHourlyDeltas:
    """Turning cumulative sums into what each hour itself contributed."""

    def test_each_hour_carries_its_own_difference(self) -> None:
        assert hourly_deltas({H(1): 10.0, H(2): 12.5, H(3): 13.0}) == {
            H(2): pytest.approx(2.5),
            H(3): pytest.approx(0.5),
        }

    def test_the_first_hour_produces_nothing(self) -> None:
        """It is either already accounted for or the start of the series."""
        assert hourly_deltas({H(1): 10.0}) == {}

    def test_input_order_does_not_matter(self) -> None:
        assert hourly_deltas({H(3): 13.0, H(1): 10.0, H(2): 12.5}) == hourly_deltas(
            {H(1): 10.0, H(2): 12.5, H(3): 13.0}
        )


def _running() -> dict[str, float]:
    return dict.fromkeys((SOLAR_SELF_CONSUMED, SOLAR_AVOIDED_COST, SOLAR_REVENUE), 0.0)


class TestSolarRows:
    """Valuing the solar against the grid it displaced.

    The inputs are per-hour deltas already, and the prices are what a kWh is
    worth that hour with the tariff applied.
    """

    def test_self_consumption_is_production_not_exported(self) -> None:
        rows = solar_rows(
            {H(1): 5.0}, {H(1): 2.0}, {H(1): 1.0}, {H(1): 0.5}, _running()
        )
        assert sum_of(rows[SOLAR_SELF_CONSUMED][0]) == pytest.approx(3.0)

    def test_savings_value_it_at_what_buying_would_have_cost(self) -> None:
        rows = solar_rows(
            {H(1): 5.0}, {H(1): 2.0}, {H(1): 4.0}, {H(1): 0.5}, _running()
        )
        assert sum_of(rows[SOLAR_AVOIDED_COST][0]) == pytest.approx(3.0 * 4.0)

    def test_revenue_is_the_saving_plus_what_the_export_earned(self) -> None:
        rows = solar_rows(
            {H(1): 5.0}, {H(1): 2.0}, {H(1): 4.0}, {H(1): 0.5}, _running()
        )
        assert sum_of(rows[SOLAR_REVENUE][0]) == pytest.approx(3.0 * 4.0 + 2.0 * 0.5)

    def test_export_beyond_production_is_not_negative_self_consumption(self) -> None:
        """Two meters, two clocks: an hour can compute negative from skew alone."""
        rows = solar_rows(
            {H(1): 1.0}, {H(1): 3.0}, {H(1): 4.0}, {H(1): 0.5}, _running()
        )
        assert sum_of(rows[SOLAR_SELF_CONSUMED][0]) == pytest.approx(0.0)
        assert sum_of(rows[SOLAR_AVOIDED_COST][0]) == pytest.approx(0.0)

    def test_a_negative_price_lowers_the_running_total(self) -> None:
        """Exporting at a negative spot costs money, and must be allowed to."""
        rows = solar_rows(
            {H(1): 5.0}, {H(1): 4.0}, {H(1): 0.1}, {H(1): -0.5}, _running()
        )
        # 1 kWh kept, worth 0.10; 4 kWh exported at -0.50 costs 2.00.
        assert sum_of(rows[SOLAR_REVENUE][0]) == pytest.approx(0.1 - 2.0)

    def test_continues_from_the_previous_totals(self) -> None:
        running = _running() | {SOLAR_REVENUE: 100.0}
        rows = solar_rows({H(1): 5.0}, {H(1): 2.0}, {H(1): 4.0}, {H(1): 0.5}, running)
        assert sum_of(rows[SOLAR_REVENUE][0]) == pytest.approx(100.0 + 13.0)

    def test_an_hour_missing_either_price_is_skipped(self) -> None:
        rows = solar_rows({H(1): 5.0}, {H(1): 2.0}, {}, {H(1): 0.5}, _running())
        assert rows[SOLAR_REVENUE] == []

    def test_an_hour_missing_from_either_meter_is_skipped(self) -> None:
        rows = solar_rows(
            {H(1): 5.0, H(2): 5.0},
            {H(2): 2.0},
            {H(1): 4.0, H(2): 4.0},
            {H(1): 0.5, H(2): 0.5},
            _running(),
        )
        assert [row["start"] for row in rows[SOLAR_REVENUE]] == [H(2)]


# The capture is from 2026-09-21 02:55-03:05 CEST, which is 00:55-01:05 UTC.
# Freezing just after it keeps these tests from rotting the next day.
CAPTURE_HOURS = (
    datetime(2026, 9, 21, 0, tzinfo=UTC),
    datetime(2026, 9, 21, 1, tzinfo=UTC),
)
JUST_AFTER_CAPTURE = "2026-09-21 01:30:00+00:00"


class TestResumePoint:
    """Reading back where the last run stopped."""

    async def test_is_none_for_an_untouched_series(
        self, recorder_mock: None, hass: HomeAssistant
    ) -> None:
        assert await async_resume_point(hass, "perific:1_energy_import") is None

    async def test_reads_back_our_own_last_row(
        self, recorder_mock: None, hass: HomeAssistant, meters: list[Item]
    ) -> None:
        metadata = statistic_metadata(
            meters[0], "energy_import", "Imported electricity"
        )
        async_add_external_statistics(
            hass,
            metadata,
            [
                StatisticData(start=CAPTURE_HOURS[0], state=248879.629, sum=0.0),
                StatisticData(start=CAPTURE_HOURS[1], state=248880.710, sum=1.081),
            ],
        )
        await async_wait_recording_done(hass)

        resume = await async_resume_point(hass, metadata["statistic_id"])

        assert resume is not None
        assert resume.after == CAPTURE_HOURS[1]
        assert resume.offset == pytest.approx(1.081 - 248880.710)


@pytest.mark.freeze_time(JUST_AFTER_CAPTURE)
class TestHistoryImporter:
    """The catch-up loop."""

    async def test_first_run_starts_at_registration(
        self,
        recorder_mock: None,
        hass: HomeAssistant,
        setup_integration: MockConfigEntry,
        mock_client: AsyncMock,
    ) -> None:
        mock_client.async_get_phase_data.return_value = []
        meter = setup_integration.runtime_data.meters[0]

        await HistoryImporter(hass, setup_integration).async_run()

        assert mock_client.async_get_phase_data.await_args_list[0].args[1] == (
            start_of_hour(registered_at(meter.item_id))
        )

    async def test_writes_the_captured_hours(
        self,
        recorder_mock: None,
        hass: HomeAssistant,
        setup_integration: MockConfigEntry,
        mock_client: AsyncMock,
        phasedata: object,
    ) -> None:
        mock_client.async_get_phase_data.return_value = parse_phase_data(phasedata)
        meter = setup_integration.runtime_data.meters[0]

        written = await HistoryImporter(hass, setup_integration).async_import_since(
            CAPTURE_HOURS[0]
        )
        await async_wait_recording_done(hass)

        # Two hours, for each of the import and export registers.
        assert written == 4
        resume = await async_resume_point(
            hass, statistic_id(meter.item_id, "energy_import")
        )
        assert resume is not None
        assert resume.after == CAPTURE_HOURS[1]

    async def test_the_series_starts_at_zero(
        self,
        recorder_mock: None,
        hass: HomeAssistant,
        setup_integration: MockConfigEntry,
        mock_client: AsyncMock,
        phasedata: object,
    ) -> None:
        mock_client.async_get_phase_data.return_value = parse_phase_data(phasedata)
        meter = setup_integration.runtime_data.meters[0]

        await HistoryImporter(hass, setup_integration).async_import_since(
            CAPTURE_HOURS[0]
        )
        await async_wait_recording_done(hass)

        # First hour's register is 248879.629, and the offset makes it zero.
        resume = await async_resume_point(
            hass, statistic_id(meter.item_id, "energy_import")
        )
        assert resume is not None
        assert resume.offset == pytest.approx(-248879.629)

    async def test_a_second_run_does_not_change_what_it_wrote(
        self,
        recorder_mock: None,
        hass: HomeAssistant,
        setup_integration: MockConfigEntry,
        mock_client: AsyncMock,
        phasedata: object,
    ) -> None:
        """Re-importing an overlapping window must be a no-op."""
        mock_client.async_get_phase_data.return_value = parse_phase_data(phasedata)
        importer = HistoryImporter(hass, setup_integration)
        meter = setup_integration.runtime_data.meters[0]
        sid = statistic_id(meter.item_id, "energy_import")

        await importer.async_import_since(CAPTURE_HOURS[0])
        await async_wait_recording_done(hass)
        first = await async_resume_point(hass, sid)

        await importer.async_run()
        await async_wait_recording_done(hass)

        assert await async_resume_point(hass, sid) == first

    async def test_one_request_per_chunk_not_one_per_register(
        self,
        recorder_mock: None,
        hass: HomeAssistant,
        setup_integration: MockConfigEntry,
        mock_client: AsyncMock,
        phasedata: object,
    ) -> None:
        """Both registers come out of the same response.

        Fetching the window once per register would double the calls against an
        API whose rate limits are unmeasured.
        """
        mock_client.async_get_phase_data.return_value = parse_phase_data(phasedata)
        # Setting the entry up starts an import of its own; let it finish before
        # looking at what this run asked for.
        await hass.async_block_till_done()
        mock_client.async_get_phase_data.reset_mock()

        await HistoryImporter(hass, setup_integration).async_import_since(
            CAPTURE_HOURS[0]
        )

        # Asserted as "no window fetched twice" rather than a call count: one
        # request per register would fetch each window once per register, and
        # a stray call from elsewhere must not be able to fail this.
        windows = [
            (call.args[1], call.args[2])
            for call in mock_client.async_get_phase_data.await_args_list
        ]
        assert windows
        assert len(windows) == len(set(windows))

    async def test_a_caught_up_series_makes_one_request(
        self,
        recorder_mock: None,
        hass: HomeAssistant,
        setup_integration: MockConfigEntry,
        mock_client: AsyncMock,
        meters: list[Item],
    ) -> None:
        """Steady state is one call, not a walk over the whole history."""
        for key in HISTORY_REGISTERS:
            async_add_external_statistics(
                hass,
                statistic_metadata(meters[0], key, HISTORY_NAMES[key]),
                [StatisticData(start=CAPTURE_HOURS[1], state=100.0, sum=0.0)],
            )
        await async_wait_recording_done(hass)
        await hass.async_block_till_done()
        mock_client.async_get_phase_data.reset_mock()

        await HistoryImporter(hass, setup_integration).async_run()

        assert mock_client.async_get_phase_data.await_count == 1


def serve(hourly: dict[datetime, float]) -> Callable[..., Awaitable[list[PhasePoint]]]:
    """Answer phase-data requests from hourly import readings, like the API does."""
    zone = ZoneInfo(STOCKHOLM)

    async def get_phase_data(
        _item_id: int, start: datetime, end: datetime
    ) -> list[PhasePoint]:
        return [
            PhasePoint(
                timestamp=(hour + timedelta(minutes=55))
                .astimezone(zone)
                .replace(tzinfo=None),
                data=PhaseData(energy_import=value, energy_export=0.0),
            )
            for hour, value in sorted(hourly.items())
            if start <= hour + timedelta(minutes=55) < end
        ]

    return get_phase_data


async def store_import_rows(
    hass: HomeAssistant, meter: Item, rows: dict[datetime, tuple[float, float]]
) -> None:
    """Store ``hour -> (state, sum)`` rows for both registers."""
    for key in HISTORY_REGISTERS:
        async_add_external_statistics(
            hass,
            statistic_metadata(meter, key, HISTORY_NAMES[key]),
            [
                StatisticData(start=hour, state=state, sum=total)
                for hour, (state, total) in rows.items()
            ],
        )
    await async_wait_recording_done(hass)


async def stored_sums(hass: HomeAssistant, meter: Item) -> dict[datetime, float]:
    return await async_stored_hours(
        hass,
        statistic_id(meter.item_id, "energy_import"),
        (H(0), H(24 * 3)),
    )


@pytest.mark.freeze_time("2026-09-23 06:00:00+00:00")
class TestBadReadingsAcrossRuns:
    """Bad readings handled by the importer, not just by ``statistic_rows``."""

    async def test_a_reset_at_the_end_of_a_chunk_loses_no_hours(
        self,
        recorder_mock: None,
        hass: HomeAssistant,
        setup_integration: MockConfigEntry,
        mock_client: AsyncMock,
    ) -> None:
        # Chunks are one day; the meter is swapped at 22:00 on the first day.
        values = {H(hour): 100.0 + hour for hour in range(22)}
        values |= {H(hour): float(hour - 21) for hour in range(22, 30)}
        mock_client.async_get_phase_data.side_effect = serve(values)
        meter = setup_integration.runtime_data.meters[0]
        await hass.async_block_till_done()

        await HistoryImporter(hass, setup_integration).async_import_since(H(0))
        await async_wait_recording_done(hass)

        sums = await stored_sums(hass, meter)
        assert sorted(sums) == sorted(values)
        assert sums[H(21)] == pytest.approx(21.0)
        assert sums[H(22)] == pytest.approx(21.0)  # the swap is not usage
        assert sums[H(29)] == pytest.approx(28.0)

    async def test_a_rebuild_continues_from_the_row_before_it(
        self,
        recorder_mock: None,
        hass: HomeAssistant,
        setup_integration: MockConfigEntry,
        mock_client: AsyncMock,
    ) -> None:
        meter = setup_integration.runtime_data.meters[0]
        await hass.async_block_till_done()
        await store_import_rows(
            hass, meter, {H(2): (102.0, 2.0), H(10): (500.0, 900.0)}
        )
        mock_client.async_get_phase_data.side_effect = serve({H(3): 103.0, H(4): 104.0})

        await HistoryImporter(hass, setup_integration).async_import_since(H(3))
        await async_wait_recording_done(hass)

        sums = await stored_sums(hass, meter)
        assert sums[H(3)] == pytest.approx(3.0)
        assert sums[H(4)] == pytest.approx(4.0)

    async def test_a_rebuild_from_mid_hour_includes_that_hour(
        self,
        recorder_mock: None,
        hass: HomeAssistant,
        setup_integration: MockConfigEntry,
        mock_client: AsyncMock,
    ) -> None:
        meter = setup_integration.runtime_data.meters[0]
        await hass.async_block_till_done()
        await store_import_rows(hass, meter, {H(2): (102.0, 2.0), H(3): (0.0, -100.0)})
        mock_client.async_get_phase_data.side_effect = serve({H(3): 103.0, H(4): 104.0})

        await HistoryImporter(hass, setup_integration).async_import_since(
            H(3) + timedelta(minutes=30)
        )
        await async_wait_recording_done(hass)

        sums = await stored_sums(hass, meter)
        assert sums[H(3)] == pytest.approx(3.0)

    async def test_a_rebuild_finds_a_row_long_before_it(
        self,
        recorder_mock: None,
        hass: HomeAssistant,
        setup_integration: MockConfigEntry,
        mock_client: AsyncMock,
    ) -> None:
        meter = setup_integration.runtime_data.meters[0]
        await hass.async_block_till_done()
        long_ago = H(0) - timedelta(days=60)
        await store_import_rows(hass, meter, {long_ago: (100.0, 5000.0)})
        mock_client.async_get_phase_data.side_effect = serve({H(3): 103.0})

        await HistoryImporter(hass, setup_integration).async_import_since(H(3))
        await async_wait_recording_done(hass)

        sums = await stored_sums(hass, meter)
        assert sums[H(3)] == pytest.approx(5003.0)

    async def test_the_last_stored_hour_is_checked_when_fetched_again(
        self,
        recorder_mock: None,
        hass: HomeAssistant,
        setup_integration: MockConfigEntry,
        mock_client: AsyncMock,
    ) -> None:
        meter = setup_integration.runtime_data.meters[0]
        await hass.async_block_till_done()
        await store_import_rows(hass, meter, {H(0): (100.0, 0.0), H(1): (101.0, 1.0)})
        # The vendor now reports the last stored hour as 0.0.
        mock_client.async_get_phase_data.side_effect = serve(
            {H(0): 100.0, H(1): 0.0, H(2): 102.0}
        )

        await HistoryImporter(hass, setup_integration).async_run()
        await async_wait_recording_done(hass)

        sums = await stored_sums(hass, meter)
        assert sums[H(1)] == pytest.approx(1.0)
        assert sums[H(2)] == pytest.approx(2.0)


class TestHistoryImporterFailures:
    """A timed run must never let a failure escape.

    Not frozen in time: none of these reach the capture, and the class-level
    freeze collides with the ``caplog`` fixture.
    """

    async def test_cost_is_imported_when_a_price_entity_is_configured(
        self,
        recorder_mock: None,
        hass: HomeAssistant,
        setup_integration: MockConfigEntry,
        mock_client: AsyncMock,
        meters: list[Item],
        phasedata: object,
    ) -> None:
        """End to end, including that the recorder's prices are read correctly.

        `statistics_during_period` hands `start` back in seconds, the same trap
        `async_resume_point` fell into; a mis-read here silently prices every
        hour at nothing.
        """
        async_add_external_statistics(
            hass,
            StatisticMetaData(
                mean_type=StatisticMeanType.ARITHMETIC,
                has_sum=False,
                name="Spot",
                source=DOMAIN,
                statistic_id=f"{DOMAIN}:spot",
                unit_of_measurement="SEK/kWh",
                unit_class=None,
            ),
            [StatisticData(start=CAPTURE_HOURS[1], mean=2.0)],
        )
        await async_wait_recording_done(hass)

        hass.config_entries.async_update_entry(
            setup_integration,
            options={
                CONF_PRICE_ENTITY: f"{DOMAIN}:spot",
                CONF_PRICE_MARKUP: 0.0,
                CONF_ENERGY_TAX: 0.0,
                CONF_VAT_PERCENT: 0.0,
            },
        )
        await hass.async_block_till_done()
        mock_client.async_get_phase_data.return_value = parse_phase_data(phasedata)

        await HistoryImporter(hass, setup_integration).async_import_since(
            CAPTURE_HOURS[0]
        )
        await async_wait_recording_done(hass)

        meter = setup_integration.runtime_data.meters[0]
        resume = await async_resume_point(
            hass, cost_statistic_id(meter.item_id, "energy_import")
        )
        # The capture's second hour consumed 248880.710 - 248879.629 kWh, at 2.00.
        assert resume is not None
        assert resume.total == pytest.approx((248880.710 - 248879.629) * 2.0)

    async def test_cost_reaches_back_over_energy_already_stored(
        self,
        recorder_mock: None,
        hass: HomeAssistant,
        setup_integration: MockConfigEntry,
        mock_client: AsyncMock,
        meters: list[Item],
    ) -> None:
        """Cost walks the stored series, not the window this run fetched.

        Tying it to the fetch would mean nothing was ever costed: a steady-state
        run covers one hour, and an hour needs its predecessor to difference
        against. It would also strand every hour imported before a price entity
        was configured.
        """
        meter = meters[0]
        hours = [CAPTURE_HOURS[0] + i * timedelta(hours=1) for i in range(4)]
        async_add_external_statistics(
            hass,
            statistic_metadata(meter, "energy_import", "Imported"),
            [
                StatisticData(start=hour, state=100.0 + i, sum=float(i))
                for i, hour in enumerate(hours)
            ],
        )
        async_add_external_statistics(
            hass,
            StatisticMetaData(
                mean_type=StatisticMeanType.ARITHMETIC,
                has_sum=False,
                name="Spot",
                source=DOMAIN,
                statistic_id=f"{DOMAIN}:spot",
                unit_of_measurement="SEK/kWh",
                unit_class=None,
            ),
            [StatisticData(start=hour, mean=2.0) for hour in hours],
        )
        await async_wait_recording_done(hass)

        with patch("custom_components.perific.EnegicClient", return_value=mock_client):
            hass.config_entries.async_update_entry(
                setup_integration, options={CONF_PRICE_ENTITY: f"{DOMAIN}:spot"}
            )
            await hass.async_block_till_done()
        # Nothing new to fetch, so the energy walk does nothing at all.
        mock_client.async_get_phase_data.return_value = []

        await HistoryImporter(hass, setup_integration).async_run()
        await async_wait_recording_done(hass)

        resume = await async_resume_point(
            hass, cost_statistic_id(meter.item_id, "energy_import")
        )
        # Three differenced hours, 1 kWh each, at 2.00.
        assert resume is not None
        assert resume.total == pytest.approx(6.0)
        assert resume.after == hours[-1]

    async def test_costing_twice_does_not_count_an_hour_twice(
        self,
        recorder_mock: None,
        hass: HomeAssistant,
        setup_integration: MockConfigEntry,
        mock_client: AsyncMock,
        meters: list[Item],
    ) -> None:
        """The last costed hour is the next run's predecessor, not its output."""
        meter = meters[0]
        hours = [CAPTURE_HOURS[0] + i * timedelta(hours=1) for i in range(3)]
        async_add_external_statistics(
            hass,
            statistic_metadata(meter, "energy_import", "Imported"),
            [
                StatisticData(start=hour, state=100.0 + i, sum=float(i))
                for i, hour in enumerate(hours)
            ],
        )
        async_add_external_statistics(
            hass,
            StatisticMetaData(
                mean_type=StatisticMeanType.ARITHMETIC,
                has_sum=False,
                name="Spot",
                source=DOMAIN,
                statistic_id=f"{DOMAIN}:spot",
                unit_of_measurement="SEK/kWh",
                unit_class=None,
            ),
            [StatisticData(start=hour, mean=2.0) for hour in hours],
        )
        await async_wait_recording_done(hass)
        with patch("custom_components.perific.EnegicClient", return_value=mock_client):
            hass.config_entries.async_update_entry(
                setup_integration, options={CONF_PRICE_ENTITY: f"{DOMAIN}:spot"}
            )
            await hass.async_block_till_done()
        mock_client.async_get_phase_data.return_value = []

        importer = HistoryImporter(hass, setup_integration)
        await importer.async_run()
        await async_wait_recording_done(hass)
        cost_id = cost_statistic_id(meter.item_id, "energy_import")
        first = await async_resume_point(hass, cost_id)

        await importer.async_run()
        await async_wait_recording_done(hass)

        assert await async_resume_point(hass, cost_id) == first

    async def test_an_hour_costed_while_it_was_still_running_is_corrected(
        self,
        recorder_mock: None,
        hass: HomeAssistant,
        setup_integration: MockConfigEntry,
        mock_client: AsyncMock,
        meters: list[Item],
    ) -> None:
        """A restart mid-hour costs part of it; the rest must not be lost.

        The next hour is differenced against the register's final value, so
        whatever arrives after the cost was written is counted nowhere unless
        that hour is recomputed.
        """
        meter = meters[0]
        hours = [CAPTURE_HOURS[0] + i * timedelta(hours=1) for i in range(2)]
        spot = StatisticMetaData(
            mean_type=StatisticMeanType.ARITHMETIC,
            has_sum=False,
            name="Spot",
            source=DOMAIN,
            statistic_id=f"{DOMAIN}:spot",
            unit_of_measurement="SEK/kWh",
            unit_class=None,
        )
        async_add_external_statistics(
            hass, spot, [StatisticData(start=hour, mean=2.0) for hour in hours]
        )

        def register(second_hour: float) -> None:
            async_add_external_statistics(
                hass,
                statistic_metadata(meter, "energy_import", "Imported"),
                [
                    StatisticData(start=hours[0], state=100.0, sum=0.0),
                    StatisticData(
                        start=hours[1], state=100.0 + second_hour, sum=second_hour
                    ),
                ],
            )

        # A tenth of the hour has arrived when the importer first runs.
        register(0.1)
        await async_wait_recording_done(hass)
        with patch("custom_components.perific.EnegicClient", return_value=mock_client):
            hass.config_entries.async_update_entry(
                setup_integration, options={CONF_PRICE_ENTITY: f"{DOMAIN}:spot"}
            )
            await hass.async_block_till_done()
        mock_client.async_get_phase_data.return_value = []

        importer = HistoryImporter(hass, setup_integration)
        await importer.async_run()
        await async_wait_recording_done(hass)
        cost_id = cost_statistic_id(meter.item_id, "energy_import")
        partial = await async_resume_point(hass, cost_id)
        assert partial is not None
        assert partial.total == pytest.approx(0.1 * 2.0)

        # The hour finishes and the register is rewritten with its real value.
        register(1.0)
        await async_wait_recording_done(hass)
        await importer.async_run()
        await async_wait_recording_done(hass)

        corrected = await async_resume_point(hass, cost_id)
        assert corrected is not None
        assert corrected.total == pytest.approx(1.0 * 2.0)

    async def test_solar_is_valued_and_its_unit_converted(
        self,
        recorder_mock: None,
        hass: HomeAssistant,
        setup_integration: MockConfigEntry,
        mock_client: AsyncMock,
        meters: list[Item],
        phasedata: object,
    ) -> None:
        """End to end, including the Wh production most inverters report.

        SolarEdge stores Wh. Taking that for kWh would value the solar at a
        thousandth of the truth, and nothing else in the pipeline would notice.
        """
        for source, unit, rows in (
            ("spot", "SEK/kWh", [StatisticData(start=CAPTURE_HOURS[1], mean=2.0)]),
            (
                "solar",
                "Wh",
                [
                    StatisticData(start=CAPTURE_HOURS[0], state=0.0, sum=0.0),
                    StatisticData(
                        start=CAPTURE_HOURS[1], state=100_000.0, sum=100_000.0
                    ),
                ],
            ),
        ):
            async_add_external_statistics(
                hass,
                StatisticMetaData(
                    mean_type=(
                        StatisticMeanType.ARITHMETIC
                        if source == "spot"
                        else StatisticMeanType.NONE
                    ),
                    has_sum=source != "spot",
                    name=source,
                    source=DOMAIN,
                    statistic_id=f"{DOMAIN}:{source}",
                    unit_of_measurement=unit,
                    unit_class=None,
                ),
                rows,
            )
        await async_wait_recording_done(hass)

        with patch("custom_components.perific.EnegicClient", return_value=mock_client):
            hass.config_entries.async_update_entry(
                setup_integration,
                options={
                    CONF_PRICE_ENTITY: f"{DOMAIN}:spot",
                    CONF_PRICE_MARKUP: 0.0,
                    CONF_ENERGY_TAX: 0.0,
                    CONF_VAT_PERCENT: 0.0,
                    CONF_SOLAR_STATISTIC: f"{DOMAIN}:solar",
                },
            )
            await hass.async_block_till_done()
        mock_client.async_get_phase_data.return_value = parse_phase_data(phasedata)

        await HistoryImporter(hass, setup_integration).async_import_since(
            CAPTURE_HOURS[0]
        )
        await async_wait_recording_done(hass)

        meter = setup_integration.runtime_data.meters[0]
        exported = await async_resume_point(
            hass, statistic_id(meter.item_id, "energy_export")
        )
        used = await async_resume_point(
            hass, statistic_id(meter.item_id, SOLAR_SELF_CONSUMED)
        )
        revenue = await async_resume_point(
            hass, statistic_id(meter.item_id, SOLAR_REVENUE)
        )
        assert exported is not None
        assert used is not None
        assert revenue is not None

        # 100 kWh produced, so the export the capture recorded was kept back.
        assert used.total == pytest.approx(100.0 - exported.total)
        # Every kWh is worth 2.00 whether it was kept or sold, so the split
        # cancels: the hour is worth the whole production either way.
        assert revenue.total == pytest.approx(100.0 * 2.0)

    async def test_no_cost_without_a_price_entity(
        self,
        recorder_mock: None,
        hass: HomeAssistant,
        setup_integration: MockConfigEntry,
        mock_client: AsyncMock,
        phasedata: object,
    ) -> None:
        mock_client.async_get_phase_data.return_value = parse_phase_data(phasedata)

        await HistoryImporter(hass, setup_integration).async_import_since(
            CAPTURE_HOURS[0]
        )
        await async_wait_recording_done(hass)

        meter = setup_integration.runtime_data.meters[0]
        assert (
            await async_resume_point(
                hass, cost_statistic_id(meter.item_id, "energy_import")
            )
            is None
        )

    async def test_the_walk_never_asks_for_a_sub_minute_window(
        self,
        recorder_mock: None,
        hass: HomeAssistant,
        setup_integration: MockConfigEntry,
        mock_client: AsyncMock,
        phasedata: object,
    ) -> None:
        """Found against the real API, and it fired on every steady-state run.

        The walk re-read the clock each time round, so the last chunk ended a
        few hundred milliseconds short of the next reading of it. The window
        that followed went out as startTime == endTime once truncated to whole
        seconds, which answers 400. Deliberately not frozen in time: a frozen
        clock is exactly what hid this.
        """
        mock_client.async_get_phase_data.return_value = parse_phase_data(phasedata)
        mock_client.async_get_phase_data.reset_mock()

        await HistoryImporter(hass, setup_integration).async_import_since(
            dt_util.utcnow() - timedelta(hours=2)
        )

        windows = [
            (call.args[1], call.args[2])
            for call in mock_client.async_get_phase_data.await_args_list
        ]
        # One minute spelled out rather than taken from the constant the code
        # reads: a test that shares its threshold cannot fail when it moves.
        assert windows, "expected at least one request"
        assert all(end - start >= timedelta(minutes=1) for start, end in windows), (
            windows
        )

    async def test_an_empty_response_stops_the_run_without_writing(
        self,
        recorder_mock: None,
        hass: HomeAssistant,
        setup_integration: MockConfigEntry,
        mock_client: AsyncMock,
    ) -> None:
        """A wrong parameter name also returns 200 with an empty list.

        An empty response therefore means "nothing more to read", never "no
        energy was used".
        """
        mock_client.async_get_phase_data.return_value = []

        assert await HistoryImporter(hass, setup_integration).async_run() == 0

    async def test_an_api_failure_does_not_raise(
        self,
        recorder_mock: None,
        hass: HomeAssistant,
        setup_integration: MockConfigEntry,
        mock_client: AsyncMock,
    ) -> None:
        mock_client.async_get_phase_data.side_effect = PerificError("boom")

        with patch.object(history, "_LOGGER") as logger:
            assert await HistoryImporter(hass, setup_integration).async_run() == 0

        # Surfaced, not swallowed: a run that quietly returns zero looks exactly
        # like a caught-up series. Not asserted as exactly once — setting the
        # entry up starts its own import, which can still be in flight here.
        logger.exception.assert_called()
        assert logger.exception.call_args is not None
        assert "History import failed" in logger.exception.call_args.args[0]

    async def test_a_revoked_token_is_renewed_rather_than_failing_the_run(
        self,
        recorder_mock: None,
        hass: HomeAssistant,
        setup_integration: MockConfigEntry,
        mock_client: AsyncMock,
    ) -> None:
        """Polling stops once reauth is pending; this timer does not."""
        rejected = False

        async def revoked_once(*_args: object) -> list[PhasePoint]:
            nonlocal rejected
            if not rejected:
                rejected = True
                raise PerificAuthError("revoked")
            return []

        mock_client.async_get_phase_data.side_effect = revoked_once
        mock_client.async_login.return_value = RENEWED_TOKEN_INFO

        with patch.object(history, "_LOGGER") as logger:
            assert await HistoryImporter(hass, setup_integration).async_run() == 0

        mock_client.async_login.assert_awaited_once()
        assert setup_integration.data[CONF_TOKEN] == RENEWED_TOKEN_INFO.token
        logger.warning.assert_not_called()
        logger.exception.assert_not_called()

    async def test_a_rejected_password_is_one_line_not_a_traceback(
        self,
        recorder_mock: None,
        hass: HomeAssistant,
        setup_integration: MockConfigEntry,
        mock_client: AsyncMock,
    ) -> None:
        """The reauth prompt is the signal; an hourly traceback only buries it."""
        mock_client.async_get_phase_data.side_effect = PerificAuthError("revoked")
        mock_client.async_login.side_effect = PerificAuthError("wrong password")

        with patch.object(history, "_LOGGER") as logger:
            assert await HistoryImporter(hass, setup_integration).async_run() == 0

        logger.exception.assert_not_called()
        logger.warning.assert_called()
        assert "History import skipped" in logger.warning.call_args.args[0]

    async def test_a_meter_reset_does_not_raise(
        self,
        recorder_mock: None,
        hass: HomeAssistant,
        setup_integration: MockConfigEntry,
        mock_client: AsyncMock,
    ) -> None:
        """A falling register no longer aborts the import."""
        mock_client.async_get_phase_data.return_value = [
            point("2026-09-21T02:55:00", 100.0),
            point("2026-09-21T03:55:00", 50.0),
        ]

        with patch.object(history, "_LOGGER") as logger:
            assert await HistoryImporter(hass, setup_integration).async_run() > 0

        logger.exception.assert_not_called()
