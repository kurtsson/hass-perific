"""Config flow: every path a user can take through it."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.perific.api import (
    PerificAuthError,
    PerificConnectionError,
    PerificRateLimitError,
    PerificResponseError,
)
from custom_components.perific.const import DOMAIN

from .conftest import PASSWORD, USERNAME

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
def _patch_login(side_effect: Exception | None = None) -> Iterator[None]:
    """Answer the flow's login attempt, and keep a created entry from loading."""
    client = AsyncMock()
    client.async_login.side_effect = side_effect
    with (
        patch(
            "custom_components.perific.config_flow.EnegicClient", return_value=client
        ),
        patch("custom_components.perific.async_setup_entry", return_value=True),
    ):
        yield


async def _start_user_flow(hass: HomeAssistant) -> dict[str, Any]:
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
        assert result["data"] == CREDENTIALS

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

    async def test_a_new_password_is_written_back_to_the_entry(
        self, hass: HomeAssistant, config_entry: MockConfigEntry
    ) -> None:
        config_entry.add_to_hass(hass)
        result = await config_entry.start_reauth_flow(hass)
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "reauth_confirm"

        with _patch_login():
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {CONF_PASSWORD: "a new password"}
            )
            await hass.async_block_till_done()

        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "reauth_successful"
        assert config_entry.data[CONF_PASSWORD] == "a new password"
        # The username is never re-asked for, so it must survive untouched.
        assert config_entry.data[CONF_USERNAME] == USERNAME

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
        assert config_entry.data[CONF_PASSWORD] == PASSWORD
