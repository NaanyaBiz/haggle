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
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, NoReturn

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


def _redact_body(text: str) -> str:
    """Debug-safe body snippet: redact token-like values BEFORE truncating.

    Truncating first can cut a long token before its closing quote, so the
    redaction pattern would miss it and leak the prefix (Class A rule,
    docs/threat-model.md §2). Users paste debug logs into public issues.
    """
    text = re.sub(
        r'"([a-z_]*token)"\s*:\s*"[^"]*"',
        r'"\1":"«redacted»"',
        text,
    )
    # Bodies can echo the request path or identifier fields (Class B):
    # apply the same digit-segment and keyed-identifier redactions.
    text = re.sub(r"/(\d{2,8})(\d{4})(?=[/?\"]|$)", r"/…\2", text)
    text = re.sub(
        r'"((?:account|contract)_?[Nn]umber)"\s*:\s*"?(?:\d{2,8})(\d{4})',
        r'"\1":"…\2',
        text,
    )
    return text[:200]


def _redact_url(url: str) -> str:
    """Debug-safe URL: usage/plan paths embed the contract number (Class B).

    Long digit runs in path segments are reduced to their last 4 digits.
    """
    return re.sub(r"/(\d{2,8})(\d{4})(?=/|\?|$)", r"/…\2", url)


# Failures below the HTTP-status layer. Every coordinator catch site is
# designed around the AGLError family; letting these escape raw crashes the
# whole update cycle BEFORE the solar heal's attempt accounting runs, which
# can wedge a pending heal in an unbounded uncounted retry loop (#151).
# TimeoutError covers asyncio.TimeoutError (alias since Python 3.11).
_TRANSPORT_ERRORS = (TimeoutError, aiohttp.ClientError)

# Refresh when this many seconds remain before expiry.
_REFRESH_MARGIN_SECONDS = 120

TOKEN_ENDPOINT = f"{AGL_AUTH_HOST}/oauth/token"

# OAuth error codes are lowercase snake_case slugs. Only a string matching
# this alphabet is ever echoed into an exception (which reaches HA Persistent
# Notifications and diagnostics via str(last_exception)) — a short string is
# NOT automatically a safe slug: a token fragment, an email address, or a
# control-character payload all fit in 64 chars (Codex, PR #265).
_OAUTH_ERROR_SLUG = re.compile(r"[a-z0-9_]{1,64}")

# The only error codes that mean the refresh GRANT itself is dead, so reauth
# is the fix. Everything else — server_error, temporarily_unavailable, a
# malformed or unknown code — says nothing about the grant and must stay
# retryable: routing it to reauth abandons a working rotated token family
# over a blip (Codex, PR #265; AGENTS.md "network blip is never AGLAuthError").
_TERMINAL_GRANT_ERRORS = frozenset({"invalid_grant"})

# Auth0-documented access-token lifetime (AGENTS.md: expires_in 900,
# confirmed 2026-05-01) — the fallback when expires_in is absent/malformed.
_EXPIRES_IN_FALLBACK = 900
# Sanity ceiling: anything above a day is not a plausible access-token
# lifetime, and an absurd value (10**20) would overflow fromtimestamp.
_EXPIRES_IN_CEILING = 86400


def _oauth_error_slug(error: object) -> str:
    """Return `error` if it is a plausible OAuth error slug, else 'unspecified'."""
    if isinstance(error, str) and _OAUTH_ERROR_SLUG.fullmatch(error):
        return error
    return "unspecified"


# Auth0 token material is printable non-space ASCII (base64url segments,
# dots, the "v1." refresh prefix family). A credential outside that set —
# empty, whitespace-only, or carrying embedded CR/LF/control characters —
# is unusable: persisted, it discards the real grant on the next refresh;
# in the Authorization header, CR/LF raises from aiohttp's header
# validation OUTSIDE the AGLError family (Codex pass 4, PR #265).
_TOKEN_CHARS = re.compile(r"[\x21-\x7e]+")


def _plausible_token(token: str) -> bool:
    """True when `token` is non-empty printable non-space ASCII."""
    return _TOKEN_CHARS.fullmatch(token) is not None


def _raise_for_token_error_status(status: int, text: str) -> NoReturn:
    """Classify a non-200 token response by its error slug, not bare status.

    Auth0 delivers invalid_grant as HTTP 403 + JSON body — only THAT means
    the grant is dead. A 5xx/429/other 4xx says nothing about the grant, and
    the old blanket AGLAuthError pushed users through reauth over an Auth0
    blip (Codex taxonomy finding, PR #265 — same rule as the 200-with-error
    branch in async_force_refresh).
    """
    code = ""
    try:
        body_json = json.loads(text)
        if isinstance(body_json, dict):
            code = _oauth_error_slug(body_json.get("error"))
    except ValueError, RecursionError:
        # RecursionError: json.loads on a deeply nested (but valid) body is
        # not a ValueError, and a raw escape here bypasses the AGLError
        # retry family (#151 class; Codex pass 5, PR #265).
        code = ""
    if code in _TERMINAL_GRANT_ERRORS:
        raise AGLAuthError(f"Token refresh error: {code}")
    raise AGLTransportError(f"token refresh failed HTTP {status}")


def _validated_token_fields(data: dict[str, Any]) -> tuple[str, str, str]:
    """Return (access_token, refresh_token, id_token) or raise retryable.

    Blank strings are rejected too: "" passes the type check but a persisted
    blank refresh token permanently discards the real grant (Codex, PR #265).
    """
    try:
        access_token = data["access_token"]
        new_refresh_token = data["refresh_token"]
        if not isinstance(access_token, str) or not isinstance(new_refresh_token, str):
            raise TypeError("token fields are not strings")
        if not _plausible_token(access_token) or not _plausible_token(
            new_refresh_token
        ):
            # Charset check, not just non-empty: whitespace-only slipped the
            # truthiness check (pass 3) and "r\n" slips a .strip() check
            # while persisting an invalid credential and breaking header
            # construction downstream (pass 4).
            raise ValueError("token fields are empty or malformed")
        id_token = data.get("id_token", "")
        if not isinstance(id_token, str):
            id_token = ""
    except (KeyError, TypeError, ValueError) as err:
        # Deliberately the type NAME only: an exception message that embeds
        # response content reaches diagnostics.py via str(last_exception),
        # published verbatim into files users attach to public issues.
        raise AGLTransportError(
            f"malformed token response from AGL auth ({type(err).__name__})"
        ) from err
    return access_token, new_refresh_token, id_token


def _token_expiry(data: dict[str, Any]) -> datetime:
    """Compute expires_at, degrading a malformed expires_in to the default.

    Called only AFTER the tokens are accepted, and NEVER raises: by that
    point Auth0 has already rotated the refresh token, so any exception
    before _persist() discards the only valid grant — the advertised retry
    would submit the stale token, get invalid_grant, and force the exact
    reauth lockout the schema shield exists to prevent (Codex P1, PR #265).
    """
    try:
        expires_in = int(data.get("expires_in", _EXPIRES_IN_FALLBACK))
    except TypeError, ValueError, OverflowError:
        # No value in the message — int()'s ValueError embeds its input.
        _LOGGER.warning(
            "Malformed expires_in in token response; assuming %s s",
            _EXPIRES_IN_FALLBACK,
        )
        expires_in = _EXPIRES_IN_FALLBACK
    if not 0 < expires_in <= _EXPIRES_IN_CEILING:
        # Also forecloses the fromtimestamp OverflowError/OSError an absurd
        # value (10**20) used to raise.
        _LOGGER.warning(
            "Implausible expires_in in token response; assuming %s s",
            _EXPIRES_IN_FALLBACK,
        )
        expires_in = _EXPIRES_IN_FALLBACK
    return datetime.fromtimestamp(
        int(datetime.now(tz=UTC).timestamp()) + expires_in,
        tz=UTC,
    )


class AGLError(Exception):
    """Base class for AGL API errors."""


class AGLTransportError(AGLError):
    """Below-HTTP failure: network error, timeout, or non-JSON body.

    Distinct from a plain AGLError so per-day backfill fetchers can HALT the
    chunk (transport failures are endpoint-wide and transient — retrying the
    whole chunk next cycle is right) instead of SKIPPING the day, which would
    advance the resume point past it and leave a permanent hole (Codex on
    #157). Still an AGLError, so every existing catch site behaves.
    """


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
                    # Body lives in DEBUG only — and even there, token-like
                    # values are redacted first: users enable debug logging
                    # while troubleshooting and paste logs into public issues
                    # (Class A rule, docs/threat-model.md §2).
                    text = await resp.text()
                    _LOGGER.debug(
                        "Token refresh non-200 body: %s",
                        _redact_body(text),
                    )
                    _raise_for_token_error_status(resp.status, text)
                data: dict[str, Any] = await resp.json(content_type=None)
        except _TRANSPORT_ERRORS as err:
            # A network blip is NOT an auth failure — wrap as retryable
            # AGLError, never AGLAuthError (which triggers the reauth flow)
            # and never a raw escape (which crashes the cycle, #151).
            raise AGLTransportError(
                f"transport error during token refresh: {type(err).__name__}"
            ) from err
        except (json.JSONDecodeError, RecursionError) as err:
            # RecursionError: deeply nested valid JSON raises it instead of
            # JSONDecodeError (Codex pass 5 — same shield as the non-200
            # branch and _get).
            raise AGLTransportError("non-JSON response from token endpoint") from err

        # Schema-trusting section (#243): a malformed-but-200 body raises
        # retryable AGLTransportError — never AGLAuthError, never a raw escape.
        if not isinstance(data, dict):
            # Valid JSON, wrong shape: `null`, `[]`, `"x"`, `3`. The
            # annotation above is a promise the wire cannot keep.
            raise AGLTransportError("unexpected token-response shape from AGL auth")

        error = data.get("error")
        if error:
            # Auth0's `error` is a short snake_case slug ("invalid_grant") —
            # only a string matching that alphabet is echoed, because this
            # message reaches ConfigEntryAuthFailed -> HA Persistent
            # Notifications and diagnostics.py's str(last_exception).
            code = _oauth_error_slug(error)
            if code in _TERMINAL_GRANT_ERRORS:
                raise AGLAuthError(f"Token refresh error: {code}")
            # Unknown/malformed codes say nothing about the grant: stay
            # retryable rather than burning the rotated token family.
            raise AGLTransportError(f"token endpoint error: {code}")

        access_token, new_refresh_token, id_token = _validated_token_fields(data)
        # _token_expiry never raises — the tokens above are already rotated
        # on Auth0's side, so failing here would discard the only valid
        # grant (Codex P1, PR #265).
        expires_at = _token_expiry(data)

        self._token_set = TokenSet(
            access_token=access_token,
            refresh_token=new_refresh_token,
            expires_at=expires_at,
            id_token=id_token,
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
            raise AGLTransportError(f"transport error: {type(err).__name__}") from err
        except (json.JSONDecodeError, RecursionError) as err:
            # Body may be an Akamai/HTML page; never surface it (#151).
            raise AGLTransportError("non-JSON response from AGL endpoint") from err

    async def _get_raw(self, url: str) -> Any:
        token = await self._auth.async_ensure_valid_token(self._session)
        headers = {**self._default_headers, "Authorization": f"Bearer {token}"}

        async with self._session.get(url, headers=headers) as resp:
            if resp.status == 429:
                raise AGLRateLimitError(f"Rate limited (HTTP {resp.status})")
            if resp.status == 401:
                _LOGGER.debug("Got 401 on %s; forcing token refresh", _redact_url(url))
            elif resp.status >= 400:
                # URL contains contract_number (PII) and body may carry
                # AGL-side diagnostics; keep both in DEBUG only.
                text = await resp.text()
                _LOGGER.debug(
                    "HTTP %s on %s body: %s",
                    resp.status,
                    _redact_url(url),
                    _redact_body(text),
                )
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
                    _redact_url(url),
                    _redact_body(text),
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
