"""Polls the Enegic API on a timer and owns the failure semantics.

The mapping from client exceptions onto Home Assistant's is in
``docs/specs/perific-integration.md``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

# ItemPackets parameterises the coordinator's base class, so it is needed at runtime.
from .api import ItemPackets, PerificAuthError, PerificError, PerificRateLimitError
from .const import DOMAIN, SCAN_INTERVAL

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .api import EnegicClient, Item

_LOGGER = logging.getLogger(__name__)

type PerificConfigEntry = ConfigEntry[PerificCoordinator]


class PerificCoordinator(DataUpdateCoordinator[dict[int, ItemPackets]]):
    """Fetches the latest packets for every meter on the account."""

    config_entry: PerificConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: PerificConfigEntry,
        client: EnegicClient,
    ) -> None:
        """Set up the poll and the list of meters it will cover."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=SCAN_INTERVAL,
            # The models compare by value, so listeners only wake on a real change.
            always_update=False,
        )
        self.client = client
        self.meters: list[Item] = []

    async def _async_setup(self) -> None:
        """Log in and discover meters, once per setup of the config entry.

        Re-minting the token here rather than persisting it costs one request per
        restart and removes a class of stale-credential-on-disk bugs.
        """
        data = self.config_entry.data
        try:
            await self.client.async_login(data[CONF_USERNAME], data[CONF_PASSWORD])
            self.meters = await self.client.async_get_meters()
        except PerificAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except PerificError as err:
            raise UpdateFailed(str(err)) from err

        if not self.meters:
            raise UpdateFailed("The account has no Perific meters on it")

        _LOGGER.debug("Tracking %d meter(s)", len(self.meters))

    async def _async_update_data(self) -> dict[int, ItemPackets]:
        """Fetch one round of packets for every item on the account."""
        try:
            return await self.client.async_get_latest_packets()
        except PerificAuthError as err:
            # Never wrapped in UpdateFailed. Wrapped, the coordinator reads it as a
            # transient failure, reauth never triggers, and updates stop silently.
            raise ConfigEntryAuthFailed(str(err)) from err
        except PerificRateLimitError as err:
            raise UpdateFailed(str(err), retry_after=err.retry_after) from err
        except PerificError as err:
            raise UpdateFailed(str(err)) from err
