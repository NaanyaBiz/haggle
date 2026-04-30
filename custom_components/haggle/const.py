"""Constants for the haggle integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "haggle"

# AGL portal data lags 24-48h. Polling more often is wasted requests
# (and the portal rate-limits aggressively).
SCAN_INTERVAL: Final = timedelta(hours=24)

# Config-flow / config-entry keys.
# `CONF_EMAIL` and `CONF_PASSWORD` are imported from `homeassistant.const`
# (the standard HA keys); only haggle-specific ones live here.
CONF_OTP: Final = "otp"
CONF_NMI: Final = "nmi"
CONF_ACCOUNT_ID: Final = "account_id"
CONF_SESSION_COOKIE: Final = "session_cookie"

# Sensor keys produced by the coordinator.
DATA_GRID_IMPORT_KWH: Final = "grid_import_kwh"
DATA_GRID_EXPORT_KWH: Final = "grid_export_kwh"
DATA_LAST_READING_AT: Final = "last_reading_at"
