"""Constants for the Perific integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "perific"
MANUFACTURER: Final = "Perific"

# Rate limits are unverified and long-term statistics are hourly buckets, so there is
# nothing to gain from polling faster. See docs/api/enegic.md.
SCAN_INTERVAL: Final = timedelta(minutes=5)
