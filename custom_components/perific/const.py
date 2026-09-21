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

# The two cumulative registers imported as external statistics, as
# {statistic key: PhaseData field}. Both names match; the mapping is explicit so
# a rename of either side cannot silently pair them wrongly.
HISTORY_REGISTERS: Final = {
    "energy_import": "energy_import",
    "energy_export": "energy_export",
}

# Shown in the statistics picker and the Energy dashboard, where there is no
# entity to take a translated name from.
HISTORY_NAMES: Final = {
    "energy_import": "Imported electricity",
    "energy_export": "Exported electricity",
}

# One request covers at most this much, so a long catch-up arrives as a series
# of ordinary responses rather than one very large one. The cap only bites while
# catching up: a steady-state run finds a single hour outstanding and stops after
# the first chunk.
HISTORY_CHUNK: Final = timedelta(days=1)
HISTORY_MAX_CHUNKS: Final = 40

# Windows shorter than this are not asked for. Points are a minute apart so one
# could hold nothing, and the API answers 400 to a window whose start and end
# are the same once both are truncated to whole seconds.
HISTORY_MIN_WINDOW: Final = timedelta(minutes=1)

# Statistics are hourly, so importing more often than that only rewrites the
# current unfinished hour. Run a few minutes past the hour, by which time the
# hour that just ended is complete and the vendor has it. Phase matters: an
# interval timer would drift to whatever minute setup happened at, leaving a
# finished hour unwritten for up to an hour.
HISTORY_RUN_AT_MINUTE: Final = 5

# Cost is imported as its own series per register, because the Energy
# dashboard's own cost sensor refuses to work against an external statistic —
# `energy/data.py` rejects a price entity outright when `stat_energy_from` is
# not an entity id, and points at `stat_cost` instead.
COST_KEYS: Final = {
    "energy_import": "energy_import_cost",
    "energy_export": "energy_export_compensation",
}
COST_NAMES: Final = {
    "energy_import_cost": "Imported electricity cost",
    "energy_export_compensation": "Exported electricity compensation",
}

CONF_PRICE_ENTITY: Final = "price_entity"
CONF_PRICE_MARKUP: Final = "price_markup"
CONF_ENERGY_TAX: Final = "energy_tax"
CONF_VAT_PERCENT: Final = "vat_percent"
CONF_EXPORT_PRICE_ENTITY: Final = "export_price_entity"
CONF_EXPORT_PREMIUM: Final = "export_premium"

SERVICE_IMPORT_HISTORY: Final = "import_history"
ATTR_START: Final = "start"

KEY_STATUS: Final = "status"
KEY_LAST_PACKET: Final = "last_packet"

STATUS_OK: Final = "ok"
STATUS_STALE: Final = "stale"
STATUS_NO_DATA: Final = "no_data"
STATUS_OFFLINE: Final = "offline"

STATUS_OPTIONS: Final = [STATUS_OK, STATUS_STALE, STATUS_NO_DATA, STATUS_OFFLINE]
