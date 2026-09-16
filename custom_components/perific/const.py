"""Constants for the Perific integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "perific"
MANUFACTURER: Final = "Perific"

# Seconds. One minute matches the cadence of the energy registers, which only advance
# once a minute; the real-time bucket moves every ~10 s, so a shorter interval buys
# fresher power and current readings and nothing else. Rate limits are unmeasured,
# which is why the floor is not the device's own 10 s. See docs/api/enegic.md.
DEFAULT_SCAN_INTERVAL: Final = 60
MIN_SCAN_INTERVAL: Final = 15
MAX_SCAN_INTERVAL: Final = 3600
