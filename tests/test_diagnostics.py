"""Diagnostics, above all what they must not contain."""

from __future__ import annotations

import json

import pytest
from homeassistant.components.diagnostics import REDACTED
from homeassistant.core import HomeAssistant
from homeassistant.helpers.json import ExtendedJSONEncoder
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.perific.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .conftest import PASSWORD, USERNAME

METER_ID = 10004


@pytest.fixture(autouse=True)
def _enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Home Assistant only loads custom_components when this fixture is active."""


class TestRedaction:
    """Diagnostics get pasted into public issues."""

    async def test_the_credentials_never_appear(
        self, hass: HomeAssistant, setup_integration: MockConfigEntry
    ) -> None:
        result = await async_get_config_entry_diagnostics(hass, setup_integration)

        assert result["entry"]["data"]["username"] == REDACTED
        assert result["entry"]["data"]["password"] == REDACTED
        serialised = json.dumps(result, cls=ExtendedJSONEncoder)
        assert PASSWORD not in serialised
        assert USERNAME not in serialised

    async def test_the_mac_address_is_redacted(
        self, hass: HomeAssistant, setup_integration: MockConfigEntry
    ) -> None:
        result = await async_get_config_entry_diagnostics(hass, setup_integration)

        assert result["meters"][0]["mac_address"] == REDACTED


class TestContent:
    """What a bug report actually needs to be useful."""

    async def test_the_raw_packets_are_included(
        self, hass: HomeAssistant, setup_integration: MockConfigEntry
    ) -> None:
        """Most failures here are the API returning an unexpected shape."""
        result = await async_get_config_entry_diagnostics(hass, setup_integration)

        packets = result["packets"][str(METER_ID)]["packets"]
        assert "PhaseMinute" in packets
        assert packets["PhaseMinute"]["data"]["energy_import"] == 248718.155

    async def test_the_coordinator_health_is_included(
        self, hass: HomeAssistant, setup_integration: MockConfigEntry
    ) -> None:
        result = await async_get_config_entry_diagnostics(hass, setup_integration)

        assert result["coordinator"]["last_update_success"] is True
        assert result["coordinator"]["update_interval_seconds"] == 60.0
        assert result["coordinator"]["meter_count"] == 1

    async def test_the_payload_survives_the_diagnostics_encoder(
        self, hass: HomeAssistant, setup_integration: MockConfigEntry
    ) -> None:
        """Packet timestamps are datetimes, which plain json.dumps would reject."""
        result = await async_get_config_entry_diagnostics(hass, setup_integration)

        json.dumps(result, cls=ExtendedJSONEncoder)
