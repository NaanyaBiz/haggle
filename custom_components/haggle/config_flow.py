"""Config flow for haggle.

Onboarding strategy:
  Step 1 (user) -- generate PKCE verifier + challenge, build the /authorize
            URL, show it to the user. The user opens it in a browser, logs
            in, and is redirected to a "Not Found" page. They copy the full
            URL from the address bar and paste it back into HA.
  Step 2 (exchange) -- extract the authorization code from the pasted URL,
            POST /oauth/token (authorization_code grant + PKCE), store tokens.
            Captures the SHA-256 SPKI hash of secure.agl.com.au's leaf cert
            for Trust-On-First-Use pinning (see agl/pinning.py).
  Step 3 (select_contract) -- fetch /api/v3/overview, let user pick a contract
            if multiple electricity contracts are found. Captures the SPKI
            hash of api.platform.agl.com.au at the same time.
  Step 4 -- create entry. Both pinned SPKI hashes are persisted to entry.data
            and validated on every subsequent request from coordinator polling.

Reauth re-enters at Step 1 with fresh PKCE params, which naturally re-pins
both endpoints — the recommended remediation for legitimate AGL cert rotation.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlencode, urlparse

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .agl.client import AGLAuthError, AGLError
from .agl.parser import parse_overview

if TYPE_CHECKING:
    from .agl.models import Contract
from .agl.pinning import (
    AGL_AUTH_HOST_NAME,
    AGL_BFF_HOST_NAME,
    HagglePinningConnector,
)
from .const import (
    AGL_API_HOST,
    AGL_AUTH0_CLIENT,
    AGL_AUTH_HOST,
    AGL_CLIENT_FLAVOR,
    AGL_CLIENT_ID,
    AGL_OAUTH_AUDIENCE,
    AGL_OAUTH_SCOPE,
    AGL_REDIRECT_URI,
    AGL_USER_AGENT,
    CONF_ACCOUNT_NUMBER,
    CONF_CONTRACT_NUMBER,
    CONF_PINNED_SPKI_AUTH,
    CONF_PINNED_SPKI_BFF,
    CONF_REFRESH_TOKEN,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

CALLBACK_URL_FIELD = "callback_url"


def _gen_pkce() -> tuple[str, str]:
    """Return (verifier, S256-challenge) for an OAuth2 PKCE exchange."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    return verifier, challenge


def _build_authorize_url(challenge: str, state: str) -> str:
    """Build the Auth0 /authorize URL for the AGL iOS client."""
    params = {
        "response_type": "code",
        "client_id": AGL_CLIENT_ID,
        "redirect_uri": AGL_REDIRECT_URI,
        "scope": AGL_OAUTH_SCOPE,
        "audience": AGL_OAUTH_AUDIENCE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    return f"{AGL_AUTH_HOST}/authorize?{urlencode(params)}"


def _extract_code(
    callback_url: str, expected_state: str
) -> tuple[str | None, str | None]:
    """Parse the authorization code and state from the callback URL.

    Returns (code, None) on success, (None, error_key) on failure.
    """
    try:
        parsed = urlparse(callback_url)
        params = parse_qs(parsed.query)
    except Exception:
        return None, "invalid_auth"

    if "error" in params:
        return None, "invalid_auth"

    state = (params.get("state") or [""])[0]
    if state != expected_state:
        return None, "invalid_auth"

    code = (params.get("code") or [""])[0]
    return (code or None), None


async def _exchange_code(code: str, verifier: str) -> tuple[str, str, str]:
    """POST /oauth/token and return (access_token, refresh_token, auth_spki).

    `auth_spki` is the SHA-256 hex of the leaf-cert SPKI for
    secure.agl.com.au, captured by `HagglePinningConnector` during the
    TLS handshake. Empty string if capture fails (degrades to no-pin).

    Uses a short-lived `aiohttp.ClientSession` with our own pinning
    connector — HA's shared session uses HA's TCPConnector which we cannot
    subclass, so we own this one and close it on exit.

    Raises AGLAuthError on 4xx auth errors, AGLError on other failures.
    """
    url = f"{AGL_AUTH_HOST}/oauth/token"
    payload = {
        "grant_type": "authorization_code",
        "client_id": AGL_CLIENT_ID,
        "code": code,
        "code_verifier": verifier,
        "redirect_uri": AGL_REDIRECT_URI,
    }
    headers = {
        "Client-Flavor": AGL_CLIENT_FLAVOR,
        "auth0-client": AGL_AUTH0_CLIENT,
        "User-Agent": AGL_USER_AGENT,
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }
    connector = HagglePinningConnector()
    async with (
        aiohttp.ClientSession(connector=connector) as session,
        session.post(url, data=payload, headers=headers) as resp,
    ):
        if resp.status in (400, 401, 403):
            raise AGLAuthError(f"Token exchange failed: HTTP {resp.status}")
        if not resp.ok:
            raise AGLError(f"Token exchange error: HTTP {resp.status}")
        body: dict[str, Any] = await resp.json()

    access_token: str = body.get("access_token", "")
    refresh_token: str = body.get("refresh_token", "")
    if not access_token or not refresh_token:
        raise AGLAuthError("Token response missing access_token or refresh_token")

    auth_spki = connector.observed.get(AGL_AUTH_HOST_NAME, "")
    return access_token, refresh_token, auth_spki


async def _fetch_contracts(access_token: str) -> tuple[list[Contract], str]:
    """Fetch /api/v3/overview and return (contracts, bff_spki).

    `bff_spki` is the SHA-256 hex of the leaf-cert SPKI for
    api.platform.agl.com.au. Empty string if capture fails.

    Uses aiohttp directly rather than AglAuth/AglClient: AglAuth always calls
    async_force_refresh on first use (token_set is None on construction), which
    would POST the access_token as a refresh_token to Auth0 and fail.
    """
    url = f"{AGL_API_HOST}/mobile/bff/api/v3/overview"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Client-Flavor": AGL_CLIENT_FLAVOR,
        "User-Agent": AGL_USER_AGENT,
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate, br",
    }
    connector = HagglePinningConnector()
    async with (
        aiohttp.ClientSession(connector=connector) as session,
        session.get(url, headers=headers) as resp,
    ):
        if resp.status >= 400:
            # Body kept out of the exception so it doesn't surface in
            # ConfigEntryAuthFailed → HA Persistent Notifications.
            text = await resp.text()
            _LOGGER.debug("HTTP %s on %s body: %s", resp.status, url, text[:200])
            raise AGLError(f"HTTP {resp.status} fetching AGL overview")
        data = await resp.json(content_type=None)

    bff_spki = connector.observed.get(AGL_BFF_HOST_NAME, "")
    return parse_overview(data), bff_spki


class HaggleConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the haggle config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._pkce_verifier: str = ""
        self._pkce_challenge: str = ""
        self._oauth_state: str = ""
        self._access_token: str = ""
        self._refresh_token: str = ""
        self._auth_spki: str = ""
        self._bff_spki: str = ""
        self._contracts: list[Contract] = []

    # ------------------------------------------------------------------
    # Step 1 -- show /authorize URL, collect callback URL
    # ------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the /authorize URL and accept the pasted callback URL.

        On first call: generate PKCE verifier+challenge and random state.
        On submit: validate state, extract code, hand off to async_step_exchange.
        """
        errors: dict[str, str] = {}

        # Regenerate PKCE params only on first visit (not on re-show with errors).
        if not self._pkce_verifier:
            self._pkce_verifier, self._pkce_challenge = _gen_pkce()
            self._oauth_state = secrets.token_urlsafe(16)

        authorize_url = _build_authorize_url(self._pkce_challenge, self._oauth_state)

        if user_input is not None:
            callback_url: str = user_input.get(CALLBACK_URL_FIELD, "").strip()
            code, state_error = _extract_code(callback_url, self._oauth_state)
            if state_error:
                errors["base"] = state_error
            elif code:
                return await self.async_step_exchange(code)
            else:
                errors["base"] = "invalid_auth"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CALLBACK_URL_FIELD): str}),
            description_placeholders={"authorize_url": authorize_url},
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Step 2 -- exchange code for tokens
    # ------------------------------------------------------------------

    async def async_step_exchange(self, code: str) -> ConfigFlowResult:
        """Exchange the authorization code for tokens (PKCE grant)."""
        authorize_url = _build_authorize_url(self._pkce_challenge, self._oauth_state)
        schema = vol.Schema({vol.Required(CALLBACK_URL_FIELD): str})

        try:
            access_token, refresh_token, auth_spki = await _exchange_code(
                code, self._pkce_verifier
            )
        except AGLAuthError:
            return self.async_show_form(
                step_id="user",
                data_schema=schema,
                description_placeholders={"authorize_url": authorize_url},
                errors={"base": "invalid_auth"},
            )
        except (AGLError, aiohttp.ClientError, TimeoutError):
            return self.async_show_form(
                step_id="user",
                data_schema=schema,
                description_placeholders={"authorize_url": authorize_url},
                errors={"base": "cannot_connect"},
            )

        # Clear PKCE material now that the one-shot exchange has consumed it.
        # The flow object can persist in memory across multi-step retries; a
        # stale verifier is no longer useful and is one less secret to leak.
        self._pkce_verifier = ""
        self._pkce_challenge = ""

        self._access_token = access_token
        self._refresh_token = refresh_token
        self._auth_spki = auth_spki
        return await self.async_step_select_contract()

    # ------------------------------------------------------------------
    # Step 3 -- select contract
    # ------------------------------------------------------------------

    async def async_step_select_contract(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select contract if multiple discovered; skip selection if just one."""
        errors: dict[str, str] = {}

        if not self._contracts:
            try:
                self._contracts, self._bff_spki = await _fetch_contracts(
                    self._access_token
                )
            except Exception:
                errors["base"] = "cannot_connect"
                return self.async_show_form(
                    step_id="select_contract",
                    data_schema=vol.Schema({}),
                    errors=errors,
                )

        if not self._contracts:
            return await self._async_create_entry(contract_number="", account_number="")

        if len(self._contracts) == 1:
            c = self._contracts[0]
            return await self._async_create_entry(
                contract_number=c.contract_number,
                account_number=c.account_number,
                title=c.address,
            )

        if user_input is not None:
            chosen = user_input[CONF_CONTRACT_NUMBER]
            contract = next(
                (c for c in self._contracts if c.contract_number == chosen),
                self._contracts[0],
            )
            return await self._async_create_entry(
                contract_number=contract.contract_number,
                account_number=contract.account_number,
                title=contract.address,
            )

        options = {
            c.contract_number: f"{c.address} ({c.fuel_type})" for c in self._contracts
        }
        return self.async_show_form(
            step_id="select_contract",
            data_schema=vol.Schema(
                {vol.Required(CONF_CONTRACT_NUMBER): vol.In(options)}
            ),
        )

    # ------------------------------------------------------------------
    # Reauth
    # ------------------------------------------------------------------

    async def async_step_reauth(self, _entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Re-enter at Step 1 with fresh PKCE params when refresh token expires."""
        self._pkce_verifier = ""
        return await self.async_step_user()

    # ------------------------------------------------------------------
    # Entry creation
    # ------------------------------------------------------------------

    async def _async_create_entry(
        self,
        contract_number: str,
        account_number: str,
        title: str | None = None,
    ) -> ConfigFlowResult:
        # Fall back to a SHA-256 hash of the refresh token (one-way) so the
        # entity registry — written as plaintext JSON — never sees raw token
        # material. The token itself stays in entry.data which HAOS encrypts.
        if account_number and contract_number:
            unique_id = f"{account_number}_{contract_number}"
        elif self._refresh_token:
            unique_id = hashlib.sha256(self._refresh_token.encode()).hexdigest()[:16]
        else:
            unique_id = "unknown"
        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured()
        _LOGGER.info(
            "Creating haggle entry: account=%s contract=%s pin_auth=%s pin_bff=%s",
            account_number or "unknown",
            contract_number or "unknown",
            "set" if self._auth_spki else "missing",
            "set" if self._bff_spki else "missing",
        )
        # Only refresh_token is persisted as a credential. The short-lived
        # access_token (15 min) must never live on disk per AGENTS.md.
        return self.async_create_entry(
            title=title or f"AGL {contract_number or 'account'}",
            data={
                CONF_REFRESH_TOKEN: self._refresh_token,
                CONF_CONTRACT_NUMBER: contract_number,
                CONF_ACCOUNT_NUMBER: account_number,
                CONF_PINNED_SPKI_AUTH: self._auth_spki,
                CONF_PINNED_SPKI_BFF: self._bff_spki,
            },
        )
