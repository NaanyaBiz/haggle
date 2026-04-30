"""Smoke tests for the config flow.

v1 flow: user pastes refresh token -> contracts auto-discovered ->
         single contract creates entry; multiple contracts show selector.

The AGL client is fully stubbed (NotImplementedError) so these tests
verify flow navigation and entry creation shape only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType

from custom_components.haggle.const import CONF_REFRESH_TOKEN, DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


async def test_user_step_shows_form(hass: HomeAssistant) -> None:
    """User step renders the refresh-token form."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"


async def test_user_flow_stub_creates_entry(hass: HomeAssistant) -> None:
    """Stub path (NotImplementedError) completes and creates an entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CONF_REFRESH_TOKEN: "v1.testtoken123"},
    )
    # Stub path skips discovery and jumps to select_contract with no contracts,
    # which creates the entry immediately.
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_REFRESH_TOKEN] == "v1.testtoken123"
