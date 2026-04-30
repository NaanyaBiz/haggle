"""Smoke tests for setup / unload roundtrip."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.haggle.const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


async def test_setup_and_unload(hass: HomeAssistant) -> None:
    """A config entry sets up, registers a sensor platform, then unloads."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_EMAIL: "test@example.com", CONF_PASSWORD: "pw"},
        unique_id="test@example.com",
    )
    entry.add_to_hass(hass)

    with (
        patch("custom_components.haggle.agl.client.AGLClient.async_ensure_session"),
        patch(
            "custom_components.haggle.agl.client.AGLClient.async_fetch_usage",
            return_value={"grid_import_kwh": 1.0, "grid_export_kwh": 0.0},
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert entry.runtime_data is not None
        assert entry.runtime_data.coordinator.last_update_success is True

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
