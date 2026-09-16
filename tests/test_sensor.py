"""Sensor entities, above all their typing.

Wrong typing produces an integration that looks like it works — entities update, and
Home Assistant silently declines to record statistics. These assertions are the only
place that mistake surfaces loudly.
"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from homeassistant.components.sensor import (
    ATTR_STATE_CLASS,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNAVAILABLE,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.perific.api import BUCKET_MINUTE, BUCKET_REALTIME
from custom_components.perific.const import DOMAIN

METER_ID = 10004
ENERGY_KEYS = ("energy_import", "energy_export")
POWER_KEYS = ("power_import", "power_export")
CURRENT_KEYS = tuple(f"current_l{phase}" for phase in (1, 2, 3))
VOLTAGE_KEYS = tuple(f"voltage_l{phase}" for phase in (1, 2, 3))
REALTIME_KEYS = POWER_KEYS + CURRENT_KEYS + VOLTAGE_KEYS
KEYS = ENERGY_KEYS + REALTIME_KEYS


@pytest.fixture(autouse=True)
def _enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Home Assistant only loads custom_components when this fixture is active."""


@pytest.fixture
def entity_ids(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> dict[str, str]:
    """Look entities up by unique id rather than guessing their entity ids."""
    registry = er.async_get(hass)
    found = {}
    for key in KEYS:
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{METER_ID}_{key}")
        assert entity_id is not None
        found[key] = entity_id
    return found


class TestTyping:
    """The typing that decides whether statistics get recorded at all."""

    @pytest.mark.parametrize("key", ENERGY_KEYS)
    async def test_energy_is_typed_for_long_term_statistics(
        self, hass: HomeAssistant, entity_ids: dict[str, str], key: str
    ) -> None:
        state = hass.states.get(entity_ids[key])
        assert state is not None
        assert state.attributes[ATTR_DEVICE_CLASS] == SensorDeviceClass.ENERGY
        assert state.attributes[ATTR_STATE_CLASS] == SensorStateClass.TOTAL_INCREASING
        assert state.attributes[ATTR_UNIT_OF_MEASUREMENT] == UnitOfEnergy.KILO_WATT_HOUR

    @pytest.mark.parametrize("key", POWER_KEYS)
    async def test_power_is_typed_for_the_energy_dashboard(
        self, hass: HomeAssistant, entity_ids: dict[str, str], key: str
    ) -> None:
        """The dashboard's two-sensor mode wants POWER, a power unit, and statistics."""
        state = hass.states.get(entity_ids[key])
        assert state is not None
        assert state.attributes[ATTR_DEVICE_CLASS] == SensorDeviceClass.POWER
        assert state.attributes[ATTR_STATE_CLASS] == SensorStateClass.MEASUREMENT
        assert state.attributes[ATTR_UNIT_OF_MEASUREMENT] == UnitOfPower.WATT

    @pytest.mark.parametrize(
        ("keys", "device_class", "unit"),
        [
            (CURRENT_KEYS, SensorDeviceClass.CURRENT, UnitOfElectricCurrent.AMPERE),
            (VOLTAGE_KEYS, SensorDeviceClass.VOLTAGE, UnitOfElectricPotential.VOLT),
        ],
    )
    async def test_per_phase_readings_are_measurements(
        self,
        hass: HomeAssistant,
        entity_ids: dict[str, str],
        keys: tuple[str, ...],
        device_class: SensorDeviceClass,
        unit: str,
    ) -> None:
        for key in keys:
            state = hass.states.get(entity_ids[key])
            assert state is not None
            assert state.attributes[ATTR_DEVICE_CLASS] == device_class
            assert state.attributes[ATTR_STATE_CLASS] == SensorStateClass.MEASUREMENT
            assert state.attributes[ATTR_UNIT_OF_MEASUREMENT] == unit

    async def test_voltage_is_diagnostic(
        self, hass: HomeAssistant, setup_integration: MockConfigEntry
    ) -> None:
        """Voltage is context for the other readings, not a headline measurement."""
        registry = er.async_get(hass)
        for key in VOLTAGE_KEYS:
            entry = registry.async_get(
                registry.async_get_entity_id("sensor", DOMAIN, f"{METER_ID}_{key}")
            )
            assert entry is not None
            assert entry.entity_category is EntityCategory.DIAGNOSTIC

    async def test_no_net_energy_sensor_exists(
        self, hass: HomeAssistant, setup_integration: MockConfigEntry
    ) -> None:
        """Net isn't monotonic, so it could never be TOTAL_INCREASING."""
        entities = er.async_entries_for_config_entry(
            er.async_get(hass), setup_integration.entry_id
        )
        assert not [e for e in entities if "net" in e.unique_id]


class TestValue:
    """Where the reading comes from, and what happens when there isn't one."""

    @pytest.mark.parametrize(
        ("key", "expected"),
        [("energy_import", 248718.155), ("energy_export", 18048.557)],
    )
    async def test_the_reading_comes_from_the_minute_bucket(
        self, hass: HomeAssistant, entity_ids: dict[str, str], key: str, expected: float
    ) -> None:
        state = hass.states.get(entity_ids[key])
        assert state is not None
        assert float(state.state) == expected

    @pytest.mark.parametrize(
        ("key", "expected"),
        [
            ("current_l1", 10.0),
            ("current_l2", 7.5),
            ("current_l3", -3.69),
            ("voltage_l1", 231.6),
            ("voltage_l2", 231.8),
            ("voltage_l3", 236.9),
        ],
    )
    async def test_per_phase_readings_come_from_the_real_time_bucket(
        self, hass: HomeAssistant, entity_ids: dict[str, str], key: str, expected: float
    ) -> None:
        """Current keeps its sign: negative is a phase exporting."""
        state = hass.states.get(entity_ids[key])
        assert state is not None
        assert float(state.state) == expected

    @pytest.mark.parametrize(
        ("key", "expected"), [("power_import", 4054.5), ("power_export", 874.2)]
    )
    async def test_power_splits_the_phases_by_direction(
        self, hass: HomeAssistant, entity_ids: dict[str, str], key: str, expected: float
    ) -> None:
        """This capture imports on L1/L2 while exporting on L3, so both are non-zero.

        Netting them would report 3180 W and hide the export entirely.
        """
        state = hass.states.get(entity_ids[key])
        assert state is not None
        assert float(state.state) == expected

    async def test_an_item_that_stops_reporting_goes_unavailable(
        self,
        hass: HomeAssistant,
        entity_ids: dict[str, str],
        setup_integration: MockConfigEntry,
        mock_client: AsyncMock,
    ) -> None:
        mock_client.async_get_latest_packets.return_value = {}

        await setup_integration.runtime_data.async_refresh()
        await hass.async_block_till_done()

        for entity_id in entity_ids.values():
            assert hass.states.get(entity_id).state == STATE_UNAVAILABLE

    @pytest.mark.parametrize(
        ("dropped", "lost", "kept"),
        [
            (BUCKET_MINUTE, ENERGY_KEYS, REALTIME_KEYS),
            (BUCKET_REALTIME, REALTIME_KEYS, ENERGY_KEYS),
        ],
    )
    async def test_a_missing_bucket_costs_only_its_own_sensors(
        self,
        hass: HomeAssistant,
        entity_ids: dict[str, str],
        setup_integration: MockConfigEntry,
        mock_client: AsyncMock,
        dropped: str,
        lost: tuple[str, ...],
        kept: tuple[str, ...],
    ) -> None:
        """The two buckets carry different readings and fail independently."""
        packets = mock_client.async_get_latest_packets.return_value
        stripped = {
            key: value
            for key, value in packets[METER_ID].packets.items()
            if key != dropped
        }
        mock_client.async_get_latest_packets.return_value = {
            METER_ID: replace(packets[METER_ID], packets=stripped)
        }

        await setup_integration.runtime_data.async_refresh()
        await hass.async_block_till_done()

        for key in lost:
            assert hass.states.get(entity_ids[key]).state == STATE_UNAVAILABLE
        for key in kept:
            assert hass.states.get(entity_ids[key]).state != STATE_UNAVAILABLE


class TestIdentity:
    """Devices and unique IDs."""

    async def test_only_the_meter_becomes_a_device(
        self, hass: HomeAssistant, setup_integration: MockConfigEntry
    ) -> None:
        """The capture holds two chargers and a stub alongside the meter."""
        devices = dr.async_entries_for_config_entry(
            dr.async_get(hass), setup_integration.entry_id
        )
        assert len(devices) == 1
        assert devices[0].identifiers == {(DOMAIN, str(METER_ID))}

    async def test_the_device_carries_what_the_item_reported(
        self, hass: HomeAssistant, setup_integration: MockConfigEntry
    ) -> None:
        device = dr.async_get(hass).async_get_device_by_identifier(
            (DOMAIN, str(METER_ID)), setup_integration.entry_id
        )
        assert device is not None
        assert device.manufacturer == "Perific"
        assert device.model == "EM2One"
        assert device.sw_version == "4.5.15"
        assert device.hw_version == "4.6"

    async def test_one_entity_per_meter_and_reading(
        self, hass: HomeAssistant, setup_integration: MockConfigEntry
    ) -> None:
        entities = er.async_entries_for_config_entry(
            er.async_get(hass), setup_integration.entry_id
        )
        assert sorted(entity.unique_id for entity in entities) == sorted(
            f"{METER_ID}_{key}" for key in KEYS
        )


class TestMonotonicityGuard:
    """A cumulative counter that falls is read as a meter reset.

    Home Assistant books the whole new value as one period's consumption, which
    corrupts exactly the long-term series this integration exists to build.
    """

    async def _poll(
        self,
        hass: HomeAssistant,
        setup_integration: MockConfigEntry,
        mock_client: AsyncMock,
        energy_import: float | None,
    ) -> None:
        """Serve one more packet carrying the given import register."""
        packets = mock_client.async_get_latest_packets.return_value
        entry = packets[METER_ID]
        minute = entry.packets[BUCKET_MINUTE]
        mock_client.async_get_latest_packets.return_value = {
            METER_ID: replace(
                entry,
                packets={
                    **entry.packets,
                    BUCKET_MINUTE: replace(
                        minute,
                        data=replace(minute.data, energy_import=energy_import),
                    ),
                },
            )
        }
        await setup_integration.runtime_data.async_refresh()
        await hass.async_block_till_done()

    def _state(self, hass: HomeAssistant, entity_ids: dict[str, str]) -> str:
        return hass.states.get(entity_ids["energy_import"]).state

    async def test_a_flat_register_passes_straight_through(
        self,
        hass: HomeAssistant,
        entity_ids: dict[str, str],
        setup_integration: MockConfigEntry,
        mock_client: AsyncMock,
    ) -> None:
        """Hours of an unchanged register are normal while the house exports."""
        await self._poll(hass, setup_integration, mock_client, 248718.155)

        assert float(self._state(hass, entity_ids)) == 248718.155

    async def test_a_single_fall_is_held(
        self,
        hass: HomeAssistant,
        entity_ids: dict[str, str],
        setup_integration: MockConfigEntry,
        mock_client: AsyncMock,
    ) -> None:
        await self._poll(hass, setup_integration, mock_client, 1000.0)

        assert float(self._state(hass, entity_ids)) == 248718.155

    async def test_a_fall_that_repeats_is_accepted(
        self,
        hass: HomeAssistant,
        entity_ids: dict[str, str],
        setup_integration: MockConfigEntry,
        mock_client: AsyncMock,
    ) -> None:
        """A replaced meter really does start again, so this cannot hold forever."""
        await self._poll(hass, setup_integration, mock_client, 1000.0)
        await self._poll(hass, setup_integration, mock_client, 1000.5)

        assert float(self._state(hass, entity_ids)) == 1000.5

    async def test_a_recovery_discards_the_held_reading(
        self,
        hass: HomeAssistant,
        entity_ids: dict[str, str],
        setup_integration: MockConfigEntry,
        mock_client: AsyncMock,
    ) -> None:
        """One stale packet followed by a good one must leave no trace."""
        await self._poll(hass, setup_integration, mock_client, 1000.0)
        await self._poll(hass, setup_integration, mock_client, 248718.200)

        assert float(self._state(hass, entity_ids)) == 248718.200

        await self._poll(hass, setup_integration, mock_client, 1000.0)
        assert float(self._state(hass, entity_ids)) == 248718.200

    async def test_the_baseline_survives_an_unavailable_period(
        self,
        hass: HomeAssistant,
        entity_ids: dict[str, str],
        setup_integration: MockConfigEntry,
        mock_client: AsyncMock,
    ) -> None:
        """Otherwise a gap would let the next stale packet through unchallenged."""
        await self._poll(hass, setup_integration, mock_client, None)
        assert self._state(hass, entity_ids) == STATE_UNAVAILABLE

        await self._poll(hass, setup_integration, mock_client, 1000.0)
        assert float(self._state(hass, entity_ids)) == 248718.155

    async def test_power_is_not_guarded(
        self,
        hass: HomeAssistant,
        entity_ids: dict[str, str],
        setup_integration: MockConfigEntry,
        mock_client: AsyncMock,
    ) -> None:
        """Only TOTAL_INCREASING counters are; a measurement may fall freely."""
        packets = mock_client.async_get_latest_packets.return_value
        entry = packets[METER_ID]
        realtime = entry.packets[BUCKET_REALTIME]
        mock_client.async_get_latest_packets.return_value = {
            METER_ID: replace(
                entry,
                packets={
                    **entry.packets,
                    BUCKET_REALTIME: replace(
                        realtime,
                        data=replace(realtime.data, current=(1.0, 1.0, 1.0)),
                    ),
                },
            )
        }
        await setup_integration.runtime_data.async_refresh()
        await hass.async_block_till_done()

        state = hass.states.get(entity_ids["power_import"])
        assert float(state.state) < 4054.5
