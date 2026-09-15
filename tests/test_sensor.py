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
    UnitOfEnergy,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.perific.api import BUCKET_MINUTE
from custom_components.perific.const import DOMAIN

METER_ID = 10004
UNIQUE_ID = f"{METER_ID}_energy_import"


@pytest.fixture(autouse=True)
def _enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Home Assistant only loads custom_components when this fixture is active."""


@pytest.fixture
def entity_id(hass: HomeAssistant, setup_integration: MockConfigEntry) -> str:
    """Look the entity up by unique id rather than guessing its entity id."""
    found = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, UNIQUE_ID)
    assert found is not None
    return found


class TestTyping:
    """The typing that decides whether statistics get recorded at all."""

    async def test_energy_import_is_typed_for_long_term_statistics(
        self, hass: HomeAssistant, entity_id: str
    ) -> None:
        state = hass.states.get(entity_id)
        assert state is not None
        assert state.attributes[ATTR_DEVICE_CLASS] == SensorDeviceClass.ENERGY
        assert state.attributes[ATTR_STATE_CLASS] == SensorStateClass.TOTAL_INCREASING
        assert state.attributes[ATTR_UNIT_OF_MEASUREMENT] == UnitOfEnergy.KILO_WATT_HOUR

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

    async def test_the_reading_comes_from_the_minute_bucket(
        self, hass: HomeAssistant, entity_id: str
    ) -> None:
        state = hass.states.get(entity_id)
        assert state is not None
        assert float(state.state) == 248718.155

    async def test_an_item_that_stops_reporting_goes_unavailable(
        self,
        hass: HomeAssistant,
        entity_id: str,
        setup_integration: MockConfigEntry,
        mock_client: AsyncMock,
    ) -> None:
        mock_client.async_get_latest_packets.return_value = {}

        await setup_integration.runtime_data.async_refresh()
        await hass.async_block_till_done()

        assert hass.states.get(entity_id).state == STATE_UNAVAILABLE

    async def test_a_packet_without_the_register_goes_unavailable(
        self,
        hass: HomeAssistant,
        entity_id: str,
        setup_integration: MockConfigEntry,
        mock_client: AsyncMock,
    ) -> None:
        """A missing field costs one entity, never the whole poll."""
        packets = mock_client.async_get_latest_packets.return_value
        stripped = {
            key: value
            for key, value in packets[METER_ID].packets.items()
            if key != BUCKET_MINUTE
        }
        mock_client.async_get_latest_packets.return_value = {
            METER_ID: replace(packets[METER_ID], packets=stripped)
        }

        await setup_integration.runtime_data.async_refresh()
        await hass.async_block_till_done()

        assert hass.states.get(entity_id).state == STATE_UNAVAILABLE


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
        assert [entity.unique_id for entity in entities] == [UNIQUE_ID]
