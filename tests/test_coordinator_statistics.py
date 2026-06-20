"""Tests for HaggleCoordinator statistics import logic.

Covers _import_intervals (aggregation, running sum, idempotency, none-filter),
_fetch_range (smart endpoint selection), and _fetch_and_import (chunked resume).

All recorder calls are patched at the boundary — async_add_external_statistics
and get_last_statistics — so no real SQLite DB is needed.
"""

from __future__ import annotations

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
    DOMAIN,
    REWINDOW_DAYS,
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
            await coord._fetch_range(start, end, bill_start)

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
            await coord._fetch_range(start, end, bill_start)

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
            await coord._fetch_range(start, end, None)

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
            await coord._fetch_range(start, end, None)  # must not raise

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
            await coord._fetch_range(start, end, None)

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
            await coord._fetch_range(start, end, None)

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
        actual_start = mock_range.call_args[0][0]
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

        assert mock_range.call_args[0][0] == expected_start
        # Throttle: fetch_end at most BACKFILL_CHUNK_DAYS - 1 past fetch_start.
        fetch_end = mock_range.call_args[0][1]
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
        assert mock_range.call_args[0][0] == expected_start

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

        assert mock_range.call_args[0][0] >= backfill_floor

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
        assert mock_range.call_args[0][0] == expected_start


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
