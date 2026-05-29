"""DataUpdateCoordinator for haggle.

Runs two poll cycles (per AGL-API-FINDINGS.md section 3):
  - Hourly (30-min) series: daily, for yesterday. Don't poll today -- empty.
  - Daily series: every 6 h, to pick up newly available days.

Historical data (past intervals) is pushed to HA's recorder via
async_add_external_statistics() rather than a live state update. This
ensures the Energy dashboard attributes consumption to the interval it
actually occurred in, not to the time of the poll.

Backfill strategy: first install pulls up to BACKFILL_DAYS of history, but
throttled to BACKFILL_CHUNK_DAYS per 24 h poll so we don't hammer the AGL
BFF on startup. Smart endpoint selection per day: days inside the current
billing period use Current/Hourly; older days use Previous/Hourly.

Once initial backfill is complete, every poll re-fetches the trailing
REWINDOW_DAYS so AGL's day-late AEMO backfills self-heal — a slot first
returned as a placeholder is overwritten once AGL has the real read. The
recorder is idempotent on (statistic_id, start), so the overwrite is safe.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .agl.client import AGLAuthError, AGLError, AGLRateLimitError
from .const import (
    BACKFILL_CHUNK_DAYS,
    BACKFILL_DAYS,
    BACKFILL_INTER_REQUEST_DELAY,
    DOMAIN,
    REWINDOW_DAYS,
    SCAN_INTERVAL_HOURLY,
    STAT_CONSUMPTION,
    STAT_COST,
    TARIFF_LABELS,
    TARIFF_OFFPEAK,
    TARIFF_PEAK,
    TARIFF_SHOULDER,
    TOU_BANDS,
    TOU_SERIES_TARIFFS,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .agl.client import AglClient
    from .agl.models import IntervalReading

_LOGGER = logging.getLogger(__name__)


def _safe_float(raw: Any) -> float:
    """Coerce raw API value to a non-negative finite float, defaulting to 0.0."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(value) or value < 0:
        _LOGGER.warning("Rejecting non-finite/negative coordinator value: %r", raw)
        return 0.0
    return value


@dataclass
class HaggleData:
    """Typed coordinator data returned from _async_update_data."""

    consumption_period_kwh: float
    consumption_period_cost_aud: float
    bill_projection_aud: float | None
    unit_rate_aud_per_kwh: float | None
    supply_charge_aud_per_day: float | None
    latest_cumulative_kwh: float  # for the TOTAL_INCREASING sensor
    # ToU extras (defaulted so existing construction sites/tests are unaffected).
    # active_tariffs drives conditional ToU rate-sensor registration in
    # sensor.py; empty on a flat-rate contract so those sensors never appear.
    active_tariffs: frozenset[str] = frozenset()
    unit_rate_peak_aud_per_kwh: float | None = None
    unit_rate_offpeak_aud_per_kwh: float | None = None
    unit_rate_shoulder_aud_per_kwh: float | None = None


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
        # ToU bands (peak/offpeak/shoulder) known to have statistics for this
        # contract. Monotonic within a process; seeded from stored rows each
        # cycle so it survives restarts. Drives rate-sensor registration and
        # the schedule-reload-on-growth path in _maybe_reload_for_new_tariffs.
        self._active_tou_bands: set[str] = set()
        self._prev_active_tou_bands: set[str] = set()

    def _tariff_stat_ids(self, tariff: str) -> tuple[str, str]:
        """Return (consumption_id, cost_id) for a per-tariff series."""
        return (
            f"{DOMAIN}:{STAT_CONSUMPTION}_{tariff}_{self.contract_number}",
            f"{DOMAIN}:{STAT_COST}_{tariff}_{self.contract_number}",
        )

    async def _async_setup(self) -> None:
        """No-op: first-install backfill is handled incrementally in _fetch_and_import."""

    async def _async_update_data(self) -> HaggleData:
        """Fetch yesterday's intervals, import statistics, return sensor data."""
        try:
            return await self._fetch_and_import()
        except AGLAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except AGLError as err:
            raise UpdateFailed(str(err)) from err

    async def _fetch_and_import(self) -> HaggleData:
        """Core update: fetch missing intervals + trailing rewindow, return data."""
        stat_id_cons = f"{DOMAIN}:{STAT_CONSUMPTION}_{self.contract_number}"
        stat_id_cost = f"{DOMAIN}:{STAT_COST}_{self.contract_number}"

        # Fetch live sensor data first — summary gives bill_start for endpoint selection.
        summary = await self.client.async_get_usage_summary(self.contract_number)
        plan = await self.client.async_get_plan(self.contract_number)

        bill_start: date = summary.start

        # Determine resume point — overlap both recorder lookups on the executor.
        (last_cons_sum, last_stat_date), (last_cost_sum, _) = await asyncio.gather(
            self._get_last_stat(stat_id_cons),
            self._get_last_stat(stat_id_cost),
        )

        # AGL `dateTime` slots are UTC; using `date.today()` (OS local time)
        # would skew the fetch range by a day around midnight in non-UTC zones.
        today = datetime.now(UTC).date()
        yesterday = today - timedelta(days=1)

        (
            fetch_start,
            initial_cons_sum,
            initial_cost_sum,
        ) = await self._resolve_resume_point(
            today,
            last_stat_date,
            last_cons_sum,
            last_cost_sum,
            stat_id_cons,
            stat_id_cost,
        )
        fetch_end = min(
            yesterday, fetch_start + timedelta(days=BACKFILL_CHUNK_DAYS - 1)
        )

        # Seed the sensor with the most-recent known cumulative; _fetch_range
        # bumps it forward when the import produces new rows.
        self._latest_cumulative_kwh = last_cons_sum or 0.0

        # Resolve per-tariff baselines at the SAME fetch_start as the aggregate
        # so each per-tariff series resumes from its own stored cumulative
        # (never the aggregate's, and never the per-tariff series' own last
        # date). Skipped on first install (no prior rows anywhere → all 0.0).
        # known_bands = ToU bands that already have stored statistics, so the
        # `normal`/anytime per-tariff series is only emitted on a contract that
        # has been seen using ToU (keeps flat-rate contracts on the aggregate
        # series alone).
        known_bands, tariff_initial_sums = await self._resolve_tou_state(
            fetch_start, last_stat_date
        )
        self._active_tou_bands |= known_bands

        if fetch_start <= yesterday:
            await self._fetch_range(
                fetch_start,
                fetch_end,
                bill_start,
                initial_cons_sum=initial_cons_sum,
                initial_cost_sum=initial_cost_sum,
                tariff_initial_sums=tariff_initial_sums,
                known_bands=frozenset(self._active_tou_bands),
            )

        # Extract rates from plan.
        unit_rate_aud: float | None = None
        for rate in plan.unit_rates:
            if rate.get("type") == "c/kWh":
                cents = _safe_float(rate.get("price"))
                unit_rate_aud = cents / 100.0
                break

        supply_charge_aud: float | None = None
        if plan.supply_charge_cents_per_day:
            supply_charge_aud = plan.supply_charge_cents_per_day / 100.0

        # Per-tariff rates: cents/kWh → AUD/kWh. Absent bands stay None (the
        # sensor reads `unavailable`), never a misleading 0.0.
        def _tou_rate(tariff: str) -> float | None:
            cents = plan.tou_unit_rates.get(tariff)
            return cents / 100.0 if cents is not None else None

        # A new ToU band appearing after first refresh means rate sensors need
        # to be added; schedule a (loop-safe, monotonic-growth) reload.
        self._maybe_reload_for_new_tariffs()

        # Parse bill-period totals.
        projection: float | None = None
        period_kwh = _safe_float(summary.consumption_kwh)
        period_cost = _safe_float(
            (summary.cost_label or "").lstrip("$").replace(",", "")
        )
        proj_label = (summary.projection_label or "").lstrip("$").replace(",", "")
        if proj_label:
            projection = _safe_float(proj_label)

        return HaggleData(
            consumption_period_kwh=period_kwh,
            consumption_period_cost_aud=period_cost,
            bill_projection_aud=projection,
            unit_rate_aud_per_kwh=unit_rate_aud,
            supply_charge_aud_per_day=supply_charge_aud,
            latest_cumulative_kwh=self._latest_cumulative_kwh,
            active_tariffs=frozenset(self._active_tou_bands),
            unit_rate_peak_aud_per_kwh=_tou_rate(TARIFF_PEAK),
            unit_rate_offpeak_aud_per_kwh=_tou_rate(TARIFF_OFFPEAK),
            unit_rate_shoulder_aud_per_kwh=_tou_rate(TARIFF_SHOULDER),
        )

    def _maybe_reload_for_new_tariffs(self) -> None:
        """Schedule a reload when a ToU band first appears after first refresh.

        ToU rate sensors are registered conditionally at platform setup from
        `active_tariffs`, so a flat-rate contract that later switches to a ToU
        plan would not surface the new rate sensors until the entry reloads.
        We schedule one then. Loop-safe: `_active_tou_bands` only ever grows
        (it is seeded from stored statistics), so the growth condition fires at
        most once per band. The very first refresh (`self.data is None`) never
        reloads — the platform is set up fresh straight after it.
        """
        new_bands = self._active_tou_bands - self._prev_active_tou_bands
        self._prev_active_tou_bands = set(self._active_tou_bands)
        if self.data is not None and new_bands:
            _LOGGER.info(
                "New ToU tariff band(s) %s detected; scheduling reload to add "
                "rate sensors",
                sorted(new_bands),
            )
            self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)

    async def _resolve_resume_point(
        self,
        today: date,
        last_stat_date: date | None,
        last_cons_sum: float | None,
        last_cost_sum: float | None,
        stat_id_cons: str,
        stat_id_cost: str,
    ) -> tuple[date, float, float]:
        """Choose fetch_start + initial sums per the resume-strategy decision tree.

        - First install: backfill from BACKFILL_DAYS ago, sums start at 0.
        - Big gap (> REWINDOW_DAYS behind): resume incrementally from
          last_stat_date + 1, using the last stored cumulative as the baseline.
        - Normal operation: re-fetch the trailing REWINDOW_DAYS so AGL's
          day-late AEMO backfills self-heal. Baseline is the sum at the hour
          right before fetch_start UTC midnight (looked up — NOT the latest
          stored sum, which is several days ahead of the rewindow start).
        """
        backfill_floor = today - timedelta(days=BACKFILL_DAYS)
        if last_stat_date is None:
            return backfill_floor, 0.0, 0.0
        if last_stat_date < today - timedelta(days=REWINDOW_DAYS):
            return (
                last_stat_date + timedelta(days=1),
                last_cons_sum or 0.0,
                last_cost_sum or 0.0,
            )
        fetch_start = max(today - timedelta(days=REWINDOW_DAYS), backfill_floor)
        fetch_start_utc = datetime.combine(fetch_start, time.min, tzinfo=UTC)
        baseline_cons, baseline_cost = await self._get_baseline_sums(
            stat_id_cons, stat_id_cost, fetch_start_utc
        )
        return fetch_start, baseline_cons, baseline_cost

    async def _get_last_stat(self, stat_id: str) -> tuple[float | None, date | None]:
        """Return (last_sum, last_date) for stat_id, or (None, None) if no rows."""
        from homeassistant.components.recorder.statistics import (
            get_last_statistics,
        )
        from homeassistant.helpers.recorder import get_instance

        last = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, stat_id, True, {"start", "sum"}
        )
        if not last or stat_id not in last:
            return None, None
        rows = last[stat_id]
        if not rows:
            return None, None
        row = rows[0]
        val = row.get("sum")
        raw_start: float = row.get("start") or 0.0
        last_sum = float(val) if val is not None else None
        last_date: date | None = None
        if raw_start:
            last_date = datetime.fromtimestamp(raw_start, tz=UTC).date()
        return last_sum, last_date

    async def _get_baseline_sums(
        self,
        stat_id_cons: str,
        stat_id_cost: str,
        before_dt: datetime,
    ) -> tuple[float, float]:
        """Return cumulative sums at the last hour strictly before before_dt.

        Used by the trailing-rewindow path so newly-imported rows resume from
        the correct baseline. Looks back 2 days to tolerate sparse data.
        Returns (0.0, 0.0) if no rows exist in the look-back window.
        """
        from homeassistant.components.recorder.statistics import (
            statistics_during_period,
        )
        from homeassistant.helpers.recorder import get_instance

        look_back = before_dt - timedelta(days=2)
        result = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            look_back,
            before_dt,
            {stat_id_cons, stat_id_cost},
            "hour",
            None,
            {"sum"},
        )

        def _last_sum(stat_id: str) -> float:
            rows = result.get(stat_id) or []
            if not rows:
                return 0.0
            last = rows[-1].get("sum")
            return float(last) if last is not None else 0.0

        return _last_sum(stat_id_cons), _last_sum(stat_id_cost)

    async def _resolve_tou_state(
        self, fetch_start: date, last_stat_date: date | None
    ) -> tuple[set[str], dict[str, tuple[float, float]]]:
        """Return (known ToU bands, per-tariff baseline sums) for this cycle.

        Single recorder-touching entry point for the ToU machinery, so unit
        tests can stub it in one place. `known_bands` marks the contract as
        Time-of-Use across restarts; the baselines resume each per-tariff
        series from its OWN stored cumulative at the SAME fetch_start as the
        aggregate (never the aggregate's sum, never the per-tariff series' own
        last date). First install (no prior aggregate row) → no baselines.
        """
        known_bands = await self._get_stored_tou_bands()
        tariff_initial_sums: dict[str, tuple[float, float]] = {}
        if last_stat_date is not None:
            fetch_start_utc = datetime.combine(fetch_start, time.min, tzinfo=UTC)
            band_ids: set[str] = set()
            for tariff in TOU_SERIES_TARIFFS:
                band_ids.update(self._tariff_stat_ids(tariff))
            band_sums = await self._get_tariff_baseline_sums(band_ids, fetch_start_utc)
            for tariff in TOU_SERIES_TARIFFS:
                cons_id, cost_id = self._tariff_stat_ids(tariff)
                tariff_initial_sums[tariff] = (
                    band_sums.get(cons_id, 0.0),
                    band_sums.get(cost_id, 0.0),
                )
        return known_bands, tariff_initial_sums

    async def _get_tariff_baseline_sums(
        self, stat_ids: set[str], before_dt: datetime
    ) -> dict[str, float]:
        """Return {stat_id: cumulative sum at the last hour strictly before before_dt}.

        Batched (one recorder call for all per-tariff series) so adding ToU
        doesn't multiply executor round-trips. Looks back BACKFILL_DAYS — wide
        enough that a sparse band (e.g. shoulder only on weekdays) still finds
        its true last sum rather than resetting to 0.0. Series with no rows in
        the window return 0.0.
        """
        if not stat_ids:
            return {}
        from homeassistant.components.recorder.statistics import (
            statistics_during_period,
        )
        from homeassistant.helpers.recorder import get_instance

        look_back = before_dt - timedelta(days=BACKFILL_DAYS)
        result = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            look_back,
            before_dt,
            set(stat_ids),
            "hour",
            None,
            {"sum"},
        )
        out: dict[str, float] = {}
        for stat_id in stat_ids:
            rows = result.get(stat_id) or []
            last = rows[-1].get("sum") if rows else None
            out[stat_id] = float(last) if last is not None else 0.0
        return out

    async def _get_stored_tou_bands(self) -> set[str]:
        """Return the ToU bands (peak/offpeak/shoulder) that already have stats.

        Used to mark a contract as Time-of-Use across restarts: a band is
        "stored" if its consumption series has any rows within the backfill
        window. One batched recorder call over the three band series.
        """
        from homeassistant.components.recorder.statistics import (
            statistics_during_period,
        )
        from homeassistant.helpers.recorder import get_instance

        id_to_band = {self._tariff_stat_ids(band)[0]: band for band in TOU_BANDS}
        now = datetime.now(UTC)
        look_back = now - timedelta(days=BACKFILL_DAYS + 1)
        result = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            look_back,
            now,
            set(id_to_band),
            "hour",
            None,
            {"sum"},
        )
        return {band for stat_id, band in id_to_band.items() if result.get(stat_id)}

    async def _fetch_range(
        self,
        start: date,
        end: date,
        bill_start: date | None,
        initial_cons_sum: float,
        initial_cost_sum: float,
        tariff_initial_sums: dict[str, tuple[float, float]] | None = None,
        known_bands: frozenset[str] = frozenset(),
    ) -> None:
        """Fetch [start..end] with smart endpoint selection, then import.

        Sleeps between per-day requests so a chunk-of-7 first-install backfill
        doesn't hammer AGL's BFF in under a second. On rate-limit the loop
        stops early — the next 24h poll cycle will resume from the last
        successfully imported date.
        """
        all_intervals: list[IntervalReading] = []
        current = start
        first = True
        while current <= end:
            if not first:
                await asyncio.sleep(BACKFILL_INTER_REQUEST_DELAY)
            first = False
            try:
                if bill_start is not None and current < bill_start:
                    readings = await self.client.async_get_usage_hourly_previous(
                        self.contract_number, current
                    )
                else:
                    readings = await self.client.async_get_usage_hourly(
                        self.contract_number, current
                    )
                all_intervals.extend(readings)
            except AGLRateLimitError as err:
                _LOGGER.warning(
                    "AGL rate-limited at %s; halting backfill chunk: %s", current, err
                )
                break
            except AGLError as err:
                _LOGGER.debug("Fetch skip %s: %s", current, err)
            current += timedelta(days=1)

        if all_intervals:
            await self._import_intervals(
                all_intervals,
                initial_cons_sum,
                initial_cost_sum,
                tariff_initial_sums=tariff_initial_sums,
                known_bands=known_bands,
            )
        # If no intervals were fetched (e.g. AGL had no data for the whole
        # range), leave _latest_cumulative_kwh as the caller seeded it — the
        # most recent stored cumulative remains the sensor value.

    async def _import_intervals(
        self,
        intervals: list[IntervalReading],
        initial_cons_sum: float,
        initial_cost_sum: float,
        *,
        tariff_initial_sums: dict[str, tuple[float, float]] | None = None,
        known_bands: frozenset[str] = frozenset(),
    ) -> None:
        """Aggregate 30-min intervals to hourly and push to recorder statistics.

        Always writes the aggregate consumption + cost series (consumption
        first, cost second — callers/tests depend on that order). On a ToU
        contract — when any peak/offpeak/shoulder interval has ever been seen
        (`known_bands`) or appears in this batch — it ALSO writes a per-tariff
        series for every tariff type present, so the per-tariff series sum back
        to the aggregate with no lost kWh. Flat-rate contracts (only `normal`)
        get the aggregate series alone, exactly as before.
        """
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

        tariff_initial_sums = tariff_initial_sums or {}

        # Aggregate hourly buckets (all intervals) + per-tariff hourly buckets.
        hour_cons: dict[datetime, float] = {}
        hour_cost: dict[datetime, float] = {}
        band_cons: dict[str, dict[datetime, float]] = {}
        band_cost: dict[str, dict[datetime, float]] = {}
        bands_this_batch: set[str] = set()
        for r in intervals:
            h = r.dt.replace(minute=0, second=0, microsecond=0)
            hour_cons[h] = hour_cons.get(h, 0.0) + r.kwh
            hour_cost[h] = hour_cost.get(h, 0.0) + r.cost_aud
            if r.rate_type in TOU_SERIES_TARIFFS:
                band_cons.setdefault(r.rate_type, {})
                band_cost.setdefault(r.rate_type, {})
                bc = band_cons[r.rate_type]
                bk = band_cost[r.rate_type]
                bc[h] = bc.get(h, 0.0) + r.kwh
                bk[h] = bk.get(h, 0.0) + r.cost_aud
                bands_this_batch.add(r.rate_type)

        def _build(
            hourly: dict[datetime, float], initial_sum: float
        ) -> tuple[list[StatisticData], float]:
            running = initial_sum
            stats: list[StatisticData] = []
            for h in sorted(hourly):
                running += hourly[h]
                stats.append(StatisticData(start=h, state=hourly[h], sum=running))
            return stats, running

        def _emit_consumption(
            stat_id: str, name: str, stats: list[StatisticData]
        ) -> None:
            async_add_external_statistics(
                self.hass,
                StatisticMetaData(
                    mean_type=StatisticMeanType.NONE,
                    unit_class="energy",
                    has_sum=True,
                    name=name,
                    source=DOMAIN,
                    statistic_id=stat_id,
                    unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
                ),
                stats,
            )

        def _emit_cost(stat_id: str, name: str, stats: list[StatisticData]) -> None:
            async_add_external_statistics(
                self.hass,
                StatisticMetaData(
                    mean_type=StatisticMeanType.NONE,
                    unit_class=None,
                    has_sum=True,
                    name=name,
                    source=DOMAIN,
                    statistic_id=stat_id,
                    unit_of_measurement="AUD",
                ),
                stats,
            )

        # --- Aggregate series (always; consumption first, cost second). ---
        cons_stats, cons_sum = _build(hour_cons, initial_cons_sum)
        cost_stats, _ = _build(hour_cost, initial_cost_sum)
        _emit_consumption(
            f"{DOMAIN}:{STAT_CONSUMPTION}_{self.contract_number}",
            f"AGL Electricity Consumption ({self.contract_number})",
            cons_stats,
        )
        _emit_cost(
            f"{DOMAIN}:{STAT_COST}_{self.contract_number}",
            f"AGL Electricity Cost ({self.contract_number})",
            cost_stats,
        )

        # --- Per-tariff series (ToU contracts only). ---
        tou_seen = (set(known_bands) | bands_this_batch) & set(TOU_BANDS)
        if tou_seen:
            self._active_tou_bands |= tou_seen
            for tariff in TOU_SERIES_TARIFFS:
                if tariff not in band_cons:
                    continue
                cons_id, cost_id = self._tariff_stat_ids(tariff)
                base_cons, base_cost = tariff_initial_sums.get(tariff, (0.0, 0.0))
                t_cons_stats, _ = _build(band_cons[tariff], base_cons)
                t_cost_stats, _ = _build(band_cost[tariff], base_cost)
                label = TARIFF_LABELS.get(tariff, tariff.title())
                _emit_consumption(
                    cons_id,
                    f"AGL Electricity Consumption {label} ({self.contract_number})",
                    t_cons_stats,
                )
                _emit_cost(
                    cost_id,
                    f"AGL Electricity Cost {label} ({self.contract_number})",
                    t_cost_stats,
                )

        # Update the in-memory cumulative for the TOTAL_INCREASING sensor.
        if cons_stats:
            self._latest_cumulative_kwh = cons_sum
