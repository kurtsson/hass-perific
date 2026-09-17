"""Diagnostics for a config entry.

What a bug report needs is the raw packets: nearly every failure mode here is the API
returning something the parser did not expect, and the parsed sensors alone don't show
that.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_TOKEN, CONF_USERNAME

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .coordinator import PerificConfigEntry

# Item IDs are kept: they tie the packets to the meters and are meaningless off the
# account. A MAC address identifies hardware to anyone reading a public issue. The
# token's expiry stays visible — it is what explains a reauth prompt. CONF_PASSWORD
# is only reachable on an entry that has not migrated to token storage yet.
TO_REDACT = {CONF_USERNAME, CONF_PASSWORD, CONF_TOKEN, "mac_address"}


async def async_get_config_entry_diagnostics(
    _hass: HomeAssistant, entry: PerificConfigEntry
) -> dict[str, Any]:
    """Dump the entry, the coordinator's health, and the last packets seen."""
    coordinator = entry.runtime_data
    interval = coordinator.update_interval

    return async_redact_data(
        {
            "entry": {
                "data": dict(entry.data),
                "options": dict(entry.options),
            },
            "coordinator": {
                "last_update_success": coordinator.last_update_success,
                "last_exception": str(coordinator.last_exception)
                if coordinator.last_exception
                else None,
                "update_interval_seconds": interval.total_seconds()
                if interval
                else None,
                "meter_count": len(coordinator.meters),
            },
            "meters": [asdict(meter) for meter in coordinator.meters],
            "packets": {
                str(item_id): asdict(packets)
                for item_id, packets in (coordinator.data or {}).items()
            },
        },
        TO_REDACT,
    )
