"""The Perific integration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import voluptuous as vol
from homeassistant.const import CONF_PASSWORD, CONF_TOKEN, CONF_USERNAME, Platform
from homeassistant.core import SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_change
from homeassistant.util import dt as dt_util
from homeassistant.util.yaml import dump

from .api import EnegicClient, PerificAuthError, PerificError
from .config_flow import PerificConfigFlow, entry_data
from .const import (
    ATTR_START,
    CONF_PRICE_ENTITY,
    CONF_SOLAR_STATISTIC,
    DOMAIN,
    HISTORY_RUN_AT_MINUTE,
    SERVICE_GET_DASHBOARD,
    SERVICE_IMPORT_HISTORY,
)
from .coordinator import PerificCoordinator
from .dashboard import dashboard_config
from .history import HistoryImporter, async_register_names

if TYPE_CHECKING:
    from datetime import datetime

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse
    from homeassistant.helpers.typing import ConfigType

    from .api import Item
    from .coordinator import PerificConfigEntry

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]

IMPORT_HISTORY_SCHEMA = vol.Schema({vol.Optional(ATTR_START): cv.datetime})

# `async_setup` exists only to register the service, so there is nothing to
# configure in YAML. Without this, hassfest flags the integration for defining
# `async_setup` with no schema.
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, _config: ConfigType) -> bool:
    """Register the integration-wide service.

    Registered here rather than per entry: the service spans every entry, and
    re-registering it on each reload would rebind the handler.
    """

    async def async_handle_import(call: ServiceCall) -> None:
        start = call.data.get(ATTR_START)
        if start is not None:
            start = dt_util.as_utc(start)
        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            await HistoryImporter(hass, entry).async_import_since(start)

    hass.services.async_register(
        DOMAIN,
        SERVICE_IMPORT_HISTORY,
        async_handle_import,
        schema=IMPORT_HISTORY_SCHEMA,
    )

    async def async_handle_dashboard(_call: ServiceCall) -> ServiceResponse:
        """Hand back a dashboard for whichever series this instance writes."""
        names = await async_register_names(hass)
        meters: list[Item] = []
        solar_statistic: str | None = None
        cost = False
        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            meters.extend(entry.runtime_data.meters)
            solar_statistic = solar_statistic or entry.options.get(CONF_SOLAR_STATISTIC)
            cost = cost or bool(entry.options.get(CONF_PRICE_ENTITY))
        if not meters:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="no_meters"
            )

        config = dashboard_config(
            meters,
            names,
            hass.config.language,
            solar_statistic=solar_statistic,
            cost=cost,
        )
        # Both forms, because Home Assistant has two YAML editors that take
        # different shapes: the dashboard's raw editor wants `views`, a single
        # view's editor wants the view on its own.
        return {
            "dashboard": dump(config),
            "view": dump(config["views"][0]),
        }

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_DASHBOARD,
        async_handle_dashboard,
        supports_response=SupportsResponse.ONLY,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: PerificConfigEntry) -> bool:
    """Set up Perific from a config entry."""
    client = EnegicClient(
        async_get_clientsession(hass), token=entry.data.get(CONF_TOKEN)
    )
    coordinator = PerificCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    importer = HistoryImporter(hass, entry)

    async def async_scheduled_import(now: datetime) -> None:
        # The listener signature returns None; async_run's count is for the
        # service and the tests.
        await importer.async_run(now)

    entry.async_on_unload(
        async_track_time_change(
            hass, async_scheduled_import, minute=HISTORY_RUN_AT_MINUTE, second=0
        )
    )
    # Once at startup as well, so an instance that was down for hours catches up
    # immediately rather than at the next hour mark. A first import walks the
    # whole account, so it must not hold up setup.
    entry.async_create_background_task(
        hass, importer.async_run(), name=f"{DOMAIN} history import"
    )
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate a version 1 entry, which held no token.

    Version 1 stored the username and password and re-minted a token on every setup.
    Version 2 stores a token alongside them and only re-mints when it stops working.
    """
    if entry.version > PerificConfigFlow.VERSION:
        return False
    if entry.version == 1:
        client = EnegicClient(async_get_clientsession(hass))
        username = entry.data[CONF_USERNAME]
        password = entry.data[CONF_PASSWORD]
        try:
            info = await client.async_login(username, password)
        except PerificAuthError:
            # Migrate anyway, without a token or the password: setup then raises
            # ConfigEntryAuthFailed and the user is asked for the password once,
            # rather than being left on one we already know the API rejects.
            _LOGGER.warning(
                "Stored Perific credentials were rejected; reauthentication required"
            )
            data = {CONF_USERNAME: username}
        except PerificError as err:
            # Transient. Let Home Assistant retry the migration rather than forcing
            # a reauth the user does not actually need.
            _LOGGER.warning("Could not migrate the Perific entry yet: %s", err)
            return False
        else:
            data = entry_data(username, password, info)

        hass.config_entries.async_update_entry(entry, data=data, version=2)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: PerificConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
