"""Constants for the haggle integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "haggle"

# Auth0 / AGL API
AGL_AUTH_HOST: Final = "https://secure.agl.com.au"
AGL_API_HOST: Final = "https://api.platform.agl.com.au"
# iOS app client_id — captured from mitmproxy session (app.iOS.public.8.38.0-531).
AGL_CLIENT_ID: Final = "2mDkNcC8gkDLL7FTT1ZxF5rrQHrLTHL3"
# Match the captured client-flavor; AGL servers may reject unknown clients.
AGL_CLIENT_FLAVOR: Final = "app.iOS.public.8.38.0-531"
AGL_USER_AGENT: Final = "AGL/531 CFNetwork/3860.500.112 Darwin/25.4.0"

# Polling cadences.
# AGL interval data is delayed 24-48 h from the meter (AEMO feed lag).
SCAN_INTERVAL_HOURLY: Final = timedelta(hours=24)  # 30-min intervals: fetch yesterday
SCAN_INTERVAL_DAILY: Final = timedelta(hours=6)  # daily series: pick up new days
SCAN_INTERVAL_PLAN: Final = timedelta(days=7)  # plan/rates: rarely changes

# How many minutes before access-token expiry to proactively refresh.
TOKEN_REFRESH_MARGIN_MINUTES: Final = 5

# Config-entry keys.
# CONF_EMAIL / CONF_PASSWORD are NOT used — auth is via refresh token.
CONF_REFRESH_TOKEN: Final = "refresh_token"  # ← it IS a token key
CONF_ACCESS_TOKEN: Final = "access_token"
CONF_ACCESS_TOKEN_EXPIRY: Final = "access_token_expiry"
CONF_CONTRACT_NUMBER: Final = "contract_number"
CONF_ACCOUNT_NUMBER: Final = "account_number"

# Coordinator data keys.
DATA_CONSUMPTION_KWH: Final = "consumption_kwh"  # cumulative kWh (total_increasing)
DATA_CONSUMPTION_COST: Final = "consumption_cost"  # cumulative AUD cost
DATA_CONSUMPTION_TODAY: Final = (
    "consumption_today"  # kWh this local day (resets midnight)
)
DATA_CONSUMPTION_PERIOD: Final = "consumption_period"  # kWh this bill period
DATA_BILL_PROJECTION: Final = "bill_projection"  # AUD forecast for current period
DATA_UNIT_RATE: Final = "unit_rate"  # c/kWh
DATA_SUPPLY_CHARGE: Final = "supply_charge"  # c/day
