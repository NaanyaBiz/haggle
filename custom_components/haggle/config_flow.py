"""Config flow for haggle.

Onboarding strategy (per AGL-API-FINDINGS.md §1, Decision for v1):
  Step 1 — user provides a refresh token (obtained by running mitmproxy
            against the AGL iOS app, then copying the token from the
            oauth/token response).
  Step 2 — integration uses the refresh token to fetch /api/v3/overview
            and discover available contracts; if >1, user selects.
  Step 3 — entry is created; coordinator takes over.

PKCE flow (Sprint 2): replace Step 1 with an OAuth redirect so the user
just clicks "Log in" in the HA UI and doesn't need mitmproxy at all.
"""

from __future__ import annotations

from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .agl.client import AglAuth, AGLAuthError, AglClient, AGLError, Contract
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_ACCESS_TOKEN_EXPIRY,
    CONF_ACCOUNT_NUMBER,
    CONF_CONTRACT_NUMBER,
    CONF_REFRESH_TOKEN,
    DOMAIN,
)

REFRESH_TOKEN_SCHEMA = vol.Schema({vol.Required(CONF_REFRESH_TOKEN): str})


class HaggleConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the haggle config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._refresh_token: str | None = None
        self._contracts: list[Contract] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1: accept a refresh token from the user.

        The token was obtained by proxying the AGL iOS app (see README).
        PKCE replaces this in Sprint 2.
        """
        errors: dict[str, str] = {}
        if user_input is not None:
            token = user_input[CONF_REFRESH_TOKEN].strip()
            try:
                contracts = await self._discover_contracts(token)
                self._refresh_token = token
                self._contracts = contracts
            except AGLAuthError:
                errors["base"] = "invalid_auth"
            except (AGLError, TimeoutError):
                errors["base"] = "cannot_connect"
            except NotImplementedError:
                # Stub path: pretend discovery worked with a placeholder.
                self._refresh_token = token
                self._contracts = []
                return await self.async_step_select_contract()
            else:
                return await self.async_step_select_contract()

        return self.async_show_form(
            step_id="user",
            data_schema=REFRESH_TOKEN_SCHEMA,
            errors=errors,
        )

    async def async_step_select_contract(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2: select contract if multiple discovered.

        Single-contract accounts skip selection and create the entry directly.
        """
        if not self._contracts:
            # Stub path or single contract — create entry with placeholders.
            return await self._create_entry(contract_number="", account_number="")

        if len(self._contracts) == 1:
            c = self._contracts[0]
            return await self._create_entry(
                contract_number=c.contract_number,
                account_number=c.account_number,
            )

        if user_input is not None:
            chosen = user_input[CONF_CONTRACT_NUMBER]
            contract = next(
                (c for c in self._contracts if c.contract_number == chosen),
                self._contracts[0],
            )
            return await self._create_entry(
                contract_number=contract.contract_number,
                account_number=contract.account_number,
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

    async def async_step_reauth(self, _entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Reauth when the refresh token has been revoked.

        Re-enters at Step 1; the rotated token in the config entry should
        normally prevent this — it only fires if the token is force-revoked
        (e.g. user changed AGL password) or somehow lost.
        """
        return await self.async_step_user()

    async def _create_entry(
        self, contract_number: str, account_number: str
    ) -> ConfigFlowResult:
        assert self._refresh_token is not None
        unique_id = f"{account_number}_{contract_number}" or self._refresh_token[:16]
        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=f"AGL {contract_number or 'account'}",
            data={
                CONF_REFRESH_TOKEN: self._refresh_token,
                CONF_ACCESS_TOKEN: "",
                CONF_ACCESS_TOKEN_EXPIRY: 0,
                CONF_CONTRACT_NUMBER: contract_number,
                CONF_ACCOUNT_NUMBER: account_number,
            },
        )

    async def _discover_contracts(self, refresh_token: str) -> list[Contract]:
        """Exchange token and fetch /overview to discover contracts."""

        async def _noop_persist(_token: str) -> None:
            pass  # during discovery we don't have an entry yet

        auth = AglAuth(refresh_token, _noop_persist)
        async with aiohttp.ClientSession() as session:
            access_token = await auth.async_ensure_valid_token(session)
            _ = access_token  # will be used by AglClient in Sprint 1
            client = AglClient(auth, session)
            return await client.async_get_overview()
