"""Client for the Enegic API (api.enegic.com), which Perific devices report to.

Vendored rather than installed from PyPI. It must not import from ``homeassistant`` —
that constraint is what keeps this package liftable into a standalone library, and it
keeps Home Assistant concerns out of here.

See ``docs/api/enegic.md`` for the endpoint and field reference.
"""

from .client import BASE_URL, EnegicClient
from .exceptions import (
    PerificAuthError,
    PerificConnectionError,
    PerificError,
    PerificRateLimitError,
    PerificResponseError,
)
from .models import (
    BUCKET_DAY,
    BUCKET_HOUR,
    BUCKET_MINUTE,
    BUCKET_REALTIME,
    Item,
    ItemPackets,
    Packet,
    PhaseData,
    TokenInfo,
    parse_items,
    parse_latest_packets,
)

__all__ = [
    "BASE_URL",
    "BUCKET_DAY",
    "BUCKET_HOUR",
    "BUCKET_MINUTE",
    "BUCKET_REALTIME",
    "EnegicClient",
    "Item",
    "ItemPackets",
    "Packet",
    "PerificAuthError",
    "PerificConnectionError",
    "PerificError",
    "PerificRateLimitError",
    "PerificResponseError",
    "PhaseData",
    "TokenInfo",
    "parse_items",
    "parse_latest_packets",
]
