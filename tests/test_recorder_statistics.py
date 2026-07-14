"""Recorder-backed statistics tests — the real engine, no boundary mocks.

Every other statistics test in this suite patches the recorder at the
boundary (async_add_external_statistics / get_last_statistics /
get_instance).  That mocked seam is exactly where the repo's production
defects escaped (#114 monotonicity break, the v0.3.0 phantom-midnight-spike,
#137), so this module re-runs the sum-chain scenarios against phcc's
`recorder_mock` — a real in-memory SQLite recorder running HA's real
statistics engine.  Slow-ish per test (~0.2 s) but a different failure
domain: these tests catch semantic drift between our import logic and the
recorder's actual cumulative-sum handling across HA releases.

Scenario map (each pins a real production defect class):
- test_rewindow_overwrite_no_midnight_spike  → v0.3.0 phantom midnight spike
- test_band_reachback_baseline_after_long_absence → #114 TOTAL_INCREASING break
- test_tou_partition_sums_to_aggregate → ToU partition completeness (docs
  contract: per-band series must sum back to the aggregate, no lost kWh)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.haggle.agl.models import IntervalReading
from custom_components.haggle.const import (
    CONF_ACCOUNT_NUMBER,
    CONF_CONTRACT_NUMBER,
    CONF_REFRESH_TOKEN,
    DOMAIN,
    STAT_CONSUMPTION,
)
from custom_components.haggle.coordinator import HaggleCoordinator

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

# --- cpython gh-145754 shim -------------------------------------------------
# Python 3.14.2's unittest.mock resolves autospec signatures with
# inspect.signature(..., Format.VALUE), which evaluates PEP 649 deferred
# annotations.  phcc's async_test_recorder fixture autospec-patches recorder
# functions whose annotations name TYPE_CHECKING-only symbols, so fixture
# setup dies with NameError ('Recorder' at recorder/migration.py, 'Session'
# at helpers/recorder.py).  Fixed upstream (cpython PR #146191, 3.14 branch);
# until every dev/CI interpreter carries the fix, materialise the two names.
# Harmless where the interpreter is already fixed (hasattr guards).


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


@pytest.fixture(autouse=True)
def _auto_enable_custom_integrations(
    recorder_mock, enable_custom_integrations: None
) -> None:
    """Override the suite-wide autouse fixture for this module only.

    The conftest version depends on `hass` alone, which would set hass up
    before the recorder — phcc's `recorder_db_url` asserts the recorder
    fixtures initialise first.  Requesting `recorder_mock` ahead of
    `enable_custom_integrations` restores the required order.
    """


_CONTRACT = "9999999999"


def _make_coordinator(hass: HomeAssistant) -> HaggleCoordinator:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_REFRESH_TOKEN: "v1.testtoken",
            CONF_CONTRACT_NUMBER: _CONTRACT,
            CONF_ACCOUNT_NUMBER: "1234567890",
        },
        unique_id="1234567890_9999999999",
    )
    entry.add_to_hass(hass)
    return HaggleCoordinator(hass, entry, AsyncMock(), _CONTRACT)


def _hourly_intervals(
    start: datetime, hours: int, kwh: float = 1.0
) -> list[IntervalReading]:
    return [
        IntervalReading(
            dt=start + timedelta(hours=i), kwh=kwh, cost_aud=0.30, rate_type="normal"
        )
        for i in range(hours)
    ]


async def _read_series(hass: HomeAssistant, stat_id: str) -> list[dict]:
    """Read every stored hourly row for stat_id from the REAL recorder."""
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


async def test_rewindow_overwrite_no_midnight_spike(
    recorder_mock, hass: HomeAssistant
) -> None:
    """v0.3.0 phantom-midnight-spike class, on the real statistics engine.

    Import a 48 h chain, then re-import the trailing 24 h (the rewindow)
    whose earliest fetched hour is AEST local midnight (14:00Z).  The
    baseline must resolve at the earliest fetched hour, so the overwrite
    keeps every hourly sum delta exactly equal to that hour's state — no
    +N kWh jump at the overlap boundary.
    """
    coord = _make_coordinator(hass)
    stat_id = f"{DOMAIN}:{STAT_CONSUMPTION}_{_CONTRACT}"

    t0 = datetime(2026, 6, 28, 14, tzinfo=UTC)
    await coord._import_intervals(_hourly_intervals(t0, 48))
    await async_wait_recording_done(hass)

    # Rewindow re-fetch: same values, idempotent overwrite expected.
    await coord._import_intervals(_hourly_intervals(t0 + timedelta(hours=24), 24))
    await async_wait_recording_done(hass)

    rows = await _read_series(hass, stat_id)
    assert len(rows) == 48
    sums = [row["sum"] for row in rows]
    deltas = [b - a for a, b in pairwise(sums)]
    assert all(abs(d - 1.0) < 1e-9 for d in deltas), deltas
    assert abs(sums[-1] - 48.0) < 1e-9


async def test_band_reachback_baseline_after_long_absence(
    recorder_mock, hass: HomeAssistant
) -> None:
    """#114 class: a ToU band absent longer than the narrow lookup window
    must continue its cumulative sum via the reach-back stage of
    _baseline_sums_before — never reset to 0.0 (a downward step breaks
    TOTAL_INCREASING monotonicity)."""
    coord = _make_coordinator(hass)
    stat_id = f"{DOMAIN}:consumption_shoulder_{_CONTRACT}"

    t0 = datetime(2026, 5, 1, 14, tzinfo=UTC)
    first = [
        IntervalReading(dt=t0, kwh=2.0, cost_aud=0.5, rate_type="shoulder"),
        IntervalReading(
            dt=t0 + timedelta(hours=1), kwh=3.0, cost_aud=0.5, rate_type="shoulder"
        ),
    ]
    await coord._import_intervals(first)
    await async_wait_recording_done(hass)

    # 40 days later — outside the per-band BACKFILL_DAYS lookup window.
    t1 = datetime(2026, 6, 10, 14, tzinfo=UTC)
    second = [IntervalReading(dt=t1, kwh=1.0, cost_aud=0.5, rate_type="shoulder")]
    await coord._import_intervals(second, known_bands=frozenset({"shoulder"}))
    await async_wait_recording_done(hass)

    rows = await _read_series(hass, stat_id)
    sums = [row["sum"] for row in rows]
    assert sums == sorted(sums), f"monotonicity broken: {sums}"
    assert abs(sums[-1] - 6.0) < 1e-9, sums  # 2 + 3 + 1 — chain continued


async def test_tou_partition_sums_to_aggregate(
    recorder_mock, hass: HomeAssistant
) -> None:
    """ToU partition completeness: on the real engine, the per-band series
    (peak/offpeak/shoulder/normal) must partition the aggregate exactly —
    the documented Energy-dashboard contract (no kWh lost, none counted
    twice across the band split)."""
    coord = _make_coordinator(hass)
    t0 = datetime(2026, 6, 28, 14, tzinfo=UTC)
    bands = ["peak", "offpeak", "shoulder", "normal"]
    intervals = [
        IntervalReading(
            dt=t0 + timedelta(hours=i),
            kwh=0.5 + 0.1 * (i % 4),
            cost_aud=0.2,
            rate_type=bands[i % 4],
        )
        for i in range(8)
    ]
    await coord._import_intervals(intervals)
    await async_wait_recording_done(hass)

    agg_rows = await _read_series(hass, f"{DOMAIN}:{STAT_CONSUMPTION}_{_CONTRACT}")
    band_final = 0.0
    for band in bands:
        rows = await _read_series(hass, f"{DOMAIN}:consumption_{band}_{_CONTRACT}")
        assert rows, f"missing band series {band}"
        band_final += rows[-1]["sum"]
    assert abs(band_final - agg_rows[-1]["sum"]) < 1e-9
