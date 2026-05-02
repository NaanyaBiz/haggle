"""The haggle integration.

Fetches smart-meter data from the AGL Energy API and feeds it into the
HA Energy dashboard. See AGENTS.md for design notes (Auth0 refresh-token
rotation, daily polling, import_statistics for historical data).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import aiohttp
from homeassistant.const import Platform

from .agl.client import AglAuth, AglClient
from .const import CONF_CONTRACT_NUMBER, CONF_REFRESH_TOKEN
from .coordinator import HaggleCoordinator

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]

type HaggleConfigEntry = ConfigEntry[HaggleRuntimeData]


@dataclass(slots=True)
class HaggleRuntimeData:
    """Per-config-entry runtime state.

    Stored on `entry.runtime_data` (HA 2025.1+ pattern).
    """

    auth: AglAuth
    client: AglClient
    coordinator: HaggleCoordinator
    session: aiohttp.ClientSession


async def async_setup_entry(hass: HomeAssistant, entry: HaggleConfigEntry) -> bool:
    """Set up haggle from a config entry."""
    refresh_token = entry.data[CONF_REFRESH_TOKEN]
    contract_number: str = entry.data.get(CONF_CONTRACT_NUMBER, "")

    _LOGGER.info("Setting up haggle entry: contract=%s", contract_number or "unknown")

    async def _persist_refresh_token(new_token: str) -> None:
        """Persist rotated refresh token back to config entry data.

        Auth0 has already consumed the previous refresh token by the time this
        callback runs; failing to persist the new one means the next HA restart
        will load a stale (revoked) token. Surface that immediately via the
        reauth flow rather than letting the user discover it on next restart.
        """
        try:
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_REFRESH_TOKEN: new_token}
            )
            _LOGGER.debug("Refresh token persisted (len=%d)", len(new_token))
        except Exception:
            _LOGGER.exception(
                "Failed to persist rotated refresh token — triggering reauth"
            )
            entry.async_start_reauth(hass)

    session = aiohttp.ClientSession()
    auth = AglAuth(refresh_token, _persist_refresh_token)
    client = AglClient(auth, session)
    coordinator = HaggleCoordinator(hass, entry, client, contract_number)  # type: ignore[arg-type]

    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = HaggleRuntimeData(
        auth=auth,
        client=client,
        coordinator=coordinator,
        session=session,
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: HaggleConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.session.close()
    return unload_ok
