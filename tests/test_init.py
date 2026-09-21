"""Setup, unload, and the failure semantics in docs/specs/perific-integration.md."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_TOKEN, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.perific.api import (
    PerificAuthError,
    PerificConnectionError,
    PerificRateLimitError,
    PerificResponseError,
    parse_latest_packets,
)
from custom_components.perific.const import (
    AUTH_FAILURES_BEFORE_REAUTH,
    CONF_TOKEN_VALID_TO,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)

from .conftest import PASSWORD, TOKEN, TOKEN_VALID_TO, USERNAME


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
        assert setup_integration.runtime_data.meters

    async def test_the_stored_token_is_used_without_signing_in_again(
        self, setup_integration: MockConfigEntry, mock_client: AsyncMock
    ) -> None:
        """Setup spends no credentials: the password was traded for a token once."""
        mock_client.async_login.assert_not_awaited()
        assert set(setup_integration.data) == {
            CONF_USERNAME,
            CONF_TOKEN,
            CONF_TOKEN_VALID_TO,
        }
        assert CONF_PASSWORD not in setup_integration.data

    async def test_unloading_releases_the_entry(
        self, hass: HomeAssistant, setup_integration: MockConfigEntry
    ) -> None:
        assert await hass.config_entries.async_unload(setup_integration.entry_id)
        await hass.async_block_till_done()
        assert setup_integration.state is ConfigEntryState.NOT_LOADED

    async def test_a_rejected_token_starts_reauth_rather_than_retrying(
        self, hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: AsyncMock
    ) -> None:
        mock_client.async_get_meters.side_effect = PerificAuthError("rejected")
        await _setup(hass, config_entry, mock_client)

        assert config_entry.state is ConfigEntryState.SETUP_ERROR
        assert len(_reauth_flows(hass)) == 1

    async def test_a_missing_token_starts_reauth(
        self, hass: HomeAssistant, mock_client: AsyncMock
    ) -> None:
        """An entry that migrated without one has to ask for the password."""
        entry = MockConfigEntry(
            domain=DOMAIN, unique_id=USERNAME, version=2, data={CONF_USERNAME: USERNAME}
        )
        await _setup(hass, entry, mock_client)

        assert entry.state is ConfigEntryState.SETUP_ERROR
        assert len(_reauth_flows(hass)) == 1

    async def test_an_expired_token_is_not_spent_on_a_certain_401(
        self, hass: HomeAssistant, mock_client: AsyncMock
    ) -> None:
        """The API is never called: the stored expiry already settles it."""
        entry = MockConfigEntry(
            domain=DOMAIN,
            unique_id=USERNAME,
            version=2,
            data={
                CONF_USERNAME: USERNAME,
                CONF_TOKEN: TOKEN,
                CONF_TOKEN_VALID_TO: (
                    dt_util.utcnow() - timedelta(minutes=1)
                ).isoformat(),
            },
        )
        await _setup(hass, entry, mock_client)

        assert entry.state is ConfigEntryState.SETUP_ERROR
        mock_client.async_get_meters.assert_not_awaited()
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

    async def test_a_single_rejection_does_not_stop_collection(
        self,
        hass: HomeAssistant,
        setup_integration: MockConfigEntry,
        mock_client: AsyncMock,
    ) -> None:
        """Home Assistant never reschedules after ConfigEntryAuthFailed.

        Escalating on the first 401 ends collection until someone answers the prompt,
        which is far too much to pay for one bad answer from the API.
        """
        coordinator = setup_integration.runtime_data
        mock_client.async_get_latest_packets.side_effect = PerificAuthError("rejected")

        await coordinator.async_refresh()
        await hass.async_block_till_done()

        assert not coordinator.last_update_success
        assert not _reauth_flows(hass)
        assert coordinator.auth_failures == 1

    async def test_a_rejection_that_repeats_triggers_reauth(
        self,
        hass: HomeAssistant,
        setup_integration: MockConfigEntry,
        mock_client: AsyncMock,
    ) -> None:
        """A token that is genuinely dead still has to reach the user."""
        coordinator = setup_integration.runtime_data
        mock_client.async_get_latest_packets.side_effect = PerificAuthError("expired")

        for _ in range(AUTH_FAILURES_BEFORE_REAUTH):
            await coordinator.async_refresh()
        await hass.async_block_till_done()

        assert not coordinator.last_update_success
        assert len(_reauth_flows(hass)) == 1

    async def test_a_success_forgives_earlier_rejections(
        self,
        hass: HomeAssistant,
        setup_integration: MockConfigEntry,
        mock_client: AsyncMock,
        packets_t0: Any,
    ) -> None:
        """Otherwise isolated 401s days apart would eventually add up to a prompt."""
        coordinator = setup_integration.runtime_data
        good = parse_latest_packets(packets_t0)

        for _ in range(AUTH_FAILURES_BEFORE_REAUTH - 1):
            mock_client.async_get_latest_packets.side_effect = PerificAuthError("blip")
            await coordinator.async_refresh()

            mock_client.async_get_latest_packets.side_effect = None
            mock_client.async_get_latest_packets.return_value = good
            await coordinator.async_refresh()
            assert coordinator.auth_failures == 0

        await hass.async_block_till_done()
        assert coordinator.last_update_success
        assert not _reauth_flows(hass)

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

    async def test_a_rate_limit_raises_a_repair_issue(
        self,
        hass: HomeAssistant,
        setup_integration: MockConfigEntry,
        mock_client: AsyncMock,
    ) -> None:
        """The coordinator logs once and then goes quiet, so the log is not enough."""
        coordinator = setup_integration.runtime_data
        mock_client.async_get_latest_packets.side_effect = PerificRateLimitError(
            "slow down", retry_after=42.0
        )

        await coordinator.async_refresh()

        issue = ir.async_get(hass).async_get_issue(DOMAIN, coordinator._issue_id)
        assert issue is not None
        assert issue.translation_key == "rate_limited"
        assert issue.severity is ir.IssueSeverity.WARNING
        assert issue.translation_placeholders == {"seconds": str(DEFAULT_SCAN_INTERVAL)}

    async def test_the_issue_clears_once_a_poll_gets_through(
        self,
        hass: HomeAssistant,
        setup_integration: MockConfigEntry,
        mock_client: AsyncMock,
        packets_t0: Any,
    ) -> None:
        coordinator = setup_integration.runtime_data
        mock_client.async_get_latest_packets.side_effect = PerificRateLimitError(
            "slow down"
        )
        await coordinator.async_refresh()
        assert ir.async_get(hass).async_get_issue(DOMAIN, coordinator._issue_id)

        mock_client.async_get_latest_packets.side_effect = None
        mock_client.async_get_latest_packets.return_value = parse_latest_packets(
            packets_t0
        )
        await coordinator.async_refresh()

        assert coordinator.last_update_success
        assert ir.async_get(hass).async_get_issue(DOMAIN, coordinator._issue_id) is None

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


class TestMigration:
    """Version 1 entries hold the password; version 2 holds a token instead."""

    async def test_the_password_is_spent_once_and_dropped(
        self,
        hass: HomeAssistant,
        legacy_config_entry: MockConfigEntry,
        mock_client: AsyncMock,
    ) -> None:
        await _setup(hass, legacy_config_entry, mock_client)

        mock_client.async_login.assert_awaited_once_with(USERNAME, PASSWORD)
        assert legacy_config_entry.version == 2
        assert legacy_config_entry.state is ConfigEntryState.LOADED
        assert legacy_config_entry.data == {
            CONF_USERNAME: USERNAME,
            CONF_TOKEN: TOKEN,
            CONF_TOKEN_VALID_TO: TOKEN_VALID_TO.isoformat(),
        }

    async def test_a_rejected_password_migrates_into_a_reauth_prompt(
        self,
        hass: HomeAssistant,
        legacy_config_entry: MockConfigEntry,
        mock_client: AsyncMock,
    ) -> None:
        """Better than leaving the entry on a password the API already refuses."""
        mock_client.async_login.side_effect = PerificAuthError("rejected")
        await _setup(hass, legacy_config_entry, mock_client)

        assert legacy_config_entry.version == 2
        assert CONF_PASSWORD not in legacy_config_entry.data
        assert len(_reauth_flows(hass)) == 1

    async def test_a_transient_failure_keeps_the_password_for_a_retry(
        self,
        hass: HomeAssistant,
        legacy_config_entry: MockConfigEntry,
        mock_client: AsyncMock,
    ) -> None:
        """The network being down is no reason to make the user type it again."""
        mock_client.async_login.side_effect = PerificConnectionError("down")
        await _setup(hass, legacy_config_entry, mock_client)

        assert legacy_config_entry.version == 1
        assert legacy_config_entry.data[CONF_PASSWORD] == PASSWORD
        assert legacy_config_entry.state is ConfigEntryState.MIGRATION_ERROR
        assert not _reauth_flows(hass)
