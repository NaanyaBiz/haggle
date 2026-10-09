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


async def test_duplicate_solar_slot_replaces_not_sums(
    recorder_mock, hass: HomeAssistant
) -> None:
    """Codex pass-4 P1 (PR #266): the dedupe must cover generation too.

    Pass 3 deduped _import_intervals only; _import_generation's hourly loop
    still summed a slot appearing twice in one batch, inflating the
    generation and feed-in-credit series through the identical trailing-slack
    overlap. Both importers now share _dedupe_slots.
    """
    coord = _make_coordinator(hass)
    stat_id_gen, _ = coord._generation_stat_ids()

    slot = datetime(2026, 6, 30, 2, tzinfo=UTC)
    batch = [
        # Day D's trailing-slack copy with an inflated value.
        IntervalReading(dt=slot, kwh=4.0, cost_aud=0.80, rate_type="normal"),
        # Day D+1's genuine response for the same slot.
        IntervalReading(dt=slot, kwh=1.5, cost_aud=0.25, rate_type="normal"),
    ]
    await coord._import_generation(batch)
    await async_wait_recording_done(hass)

    rows = await _read_series(hass, stat_id_gen)
    assert len(rows) == 1
    # 1.5, not 5.5: the duplicate replaced.
    assert abs(rows[-1]["sum"] - 1.5) < 1e-9


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


async def test_half_hour_zone_straddle_23_30_slot_survives_re_import(
    recorder_mock, hass: HomeAssistant
) -> None:
    """#292 A4: the day-boundary straddle bucket survives an overlap re-import.

    For Adelaide (ACST = UTC+9:30), local midnight falls at :30 past the UTC
    hour. The UTC bucket that straddles the day boundary is half-owned by each
    adjacent local day. _straddle_trim_before detects a batch starting at :30
    and trims the first (partial) UTC bucket so the prior day's fully-written
    row is never overwritten.

    Mutation: _straddle_trim_before returns None → the second import includes
    the partial 14:00Z bucket in hour_cons; its baseline is computed including
    that bucket (partial overlap), and the re-emitted chain steps down at
    14:00Z — a #114-class defect.

    Scenario (Adelaide ACST winter 2026-05-31):
    - PRIOR DAY import: 4 half-hour slots starting at 13:00Z (minute=0 →
      no straddle trim). Establishes the 14:00Z bucket with 2.0 kWh (the
      two half-hour slots at 14:00 and 14:30).
    - OVERLAP BATCH import: starts at 14:30Z (minute=30 → _straddle_trim_before
      returns 15:00Z). The 14:00Z bucket is trimmed; only 15:00Z onward is
      written. The stored 14:00Z row (2.0 kWh sum) must remain unchanged.
    """
    from zoneinfo import ZoneInfo

    coord = _make_coordinator(hass)
    coord.client.local_tz = ZoneInfo("Australia/Adelaide")
    coord.client.tz_is_contract = True

    stat_id = f"{DOMAIN}:{STAT_CONSUMPTION}_{_CONTRACT}"

    def _ts(dt: datetime) -> float:
        return dt.timestamp()

    straddle_hour = datetime(2026, 5, 31, 14, 0, tzinfo=UTC)

    # --- PRIOR DAY import: slots starting at 13:00Z (minute=0 → no trim) ---
    # 4 half-hour slots: 13:00, 13:30 → bucket 13:00Z; 14:00, 14:30 → bucket 14:00Z.
    prior_slots: list[IntervalReading] = [
        IntervalReading(
            dt=datetime(2026, 5, 31, 13, 0, tzinfo=UTC),
            kwh=1.0,
            cost_aud=0.30,
            rate_type="normal",
        ),
        IntervalReading(
            dt=datetime(2026, 5, 31, 13, 30, tzinfo=UTC),
            kwh=1.0,
            cost_aud=0.30,
            rate_type="normal",
        ),
        IntervalReading(
            dt=datetime(2026, 5, 31, 14, 0, tzinfo=UTC),
            kwh=1.0,
            cost_aud=0.30,
            rate_type="normal",
        ),
        IntervalReading(
            dt=datetime(2026, 5, 31, 14, 30, tzinfo=UTC),
            kwh=1.0,
            cost_aud=0.30,
            rate_type="normal",
        ),
    ]
    await coord._import_intervals(prior_slots)
    await async_wait_recording_done(hass)

    rows_prior = await _read_series(hass, stat_id)
    # row["start"] is a Unix timestamp float.
    straddle_ts = _ts(straddle_hour)
    straddle_after_prior = next(
        (r for r in rows_prior if abs(r["start"] - straddle_ts) < 1), None
    )
    assert straddle_after_prior is not None, (
        "Straddle bucket at 14:00Z must be written on prior import"
    )
    # Both 14:00Z and 14:30Z slots land in the 14:00Z bucket → sum = 2.0.
    # (Cumulative sum: 13:00Z bucket = 2.0 kWh, 14:00Z bucket = 4.0 kWh total)
    straddle_sum_after_prior = straddle_after_prior["sum"]

    # --- OVERLAP BATCH: starts at 14:30Z (minute=30 → straddle trim = 15:00Z) ---
    # _straddle_trim_before returns 15:00Z; the 14:00Z bucket is dropped.
    overlap_slots: list[IntervalReading] = [
        IntervalReading(
            dt=datetime(2026, 5, 31, 14, 30, tzinfo=UTC),  # minute=30 → triggers trim
            kwh=0.5,
            cost_aud=0.15,
            rate_type="normal",
        ),
        IntervalReading(
            dt=datetime(2026, 5, 31, 15, 0, tzinfo=UTC),
            kwh=1.0,
            cost_aud=0.30,
            rate_type="normal",
        ),
        IntervalReading(
            dt=datetime(2026, 5, 31, 15, 30, tzinfo=UTC),
            kwh=1.0,
            cost_aud=0.30,
            rate_type="normal",
        ),
        IntervalReading(
            dt=datetime(2026, 5, 31, 16, 0, tzinfo=UTC),
            kwh=1.0,
            cost_aud=0.30,
            rate_type="normal",
        ),
        IntervalReading(
            dt=datetime(2026, 5, 31, 16, 30, tzinfo=UTC),
            kwh=1.0,
            cost_aud=0.30,
            rate_type="normal",
        ),
    ]
    await coord._import_intervals(overlap_slots)
    await async_wait_recording_done(hass)

    rows_after = await _read_series(hass, stat_id)

    # Straddle bucket at 14:00Z must be unchanged.
    straddle_after_overlap = next(
        (r for r in rows_after if abs(r["start"] - straddle_ts) < 1), None
    )
    assert straddle_after_overlap is not None, "Straddle bucket must still exist"
    assert straddle_after_overlap["sum"] == pytest.approx(straddle_sum_after_prior), (
        f"Straddle sum changed from {straddle_sum_after_prior} to "
        f"{straddle_after_overlap['sum']}: "
        "_straddle_trim_before failed to protect the straddle bucket"
    )

    # Monotonicity check: no downward steps anywhere in the chain.
    sums = [r["sum"] for r in sorted(rows_after, key=lambda r: r["start"])]
    for earlier, later in pairwise(sums):
        assert later >= earlier - 1e-9, (
            f"Sum chain is not monotone: {earlier} → {later} (downward step)"
        )


def _slot(
    dt: datetime, kwh: float, rate_type: str = "normal", cost: float = 0.30
) -> IntervalReading:
    return IntervalReading(dt=dt, kwh=kwh, cost_aud=cost, rate_type=rate_type)


async def _row_sum_at(hass: HomeAssistant, stat_id: str, hour: datetime) -> float:
    """Stored cumulative sum of the `hour` row, or fail if the row is absent."""
    rows = await _read_series(hass, stat_id)
    row = next((r for r in rows if abs(r["start"] - hour.timestamp()) < 1), None)
    assert row is not None, f"{stat_id}: no row at {hour.isoformat()}"
    return float(row["sum"])


async def _assert_monotone(hass: HomeAssistant, stat_id: str) -> None:
    rows = sorted(await _read_series(hass, stat_id), key=lambda r: r["start"])
    for earlier, later in pairwise(r["sum"] for r in rows):
        assert later >= earlier - 1e-9, f"{stat_id}: downward step {earlier} → {later}"


async def test_half_hour_zone_straddle_trim_protects_every_tou_band(
    recorder_mock, hass: HomeAssistant
) -> None:
    """#292 A4(b): the straddle trim covers the per-tariff series, not only the
    aggregate — the rows a ToU user's Energy dashboard actually reads.

    Same Adelaide shape as test_half_hour_zone_straddle_23_30_slot_survives_re_import
    but every slot carries a ToU band, so haggle:consumption_<band>_* and
    haggle:cost_<band>_* are emitted.

    Mutation: skip `_drop_bands_before` (trim the aggregate only) → the band
    batch still holds the half-full 14:00Z bucket while its baseline is read
    at the aggregate's trimmed cutoff (15:00Z, i.e. the stored FULL 14:00Z
    row), so the band's 14:00Z row is rewritten as full + half: an UPWARD
    double-count (2.0 → 2.5 kWh on offpeak) that a monotonicity check alone
    would never catch, repeated on every rewindow while the day is inside it.
    """
    from zoneinfo import ZoneInfo

    coord = _make_coordinator(hass)
    coord.client.local_tz = ZoneInfo("Australia/Adelaide")
    coord.client.tz_is_contract = True
    bands = frozenset({"peak", "offpeak"})
    straddle_hour = datetime(2026, 5, 31, 14, 0, tzinfo=UTC)  # Adelaide 23:30/00:00
    band_ids = [
        *coord._tariff_stat_ids("peak"),
        *coord._tariff_stat_ids("offpeak"),
    ]

    # PRIOR import: 13:00Z..14:30Z, alternating peak/offpeak, 1.0 kWh each.
    # The 14:00Z bucket ends up holding 1.0 peak (14:00) + 1.0 offpeak (14:30).
    prior = [
        _slot(datetime(2026, 5, 31, 13, 0, tzinfo=UTC), 1.0, "peak"),
        _slot(datetime(2026, 5, 31, 13, 30, tzinfo=UTC), 1.0, "offpeak"),
        _slot(datetime(2026, 5, 31, 14, 0, tzinfo=UTC), 1.0, "peak"),
        _slot(datetime(2026, 5, 31, 14, 30, tzinfo=UTC), 1.0, "offpeak"),
    ]
    await coord._import_intervals(prior, known_bands=bands)
    await async_wait_recording_done(hass)
    straddle_before = {
        sid: await _row_sum_at(hass, sid, straddle_hour) for sid in band_ids
    }

    # OVERLAP batch: opens on the Adelaide-midnight slot (14:30Z, offpeak, re-fetched
    # at a different value) → trim to 15:00Z in EVERY series.
    overlap = [
        _slot(datetime(2026, 5, 31, 14, 30, tzinfo=UTC), 0.5, "offpeak"),
        _slot(datetime(2026, 5, 31, 15, 0, tzinfo=UTC), 1.0, "peak"),
        _slot(datetime(2026, 5, 31, 15, 30, tzinfo=UTC), 1.0, "offpeak"),
        _slot(datetime(2026, 5, 31, 16, 0, tzinfo=UTC), 1.0, "peak"),
        _slot(datetime(2026, 5, 31, 16, 30, tzinfo=UTC), 1.0, "offpeak"),
    ]
    await coord._import_intervals(overlap, known_bands=bands)
    await async_wait_recording_done(hass)

    for sid in band_ids:
        # Load-bearing: the straddle row is untouched in every band series.
        assert await _row_sum_at(hass, sid, straddle_hour) == pytest.approx(
            straddle_before[sid]
        ), f"{sid}: straddle row rewritten"
        # The rows after the trim exist (an over-aggressive trim that dropped
        # the whole band would leave the series at its prior length).
        for hour in (
            datetime(2026, 5, 31, 15, 0, tzinfo=UTC),
            datetime(2026, 5, 31, 16, 0, tzinfo=UTC),
        ):
            await _row_sum_at(hass, sid, hour)
        await _assert_monotone(hass, sid)

    # Expected chains: each band had 2.0 kWh (two 1.0 slots) stored by the prior
    # import and gains 1.0 per hour from 15:00Z — 3.0 then 4.0.
    peak_cons, _ = coord._tariff_stat_ids("peak")
    offpeak_cons, _ = coord._tariff_stat_ids("offpeak")
    for sid in (peak_cons, offpeak_cons):
        assert await _row_sum_at(
            hass, sid, datetime(2026, 5, 31, 15, 0, tzinfo=UTC)
        ) == pytest.approx(3.0)
        assert await _row_sum_at(
            hass, sid, datetime(2026, 5, 31, 16, 0, tzinfo=UTC)
        ) == pytest.approx(4.0)


def _adelaide_day_slots(day: date, day_idx: int) -> list[IntervalReading]:
    """48 distinct-valued half-hour slots of one Adelaide local day."""
    from zoneinfo import ZoneInfo

    start = datetime(
        day.year, day.month, day.day, tzinfo=ZoneInfo("Australia/Adelaide")
    ).astimezone(UTC)
    return [
        _slot(start + timedelta(minutes=30 * j), (day_idx + 1) + j / 100)
        for j in range(48)
    ]


async def _bucket_deltas(hass: HomeAssistant, stat_id: str) -> dict[datetime, float]:
    """{hour: kWh written for that hour} from the stored cumulative chain."""
    rows = sorted(await _read_series(hass, stat_id), key=lambda r: r["start"])
    deltas: dict[datetime, float] = {}
    prev = 0.0
    for r in rows:
        assert r["sum"] >= prev - 1e-9, f"{stat_id}: downward step at {r['start']}"
        deltas[datetime.fromtimestamp(r["start"], tz=UTC)] = r["sum"] - prev
        prev = r["sum"]
    return deltas


async def test_half_hour_zone_overlap_day_error_residual_is_one_half_slot(
    recorder_mock, hass: HomeAssistant
) -> None:
    """#292 A4(c): pins the documented failure-path residual at its TRUE size.

    Cycle 1 imports days A+B (A at the floor: no overlap). Cycle 2 imports C
    ALONE — its overlap day B errored. Cycle 3 imports C (now the overlap /
    context day) + D. Cycle 1 wrote the B/C boundary bucket (14:00Z) with
    B's 23:30 only, C not being in that batch. Cycle 2 opens on C's 00:00 —
    the straddle slot — so the content trim drops that bucket and the stored
    row is left as is. Cycle 3 opens on the same slot again (C is the
    batch's first day) and trims it again. C's 00:00 half-slot is therefore
    never written: the residual is one 30-min slot, permanently — NOT
    "restored by the next cycle's overlap". Every other bucket, including the
    C/D boundary that cycle 3's healthy overlap fills whole, must equal the
    true slot total; A's own 00:00 bucket (the accepted floor residual) is
    trimmed in cycle 1 and has no row at all; the chain must stay monotone
    (a missing slot is never a downward step).
    """
    from zoneinfo import ZoneInfo

    coord = _make_coordinator(hass)
    coord.client.local_tz = ZoneInfo("Australia/Adelaide")
    coord.client.tz_is_contract = True
    stat_id = f"{DOMAIN}:{STAT_CONSUMPTION}_{_CONTRACT}"
    day_a = _adelaide_day_slots(date(2026, 5, 30), 0)
    day_b = _adelaide_day_slots(date(2026, 5, 31), 1)
    day_c = _adelaide_day_slots(date(2026, 6, 1), 2)
    day_d = _adelaide_day_slots(date(2026, 6, 2), 3)

    await coord._import_intervals(day_a + day_b)
    await async_wait_recording_done(hass)
    await coord._import_intervals(day_c)  # overlap day B errored
    await async_wait_recording_done(hass)
    await coord._import_intervals(day_c + day_d)  # C is the overlap day now
    await async_wait_recording_done(hass)

    got = await _bucket_deltas(hass, stat_id)
    expected: dict[datetime, float] = {}
    for s in day_a + day_b + day_c + day_d:
        h = s.dt.replace(minute=0)
        expected[h] = expected.get(h, 0.0) + s.kwh
    floor_bucket = day_a[0].dt.replace(minute=0)  # A 00:00 (no overlap at floor)
    bc_boundary = day_c[0].dt.replace(minute=0)  # B 23:30 + C 00:00
    cd_boundary = day_d[0].dt.replace(minute=0)  # C 23:30 + D 00:00

    # The accepted floor residual: A's 00:00 bucket was trimmed, no row at all.
    assert floor_bucket not in got
    # The failure-path residual: C's 00:00 half-slot is absent, permanently.
    assert got[bc_boundary] == pytest.approx(day_b[-1].kwh)
    assert got[bc_boundary] != pytest.approx(day_b[-1].kwh + day_c[0].kwh)
    # The healthy overlap in cycle 3 wrote the C/D boundary whole.
    assert got[cd_boundary] == pytest.approx(expected[cd_boundary])
    # Everything else is exact.
    for hour, kwh in expected.items():
        if hour in (floor_bucket, bc_boundary):
            continue
        assert got[hour] == pytest.approx(kwh), hour.isoformat()
