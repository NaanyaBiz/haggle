"""AGL Energy API client.

Architecture (§7 of AGL-API-FINDINGS.md):
  AglAuth   — manages the Auth0 refresh-token grant, token rotation, and
              proactive refresh. Persists the rotated refresh token via a
              callback so the integration doesn't lock itself out.
  AglClient — thin async HTTP wrapper. Adds required headers (Authorization,
              Client-Flavor, User-Agent) and retries once on 401 by forcing
              an auth refresh.

TLS pinning is handled by the `aiohttp.ClientSession`'s connector, not by
this module. See `agl/pinning.py::HagglePinningConnector` — it captures the
leaf-cert SPKI on every new connection and (optionally) invokes a pin-check
callback for TOFU validation. This module is connector-agnostic.

Token endpoint: POST https://secure.agl.com.au/oauth/token (grant=refresh_token).
Access tokens expire in 900 s (15 min); refresh when exp - now < 120 s (2 min).
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import aiohttp

from ..const import (
    AGL_ACCEPT_FEATURES,
    AGL_AUTH0_CLIENT,
    AGL_AUTH_HOST,
    AGL_CLIENT_DEVICE,
    AGL_CLIENT_FLAVOR,
    AGL_CLIENT_ID,
    AGL_SCALING,
    AGL_USER_AGENT,
)
from .models import (
    BillPeriod,
    Contract,
    IntervalReading,
    PlanRates,
    TokenSet,
)
from .parser import (
    parse_bill_period,
    parse_interval_readings,
    parse_overview,
    parse_plan,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from datetime import date

_LOGGER = logging.getLogger(__name__)

# Failures below the HTTP-status layer. Every coordinator catch site is
# designed around the AGLError family; letting these escape raw crashes the
# whole update cycle BEFORE the solar heal's attempt accounting runs, which
# can wedge a pending heal in an unbounded uncounted retry loop (#151).
# TimeoutError covers asyncio.TimeoutError (alias since Python 3.11).
_TRANSPORT_ERRORS = (TimeoutError, aiohttp.ClientError)

# Refresh when this many seconds remain before expiry.
_REFRESH_MARGIN_SECONDS = 120

TOKEN_ENDPOINT = f"{AGL_AUTH_HOST}/oauth/token"


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

        return await self.async_force_refresh(session)

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
            "auth0-client": AGL_AUTH0_CLIENT,
        }
        body = {
            "grant_type": "refresh_token",
            "client_id": AGL_CLIENT_ID,
            "refresh_token": self._refresh_token,
        }

        try:
            async with session.post(TOKEN_ENDPOINT, json=body, headers=headers) as resp:
                if resp.status == 401:
                    raise AGLAuthError("Token refresh rejected (401) — reauth required")
                if resp.status != 200:
                    # Auth0 error bodies can include diagnostic fields
                    # (mfa_token, error_description, internal trace IDs); keep
                    # them out of the exception that propagates to
                    # ConfigEntryAuthFailed → HA Persistent Notifications.
                    # Body lives in DEBUG only.
                    text = await resp.text()
                    _LOGGER.debug("Token refresh non-200 body: %s", text[:200])
                    raise AGLAuthError(f"Token refresh failed HTTP {resp.status}")
                data: dict[str, Any] = await resp.json(content_type=None)
        except _TRANSPORT_ERRORS as err:
            # A network blip is NOT an auth failure — wrap as retryable
            # AGLError, never AGLAuthError (which triggers the reauth flow)
            # and never a raw escape (which crashes the cycle, #151).
            raise AGLError(
                f"transport error during token refresh: {type(err).__name__}"
            ) from err
        except json.JSONDecodeError as err:
            raise AGLError("non-JSON response from token endpoint") from err

        error = data.get("error")
        if error:
            raise AGLAuthError(f"Token refresh error: {error}")

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

        _LOGGER.info(
            "AGL token refreshed; expires_at=%s",
            expires_at.isoformat(),
        )
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
            "Client-Device": AGL_CLIENT_DEVICE,
            "User-Agent": AGL_USER_AGENT,
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate, br",
            "Accept-Language": "en-AU,en;q=0.9",
            "Accept-Features": AGL_ACCEPT_FEATURES,
        }

    async def _get(self, url: str) -> Any:
        """GET a URL with auth, retrying once on 401.

        Transport- and parse-level failures (network errors, timeouts, a 200
        with a non-JSON body such as an Akamai challenge page) are wrapped
        into AGLError so callers see the one exception family every catch
        site was designed around (#151). Typed AGL errors pass through
        unchanged.
        """
        try:
            return await self._get_raw(url)
        except _TRANSPORT_ERRORS as err:
            raise AGLError(f"transport error: {type(err).__name__}") from err
        except json.JSONDecodeError as err:
            # Body may be an Akamai/HTML page; never surface it (#151).
            raise AGLError("non-JSON response from AGL endpoint") from err

    async def _get_raw(self, url: str) -> Any:
        token = await self._auth.async_ensure_valid_token(self._session)
        headers = {**self._default_headers, "Authorization": f"Bearer {token}"}

        async with self._session.get(url, headers=headers) as resp:
            if resp.status == 429:
                raise AGLRateLimitError(f"Rate limited (HTTP {resp.status})")
            if resp.status == 401:
                _LOGGER.debug("Got 401 on %s; forcing token refresh", url)
            elif resp.status >= 400:
                # URL contains contract_number (PII) and body may carry
                # AGL-side diagnostics; keep both in DEBUG only.
                text = await resp.text()
                _LOGGER.debug("HTTP %s on %s body: %s", resp.status, url, text[:200])
                raise AGLError(f"HTTP {resp.status} fetching AGL data")
            else:
                return await resp.json(content_type=None)

        # Only reached on 401 — force refresh and retry once.
        token = await self._auth.async_force_refresh(self._session)
        headers = {**self._default_headers, "Authorization": f"Bearer {token}"}

        async with self._session.get(url, headers=headers) as resp2:
            if resp2.status == 401:
                raise AGLAuthError("Still 401 after token refresh")
            if resp2.status == 429:
                raise AGLRateLimitError(f"Rate limited (HTTP {resp2.status})")
            if resp2.status >= 400:
                text = await resp2.text()
                _LOGGER.debug(
                    "HTTP %s on %s body (post-refresh): %s",
                    resp2.status,
                    url,
                    text[:200],
                )
                raise AGLError(f"HTTP {resp2.status} fetching AGL data")
            return await resp2.json(content_type=None)

    # --- Discovery ---

    async def async_get_overview(self) -> list[Contract]:
        """Fetch /api/v3/overview and return a Contract per fuel service."""
        url = f"{self.BASE_URL}/api/v3/overview"
        data = await self._get(url)
        return parse_overview(data)

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
        Field to use: consumption.quantity (outer) for kWh, NOT
        consumption.values.quantity (inner DPI/chart-scaled helper).
        dateTime is slot-start in UTC, but the `period=` parameter is
        interpreted in the contract's LOCAL timezone, so a single-day query
        returns intervals from local midnight that day to local midnight the
        next day (spanning two UTC dates). The statistics importer relies on
        this: it cuts the cumulative-sum baseline at the earliest returned
        interval hour rather than a UTC-midnight derived from `day`.
        """
        period = f"{day}_{day}"
        url = f"{self.BASE_URL}/api/v2/usage/smart/Electricity/{contract_number}/Current/Hourly?period={period}&scaling={AGL_SCALING}"
        data = await self._get(url)
        return parse_interval_readings(data)

    async def async_get_usage_hourly_previous(
        self, contract_number: str, day: date
    ) -> list[IntervalReading]:
        """Fetch /Previous/Hourly — useful for backfill on first install."""
        period = f"{day}_{day}"
        url = f"{self.BASE_URL}/api/v2/usage/smart/Electricity/{contract_number}/Previous/Hourly?period={period}&scaling={AGL_SCALING}"
        data = await self._get(url)
        return parse_interval_readings(data)

    # --- Solar (feed-in) ---

    async def async_get_solar_hourly(
        self, contract_number: str, day: date, *, previous: bool = False
    ) -> list[IntervalReading]:
        """Fetch ElectricitySolar /Hourly feed-in intervals for a single day.

        Same envelope, headers, and scaling requirement as the Electricity
        endpoint — the path substitutes the ElectricitySolar segment and each
        item carries an extra "feedIn" block (documented from a real capture,
        #128). Returns the feedIn side only: kwh = exported kWh, cost_aud =
        AUD feed-in credit for the slot. `previous=True` selects the
        Previous/Hourly variant for days before the current bill period.
        """
        period_segment = "Previous" if previous else "Current"
        period = f"{day}_{day}"
        url = f"{self.BASE_URL}/api/v2/usage/smart/ElectricitySolar/{contract_number}/{period_segment}/Hourly?period={period}&scaling={AGL_SCALING}"
        data = await self._get(url)
        return parse_interval_readings(data, source_field="feedIn")

    # --- Plan ---

    async def async_get_plan(self, contract_number: str) -> PlanRates:
        """Fetch /api/v2/plan/energy/{contractNumber} tariff rates."""
        url = f"{self.BASE_URL}/api/v2/plan/energy/{contract_number}"
        data = await self._get(url)
        return parse_plan(data)
