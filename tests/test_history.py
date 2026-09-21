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

import pytest
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.perific import history
from custom_components.perific.api import (
    PerificError,
    PhaseData,
    PhasePoint,
    parse_phase_data,
)
from custom_components.perific.const import DOMAIN, HISTORY_REGISTERS
from custom_components.perific.history import (
    HistoryImporter,
    Resume,
    async_resume_point,
    hourly_registers,
    localise,
    registered_at,
    statistic_id,
    statistic_metadata,
    statistic_rows,
)

if TYPE_CHECKING:
    from unittest.mock import AsyncMock

    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.perific.api import Item

STOCKHOLM = "Europe/Stockholm"

# Taken from the live instance: sum - state is this constant on every row.
LIVE_ANCHOR = Resume(offset=25.807 - 248762.995, after=None)


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
        metadata = statistic_metadata(hass, meters[0], "energy_import")
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

    async def test_metadata_names_the_two_registers_apart(
        self, hass: HomeAssistant, meters: list[Item]
    ) -> None:
        imported = name_of(statistic_metadata(hass, meters[0], "energy_import"))
        exported = name_of(statistic_metadata(hass, meters[0], "energy_export"))
        assert imported != exported

    async def test_name_falls_back_to_english_without_an_entity(
        self, hass: HomeAssistant, meters: list[Item]
    ) -> None:
        # Nothing registered in this test, so there is no resolved sensor name
        # to borrow.
        name = name_of(statistic_metadata(hass, meters[0], "energy_import"))
        assert name.endswith("Imported electricity")

    async def test_name_borrows_the_sensors_translated_name(
        self, hass: HomeAssistant, meters: list[Item]
    ) -> None:
        """An external statistic's name is a plain stored string.

        Home Assistant offers no way to translate it, so it is taken from the
        matching sensor, which does carry a translation. Without this the
        picker shows the two series in different languages.
        """
        meter = meters[0]
        er.async_get(hass).async_get_or_create(
            "sensor",
            DOMAIN,
            f"{meter.item_id}_energy_import",
            original_name="Inköpt elektricitet",
        )

        name = name_of(statistic_metadata(hass, meter, "energy_import"))

        assert name.endswith("Inköpt elektricitet")

    async def test_name_follows_a_user_rename(
        self, hass: HomeAssistant, meters: list[Item]
    ) -> None:
        meter = meters[0]
        registry = er.async_get(hass)
        entry = registry.async_get_or_create(
            "sensor",
            DOMAIN,
            f"{meter.item_id}_energy_import",
            original_name="Inköpt elektricitet",
        )
        registry.async_update_entity(entry.entity_id, name="Husets elmätare")

        name = name_of(statistic_metadata(hass, meter, "energy_import"))

        assert name.endswith("Husets elmätare")

    def test_registered_at_decodes_the_item_id(self) -> None:
        # ItemId is a millisecond epoch of the device's registration, and the
        # oldest reading the account serves is one minute after it.
        assert registered_at(1788016523401) == datetime(
            2026, 8, 29, 15, 15, 23, 401000, tzinfo=UTC
        )


class TestStatisticRows:
    """Mapping end-of-hour registers onto importable rows."""

    def test_first_ever_import_starts_the_series_at_zero(self) -> None:
        resume = Resume(offset=-248868.26, after=None)
        registers = {
            datetime(2026, 9, 21, 2, tzinfo=UTC): 248868.26,
            datetime(2026, 9, 21, 3, tzinfo=UTC): 248871.76,
        }
        first, second = statistic_rows(registers, resume)
        assert sum_of(first) == pytest.approx(0.0)
        assert sum_of(second) == pytest.approx(3.5)

    def test_rows_carry_the_register_as_state(self) -> None:
        [row] = statistic_rows(
            {datetime(2026, 9, 21, 2, tzinfo=UTC): 248868.26}, LIVE_ANCHOR
        )
        assert state_of(row) == pytest.approx(248868.26)
        assert row["start"] == datetime(2026, 9, 21, 2, tzinfo=UTC)

    def test_consecutive_rows_differ_by_the_real_consumption(self) -> None:
        registers = {
            datetime(2026, 9, 21, 2, tzinfo=UTC): 248868.26,
            datetime(2026, 9, 21, 3, tzinfo=UTC): 248871.76,
        }
        first, second = statistic_rows(registers, LIVE_ANCHOR)
        assert sum_of(second) - sum_of(first) == pytest.approx(3.5)

    def test_rows_are_ordered_and_on_the_hour(self) -> None:
        registers = {
            datetime(2026, 9, 21, 3, tzinfo=UTC): 2.0,
            datetime(2026, 9, 21, 2, tzinfo=UTC): 1.0,
        }
        rows = statistic_rows(registers, Resume(offset=0.0, after=None))
        assert [row["start"] for row in rows] == sorted(row["start"] for row in rows)
        assert all(row["start"].minute == 0 for row in rows)
        assert all(row["start"].tzinfo is not None for row in rows)

    def test_sums_may_be_negative_below_the_anchor(self) -> None:
        """Only differences between sums are ever read.

        ``change`` is a plain subtraction (``recorder/statistics.py:2089``), so
        an hour below the anchor is correct rather than a bug.
        """
        [row] = statistic_rows(
            {datetime(2026, 9, 1, tzinfo=UTC): 248700.0}, LIVE_ANCHOR
        )
        assert sum_of(row) < 0

    def test_rewriting_an_hour_reproduces_the_same_sum(self) -> None:
        """A run that overlaps what it already wrote must be a no-op.

        The offset is read back off the previous run's own row, so the
        arithmetic that produced a value is the arithmetic that reproduces it.
        """
        registers = {datetime(2026, 9, 21, 2, tzinfo=UTC): 248868.26}
        [first] = statistic_rows(registers, Resume(offset=-248868.26, after=None))
        resume = Resume(offset=sum_of(first) - state_of(first), after=None)
        [again] = statistic_rows(registers, resume)
        assert sum_of(again) == pytest.approx(sum_of(first))

    def test_a_flat_register_is_accepted(self) -> None:
        # The export register sits flat for hours at a time. Only a fall is
        # suspect.
        registers = {
            datetime(2026, 9, 21, 2, tzinfo=UTC): 18116.944,
            datetime(2026, 9, 21, 3, tzinfo=UTC): 18116.944,
        }
        first, second = statistic_rows(registers, Resume(offset=0.0, after=None))
        assert sum_of(second) == sum_of(first)

    def test_a_falling_register_is_refused(self) -> None:
        """A register that goes backwards is a meter reset.

        The offset is no longer constant across one, so writing through it
        would corrupt the series.
        """
        registers = {
            datetime(2026, 9, 21, 2, tzinfo=UTC): 248868.26,
            datetime(2026, 9, 21, 3, tzinfo=UTC): 248860.0,
        }
        with pytest.raises(ValueError, match="backwards"):
            statistic_rows(registers, Resume(offset=0.0, after=None))

    def test_an_empty_mapping_yields_no_rows(self) -> None:
        assert statistic_rows({}, Resume(offset=0.0, after=None)) == []


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
        metadata = statistic_metadata(hass, meters[0], "energy_import")
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
            registered_at(meter.item_id)
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
                statistic_metadata(hass, meters[0], key),
                [StatisticData(start=CAPTURE_HOURS[1], state=100.0, sum=0.0)],
            )
        await async_wait_recording_done(hass)
        await hass.async_block_till_done()
        mock_client.async_get_phase_data.reset_mock()

        await HistoryImporter(hass, setup_integration).async_run()

        assert mock_client.async_get_phase_data.await_count == 1


class TestHistoryImporterFailures:
    """A timed run must never let a failure escape.

    Not frozen in time: none of these reach the capture, and the class-level
    freeze collides with the ``caplog`` fixture.
    """

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

    async def test_a_meter_reset_does_not_raise(
        self,
        recorder_mock: None,
        hass: HomeAssistant,
        setup_integration: MockConfigEntry,
        mock_client: AsyncMock,
    ) -> None:
        """A falling register is refused, and the refusal stays contained."""
        mock_client.async_get_phase_data.return_value = [
            point("2026-09-21T02:55:00", 100.0),
            point("2026-09-21T03:55:00", 50.0),
        ]

        with patch.object(history, "_LOGGER") as logger:
            assert await HistoryImporter(hass, setup_integration).async_run() == 0

        logger.exception.assert_called()
