"""AGL Energy API client.

Architecture (§7 of AGL-API-FINDINGS.md):
  AglAuth   — manages the Auth0 refresh-token grant, token rotation, and
              proactive refresh. Persists the rotated refresh token via a
              callback so the integration doesn't lock itself out.
  AglClient — thin async HTTP wrapper. Adds required headers (Authorization,
              Client-Flavor, User-Agent) and retries once on 401 by forcing
              an auth refresh.

Both classes are stubs here. Real implementation is Sprint 1; see
AGENTS.md > AGL gotchas and ~/scratch/aglreversing/AGL-API-FINDINGS.md.
The actual API responses are captured in ~/scratch/aglreversing/flows/agl-json/
and should be used as pytest fixtures (real captures = real tests).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..const import AGL_CLIENT_FLAVOR, AGL_USER_AGENT

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from datetime import date, datetime

    import aiohttp


class AGLError(Exception):
    """Base class for AGL API errors."""


class AGLAuthError(AGLError):
    """Auth failure — refresh token invalid / revoked; reauth required."""


class AGLRateLimitError(AGLError):
    """HTTP 429 — caller should back off before retrying."""


# ---------------------------------------------------------------------------
# Auth0 token models
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class TokenSet:
    """Holds a live access token + the current (rotated) refresh token."""

    access_token: str
    refresh_token: str  # MUST be persisted after every rotation
    expires_at: datetime  # UTC; derived from `expires_in`
    id_token: str = ""  # for identity claims if needed


# ---------------------------------------------------------------------------
# Data models — each wraps one API response section.
# Defined as dataclasses so the rest of the integration never touches raw
# dicts (and type-checking catches field-name changes at compile time).
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Contract:
    """One fuel/service contract from /api/v3/overview."""

    contract_number: str
    account_number: str
    address: str
    fuel_type: str  # "electricityContract" | "gasContract"
    status: str  # "active" | ...
    has_solar: bool = False
    meter_type: str = "smart"


@dataclass(slots=True)
class IntervalReading:
    """One 30-minute interval reading from /Hourly."""

    dt: datetime  # slot start, UTC
    kwh: float  # consumption.values.quantity — source of truth
    cost_aud: float  # consumption.amount
    rate_type: str  # "normal" | "peak" | "offpeak" | "shoulder" | "none"


@dataclass(slots=True)
class DailyReading:
    """One day aggregate from /Daily."""

    day: date
    kwh: float
    cost_aud: float


@dataclass(slots=True)
class BillPeriod:
    """Current bill period boundaries + totals from /usage summary."""

    start: date
    end: date
    consumption_kwh: float
    cost_label: str  # e.g. "$87.38"
    projection_label: str  # e.g. "$139.15"


@dataclass(slots=True)
class PlanRates:
    """Tariff rates from /api/v2/plan/energy/{contractNumber}."""

    product_name: str
    unit_rates: list[dict[str, Any]] = field(default_factory=list)
    supply_charge_cents_per_day: float = 0.0


# ---------------------------------------------------------------------------
# AglAuth — Auth0 token lifecycle
# ---------------------------------------------------------------------------


class AglAuth:
    """Manages Auth0 refresh-token grant for AGL.

    - Proactively refreshes when the access token is within
      TOKEN_REFRESH_MARGIN_MINUTES of expiry.
    - Rotates the refresh token on every exchange and calls
      `persist_callback(new_refresh_token)` so the caller can persist it.
      Failure to persist = lockout within one cycle.
    """

    def __init__(
        self,
        refresh_token: str,
        persist_callback: Callable[[str], Awaitable[None]],
    ) -> None:
        self._refresh_token = refresh_token
        self._persist = persist_callback
        self._token_set: TokenSet | None = None

    async def async_ensure_valid_token(self, session: aiohttp.ClientSession) -> str:
        """Return a live access token, refreshing proactively if needed."""
        raise NotImplementedError("agl-api: implement in Sprint 1")

    async def async_force_refresh(self, session: aiohttp.ClientSession) -> str:
        """Force a token refresh (called after a 401 on a data request)."""
        raise NotImplementedError("agl-api: implement in Sprint 1")


# ---------------------------------------------------------------------------
# AglClient — data API wrapper
# ---------------------------------------------------------------------------


class AglClient:
    """Async client for the AGL platform data API."""

    BASE_URL = "https://api.platform.agl.com.au/mobile/bff"

    def __init__(
        self,
        auth: AglAuth,
        session: aiohttp.ClientSession,
    ) -> None:
        self._auth = auth
        self._session = session

    @property
    def _default_headers(self) -> dict[str, str]:
        return {
            "Client-Flavor": AGL_CLIENT_FLAVOR,
            "User-Agent": AGL_USER_AGENT,
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate, br",
        }

    # --- Discovery ---

    async def async_get_overview(self) -> list[Contract]:
        """Fetch /api/v3/overview and return a Contract per fuel service."""
        raise NotImplementedError("agl-api: implement in Sprint 1")

    async def async_get_servicehub(self, contract_number: str) -> dict[str, str]:
        """Fetch /api/v1/servicehub/energy/{contractNumber} hyperlinks."""
        raise NotImplementedError("agl-api: implement in Sprint 1")

    # --- Usage ---

    async def async_get_usage_summary(self, contract_number: str) -> BillPeriod:
        """Fetch /api/v2/usage/smart/Electricity/{contractNumber}."""
        raise NotImplementedError("agl-api: implement in Sprint 1")

    async def async_get_usage_hourly(
        self, contract_number: str, day: date
    ) -> list[IntervalReading]:
        """Fetch /Hourly for a single day (30-min intervals).

        Use `day == yesterday` for reliable data; today will be empty.
        Field to use: consumption.values.quantity (kWh), NOT consumption.quantity.
        dateTime is slot-start in UTC.
        """
        raise NotImplementedError("agl-api: implement in Sprint 1")

    async def async_get_usage_daily(
        self, contract_number: str, start: date, end: date
    ) -> list[DailyReading]:
        """Fetch /Daily for a date range."""
        raise NotImplementedError("agl-api: implement in Sprint 1")

    async def async_get_usage_hourly_previous(
        self, contract_number: str, day: date
    ) -> list[IntervalReading]:
        """Fetch /Previous/Hourly — useful for backfill on first install."""
        raise NotImplementedError("agl-api: implement in Sprint 1")

    # --- Plan ---

    async def async_get_plan(self, contract_number: str) -> PlanRates:
        """Fetch /api/v2/plan/energy/{contractNumber} tariff rates."""
        raise NotImplementedError("agl-api: implement in Sprint 1")

    async def async_close(self) -> None:
        """Close the underlying aiohttp session if we own it."""
        raise NotImplementedError("agl-api: implement in Sprint 1")
