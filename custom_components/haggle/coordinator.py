"""DataUpdateCoordinator for haggle.

Polls the AGL portal once per `SCAN_INTERVAL` (24 h). AGL data lags
24-48 h so faster polling is pointless and burns rate-limit budget.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .agl.client import AGLAuthError, AGLError
from .const import DOMAIN, SCAN_INTERVAL

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .agl.client import AGLClient

_LOGGER = logging.getLogger(__name__)


class HaggleCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator that fetches NEM12 daily and parses it for sensors."""

    config_entry: ConfigEntry[Any]

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry[Any],
        client: AGLClient,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=SCAN_INTERVAL,
            config_entry=entry,
        )
        self.client = client

    async def _async_setup(self) -> None:
        """One-time setup before the first refresh.

        Per HA 2024.8+ guidance, prefer this over doing setup work in the
        first call to `_async_update_data`. Fail loudly here so HA can
        retry / surface a clean error to the user.
        """
        await self.client.async_ensure_session()

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch the latest NEM12 export and return parsed totals."""
        try:
            return await self.client.async_fetch_usage()
        except AGLAuthError as err:
            # Triggers the reauth flow; user re-enters OTP.
            raise ConfigEntryAuthFailed(str(err)) from err
        except AGLError as err:
            raise UpdateFailed(str(err)) from err
