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

from datetime import UTC, date, datetime, timedelta
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


async def test_poisoned_old_timestamp_cannot_step_sum_down(
    recorder_mock, hass: HomeAssistant
) -> None:
    """T-4 (#242) on the REAL statistics engine — the test docs/testing.md
    requires for any baseline/cumulative-sum change.

    Build a mature 48 h chain (sum reaches 48.0). Then import a later batch
    that ALSO carries one interval timestamped 1970 — the crafted-timestamp
    attack. Pre-fix, that row pins the baseline cutoff at 1970, the baseline
    resolves to 0.0, and the new rows restart the chain near zero: a massive
    downward step in the recorder's sum column.

    The parser's window guard drops the 1970 row before it ever reaches
    _import_intervals, so this test feeds the POST-PARSER path exactly as the
    client wires it: parse_interval_readings(payload, expected_day=...).
    """
    from custom_components.haggle.agl.parser import parse_interval_readings

    coord = _make_coordinator(hass)
    stat_id = f"{DOMAIN}:{STAT_CONSUMPTION}_{_CONTRACT}"

    t0 = datetime(2026, 6, 28, 14, tzinfo=UTC)
    await coord._import_intervals(_hourly_intervals(t0, 48))
    await async_wait_recording_done(hass)

    # Attack batch: two legitimate hours for 2026-06-30 plus one 1970 row.
    day = date(2026, 6, 30)

    def _item(iso: str) -> dict:
        return {
            "dateTime": iso,
            "consumption": {"type": "normal", "quantity": 1.0, "amount": 0.30},
        }

    payload = {
        "sections": [
            {
                "items": [
                    _item("2026-06-30T14:00:00Z"),
                    _item("2026-06-30T15:00:00Z"),
                    _item("1970-01-02T00:00:00Z"),  # the poison
                ]
            }
        ]
    }
    readings = parse_interval_readings(payload, expected_day=day)
    assert len(readings) == 2, "window guard must drop the 1970 row"

    await coord._import_intervals(readings)
    await async_wait_recording_done(hass)

    rows = await _read_series(hass, stat_id)
    sums = [row["sum"] for row in rows]
    # No downward step anywhere, and the chain continued from 48, not from 0.
    assert all(b >= a for a, b in pairwise(sums)), sums
    assert abs(sums[-1] - 50.0) < 1e-9
    # And no phantom 1970 row was written (start is an epoch float here).
    assert all(
        datetime.fromtimestamp(row["start"], tz=UTC).year >= 2026 for row in rows
    )


async def test_adjacent_date_injection_cannot_step_sum_down(
    recorder_mock, hass: HomeAssistant
) -> None:
    """Codex P1 (PR #266) on the REAL statistics engine — the adjacent-date
    variant the 1970 test misses.

    For an AEST contract, day D legitimately starts at D-1T14:00Z, and the
    old ±1-DATE window therefore accepted EVERY instant of D-1. An injected
    D-1T00:00Z reading (here 2026-06-30T00:00Z against requested day
    2026-07-01) sat INSIDE the mature stored chain: it pinned the baseline
    cutoff ~14 h early, the baseline resolved to the sum as of that hour, and
    the day's genuine rows were then written far below the stored tip — a
    downward step in the sum column with no 1970-style absurdity to catch.
    The tz-derived window (local day ± 2 h slack) drops the injected row.
    """
    from zoneinfo import ZoneInfo

    from custom_components.haggle.agl.parser import parse_interval_readings

    coord = _make_coordinator(hass)
    stat_id = f"{DOMAIN}:{STAT_CONSUMPTION}_{_CONTRACT}"

    t0 = datetime(2026, 6, 28, 14, tzinfo=UTC)
    await coord._import_intervals(_hourly_intervals(t0, 48))
    await async_wait_recording_done(hass)

    # Requested local day 2026-07-01 (AEST): true window starts 06-30T14:00Z.
    day = date(2026, 7, 1)

    def _item(iso: str) -> dict:
        return {
            "dateTime": iso,
            "consumption": {"type": "normal", "quantity": 1.0, "amount": 0.30},
        }

    payload = {
        "sections": [
            {
                "items": [
                    _item("2026-06-30T14:00:00Z"),  # genuine first slot
                    _item("2026-06-30T15:00:00Z"),
                    # The poison: same UTC DATE as a genuine slot, so the old
                    # date window passed it; 14 h before the true local start.
                    _item("2026-06-30T00:00:00Z"),
                ]
            }
        ]
    }
    readings = parse_interval_readings(
        payload, expected_day=day, tz=ZoneInfo("Australia/Brisbane")
    )
    assert len(readings) == 2, "tz window must drop the adjacent-date poison"
    assert min(r.dt for r in readings) == datetime(2026, 6, 30, 14, tzinfo=UTC)

    await coord._import_intervals(readings)
    await async_wait_recording_done(hass)

    rows = await _read_series(hass, stat_id)
    sums = [row["sum"] for row in rows]
    # No downward step, and the chain continued from 48 (not restarted from
    # the mid-chain baseline the poisoned cutoff would have produced).
    assert all(b >= a for a, b in pairwise(sums)), sums
    assert abs(sums[-1] - 50.0) < 1e-9


async def test_duplicate_slot_in_one_batch_replaces_not_sums(
    recorder_mock, hass: HomeAssistant
) -> None:
    """Codex pass-3 P1 (PR #266) on the REAL statistics engine.

    A multi-day _fetch_range appends every day's readings to one list and
    imports it once. If day D's response carries a row inside the trailing-
    slack window that day D+1's response then also returns, the same slot is
    in the batch twice — and _bucket_hourly sums everything it is given, so
    the hour was silently inflated (the recorder's idempotent overwrite only
    dedupes ACROSS imports, not within one). _import_intervals now dedupes
    by slot last-wins: days arrive in chronological order, so the later
    day's response is authoritative for its own slots.
    """
    from custom_components.haggle.agl.models import IntervalReading

    coord = _make_coordinator(hass)
    stat_id = f"{DOMAIN}:{STAT_CONSUMPTION}_{_CONTRACT}"

    t0 = datetime(2026, 6, 28, 14, tzinfo=UTC)
    await coord._import_intervals(_hourly_intervals(t0, 48))
    await async_wait_recording_done(hass)

    slot = datetime(2026, 6, 30, 14, tzinfo=UTC)
    batch = [
        # Day D's response: a trailing-slack copy of D+1's first slot,
        # carrying an inflated value.
        IntervalReading(dt=slot, kwh=5.0, cost_aud=1.50, rate_type="normal"),
        # Day D+1's genuine response for the same slot, appended later.
        IntervalReading(dt=slot, kwh=1.0, cost_aud=0.30, rate_type="normal"),
        IntervalReading(
            dt=slot + timedelta(hours=1), kwh=1.0, cost_aud=0.30, rate_type="normal"
        ),
    ]
    await coord._import_intervals(batch)
    await async_wait_recording_done(hass)

    rows = await _read_series(hass, stat_id)
    sums = [row["sum"] for row in rows]
    assert all(b >= a for a, b in pairwise(sums)), sums
    # 48 stored + 1.0 + 1.0 — NOT 48 + (5+1) + 1: the duplicate replaced.
    assert abs(sums[-1] - 50.0) < 1e-9


async def test_two_overbound_readings_cannot_write_inf_sum(
    recorder_mock, hass: HomeAssistant
) -> None:
    """#241's exact scenario at the recorder layer: two 1e308 readings in one
    hourly bucket. Pre-fix, safe_float passed them through, _bucket_hourly
    summed them to inf, and the recorder stored a non-finite sum. Post-fix
    they reject to 0.0 -> zero-delta hours, sum chain flat and finite.
    """
    import math

    coord = _make_coordinator(hass)
    stat_id = f"{DOMAIN}:{STAT_CONSUMPTION}_{_CONTRACT}"

    t0 = datetime(2026, 6, 28, 14, tzinfo=UTC)
    await coord._import_intervals(_hourly_intervals(t0, 24))
    await async_wait_recording_done(hass)

    from custom_components.haggle.agl.parser import parse_interval_readings

    def _item(iso: str, qty: float) -> dict:
        return {
            "dateTime": iso,
            "consumption": {"type": "normal", "quantity": qty, "amount": 0.30},
        }

    payload = {
        "sections": [
            {
                "items": [
                    # Two half-hour slots in the SAME hour, both over-bound.
                    _item("2026-06-29T14:00:00Z", 1e308),
                    _item("2026-06-29T14:30:00Z", 1e308),
                    # One sane reading after, so the batch isn't empty-ish.
                    _item("2026-06-29T15:00:00Z", 1.0),
                ]
            }
        ]
    }
    readings = parse_interval_readings(payload, expected_day=date(2026, 6, 29))
    await coord._import_intervals(readings)
    await async_wait_recording_done(hass)

    rows = await _read_series(hass, stat_id)
    sums = [row["sum"] for row in rows]
    assert all(math.isfinite(v) for v in sums), sums
    assert all(b >= a for a, b in pairwise(sums)), sums
    # 24 legit + the sane 1.0; the two 1e308s contributed exactly nothing.
    assert abs(sums[-1] - 25.0) < 1e-9


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


# ---------------------------------------------------------------------------
# _earliest_stat_date (#214) — real recorder, real timezone conversion
# ---------------------------------------------------------------------------
#
# Every other test of this helper (test_coordinator_statistics.py) mocks it
# out entirely, so its actual recorder query — including the local-date
# conversion that #214 review caught as wrong in an earlier draft (raw UTC
# .date() rather than dt_util.as_local(...).date()) — was never exercised
# against a real statistics row. These tests close that gap.


async def test_earliest_stat_date_reports_local_calendar_day(
    recorder_mock, hass: HomeAssistant
) -> None:
    """A row at local midnight must report the LOCAL date, not the raw UTC
    date of its stored timestamp.

    Australia/Brisbane is a fixed +10:00 offset (no DST) — real AGL contract
    territory. Local midnight 2026-06-29T00:00+10:00 is stored under the
    PREVIOUS UTC calendar date, 2026-06-28T14:00Z. Comparing the raw UTC
    date would misreport covered_from by a day (and could mask a real
    one-day truncation as "not truncated").
    """
    await hass.config.async_set_time_zone("Australia/Brisbane")
    coord = _make_coordinator(hass)
    stat_id_gen, _ = coord._generation_stat_ids()

    t0 = datetime(2026, 6, 28, 14, tzinfo=UTC)  # local 2026-06-29 00:00 +10:00
    await coord._import_generation(
        [IntervalReading(dt=t0, kwh=1.0, cost_aud=0.1, rate_type="normal")]
    )
    await async_wait_recording_done(hass)

    earliest = await coord._earliest_stat_date(stat_id_gen, t0 - timedelta(days=1))
    assert earliest == date(2026, 6, 29)


async def test_earliest_stat_date_ignores_rows_before_since(
    recorder_mock, hass: HomeAssistant
) -> None:
    """`since` is a hard lower bound on the query — an earlier row that
    exists in the recorder must not be returned (this is what lets
    _get_generation_period_totals bound the query at bill_start's local
    midnight instead of scanning a mature series' entire history)."""
    coord = _make_coordinator(hass)
    stat_id_gen, _ = coord._generation_stat_ids()

    older = datetime(2026, 6, 1, 14, tzinfo=UTC)
    newer = datetime(2026, 6, 20, 14, tzinfo=UTC)
    await coord._import_generation(
        [
            IntervalReading(dt=older, kwh=1.0, cost_aud=0.1, rate_type="normal"),
            IntervalReading(dt=newer, kwh=1.0, cost_aud=0.1, rate_type="normal"),
        ]
    )
    await async_wait_recording_done(hass)

    since = datetime(2026, 6, 15, tzinfo=UTC)
    earliest = await coord._earliest_stat_date(stat_id_gen, since)
    assert earliest == newer.date()


async def test_earliest_stat_date_no_rows_returns_none(
    recorder_mock, hass: HomeAssistant
) -> None:
    """No stored rows at/after `since` (fresh series) → None, not a crash."""
    coord = _make_coordinator(hass)
    stat_id_gen, _ = coord._generation_stat_ids()

    since = datetime(2026, 1, 1, tzinfo=UTC)
    assert await coord._earliest_stat_date(stat_id_gen, since) is None
