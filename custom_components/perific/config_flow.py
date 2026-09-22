"""Config flow for Perific.

The password is exchanged for a token during setup and is not kept. The token the
API mints is valid for a year; when it is finally rejected the reauth step below
asks for the password again and mints a replacement.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import (
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_TOKEN,
    CONF_USERNAME,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    StatisticSelector,
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
from .const import (
    CONF_ENERGY_TAX,
    CONF_EXPORT_PREMIUM,
    CONF_EXPORT_PRICE_ENTITY,
    CONF_PRICE_ENTITY,
    CONF_PRICE_MARKUP,
    CONF_SOLAR_STATISTIC,
    CONF_TOKEN_VALID_TO,
    CONF_VAT_PERCENT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.config_entries import ConfigEntry

    from .api import TokenInfo

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

# Per-kWh money, in whatever currency the price entity reports. `step="any"`
# rather than a figure: these are quoted in öre, so 42.80 öre/kWh is entered as
# 0.4280, and the selector's minimum step is 0.001.
_PRICE_FIELD = NumberSelector(
    NumberSelectorConfig(min=0, step="any", mode=NumberSelectorMode.BOX)
)

OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): vol.All(
            NumberSelector(
                NumberSelectorConfig(
                    min=MIN_SCAN_INTERVAL,
                    max=MAX_SCAN_INTERVAL,
                    step=5,
                    unit_of_measurement="s",
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Coerce(int),
        ),
        # Cost is optional: leave the price entity empty and no cost series is
        # imported at all. Every amount below is per kWh and excludes VAT,
        # which is applied once to their sum — the published Swedish energy tax
        # of 53.50 öre/kWh includes it, so the figure to enter here is 42.80.
        vol.Optional(CONF_PRICE_ENTITY): vol.Any(
            EntitySelector(EntitySelectorConfig(domain="sensor")), None
        ),
        vol.Optional(CONF_PRICE_MARKUP, default=0.0): _PRICE_FIELD,
        vol.Optional(CONF_ENERGY_TAX, default=0.0): _PRICE_FIELD,
        vol.Optional(CONF_VAT_PERCENT, default=0.0): NumberSelector(
            NumberSelectorConfig(
                min=0,
                max=100,
                step=0.1,
                unit_of_measurement="%",
                mode=NumberSelectorMode.BOX,
            )
        ),
        vol.Optional(CONF_EXPORT_PRICE_ENTITY): vol.Any(
            EntitySelector(EntitySelectorConfig(domain="sensor")), None
        ),
        vol.Optional(CONF_EXPORT_PREMIUM, default=0.0): _PRICE_FIELD,
        # A statistic rather than an entity: what the panels produced is only
        # kept usefully far back as long-term statistics, and the inverter
        # integrations that write them do so under ids with no entity at all.
        vol.Optional(CONF_SOLAR_STATISTIC): vol.Any(StatisticSelector(), None),
    }
)


def token_entry_data(username: str, info: TokenInfo) -> dict[str, Any]:
    """Build the stored entry data. The password is deliberately not part of it."""
    return {
        CONF_USERNAME: username,
        CONF_TOKEN: info.token,
        CONF_TOKEN_VALID_TO: info.valid_to.isoformat() if info.valid_to else None,
    }


class PerificConfigFlow(ConfigFlow, domain=DOMAIN):
    """Take a username and password, and trade them for a token."""

    VERSION = 2

    @staticmethod
    @callback
    def async_get_options_flow(_config_entry: ConfigEntry) -> PerificOptionsFlow:
        """Return the options flow for this entry."""
        return PerificOptionsFlow()

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

            info = await self._async_login(username, user_input[CONF_PASSWORD], errors)
            if info is not None:
                return self.async_create_entry(
                    title=username, data=token_entry_data(username, info)
                )

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )

    async def async_step_reauth(
        self, _entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle a token that the API has stopped accepting."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the password again, against the entry's existing username."""
        entry = self._get_reauth_entry()
        username = entry.data[CONF_USERNAME]
        errors: dict[str, str] = {}
        if user_input is not None:
            info = await self._async_login(username, user_input[CONF_PASSWORD], errors)
            if info is not None:
                return self.async_update_reload_and_abort(
                    entry, data_updates=token_entry_data(username, info)
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_REAUTH_SCHEMA,
            description_placeholders={CONF_USERNAME: username},
            errors=errors,
        )

    async def _async_login(
        self, username: str, password: str, errors: dict[str, str]
    ) -> TokenInfo | None:
        """Mint a token, or record the error key for why it could not be minted."""
        client = EnegicClient(async_get_clientsession(self.hass))
        try:
            info = await client.async_login(username, password)
        except PerificAuthError:
            errors["base"] = "invalid_auth"
        except PerificConnectionError:
            errors["base"] = "cannot_connect"
        except PerificRateLimitError:
            errors["base"] = "rate_limited"
        except PerificError:
            _LOGGER.exception("Unexpected response from the Perific API")
            errors["base"] = "unknown"
        except Exception:
            _LOGGER.exception("Unexpected error signing in to Perific")
            errors["base"] = "unknown"
        else:
            return info
        return None


class PerificOptionsFlow(OptionsFlow):
    """Lets the poll interval be tuned after setup."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the poll interval, prefilled with whatever is in force."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                OPTIONS_SCHEMA, self.config_entry.options
            ),
        )
