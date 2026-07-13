"""Tests for HaggleCoordinator statistics import logic.

Covers _import_intervals (aggregation, running sum, idempotency, none-filter),
_fetch_range (smart endpoint selection), and _fetch_and_import (chunked resume).

All recorder calls are patched at the boundary — async_add_external_statistics
and get_last_statistics — so no real SQLite DB is needed.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.haggle.agl.models import BillPeriod, IntervalReading, PlanRates
from custom_components.haggle.const import (
    BACKFILL_CHUNK_DAYS,
    BACKFILL_DAYS,
    CONF_ACCOUNT_NUMBER,
    CONF_CONTRACT_NUMBER,
    CONF_REFRESH_TOKEN,
    CONF_SOLAR_HEAL,
    CONF_SOLAR_STALL_SPANS,
    DOMAIN,
    MAX_SOLAR_HEAL_ATTEMPTS,
    MAX_STALL_SPAN_RECORDS,
    REWINDOW_DAYS,
    SOLAR_HEAL_DONE,
    SOLAR_HEAL_PENDING,
    SOLAR_STALL_GIVE_UP_CYCLES,
    STAT_CONSUMPTION,
)
from custom_components.haggle.coordinator import HaggleCoordinator

# Capture the real coroutine function before any autouse fixture can patch the
# class attribute.  Used by TestTouRecorderHelpers to restore the live
# implementation on a per-instance basis while the autouse fixture is active.
_REAL_GET_STORED_TOU_BANDS = HaggleCoordinator._get_stored_tou_bands

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CONTRACT = "9999999999"

_ENTRY_DATA = {
    CONF_REFRESH_TOKEN: "v1.testtoken",
    CONF_CONTRACT_NUMBER: _CONTRACT,
    CONF_ACCOUNT_NUMBER: "1234567890",
}


def _make_interval(dt: datetime, kwh: float, cost: float = 0.05) -> IntervalReading:
    return IntervalReading(dt=dt, kwh=kwh, cost_aud=cost, rate_type="normal")


def _empty_summary() -> BillPeriod:
    """Default BillPeriod that flows through coordinator without contributing data."""
    today = datetime.now(UTC).date()
    return BillPeriod(
        start=today,
        end=today,
        consumption_kwh=0.0,
        cost_label="$0.00",
        projection_label="",
    )


def _empty_plan() -> PlanRates:
    return PlanRates(
        product_name="",
        unit_rates=[],
        supply_charge_cents_per_day=0.0,
    )


def _make_coordinator(
    hass: HomeAssistant,
    client: MagicMock | None = None,
) -> HaggleCoordinator:
    """Create a HaggleCoordinator without running setup."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=_ENTRY_DATA,
        unique_id="1234567890_9999999999",
    )
    entry.add_to_hass(hass)
    if client is None:
        client = AsyncMock()
    return HaggleCoordinator(hass, entry, client, _CONTRACT)


# These functions are imported with `from ... import` inside method bodies, so
# we patch them at the source module (not on the coordinator module itself).
_PATCH_ADD_STATS = (
    "homeassistant.components.recorder.statistics.async_add_external_statistics"
)
_PATCH_GET_LAST = "homeassistant.components.recorder.statistics.get_last_statistics"
_PATCH_GET_INSTANCE = "homeassistant.helpers.recorder.get_instance"


def _mock_get_instance(return_value: dict) -> MagicMock:
    """Return a mock for get_instance whose executor_job returns return_value."""
    mock_instance = MagicMock()
    mock_instance.async_add_executor_job = AsyncMock(return_value=return_value)
    mock_get_inst = MagicMock(return_value=mock_instance)
    return mock_get_inst


# ---------------------------------------------------------------------------
# _import_intervals — aggregation
# ---------------------------------------------------------------------------


class TestImportIntervalsAggregation:
    async def test_two_half_hour_slots_aggregate_to_one_hourly_row(
        self, hass: HomeAssistant
    ) -> None:
        """Two 30-min readings in the same UTC hour → a single hourly StatisticData."""
        coord = _make_coordinator(hass)

        # Both slots fall in the 13:00 UTC hour
        intervals = [
            _make_interval(datetime(2026, 4, 28, 13, 0, tzinfo=UTC), kwh=0.163),
            _make_interval(datetime(2026, 4, 28, 13, 30, tzinfo=UTC), kwh=0.153),
        ]

        with (
            patch(_PATCH_ADD_STATS) as mock_add,
            patch.object(
                coord,
                "_get_baseline_sums",
                new=AsyncMock(return_value=(0.0, 0.0)),
            ),
        ):
            await coord._import_intervals(intervals)

        # Called twice: once for consumption, once for cost
        assert mock_add.call_count == 2
        # First call = consumption; check the StatisticData list
        cons_stats = mock_add.call_args_list[0][0][2]
        assert len(cons_stats) == 1
        assert cons_stats[0]["start"] == datetime(2026, 4, 28, 13, 0, tzinfo=UTC)
        assert cons_stats[0]["state"] == pytest.approx(0.163 + 0.153)

    async def test_two_different_hours_produce_two_rows(
        self, hass: HomeAssistant
    ) -> None:
        """30-min slots in two different UTC hours → two hourly rows."""
        coord = _make_coordinator(hass)
        intervals = [
            _make_interval(datetime(2026, 4, 28, 12, 0, tzinfo=UTC), kwh=0.157),
            _make_interval(datetime(2026, 4, 28, 12, 30, tzinfo=UTC), kwh=0.153),
            _make_interval(datetime(2026, 4, 28, 13, 0, tzinfo=UTC), kwh=0.163),
            _make_interval(datetime(2026, 4, 28, 13, 30, tzinfo=UTC), kwh=0.153),
        ]

        with (
            patch(_PATCH_ADD_STATS) as mock_add,
            patch.object(
                coord,
                "_get_baseline_sums",
                new=AsyncMock(return_value=(0.0, 0.0)),
            ),
        ):
            await coord._import_intervals(intervals)

        cons_stats = mock_add.call_args_list[0][0][2]
        assert len(cons_stats) == 2

    async def test_running_sum_is_cumulative(self, hass: HomeAssistant) -> None:
        """Three consecutive hours → sum is strictly monotonically increasing."""
        coord = _make_coordinator(hass)
        intervals = [
            _make_interval(datetime(2026, 4, 28, h, 0, tzinfo=UTC), kwh=1.0)
            for h in range(3)
        ]

        with (
            patch(_PATCH_ADD_STATS) as mock_add,
            patch.object(
                coord,
                "_get_baseline_sums",
                new=AsyncMock(return_value=(0.0, 0.0)),
            ),
        ):
            await coord._import_intervals(intervals)

        cons_stats = mock_add.call_args_list[0][0][2]
        assert len(cons_stats) == 3
        sums = [s["sum"] for s in cons_stats]
        assert sums == [pytest.approx(1.0), pytest.approx(2.0), pytest.approx(3.0)]

    async def test_baseline_offset_applied_to_running_total(
        self, hass: HomeAssistant
    ) -> None:
        """Baseline from _get_baseline_sums is added as an offset to the running total."""
        coord = _make_coordinator(hass)
        intervals = [
            _make_interval(datetime(2026, 4, 28, 0, 0, tzinfo=UTC), kwh=0.5),
        ]

        with (
            patch(_PATCH_ADD_STATS) as mock_add,
            patch.object(
                coord,
                "_get_baseline_sums",
                new=AsyncMock(return_value=(100.0, 0.0)),
            ),
        ):
            await coord._import_intervals(intervals)

        cons_stats = mock_add.call_args_list[0][0][2]
        assert cons_stats[0]["sum"] == pytest.approx(100.5)

    async def test_latest_cumulative_kwh_updated(self, hass: HomeAssistant) -> None:
        """_latest_cumulative_kwh is updated to the final cumulative sum."""
        coord = _make_coordinator(hass)
        intervals = [
            _make_interval(datetime(2026, 4, 28, h, 0, tzinfo=UTC), kwh=1.0)
            for h in range(3)
        ]

        with (
            patch(_PATCH_ADD_STATS),
            patch.object(
                coord,
                "_get_baseline_sums",
                new=AsyncMock(return_value=(50.0, 0.0)),
            ),
        ):
            await coord._import_intervals(intervals)

        assert coord._latest_cumulative_kwh == pytest.approx(53.0)

    async def test_empty_intervals_skips_import(self, hass: HomeAssistant) -> None:
        """No intervals → async_add_external_statistics is NOT called at all,
        and _get_baseline_sums is also not called (no recorder round-trip)."""
        coord = _make_coordinator(hass)
        mock_baseline = AsyncMock(return_value=(0.0, 0.0))

        with (
            patch(_PATCH_ADD_STATS) as mock_add,
            patch.object(coord, "_get_baseline_sums", new=mock_baseline),
        ):
            await coord._import_intervals([])

        mock_add.assert_not_called()
        mock_baseline.assert_not_called()

    async def test_baseline_looked_up_at_earliest_fetched_hour(
        self, hass: HomeAssistant
    ) -> None:
        """The baseline cutoff must be the EARLIEST interval hour, not fetch_start midnight.

        Regression guard for the phantom kWh spike bug: AGL's period= query is
        interpreted in the contract's local timezone, so the first interval of a
        day query lands at local midnight in UTC (e.g. 2026-04-27T14:00Z for an
        AEST account). If the cutoff were fixed to fetch_start T00:00Z UTC it
        would include ~10 h of about-to-be-overwritten old sums in the baseline
        and then re-add those same hours' deltas — spiking the cumulative sum
        every local-midnight UTC row.

        Pass intervals OUT of chronological order; earliest hour is 14:00Z.
        Assert _get_baseline_sums is called once with that exact cutoff.
        """
        coord = _make_coordinator(hass)
        earliest_hour = datetime(2026, 4, 28, 14, 0, tzinfo=UTC)
        intervals = [
            # Later hour first (out of order) to prove min() is used, not first-seen.
            _make_interval(datetime(2026, 4, 28, 15, 0, tzinfo=UTC), kwh=0.3),
            _make_interval(datetime(2026, 4, 28, 15, 30, tzinfo=UTC), kwh=0.2),
            _make_interval(earliest_hour, kwh=0.5),
            _make_interval(datetime(2026, 4, 28, 14, 30, tzinfo=UTC), kwh=0.4),
        ]
        mock_baseline = AsyncMock(return_value=(0.0, 0.0))

        with (
            patch(_PATCH_ADD_STATS),
            patch.object(coord, "_get_baseline_sums", new=mock_baseline),
        ):
            await coord._import_intervals(intervals)

        mock_baseline.assert_awaited_once()
        # Third positional arg is before_dt / cutoff.
        _, _, cutoff_arg = mock_baseline.call_args[0]
        assert cutoff_arg == earliest_hour


# ---------------------------------------------------------------------------
# _import_intervals — stat_id and metadata
# ---------------------------------------------------------------------------


class TestImportIntervalsStatId:
    async def test_statistic_id_contains_domain_and_contract(
        self, hass: HomeAssistant
    ) -> None:
        coord = _make_coordinator(hass)
        intervals = [
            _make_interval(datetime(2026, 4, 28, 0, 0, tzinfo=UTC), kwh=0.2),
        ]

        with (
            patch(_PATCH_ADD_STATS) as mock_add,
            patch.object(
                coord,
                "_get_baseline_sums",
                new=AsyncMock(return_value=(0.0, 0.0)),
            ),
        ):
            await coord._import_intervals(intervals)

        cons_meta = mock_add.call_args_list[0][0][1]
        expected_id = f"{DOMAIN}:{STAT_CONSUMPTION}_{_CONTRACT}"
        assert cons_meta["statistic_id"] == expected_id
        assert cons_meta["source"] == DOMAIN

    async def test_metadata_has_correct_unit_and_has_sum(
        self, hass: HomeAssistant
    ) -> None:
        from homeassistant.const import UnitOfEnergy

        coord = _make_coordinator(hass)
        intervals = [
            _make_interval(datetime(2026, 4, 28, 0, 0, tzinfo=UTC), kwh=0.2),
        ]

        with (
            patch(_PATCH_ADD_STATS) as mock_add,
            patch.object(
                coord,
                "_get_baseline_sums",
                new=AsyncMock(return_value=(0.0, 0.0)),
            ),
        ):
            await coord._import_intervals(intervals)

        cons_meta = mock_add.call_args_list[0][0][1]
        assert cons_meta["unit_of_measurement"] == UnitOfEnergy.KILO_WATT_HOUR
        assert cons_meta["has_sum"] is True


# ---------------------------------------------------------------------------
# _import_intervals — aggregation of already-filtered intervals
# ---------------------------------------------------------------------------


class TestImportIntervalsAggregationFiltered:
    async def test_two_slots_same_hour_single_row(self, hass: HomeAssistant) -> None:
        """Two 30-min slots already filtered (no none-types) in same hour → 1 row."""
        coord = _make_coordinator(hass)
        intervals = [
            _make_interval(datetime(2026, 4, 28, 0, 0, tzinfo=UTC), kwh=0.5),
            _make_interval(datetime(2026, 4, 28, 0, 30, tzinfo=UTC), kwh=0.3),
        ]

        with (
            patch(_PATCH_ADD_STATS) as mock_add,
            patch.object(
                coord,
                "_get_baseline_sums",
                new=AsyncMock(return_value=(0.0, 0.0)),
            ),
        ):
            await coord._import_intervals(intervals)

        cons_stats = mock_add.call_args_list[0][0][2]
        assert len(cons_stats) == 1
        assert cons_stats[0]["state"] == pytest.approx(0.8)


# ---------------------------------------------------------------------------
# _async_setup — now a no-op
# ---------------------------------------------------------------------------


class TestAsyncSetupIsNoop:
    async def test_setup_does_not_call_client(self, hass: HomeAssistant) -> None:
        """_async_setup is a no-op; first-install backfill runs via _fetch_and_import."""
        mock_client = AsyncMock()
        coord = _make_coordinator(hass, client=mock_client)
        await coord._async_setup()
        mock_client.async_get_usage_hourly.assert_not_called()
        mock_client.async_get_usage_hourly_previous.assert_not_called()


# ---------------------------------------------------------------------------
# _async_update_data — auth error bubbling
# ---------------------------------------------------------------------------


class TestUpdateDataAuthError:
    async def test_auth_error_raises_config_entry_auth_failed(
        self, hass: HomeAssistant
    ) -> None:
        from homeassistant.exceptions import ConfigEntryAuthFailed

        from custom_components.haggle.agl.client import AGLAuthError

        coord = _make_coordinator(hass)

        with (
            patch.object(
                coord,
                "_fetch_and_import",
                new_callable=AsyncMock,
                side_effect=AGLAuthError("token revoked"),
            ),
            pytest.raises(ConfigEntryAuthFailed),
        ):
            await coord._async_update_data()

    async def test_agl_error_raises_update_failed(self, hass: HomeAssistant) -> None:
        from homeassistant.helpers.update_coordinator import UpdateFailed

        from custom_components.haggle.agl.client import AGLError

        coord = _make_coordinator(hass)

        with (
            patch.object(
                coord,
                "_fetch_and_import",
                new_callable=AsyncMock,
                side_effect=AGLError("network error"),
            ),
            pytest.raises(UpdateFailed),
        ):
            await coord._async_update_data()

    async def test_failed_poll_shortens_retry_interval(
        self, hass: HomeAssistant
    ) -> None:
        """#155: a transient AGL failure retries after RETRY_INTERVAL_ON_ERROR
        instead of silently waiting a full 24 h (#126 'poll never ran')."""
        from homeassistant.helpers.update_coordinator import UpdateFailed

        from custom_components.haggle.agl.client import AGLError
        from custom_components.haggle.const import (
            RETRY_INTERVAL_ON_ERROR,
            SCAN_INTERVAL_HOURLY,
        )

        coord = _make_coordinator(hass)
        assert coord.update_interval == SCAN_INTERVAL_HOURLY
        with (
            patch.object(
                coord,
                "_fetch_and_import",
                new_callable=AsyncMock,
                side_effect=AGLError("HTTP 500 fetching AGL data"),
            ),
            pytest.raises(UpdateFailed),
        ):
            await coord._async_update_data()
        assert coord.update_interval == RETRY_INTERVAL_ON_ERROR

    async def test_successful_poll_restores_cadence(self, hass: HomeAssistant) -> None:
        from custom_components.haggle.const import (
            RETRY_INTERVAL_ON_ERROR,
            SCAN_INTERVAL_HOURLY,
        )

        coord = _make_coordinator(hass)
        coord.update_interval = RETRY_INTERVAL_ON_ERROR  # as after a failure
        with patch.object(
            coord,
            "_fetch_and_import",
            new_callable=AsyncMock,
            return_value=MagicMock(),
        ):
            await coord._async_update_data()
        assert coord.update_interval == SCAN_INTERVAL_HOURLY

    async def test_halted_sweep_schedules_fast_retry(self, hass: HomeAssistant) -> None:
        """Codex on #157: a chunk halted by 429/transport returns data (cycle
        succeeds, sensors keep values) but must still schedule the 30-min
        retry — otherwise the halted chunk waits a full day."""
        from custom_components.haggle.const import (
            RETRY_INTERVAL_ON_ERROR,
            SCAN_INTERVAL_HOURLY,
        )

        coord = _make_coordinator(hass)
        assert coord.update_interval == SCAN_INTERVAL_HOURLY

        async def _fetch_and_mark_halt() -> MagicMock:
            coord._sweep_halted = True  # as _fetch_range sets on a halt
            return MagicMock()

        with patch.object(coord, "_fetch_and_import", side_effect=_fetch_and_mark_halt):
            await coord._async_update_data()
        assert coord.update_interval == RETRY_INTERVAL_ON_ERROR

        # Next clean cycle restores the daily cadence.
        async def _fetch_clean() -> MagicMock:
            coord._sweep_halted = False
            return MagicMock()

        with patch.object(coord, "_fetch_and_import", side_effect=_fetch_clean):
            await coord._async_update_data()
        assert coord.update_interval == SCAN_INTERVAL_HOURLY

    async def test_auth_failure_leaves_interval_untouched(
        self, hass: HomeAssistant
    ) -> None:
        """Auth failures hand over to the reauth flow — no fast retry that
        would hammer a rejected token."""
        from homeassistant.exceptions import ConfigEntryAuthFailed

        from custom_components.haggle.agl.client import AGLAuthError
        from custom_components.haggle.const import SCAN_INTERVAL_HOURLY

        coord = _make_coordinator(hass)
        with (
            patch.object(
                coord,
                "_fetch_and_import",
                new_callable=AsyncMock,
                side_effect=AGLAuthError("token revoked"),
            ),
            pytest.raises(ConfigEntryAuthFailed),
        ):
            await coord._async_update_data()
        assert coord.update_interval == SCAN_INTERVAL_HOURLY


# ---------------------------------------------------------------------------
# _fetch_range — smart endpoint selection
# ---------------------------------------------------------------------------


class TestFetchRange:
    async def test_uses_previous_hourly_for_days_before_bill_start(
        self, hass: HomeAssistant
    ) -> None:
        """Days strictly before bill_start must use Previous/Hourly."""
        mock_client = AsyncMock()
        mock_client.async_get_usage_hourly_previous.return_value = []
        coord = _make_coordinator(hass, client=mock_client)

        today = datetime.now(UTC).date()
        bill_start = today - timedelta(days=2)
        start = today - timedelta(days=5)
        end = today - timedelta(days=3)  # all days are before bill_start

        with patch.object(coord, "_import_intervals", new_callable=AsyncMock):
            await coord._fetch_range((start, end), None, bill_start)

        assert mock_client.async_get_usage_hourly_previous.call_count == 3
        mock_client.async_get_usage_hourly.assert_not_called()

    async def test_uses_current_hourly_for_days_on_or_after_bill_start(
        self, hass: HomeAssistant
    ) -> None:
        """Days on or after bill_start must use Current/Hourly."""
        mock_client = AsyncMock()
        mock_client.async_get_usage_hourly.return_value = []
        coord = _make_coordinator(hass, client=mock_client)

        today = datetime.now(UTC).date()
        bill_start = today - timedelta(days=5)
        start = today - timedelta(days=3)  # start is after bill_start
        end = today - timedelta(days=1)

        with patch.object(coord, "_import_intervals", new_callable=AsyncMock):
            await coord._fetch_range((start, end), None, bill_start)

        assert mock_client.async_get_usage_hourly.call_count == 3
        mock_client.async_get_usage_hourly_previous.assert_not_called()

    async def test_uses_current_hourly_when_bill_start_is_none(
        self, hass: HomeAssistant
    ) -> None:
        """When bill_start is None, always use Current/Hourly."""
        mock_client = AsyncMock()
        mock_client.async_get_usage_hourly.return_value = []
        coord = _make_coordinator(hass, client=mock_client)

        today = datetime.now(UTC).date()
        start = today - timedelta(days=2)
        end = today - timedelta(days=1)

        with patch.object(coord, "_import_intervals", new_callable=AsyncMock):
            await coord._fetch_range((start, end), None, None)

        assert mock_client.async_get_usage_hourly.call_count == 2
        mock_client.async_get_usage_hourly_previous.assert_not_called()

    async def test_fetch_range_skips_agl_error(self, hass: HomeAssistant) -> None:
        """AGLError on a day is skipped; remaining days still fetched."""
        from custom_components.haggle.agl.client import AGLError

        mock_client = AsyncMock()
        mock_client.async_get_usage_hourly.side_effect = AGLError("timeout")
        coord = _make_coordinator(hass, client=mock_client)

        today = datetime.now(UTC).date()
        start = today - timedelta(days=3)
        end = today - timedelta(days=1)

        with (
            patch.object(
                coord, "_import_intervals", new_callable=AsyncMock
            ) as mock_imp,
            patch(
                "custom_components.haggle.coordinator.asyncio.sleep", new=AsyncMock()
            ),
        ):
            await coord._fetch_range((start, end), None, None)  # must not raise

        # All days attempted despite errors; no intervals → _import_intervals not called
        assert mock_client.async_get_usage_hourly.call_count == 3
        mock_imp.assert_not_called()

    async def test_fetch_range_halts_on_rate_limit(self, hass: HomeAssistant) -> None:
        """A 429 mid-chunk halts the loop so the next poll resumes from the gap.

        Closes #34: silently dropping post-429 days corrupts the backfill resume
        point because get_last_statistics returns the last *successful* import,
        skipping any failures after the 429.
        """
        from custom_components.haggle.agl.client import AGLRateLimitError

        mock_client = AsyncMock()
        mock_client.async_get_usage_hourly.side_effect = [
            [],  # day 1 ok
            AGLRateLimitError("HTTP 429"),  # day 2 — halt
            [],  # day 3 — must not be attempted
        ]
        coord = _make_coordinator(hass, client=mock_client)

        today = datetime.now(UTC).date()
        start = today - timedelta(days=3)
        end = today - timedelta(days=1)

        with (
            patch.object(coord, "_import_intervals", new_callable=AsyncMock),
            patch(
                "custom_components.haggle.coordinator.asyncio.sleep", new=AsyncMock()
            ),
        ):
            await coord._fetch_range((start, end), None, None)

        # Two attempts only: day 1 succeeded, day 2 halted the loop.
        assert mock_client.async_get_usage_hourly.call_count == 2

    async def test_fetch_range_sleeps_between_requests(
        self, hass: HomeAssistant
    ) -> None:
        """Per-day fetches are spaced so a chunk-of-7 doesn't fire in <1 s.

        Closes #34: no inter-request sleep == 7 sequential GETs in a tight
        loop, which AGL's BFF can rate-limit.
        """
        mock_client = AsyncMock()
        mock_client.async_get_usage_hourly.return_value = []
        coord = _make_coordinator(hass, client=mock_client)

        today = datetime.now(UTC).date()
        start = today - timedelta(days=3)
        end = today - timedelta(days=1)  # 3 days → expect 2 sleeps

        sleep_mock = AsyncMock()
        with (
            patch.object(coord, "_import_intervals", new_callable=AsyncMock),
            patch("custom_components.haggle.coordinator.asyncio.sleep", new=sleep_mock),
        ):
            await coord._fetch_range((start, end), None, None)

        # 3 fetches, 2 inter-request sleeps (no sleep before first).
        assert mock_client.async_get_usage_hourly.call_count == 3
        assert sleep_mock.await_count == 2


# ---------------------------------------------------------------------------
# _fetch_and_import — chunked resume behaviour
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _stub_stored_tou_bands():
    """Default _get_stored_tou_bands to return an empty set for every test.

    _fetch_and_import calls _get_stored_tou_bands, which touches the recorder;
    these unit tests don't set one up. Tests exercising ToU wiring patch
    coord._get_stored_tou_bands explicitly, shadowing this class-level default.
    """
    with patch.object(
        HaggleCoordinator,
        "_get_stored_tou_bands",
        new_callable=AsyncMock,
        return_value=set(),
    ):
        yield


class TestFetchAndImport:
    async def test_no_previous_stats_starts_from_backfill_days_ago(
        self, hass: HomeAssistant
    ) -> None:
        """First install: fetch_start = today - BACKFILL_DAYS."""
        mock_client = AsyncMock()
        mock_client.async_get_usage_summary.return_value = _empty_summary()
        mock_client.async_get_plan.return_value = _empty_plan()
        coord = _make_coordinator(hass, client=mock_client)

        today = datetime.now(UTC).date()
        expected_start = today - timedelta(days=BACKFILL_DAYS)

        with (
            patch.object(
                coord,
                "_get_last_stat",
                new_callable=AsyncMock,
                return_value=(None, None),
            ),
            patch.object(coord, "_fetch_range", new_callable=AsyncMock) as mock_range,
        ):
            await coord._fetch_and_import()

        assert mock_range.called
        actual_start = mock_range.call_args[0][0][0]
        assert actual_start == expected_start

    async def test_big_gap_resumes_incrementally_without_rewindow(
        self, hass: HomeAssistant
    ) -> None:
        """Gap larger than REWINDOW_DAYS → resume from last_stat_date+1, throttled."""
        mock_client = AsyncMock()
        mock_client.async_get_usage_summary.return_value = _empty_summary()
        mock_client.async_get_plan.return_value = _empty_plan()
        coord = _make_coordinator(hass, client=mock_client)

        today = datetime.now(UTC).date()
        # last_date well outside the rewindow.
        last_date = today - timedelta(days=REWINDOW_DAYS + 5)
        expected_start = last_date + timedelta(days=1)

        with (
            patch.object(
                coord,
                "_get_last_stat",
                new_callable=AsyncMock,
                return_value=(100.0, last_date),
            ),
            patch.object(coord, "_fetch_range", new_callable=AsyncMock) as mock_range,
        ):
            await coord._fetch_and_import()

        assert mock_range.call_args[0][0][0] == expected_start
        # Throttle: fetch_end at most BACKFILL_CHUNK_DAYS - 1 past fetch_start.
        fetch_end = mock_range.call_args[0][0][1]
        assert (fetch_end - expected_start).days <= BACKFILL_CHUNK_DAYS - 1

    async def test_trailing_rewindow_when_within_window(
        self, hass: HomeAssistant
    ) -> None:
        """When last_stat_date is within REWINDOW_DAYS, refetch trailing window."""
        mock_client = AsyncMock()
        mock_client.async_get_usage_summary.return_value = _empty_summary()
        mock_client.async_get_plan.return_value = _empty_plan()
        coord = _make_coordinator(hass, client=mock_client)

        today = datetime.now(UTC).date()
        # last_stat_date is yesterday (definitely within rewindow).
        last_date = today - timedelta(days=1)
        expected_start = today - timedelta(days=REWINDOW_DAYS)

        with (
            patch.object(
                coord,
                "_get_last_stat",
                new_callable=AsyncMock,
                return_value=(500.0, last_date),
            ),
            patch.object(coord, "_fetch_range", new_callable=AsyncMock) as mock_range,
        ):
            await coord._fetch_and_import()

        # Rewindow re-fetches even though last_stat_date is "up to date".
        mock_range.assert_called_once()
        assert mock_range.call_args[0][0][0] == expected_start

    async def test_rewindow_clamped_to_backfill_floor(
        self, hass: HomeAssistant
    ) -> None:
        """fetch_start can't go further back than today - BACKFILL_DAYS."""
        # We can't actually trigger this (REWINDOW_DAYS < BACKFILL_DAYS) but the
        # logic must clamp safely if someone bumps REWINDOW_DAYS above BACKFILL_DAYS.
        # Best we can do here: assert fetch_start >= backfill_floor.
        mock_client = AsyncMock()
        mock_client.async_get_usage_summary.return_value = _empty_summary()
        mock_client.async_get_plan.return_value = _empty_plan()
        coord = _make_coordinator(hass, client=mock_client)

        today = datetime.now(UTC).date()
        last_date = today - timedelta(days=1)
        backfill_floor = today - timedelta(days=BACKFILL_DAYS)

        with (
            patch.object(
                coord,
                "_get_last_stat",
                new_callable=AsyncMock,
                return_value=(500.0, last_date),
            ),
            patch.object(coord, "_fetch_range", new_callable=AsyncMock) as mock_range,
        ):
            await coord._fetch_and_import()

        assert mock_range.call_args[0][0][0] >= backfill_floor

    async def test_returns_haggle_data_instance(self, hass: HomeAssistant) -> None:
        from custom_components.haggle.coordinator import HaggleData

        mock_client = AsyncMock()
        mock_client.async_get_usage_summary.return_value = _empty_summary()
        mock_client.async_get_plan.return_value = _empty_plan()
        coord = _make_coordinator(hass, client=mock_client)

        today = datetime.now(UTC).date()
        yesterday = today - timedelta(days=1)

        with (
            patch.object(
                coord,
                "_get_last_stat",
                new_callable=AsyncMock,
                return_value=(200.0, yesterday),
            ),
            patch.object(coord, "_fetch_range", new_callable=AsyncMock),
        ):
            result = await coord._fetch_and_import()

        assert isinstance(result, HaggleData)

    async def test_plan_unit_rate_extracted(self, hass: HomeAssistant) -> None:
        from custom_components.haggle.agl.models import PlanRates

        mock_client = AsyncMock()
        mock_client.async_get_usage_summary.return_value = _empty_summary()
        mock_client.async_get_plan.return_value = PlanRates(
            product_name="Smart Saver",
            unit_rates=[
                {"kind": "detail", "type": "c/kWh", "price": 33.792, "title": "Usage"}
            ],
            supply_charge_cents_per_day=131.714,
        )
        coord = _make_coordinator(hass, client=mock_client)

        today = datetime.now(UTC).date()
        yesterday = today - timedelta(days=1)

        with (
            patch.object(
                coord,
                "_get_last_stat",
                new_callable=AsyncMock,
                return_value=(100.0, yesterday),
            ),
            patch.object(coord, "_fetch_range", new_callable=AsyncMock),
        ):
            result = await coord._fetch_and_import()

        assert result.unit_rate_aud_per_kwh == pytest.approx(33.792 / 100.0)
        assert result.supply_charge_aud_per_day == pytest.approx(131.714 / 100.0)


# ---------------------------------------------------------------------------
# SAST-008: numeric bounds before recorder import
# ---------------------------------------------------------------------------


class TestNumericGuards:
    """Adversarial summaries (inf/nan/negative) must clamp to 0.0, not poison stats."""

    async def test_summary_with_inf_clamps_to_zero(self, hass: HomeAssistant) -> None:
        from custom_components.haggle.agl.models import BillPeriod

        mock_client = AsyncMock()
        mock_client.async_get_usage_summary.return_value = BillPeriod(
            start=date.today() - timedelta(days=15),
            end=date.today() + timedelta(days=15),
            consumption_kwh=float("inf"),
            cost_label="$inf",
            projection_label="$nan",
        )
        mock_client.async_get_plan.return_value = _empty_plan()
        coord = _make_coordinator(hass, client=mock_client)

        today = datetime.now(UTC).date()
        yesterday = today - timedelta(days=1)

        with (
            patch.object(
                coord,
                "_get_last_stat",
                new_callable=AsyncMock,
                return_value=(100.0, yesterday),
            ),
            patch.object(coord, "_fetch_range", new_callable=AsyncMock),
        ):
            result = await coord._fetch_and_import()

        # Adversarial values must not propagate to recorder-bound HaggleData.
        assert result.consumption_period_kwh == 0.0
        assert result.consumption_period_cost_aud == 0.0
        # "$nan" parses to nan → clamped to 0.0 (not None — proj_label was non-empty).
        assert result.bill_projection_aud == 0.0

    async def test_summary_with_negative_clamps_to_zero(
        self, hass: HomeAssistant
    ) -> None:
        from custom_components.haggle.agl.models import BillPeriod

        mock_client = AsyncMock()
        mock_client.async_get_usage_summary.return_value = BillPeriod(
            start=date.today() - timedelta(days=15),
            end=date.today() + timedelta(days=15),
            consumption_kwh=-5.0,
            cost_label="$-99.99",
            projection_label="",
        )
        mock_client.async_get_plan.return_value = _empty_plan()
        coord = _make_coordinator(hass, client=mock_client)

        today = datetime.now(UTC).date()
        yesterday = today - timedelta(days=1)

        with (
            patch.object(
                coord,
                "_get_last_stat",
                new_callable=AsyncMock,
                return_value=(100.0, yesterday),
            ),
            patch.object(coord, "_fetch_range", new_callable=AsyncMock),
        ):
            result = await coord._fetch_and_import()

        assert result.consumption_period_kwh == 0.0
        assert result.consumption_period_cost_aud == 0.0


# ---------------------------------------------------------------------------
# #31: fetch range is computed from UTC, not OS local time
# ---------------------------------------------------------------------------


class TestUTCDateBoundary:
    async def test_fetch_uses_utc_today_not_local(self, hass: HomeAssistant) -> None:
        """When UTC and OS local date differ, the integration must follow UTC.

        AGL `dateTime` slots are UTC; using `date.today()` on a non-UTC OS
        could fetch tomorrow's empty data or skip yesterday entirely.
        """
        from custom_components.haggle import coordinator as coord_mod

        # 14:30 UTC on 2026-05-02 → in Sydney (UTC+10) it is already
        # 00:30 on 2026-05-03. `date.today()` (local) would say 5/3 here;
        # `datetime.now(UTC).date()` correctly says 5/2.
        fixed_utc = datetime(2026, 5, 2, 14, 30, 0, tzinfo=UTC)

        class _FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz=None):  # type: ignore[override]
                return fixed_utc.astimezone(tz) if tz else fixed_utc

        mock_client = AsyncMock()
        mock_client.async_get_usage_summary.return_value = _empty_summary()
        mock_client.async_get_plan.return_value = _empty_plan()
        coord = _make_coordinator(hass, client=mock_client)

        with (
            patch.object(coord_mod, "datetime", _FrozenDateTime),
            patch.object(
                coord,
                "_get_last_stat",
                new_callable=AsyncMock,
                return_value=(None, None),
            ),
            patch.object(coord, "_fetch_range", new_callable=AsyncMock) as mock_range,
        ):
            await coord._fetch_and_import()

        # The UTC `today` is 2026-05-02; backfill starts BACKFILL_DAYS earlier.
        expected_start = date(2026, 5, 2) - timedelta(days=BACKFILL_DAYS)
        assert mock_range.call_args[0][0][0] == expected_start


# ---------------------------------------------------------------------------
# Time-of-Use: per-tariff statistic series
# ---------------------------------------------------------------------------


def _iv(dt: datetime, kwh: float, cost: float, rate_type: str) -> IntervalReading:
    return IntervalReading(dt=dt, kwh=kwh, cost_aud=cost, rate_type=rate_type)


def _stat_ids(mock_add: MagicMock) -> list[str]:
    """Statistic IDs across all async_add_external_statistics calls, in order."""
    return [c[0][1]["statistic_id"] for c in mock_add.call_args_list]


class TestImportIntervalsTou:
    async def test_tou_intervals_emit_aggregate_plus_per_tariff(
        self, hass: HomeAssistant
    ) -> None:
        """Mixed tariff data → aggregate (2) + one cons+cost per present band (8)."""
        coord = _make_coordinator(hass)
        intervals = [
            _iv(datetime(2026, 4, 28, 7, 0, tzinfo=UTC), 1.0, 0.42, "peak"),
            _iv(datetime(2026, 4, 28, 10, 0, tzinfo=UTC), 0.8, 0.18, "shoulder"),
            _iv(datetime(2026, 4, 28, 18, 0, tzinfo=UTC), 0.4, 0.07, "offpeak"),
            _iv(datetime(2026, 4, 28, 2, 0, tzinfo=UTC), 0.2, 0.03, "normal"),
        ]

        with (
            patch(_PATCH_ADD_STATS) as mock_add,
            patch.object(
                coord,
                "_get_baseline_sums",
                new=AsyncMock(return_value=(0.0, 0.0)),
            ),
            patch.object(
                coord,
                "_get_tariff_baseline_sums",
                new=AsyncMock(return_value={}),
            ),
        ):
            await coord._import_intervals(intervals)

        ids = _stat_ids(mock_add)
        assert mock_add.call_count == 10
        # Aggregate emitted first (consumption then cost), preserving order.
        assert ids[0] == f"{DOMAIN}:{STAT_CONSUMPTION}_{_CONTRACT}"
        assert ids[1] == f"{DOMAIN}:cost_{_CONTRACT}"
        for band in ("peak", "offpeak", "shoulder", "normal"):
            assert f"{DOMAIN}:{STAT_CONSUMPTION}_{band}_{_CONTRACT}" in ids
            assert f"{DOMAIN}:cost_{band}_{_CONTRACT}" in ids

    async def test_per_tariff_states_partition_aggregate(
        self, hass: HomeAssistant
    ) -> None:
        """#114: per-tariff consumption states sum back to the aggregate state,
        hour by hour — so the per-band series are a true partition with no kWh
        lost or double-counted (presence/count alone wouldn't catch a drop)."""
        coord = _make_coordinator(hass)
        intervals = [
            # Two bands sharing the 07:00 UTC hour, plus a second hour, so the
            # partition is checked per-hour and not merely in total.
            _iv(datetime(2026, 4, 28, 7, 0, tzinfo=UTC), 1.0, 0.42, "peak"),
            _iv(datetime(2026, 4, 28, 7, 30, tzinfo=UTC), 0.5, 0.20, "shoulder"),
            _iv(datetime(2026, 4, 28, 8, 0, tzinfo=UTC), 0.4, 0.07, "offpeak"),
            _iv(datetime(2026, 4, 28, 8, 30, tzinfo=UTC), 0.2, 0.03, "normal"),
        ]

        with (
            patch(_PATCH_ADD_STATS) as mock_add,
            patch.object(
                coord, "_get_baseline_sums", new=AsyncMock(return_value=(0.0, 0.0))
            ),
            patch.object(
                coord, "_get_tariff_baseline_sums", new=AsyncMock(return_value={})
            ),
        ):
            await coord._import_intervals(intervals)

        # {statistic_id: {hour_start: state}} across every emitted series.
        states: dict[str, dict[datetime, float]] = {}
        for call in mock_add.call_args_list:
            stat_id = call[0][1]["statistic_id"]
            states[stat_id] = {s["start"]: s["state"] for s in call[0][2]}

        agg = states[f"{DOMAIN}:{STAT_CONSUMPTION}_{_CONTRACT}"]
        bands = ("peak", "offpeak", "shoulder", "normal")
        for hour, agg_state in agg.items():
            band_total = sum(
                states.get(f"{DOMAIN}:{STAT_CONSUMPTION}_{b}_{_CONTRACT}", {}).get(
                    hour, 0.0
                )
                for b in bands
            )
            assert band_total == pytest.approx(agg_state)

    async def test_flat_rate_only_emits_aggregate(self, hass: HomeAssistant) -> None:
        """Only `normal` intervals and no known ToU bands → aggregate series only."""
        coord = _make_coordinator(hass)
        intervals = [
            _iv(datetime(2026, 4, 28, 1, 0, tzinfo=UTC), 0.5, 0.16, "normal"),
            _iv(datetime(2026, 4, 28, 2, 0, tzinfo=UTC), 0.3, 0.10, "normal"),
        ]

        with (
            patch(_PATCH_ADD_STATS) as mock_add,
            patch.object(
                coord,
                "_get_baseline_sums",
                new=AsyncMock(return_value=(0.0, 0.0)),
            ),
        ):
            await coord._import_intervals(intervals)

        assert mock_add.call_count == 2
        assert not coord._active_tou_bands

    async def test_known_band_makes_normal_a_tou_series(
        self, hass: HomeAssistant
    ) -> None:
        """A contract already seen on ToU emits the normal/anytime per-tariff series."""
        coord = _make_coordinator(hass)
        intervals = [_iv(datetime(2026, 4, 28, 1, 0, tzinfo=UTC), 0.5, 0.16, "normal")]

        with (
            patch(_PATCH_ADD_STATS) as mock_add,
            patch.object(
                coord,
                "_get_baseline_sums",
                new=AsyncMock(return_value=(0.0, 0.0)),
            ),
            patch.object(
                coord,
                "_get_tariff_baseline_sums",
                new=AsyncMock(return_value={}),
            ),
        ):
            await coord._import_intervals(intervals, known_bands=frozenset({"peak"}))

        ids = _stat_ids(mock_add)
        assert mock_add.call_count == 4  # aggregate(2) + normal per-tariff(2)
        assert f"{DOMAIN}:{STAT_CONSUMPTION}_normal_{_CONTRACT}" in ids

    async def test_per_tariff_baseline_offset_applied(
        self, hass: HomeAssistant
    ) -> None:
        """_get_tariff_baseline_sums offsets the per-tariff cumulative sum."""
        coord = _make_coordinator(hass)
        intervals = [_iv(datetime(2026, 4, 28, 7, 0, tzinfo=UTC), 1.0, 0.42, "peak")]
        peak_cons_id, peak_cost_id = coord._tariff_stat_ids("peak")

        with (
            patch(_PATCH_ADD_STATS) as mock_add,
            patch.object(
                coord,
                "_get_baseline_sums",
                new=AsyncMock(return_value=(0.0, 0.0)),
            ),
            patch.object(
                coord,
                "_get_tariff_baseline_sums",
                new=AsyncMock(return_value={peak_cons_id: 10.0, peak_cost_id: 5.0}),
            ),
        ):
            await coord._import_intervals(intervals, known_bands=frozenset({"peak"}))

        for c in mock_add.call_args_list:
            if c[0][1]["statistic_id"] == peak_cons_id:
                assert c[0][2][0]["sum"] == pytest.approx(11.0)
                break
        else:  # pragma: no cover
            pytest.fail("peak consumption series not emitted")

    async def test_active_bands_updated_after_import(self, hass: HomeAssistant) -> None:
        coord = _make_coordinator(hass)
        intervals = [_iv(datetime(2026, 4, 28, 7, 0, tzinfo=UTC), 1.0, 0.42, "peak")]
        with (
            patch(_PATCH_ADD_STATS),
            patch.object(
                coord,
                "_get_baseline_sums",
                new=AsyncMock(return_value=(0.0, 0.0)),
            ),
            patch.object(
                coord,
                "_get_tariff_baseline_sums",
                new=AsyncMock(return_value={}),
            ),
        ):
            await coord._import_intervals(intervals)
        assert "peak" in coord._active_tou_bands


# ---------------------------------------------------------------------------
# Time-of-Use: recorder helper methods
# ---------------------------------------------------------------------------


class TestTouRecorderHelpers:
    async def test_baseline_sums_returns_last_sum_per_id(
        self, hass: HomeAssistant
    ) -> None:
        coord = _make_coordinator(hass)
        cons_id, cost_id = coord._tariff_stat_ids("peak")
        rows = {
            cons_id: [{"sum": 10.0}, {"sum": 22.0}],
            cost_id: [{"sum": 3.0}, {"sum": 7.5}],
        }
        with patch(_PATCH_GET_INSTANCE, _mock_get_instance(rows)):
            out = await coord._get_tariff_baseline_sums(
                {cons_id, cost_id}, datetime(2026, 4, 28, tzinfo=UTC)
            )
        assert out[cons_id] == pytest.approx(22.0)
        assert out[cost_id] == pytest.approx(7.5)

    async def test_baseline_sums_empty_ids_short_circuits(
        self, hass: HomeAssistant
    ) -> None:
        coord = _make_coordinator(hass)
        assert await coord._get_tariff_baseline_sums(set(), datetime.now(UTC)) == {}

    async def test_baseline_sums_missing_rows_default_zero(
        self, hass: HomeAssistant
    ) -> None:
        coord = _make_coordinator(hass)
        cons_id, _ = coord._tariff_stat_ids("shoulder")
        with patch(_PATCH_GET_INSTANCE, _mock_get_instance({})):
            out = await coord._get_tariff_baseline_sums({cons_id}, datetime.now(UTC))
        assert out[cons_id] == 0.0

    async def test_baseline_reaches_back_when_band_absent_from_window(
        self, hass: HomeAssistant
    ) -> None:
        """#114: a band with no rows in the look-back window but older stored
        history resumes from its true last sum — not 0.0 — so a band that goes
        quiet for longer than the window then reappears inside the trailing
        rewindow keeps its cumulative sum monotonic instead of stepping down.
        """
        from custom_components.haggle.coordinator import _EARLIEST_HISTORY

        coord = _make_coordinator(hass)
        cons_id, _ = coord._tariff_stat_ids("shoulder")
        before = datetime(2026, 6, 1, tzinfo=UTC)

        def _executor(_func: object, _hass: object, start_dt: datetime, *_rest: object):
            # Narrow window → band absent; reach-back to history start → found.
            if start_dt == _EARLIEST_HISTORY:
                return {cons_id: [{"sum": 5.0}, {"sum": 42.0}]}
            return {}

        mock_instance = MagicMock()
        mock_instance.async_add_executor_job = AsyncMock(side_effect=_executor)
        with patch(_PATCH_GET_INSTANCE, MagicMock(return_value=mock_instance)):
            out = await coord._get_tariff_baseline_sums({cons_id}, before)

        assert out[cons_id] == pytest.approx(42.0)
        # Two recorder calls: the narrow window, then the reach-back fallback.
        assert mock_instance.async_add_executor_job.await_count == 2

    async def test_baseline_no_reach_back_when_band_in_window(
        self, hass: HomeAssistant
    ) -> None:
        """#114: when the band has rows in the normal look-back window, the
        extra reach-back lookup is skipped (a single recorder round-trip)."""
        coord = _make_coordinator(hass)
        cons_id, _ = coord._tariff_stat_ids("peak")
        mock_instance = MagicMock()
        mock_instance.async_add_executor_job = AsyncMock(
            return_value={cons_id: [{"sum": 9.0}]}
        )
        with patch(_PATCH_GET_INSTANCE, MagicMock(return_value=mock_instance)):
            out = await coord._get_tariff_baseline_sums(
                {cons_id}, datetime(2026, 6, 1, tzinfo=UTC)
            )
        assert out[cons_id] == pytest.approx(9.0)
        assert mock_instance.async_add_executor_job.await_count == 1

    async def test_stored_tou_bands_detects_present_band(
        self, hass: HomeAssistant
    ) -> None:
        coord = _make_coordinator(hass)
        peak_cons_id, _ = coord._tariff_stat_ids("peak")
        # Restore the real implementation on this instance (the autouse fixture
        # patches the class-level method to return set(); these tests exercise
        # the real recorder interaction via a _PATCH_GET_INSTANCE mock).
        # _REAL_GET_STORED_TOU_BANDS was captured at module import time before
        # any autouse fixture could replace the class attribute.
        with (
            patch.object(
                coord,
                "_get_stored_tou_bands",
                new=_REAL_GET_STORED_TOU_BANDS.__get__(coord),
            ),
            patch(
                _PATCH_GET_INSTANCE, _mock_get_instance({peak_cons_id: [{"sum": 1.0}]})
            ),
        ):
            bands = await coord._get_stored_tou_bands()
        assert bands == {"peak"}

    async def test_stored_tou_bands_empty_when_no_rows(
        self, hass: HomeAssistant
    ) -> None:
        coord = _make_coordinator(hass)
        with (
            patch.object(
                coord,
                "_get_stored_tou_bands",
                new=_REAL_GET_STORED_TOU_BANDS.__get__(coord),
            ),
            patch(_PATCH_GET_INSTANCE, _mock_get_instance({})),
        ):
            assert await coord._get_stored_tou_bands() == set()


# ---------------------------------------------------------------------------
# Time-of-Use: _fetch_and_import wiring (rates, active_tariffs, reload)
# ---------------------------------------------------------------------------


def _tou_plan() -> PlanRates:
    return PlanRates(
        product_name="Time of Use Saver",
        unit_rates=[
            {"kind": "detail", "type": "c/kWh", "price": 41.9, "title": "Peak"}
        ],
        supply_charge_cents_per_day=131.714,
        tou_unit_rates={"peak": 41.9, "offpeak": 18.04, "shoulder": 22.55},
    )


class TestFetchAndImportTou:
    async def test_tou_rates_and_active_tariffs_in_data(
        self, hass: HomeAssistant
    ) -> None:
        mock_client = AsyncMock()
        mock_client.async_get_usage_summary.return_value = _empty_summary()
        mock_client.async_get_plan.return_value = _tou_plan()
        coord = _make_coordinator(hass, client=mock_client)

        today = datetime.now(UTC).date()
        yesterday = today - timedelta(days=1)

        with (
            patch.object(
                coord,
                "_get_last_stat",
                new_callable=AsyncMock,
                return_value=(100.0, yesterday),
            ),
            patch.object(
                coord,
                "_get_stored_tou_bands",
                new_callable=AsyncMock,
                return_value={"peak", "offpeak"},
            ),
            patch.object(coord, "_fetch_range", new_callable=AsyncMock),
        ):
            result = await coord._fetch_and_import()

        assert result.unit_rate_peak_aud_per_kwh == pytest.approx(0.419)
        assert result.unit_rate_offpeak_aud_per_kwh == pytest.approx(0.1804)
        assert result.unit_rate_shoulder_aud_per_kwh == pytest.approx(0.2255)
        assert {"peak", "offpeak"} <= result.active_tariffs

    async def test_flat_plan_leaves_tou_rates_none(self, hass: HomeAssistant) -> None:
        mock_client = AsyncMock()
        mock_client.async_get_usage_summary.return_value = _empty_summary()
        mock_client.async_get_plan.return_value = _empty_plan()
        coord = _make_coordinator(hass, client=mock_client)

        today = datetime.now(UTC).date()
        yesterday = today - timedelta(days=1)

        with (
            patch.object(
                coord,
                "_get_last_stat",
                new_callable=AsyncMock,
                return_value=(100.0, yesterday),
            ),
            patch.object(
                coord,
                "_get_stored_tou_bands",
                new_callable=AsyncMock,
                return_value=set(),
            ),
            patch.object(coord, "_fetch_range", new_callable=AsyncMock),
        ):
            result = await coord._fetch_and_import()

        assert result.unit_rate_peak_aud_per_kwh is None
        assert result.active_tariffs == frozenset()

    async def test_reload_scheduled_when_new_band_appears(
        self, hass: HomeAssistant
    ) -> None:
        """Non-first refresh + newly stored ToU band → schedule_reload fires once."""
        from custom_components.haggle.coordinator import HaggleData

        mock_client = AsyncMock()
        mock_client.async_get_usage_summary.return_value = _empty_summary()
        mock_client.async_get_plan.return_value = _tou_plan()
        coord = _make_coordinator(hass, client=mock_client)
        # Simulate a prior successful refresh (so this is not the first one).
        coord.data = HaggleData(0.0, 0.0, None, None, None, 0.0)

        today = datetime.now(UTC).date()
        yesterday = today - timedelta(days=1)

        with (
            patch.object(
                coord,
                "_get_last_stat",
                new_callable=AsyncMock,
                return_value=(100.0, yesterday),
            ),
            patch.object(
                coord,
                "_get_stored_tou_bands",
                new_callable=AsyncMock,
                return_value={"peak"},
            ),
            patch.object(coord, "_fetch_range", new_callable=AsyncMock),
            patch.object(
                coord.hass.config_entries, "async_schedule_reload"
            ) as mock_reload,
        ):
            await coord._fetch_and_import()

        mock_reload.assert_called_once()

    async def test_no_reload_on_first_refresh(self, hass: HomeAssistant) -> None:
        """First refresh (coord.data is None) must not schedule a reload."""
        mock_client = AsyncMock()
        mock_client.async_get_usage_summary.return_value = _empty_summary()
        mock_client.async_get_plan.return_value = _tou_plan()
        coord = _make_coordinator(hass, client=mock_client)

        today = datetime.now(UTC).date()
        yesterday = today - timedelta(days=1)

        with (
            patch.object(
                coord,
                "_get_last_stat",
                new_callable=AsyncMock,
                return_value=(100.0, yesterday),
            ),
            patch.object(
                coord,
                "_get_stored_tou_bands",
                new_callable=AsyncMock,
                return_value={"peak"},
            ),
            patch.object(coord, "_fetch_range", new_callable=AsyncMock),
            patch.object(
                coord.hass.config_entries, "async_schedule_reload"
            ) as mock_reload,
        ):
            await coord._fetch_and_import()

        mock_reload.assert_not_called()


# ---------------------------------------------------------------------------
# Solar generation import (#128)
# ---------------------------------------------------------------------------


class TestSolarGeneration:
    async def test_import_generation_writes_generation_and_credit_series(
        self, hass: HomeAssistant
    ) -> None:
        """Feed-in intervals produce haggle:generation_* and *_credit series."""
        coord = _make_coordinator(hass)
        intervals = [
            _make_interval(datetime(2026, 6, 29, 2, 0, tzinfo=UTC), kwh=1.276),
            _make_interval(datetime(2026, 6, 29, 2, 30, tzinfo=UTC), kwh=1.168),
        ]

        with (
            patch(_PATCH_ADD_STATS) as mock_add,
            patch.object(
                coord,
                "_get_baseline_sums",
                new=AsyncMock(return_value=(10.0, 1.0)),
            ),
        ):
            await coord._import_generation(intervals)

        assert mock_add.call_count == 2
        gen_meta = mock_add.call_args_list[0][0][1]
        credit_meta = mock_add.call_args_list[1][0][1]
        assert gen_meta["statistic_id"] == f"{DOMAIN}:generation_{_CONTRACT}"
        assert gen_meta["unit_class"] == "energy"
        assert gen_meta["unit_of_measurement"] == "kWh"
        assert gen_meta["has_sum"] is True
        assert credit_meta["statistic_id"] == f"{DOMAIN}:generation_credit_{_CONTRACT}"
        assert credit_meta["unit_class"] is None
        assert credit_meta["unit_of_measurement"] == "AUD"

        # Both 30-min slots aggregate into the 02:00 hour, on the baseline.
        gen_stats = mock_add.call_args_list[0][0][2]
        assert len(gen_stats) == 1
        assert gen_stats[0]["state"] == pytest.approx(1.276 + 1.168)
        assert gen_stats[0]["sum"] == pytest.approx(10.0 + 1.276 + 1.168)
        assert coord._latest_generation_kwh == pytest.approx(12.444)

    async def test_import_generation_empty_is_noop(self, hass: HomeAssistant) -> None:
        coord = _make_coordinator(hass)
        with patch(_PATCH_ADD_STATS) as mock_add:
            await coord._import_generation([])
        assert mock_add.call_count == 0

    async def test_zero_export_day_writes_resume_marker(
        self, hass: HomeAssistant
    ) -> None:
        """A fetched all-zero day gets one zero-delta row so resume advances.

        Codex review on PR #144: without a marker, a cloudy zero-export week
        (a full chunk) or a solar system newer than the backfill floor would
        refetch the same days forever and never unlock the period sensors.
        """
        coord = _make_coordinator(hass)
        with (
            patch(_PATCH_ADD_STATS) as mock_add,
            patch.object(
                coord,
                "_get_baseline_sums",
                new=AsyncMock(return_value=(10.0, 1.0)),
            ),
        ):
            await coord._import_generation([], fetched_days=[date(2026, 6, 29)])

        assert mock_add.call_count == 2
        gen_stats = mock_add.call_args_list[0][0][2]
        assert len(gen_stats) == 1
        assert gen_stats[0]["state"] == 0.0
        # Zero delta: the cumulative sum stays on the baseline.
        assert gen_stats[0]["sum"] == pytest.approx(10.0)
        # Marker lands inside the local day (floored to the hour).
        from homeassistant.util import dt as dt_util

        marker = gen_stats[0]["start"]
        expected = dt_util.as_utc(
            dt_util.start_of_local_day(date(2026, 6, 29))
        ).replace(minute=0, second=0, microsecond=0)
        assert marker == expected

    async def test_no_marker_when_day_has_real_intervals(
        self, hass: HomeAssistant
    ) -> None:
        """A day with surviving intervals must not get an extra zero row."""
        from homeassistant.util import dt as dt_util

        day = date(2026, 6, 29)
        slot = dt_util.as_utc(dt_util.start_of_local_day(day)) + timedelta(hours=3)
        coord = _make_coordinator(hass)
        with (
            patch(_PATCH_ADD_STATS) as mock_add,
            patch.object(
                coord,
                "_get_baseline_sums",
                new=AsyncMock(return_value=(0.0, 0.0)),
            ),
        ):
            await coord._import_generation(
                [_make_interval(slot, kwh=1.0)], fetched_days=[day]
            )

        gen_stats = mock_add.call_args_list[0][0][2]
        assert len(gen_stats) == 1  # the real hour only, no marker row

    async def test_fetch_range_marks_zero_days_but_not_errored_days(
        self, hass: HomeAssistant
    ) -> None:
        """Successful-but-empty solar days count as fetched; errored days don't."""
        from custom_components.haggle.agl.client import AGLError

        mock_client = AsyncMock()
        mock_client.async_get_usage_hourly.return_value = []
        mock_client.async_get_solar_hourly.side_effect = [
            [],  # day 1: success, zero export → marked
            AGLError("HTTP 500"),  # day 2: errored → NOT marked, retried later
            [],  # day 3: success → marked
        ]
        coord = _make_coordinator(hass, client=mock_client)

        day_range = (date(2026, 6, 27), date(2026, 6, 29))
        with (
            patch.object(
                coord, "_import_generation", new_callable=AsyncMock
            ) as mock_gen,
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            await coord._fetch_range(day_range, day_range, None)

        mock_gen.assert_called_once()
        assert mock_gen.call_args.kwargs["fetched_days"] == [
            date(2026, 6, 27),
            date(2026, 6, 29),
        ]

    async def test_fetch_range_solar_range_fetches_solar_endpoint(
        self, hass: HomeAssistant
    ) -> None:
        """A solar_range adds one solar fetch per day with the right variant."""
        mock_client = AsyncMock()
        mock_client.async_get_usage_hourly.return_value = []
        mock_client.async_get_usage_hourly_previous.return_value = []
        mock_client.async_get_solar_hourly.return_value = []
        coord = _make_coordinator(hass, client=mock_client)

        bill_start = date(2026, 6, 29)
        day_range = (date(2026, 6, 28), date(2026, 6, 29))
        with (
            patch.object(coord, "_import_generation", new_callable=AsyncMock),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            await coord._fetch_range(day_range, day_range, bill_start)

        calls = mock_client.async_get_solar_hourly.call_args_list
        assert len(calls) == 2
        # Day before bill_start uses the Previous variant; day inside, Current.
        assert calls[0][0][1] == date(2026, 6, 28)
        assert calls[0][1]["previous"] is True
        assert calls[1][0][1] == date(2026, 6, 29)
        assert calls[1][1]["previous"] is False

    async def test_fetch_range_without_solar_makes_no_solar_calls(
        self, hass: HomeAssistant
    ) -> None:
        mock_client = AsyncMock()
        mock_client.async_get_usage_hourly.return_value = []
        coord = _make_coordinator(hass, client=mock_client)

        with patch("asyncio.sleep", new=AsyncMock()):
            await coord._fetch_range((date(2026, 6, 29), date(2026, 6, 29)), None, None)

        assert mock_client.async_get_solar_hourly.call_count == 0

    async def test_fetch_range_disjoint_ranges_skip_other_series(
        self, hass: HomeAssistant
    ) -> None:
        """Each series only fetches days inside its own range.

        The upgrade shape: consumption is caught up (trailing rewindow) while
        the new generation series backfills from 30 days back. Consumption
        must not re-fetch the solar backlog days, and vice versa.
        """
        mock_client = AsyncMock()
        mock_client.async_get_usage_hourly.return_value = []
        mock_client.async_get_solar_hourly.return_value = []
        coord = _make_coordinator(hass, client=mock_client)

        cons_range = (date(2026, 6, 27), date(2026, 6, 29))
        solar_range = (date(2026, 6, 1), date(2026, 6, 3))
        with (
            patch.object(coord, "_import_generation", new_callable=AsyncMock),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            await coord._fetch_range(cons_range, solar_range, None)

        cons_days = [c[0][1] for c in mock_client.async_get_usage_hourly.call_args_list]
        solar_days = [
            c[0][1] for c in mock_client.async_get_solar_hourly.call_args_list
        ]
        assert cons_days == [
            date(2026, 6, 27),
            date(2026, 6, 28),
            date(2026, 6, 29),
        ]
        assert solar_days == [date(2026, 6, 1), date(2026, 6, 2), date(2026, 6, 3)]

    async def test_fetch_range_solar_rate_limit_still_imports_partial(
        self, hass: HomeAssistant
    ) -> None:
        """A 429 on the solar endpoint halts the chunk but imports both batches.

        The consumption batch and the partial solar batch are idempotent, so
        importing them preserves each series' own resume point for next cycle.
        """
        from custom_components.haggle.agl.client import AGLRateLimitError

        reading = _make_interval(datetime(2026, 6, 29, 2, 0, tzinfo=UTC), kwh=1.0)
        mock_client = AsyncMock()
        mock_client.async_get_usage_hourly.return_value = [reading]
        mock_client.async_get_solar_hourly.side_effect = [
            [reading],  # day 1 ok
            AGLRateLimitError("HTTP 429"),  # day 2 — halt
            [reading],  # day 3 — must not be attempted
        ]
        coord = _make_coordinator(hass, client=mock_client)

        day_range = (date(2026, 6, 28), date(2026, 6, 30))
        with (
            patch.object(
                coord, "_import_intervals", new_callable=AsyncMock
            ) as mock_imp,
            patch.object(
                coord, "_import_generation", new_callable=AsyncMock
            ) as mock_gen,
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            await coord._fetch_range(day_range, day_range, None)

        # Solar halted on day 2; day 3 fetched for neither series.
        assert mock_client.async_get_solar_hourly.call_count == 2
        assert mock_client.async_get_usage_hourly.call_count == 2
        # Both partial batches still imported.
        mock_imp.assert_called_once()
        mock_gen.assert_called_once()
        assert mock_gen.call_args[0][0] == [reading]

    async def test_refresh_has_solar_sets_flag_from_overview(
        self, hass: HomeAssistant
    ) -> None:
        from custom_components.haggle.agl.models import Contract

        mock_client = AsyncMock()
        mock_client.async_get_overview.return_value = [
            Contract(
                contract_number=_CONTRACT,
                account_number="1234567890",
                address="",
                fuel_type="electricityContract",
                status="active",
                has_solar=True,
            )
        ]
        coord = _make_coordinator(hass, client=mock_client)
        assert coord._has_solar is False
        await coord._refresh_has_solar()
        assert coord._has_solar is True

    async def test_refresh_has_solar_ignores_other_contracts(
        self, hass: HomeAssistant
    ) -> None:
        from custom_components.haggle.agl.models import Contract

        mock_client = AsyncMock()
        mock_client.async_get_overview.return_value = [
            Contract(
                contract_number="0000000000",
                account_number="1234567890",
                address="",
                fuel_type="electricityContract",
                status="active",
                has_solar=True,
            )
        ]
        coord = _make_coordinator(hass, client=mock_client)
        await coord._refresh_has_solar()
        assert coord._has_solar is False

    async def test_refresh_has_solar_sticky_on_error(self, hass: HomeAssistant) -> None:
        from custom_components.haggle.agl.client import AGLError

        mock_client = AsyncMock()
        mock_client.async_get_overview.side_effect = AGLError("boom")
        coord = _make_coordinator(hass, client=mock_client)
        coord._has_solar = True
        await coord._refresh_has_solar()
        assert coord._has_solar is True


# ---------------------------------------------------------------------------
# Per-series backfill ranges (#128 beta.2) — solar catches up independently
# ---------------------------------------------------------------------------


def _solar_contract() -> object:
    from custom_components.haggle.agl.models import Contract

    return Contract(
        contract_number=_CONTRACT,
        account_number="1234567890",
        address="",
        fuel_type="electricityContract",
        status="active",
        has_solar=True,
    )


class TestPerSeriesBackfill:
    async def test_upgrader_solar_backfills_from_floor_while_consumption_rewindows(
        self, hass: HomeAssistant
    ) -> None:
        """Upgrade shape: consumption caught up, generation series empty.

        The generation series must start its own 30-day backfill instead of
        inheriting consumption's trailing rewindow (which would silently cap
        solar history at REWINDOW_DAYS forever).
        """
        mock_client = AsyncMock()
        mock_client.async_get_usage_summary.return_value = _empty_summary()
        mock_client.async_get_plan.return_value = _empty_plan()
        mock_client.async_get_overview.return_value = [_solar_contract()]
        coord = _make_coordinator(hass, client=mock_client)

        today = datetime.now(UTC).date()
        yesterday = today - timedelta(days=1)
        stat_id_cons = f"{DOMAIN}:{STAT_CONSUMPTION}_{_CONTRACT}"

        async def _last_stat(stat_id: str) -> tuple[float | None, date | None]:
            if stat_id == stat_id_cons:
                return (500.0, yesterday)  # consumption caught up
            return (None, None)  # generation/credit series empty

        with (
            patch.object(coord, "_get_last_stat", side_effect=_last_stat),
            patch.object(coord, "_fetch_range", new_callable=AsyncMock) as mock_range,
        ):
            await coord._fetch_and_import()

        cons_range, solar_range = mock_range.call_args[0][0:2]
        assert cons_range == (today - timedelta(days=REWINDOW_DAYS), yesterday)
        backfill_floor = today - timedelta(days=BACKFILL_DAYS)
        assert solar_range == (
            backfill_floor,
            backfill_floor + timedelta(days=BACKFILL_CHUNK_DAYS - 1),
        )

    async def test_fresh_solar_install_ranges_coincide(
        self, hass: HomeAssistant
    ) -> None:
        """Both series empty → both ranges are the same first backfill chunk."""
        mock_client = AsyncMock()
        mock_client.async_get_usage_summary.return_value = _empty_summary()
        mock_client.async_get_plan.return_value = _empty_plan()
        mock_client.async_get_overview.return_value = [_solar_contract()]
        coord = _make_coordinator(hass, client=mock_client)

        with (
            patch.object(
                coord,
                "_get_last_stat",
                new_callable=AsyncMock,
                return_value=(None, None),
            ),
            patch.object(coord, "_fetch_range", new_callable=AsyncMock) as mock_range,
        ):
            await coord._fetch_and_import()

        cons_range, solar_range = mock_range.call_args[0][0:2]
        assert cons_range == solar_range

    async def test_non_solar_contract_passes_no_solar_range(
        self, hass: HomeAssistant
    ) -> None:
        mock_client = AsyncMock()
        mock_client.async_get_usage_summary.return_value = _empty_summary()
        mock_client.async_get_plan.return_value = _empty_plan()
        coord = _make_coordinator(hass, client=mock_client)

        with (
            patch.object(
                coord,
                "_get_last_stat",
                new_callable=AsyncMock,
                return_value=(None, None),
            ),
            patch.object(coord, "_fetch_range", new_callable=AsyncMock) as mock_range,
        ):
            await coord._fetch_and_import()

        assert mock_range.call_args[0][1] is None


# ---------------------------------------------------------------------------
# Bill-period solar totals (#128 beta.2) — match the app's "Sold To Grid" tile
# ---------------------------------------------------------------------------


class TestGenerationPeriodTotals:
    async def test_returns_none_when_no_generation_stats(
        self, hass: HomeAssistant
    ) -> None:
        coord = _make_coordinator(hass)
        today = datetime.now(UTC).date()
        result = await coord._get_generation_period_totals(
            today - timedelta(days=10), None, today
        )
        assert result == (None, None)

    async def test_returns_none_while_backfill_behind_rewindow(
        self, hass: HomeAssistant
    ) -> None:
        """Mid-backfill partials never publish — they can't match the app."""
        coord = _make_coordinator(hass)
        today = datetime.now(UTC).date()
        stale = today - timedelta(days=REWINDOW_DAYS + 1)
        result = await coord._get_generation_period_totals(
            today - timedelta(days=10), stale, today
        )
        assert result == (None, None)

    async def test_caught_up_returns_latest_minus_bill_start_baseline(
        self, hass: HomeAssistant
    ) -> None:
        """Period totals = in-memory latest sums - sums at bill_start midnight."""
        from homeassistant.util import dt as dt_util

        coord = _make_coordinator(hass)
        coord._latest_generation_kwh = 18.019
        coord._latest_generation_credit = 2.3629
        today = datetime.now(UTC).date()
        bill_start = today - timedelta(days=12)

        with patch.object(
            coord,
            "_get_baseline_sums",
            new=AsyncMock(return_value=(10.0, 1.0)),
        ) as mock_base:
            kwh, credit = await coord._get_generation_period_totals(
                bill_start, today - timedelta(days=1), today
            )

        assert kwh == pytest.approx(8.019)
        assert credit == pytest.approx(1.3629)
        cutoff = mock_base.call_args[0][2]
        assert cutoff == dt_util.start_of_local_day(bill_start)

    async def test_clamps_negative_diff_to_zero(self, hass: HomeAssistant) -> None:
        coord = _make_coordinator(hass)
        coord._latest_generation_kwh = 5.0
        coord._latest_generation_credit = 0.5
        today = datetime.now(UTC).date()

        with patch.object(
            coord,
            "_get_baseline_sums",
            new=AsyncMock(return_value=(6.0, 1.0)),
        ):
            kwh, credit = await coord._get_generation_period_totals(
                today - timedelta(days=3), today - timedelta(days=1), today
            )

        assert kwh == 0.0
        assert credit == 0.0

    async def test_fetch_and_import_wires_period_totals_and_feed_in_rate(
        self, hass: HomeAssistant
    ) -> None:
        """HaggleData carries the period totals and the AUD/kWh feed-in rate."""
        mock_client = AsyncMock()
        mock_client.async_get_usage_summary.return_value = _empty_summary()
        plan = _empty_plan()
        plan.feed_in_rate_cents_per_kwh = 1.2
        mock_client.async_get_plan.return_value = plan
        mock_client.async_get_overview.return_value = [_solar_contract()]
        coord = _make_coordinator(hass, client=mock_client)

        with (
            patch.object(
                coord,
                "_get_last_stat",
                new_callable=AsyncMock,
                return_value=(None, None),
            ),
            patch.object(coord, "_fetch_range", new_callable=AsyncMock),
        ):
            data = await coord._fetch_and_import()

        assert data.feed_in_rate_aud_per_kwh == pytest.approx(0.012)
        # Generation series empty → gated to None, not a partial number.
        assert data.generation_period_kwh is None
        assert data.generation_period_credit_aud is None

    async def test_feed_in_rate_none_without_plan_rate(
        self, hass: HomeAssistant
    ) -> None:
        mock_client = AsyncMock()
        mock_client.async_get_usage_summary.return_value = _empty_summary()
        mock_client.async_get_plan.return_value = _empty_plan()
        coord = _make_coordinator(hass, client=mock_client)

        with (
            patch.object(
                coord,
                "_get_last_stat",
                new_callable=AsyncMock,
                return_value=(None, None),
            ),
            patch.object(coord, "_fetch_range", new_callable=AsyncMock),
        ):
            data = await coord._fetch_and_import()

        assert data.feed_in_rate_aud_per_kwh is None


# ---------------------------------------------------------------------------
# Leading-hole heal (#128 follow-up) — a generation series seeded only from the
# trailing rewindow (beta.1 upgrade artifact) is re-imported from the floor.
# ---------------------------------------------------------------------------


def _ts(d: date) -> float:
    """UTC-midnight unix timestamp for a date, matching the recorder's `start`."""
    return datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp()


class TestGenerationHeal:
    async def test_leading_hole_detected(self, hass: HomeAssistant) -> None:
        """Earliest stored row well after the floor → heal needed."""
        coord = _make_coordinator(hass)
        stat_id_gen = coord._generation_stat_ids()[0]
        today = datetime.now(UTC).date()
        floor = today - timedelta(days=BACKFILL_DAYS)
        # Rows only from the last 5 days: seeded from the trailing rewindow.
        rows = {
            stat_id_gen: [
                {"start": _ts(today - timedelta(days=n)), "sum": float(10 - n)}
                for n in range(5, 0, -1)
            ]
        }
        with patch(_PATCH_GET_INSTANCE, _mock_get_instance(rows)):
            assert await coord._generation_needs_heal(stat_id_gen, floor, today) is True

    async def test_contiguous_from_floor_no_heal(self, hass: HomeAssistant) -> None:
        """First row at the floor with a monotonic sum → no heal."""
        coord = _make_coordinator(hass)
        stat_id_gen = coord._generation_stat_ids()[0]
        today = datetime.now(UTC).date()
        floor = today - timedelta(days=BACKFILL_DAYS)
        rows = {
            stat_id_gen: [
                {"start": _ts(floor + timedelta(days=n)), "sum": float(n)}
                for n in range(BACKFILL_DAYS)
            ]
        }
        with patch(_PATCH_GET_INSTANCE, _mock_get_instance(rows)):
            assert (
                await coord._generation_needs_heal(stat_id_gen, floor, today) is False
            )

    async def test_downward_sum_step_detected(self, hass: HomeAssistant) -> None:
        """A downward step (interrupted heal) re-triggers even when rows reach
        the floor — this is what makes the uncapped re-import 429-safe."""
        coord = _make_coordinator(hass)
        stat_id_gen = coord._generation_stat_ids()[0]
        today = datetime.now(UTC).date()
        floor = today - timedelta(days=BACKFILL_DAYS)
        rows = {
            stat_id_gen: [
                {"start": _ts(floor), "sum": 5.0},
                {"start": _ts(floor + timedelta(days=1)), "sum": 27.0},
                {"start": _ts(floor + timedelta(days=2)), "sum": 3.0},  # step down
            ]
        }
        with patch(_PATCH_GET_INSTANCE, _mock_get_instance(rows)):
            assert await coord._generation_needs_heal(stat_id_gen, floor, today) is True

    async def test_credit_only_step_also_triggers(self, hass: HomeAssistant) -> None:
        """Codex on #157: the AUD credit chain accumulates independently — a
        step in credit alone (kWh monotonic) must still arm the repair."""
        coord = _make_coordinator(hass)
        stat_id_gen, stat_id_credit = coord._generation_stat_ids()
        today = datetime.now(UTC).date()
        floor = today - timedelta(days=BACKFILL_DAYS)
        rows = {
            stat_id_gen: [
                {"start": _ts(floor), "sum": 5.0},
                {"start": _ts(floor + timedelta(days=1)), "sum": 6.0},  # monotonic
            ],
            stat_id_credit: [
                {"start": _ts(floor), "sum": 2.0},
                {"start": _ts(floor + timedelta(days=1)), "sum": 0.5},  # step down
            ],
        }
        with patch(_PATCH_GET_INSTANCE, _mock_get_instance(rows)):
            leading, broken = await coord._generation_heal_triggers(
                stat_id_gen, floor, today
            )
        assert broken is True
        assert leading is False

    async def test_empty_series_no_heal(self, hass: HomeAssistant) -> None:
        """No rows (fresh install) → normal backfill handles it, not a heal."""
        coord = _make_coordinator(hass)
        stat_id_gen = coord._generation_stat_ids()[0]
        today = datetime.now(UTC).date()
        floor = today - timedelta(days=BACKFILL_DAYS)
        with patch(_PATCH_GET_INSTANCE, _mock_get_instance({})):
            assert (
                await coord._generation_needs_heal(stat_id_gen, floor, today) is False
            )

    async def test_leading_hole_triggers_full_window_reimport(
        self, hass: HomeAssistant
    ) -> None:
        """A needed heal makes solar_range span the FULL floor..yesterday window
        (uncapped by the 7-day chunk) so the whole chain is rewritten in one
        contiguous batch — a partial fill would step the sum down (#114 class)."""
        mock_client = AsyncMock()
        mock_client.async_get_usage_summary.return_value = _empty_summary()
        mock_client.async_get_plan.return_value = _empty_plan()
        mock_client.async_get_overview.return_value = [_solar_contract()]
        coord = _make_coordinator(hass, client=mock_client)

        today = datetime.now(UTC).date()
        yesterday = today - timedelta(days=1)
        backfill_floor = today - timedelta(days=BACKFILL_DAYS)
        stat_id_cons = f"{DOMAIN}:{STAT_CONSUMPTION}_{_CONTRACT}"

        async def _last_stat(stat_id: str) -> tuple[float | None, date | None]:
            if stat_id == stat_id_cons:
                return (500.0, yesterday)  # consumption caught up
            return (12.3, today - timedelta(days=8))  # generation seeded recent

        with (
            patch.object(coord, "_get_last_stat", side_effect=_last_stat),
            patch.object(
                coord,
                "_generation_needs_heal",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch.object(
                coord,
                "_get_generation_period_totals",
                new_callable=AsyncMock,
                return_value=(None, None),
            ),
            patch.object(coord, "_fetch_range", new_callable=AsyncMock) as mock_range,
        ):
            await coord._fetch_and_import()

        solar_range = mock_range.call_args[0][1]
        assert solar_range == (backfill_floor, yesterday)

    async def test_healthy_series_uses_normal_chunked_range(
        self, hass: HomeAssistant
    ) -> None:
        """No heal needed → solar_range stays the normal trailing-rewindow chunk."""
        mock_client = AsyncMock()
        mock_client.async_get_usage_summary.return_value = _empty_summary()
        mock_client.async_get_plan.return_value = _empty_plan()
        mock_client.async_get_overview.return_value = [_solar_contract()]
        coord = _make_coordinator(hass, client=mock_client)

        today = datetime.now(UTC).date()
        yesterday = today - timedelta(days=1)

        with (
            patch.object(
                coord,
                "_get_last_stat",
                new_callable=AsyncMock,
                return_value=(12.3, yesterday),
            ),
            patch.object(
                coord,
                "_generation_needs_heal",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch.object(
                coord,
                "_get_generation_period_totals",
                new_callable=AsyncMock,
                return_value=(None, None),
            ),
            patch.object(coord, "_fetch_range", new_callable=AsyncMock) as mock_range,
        ):
            await coord._fetch_and_import()

        solar_range = mock_range.call_args[0][1]
        assert solar_range == (today - timedelta(days=REWINDOW_DAYS), yesterday)


class TestGenerationHealState:
    """The persisted heal state machine (Codex P1/P2/P3 on #150).

    Completion is tracked in entry.data, not inferred from the sum chain: an
    interrupted heal resumes until it finishes; a completed one never re-arms.
    """

    @staticmethod
    def _solar_client() -> AsyncMock:
        client = AsyncMock()
        client.async_get_usage_summary.return_value = _empty_summary()
        client.async_get_plan.return_value = _empty_plan()
        client.async_get_overview.return_value = [_solar_contract()]
        return client

    async def _run(
        self,
        hass: HomeAssistant,
        *,
        preset: dict | None = None,
        needs_heal: bool = True,
        fetch_complete: bool = True,
        drained: bool = False,
        chain_broken: bool = False,
    ) -> tuple[HaggleCoordinator, MagicMock, MagicMock, object]:
        coord = _make_coordinator(hass, client=self._solar_client())
        if preset is not None:
            hass.config_entries.async_update_entry(
                coord.config_entry,
                data={**coord.config_entry.data, CONF_SOLAR_HEAL: preset},
            )
        today = datetime.now(UTC).date()
        yesterday = today - timedelta(days=1)
        stat_id_cons = f"{DOMAIN}:{STAT_CONSUMPTION}_{_CONTRACT}"

        async def _last_stat(stat_id: str) -> tuple[float | None, date | None]:
            if stat_id == stat_id_cons:
                return (500.0, yesterday)  # consumption caught up
            return (12.3, today - timedelta(days=8))  # generation seeded recent

        with (
            patch.object(coord, "_get_last_stat", side_effect=_last_stat),
            patch.object(
                coord,
                "_generation_needs_heal",
                new_callable=AsyncMock,
                return_value=needs_heal,
            ) as mock_needs,
            patch.object(
                coord,
                "_generation_heal_triggers",
                new_callable=AsyncMock,
                return_value=(False, chain_broken),
            ),
            patch.object(
                coord,
                "_get_generation_period_totals",
                new_callable=AsyncMock,
                return_value=(4.0, 1.0),
            ),
            patch.object(
                coord,
                "_recorder_drained",
                new_callable=AsyncMock,
                return_value=drained,
            ),
            patch.object(
                coord,
                "_fetch_range",
                new_callable=AsyncMock,
                return_value=fetch_complete,
            ) as mock_range,
        ):
            data = await coord._fetch_and_import()
        return coord, mock_range, mock_needs, data

    @staticmethod
    def _heal(coord: HaggleCoordinator) -> dict:
        return coord.config_entry.data.get(CONF_SOLAR_HEAL) or {}

    async def test_unexpected_raise_mid_heal_still_bumps_attempts(
        self, hass: HomeAssistant
    ) -> None:
        """Belt-and-braces (#151): even a non-AGLError escaping _fetch_range
        must count a heal attempt — otherwise a deterministic raise on an old
        heal-window day re-fires an unbounded, uncounted 30-day sweep every
        cycle with the whole integration unavailable."""
        coord = _make_coordinator(hass, client=self._solar_client())
        today = datetime.now(UTC).date()
        yesterday = today - timedelta(days=1)
        stat_id_cons = f"{DOMAIN}:{STAT_CONSUMPTION}_{_CONTRACT}"

        async def _last_stat(stat_id: str) -> tuple[float | None, date | None]:
            if stat_id == stat_id_cons:
                return (500.0, yesterday)
            return (12.3, today - timedelta(days=8))

        with (
            patch.object(coord, "_get_last_stat", side_effect=_last_stat),
            patch.object(
                coord,
                "_generation_needs_heal",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch.object(
                coord,
                "_fetch_range",
                new_callable=AsyncMock,
                side_effect=RuntimeError("unwrapped transport bug"),
            ),
            pytest.raises(RuntimeError),
        ):
            await coord._fetch_and_import()

        heal = self._heal(coord)
        assert heal["state"] == SOLAR_HEAL_PENDING
        assert heal["attempts"] == 1  # the crashed sweep still counted

    async def test_pending_persisted_before_fetch(self, hass: HomeAssistant) -> None:
        """The frozen-floor pending record is written BEFORE the long fetch, so an
        HA restart mid-heal resumes the same window (Codex P2, pass 3). Captured
        by reading entry.data at the moment _fetch_range is entered."""
        coord = _make_coordinator(hass, client=self._solar_client())
        today = datetime.now(UTC).date()
        yesterday = today - timedelta(days=1)
        stat_id_cons = f"{DOMAIN}:{STAT_CONSUMPTION}_{_CONTRACT}"
        seen: dict[str, object] = {}

        async def _capture_fetch(*_a: object, **_k: object) -> bool:
            seen["heal"] = coord.config_entry.data.get(CONF_SOLAR_HEAL)
            return True

        async def _last_stat(stat_id: str) -> tuple[float | None, date | None]:
            if stat_id == stat_id_cons:
                return (500.0, yesterday)
            return (12.3, today - timedelta(days=8))

        with (
            patch.object(coord, "_get_last_stat", side_effect=_last_stat),
            patch.object(
                coord,
                "_generation_needs_heal",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch.object(
                coord,
                "_get_generation_period_totals",
                new_callable=AsyncMock,
                return_value=(None, None),
            ),
            patch.object(coord, "_fetch_range", side_effect=_capture_fetch),
        ):
            await coord._fetch_and_import()

        assert seen["heal"] == {
            "state": SOLAR_HEAL_PENDING,
            "floor": (today - timedelta(days=BACKFILL_DAYS)).isoformat(),
            "attempts": 0,
        }

    async def test_completed_heal_marks_done(self, hass: HomeAssistant) -> None:
        today = datetime.now(UTC).date()
        coord, mock_range, _, _ = await self._run(hass, fetch_complete=True)
        assert mock_range.call_args[0][1] == (
            today - timedelta(days=BACKFILL_DAYS),
            today - timedelta(days=1),
        )
        assert self._heal(coord)["state"] == SOLAR_HEAL_DONE

    async def test_interrupted_heal_stays_pending(self, hass: HomeAssistant) -> None:
        """A 429/skip mid-heal (_fetch_range → False) keeps the heal pending and
        bumps the attempt count, freezing the floor for the retry (Codex P1/P2)."""
        today = datetime.now(UTC).date()
        coord, _, _, _ = await self._run(hass, fetch_complete=False)
        heal = self._heal(coord)
        assert heal["state"] == SOLAR_HEAL_PENDING
        assert heal["attempts"] == 1
        assert heal["floor"] == (today - timedelta(days=BACKFILL_DAYS)).isoformat()

    async def test_pending_resumes_from_frozen_floor(self, hass: HomeAssistant) -> None:
        """PENDING resumes the full-window heal from the FROZEN floor regardless
        of the detector (Codex P1/P2): the retry re-fetches the same window, not a
        today-recomputed one that would slide off the oldest day."""
        today = datetime.now(UTC).date()
        frozen_floor = today - timedelta(days=25)  # deliberately != today-30
        coord, mock_range, mock_needs, _ = await self._run(
            hass,
            preset={
                "state": SOLAR_HEAL_PENDING,
                "floor": frozen_floor.isoformat(),
                "attempts": 1,
            },
            needs_heal=False,
            fetch_complete=True,
        )
        mock_needs.assert_not_awaited()  # short-circuits on the pending state
        assert mock_range.call_args[0][1] == (frozen_floor, today - timedelta(days=1))
        assert self._heal(coord)["state"] == SOLAR_HEAL_DONE

    async def test_frozen_floor_preserved_across_retry(
        self, hass: HomeAssistant
    ) -> None:
        """An interrupted retry keeps the same frozen floor (not today-recomputed)
        and increments attempts."""
        today = datetime.now(UTC).date()
        frozen_floor = today - timedelta(days=25)
        coord, _, _, _ = await self._run(
            hass,
            preset={
                "state": SOLAR_HEAL_PENDING,
                "floor": frozen_floor.isoformat(),
                "attempts": 1,
            },
            fetch_complete=False,
        )
        heal = self._heal(coord)
        assert heal["state"] == SOLAR_HEAL_PENDING
        assert heal["floor"] == frozen_floor.isoformat()  # unchanged
        assert heal["attempts"] == 2

    async def test_gives_up_after_max_attempts(self, hass: HomeAssistant) -> None:
        """A permanently-erroring day can't wedge the heal: after
        MAX_SOLAR_HEAL_ATTEMPTS incomplete sweeps it gives up to DONE (Codex P3)."""
        today = datetime.now(UTC).date()
        frozen_floor = today - timedelta(days=25)
        coord, _, _, _ = await self._run(
            hass,
            preset={
                "state": SOLAR_HEAL_PENDING,
                "floor": frozen_floor.isoformat(),
                "attempts": MAX_SOLAR_HEAL_ATTEMPTS - 1,
            },
            fetch_complete=False,
        )
        heal = self._heal(coord)
        assert heal["state"] == SOLAR_HEAL_DONE
        assert heal["gave_up"] is True
        assert heal["attempts"] == MAX_SOLAR_HEAL_ATTEMPTS
        from homeassistant.helpers import issue_registry as ir

        registry = ir.async_get(hass)
        assert (
            registry.async_get_issue(DOMAIN, f"solar_heal_gave_up_{_CONTRACT}")
            is not None
        )

    async def test_clean_completion_records_no_give_up(
        self, hass: HomeAssistant
    ) -> None:
        """A clean sweep records plain done — no give-up marker, no Repairs
        issue (the two previously collapsed to the same record; CO-16.4)."""
        today = datetime.now(UTC).date()
        coord, _, _, _ = await self._run(
            hass,
            preset={
                "state": SOLAR_HEAL_PENDING,
                "floor": (today - timedelta(days=25)).isoformat(),
                "attempts": 1,
            },
            fetch_complete=True,
        )
        assert self._heal(coord) == {"state": SOLAR_HEAL_DONE}
        from homeassistant.helpers import issue_registry as ir

        registry = ir.async_get(hass)
        assert not [i for i in registry.issues.values() if i.domain == DOMAIN]

    async def test_done_never_rearms(self, hass: HomeAssistant) -> None:
        """Once DONE, a still-present leading gap (needs_heal True) is NOT
        re-swept — the P3 fix against an unfetchable permanent leading gap
        re-bursting a 30-day fetch every poll."""
        today = datetime.now(UTC).date()
        coord, mock_range, _, _ = await self._run(
            hass, preset={"state": SOLAR_HEAL_DONE}, needs_heal=True
        )
        # Not the heal window: a normal trailing chunk, never starting at floor.
        assert mock_range.call_args[0][1][0] != today - timedelta(days=BACKFILL_DAYS)
        assert self._heal(coord)["state"] == SOLAR_HEAL_DONE

    async def test_chain_break_after_done_arms_bounded_repair(
        self, hass: HomeAssistant
    ) -> None:
        """#153: a downward sum step frozen by a 429 on the give-up sweep
        re-arms ONE repair generation — full window, fresh attempt budget,
        marked `repair` so it can never re-arm again."""
        today = datetime.now(UTC).date()
        coord, mock_range, _, _ = await self._run(
            hass,
            preset={"state": SOLAR_HEAL_DONE},
            chain_broken=True,
            fetch_complete=True,
        )
        assert mock_range.call_args[0][1] == (
            today - timedelta(days=BACKFILL_DAYS),
            today - timedelta(days=1),
        )
        heal = self._heal(coord)
        assert heal["state"] == SOLAR_HEAL_DONE  # repair completed this cycle
        assert heal["repair"] is True  # mark survives → no second generation

    async def test_repair_mark_blocks_second_rearm(self, hass: HomeAssistant) -> None:
        """A record already marked `repair` never re-arms, even with the chain
        still broken — lifetime sweeps hard-capped at 2x MAX (#153)."""
        today = datetime.now(UTC).date()
        coord, mock_range, _, _ = await self._run(
            hass,
            preset={"state": SOLAR_HEAL_DONE, "repair": True},
            chain_broken=True,
        )
        assert mock_range.call_args[0][1][0] != today - timedelta(days=BACKFILL_DAYS)
        assert self._heal(coord) == {"state": SOLAR_HEAL_DONE, "repair": True}

    async def test_normal_cycle_tracks_stall_heal_cycle_does_not(
        self, hass: HomeAssistant
    ) -> None:
        """#154 wiring: _fetch_range gets track_stall=True only on non-heal
        cycles — heal sweeps have their own attempt accounting. The _run
        harness leaves the generation series 8 days behind, so the normal
        chunk starts strictly beyond the last stored day."""
        _, mock_range, _, _ = await self._run(hass, needs_heal=True)
        assert mock_range.call_args.kwargs["track_stall"] is False
        _, mock_range, _, _ = await self._run(
            hass, preset={"state": SOLAR_HEAL_DONE}, needs_heal=False
        )
        assert mock_range.call_args.kwargs["track_stall"] is True

    async def test_rewindow_overlap_never_tracks_stall(
        self, hass: HomeAssistant
    ) -> None:
        """Codex on #157: a caught-up series' chunk IS the trailing rewindow,
        overlapping real rows — give-up markers there would corrupt the
        cumulative chain, so tracking must stay off."""
        coord = _make_coordinator(hass, client=self._solar_client())
        today = datetime.now(UTC).date()
        yesterday = today - timedelta(days=1)

        with (
            patch.object(
                coord,
                "_get_last_stat",
                new_callable=AsyncMock,
                return_value=(500.0, yesterday),  # both series caught up
            ),
            patch.object(
                coord,
                "_generation_needs_heal",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch.object(
                coord,
                "_generation_heal_triggers",
                new_callable=AsyncMock,
                return_value=(False, False),
            ),
            patch.object(
                coord,
                "_get_generation_period_totals",
                new_callable=AsyncMock,
                return_value=(None, None),
            ),
            patch.object(
                coord, "_fetch_range", new_callable=AsyncMock, return_value=True
            ) as mock_range,
        ):
            await coord._fetch_and_import()

        # Chunk starts inside the rewindow (start <= last stored day).
        assert mock_range.call_args.kwargs["track_stall"] is False

    async def test_cancelled_mid_heal_does_not_bump_attempts(
        self, hass: HomeAssistant
    ) -> None:
        """Codex on #157: HA unload/restart cancels the update task — that is
        not an AGL failure and must not consume heal budget; the frozen floor
        already makes restarts resume-safe."""
        coord = _make_coordinator(hass, client=self._solar_client())
        today = datetime.now(UTC).date()
        yesterday = today - timedelta(days=1)
        stat_id_cons = f"{DOMAIN}:{STAT_CONSUMPTION}_{_CONTRACT}"
        preset = {
            "state": SOLAR_HEAL_PENDING,
            "floor": (today - timedelta(days=25)).isoformat(),
            "attempts": 1,
        }
        hass.config_entries.async_update_entry(
            coord.config_entry,
            data={**coord.config_entry.data, CONF_SOLAR_HEAL: preset},
        )

        async def _last_stat(stat_id: str) -> tuple[float | None, date | None]:
            if stat_id == stat_id_cons:
                return (500.0, yesterday)
            return (12.3, today - timedelta(days=8))

        with (
            patch.object(coord, "_get_last_stat", side_effect=_last_stat),
            patch.object(
                coord,
                "_fetch_range",
                new_callable=AsyncMock,
                side_effect=asyncio.CancelledError(),
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            await coord._fetch_and_import()

        # Record untouched: same attempts, still pending, same frozen floor.
        assert self._heal(coord) == preset

    async def test_repair_giveup_keeps_mark(self, hass: HomeAssistant) -> None:
        """A repair generation that exhausts its budget goes done WITH the
        mark, permanently blocking further re-arms."""
        today = datetime.now(UTC).date()
        coord, _, _, _ = await self._run(
            hass,
            preset={
                "state": SOLAR_HEAL_PENDING,
                "floor": (today - timedelta(days=25)).isoformat(),
                "attempts": MAX_SOLAR_HEAL_ATTEMPTS - 1,
                "repair": True,
            },
            fetch_complete=False,
        )
        assert self._heal(coord) == {
            "state": SOLAR_HEAL_DONE,
            "repair": True,
            "gave_up": True,
            "attempts": MAX_SOLAR_HEAL_ATTEMPTS,
        }
        from homeassistant.helpers import issue_registry as ir

        registry = ir.async_get(hass)
        assert (
            registry.async_get_issue(DOMAIN, f"solar_repair_gave_up_{_CONTRACT}")
            is not None
        )

    async def test_period_totals_published_after_complete_drained_heal(
        self, hass: HomeAssistant
    ) -> None:
        """#152: a COMPLETE heal sweep drains the recorder queue and then
        publishes through the proven totals path — the sensors show the healed
        number the same cycle the heal finishes, no blank day."""
        _, _, _, data = await self._run(
            hass, needs_heal=True, fetch_complete=True, drained=True
        )
        assert data.generation_period_kwh == 4.0
        assert data.generation_period_credit_aud == 1.0

    async def test_period_totals_suppressed_on_drain_timeout(
        self, hass: HomeAssistant
    ) -> None:
        """Drain timeout → keep the suppression (a wrong number is worse than
        a blank one); the next normal cycle publishes."""
        _, _, _, data = await self._run(
            hass, needs_heal=True, fetch_complete=True, drained=False
        )
        assert data.generation_period_kwh is None
        assert data.generation_period_credit_aud is None

    async def test_period_totals_suppressed_on_incomplete_heal_sweep(
        self, hass: HomeAssistant
    ) -> None:
        """An incomplete sweep (429/skipped day) must NOT publish even if the
        recorder would drain — the partial chain undercounts (Codex P2/RT4
        condition on #152)."""
        _, _, _, data = await self._run(
            hass, needs_heal=True, fetch_complete=False, drained=True
        )
        assert data.generation_period_kwh is None
        assert data.generation_period_credit_aud is None

    async def test_period_totals_reported_when_not_healing(
        self, hass: HomeAssistant
    ) -> None:
        _, _, _, data = await self._run(
            hass, preset={"state": SOLAR_HEAL_DONE}, needs_heal=True
        )
        assert data.generation_period_kwh == 4.0
        assert data.generation_period_credit_aud == 1.0

    async def test_fetch_range_incomplete_when_solar_day_skipped(
        self, hass: HomeAssistant
    ) -> None:
        """A skipped solar day (transient AGL error → None) makes _fetch_range
        report incomplete, so a heal retries it instead of declaring DONE with a
        hole (Codex P1)."""
        coord = _make_coordinator(hass)
        day = datetime.now(UTC).date() - timedelta(days=10)
        with (
            patch.object(
                coord, "_fetch_day_solar", new_callable=AsyncMock, return_value=None
            ),
            patch.object(coord, "_import_generation", new_callable=AsyncMock),
        ):
            complete = await coord._fetch_range(None, (day, day), None)
        assert complete is False

    async def test_fetch_range_complete_when_solar_day_ok(
        self, hass: HomeAssistant
    ) -> None:
        """A cleanly-fetched (even empty) solar day reports complete."""
        coord = _make_coordinator(hass)
        day = datetime.now(UTC).date() - timedelta(days=10)
        with (
            patch.object(
                coord, "_fetch_day_solar", new_callable=AsyncMock, return_value=[]
            ),
            patch.object(coord, "_import_generation", new_callable=AsyncMock),
        ):
            complete = await coord._fetch_range(None, (day, day), None)
        assert complete is True


class TestSolarStallGiveUp:
    """#154 — the normal-path backfill give-up counter.

    A contiguous span of permanently-erroring days at resume+1.. refetched the
    identical chunk forever; after SOLAR_STALL_GIVE_UP_CYCLES zero-progress
    cycles on the SAME chunk, marker rows advance the resume point past it.
    """

    @staticmethod
    def _chunk() -> tuple[date, date]:
        start = datetime.now(UTC).date() - timedelta(days=20)
        return (start, start + timedelta(days=BACKFILL_CHUNK_DAYS - 1))

    async def test_gives_up_after_n_zero_progress_cycles(
        self, hass: HomeAssistant
    ) -> None:
        coord = _make_coordinator(hass)
        chunk = self._chunk()
        with patch.object(
            coord, "_import_generation", new_callable=AsyncMock
        ) as mock_import:
            for _ in range(SOLAR_STALL_GIVE_UP_CYCLES - 1):
                await coord._track_solar_stall(chunk, progressed=False, skipped=True)
            mock_import.assert_not_awaited()  # still retrying
            await coord._track_solar_stall(chunk, progressed=False, skipped=True)

        mock_import.assert_awaited_once()
        marked = mock_import.await_args.kwargs["fetched_days"]
        assert marked[0] == chunk[0]
        assert marked[-1] == chunk[1]
        assert len(marked) == BACKFILL_CHUNK_DAYS
        assert coord._solar_stall is None  # counter reset after give-up
        spans = coord.config_entry.data[CONF_SOLAR_STALL_SPANS]
        assert len(spans) == 1
        assert spans[0]["start"] == chunk[0].isoformat()
        assert spans[0]["end"] == chunk[1].isoformat()
        assert spans[0]["cycles"] == SOLAR_STALL_GIVE_UP_CYCLES
        from homeassistant.helpers import issue_registry as ir

        registry = ir.async_get(hass)
        assert (
            registry.async_get_issue(
                DOMAIN,
                f"solar_stall_gave_up_{_CONTRACT}_{chunk[0].isoformat()}",
            )
            is not None
        )

    async def test_stall_span_list_is_bounded(self, hass: HomeAssistant) -> None:
        """The persisted span list drops the oldest beyond
        MAX_STALL_SPAN_RECORDS so entry.data stays small."""
        coord = _make_coordinator(hass)
        dummies = [
            {
                "start": f"2026-01-{d:02d}",
                "end": f"2026-01-{d:02d}",
                "cycles": 3,
                "gave_up_at": "2026-01-01T00:00:00+00:00",
            }
            for d in range(1, MAX_STALL_SPAN_RECORDS + 1)
        ]
        hass.config_entries.async_update_entry(
            coord.config_entry,
            data={**coord.config_entry.data, CONF_SOLAR_STALL_SPANS: dummies},
        )
        chunk = self._chunk()
        with patch.object(coord, "_import_generation", new_callable=AsyncMock):
            for _ in range(SOLAR_STALL_GIVE_UP_CYCLES):
                await coord._track_solar_stall(chunk, progressed=False, skipped=True)
        spans = coord.config_entry.data[CONF_SOLAR_STALL_SPANS]
        assert len(spans) == MAX_STALL_SPAN_RECORDS
        assert spans[-1]["start"] == chunk[0].isoformat()  # newest last

    async def test_progress_resets_counter(self, hass: HomeAssistant) -> None:
        """Any successful day (even alongside skips) resets the count — the
        chunk is moving, not stalled."""
        coord = _make_coordinator(hass)
        chunk = self._chunk()
        with patch.object(
            coord, "_import_generation", new_callable=AsyncMock
        ) as mock_import:
            for _ in range(SOLAR_STALL_GIVE_UP_CYCLES - 1):
                await coord._track_solar_stall(chunk, progressed=False, skipped=True)
            await coord._track_solar_stall(chunk, progressed=True, skipped=True)
            for _ in range(SOLAR_STALL_GIVE_UP_CYCLES - 1):
                await coord._track_solar_stall(chunk, progressed=False, skipped=True)
        mock_import.assert_not_awaited()

    async def test_different_chunk_restarts_counter(self, hass: HomeAssistant) -> None:
        """A moved resume point means the old span cleared — the count must
        not carry over to the new chunk."""
        coord = _make_coordinator(hass)
        a = self._chunk()
        b = (a[0] + timedelta(days=7), a[1] + timedelta(days=7))
        with patch.object(
            coord, "_import_generation", new_callable=AsyncMock
        ) as mock_import:
            for _ in range(SOLAR_STALL_GIVE_UP_CYCLES - 1):
                await coord._track_solar_stall(a, progressed=False, skipped=True)
            for _ in range(SOLAR_STALL_GIVE_UP_CYCLES - 1):
                await coord._track_solar_stall(b, progressed=False, skipped=True)
        mock_import.assert_not_awaited()

    async def test_rate_limited_sweep_does_not_count(self, hass: HomeAssistant) -> None:
        """A 429-halted sweep never reaches the tracker (days were not
        attempted) — verified at the _fetch_range call site."""
        from custom_components.haggle.agl.client import AGLRateLimitError

        coord = _make_coordinator(hass)
        day = datetime.now(UTC).date() - timedelta(days=10)
        with (
            patch.object(
                coord,
                "_fetch_day_solar",
                new_callable=AsyncMock,
                side_effect=AGLRateLimitError("429"),
            ),
            patch.object(
                coord, "_track_solar_stall", new_callable=AsyncMock
            ) as mock_track,
        ):
            await coord._fetch_range(None, (day, day), None, track_stall=True)
        mock_track.assert_not_awaited()

    async def test_clean_zero_export_sweep_resets(self, hass: HomeAssistant) -> None:
        """A sweep with no skips (all days fetched clean, even if zero export)
        resets the counter."""
        coord = _make_coordinator(hass)
        chunk = self._chunk()
        with patch.object(coord, "_import_generation", new_callable=AsyncMock):
            for _ in range(SOLAR_STALL_GIVE_UP_CYCLES - 1):
                await coord._track_solar_stall(chunk, progressed=False, skipped=True)
            assert coord._solar_stall is not None
            await coord._track_solar_stall(chunk, progressed=False, skipped=False)
        assert coord._solar_stall is None


class TestTransportHaltSemantics:
    """Codex on #157: transport failures HALT the chunk (retry next cycle);
    only per-day AGL HTTP errors keep the documented skip tradeoff. A skip on
    a transient network blip would advance the resume past the day forever.
    """

    async def test_consumption_transport_error_halts_chunk(
        self, hass: HomeAssistant
    ) -> None:
        from custom_components.haggle.agl.client import AGLTransportError

        coord = _make_coordinator(hass)
        coord.client.async_get_usage_hourly = AsyncMock(
            side_effect=AGLTransportError("transport error: ClientError")
        )
        day = datetime.now(UTC).date() - timedelta(days=2)
        assert await coord._fetch_day_consumption(day, previous=False) is None

    async def test_consumption_http_error_still_skips(
        self, hass: HomeAssistant
    ) -> None:
        from custom_components.haggle.agl.client import AGLError

        coord = _make_coordinator(hass)
        coord.client.async_get_usage_hourly = AsyncMock(
            side_effect=AGLError("HTTP 500 fetching AGL data")
        )
        day = datetime.now(UTC).date() - timedelta(days=2)
        assert await coord._fetch_day_consumption(day, previous=False) == []

    async def test_solar_transport_error_halts_not_skips(
        self, hass: HomeAssistant
    ) -> None:
        """Transport failure mid-solar-sweep halts the whole chunk (like a
        429) and leaves the day unmarked — no resume advance past it."""
        from custom_components.haggle.agl.client import AGLTransportError

        coord = _make_coordinator(hass)
        day = datetime.now(UTC).date() - timedelta(days=10)
        with (
            patch.object(
                coord,
                "_fetch_day_solar",
                new_callable=AsyncMock,
                side_effect=AGLTransportError("transport error: TimeoutError"),
            ),
            patch.object(
                coord, "_import_generation", new_callable=AsyncMock
            ) as mock_import,
        ):
            complete = await coord._fetch_range(None, (day, day), None)
        assert complete is False
        mock_import.assert_not_awaited()  # nothing marked, nothing advanced
