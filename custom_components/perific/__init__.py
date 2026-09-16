"""The Perific integration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.const import Platform
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import EnegicClient
from .coordinator import PerificCoordinator

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .coordinator import PerificConfigEntry

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: PerificConfigEntry) -> bool:
    """Set up Perific from a config entry."""
    client = EnegicClient(async_get_clientsession(hass))
    coordinator = PerificCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_options_updated(
    hass: HomeAssistant, entry: PerificConfigEntry
) -> None:
    """Rebuild the entry so a new poll interval takes effect.

    Assigning ``update_interval`` on a live coordinator stores the value without
    rescheduling the pending refresh, so the change would not apply until after the
    next poll.
    """
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: PerificConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
