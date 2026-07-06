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
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util import dt as dt_util

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
    STAT_GENERATION,
    STAT_GENERATION_CREDIT,
    TARIFF_LABELS,
    TARIFF_OFFPEAK,
    TARIFF_PEAK,
    TARIFF_SHOULDER,
    TOU_BANDS,
    TOU_SERIES_TARIFFS,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .agl.client import AglClient
    from .agl.models import IntervalReading

_LOGGER = logging.getLogger(__name__)

# Lower bound for the reach-back baseline lookup (#114). Earlier than any
# possible recorder row, so a series whose last stored hour predates the normal
# look-back window is still found. Bounded ABOVE at the fetch cutoff by the
# caller, so it never reads a sum from inside the rewindow being rewritten.
_EARLIEST_HISTORY = datetime(1970, 1, 1, tzinfo=UTC)


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
    # ToU extras, defaulted to keep the dataclass backward-compatible.
    # active_tariffs gates conditional ToU rate-sensor registration; empty on a
    # flat-rate contract so those sensors never appear.
    active_tariffs: frozenset[str] = frozenset()
    unit_rate_peak_aud_per_kwh: float | None = None
    unit_rate_offpeak_aud_per_kwh: float | None = None
    unit_rate_shoulder_aud_per_kwh: float | None = None
    # Solar extras — has_solar gates conditional generation-sensor
    # registration; False on non-solar contracts so those sensors never appear.
    has_solar: bool = False
    latest_generation_kwh: float = 0.0
    latest_generation_credit_aud: float = 0.0
    # Bill-period solar totals (match the AGL app's "Sold To Grid" tile).
    # None until the generation series has backfilled to the current rewindow
    # — publishing a mid-backfill partial would show numbers that can't match
    # the app (the exact confusion reported on #128 for beta.1).
    generation_period_kwh: float | None = None
    generation_period_credit_aud: float | None = None
    # Solar feed-in tariff (AUD/kWh) from the plan's gstExclusiveRates.
    feed_in_rate_aud_per_kwh: float | None = None


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
        # Solar feed-in state. has_solar comes from /v3/overview each cycle;
        # _prev_has_solar drives the reload-when-solar-appears path.
        self._has_solar: bool = False
        self._prev_has_solar: bool = False
        self._latest_generation_kwh: float = 0.0
        self._latest_generation_credit: float = 0.0

    def _tariff_stat_ids(self, tariff: str) -> tuple[str, str]:
        """Return (consumption_id, cost_id) for a per-tariff series."""
        return (
            f"{DOMAIN}:{STAT_CONSUMPTION}_{tariff}_{self.contract_number}",
            f"{DOMAIN}:{STAT_COST}_{tariff}_{self.contract_number}",
        )

    def _generation_stat_ids(self) -> tuple[str, str]:
        """Return (generation_id, generation_credit_id) for the solar series."""
        return (
            f"{DOMAIN}:{STAT_GENERATION}_{self.contract_number}",
            f"{DOMAIN}:{STAT_GENERATION_CREDIT}_{self.contract_number}",
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

        # Fetch live sensor data first — summary gives bill_start for endpoint selection.
        summary = await self.client.async_get_usage_summary(self.contract_number)
        plan = await self.client.async_get_plan(self.contract_number)
        await self._refresh_has_solar()

        bill_start: date = summary.start

        # Resume point comes from the consumption stat. The cost stat is written
        # in lockstep, and every cumulative-sum baseline is now looked up inside
        # _import_intervals (against the earliest fetched-interval hour), so the
        # cost stat's last row is no longer needed here.
        last_cons_sum, last_cons_date = await self._get_last_stat(stat_id_cons)

        # AGL `dateTime` slots are UTC; using `date.today()` (OS local time)
        # would skew the fetch range by a day around midnight in non-UTC zones.
        today = datetime.now(UTC).date()
        yesterday = today - timedelta(days=1)

        # Seed the sensor with the most-recent known cumulative; _fetch_range
        # bumps it forward when the import produces new rows.
        self._latest_cumulative_kwh = last_cons_sum or 0.0

        # Each series resolves its own fetch range from its own resume point,
        # chunk-capped independently. A single consumption-derived range would
        # starve a generation series added by upgrade: consumption is caught up,
        # so the range never reaches further back than the trailing rewindow and
        # the older BACKFILL_DAYS of solar history would never arrive.
        cons_range = self._chunked_range(
            self._resolve_fetch_start(today, last_cons_date), yesterday
        )

        # Pre-fetch generation resume state. last_gen_date also gates the
        # bill-period totals below: reading it BEFORE the fetch guarantees the
        # rows behind the bill_start baseline were committed in an earlier
        # cycle, not queued in the recorder by this one.
        last_gen_date: date | None = None
        solar_range: tuple[date, date] | None = None
        if self._has_solar:
            stat_id_gen, stat_id_credit = self._generation_stat_ids()
            last_gen_sum, last_gen_date = await self._get_last_stat(stat_id_gen)
            last_credit_sum, _ = await self._get_last_stat(stat_id_credit)
            self._latest_generation_kwh = last_gen_sum or 0.0
            self._latest_generation_credit = last_credit_sum or 0.0
            solar_range = self._chunked_range(
                self._resolve_fetch_start(today, last_gen_date), yesterday
            )

        # Mark which ToU bands already have stored statistics so the per-tariff
        # series are emitted only for a contract that has been seen using ToU
        # (flat-rate contracts stay on the aggregate series alone). The
        # per-tariff baseline sums themselves are resolved in _import_intervals.
        self._active_tou_bands |= await self._get_stored_tou_bands()

        if cons_range or solar_range:
            await self._fetch_range(
                cons_range,
                solar_range,
                bill_start,
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

        feed_in_cents = plan.feed_in_rate_cents_per_kwh
        feed_in_rate_aud = feed_in_cents / 100.0 if feed_in_cents is not None else None

        # Bill-period solar totals — after the fetch so this cycle's rows are
        # included in the in-memory latest sums.
        generation_period_kwh: float | None = None
        generation_period_credit: float | None = None
        if self._has_solar:
            (
                generation_period_kwh,
                generation_period_credit,
            ) = await self._get_generation_period_totals(
                bill_start, last_gen_date, today
            )

        # A new ToU band (or solar newly detected) appearing after first
        # refresh means sensors need to be added; schedule a loop-safe reload.
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
            has_solar=self._has_solar,
            latest_generation_kwh=self._latest_generation_kwh,
            latest_generation_credit_aud=self._latest_generation_credit,
            generation_period_kwh=generation_period_kwh,
            generation_period_credit_aud=generation_period_credit,
            feed_in_rate_aud_per_kwh=feed_in_rate_aud,
        )

    async def _refresh_has_solar(self) -> None:
        """Update the solar flag from /v3/overview.

        Sticky-on-failure: an overview error keeps the previous flag rather
        than flapping the generation series off for one cycle. hasSolar never
        goes back to False once seen True in-process — retiring a solar system
        mid-contract is rare enough that a reload/restart picking it up is fine.
        """
        try:
            contracts = await self.client.async_get_overview()
        except AGLError as err:
            _LOGGER.debug(
                "Overview fetch failed; keeping has_solar=%s: %s", self._has_solar, err
            )
            return
        for contract in contracts:
            if contract.contract_number == self.contract_number:
                self._has_solar = self._has_solar or contract.has_solar
                return

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
        solar_appeared = self._has_solar and not self._prev_has_solar
        self._prev_active_tou_bands = set(self._active_tou_bands)
        self._prev_has_solar = self._has_solar
        if self.data is not None and (new_bands or solar_appeared):
            _LOGGER.info(
                "New ToU tariff band(s) %s / solar_appeared=%s detected; "
                "scheduling reload to add sensors",
                sorted(new_bands),
                solar_appeared,
            )
            self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)

    def _resolve_fetch_start(
        self,
        today: date,
        last_stat_date: date | None,
    ) -> date:
        """Choose the first day to fetch per the resume-strategy decision tree.

        - First install: backfill from BACKFILL_DAYS ago.
        - Big gap (> REWINDOW_DAYS behind): resume incrementally from
          last_stat_date + 1.
        - Normal operation: re-fetch the trailing REWINDOW_DAYS so AGL's
          day-late AEMO backfills self-heal.

        The cumulative-sum baseline is NOT chosen here. _import_intervals looks
        it up from the recorder using the actual earliest fetched-interval hour
        as the cutoff. Deriving it from fetch_start UTC midnight was wrong:
        AGL's period= query is interpreted in the contract's local timezone, so
        the first new interval lands at (fetch_start - 1)T14:00Z for an AEST
        account; a cutoff at fetch_start T00:00Z folded ~10 h of
        about-to-be-overwritten old sums into the baseline, producing a phantom
        kWh jump in the cumulative sum every local midnight.
        """
        backfill_floor = today - timedelta(days=BACKFILL_DAYS)
        if last_stat_date is None:
            return backfill_floor
        if last_stat_date < today - timedelta(days=REWINDOW_DAYS):
            return last_stat_date + timedelta(days=1)
        return max(today - timedelta(days=REWINDOW_DAYS), backfill_floor)

    @staticmethod
    def _chunked_range(start: date, yesterday: date) -> tuple[date, date] | None:
        """Cap a series' fetch start to one BACKFILL_CHUNK_DAYS chunk.

        Returns None when the series has nothing to fetch (start past
        yesterday — AGL never has data for today).
        """
        if start > yesterday:
            return None
        return start, min(yesterday, start + timedelta(days=BACKFILL_CHUNK_DAYS - 1))

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
        the correct baseline. Looks back 2 days to tolerate sparse data, with a
        reach-back fallback for a series whose last row predates that window.
        Returns (0.0, 0.0) if the series has no stored rows at all.
        """
        sums = await self._baseline_sums_before(
            {stat_id_cons, stat_id_cost}, before_dt, look_back_days=2
        )
        return sums[stat_id_cons], sums[stat_id_cost]

    async def _get_tariff_baseline_sums(
        self, stat_ids: set[str], before_dt: datetime
    ) -> dict[str, float]:
        """Return {stat_id: cumulative sum at the last hour strictly before before_dt}.

        Batched (one recorder call for all per-tariff series) so adding ToU
        doesn't multiply executor round-trips. Looks back BACKFILL_DAYS — wide
        enough that a sparse band (e.g. shoulder only on weekdays) still finds
        its true last sum rather than resetting to 0.0.
        """
        return await self._baseline_sums_before(
            set(stat_ids), before_dt, look_back_days=BACKFILL_DAYS
        )

    async def _baseline_sums_before(
        self, stat_ids: set[str], before_dt: datetime, *, look_back_days: int
    ) -> dict[str, float]:
        """Return {stat_id: cumulative sum at the last hour strictly before before_dt}.

        Resolution is two-stage. First a cheap bounded window of `look_back_days`
        ending at before_dt — this covers the normal case and every
        sparse-but-recent band in a single batched recorder call. Any series
        with NO rows in that window is then resolved with a second lookup that
        reaches back to the start of recorded history (still bounded above at
        before_dt).

        The reach-back stage exists for #114: a per-tariff band can be absent
        for longer than the window (e.g. a shoulder band the plan stops using
        for a month) and then reappear inside the trailing rewindow. Without it
        the baseline falls to 0.0 and _emit_series restarts that series'
        cumulative sum from zero — a downward step that breaks its
        TOTAL_INCREASING monotonicity. The lookup stays strictly *before*
        before_dt (never get_last_statistics) precisely so it cannot read a sum
        from inside the rewindow rows about to be rewritten. A series with no
        stored rows at all resolves to 0.0.
        """
        if not stat_ids:
            return {}
        from homeassistant.components.recorder.statistics import (
            statistics_during_period,
        )
        from homeassistant.helpers.recorder import get_instance

        instance = get_instance(self.hass)

        async def _last_sums(start_dt: datetime, ids: set[str]) -> dict[str, float]:
            result = await instance.async_add_executor_job(
                statistics_during_period,
                self.hass,
                start_dt,
                before_dt,
                set(ids),
                "hour",
                None,
                {"sum"},
            )
            sums: dict[str, float] = {}
            for stat_id in ids:
                rows = result.get(stat_id) or []
                last = rows[-1].get("sum") if rows else None
                if last is not None:
                    sums[stat_id] = float(last)
            return sums

        out = await _last_sums(before_dt - timedelta(days=look_back_days), stat_ids)
        missing = {stat_id for stat_id in stat_ids if stat_id not in out}
        if missing:
            out.update(await _last_sums(_EARLIEST_HISTORY, missing))
        for stat_id in stat_ids:
            out.setdefault(stat_id, 0.0)
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

    async def _get_generation_period_totals(
        self,
        bill_start: date,
        last_gen_date: date | None,
        today: date,
    ) -> tuple[float | None, float | None]:
        """Return (kWh, AUD) exported since bill_start, or (None, None).

        Matches the AGL app's billing-period "Sold To Grid" tile: latest
        cumulative sum minus the stored sum at local midnight of bill_start.

        Gated on the PRE-FETCH generation resume point: values publish only
        once the series was already caught up to the trailing rewindow at the
        start of this cycle. Mid-backfill partials are suppressed (None →
        sensor `unknown`) because they cannot match the app — the exact
        confusion behind the beta.1 report on #128. Using the pre-fetch date
        also sidesteps the recorder's async write queue: every row behind the
        bill_start baseline was committed by an earlier cycle, never queued by
        this one. The latest sums are the in-memory values returned
        synchronously by _emit_series, so they are exact either way.

        Known limitation (not gated): a billing period longer than
        BACKFILL_DAYS — quarterly bills — starts before the backfill floor,
        so the baseline resolves to 0.0 and the totals cover only the stored
        history. Gating on it would leave quarterly-billed users permanently
        unavailable.

        Half-hour timezones (e.g. ACST, local midnight = 14:30Z): the hourly
        row strictly before the cutoff contains the day's first 30-min slot,
        so at most one midnight slot folds into the baseline — solar export at
        local midnight is zero, so no correction is needed.
        """
        if last_gen_date is None or last_gen_date < today - timedelta(
            days=REWINDOW_DAYS
        ):
            return None, None
        stat_id_gen, stat_id_credit = self._generation_stat_ids()
        cutoff = dt_util.start_of_local_day(bill_start)
        base_gen, base_credit = await self._get_baseline_sums(
            stat_id_gen, stat_id_credit, cutoff
        )
        kwh = max(0.0, self._latest_generation_kwh - base_gen)
        credit = max(0.0, self._latest_generation_credit - base_credit)
        return kwh, credit

    async def _fetch_range(
        self,
        cons_range: tuple[date, date] | None,
        solar_range: tuple[date, date] | None,
        bill_start: date | None,
        *,
        known_bands: frozenset[str] = frozenset(),
    ) -> None:
        """Fetch per-series day ranges with smart endpoint selection, then import.

        Each series carries its own (start, end) so a generation series that is
        behind (e.g. solar support added by upgrade while consumption is caught
        up) backfills from its own resume point without re-fetching consumption
        days — and vice versa. A day outside both ranges is skipped without a
        request. Worst case (fully disjoint chunks) is 7 + 7 requests, the same
        peak load as a steady-state solar cycle (7 days x 2 requests).

        Sleeps between requests so a chunk-of-7 first-install backfill doesn't
        hammer AGL's BFF in under a second. AGL rate limits are account-wide,
        so an AGLRateLimitError from either endpoint halts the whole chunk —
        each series resumes from its own last imported date next cycle.

        The consumption series keeps reading the proven Electricity endpoint —
        the solar endpoint's own consumption block is ignored until it has been
        reconciled against a real bill.
        """
        ranges = [r for r in (cons_range, solar_range) if r is not None]
        if not ranges:
            return
        all_intervals: list[IntervalReading] = []
        solar_intervals: list[IntervalReading] = []
        fetched_solar_days: list[date] = []
        current = min(r[0] for r in ranges)
        loop_end = max(r[1] for r in ranges)
        first = True
        rate_limited = False
        while current <= loop_end and not rate_limited:
            fetch_cons = cons_range is not None and (
                cons_range[0] <= current <= cons_range[1]
            )
            fetch_solar = solar_range is not None and (
                solar_range[0] <= current <= solar_range[1]
            )
            previous = bill_start is not None and current < bill_start
            if fetch_cons:
                if not first:
                    await asyncio.sleep(BACKFILL_INTER_REQUEST_DELAY)
                first = False
                readings = await self._fetch_day_consumption(current, previous)
                if readings is None:  # rate-limited
                    break
                all_intervals.extend(readings)
            if fetch_solar:
                if not first:
                    await asyncio.sleep(BACKFILL_INTER_REQUEST_DELAY)
                first = False
                try:
                    solar_readings = await self._fetch_day_solar(current, previous)
                except AGLRateLimitError as err:
                    _LOGGER.warning(
                        "AGL rate-limited at %s (solar); halting backfill chunk: %s",
                        current,
                        err,
                    )
                    rate_limited = True
                else:
                    if solar_readings is not None:
                        solar_intervals.extend(solar_readings)
                        # Only a *successful* fetch marks the day as covered.
                        # An errored day stays unmarked; it is retried until a
                        # LATER day in the chunk writes rows (same
                        # skip-and-continue semantics as consumption — see
                        # _fetch_day_solar docstring for the tradeoff).
                        fetched_solar_days.append(current)
            current += timedelta(days=1)

        if all_intervals:
            await self._import_intervals(all_intervals, known_bands=known_bands)
        # Partial solar batches import too — idempotent, and the trailing
        # rewindow re-fetches the last REWINDOW_DAYS so short gaps self-heal.
        # fetched_solar_days matters even with zero intervals: an all-zero
        # export day must still advance the generation resume point.
        if solar_intervals or fetched_solar_days:
            await self._import_generation(
                solar_intervals, fetched_days=fetched_solar_days
            )
        # If no intervals were fetched (e.g. AGL had no data for the whole
        # range), leave the cumulative seeds as the caller set them — the
        # most recent stored cumulative remains the sensor value.

    async def _fetch_day_consumption(
        self, day: date, previous: bool
    ) -> list[IntervalReading] | None:
        """Fetch one day of consumption intervals.

        Returns [] on a skippable AGLError (logged, the loop continues) and
        None on AGLRateLimitError so the caller halts the chunk.
        """
        try:
            if previous:
                return await self.client.async_get_usage_hourly_previous(
                    self.contract_number, day
                )
            return await self.client.async_get_usage_hourly(self.contract_number, day)
        except AGLRateLimitError as err:
            _LOGGER.warning(
                "AGL rate-limited at %s; halting backfill chunk: %s", day, err
            )
            return None
        except AGLError as err:
            _LOGGER.debug("Fetch skip %s: %s", day, err)
            return []

    async def _fetch_day_solar(
        self, day: date, previous: bool
    ) -> list[IntervalReading] | None:
        """Fetch one day of solar feed-in intervals (same contract as above).

        Returns None on a skippable AGLError so the caller does NOT count the
        day as fetched (no zero-marker row). The day is retried while the
        resume point still trails it; once a LATER day in the chunk imports
        rows, resume moves past the gap and — outside the trailing rewindow —
        it is not revisited. Deliberate tradeoff, same skip-and-continue
        semantics as the consumption backfill (#34): halting at the first
        errored day would let a permanently-500ing old day (AGL does this on
        very old dates) stall the backfill and the period sensors forever,
        which is worse than a rare one-day historical hole.
        AGLRateLimitError propagates so the caller halts the whole chunk.
        """
        try:
            return await self.client.async_get_solar_hourly(
                self.contract_number, day, previous=previous
            )
        except AGLRateLimitError:
            raise
        except AGLError as err:
            _LOGGER.debug("Solar fetch skip %s: %s", day, err)
            return None

    @staticmethod
    def _bucket_hourly(
        intervals: list[IntervalReading],
    ) -> tuple[
        dict[datetime, float],
        dict[datetime, float],
        dict[str, dict[datetime, float]],
        dict[str, dict[datetime, float]],
        set[str],
    ]:
        """Bucket 30-min intervals into hourly aggregate + per-tariff sums.

        Returns (hour_cons, hour_cost, band_cons, band_cost, bands_this_batch).
        Per-tariff buckets are only populated for TOU_SERIES_TARIFFS rate types.
        """
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
                bc = band_cons.setdefault(r.rate_type, {})
                bk = band_cost.setdefault(r.rate_type, {})
                bc[h] = bc.get(h, 0.0) + r.kwh
                bk[h] = bk.get(h, 0.0) + r.cost_aud
                bands_this_batch.add(r.rate_type)
        return hour_cons, hour_cost, band_cons, band_cost, bands_this_batch

    async def _import_intervals(
        self,
        intervals: list[IntervalReading],
        *,
        known_bands: frozenset[str] = frozenset(),
    ) -> None:
        """Aggregate 30-min intervals to hourly and push to recorder statistics.

        Always writes the aggregate consumption + cost series. On a ToU
        contract — when any peak/offpeak/shoulder interval has ever been seen
        (`known_bands`) or appears in this batch — it ALSO writes a per-tariff
        series for every tariff type present, so the per-tariff series sum back
        to the aggregate with no lost kWh. Flat-rate contracts (only `normal`)
        get the aggregate series alone, exactly as before.

        Every series' cumulative-sum baseline (aggregate AND per-tariff) is
        looked up against the recorder using the hour right before the EARLIEST
        fetched interval as the cutoff — never a fetch_start-derived UTC
        midnight. AGL's period= query is interpreted in the contract's local
        timezone, so the first interval of a day query lands at local midnight
        in UTC ((fetch_start - 1)T14:00Z for AEST), and a fixed-UTC cutoff would
        fold ~10 h of about-to-be-overwritten old sums into the baseline and
        re-add them — spiking the cumulative sum every local midnight.
        """
        from homeassistant.const import UnitOfEnergy

        # Aggregate hourly buckets (all intervals) + per-tariff hourly buckets.
        hour_cons, hour_cost, band_cons, band_cost, bands_this_batch = (
            self._bucket_hourly(intervals)
        )

        # Nothing fetched → nothing to import, and no baseline lookup needed.
        if not hour_cons:
            return

        stat_id_cons = f"{DOMAIN}:{STAT_CONSUMPTION}_{self.contract_number}"
        stat_id_cost = f"{DOMAIN}:{STAT_COST}_{self.contract_number}"

        # Baseline cutoff = the earliest fetched interval hour. _get_baseline_sums
        # returns the cumulative sum at the last hour strictly before it, so the
        # to-be-overwritten rewindow rows are excluded regardless of timezone/DST.
        cutoff = min(hour_cons)

        # Per-tariff series to emit (and therefore to baseline-look-up).
        tou_seen = (set(known_bands) | bands_this_batch) & set(TOU_BANDS)
        band_ids: set[str] = set()
        if tou_seen:
            for tariff in TOU_SERIES_TARIFFS:
                band_ids.update(self._tariff_stat_ids(tariff))

        # Resolve aggregate + per-tariff baselines, overlapped on the executor.
        # _get_tariff_baseline_sums short-circuits to {} for an empty band set
        # (flat-rate contract), so no extra recorder round-trip is incurred.
        (initial_cons_sum, initial_cost_sum), band_sums = await asyncio.gather(
            self._get_baseline_sums(stat_id_cons, stat_id_cost, cutoff),
            self._get_tariff_baseline_sums(band_ids, cutoff),
        )

        _emit_series = self._emit_series

        kwh = UnitOfEnergy.KILO_WATT_HOUR
        contract = self.contract_number

        # Aggregate series (always; consumption first, then cost).
        cons_sum = _emit_series(
            f"{DOMAIN}:{STAT_CONSUMPTION}_{contract}",
            f"AGL Electricity Consumption ({contract})",
            kwh,
            "energy",
            hour_cons,
            initial_cons_sum,
        )
        _emit_series(
            f"{DOMAIN}:{STAT_COST}_{contract}",
            f"AGL Electricity Cost ({contract})",
            "AUD",
            None,
            hour_cost,
            initial_cost_sum,
        )

        # Per-tariff series (ToU contracts only). tou_seen / band_ids were
        # resolved above so the baselines could be looked up in one round-trip.
        if tou_seen:
            self._active_tou_bands |= tou_seen
            for tariff in TOU_SERIES_TARIFFS:
                if tariff not in band_cons:
                    continue
                cons_id, cost_id = self._tariff_stat_ids(tariff)
                base_cons = band_sums.get(cons_id, 0.0)
                base_cost = band_sums.get(cost_id, 0.0)
                label = TARIFF_LABELS.get(tariff, tariff.title())
                _emit_series(
                    cons_id,
                    f"AGL Electricity Consumption {label} ({contract})",
                    kwh,
                    "energy",
                    band_cons[tariff],
                    base_cons,
                )
                _emit_series(
                    cost_id,
                    f"AGL Electricity Cost {label} ({contract})",
                    "AUD",
                    None,
                    band_cost[tariff],
                    base_cost,
                )

        # Update the in-memory cumulative for the TOTAL_INCREASING sensor.
        # hour_cons is non-empty here (we returned early otherwise).
        self._latest_cumulative_kwh = cons_sum

    def _emit_series(
        self,
        stat_id: str,
        name: str,
        unit: str,
        unit_class: str | None,
        hourly: dict[datetime, float],
        initial_sum: float,
    ) -> float:
        """Build the cumulative hourly rows for one series and import them.

        Idempotent on (statistic_id, start) — safe for rewindow overwrites.
        Returns the final cumulative sum.
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

        running = initial_sum
        stats: list[StatisticData] = []
        for h in sorted(hourly):
            running += hourly[h]
            stats.append(StatisticData(start=h, state=hourly[h], sum=running))
        async_add_external_statistics(
            self.hass,
            StatisticMetaData(
                mean_type=StatisticMeanType.NONE,
                unit_class=unit_class,
                has_sum=True,
                name=name,
                source=DOMAIN,
                statistic_id=stat_id,
                unit_of_measurement=unit,
            ),
            stats,
        )
        return running

    async def _import_generation(
        self,
        intervals: list[IntervalReading],
        *,
        fetched_days: Iterable[date] = (),
    ) -> None:
        """Aggregate solar feed-in intervals to hourly and push to statistics.

        Writes haggle:generation_<contract> (exported kWh, unit_class="energy"
        so it appears in the Energy dashboard "Return to grid" picker) and
        haggle:generation_credit_<contract> (AUD feed-in credit). Uses the same
        earliest-fetched-hour baseline cutoff as the consumption import — see
        _import_intervals for why a fetch_start-derived UTC midnight is wrong.

        A successfully fetched day whose intervals all filtered out (zero
        export: cloudy day, or a solar system newer than the backfill floor)
        still gets ONE zero-delta marker row at the hour of local midnight so
        the generation resume point advances. Without it the backfill would
        refetch the same all-zero chunk forever and the bill-period sensors
        would never unlock (Codex review, PR #144). An AEMO-lag placeholder
        day marked this way self-heals: the trailing rewindow re-fetches the
        last REWINDOW_DAYS and the idempotent import overwrites the marker
        (data lag is 24-48 h, well inside the window).
        """
        from homeassistant.const import UnitOfEnergy

        hour_kwh: dict[datetime, float] = {}
        hour_credit: dict[datetime, float] = {}
        for r in intervals:
            h = r.dt.replace(minute=0, second=0, microsecond=0)
            hour_kwh[h] = hour_kwh.get(h, 0.0) + r.kwh
            hour_credit[h] = hour_credit.get(h, 0.0) + r.cost_aud

        for day in fetched_days:
            day_start = dt_util.start_of_local_day(day)
            day_end = day_start + timedelta(days=1)
            if any(day_start <= h < day_end for h in hour_kwh):
                continue
            # Floor to the hour: half-hour zones (ACST) have local midnight at
            # :30 past a UTC hour, and statistics rows start on the hour.
            marker = dt_util.as_utc(day_start).replace(
                minute=0, second=0, microsecond=0
            )
            hour_kwh.setdefault(marker, 0.0)
            hour_credit.setdefault(marker, 0.0)

        if not hour_kwh:
            return

        stat_id_gen, stat_id_credit = self._generation_stat_ids()
        cutoff = min(hour_kwh)
        base_gen, base_credit = await self._get_baseline_sums(
            stat_id_gen, stat_id_credit, cutoff
        )

        contract = self.contract_number
        gen_sum = self._emit_series(
            stat_id_gen,
            f"AGL Solar Generation ({contract})",
            UnitOfEnergy.KILO_WATT_HOUR,
            "energy",
            hour_kwh,
            base_gen,
        )
        credit_sum = self._emit_series(
            stat_id_credit,
            f"AGL Solar Feed-in Credit ({contract})",
            "AUD",
            None,
            hour_credit,
            base_credit,
        )
        self._latest_generation_kwh = gen_sum
        self._latest_generation_credit = credit_sum
