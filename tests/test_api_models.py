"""Parsing tests, run against the redacted captures in ``tests/fixtures``.

The tolerant-parsing behaviour is a deliberate design choice (see
``docs/specs/perific-integration.md``), so the degradation cases are pinned here
alongside the happy paths.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from custom_components.perific.api import (
    BUCKET_DAY,
    BUCKET_HOUR,
    BUCKET_MINUTE,
    BUCKET_REALTIME,
    Item,
    ItemPackets,
    Packet,
    PerificResponseError,
    PhaseData,
    TokenInfo,
)
from custom_components.perific.api.models import parse_api_datetime

METER_ITEM_ID = 10004


class TestParseApiDatetime:
    """The API writes seven fractional digits, which fromisoformat rejects."""

    def test_accepts_seven_fractional_digits(self) -> None:
        parsed = parse_api_datetime("2026-09-15T08:16:54.4029791Z")
        assert parsed == datetime(2026, 9, 15, 8, 16, 54, 402979, tzinfo=UTC)

    def test_assumes_utc_when_unmarked(self) -> None:
        parsed = parse_api_datetime("2026-09-15T08:16:54")
        assert parsed is not None
        assert parsed.tzinfo is UTC

    @pytest.mark.parametrize("value", ["", "not a timestamp", None, 17, []])
    def test_unreadable_becomes_none(self, value: Any) -> None:
        assert parse_api_datetime(value) is None


class TestTokenInfo:
    """``PUT /createtoken``."""

    def test_parses_the_capture(self, createtoken: Any) -> None:
        info = TokenInfo.from_api(createtoken)
        assert info.token == createtoken["TokenInfo"]["Token"]
        assert info.created == datetime(2026, 9, 15, 8, 16, 54, 402979, tzinfo=UTC)

    def test_validity_is_a_year(self, createtoken: Any) -> None:
        info = TokenInfo.from_api(createtoken)
        assert info.created is not None
        assert info.valid_to is not None
        # Exactly 365 days, to within the sub-second noise between the two stamps.
        elapsed = (info.valid_to - info.created).total_seconds()
        assert elapsed == pytest.approx(timedelta(days=365).total_seconds(), abs=1)

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"TokenInfo": None},
            {"TokenInfo": {}},
            {"TokenInfo": {"Token": ""}},
            "not an object",
        ],
    )
    def test_missing_token_is_an_error(self, payload: Any) -> None:
        with pytest.raises(PerificResponseError):
            TokenInfo.from_api(payload)

    def test_unusable_dates_leave_the_token_usable(self) -> None:
        info = TokenInfo.from_api({"TokenInfo": {"Token": "t", "ValidTo": "garbage"}})
        assert info.token == "t"
        assert info.valid_to is None


class TestItem:
    """``GET /getaccountoverview``."""

    def test_parses_every_item_in_the_capture(self, overview: Any) -> None:
        items = [Item.from_api(entry) for entry in overview["Items"]]
        assert [item.item_id for item in items] == [10001, 10002, 10003, METER_ITEM_ID]

    def test_identifies_the_one_meter(self, overview: Any) -> None:
        items = [Item.from_api(entry) for entry in overview["Items"]]
        meters = [item for item in items if item.is_meter]
        assert [item.item_id for item in meters] == [METER_ITEM_ID]

    def test_reads_hardware_detail_from_nested_parameters(self, overview: Any) -> None:
        meter = next(
            Item.from_api(entry)
            for entry in overview["Items"]
            if entry["ItemId"] == METER_ITEM_ID
        )
        assert meter.sub_type == "EM2One"
        assert meter.firmware == "4.5.15"
        assert meter.hardware == "4.6"
        assert meter.state == "Active"

    def test_unset_enums_arrive_as_the_string_none(self, overview: Any) -> None:
        stub = next(
            entry for entry in overview["Items"] if entry["ItemCategory"] == "None"
        )
        item = Item.from_api(stub)
        assert item.category is None
        assert item.item_type is None
        assert item.sub_type is None
        assert item.is_meter is False

    def test_missing_nested_parameters_is_tolerated(self) -> None:
        item = Item.from_api({"ItemId": 1, "ActualItemUserParameters": None})
        assert item.firmware is None

    @pytest.mark.parametrize("payload", [{}, {"ItemId": None}, {"ItemId": "abc"}, []])
    def test_missing_item_id_is_an_error(self, payload: Any) -> None:
        with pytest.raises(PerificResponseError):
            Item.from_api(payload)

    def test_unknown_fields_are_ignored(self) -> None:
        item = Item.from_api({"ItemId": 1, "SomethingNew": {"nested": True}})
        assert item.item_id == 1


class TestPhaseData:
    """A packet's ``data`` object. Field sets differ per bucket and per device."""

    def test_reads_the_minute_bucket(self, packets_t0: Any) -> None:
        data = PhaseData.from_api(packets_t0[0]["LatestPackets"][BUCKET_MINUTE]["data"])
        assert data.energy_import == 248718.155
        assert data.energy_export == 18048.557
        assert data.current == (7.32, 7.67, -3.54)
        assert data.voltage == (232.3, 231.5, 236.4)

    def test_realtime_carries_no_energy_registers(self, packets_t0: Any) -> None:
        data = PhaseData.from_api(
            packets_t0[0]["LatestPackets"][BUCKET_REALTIME]["data"]
        )
        assert data.energy_import is None
        assert data.energy_export is None
        assert data.current == (10.0, 7.5, -3.69)
        assert data.current_min == ()

    @pytest.mark.parametrize("payload", [None, {}, "nonsense", []])
    def test_unusable_data_degrades_instead_of_raising(self, payload: Any) -> None:
        data = PhaseData.from_api(payload)
        assert data.energy_import is None
        assert data.current == ()

    def test_a_bad_phase_keeps_the_others_in_place(self) -> None:
        data = PhaseData.from_api({"hiavg": [1.5, "x", 3.5]})
        assert data.current == (1.5, None, 3.5)

    def test_booleans_are_not_numbers(self) -> None:
        # bool is an int in Python; hwi: true must not become 1.0 kWh.
        data = PhaseData.from_api({"hwi": True, "hiavg": [True, 2.0]})
        assert data.energy_import is None
        assert data.current == (None, 2.0)

    def test_integers_become_floats(self) -> None:
        data = PhaseData.from_api({"hwi": 248718, "huavg": [235, 232]})
        assert data.energy_import == 248718.0
        assert data.voltage == (235.0, 232.0)


class TestPacket:
    """One bucket's reading."""

    def test_reads_metadata(self, packets_t0: Any) -> None:
        packet = Packet.from_api(
            BUCKET_MINUTE, packets_t0[0]["LatestPackets"][BUCKET_MINUTE]
        )
        assert packet.bucket == BUCKET_MINUTE
        assert packet.timestamp == datetime.fromtimestamp(1789460100, tz=UTC)
        assert packet.seqno == 22120
        assert packet.packet_version == 3
        assert packet.firmware == "4.5.15"
        assert packet.rssi == -62

    @pytest.mark.parametrize("payload", [None, "nonsense", 5])
    def test_a_non_object_still_yields_a_packet(self, payload: Any) -> None:
        packet = Packet.from_api(BUCKET_MINUTE, payload)
        assert packet.bucket == BUCKET_MINUTE
        assert packet.timestamp is None
        assert packet.data.energy_import is None


class TestItemPackets:
    """``PUT /getlatestpackets``."""

    def test_all_four_buckets_are_present(self, packets_t0: Any) -> None:
        entry = ItemPackets.from_api(packets_t0[0])
        assert entry.item_id == METER_ITEM_ID
        assert set(entry.packets) == {
            BUCKET_REALTIME,
            BUCKET_MINUTE,
            BUCKET_HOUR,
            BUCKET_DAY,
        }

    def test_convenience_accessors(self, packets_t0: Any) -> None:
        entry = ItemPackets.from_api(packets_t0[0])
        assert entry.minute is not None
        assert entry.minute.bucket == BUCKET_MINUTE
        assert entry.realtime is not None
        assert entry.realtime.data.energy_import is None

    def test_missing_bucket_reads_as_none(self) -> None:
        entry = ItemPackets.from_api({"ItemId": 1, "LatestPackets": {}})
        assert entry.minute is None
        assert entry.realtime is None

    @pytest.mark.parametrize(
        "payload", [{}, {"ItemId": None}, {"LatestPackets": {}}, "nonsense"]
    )
    def test_missing_item_id_is_an_error(self, payload: Any) -> None:
        with pytest.raises(PerificResponseError):
            ItemPackets.from_api(payload)

    def test_a_missing_latestpackets_key_is_tolerated(self) -> None:
        entry = ItemPackets.from_api({"ItemId": 1})
        assert entry.packets == {}


class TestCaptureComparison:
    """What the two captures taken ~60 s apart are for.

    These assertions are the evidence behind ``TOTAL_INCREASING``; if they ever fail
    against regenerated fixtures, the sensor typing needs revisiting before the code
    does.
    """

    def test_the_energy_registers_are_cumulative(
        self, packets_t0: Any, packets_t1: Any
    ) -> None:
        before = ItemPackets.from_api(packets_t0[0]).minute
        after = ItemPackets.from_api(packets_t1[0]).minute
        assert before is not None
        assert after is not None
        assert before.data.energy_import is not None
        assert after.data.energy_import is not None
        assert after.data.energy_import > before.data.energy_import
        assert after.data.energy_import - before.data.energy_import == pytest.approx(
            0.067
        )

    def test_both_directions_can_rise_in_the_same_minute(
        self, packets_t0: Any, packets_t1: Any
    ) -> None:
        before = ItemPackets.from_api(packets_t0[0]).minute
        after = ItemPackets.from_api(packets_t1[0]).minute
        assert before is not None
        assert after is not None
        assert before.data.energy_export is not None
        assert after.data.energy_export is not None
        assert after.data.energy_export > before.data.energy_export

    def test_the_minute_bucket_advances_by_one(
        self, packets_t0: Any, packets_t1: Any
    ) -> None:
        before = ItemPackets.from_api(packets_t0[0]).minute
        after = ItemPackets.from_api(packets_t1[0]).minute
        assert before is not None
        assert after is not None
        assert before.seqno is not None
        assert after.seqno == before.seqno + 1

    def test_the_day_bucket_is_re_served_unchanged(
        self, packets_t0: Any, packets_t1: Any
    ) -> None:
        before = ItemPackets.from_api(packets_t0[0]).packets[BUCKET_DAY]
        after = ItemPackets.from_api(packets_t1[0]).packets[BUCKET_DAY]
        assert after == before
