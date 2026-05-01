"""AGL Energy API client.

Architecture (§7 of AGL-API-FINDINGS.md):
  AglAuth   — manages the Auth0 refresh-token grant, token rotation, and
              proactive refresh. Persists the rotated refresh token via a
              callback so the integration doesn't lock itself out.
  AglClient — thin async HTTP wrapper. Adds required headers (Authorization,
              Client-Flavor, User-Agent) and retries once on 401 by forcing
              an auth refresh.

Token endpoint: POST https://secure.agl.com.au/oauth/token (grant=refresh_token).
Access tokens expire in 900 s (15 min); refresh when exp - now < 120 s (2 min).
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ..const import AGL_AUTH_HOST, AGL_CLIENT_FLAVOR, AGL_CLIENT_ID, AGL_USER_AGENT
from .models import (
    BillPeriod,
    Contract,
    DailyReading,
    IntervalReading,
    PlanRates,
    TokenSet,
)
from .parser import (
    parse_bill_period,
    parse_daily_readings,
    parse_interval_readings,
    parse_overview,
    parse_plan,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from datetime import date

    import aiohttp

_LOGGER = logging.getLogger(__name__)

# auth0-client header value — base64-encoded SDK identity blob (Auth0.swift 2.12.0).
_AUTH0_CLIENT = "eyJlbnYiOnsic3dpZnQiOiI2LngiLCJpT1MiOiIyNi40In0sIm5hbWUiOiJBdXRoMC5zd2lmdCIsInZlcnNpb24iOiIyLjEyLjAifQ"  # gitleaks:allow

# Refresh when this many seconds remain before expiry.
_REFRESH_MARGIN_SECONDS = 120

TOKEN_ENDPOINT = f"{AGL_AUTH_HOST}/oauth/token"

# Re-export models so callers can import from client (backward compat).
__all__ = [
    "AGLAuthError",
    "AGLError",
    "AGLRateLimitError",
    "AglAuth",
    "AglClient",
    "BillPeriod",
    "Contract",
    "DailyReading",
    "IntervalReading",
    "PlanRates",
    "TokenSet",
]


class AGLError(Exception):
    """Base class for AGL API errors."""


class AGLAuthError(AGLError):
    """Auth failure — refresh token invalid / revoked; reauth required."""


class AGLRateLimitError(AGLError):
    """HTTP 429 — caller should back off before retrying."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _decode_jwt_exp(token: str) -> int | None:
    """Return the `exp` claim from a JWT, or None if it cannot be decoded."""
    try:
        payload_b64 = token.split(".")[1]
        # Base64url — pad to multiple of 4.
        padding = 4 - len(payload_b64) % 4
        if padding != 4:
            payload_b64 += "=" * padding
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return int(payload["exp"])
    except Exception:
        return None


# ---------------------------------------------------------------------------
# AglAuth — Auth0 token lifecycle
# ---------------------------------------------------------------------------


class AglAuth:
    """Manages Auth0 refresh-token grant for AGL.

    - Proactively refreshes when the access token is within
      _REFRESH_MARGIN_SECONDS of expiry.
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
        if self._token_set is not None:
            exp = _decode_jwt_exp(self._token_set.access_token)
            now = int(datetime.now(tz=UTC).timestamp())
            if exp is not None and (exp - now) >= _REFRESH_MARGIN_SECONDS:
                return self._token_set.access_token

        await self.async_force_refresh(session)
        return self._token_set.access_token  # type: ignore[union-attr]

    async def async_force_refresh(self, session: aiohttp.ClientSession) -> str:
        """Force a token refresh. Persists the rotated refresh token.

        Raises AGLAuthError on 401 / invalid_grant.
        Returns the new access token.
        """
        headers = {
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Accept-Language": "en-AU,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Client-Flavor": AGL_CLIENT_FLAVOR,
            "User-Agent": AGL_USER_AGENT,
            "auth0-client": _AUTH0_CLIENT,
        }
        body = {
            "grant_type": "refresh_token",
            "client_id": AGL_CLIENT_ID,
            "refresh_token": self._refresh_token,
        }

        async with session.post(TOKEN_ENDPOINT, json=body, headers=headers) as resp:
            if resp.status == 401:
                raise AGLAuthError("Token refresh rejected (401) — reauth required")
            if resp.status != 200:
                text = await resp.text()
                raise AGLAuthError(
                    f"Token refresh failed HTTP {resp.status}: {text[:200]}"
                )
            data: dict[str, Any] = await resp.json(content_type=None)

        error = data.get("error")
        if error:
            raise AGLAuthError(
                f"Token refresh error: {error} — {data.get('error_description', '')}"
            )

        access_token: str = data["access_token"]
        new_refresh_token: str = data["refresh_token"]
        expires_in: int = int(data.get("expires_in", 900))
        expires_at = datetime.fromtimestamp(
            int(datetime.now(tz=UTC).timestamp()) + expires_in,
            tz=UTC,
        )

        self._token_set = TokenSet(
            access_token=access_token,
            refresh_token=new_refresh_token,
            expires_at=expires_at,
            id_token=data.get("id_token", ""),
        )
        self._refresh_token = new_refresh_token
        await self._persist(new_refresh_token)

        _LOGGER.debug("AGL token refreshed; expires_in=%d", expires_in)
        return access_token


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

    async def _get(self, url: str) -> Any:
        """GET a URL with auth, retrying once on 401."""
        token = await self._auth.async_ensure_valid_token(self._session)
        headers = {**self._default_headers, "Authorization": f"Bearer {token}"}

        async with self._session.get(url, headers=headers) as resp:
            if resp.status == 429:
                raise AGLRateLimitError(f"Rate limited on {url}")
            if resp.status == 401:
                _LOGGER.debug("Got 401 on %s; forcing token refresh", url)
            elif resp.status >= 400:
                text = await resp.text()
                raise AGLError(f"HTTP {resp.status} on {url}: {text[:200]}")
            else:
                return await resp.json(content_type=None)

        # Only reached on 401 — force refresh and retry once.
        await self._auth.async_force_refresh(self._session)
        token = self._auth._token_set.access_token  # type: ignore[union-attr]
        headers = {**self._default_headers, "Authorization": f"Bearer {token}"}

        async with self._session.get(url, headers=headers) as resp2:
            if resp2.status == 401:
                raise AGLAuthError(f"Still 401 after token refresh on {url}")
            if resp2.status == 429:
                raise AGLRateLimitError(f"Rate limited on {url}")
            if resp2.status >= 400:
                text = await resp2.text()
                raise AGLError(f"HTTP {resp2.status} on {url}: {text[:200]}")
            return await resp2.json(content_type=None)

    # --- Discovery ---

    async def async_get_overview(self) -> list[Contract]:
        """Fetch /api/v3/overview and return a Contract per fuel service."""
        url = f"{self.BASE_URL}/api/v3/overview"
        data = await self._get(url)
        return parse_overview(data)

    async def async_get_servicehub(self, contract_number: str) -> dict[str, Any]:
        """Fetch /api/v1/servicehub/energy/{contractNumber} hyperlinks."""
        url = f"{self.BASE_URL}/api/v1/servicehub/energy/{contract_number}"
        data: dict[str, Any] = await self._get(url)
        return {k: str(v) for k, v in data.items() if isinstance(v, str)}

    # --- Usage ---

    async def async_get_usage_summary(self, contract_number: str) -> BillPeriod:
        """Fetch /api/v2/usage/smart/Electricity/{contractNumber}."""
        url = f"{self.BASE_URL}/api/v2/usage/smart/Electricity/{contract_number}?isRestricted=False"
        data = await self._get(url)
        return parse_bill_period(data)

    async def async_get_usage_hourly(
        self, contract_number: str, day: date
    ) -> list[IntervalReading]:
        """Fetch /Hourly for a single day (30-min intervals).

        Use `day == yesterday` for reliable data; today will be empty.
        Field to use: consumption.values.quantity (kWh), NOT consumption.quantity.
        dateTime is slot-start in UTC.
        """
        period = f"{day}_{day}"
        url = f"{self.BASE_URL}/api/v2/usage/smart/Electricity/{contract_number}/Current/Hourly?period={period}"
        data = await self._get(url)
        return parse_interval_readings(data)

    async def async_get_usage_daily(
        self, contract_number: str, start: date, end: date
    ) -> list[DailyReading]:
        """Fetch /Daily for a date range."""
        period = f"{start}_{end}"
        url = f"{self.BASE_URL}/api/v2/usage/smart/Electricity/{contract_number}/Current/Daily?period={period}"
        data = await self._get(url)
        return parse_daily_readings(data)

    async def async_get_usage_hourly_previous(
        self, contract_number: str, day: date
    ) -> list[IntervalReading]:
        """Fetch /Previous/Hourly — useful for backfill on first install."""
        period = f"{day}_{day}"
        url = f"{self.BASE_URL}/api/v2/usage/smart/Electricity/{contract_number}/Previous/Hourly?period={period}"
        data = await self._get(url)
        return parse_interval_readings(data)

    # --- Plan ---

    async def async_get_plan(self, contract_number: str) -> PlanRates:
        """Fetch /api/v2/plan/energy/{contractNumber} tariff rates."""
        url = f"{self.BASE_URL}/api/v2/plan/energy/{contract_number}"
        data = await self._get(url)
        return parse_plan(data)

    async def async_close(self) -> None:
        """Close the underlying aiohttp session if we own it."""
        await self._session.close()
