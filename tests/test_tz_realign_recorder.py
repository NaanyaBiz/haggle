"""Real-recorder tests for the #292 PR2b timezone history re-alignment machine.

These tests use phcc's real in-memory SQLite recorder to verify that
the junction-drop logic produces a monotone, conservation-correct sum chain
at the floor boundary (M1 exact junction).

Must be a separate module from test_tz_realign.py: phcc's recorder_db_url
asserts the recorder fixtures are initialised BEFORE the hass fixture; the
module-level autouse fixture override is the only clean way to guarantee that
ordering without affecting non-recorder tests.

C11 unique-per-slot profile: kwh = 0.10 + 0.01 * slot_index.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.haggle.agl.models import BillPeriod, IntervalReading, PlanRates
from custom_components.haggle.agl.parser import (
    agl_api_tz,
    agl_reported_timestamp,
)
from custom_components.haggle.const import (
    CONF_ACCOUNT_NUMBER,
    CONF_CONTRACT_NUMBER,
    CONF_LOCAL_TZ,
    CONF_REFRESH_TOKEN,
    DOMAIN,
    STAT_CONSUMPTION,
)
from custom_components.haggle.coordinator import HaggleCoordinator

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

# ---------------------------------------------------------------------------
# cpython gh-145754 shim
# ---------------------------------------------------------------------------


def _materialize_recorder_annotations() -> None:
    from homeassistant.components import recorder
    from homeassistant.components.recorder import migration
    from homeassistant.helpers import recorder as recorder_helper
    from sqlalchemy.orm.session import Session

    if not hasattr(migration, "Recorder"):
        migration.Recorder = recorder.Recorder  # type: ignore[attr-defined]
    if not hasattr(recorder_helper, "Session"):
        recorder_helper.Session = Session  # type: ignore[attr-defined]


_materialize_recorder_annotations()

# ---------------------------------------------------------------------------
# Module-level autouse: recorder_mock before hass
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _auto_enable_custom_integrations(
    recorder_mock, enable_custom_integrations: None
) -> None:
    """Override the suite-wide autouse fixture.

    recorder_mock MUST be requested first so phcc's recorder_db_url runs
    before the hass fixture is constructed.
    """


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CONTRACT = "9999999999"
_ACCOUNT = "1234567890"


def _make_interval(dt: datetime, kwh: float, cost: float = 0.05) -> IntervalReading:
    return IntervalReading(dt=dt, kwh=kwh, cost_aud=cost, rate_type="normal")


def _make_coordinator(hass: HomeAssistant, zone_name: str) -> HaggleCoordinator:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_REFRESH_TOKEN: "v1.testtoken",
            CONF_CONTRACT_NUMBER: _CONTRACT,
            CONF_ACCOUNT_NUMBER: _ACCOUNT,
            CONF_LOCAL_TZ: zone_name,
        },
        unique_id=f"{_ACCOUNT}_{_CONTRACT}",
    )
    entry.add_to_hass(hass)

    client = AsyncMock()
    client.tz_is_contract = True
    client.local_tz = ZoneInfo(zone_name)
    client.async_get_overview = AsyncMock(return_value=[])
    client.async_get_usage_summary = AsyncMock(
        return_value=BillPeriod(
            start=date.today(),
            end=date.today(),
            consumption_kwh=0.0,
            cost_label="$0.00",
            projection_label="",
        )
    )
    client.async_get_plan = AsyncMock(
        return_value=PlanRates(
            product_name="",
            unit_rates=[],
            supply_charge_cents_per_day=0.0,
        )
    )
    client.async_get_hourly_usage = AsyncMock(return_value=[])
    return HaggleCoordinator(hass, entry, client, _CONTRACT)


def _slots_for_day_sa(
    day: date,
    *,
    old_convention: bool,
) -> list[IntervalReading]:
    """Unique-per-slot SA intervals (48 x 30 min) in either convention.

    C11 profile: kwh = 0.10 + 0.01 * slot_index.
    July (non-DST): ACST = UTC+9:30, Sydney AEST = UTC+10.
    """
    sa_tz = ZoneInfo("Australia/Adelaide")
    api_tz = agl_api_tz()
    if api_tz is None:
        return []

    readings: list[IntervalReading] = []
    for i in range(48):
        kwh = round(0.10 + 0.01 * i, 3)
        local_start = datetime(day.year, day.month, day.day, tzinfo=sa_tz) + timedelta(
            minutes=30 * i
        )
        correct_utc = local_start.astimezone(UTC)
        if old_convention:
            reported = agl_reported_timestamp(
                correct_utc, api_tz=api_tz, contract_tz=sa_tz
            )
            readings.append(_make_interval(reported, kwh=kwh, cost=kwh * 0.30))
        else:
            readings.append(_make_interval(correct_utc, kwh=kwh, cost=kwh * 0.30))
    return readings


async def _read_series(hass: HomeAssistant, stat_id: str) -> list[dict]:
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import statistics_during_period

    result = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        datetime(2020, 1, 1, tzinfo=UTC),
        None,
        {stat_id},
        "hour",
        None,
        {"start", "state", "sum"},
    )
    return list(result.get(stat_id) or [])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRealRecorderSARealign:
    """Real recorder: SA complete realign — M1 exact junction, monotone, conserved."""

    async def test_sa_complete_realign_monotone(self, hass: HomeAssistant) -> None:
        """After junction-drop re-import, the cumulative-sum chain is monotone.

        The junction drop may reduce the final sum vs the seeded value
        (it prevents double-counting), so we assert the NEW chain is
        non-decreasing — not that the absolute sum grew.
        """
        if agl_api_tz() is None:
            pytest.skip("Australia/Sydney tzdata unavailable")

        sa_tz = ZoneInfo("Australia/Adelaide")
        coord = _make_coordinator(hass, "Australia/Adelaide")
        stat_id = f"{DOMAIN}:{STAT_CONSUMPTION}_{_CONTRACT}"

        # July: non-DST, SA ACST = UTC+9:30.
        floor = date(2026, 7, 10)
        for day in [floor - timedelta(days=1), floor, floor + timedelta(days=1)]:
            await coord._import_intervals(_slots_for_day_sa(day, old_convention=True))
            await async_wait_recording_done(hass)

        rows_before = await _read_series(hass, stat_id)
        assert len(rows_before) > 0

        floor_slots = _slots_for_day_sa(floor, old_convention=False)
        floor_midnight_utc = datetime(
            floor.year, floor.month, floor.day, tzinfo=sa_tz
        ).astimezone(UTC)
        cutoff = coord._first_full_hour(floor_midnight_utc)

        api_tz = agl_api_tz()
        prev = cutoff - timedelta(hours=1)
        old_bucket_kwh = sum(
            s.kwh
            for s in floor_slots
            if prev
            <= agl_reported_timestamp(s.dt, api_tz=api_tz, contract_tz=sa_tz)
            < cutoff
        )

        with patch.object(
            coord,
            "_stored_state_at",
            new=AsyncMock(return_value=old_bucket_kwh),
        ):
            dropped = await coord._realign_junction_drop(
                floor_slots, cutoff, kwh_id=stat_id
            )

        await coord._import_intervals(dropped)
        await async_wait_recording_done(hass)

        rows_after = await _read_series(hass, stat_id)
        sums = [row["sum"] for row in rows_after]

        # Chain must be monotone.
        assert all(b >= a - 1e-9 for a, b in pairwise(sums)), (
            f"Non-monotone: {[(a, b) for a, b in pairwise(sums) if b < a - 1e-9]}"
        )

    async def test_sa_junction_no_phantom_double_count(
        self, hass: HomeAssistant
    ) -> None:
        """M1 signed residual (C11): junction drop zeroes exactly old_bucket_kwh.

        Setup:
          1. Seed floor-1 day in old convention.
          2. Import floor day (correct convention) with junction drop.
             Net kWh imported = floor_kwh - old_bucket_kwh.
          3. Assert final_sum - seed_sum == net_kwh ± 1e-6.

        Without the drop the same boundary slots would be imported a second
        time (+old_bucket_kwh phantom). With the drop they are zero — phantom = 0.
        """
        if agl_api_tz() is None:
            pytest.skip("Australia/Sydney tzdata unavailable")

        sa_tz = ZoneInfo("Australia/Adelaide")
        coord = _make_coordinator(hass, "Australia/Adelaide")
        stat_id = f"{DOMAIN}:{STAT_CONSUMPTION}_{_CONTRACT}"

        floor = date(2026, 7, 10)
        pre_floor = _slots_for_day_sa(floor - timedelta(days=1), old_convention=True)
        await coord._import_intervals(pre_floor)
        await async_wait_recording_done(hass)

        seed_sum = (await _read_series(hass, stat_id))[-1]["sum"]

        floor_slots = _slots_for_day_sa(floor, old_convention=False)
        floor_kwh = sum(s.kwh for s in floor_slots)
        floor_midnight_utc = datetime(
            floor.year, floor.month, floor.day, tzinfo=sa_tz
        ).astimezone(UTC)
        cutoff = coord._first_full_hour(floor_midnight_utc)

        api_tz = agl_api_tz()
        prev = cutoff - timedelta(hours=1)
        old_bucket_kwh = sum(
            s.kwh
            for s in floor_slots
            if prev
            <= agl_reported_timestamp(s.dt, api_tz=api_tz, contract_tz=sa_tz)
            < cutoff
        )
        net_kwh = floor_kwh - old_bucket_kwh

        with patch.object(
            coord,
            "_stored_state_at",
            new=AsyncMock(return_value=old_bucket_kwh),
        ):
            dropped = await coord._realign_junction_drop(
                floor_slots, cutoff, kwh_id=stat_id
            )

        # Verify the drop zeroed exactly old_bucket_kwh.
        dropped_kwh = sum(s.kwh for s in dropped)
        assert abs(dropped_kwh - net_kwh) < 1e-9, (
            f"drop kwh={dropped_kwh:.4f} != net={net_kwh:.4f}"
        )

        await coord._import_intervals(dropped)
        await async_wait_recording_done(hass)

        final_sum = (await _read_series(hass, stat_id))[-1]["sum"]
        phantom = (final_sum - seed_sum) - net_kwh
        assert abs(phantom) < 1e-6, (
            f"Phantom kWh: {phantom:+.6f} "
            f"(final={final_sum:.4f}, seed={seed_sum:.4f}, net={net_kwh:.4f})"
        )
