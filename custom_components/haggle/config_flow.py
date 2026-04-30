"""Config flow for haggle.

Three steps: `user` (email + password) -> `otp` (one-time code) -> finish.
Reauth re-enters at `otp` because session-cookie expiry is the typical
failure mode and the user's stored credentials are still valid.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.config_entries import SOURCE_REAUTH, ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD

from .agl.client import AGLAuthError, AGLClient, AGLError
from .const import CONF_OTP, DOMAIN

if TYPE_CHECKING:
    from collections.abc import Mapping

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
    }
)

OTP_SCHEMA = vol.Schema({vol.Required(CONF_OTP): str})


class HaggleConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the haggle config flow."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._email: str | None = None
        self._password: str | None = None
        self._client: AGLClient | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1: collect email + password."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._email = user_input[CONF_EMAIL]
            self._password = user_input[CONF_PASSWORD]
            try:
                # Stubbed: real client.async_login() submits creds and
                # triggers AGL to send an OTP via SMS / email.
                client = AGLClient(self.hass, entry=None)  # type: ignore[arg-type]
                assert self._email is not None
                assert self._password is not None
                await client.async_login(self._email, self._password)
                self._client = client
            except AGLAuthError:
                errors["base"] = "invalid_auth"
            except AGLError:
                errors["base"] = "cannot_connect"
            except NotImplementedError:
                # Stub path: pretend login worked and advance to OTP step.
                # Removed once agl-portal-explorer wires the real client.
                return await self.async_step_otp()
            else:
                return await self.async_step_otp()

        return self.async_show_form(
            step_id="user", data_schema=USER_SCHEMA, errors=errors
        )

    async def async_step_otp(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2: collect the OTP AGL just sent the user."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                if self._client is not None:
                    await self._client.async_submit_otp(user_input[CONF_OTP])
            except AGLAuthError:
                errors["base"] = "invalid_otp"
            except AGLError:
                errors["base"] = "cannot_connect"
            except NotImplementedError:
                # Stub: skip validation; treat as success.
                pass

            if not errors:
                if self.source == SOURCE_REAUTH:
                    return self.async_update_reload_and_abort(
                        self._get_reauth_entry(),
                        data_updates={CONF_OTP: user_input[CONF_OTP]},
                    )

                assert self._email is not None
                await self.async_set_unique_id(self._email.lower())
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"AGL ({self._email})",
                    data={
                        CONF_EMAIL: self._email,
                        CONF_PASSWORD: self._password,
                    },
                )

        return self.async_show_form(
            step_id="otp", data_schema=OTP_SCHEMA, errors=errors
        )

    async def async_step_reauth(
        self, _entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Reauth on session-cookie expiry: jump straight to OTP."""
        return await self.async_step_otp()
