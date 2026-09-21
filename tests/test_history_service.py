"""The import timer and the on-demand service.

Nothing is written unless the timer fires or the service is called, and the
timer is phased to the hour rather than to whenever setup happened.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.perific.const import (
    DOMAIN,
    HISTORY_RUN_AT_MINUTE,
    SERVICE_IMPORT_HISTORY,
)

if TYPE_CHECKING:
    from unittest.mock import AsyncMock

    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

IMPORT_SINCE = "custom_components.perific.HistoryImporter.async_import_since"

# Far enough from the scheduled minute in both directions that the next run is
# unambiguously in the following hour.
MIDWAY_THROUGH_AN_HOUR = "2026-09-21 12:30:00+00:00"


def next_run(offset_hours: int = 1) -> datetime:
    """A moment just past the importer's next scheduled run.

    A second past rather than exactly on it: firing on the boundary makes the
    listener's due-time comparison decide the test, which showed up as roughly
    one failure in three.
    """
    scheduled = (dt_util.now() + timedelta(hours=offset_hours)).replace(
        minute=HISTORY_RUN_AT_MINUTE, second=0, microsecond=0
    )
    return scheduled + timedelta(seconds=1)


@pytest.mark.freeze_time(MIDWAY_THROUGH_AN_HOUR)
class TestTimer:
    """The hourly schedule.

    Asserted through the API client rather than by patching ``async_run``: the
    timer holds a bound method captured at setup, which a later patch of the
    class would not reach.

    Frozen in time because the schedule is phrased in wall-clock terms. Against
    the real clock, where the run sits relative to the current minute decides
    whether a fired listener is due yet, which showed up as roughly one failure
    in twenty.
    """

    async def test_import_runs_just_after_each_hour(
        self,
        recorder_mock: None,
        hass: HomeAssistant,
        setup_integration: MockConfigEntry,
        mock_client: AsyncMock,
    ) -> None:
        mock_client.async_get_phase_data.return_value = []
        before = mock_client.async_get_phase_data.await_count

        async_fire_time_changed(hass, next_run())
        await hass.async_block_till_done()

        assert mock_client.async_get_phase_data.await_count > before

    async def test_unloading_the_entry_stops_the_timer(
        self,
        recorder_mock: None,
        hass: HomeAssistant,
        setup_integration: MockConfigEntry,
        mock_client: AsyncMock,
    ) -> None:
        mock_client.async_get_phase_data.return_value = []
        await hass.config_entries.async_unload(setup_integration.entry_id)
        await hass.async_block_till_done()
        before = mock_client.async_get_phase_data.await_count

        async_fire_time_changed(hass, next_run(offset_hours=2))
        await hass.async_block_till_done()

        assert mock_client.async_get_phase_data.await_count == before


class TestImportHistoryService:
    """``perific.import_history``."""

    async def test_service_is_registered(
        self,
        recorder_mock: None,
        hass: HomeAssistant,
        setup_integration: MockConfigEntry,
    ) -> None:
        assert hass.services.has_service(DOMAIN, SERVICE_IMPORT_HISTORY)

    async def test_forces_a_rebuild_from_a_given_start(
        self,
        recorder_mock: None,
        hass: HomeAssistant,
        setup_integration: MockConfigEntry,
    ) -> None:
        with patch(IMPORT_SINCE, return_value=7) as since:
            await hass.services.async_call(
                DOMAIN,
                SERVICE_IMPORT_HISTORY,
                {"start": "2026-08-29T19:00:00+02:00"},
                blocking=True,
            )

        assert since.await_args is not None
        assert since.await_args.args[0] == datetime(2026, 8, 29, 17, tzinfo=UTC)

    async def test_without_a_start_it_resumes(
        self,
        recorder_mock: None,
        hass: HomeAssistant,
        setup_integration: MockConfigEntry,
    ) -> None:
        with patch(IMPORT_SINCE, return_value=0) as since:
            await hass.services.async_call(
                DOMAIN, SERVICE_IMPORT_HISTORY, {}, blocking=True
            )

        assert since.await_args is not None
        assert since.await_args.args[0] is None
