"""Config flow for haggle.

Onboarding strategy:
  Step 1 (user) -- generate PKCE verifier + challenge, build the /authorize
            URL, show it to the user. The user opens it in a browser, logs
            in, and is redirected to a "Not Found" page. They copy the full
            URL from the address bar and paste it back into HA.
  Step 2 (exchange) -- extract the authorization code from the pasted URL,
            POST /oauth/token (authorization_code grant + PKCE), store tokens.
  Step 3 (select_contract) -- fetch /api/v3/overview, let user pick a contract
            if multiple electricity contracts are found.
  Step 4 -- create entry.

Reauth re-enters at Step 1 with fresh PKCE params.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .agl.client import AglAuth, AGLAuthError, AglClient, AGLError, Contract
from .const import (
    AGL_AUTH0_CLIENT,
    AGL_AUTH_HOST,
    AGL_CLIENT_FLAVOR,
    AGL_CLIENT_ID,
    AGL_OAUTH_AUDIENCE,
    AGL_OAUTH_SCOPE,
    AGL_REDIRECT_URI,
    AGL_USER_AGENT,
    CONF_ACCESS_TOKEN,
    CONF_ACCESS_TOKEN_EXPIRY,
    CONF_ACCOUNT_NUMBER,
    CONF_CONTRACT_NUMBER,
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


async def _exchange_code(code: str, verifier: str) -> tuple[str, str]:
    """POST /oauth/token and return (access_token, refresh_token).

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
    async with (
        aiohttp.ClientSession() as session,
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

    return access_token, refresh_token


async def _fetch_contracts(access_token: str) -> list[Contract]:
    """Fetch /api/v3/overview with the given access token.

    Used during config flow before the coordinator session is set up.
    Raises AGLError / NotImplementedError on stub -- callers handle gracefully.
    """

    async def _noop_persist(_token: str) -> None:
        pass

    # AglAuth is used here only as a container; token rotation won\'t trigger
    # because async_ensure_valid_token is a stub in the current sprint.
    auth = AglAuth(access_token, _noop_persist)
    async with aiohttp.ClientSession() as session:
        client = AglClient(auth, session)
        return await client.async_get_overview()


class HaggleConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the haggle config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._pkce_verifier: str = ""
        self._pkce_challenge: str = ""
        self._oauth_state: str = ""
        self._access_token: str = ""
        self._refresh_token: str = ""
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
            access_token, refresh_token = await _exchange_code(
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

        self._access_token = access_token
        self._refresh_token = refresh_token
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
                self._contracts = await _fetch_contracts(self._access_token)
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
        unique_id = (
            f"{account_number}_{contract_number}"
            if account_number and contract_number
            else self._refresh_token[:16]
            if self._refresh_token
            else "unknown"
        )
        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured()
        _LOGGER.info(
            "Creating haggle entry: account=%s contract=%s",
            account_number or "unknown",
            contract_number or "unknown",
        )
        return self.async_create_entry(
            title=title or f"AGL {contract_number or 'account'}",
            data={
                CONF_REFRESH_TOKEN: self._refresh_token,
                CONF_ACCESS_TOKEN: "",
                CONF_ACCESS_TOKEN_EXPIRY: 0,
                CONF_CONTRACT_NUMBER: contract_number,
                CONF_ACCOUNT_NUMBER: account_number,
            },
        )
