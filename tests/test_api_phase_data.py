"""``PUT /getphasedata`` — the historical series the energy import reads.

Its verb, both parameter names and its timestamp handling all differ from the
community documentation, so the wire format is pinned here alongside the
parsing. See ``docs/api/enegic.md``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from itertools import pairwise
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest

from custom_components.perific.api import PhasePoint, parse_phase_data

if TYPE_CHECKING:
    from collections.abc import Callable

    from custom_components.perific.api import EnegicClient

    from .conftest import FakeApi

ITEM_ID = 12345


class TestParsePhaseData:
    """Flattening the response into points."""

    def test_parses_points_from_the_capture(self, phasedata: Any) -> None:
        points = parse_phase_data(phasedata)
        assert len(points) == 11
        assert all(isinstance(point, PhasePoint) for point in points)

    def test_points_are_ordered_and_one_minute_apart(self, phasedata: Any) -> None:
        points = parse_phase_data(phasedata)
        gaps = {
            (b.timestamp - a.timestamp).total_seconds() for a, b in pairwise(points)
        }
        assert gaps == {60.0}

    def test_carries_the_cumulative_registers(self, phasedata: Any) -> None:
        first = parse_phase_data(phasedata)[0]
        assert first.data.energy_import == pytest.approx(248878.898)
        assert first.data.energy_export == pytest.approx(18116.944)

    def test_the_import_register_advances_across_the_capture(
        self, phasedata: Any
    ) -> None:
        points = parse_phase_data(phasedata)
        first, last = points[0].data.energy_import, points[-1].data.energy_import
        assert first is not None
        assert last is not None
        assert last > first

    def test_the_export_register_is_flat(self, phasedata: Any) -> None:
        # Real overnight data: nothing was exported. A flat register is normal
        # and must not be mistaken for a fault; only a fall is suspect.
        exported = {point.data.energy_export for point in parse_phase_data(phasedata)}
        assert exported == {18116.944}

    def test_timestamps_are_naive(self, phasedata: Any) -> None:
        # The API labels points in the item's own timezone with no offset.
        # Attaching one here would hide the conversion the importer has to do.
        assert parse_phase_data(phasedata)[0].timestamp.tzinfo is None

    @pytest.mark.parametrize("payload", [None, {}, [], [{"data": None}], "nonsense"])
    def test_tolerates_rubbish(self, payload: Any) -> None:
        assert parse_phase_data(payload) == []

    def test_skips_points_with_an_unusable_timestamp(self) -> None:
        payload = [
            {
                "dt": "2026-01-01T00:00:00",
                "data": [
                    {"ts": "not-a-time", "data": {"hwi": 1.0}},
                    {"ts": "2026-09-21T02:55:00", "data": {"hwi": 2.0}},
                ],
            }
        ]
        assert [point.data.energy_import for point in parse_phase_data(payload)] == [
            2.0
        ]


class TestGetPhaseData:
    """What goes out on the wire."""

    async def test_requests_the_documented_shape(
        self,
        fake_api: FakeApi,
        make_client: Callable[..., EnegicClient],
        phasedata: Any,
    ) -> None:
        fake_api.respond_json("PUT", "/getphasedata", phasedata)
        client = make_client(token="t")

        points = await client.async_get_phase_data(
            ITEM_ID,
            datetime(2026, 9, 21, 0, tzinfo=UTC),
            datetime(2026, 9, 21, 6, tzinfo=UTC),
        )

        assert len(points) == 11
        recorded = fake_api.request_for("/getphasedata")
        assert recorded.method == "PUT"
        assert json.loads(recorded.body) == {
            "itemId": ITEM_ID,
            "startTime": "2026-09-21T00:00:00",
            "endTime": "2026-09-21T06:00:00",
        }

    async def test_converts_aware_times_to_utc_before_sending(
        self,
        fake_api: FakeApi,
        make_client: Callable[..., EnegicClient],
        phasedata: Any,
    ) -> None:
        """The server reads the naive strings as UTC.

        A local-time window therefore has to be converted rather than merely
        stripped of its offset.
        """
        fake_api.respond_json("PUT", "/getphasedata", phasedata)
        client = make_client(token="t")

        await client.async_get_phase_data(
            ITEM_ID, datetime(2026, 9, 21, 2, tzinfo=ZoneInfo("Europe/Stockholm"))
        )

        sent = json.loads(fake_api.request_for("/getphasedata").body)
        assert sent["startTime"] == "2026-09-21T00:00:00"

    async def test_omits_end_time_when_not_given(
        self,
        fake_api: FakeApi,
        make_client: Callable[..., EnegicClient],
        phasedata: Any,
    ) -> None:
        fake_api.respond_json("PUT", "/getphasedata", phasedata)
        client = make_client(token="t")

        await client.async_get_phase_data(ITEM_ID, datetime(2026, 9, 21, tzinfo=UTC))

        assert "endTime" not in json.loads(fake_api.request_for("/getphasedata").body)
