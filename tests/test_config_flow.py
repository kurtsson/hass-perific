"""Config flow: every path a user can take through it."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol
from homeassistant.config_entries import SOURCE_USER, ConfigFlowResult
from homeassistant.const import (
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_TOKEN,
    CONF_USERNAME,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.perific.api import (
    PerificAuthError,
    PerificConnectionError,
    PerificRateLimitError,
    PerificResponseError,
    TokenInfo,
)
from custom_components.perific.config_flow import OPTIONS_SCHEMA
from custom_components.perific.const import (
    CONF_TOKEN_VALID_TO,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)

from .conftest import ENTRY_DATA, PASSWORD, TOKEN, TOKEN_INFO, USERNAME

CREDENTIALS = {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}

LOGIN_FAILURES = [
    (PerificAuthError("rejected"), "invalid_auth"),
    (PerificConnectionError("unreachable"), "cannot_connect"),
    (PerificRateLimitError("slow down", retry_after=30.0), "rate_limited"),
    (PerificResponseError("nonsense", status=500), "unknown"),
    (RuntimeError("something else entirely"), "unknown"),
]


@pytest.fixture(autouse=True)
def _enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Home Assistant only loads custom_components when this fixture is active."""


@contextmanager
def _patch_login(
    side_effect: Exception | None = None, info: TokenInfo = TOKEN_INFO
) -> Iterator[None]:
    """Answer the flow's login attempt, and keep a created entry from loading."""
    client = AsyncMock()
    client.async_login.side_effect = side_effect
    client.async_login.return_value = info
    with (
        patch(
            "custom_components.perific.config_flow.EnegicClient", return_value=client
        ),
        patch("custom_components.perific.async_setup_entry", return_value=True),
    ):
        yield


async def _start_user_flow(hass: HomeAssistant) -> ConfigFlowResult:
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )


class TestUserFlow:
    """Adding an account from scratch."""

    async def test_credentials_that_work_create_an_entry(
        self, hass: HomeAssistant
    ) -> None:
        result = await _start_user_flow(hass)
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "user"

        with _patch_login():
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], CREDENTIALS
            )
            await hass.async_block_till_done()

        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["title"] == USERNAME
        assert result["data"] == ENTRY_DATA

    async def test_the_password_is_traded_for_a_token_and_not_kept(
        self, hass: HomeAssistant
    ) -> None:
        """The entry is what ends up in .storage, so the password must not reach it."""
        result = await _start_user_flow(hass)
        with _patch_login():
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], CREDENTIALS
            )
            await hass.async_block_till_done()

        assert CONF_PASSWORD not in result["data"]
        assert result["data"][CONF_TOKEN] == TOKEN
        assert result["result"].version == 2

    async def test_the_unique_id_is_the_lowercased_username(
        self, hass: HomeAssistant
    ) -> None:
        """So the same account can't be added twice under different capitalisation."""
        result = await _start_user_flow(hass)
        with _patch_login():
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_USERNAME: "User@Example.COM", CONF_PASSWORD: PASSWORD},
            )
            await hass.async_block_till_done()

        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["result"].unique_id == USERNAME

    @pytest.mark.parametrize(("failure", "expected"), LOGIN_FAILURES)
    async def test_a_failed_login_shows_the_form_again(
        self, hass: HomeAssistant, failure: Exception, expected: str
    ) -> None:
        result = await _start_user_flow(hass)
        with _patch_login(failure):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], CREDENTIALS
            )

        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": expected}

    async def test_the_flow_recovers_once_the_credentials_work(
        self, hass: HomeAssistant
    ) -> None:
        result = await _start_user_flow(hass)
        with _patch_login(PerificAuthError("rejected")):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], CREDENTIALS
            )
        assert result["errors"] == {"base": "invalid_auth"}

        with _patch_login():
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], CREDENTIALS
            )
            await hass.async_block_till_done()

        assert result["type"] is FlowResultType.CREATE_ENTRY

    async def test_the_same_account_twice_aborts(
        self, hass: HomeAssistant, config_entry: MockConfigEntry
    ) -> None:
        config_entry.add_to_hass(hass)
        result = await _start_user_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CREDENTIALS
        )

        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "already_configured"


class TestReauth:
    """Renewing credentials the API has stopped accepting."""

    async def test_a_fresh_token_is_written_back_to_the_entry(
        self, hass: HomeAssistant, config_entry: MockConfigEntry
    ) -> None:
        valid_to = dt_util.utcnow() + timedelta(days=365)
        renewed = TokenInfo(
            token="99999999-8888-7777-6666-555555555555", valid_to=valid_to
        )
        config_entry.add_to_hass(hass)
        result = await config_entry.start_reauth_flow(hass)
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "reauth_confirm"

        with _patch_login(info=renewed):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {CONF_PASSWORD: "a new password"}
            )
            await hass.async_block_till_done()

        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "reauth_successful"
        assert config_entry.data[CONF_TOKEN] == renewed.token
        assert config_entry.data[CONF_TOKEN_VALID_TO] == valid_to.isoformat()
        # Neither the username nor the new password is stored beyond the exchange.
        assert config_entry.data[CONF_USERNAME] == USERNAME
        assert CONF_PASSWORD not in config_entry.data

    @pytest.mark.parametrize(("failure", "expected"), LOGIN_FAILURES)
    async def test_a_failed_reauth_shows_the_form_again(
        self,
        hass: HomeAssistant,
        config_entry: MockConfigEntry,
        failure: Exception,
        expected: str,
    ) -> None:
        config_entry.add_to_hass(hass)
        result = await config_entry.start_reauth_flow(hass)

        with _patch_login(failure):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {CONF_PASSWORD: "still wrong"}
            )

        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": expected}
        assert config_entry.data[CONF_TOKEN] == TOKEN


class TestOptions:
    """The poll interval, the only thing tunable after setup."""

    async def test_an_entry_without_options_polls_at_the_default(
        self, hass: HomeAssistant, setup_integration: MockConfigEntry
    ) -> None:
        assert setup_integration.runtime_data.update_interval == timedelta(
            seconds=DEFAULT_SCAN_INTERVAL
        )

    async def test_a_saved_interval_reaches_the_coordinator(
        self,
        hass: HomeAssistant,
        setup_integration: MockConfigEntry,
        mock_client: AsyncMock,
    ) -> None:
        """Saving reloads the entry, which is what makes the new interval apply."""
        with patch("custom_components.perific.EnegicClient", return_value=mock_client):
            result = await hass.config_entries.options.async_init(
                setup_integration.entry_id
            )
            assert result["type"] is FlowResultType.FORM

            result = await hass.config_entries.options.async_configure(
                result["flow_id"], {CONF_SCAN_INTERVAL: 30}
            )
            await hass.async_block_till_done()

        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert setup_integration.options == {CONF_SCAN_INTERVAL: 30}
        assert setup_integration.runtime_data.update_interval == timedelta(seconds=30)

    @pytest.mark.parametrize(
        "seconds", [MIN_SCAN_INTERVAL - 1, MAX_SCAN_INTERVAL + 1, 0, -5]
    )
    def test_an_interval_outside_the_bounds_is_rejected(self, seconds: int) -> None:
        """Rate limits are unmeasured, so the floor is a guard rather than a hint."""
        with pytest.raises(vol.Invalid):
            OPTIONS_SCHEMA({CONF_SCAN_INTERVAL: seconds})
