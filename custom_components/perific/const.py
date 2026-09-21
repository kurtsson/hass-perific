"""Constants for the Perific integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "perific"
MANUFACTURER: Final = "Perific"

CONF_TOKEN_VALID_TO: Final = "token_valid_to"

# Seconds. One minute matches the cadence of the energy registers, which only advance
# once a minute; the real-time bucket moves every ~10 s, so a shorter interval buys
# fresher power and current readings and nothing else. Rate limits are unmeasured,
# which is why the floor is not the device's own 10 s. See docs/api/enegic.md.
DEFAULT_SCAN_INTERVAL: Final = 60
MIN_SCAN_INTERVAL: Final = 15
MAX_SCAN_INTERVAL: Final = 3600

# A meter that has gone quiet still serves its last packets, so freshness has to be
# read off the timestamps rather than off the poll succeeding. Five minutes is four
# missed minute packets — beyond any plausible jitter in the vendor's own pipeline.
STALE_AFTER: Final = timedelta(minutes=5)

# Consecutive rejections before the user is asked to sign in again. Home Assistant
# stops polling the moment ConfigEntryAuthFailed is raised, so a token that is merely
# being rejected intermittently must not end collection on its first bad answer.
AUTH_FAILURES_BEFORE_REAUTH: Final = 3

KEY_STATUS: Final = "status"
KEY_LAST_PACKET: Final = "last_packet"

STATUS_OK: Final = "ok"
STATUS_STALE: Final = "stale"
STATUS_NO_DATA: Final = "no_data"
STATUS_OFFLINE: Final = "offline"

STATUS_OPTIONS: Final = [STATUS_OK, STATUS_STALE, STATUS_NO_DATA, STATUS_OFFLINE]
