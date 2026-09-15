"""Config flow for Perific.

Credentials are exchanged for a token on every setup rather than persisted, so the
only thing stored on the entry is the username and password. Annual expiry is handled
by the reauth step below.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    EnegicClient,
    PerificAuthError,
    PerificConnectionError,
    PerificError,
    PerificRateLimitError,
)
from .const import DOMAIN

if TYPE_CHECKING:
    from collections.abc import Mapping

_LOGGER = logging.getLogger(__name__)

# A plain `str` renders as a visible text box, which would show the password as it is
# typed. The selector is what marks the field for masking and password autofill.
_PASSWORD_FIELD = TextSelector(
    TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): TextSelector(
            TextSelectorConfig(type=TextSelectorType.TEXT, autocomplete="username")
        ),
        vol.Required(CONF_PASSWORD): _PASSWORD_FIELD,
    }
)

STEP_REAUTH_SCHEMA = vol.Schema({vol.Required(CONF_PASSWORD): _PASSWORD_FIELD})


class PerificConfigFlow(ConfigFlow, domain=DOMAIN):
    """Take a username and password, and prove they work before creating an entry."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            username = user_input[CONF_USERNAME]
            # Matched case-insensitively so the same account can't be added twice
            # under a differently capitalised username.
            await self.async_set_unique_id(username.lower())
            self._abort_if_unique_id_configured()

            error = await self._async_check_credentials(
                username, user_input[CONF_PASSWORD]
            )
            if error is None:
                return self.async_create_entry(title=username, data=user_input)
            errors["base"] = error

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle a token that the API has stopped accepting."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the password again, against the entry's existing username."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            error = await self._async_check_credentials(
                entry.data[CONF_USERNAME], user_input[CONF_PASSWORD]
            )
            if error is None:
                return self.async_update_reload_and_abort(
                    entry, data_updates=user_input
                )
            errors["base"] = error

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_REAUTH_SCHEMA,
            description_placeholders={CONF_USERNAME: entry.data[CONF_USERNAME]},
            errors=errors,
        )

    async def _async_check_credentials(
        self, username: str, password: str
    ) -> str | None:
        """Return the error key for a failed login, or None when it worked."""
        client = EnegicClient(async_get_clientsession(self.hass))
        try:
            await client.async_login(username, password)
        except PerificAuthError:
            return "invalid_auth"
        except PerificConnectionError:
            return "cannot_connect"
        except PerificRateLimitError:
            return "rate_limited"
        except PerificError:
            _LOGGER.exception("Unexpected response from the Perific API")
            return "unknown"
        except Exception:
            _LOGGER.exception("Unexpected error signing in to Perific")
            return "unknown"
        return None
