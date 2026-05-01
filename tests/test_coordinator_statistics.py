"""Tests for HaggleCoordinator statistics import logic.

Covers _import_intervals (aggregation, running sum, idempotency, none-filter),
_async_setup (30-day backfill), and _fetch_and_import (resume from last stat).

All recorder calls are patched at the boundary — async_add_external_statistics
and get_last_statistics — so no real SQLite DB is needed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.haggle.agl.models import IntervalReading
from custom_components.haggle.const import (
    CONF_ACCESS_TOKEN,
    CONF_ACCESS_TOKEN_EXPIRY,
    CONF_ACCOUNT_NUMBER,
    CONF_CONTRACT_NUMBER,
    CONF_REFRESH_TOKEN,
    DOMAIN,
    STAT_CONSUMPTION,
)
from custom_components.haggle.coordinator import HaggleCoordinator

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CONTRACT = "9999999999"

_ENTRY_DATA = {
    CONF_REFRESH_TOKEN: "v1.testtoken",
    CONF_ACCESS_TOKEN: "",
    CONF_ACCESS_TOKEN_EXPIRY: 0,
    CONF_CONTRACT_NUMBER: _CONTRACT,
    CONF_ACCOUNT_NUMBER: "1234567890",
}


def _make_interval(dt: datetime, kwh: float, cost: float = 0.05) -> IntervalReading:
    return IntervalReading(dt=dt, kwh=kwh, cost_aud=cost, rate_type="normal")


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

        with patch(_PATCH_ADD_STATS) as mock_add:
            await coord._import_intervals(
                intervals,
                initial_cons_sum=0.0,
                initial_cost_sum=0.0,
            )

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

        with patch(_PATCH_ADD_STATS) as mock_add:
            await coord._import_intervals(
                intervals,
                initial_cons_sum=0.0,
                initial_cost_sum=0.0,
            )

        cons_stats = mock_add.call_args_list[0][0][2]
        assert len(cons_stats) == 2

    async def test_running_sum_is_cumulative(self, hass: HomeAssistant) -> None:
        """Three consecutive hours → sum is strictly monotonically increasing."""
        coord = _make_coordinator(hass)
        intervals = [
            _make_interval(datetime(2026, 4, 28, h, 0, tzinfo=UTC), kwh=1.0)
            for h in range(3)
        ]

        with patch(_PATCH_ADD_STATS) as mock_add:
            await coord._import_intervals(
                intervals,
                initial_cons_sum=0.0,
                initial_cost_sum=0.0,
            )

        cons_stats = mock_add.call_args_list[0][0][2]
        assert len(cons_stats) == 3
        sums = [s["sum"] for s in cons_stats]
        assert sums == [pytest.approx(1.0), pytest.approx(2.0), pytest.approx(3.0)]

    async def test_initial_sum_offset_applied(self, hass: HomeAssistant) -> None:
        """initial_cons_sum is added as an offset to the running total."""
        coord = _make_coordinator(hass)
        intervals = [
            _make_interval(datetime(2026, 4, 28, 0, 0, tzinfo=UTC), kwh=0.5),
        ]

        with patch(_PATCH_ADD_STATS) as mock_add:
            await coord._import_intervals(
                intervals,
                initial_cons_sum=100.0,
                initial_cost_sum=0.0,
            )

        cons_stats = mock_add.call_args_list[0][0][2]
        assert cons_stats[0]["sum"] == pytest.approx(100.5)

    async def test_latest_cumulative_kwh_updated(self, hass: HomeAssistant) -> None:
        """_latest_cumulative_kwh is updated to the final cumulative sum."""
        coord = _make_coordinator(hass)
        intervals = [
            _make_interval(datetime(2026, 4, 28, h, 0, tzinfo=UTC), kwh=1.0)
            for h in range(3)
        ]

        with patch(_PATCH_ADD_STATS):
            await coord._import_intervals(
                intervals,
                initial_cons_sum=50.0,
                initial_cost_sum=0.0,
            )

        assert coord._latest_cumulative_kwh == pytest.approx(53.0)

    async def test_empty_intervals_calls_add_with_empty_lists(
        self, hass: HomeAssistant
    ) -> None:
        """No intervals → async_add_external_statistics is called with empty lists."""
        coord = _make_coordinator(hass)

        with patch(_PATCH_ADD_STATS) as mock_add:
            await coord._import_intervals(
                [],
                initial_cons_sum=0.0,
                initial_cost_sum=0.0,
            )

        for c in mock_add.call_args_list:
            assert c[0][2] == []


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

        with patch(_PATCH_ADD_STATS) as mock_add:
            await coord._import_intervals(
                intervals,
                initial_cons_sum=0.0,
                initial_cost_sum=0.0,
            )

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

        with patch(_PATCH_ADD_STATS) as mock_add:
            await coord._import_intervals(
                intervals,
                initial_cons_sum=0.0,
                initial_cost_sum=0.0,
            )

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

        with patch(_PATCH_ADD_STATS) as mock_add:
            await coord._import_intervals(
                intervals,
                initial_cons_sum=0.0,
                initial_cost_sum=0.0,
            )

        cons_stats = mock_add.call_args_list[0][0][2]
        assert len(cons_stats) == 1
        assert cons_stats[0]["state"] == pytest.approx(0.8)


# ---------------------------------------------------------------------------
# _async_setup — 30-day backfill
# ---------------------------------------------------------------------------


class TestAsyncSetupBackfill:
    async def test_calls_previous_hourly_for_backfill_days(
        self, hass: HomeAssistant
    ) -> None:
        """_async_setup must call async_get_usage_hourly_previous once per backfill day."""
        from custom_components.haggle.const import BACKFILL_DAYS

        mock_client = AsyncMock()
        mock_client.async_get_usage_hourly_previous.return_value = []
        coord = _make_coordinator(hass, client=mock_client)

        with patch.object(coord, "_import_intervals", new_callable=AsyncMock):
            await coord._async_setup()

        assert mock_client.async_get_usage_hourly_previous.call_count == BACKFILL_DAYS

    async def test_backfill_days_are_consecutive_ending_yesterday(
        self, hass: HomeAssistant
    ) -> None:
        """Days requested span yesterday through (today - BACKFILL_DAYS)."""
        from datetime import date

        from custom_components.haggle.const import BACKFILL_DAYS

        mock_client = AsyncMock()
        mock_client.async_get_usage_hourly_previous.return_value = []
        coord = _make_coordinator(hass, client=mock_client)

        with patch.object(coord, "_import_intervals", new_callable=AsyncMock):
            await coord._async_setup()

        today = date.today()
        called_days = {
            c.args[1]
            for c in mock_client.async_get_usage_hourly_previous.call_args_list
        }
        expected_days = {today - timedelta(days=i) for i in range(1, BACKFILL_DAYS + 1)}
        assert called_days == expected_days

    async def test_backfill_aggregates_and_imports(self, hass: HomeAssistant) -> None:
        """_async_setup passes all gathered readings to _import_intervals."""
        mock_client = AsyncMock()
        reading = _make_interval(datetime(2026, 4, 28, 0, 0, tzinfo=UTC), kwh=0.5)
        mock_client.async_get_usage_hourly_previous.return_value = [reading]
        coord = _make_coordinator(hass, client=mock_client)

        with patch.object(
            coord, "_import_intervals", new_callable=AsyncMock
        ) as mock_import:
            await coord._async_setup()

        mock_import.assert_called_once()
        imported = mock_import.call_args[0][0]
        # 30 calls each returning 1 reading → 30 readings total
        assert len(imported) == 30

    async def test_backfill_skips_agl_error(self, hass: HomeAssistant) -> None:
        """A failed day is skipped; remaining days are still fetched."""
        from custom_components.haggle.agl.client import AGLError

        mock_client = AsyncMock()
        mock_client.async_get_usage_hourly_previous.side_effect = AGLError("timeout")
        coord = _make_coordinator(hass, client=mock_client)

        with patch.object(
            coord, "_import_intervals", new_callable=AsyncMock
        ) as mock_import:
            await coord._async_setup()  # Must not raise

        # Error means no intervals collected → _import_intervals not called
        mock_import.assert_not_called()


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
# _fetch_and_import — resume from last stat
# ---------------------------------------------------------------------------


class TestFetchAndImportResume:
    async def test_no_previous_stats_triggers_backfill(
        self, hass: HomeAssistant
    ) -> None:
        """When _get_last_stat_sum returns None, _async_setup is called."""
        mock_client = AsyncMock()
        mock_client.async_get_usage_summary.side_effect = NotImplementedError
        mock_client.async_get_plan.side_effect = NotImplementedError
        coord = _make_coordinator(hass, client=mock_client)

        with (
            patch.object(
                coord,
                "_get_last_stat_sum",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch.object(coord, "_async_setup", new_callable=AsyncMock) as mock_setup,
        ):
            await coord._fetch_and_import()

        mock_setup.assert_called_once()

    async def test_existing_stats_triggers_fetch_missing_days(
        self, hass: HomeAssistant
    ) -> None:
        """When stats exist, _fetch_missing_days is called with the last sum."""
        mock_client = AsyncMock()
        mock_client.async_get_usage_summary.side_effect = NotImplementedError
        mock_client.async_get_plan.side_effect = NotImplementedError
        coord = _make_coordinator(hass, client=mock_client)

        with (
            patch.object(
                coord,
                "_get_last_stat_sum",
                new_callable=AsyncMock,
                return_value=259.0,
            ),
            patch.object(
                coord, "_fetch_missing_days", new_callable=AsyncMock
            ) as mock_fetch,
        ):
            await coord._fetch_and_import()

        mock_fetch.assert_called_once_with(259.0)

    async def test_returns_haggle_data_instance(self, hass: HomeAssistant) -> None:
        from custom_components.haggle.coordinator import HaggleData

        mock_client = AsyncMock()
        mock_client.async_get_usage_summary.side_effect = NotImplementedError
        mock_client.async_get_plan.side_effect = NotImplementedError
        coord = _make_coordinator(hass, client=mock_client)

        with (
            patch.object(
                coord,
                "_get_last_stat_sum",
                new_callable=AsyncMock,
                return_value=259.0,
            ),
            patch.object(coord, "_fetch_missing_days", new_callable=AsyncMock),
        ):
            result = await coord._fetch_and_import()

        assert isinstance(result, HaggleData)

    async def test_plan_unit_rate_extracted(self, hass: HomeAssistant) -> None:
        from custom_components.haggle.agl.models import PlanRates

        mock_client = AsyncMock()
        mock_client.async_get_usage_summary.side_effect = NotImplementedError
        mock_client.async_get_plan.return_value = PlanRates(
            product_name="Smart Saver",
            unit_rates=[
                {"kind": "detail", "type": "c/kWh", "price": 33.792, "title": "Usage"}
            ],
            supply_charge_cents_per_day=131.714,
        )
        coord = _make_coordinator(hass, client=mock_client)

        with (
            patch.object(
                coord,
                "_get_last_stat_sum",
                new_callable=AsyncMock,
                return_value=100.0,
            ),
            patch.object(coord, "_fetch_missing_days", new_callable=AsyncMock),
        ):
            result = await coord._fetch_and_import()

        assert result.unit_rate_aud_per_kwh == pytest.approx(33.792 / 100.0)
        assert result.supply_charge_aud_per_day == pytest.approx(131.714 / 100.0)


# ---------------------------------------------------------------------------
# _fetch_missing_days — correct day range requested
# ---------------------------------------------------------------------------


class TestFetchMissingDays:
    async def test_fetches_days_from_gap_to_yesterday(
        self, hass: HomeAssistant
    ) -> None:
        """If last stat was 3 days ago, fetch day-2 and day-1 (yesterday)."""
        from datetime import date

        mock_client = AsyncMock()
        mock_client.async_get_usage_hourly.return_value = []
        coord = _make_coordinator(hass, client=mock_client)

        today = date.today()
        yesterday = today - timedelta(days=1)
        last_stat_day = today - timedelta(days=3)  # 3 days ago

        last_stats_raw = {
            f"{DOMAIN}:{STAT_CONSUMPTION}_{_CONTRACT}": [
                {
                    "start": float(
                        datetime.combine(last_stat_day, datetime.min.time())
                        .replace(tzinfo=UTC)
                        .timestamp()
                    ),
                    "sum": 50.0,
                }
            ]
        }

        with (
            patch(_PATCH_GET_LAST, return_value=last_stats_raw),
            patch(_PATCH_GET_INSTANCE, _mock_get_instance(last_stats_raw)),
            patch.object(coord, "_import_intervals", new_callable=AsyncMock),
        ):
            await coord._fetch_missing_days(last_sum=50.0)

        called_days = {
            c.args[1] for c in mock_client.async_get_usage_hourly.call_args_list
        }
        expected = {today - timedelta(days=2), yesterday}
        assert called_days == expected

    async def test_no_gap_does_not_fetch(self, hass: HomeAssistant) -> None:
        """If last stat is already for yesterday, no new fetch needed."""
        from datetime import date

        mock_client = AsyncMock()
        coord = _make_coordinator(hass, client=mock_client)

        today = date.today()
        yesterday = today - timedelta(days=1)

        last_stats_raw = {
            f"{DOMAIN}:{STAT_CONSUMPTION}_{_CONTRACT}": [
                {
                    "start": float(
                        datetime.combine(yesterday, datetime.min.time())
                        .replace(tzinfo=UTC)
                        .timestamp()
                    ),
                    "sum": 50.0,
                }
            ]
        }

        with (
            patch(_PATCH_GET_LAST, return_value=last_stats_raw),
            patch(_PATCH_GET_INSTANCE, _mock_get_instance(last_stats_raw)),
        ):
            await coord._fetch_missing_days(last_sum=50.0)

        mock_client.async_get_usage_hourly.assert_not_called()

    async def test_empty_stats_sets_cumulative_and_returns(
        self, hass: HomeAssistant
    ) -> None:
        """If get_last_statistics returns no rows, store last_sum and return."""
        mock_client = AsyncMock()
        coord = _make_coordinator(hass, client=mock_client)

        with (
            patch(_PATCH_GET_LAST, return_value={}),
            patch(_PATCH_GET_INSTANCE, _mock_get_instance({})),
        ):
            await coord._fetch_missing_days(last_sum=42.0)

        assert coord._latest_cumulative_kwh == pytest.approx(42.0)
        mock_client.async_get_usage_hourly.assert_not_called()
