"""Smoke tests for the config flow.

Walks user -> otp -> entry creation. The AGL client is stubbed
(NotImplementedError -> the flow's stub-path branches), so this test
verifies step navigation and entry creation, not real auth.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.data_entry_flow import FlowResultType

from custom_components.haggle.const import CONF_OTP, DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


async def test_user_flow_to_otp_creates_entry(hass: HomeAssistant) -> None:
    """user step accepts creds, otp step accepts code, entry is created."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CONF_EMAIL: "user@example.com", CONF_PASSWORD: "pw"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "otp"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_OTP: "123456"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "AGL (user@example.com)"
    assert result["data"] == {
        CONF_EMAIL: "user@example.com",
        CONF_PASSWORD: "pw",
    }
