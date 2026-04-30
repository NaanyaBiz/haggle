"""DataUpdateCoordinator for haggle.

Runs two poll cycles (per AGL-API-FINDINGS.md §3):
  - Hourly (30-min) series: daily, for yesterday. Don't poll today — empty.
  - Daily series: every 6 h, to pick up newly available days.

Plan/overview is fetched weekly in the background (low-priority, rarely
changes). Both are handled via a single coordinator; the coordinator data
dict holds the aggregated result.

Historical data (past intervals) is pushed to HA's recorder via
`async_import_statistics()` rather than a live state update. This ensures
the Energy dashboard attributes consumption to the interval it actually
occurred in, not to the time of the poll.
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
from .const import DOMAIN, SCAN_INTERVAL_HOURLY

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .agl.client import AglClient

_LOGGER = logging.getLogger(__name__)


class HaggleCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetches AGL data and drives statistics import."""

    config_entry: ConfigEntry[Any]

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry[Any],
        client: AglClient,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=SCAN_INTERVAL_HOURLY,
            config_entry=entry,
        )
        self.client = client

    async def _async_setup(self) -> None:
        """One-time setup: validate connectivity + do initial backfill.

        Per HA 2024.8+ guidance, prefer _async_setup over setup work in
        _async_update_data. Backfill last 30 days of /Hourly on first run;
        subsequent runs only fetch yesterday. Rate-limit backfill to ~1 req/s
        (watch for 429s).
        """
        raise NotImplementedError("agl-api: implement in Sprint 1")

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch yesterday's intervals, import statistics, return sensor data."""
        try:
            return await self._fetch_and_import()
        except AGLAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except AGLError as err:
            raise UpdateFailed(str(err)) from err

    async def _fetch_and_import(self) -> dict[str, Any]:
        """Core update: fetch /Hourly for yesterday + push to recorder."""
        raise NotImplementedError("agl-api: implement in Sprint 1")
