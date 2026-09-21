"""Polls the Enegic API on a timer and owns the failure semantics.

The mapping from client exceptions onto Home Assistant's is in
``docs/specs/perific-integration.md``.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING

from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

# ItemPackets parameterises the coordinator's base class, so it is needed at runtime.
from .api import ItemPackets, PerificAuthError, PerificError, PerificRateLimitError
from .const import AUTH_FAILURES_BEFORE_REAUTH, DEFAULT_SCAN_INTERVAL, DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .api import EnegicClient, Item

_LOGGER = logging.getLogger(__name__)

type PerificConfigEntry = ConfigEntry[PerificCoordinator]


def scan_interval(entry: PerificConfigEntry) -> timedelta:
    """Read the poll interval off the entry, falling back to the default."""
    seconds = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    return timedelta(seconds=seconds)


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
            update_interval=scan_interval(entry),
            # The models compare by value, so listeners only wake on a real change.
            always_update=False,
        )
        self.client = client
        self.meters: list[Item] = []
        self._rate_limited = False
        self.auth_failures = 0

    async def _async_setup(self) -> None:
        """Discover the account's meters, once per setup of the config entry.

        A rejection here escalates immediately, unlike the polling path below. Setup
        failures retry with a fresh coordinator, so a counter could never reach its
        threshold and a genuinely dead token would retry forever without ever asking
        for a password.
        """
        try:
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
            packets = await self.client.async_get_latest_packets()
        except PerificAuthError as err:
            # ConfigEntryAuthFailed is the only exception the coordinator does not
            # reschedule after, so raising it ends polling until someone answers the
            # prompt. Reserve that for a rejection that repeats; a lone 401 is worth
            # one more poll first.
            self.auth_failures += 1
            if self.auth_failures >= AUTH_FAILURES_BEFORE_REAUTH:
                raise ConfigEntryAuthFailed(str(err)) from err
            raise UpdateFailed(str(err)) from err
        except PerificRateLimitError as err:
            self._async_rate_limited()
            raise UpdateFailed(
                f"{err}. Raise the poll interval in the integration options.",
                retry_after=err.retry_after,
            ) from err
        except PerificError as err:
            raise UpdateFailed(str(err)) from err

        self.auth_failures = 0
        self._async_rate_limit_cleared()
        return packets

    @property
    def _issue_id(self) -> str:
        """One issue per config entry, so two accounts report independently."""
        return f"rate_limited_{self.config_entry.entry_id}"

    @callback
    def _async_rate_limited(self) -> None:
        """Surface throttling in Repairs.

        The coordinator logs a failure only on the transition out of success, so
        sustained throttling leaves one line in the log and nothing in the interface.
        This is user-correctable, so it needs somewhere a user will look.
        """
        if self._rate_limited:
            return
        self._rate_limited = True
        seconds = (
            int(self.update_interval.total_seconds()) if self.update_interval else 0
        )
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self._issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="rate_limited",
            translation_placeholders={"seconds": str(seconds)},
        )

    @callback
    def _async_rate_limit_cleared(self) -> None:
        """Withdraw the issue once a poll gets through again."""
        if not self._rate_limited:
            return
        self._rate_limited = False
        ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)
