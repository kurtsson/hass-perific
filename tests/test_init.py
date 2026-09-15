"""Setup, unload, and the failure semantics in docs/specs/perific-integration.md."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.perific.api import (
    PerificAuthError,
    PerificConnectionError,
    PerificRateLimitError,
    PerificResponseError,
)
from custom_components.perific.const import DOMAIN

from .conftest import PASSWORD, USERNAME


@pytest.fixture(autouse=True)
def _enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Home Assistant only loads custom_components when this fixture is active."""


async def _setup(
    hass: HomeAssistant, entry: MockConfigEntry, client: AsyncMock
) -> None:
    entry.add_to_hass(hass)
    with patch("custom_components.perific.EnegicClient", return_value=client):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()


def _reauth_flows(hass: HomeAssistant) -> list[dict]:
    return [
        flow
        for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        if flow["context"].get("source") == SOURCE_REAUTH
    ]


class TestSetup:
    """Bringing a config entry up, and the ways that can fail."""

    async def test_a_working_account_loads(
        self, setup_integration: MockConfigEntry, mock_client: AsyncMock
    ) -> None:
        assert setup_integration.state is ConfigEntryState.LOADED
        mock_client.async_login.assert_awaited_once_with(USERNAME, PASSWORD)
        assert setup_integration.runtime_data.meters

    async def test_credentials_are_exchanged_on_every_setup(
        self, setup_integration: MockConfigEntry
    ) -> None:
        """No token is persisted, so nothing stale can be loaded from disk."""
        assert set(setup_integration.data) == {CONF_USERNAME, CONF_PASSWORD}

    async def test_unloading_releases_the_entry(
        self, hass: HomeAssistant, setup_integration: MockConfigEntry
    ) -> None:
        assert await hass.config_entries.async_unload(setup_integration.entry_id)
        await hass.async_block_till_done()
        assert setup_integration.state is ConfigEntryState.NOT_LOADED

    async def test_bad_credentials_start_reauth_rather_than_retrying(
        self, hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: AsyncMock
    ) -> None:
        mock_client.async_login.side_effect = PerificAuthError("rejected")
        await _setup(hass, config_entry, mock_client)

        assert config_entry.state is ConfigEntryState.SETUP_ERROR
        assert len(_reauth_flows(hass)) == 1

    @pytest.mark.parametrize(
        "failure",
        [
            PerificConnectionError("unreachable"),
            PerificRateLimitError("slow down", retry_after=30.0),
            PerificResponseError("nonsense", status=500),
        ],
    )
    async def test_a_reachability_problem_is_retried(
        self,
        hass: HomeAssistant,
        config_entry: MockConfigEntry,
        mock_client: AsyncMock,
        failure: Exception,
    ) -> None:
        mock_client.async_get_meters.side_effect = failure
        await _setup(hass, config_entry, mock_client)

        assert config_entry.state is ConfigEntryState.SETUP_RETRY
        assert not _reauth_flows(hass)

    async def test_an_account_with_no_meter_is_retried(
        self, hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: AsyncMock
    ) -> None:
        """A real account shouldn't hit this, so retrying beats failing outright."""
        mock_client.async_get_meters.return_value = []
        await _setup(hass, config_entry, mock_client)

        assert config_entry.state is ConfigEntryState.SETUP_RETRY


class TestPolling:
    """What each client failure does to an entry that is already running."""

    async def test_an_expired_token_triggers_reauth(
        self,
        hass: HomeAssistant,
        setup_integration: MockConfigEntry,
        mock_client: AsyncMock,
    ) -> None:
        """Wrapped in UpdateFailed this would never fire, and updates would just stop."""
        coordinator = setup_integration.runtime_data
        mock_client.async_get_latest_packets.side_effect = PerificAuthError("expired")

        await coordinator.async_refresh()
        await hass.async_block_till_done()

        assert not coordinator.last_update_success
        assert len(_reauth_flows(hass)) == 1

    async def test_a_rate_limit_carries_its_retry_hint(
        self,
        hass: HomeAssistant,
        setup_integration: MockConfigEntry,
        mock_client: AsyncMock,
    ) -> None:
        coordinator = setup_integration.runtime_data
        mock_client.async_get_latest_packets.side_effect = PerificRateLimitError(
            "slow down", retry_after=42.0
        )

        await coordinator.async_refresh()

        assert not coordinator.last_update_success
        assert coordinator.last_exception.retry_after == 42.0
        assert not _reauth_flows(hass)

    async def test_a_connection_error_only_marks_the_update_failed(
        self,
        hass: HomeAssistant,
        setup_integration: MockConfigEntry,
        mock_client: AsyncMock,
    ) -> None:
        coordinator = setup_integration.runtime_data
        mock_client.async_get_latest_packets.side_effect = PerificConnectionError(
            "down"
        )

        await coordinator.async_refresh()

        assert not coordinator.last_update_success
        assert setup_integration.state is ConfigEntryState.LOADED
        assert not _reauth_flows(hass)
