"""Smoke tests for setup / unload roundtrip."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.haggle.const import (
    CONF_ACCESS_TOKEN,
    CONF_ACCESS_TOKEN_EXPIRY,
    CONF_ACCOUNT_NUMBER,
    CONF_CONTRACT_NUMBER,
    CONF_REFRESH_TOKEN,
    DOMAIN,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_ENTRY_DATA = {
    CONF_REFRESH_TOKEN: "v1.testtoken",
    CONF_ACCESS_TOKEN: "",
    CONF_ACCESS_TOKEN_EXPIRY: 0,
    CONF_CONTRACT_NUMBER: "9999999999",
    CONF_ACCOUNT_NUMBER: "1234567890",
}

_COORDINATOR_DATA = {
    "consumption_kwh": 259.0,
    "consumption_today": 8.5,
    "consumption_period": 259.0,
    "bill_projection": 139.15,
    "unit_rate": 0.33792,
    "supply_charge": 1.31714,
}


async def test_setup_and_unload(hass: HomeAssistant) -> None:
    """Config entry sets up, registers sensor platform, then unloads cleanly."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=_ENTRY_DATA,
        unique_id="1234567890_9999999999",
    )
    entry.add_to_hass(hass)

    mock_session = MagicMock()
    mock_session.close = AsyncMock()

    with (
        patch(
            "custom_components.haggle.aiohttp.ClientSession",
            return_value=mock_session,
        ),  # aiohttp imported at top-level of __init__ so this path resolves
        patch(
            "custom_components.haggle.agl.client.AglAuth.async_ensure_valid_token",
            new_callable=AsyncMock,
            return_value="access_token",
        ),
        patch(
            "custom_components.haggle.coordinator.HaggleCoordinator._async_setup",
            new_callable=AsyncMock,
        ),
        patch(
            "custom_components.haggle.coordinator.HaggleCoordinator._async_update_data",
            new_callable=AsyncMock,
            return_value=_COORDINATOR_DATA,
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert entry.runtime_data is not None
        assert entry.runtime_data.coordinator.last_update_success is True

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        mock_session.close.assert_called_once()
