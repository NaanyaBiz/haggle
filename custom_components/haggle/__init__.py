"""The haggle integration.

Pulls smart-meter usage from the AGL Australia customer portal and feeds
it into the Home Assistant Energy dashboard. See AGENTS.md for design
notes (daily polling, OTP reauth, NMI vs serial).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.const import Platform

from .agl.client import AGLClient
from .coordinator import HaggleCoordinator

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

PLATFORMS: list[Platform] = [Platform.SENSOR]

type HaggleConfigEntry = ConfigEntry[HaggleRuntimeData]


@dataclass(slots=True)
class HaggleRuntimeData:
    """Per-config-entry runtime state.

    Stored on `entry.runtime_data` (HA 2025.1+ pattern; replaces the legacy
    `hass.data[DOMAIN][entry_id]` shape).
    """

    client: AGLClient
    coordinator: HaggleCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: HaggleConfigEntry) -> bool:
    """Set up haggle from a config entry."""
    client = AGLClient.from_entry(hass, entry)
    coordinator = HaggleCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = HaggleRuntimeData(client=client, coordinator=coordinator)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: HaggleConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.client.async_close()
    return unload_ok
