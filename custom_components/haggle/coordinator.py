"""DataUpdateCoordinator for haggle.

Runs two poll cycles (per AGL-API-FINDINGS.md section 3):
  - Hourly (30-min) series: daily, for yesterday. Don't poll today -- empty.
  - Daily series: every 6 h, to pick up newly available days.

Historical data (past intervals) is pushed to HA's recorder via
async_add_external_statistics() rather than a live state update. This
ensures the Energy dashboard attributes consumption to the interval it
actually occurred in, not to the time of the poll.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .agl.client import AGLAuthError, AGLError
from .const import (
    BACKFILL_DAYS,
    DOMAIN,
    SCAN_INTERVAL_HOURLY,
    STAT_CONSUMPTION,
    STAT_COST,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .agl.client import AglClient, IntervalReading

_LOGGER = logging.getLogger(__name__)


@dataclass
class HaggleData:
    """Typed coordinator data returned from _async_update_data."""

    consumption_today_kwh: float
    consumption_period_kwh: float
    consumption_period_cost_aud: float
    bill_projection_aud: float | None
    unit_rate_aud_per_kwh: float | None
    supply_charge_aud_per_day: float | None
    latest_cumulative_kwh: float  # for the TOTAL_INCREASING sensor


class HaggleCoordinator(DataUpdateCoordinator[HaggleData]):
    """Fetches AGL data and drives statistics import."""

    config_entry: ConfigEntry[object]

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry[object],
        client: AglClient,
        contract_number: str,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=SCAN_INTERVAL_HOURLY,
            config_entry=entry,
        )
        self.client = client
        self.contract_number = contract_number
        self._latest_cumulative_kwh: float = 0.0

    async def _async_setup(self) -> None:
        """One-time setup: 30-day backfill on first install.

        Called by async_config_entry_first_refresh before the first
        _async_update_data. Fetches up to BACKFILL_DAYS of Previous/Hourly
        intervals and imports them into the recorder statistics table.
        """
        today = date.today()
        all_intervals: list[IntervalReading] = []
        for i in range(1, BACKFILL_DAYS + 1):
            day = today - timedelta(days=i)
            try:
                readings = await self.client.async_get_usage_hourly_previous(
                    self.contract_number, day
                )
                all_intervals.extend(readings)
            except (AGLError, NotImplementedError) as err:
                _LOGGER.debug("Backfill skip %s: %s", day, err)
        if all_intervals:
            await self._import_intervals(
                all_intervals,
                initial_cons_sum=0.0,
                initial_cost_sum=0.0,
            )

    async def _async_update_data(self) -> HaggleData:
        """Fetch yesterday's intervals, import statistics, return sensor data."""
        try:
            return await self._fetch_and_import()
        except AGLAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except AGLError as err:
            raise UpdateFailed(str(err)) from err

    async def _fetch_and_import(self) -> HaggleData:
        """Core update: fetch missing days + push to recorder, then return data."""
        stat_id_cons = f"{DOMAIN}:{STAT_CONSUMPTION}_{self.contract_number}"

        # Determine resume point from existing statistics.
        last_sum = await self._get_last_stat_sum(stat_id_cons)

        if last_sum is None:
            # No previous stats -- run backfill first.
            await self._async_setup()
        else:
            # Fetch each missing day from day-after-last up to yesterday.
            await self._fetch_missing_days(last_sum)

        # Fetch live sensor data for the coordinator snapshot.
        try:
            summary = await self.client.async_get_usage_summary(self.contract_number)
        except NotImplementedError:
            summary = None

        try:
            plan = await self.client.async_get_plan(self.contract_number)
        except NotImplementedError:
            plan = None

        # Extract a flat unit rate: first c/kWh entry.
        unit_rate_aud: float | None = None
        if plan is not None:
            for rate in plan.unit_rates:
                if rate.get("type") == "c/kWh":
                    cents = float(rate.get("price") or 0.0)
                    unit_rate_aud = cents / 100.0
                    break

        supply_charge_aud: float | None = None
        if plan is not None and plan.supply_charge_cents_per_day:
            supply_charge_aud = plan.supply_charge_cents_per_day / 100.0

        # Parse bill-period totals.
        period_kwh: float = 0.0
        period_cost: float = 0.0
        projection: float | None = None

        if summary is not None:
            with contextlib.suppress(TypeError, ValueError):
                period_kwh = float(summary.consumption_kwh)
            with contextlib.suppress(ValueError, AttributeError):
                period_cost = float(summary.cost_label.lstrip("$").replace(",", ""))
            with contextlib.suppress(ValueError, AttributeError):
                projection = float(
                    summary.projection_label.lstrip("$").replace(",", "")
                )

        return HaggleData(
            consumption_today_kwh=0.0,
            consumption_period_kwh=period_kwh,
            consumption_period_cost_aud=period_cost,
            bill_projection_aud=projection,
            unit_rate_aud_per_kwh=unit_rate_aud,
            supply_charge_aud_per_day=supply_charge_aud,
            latest_cumulative_kwh=self._latest_cumulative_kwh,
        )

    async def _get_last_stat_sum(self, stat_id: str) -> float | None:
        """Return the last known cumulative sum for stat_id, or None."""
        # Local imports: recorder is optional and may not be loaded yet.
        from homeassistant.components.recorder.statistics import (
            get_last_statistics,
        )
        from homeassistant.helpers.recorder import get_instance

        last = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, stat_id, True, {"start", "sum"}
        )
        if not last or stat_id not in last:
            return None
        rows = last[stat_id]
        if not rows:
            return None
        row = rows[0]
        val = row.get("sum")
        return float(val) if val is not None else None

    async def _fetch_missing_days(self, last_sum: float) -> None:
        """Fetch each day since the last statistic and import intervals."""
        # Local imports: recorder is optional and may not be loaded yet.
        from homeassistant.components.recorder.statistics import (
            get_last_statistics,
        )
        from homeassistant.helpers.recorder import get_instance

        stat_id_cons = f"{DOMAIN}:{STAT_CONSUMPTION}_{self.contract_number}"
        last = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, stat_id_cons, True, {"start", "sum"}
        )

        last_start: datetime | None = None
        if last and stat_id_cons in last and last[stat_id_cons]:
            # StatisticsRow.start is a Unix timestamp (float).
            raw_start: float = last[stat_id_cons][0].get("start") or 0.0
            if raw_start:
                last_start = datetime.fromtimestamp(raw_start, tz=UTC)

        today = date.today()
        yesterday = today - timedelta(days=1)

        if last_start is None:
            self._latest_cumulative_kwh = last_sum
            return

        next_day = last_start.date() + timedelta(days=1)
        if next_day > yesterday:
            self._latest_cumulative_kwh = last_sum
            return

        all_intervals: list[IntervalReading] = []
        current = next_day
        while current <= yesterday:
            try:
                readings = await self.client.async_get_usage_hourly(
                    self.contract_number, current
                )
                all_intervals.extend(readings)
            except (AGLError, NotImplementedError) as err:
                _LOGGER.debug("Incremental fetch skip %s: %s", current, err)
            current += timedelta(days=1)

        if all_intervals:
            await self._import_intervals(
                all_intervals,
                initial_cons_sum=last_sum,
                initial_cost_sum=0.0,
            )
        else:
            self._latest_cumulative_kwh = last_sum

    async def _import_intervals(
        self,
        intervals: list[IntervalReading],
        initial_cons_sum: float,
        initial_cost_sum: float,
    ) -> None:
        """Aggregate 30-min intervals to hourly and push to recorder statistics."""
        # Local imports: recorder must not be imported at module level.
        from homeassistant.components.recorder.models import (
            StatisticData,
            StatisticMeanType,
            StatisticMetaData,
        )
        from homeassistant.components.recorder.statistics import (
            async_add_external_statistics,
        )
        from homeassistant.const import UnitOfEnergy

        # Aggregate 30-min slots into hourly UTC buckets.
        hour_cons: dict[datetime, float] = {}
        hour_cost: dict[datetime, float] = {}
        for r in intervals:
            h = r.dt.replace(minute=0, second=0, microsecond=0)
            hour_cons[h] = hour_cons.get(h, 0.0) + r.kwh
            hour_cost[h] = hour_cost.get(h, 0.0) + r.cost_aud

        sorted_hours = sorted(hour_cons)
        cons_sum = initial_cons_sum
        cost_sum = initial_cost_sum
        cons_stats: list[StatisticData] = []
        cost_stats: list[StatisticData] = []

        for h in sorted_hours:
            cons_sum += hour_cons[h]
            cost_sum += hour_cost[h]
            cons_stats.append(StatisticData(start=h, state=hour_cons[h], sum=cons_sum))
            cost_stats.append(StatisticData(start=h, state=hour_cost[h], sum=cost_sum))

        stat_id_cons = f"{DOMAIN}:{STAT_CONSUMPTION}_{self.contract_number}"
        stat_id_cost = f"{DOMAIN}:{STAT_COST}_{self.contract_number}"

        async_add_external_statistics(
            self.hass,
            StatisticMetaData(
                mean_type=StatisticMeanType.NONE,
                unit_class=None,
                has_sum=True,
                name=f"AGL Electricity Consumption ({self.contract_number})",
                source=DOMAIN,
                statistic_id=stat_id_cons,
                unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            ),
            cons_stats,
        )

        async_add_external_statistics(
            self.hass,
            StatisticMetaData(
                mean_type=StatisticMeanType.NONE,
                unit_class=None,
                has_sum=True,
                name=f"AGL Electricity Cost ({self.contract_number})",
                source=DOMAIN,
                statistic_id=stat_id_cost,
                unit_of_measurement="AUD",
            ),
            cost_stats,
        )

        # Update the in-memory cumulative for the TOTAL_INCREASING sensor.
        if cons_stats:
            self._latest_cumulative_kwh = cons_sum
