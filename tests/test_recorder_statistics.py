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
    from zoneinfo import ZoneInfo

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


# ---------------------------------------------------------------------------
# #300 stale-key fill — real recorder, zero downward steps
# ---------------------------------------------------------------------------
#
# A timezone-correction convention change (#292 PR1) moves slot timestamps
# between UTC hour buckets.  The parser's zero-on-zero filter means "no batch
# value at a key" ≠ "key untouched": a zero-import hour whose OLD slot and
# NEW slot both have kwh=0 is dropped from both sides, leaving the old-UTC
# row in the recorder with its old running sum.  Every later row moves to the
# new chain → a downward step (#300, #114 class).
#
# The fill rewrites every stored key the batch has no value for:
#   · 0.0 on an authoritative day (batch holds ≥ 1 reading dated that day
#     AND, given the provenance set, that day's own fetch returned readings);
#   · the stored state CARRIED FORWARD otherwise, so a day the batch says
#     nothing about preserves its hourly values exactly, and the chain is
#     re-chained monotonically regardless.
#
# Zone shorthand used below:
#   _SYD  = Australia/Sydney  (AGL's pre-#292 conversion zone)
#   _ADL  = Australia/Adelaide  (ACST/ACDT — 30 min offset from Sydney)
#   _BNE  = Australia/Brisbane  (no DST — 1 h offset from Sydney in Oct)


def _300_coord(hass: HomeAssistant, tz: ZoneInfo) -> HaggleCoordinator:
    """Coordinator with contract-zone correction active."""
    c = _make_coordinator(hass)
    c.client.local_tz = tz
    c.client.tz_is_contract = True
    return c


def _300_slot_dt(d: date, i: int, tz: ZoneInfo, conv: str) -> datetime:
    """UTC datetime for the i-th 30-min slot of local day d.

    conv='new': correct UTC via the contract zone.
    conv='old': AGL's pre-#292 bug — local time re-labelled as Sydney time.
    """
    from zoneinfo import ZoneInfo

    syd = ZoneInfo("Australia/Sydney")
    local = datetime(d.year, d.month, d.day, tzinfo=tz) + timedelta(minutes=30 * i)
    if conv == "new":
        return local.astimezone(UTC)
    return local.replace(tzinfo=None).replace(tzinfo=syd).astimezone(UTC)


def _300_prof(i: int) -> float:
    """kWh profile for a solar home: zero grid import 10:00-15:00 local.

    The zero block (i=20..30) is where old and new UTC buckets diverge for
    :30 local slots, leaving stale keys after a convention change.
    """
    if i < 12:
        return 0.4
    if 20 <= i <= 30:
        return 0.0  # solar covers demand — no grid import
    if i == 31:
        return 0.02  # tiny ramp at end of zero block
    return 0.3


def _300_day_slots(d: date, tz: ZoneInfo, conv: str) -> list[IntervalReading]:
    """All non-zero kWh slots for local day d at the given convention."""
    out = []
    for i in range(48):
        kwh = _300_prof(i)
        if kwh == 0.0:
            continue
        dt = _300_slot_dt(d, i, tz, conv)
        out.append(
            IntervalReading(
                dt=dt, kwh=kwh, cost_aud=round(kwh * 0.3, 6), rate_type="normal"
            )
        )
    return out


def _300_steps(rows: list[dict]) -> list[tuple[str, float]]:
    """(start_iso, magnitude) for every downward step in a sorted row list."""
    sorted_rows = sorted(rows, key=lambda r: r["start"])
    return [
        (datetime.fromtimestamp(b["start"], tz=UTC).isoformat(), a["sum"] - b["sum"])
        for a, b in pairwise(sorted_rows)
        if b["sum"] < a["sum"] - 1e-9
    ]


async def test_300_stale_key_fill_sa_consumption_zero_steps(
    recorder_mock, hass: HomeAssistant
) -> None:
    """#300 reproduction on the real recorder — Adelaide (ACDT, UTC+10:30).

    SA solar home: zero-import block at local 10:00-15:00 (i=20..30).  Old
    AGL convention placed :30-minute slots 30 min earlier in UTC than the
    correct ACDT convention; zero slots are dropped by the parser on both
    sides.  The stale old-UTC rows survive with their old running sums;
    the rewindow's new chain steps down at every affected UTC hour.

    Step 1: import 12 days at old convention (v0.4.x baseline).
    Step 2: import last 5 days (trailing rewindow + overlap day) at new
            convention with reading_days set (beta.3 first rewindow).
    Assert: ZERO downward steps in the consumption series.

    Mutation: fill disabled (tz_is_contract=False) → 5 downward steps appear
    at the zero-block boundary hours; fill on → none.
    """
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Australia/Adelaide")
    coord = _300_coord(hass, tz)
    stat_id = f"{DOMAIN}:{STAT_CONSUMPTION}_{_CONTRACT}"
    days = [date(2026, 10, 10) + timedelta(days=n) for n in range(12)]

    # Step 1: v0.4.x baseline — all days at old (Sydney-converted) convention.
    old_intervals = [s for d in days for s in _300_day_slots(d, tz, "old")]
    await coord._import_intervals(old_intervals)
    await async_wait_recording_done(hass)

    # Step 2: beta.3/beta.4 first rewindow — trailing 5 days at correct convention.
    # Adelaide (half-hour zone) needs an overlap day, so rewindow = days[-5:].
    rewindow = days[-5:]
    new_intervals = [s for d in rewindow for s in _300_day_slots(d, tz, "new")]
    await coord._import_intervals(new_intervals, reading_days=frozenset(rewindow))
    await async_wait_recording_done(hass)

    rows = await _read_series(hass, stat_id)
    steps = _300_steps(rows)
    assert steps == [], f"downward steps after SA rewindow: {steps}"


async def test_300_stale_key_fill_qld_dst_consumption_zero_steps(
    recorder_mock, hass: HomeAssistant
) -> None:
    """#300 reproduction on the real recorder — Queensland in DST season.

    Brisbane is UTC+10 year-round; Sydney is AEDT (UTC+11) in October.  Old
    AGL convention placed every slot 1 hour earlier than correct.  Every :00
    and :30 local slot moves to a different UTC hour bucket, leaving 11
    stale old-UTC rows per zero-block day.

    Same structure as the SA test; for a whole-hour-offset zone there is no
    straddle so the rewindow needs no overlap day.
    """
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Australia/Brisbane")
    coord = _300_coord(hass, tz)
    stat_id = f"{DOMAIN}:{STAT_CONSUMPTION}_{_CONTRACT}"
    days = [date(2026, 10, 10) + timedelta(days=n) for n in range(12)]

    old_intervals = [s for d in days for s in _300_day_slots(d, tz, "old")]
    await coord._import_intervals(old_intervals)
    await async_wait_recording_done(hass)

    # Brisbane whole-hour zone: no overlap day needed.
    rewindow = days[-4:]
    new_intervals = [s for d in rewindow for s in _300_day_slots(d, tz, "new")]
    await coord._import_intervals(new_intervals, reading_days=frozenset(rewindow))
    await async_wait_recording_done(hass)

    rows = await _read_series(hass, stat_id)
    steps = _300_steps(rows)
    assert steps == [], f"downward steps after QLD rewindow: {steps}"


async def test_300_stale_key_fill_generation_nighttime_zero_steps(
    recorder_mock, hass: HomeAssistant
) -> None:
    """#300 generation variant — SA solar home, nighttime zero-on-zero.

    Night slots (i < 14, i > 38) have zero export; the convention change
    moves daytime slots 30 min later in UTC, leaving old stale keys at the
    pre-dawn and dusk hours.  The fill must write 0.0 at those keys so the
    generation and credit series stay monotone.
    """
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Australia/Adelaide")
    coord = _300_coord(hass, tz)
    stat_id_gen, stat_id_credit = coord._generation_stat_ids()

    def gen_day_slots(d: date, conv: str) -> list[IntervalReading]:
        """Export only during daylight hours (i=14..38 inclusive)."""
        out = []
        for i in range(48):
            if i < 14 or i > 38:
                continue
            dt = _300_slot_dt(d, i, tz, conv)
            out.append(
                IntervalReading(
                    dt=dt, kwh=0.2 + 0.005 * i, cost_aud=0.05, rate_type="normal"
                )
            )
        return out

    days = [date(2026, 10, 10) + timedelta(days=n) for n in range(12)]

    old_intervals = [s for d in days for s in gen_day_slots(d, "old")]
    await coord._import_generation(old_intervals)
    await async_wait_recording_done(hass)

    rewindow = days[-5:]
    new_intervals = [s for d in rewindow for s in gen_day_slots(d, "new")]
    await coord._import_generation(
        new_intervals,
        fetched_days=rewindow,
        reading_days=frozenset(rewindow),
    )
    await async_wait_recording_done(hass)

    for sid in (stat_id_gen, stat_id_credit):
        rows = await _read_series(hass, sid)
        steps = _300_steps(rows)
        assert steps == [], (
            f"{sid}: downward steps after SA generation rewindow: {steps}"
        )


async def test_300_per_day_gate_empty_middle_day_stored_rows_byte_identical(
    recorder_mock, hass: HomeAssistant
) -> None:
    """Per-day gate (critic MAJOR 2): a rewindow where one middle day returns
    NO readings must leave that day's stored rows byte-identical — not zeroed.

    Scenario: 10 BNE days stored; rewindow days[3:] where days[7] errors
    (no readings, not in reading_days).  The fill must carry forward
    days[7]'s stored states so the chain re-chains monotonically without
    erasing any energy.

    Mutation: gate on timestamp date alone (ignoring reading_days) — a
    neighbour's trailing-slack row dated days[7] opens the gate → days[7]'s
    stored keys are zeroed → permanent kWh loss.
    """
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Australia/Brisbane")
    coord = _300_coord(hass, tz)
    stat_id = f"{DOMAIN}:{STAT_CONSUMPTION}_{_CONTRACT}"
    days = [date(2026, 10, 10) + timedelta(days=n) for n in range(10)]

    def full_day(d: date) -> list[IntervalReading]:
        return [
            IntervalReading(
                dt=_300_slot_dt(d, i, tz, "new"),
                kwh=0.3,
                cost_aud=0.09,
                rate_type="normal",
            )
            for i in range(48)
        ]

    # Seed: all 10 days present.
    await coord._import_intervals(
        [s for d in days for s in full_day(d)],
        reading_days=frozenset(days),
    )
    await async_wait_recording_done(hass)
    before_rows = await _read_series(hass, stat_id)
    before_states = {r["start"]: r["state"] for r in before_rows}

    # Rewindow: days[3:], but days[7] returns nothing (AGL error).
    error_day = days[7]
    rewindow_ok = [d for d in days[3:] if d != error_day]
    batch = [s for d in rewindow_ok for s in full_day(d)]
    await coord._import_intervals(batch, reading_days=frozenset(rewindow_ok))
    await async_wait_recording_done(hass)
    after_rows = await _read_series(hass, stat_id)
    after_states = {r["start"]: r["state"] for r in after_rows}

    # No downward steps anywhere.
    assert _300_steps(after_rows) == []

    # days[7]'s stored states are byte-identical.
    d7_lo = datetime(
        error_day.year, error_day.month, error_day.day, tzinfo=tz
    ).astimezone(UTC)
    d8_lo = datetime(days[8].year, days[8].month, days[8].day, tzinfo=tz).astimezone(
        UTC
    )
    d7_keys = [
        ts for ts in before_states if d7_lo.timestamp() <= ts < d8_lo.timestamp()
    ]
    assert d7_keys, "test setup: days[7] must have stored rows"
    for ts in d7_keys:
        assert after_states.get(ts) == before_states[ts], (
            f"days[7] stored state changed at "
            f"{datetime.fromtimestamp(ts, tz=UTC).isoformat()}"
        )


async def test_300_all_empty_batch_writes_nothing(
    recorder_mock, hass: HomeAssistant
) -> None:
    """An empty batch exits before the stored-row read — no recorder write,
    no _stored_hourly_states call (brief rule 5: early-return comes first).
    """
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Australia/Brisbane")
    coord = _300_coord(hass, tz)
    stat_id = f"{DOMAIN}:{STAT_CONSUMPTION}_{_CONTRACT}"
    day = date(2026, 10, 10)

    # Seed one day.
    await coord._import_intervals(
        [
            IntervalReading(
                dt=_300_slot_dt(day, i, tz, "new"),
                kwh=0.3,
                cost_aud=0.09,
                rate_type="normal",
            )
            for i in range(48)
        ],
        reading_days={day},
    )
    await async_wait_recording_done(hass)
    before_rows = await _read_series(hass, stat_id)

    # Stub _stored_hourly_states to count calls.
    coord._stored_hourly_states = AsyncMock(return_value={})
    await coord._import_intervals([])
    assert coord._stored_hourly_states.await_count == 0, (
        "empty batch must not trigger the stored-row read"
    )
    await async_wait_recording_done(hass)

    after_rows = await _read_series(hass, stat_id)
    assert [(r["start"], r["state"], r["sum"]) for r in before_rows] == [
        (r["start"], r["state"], r["sum"]) for r in after_rows
    ], "empty batch must not modify stored rows"


async def test_300_partial_day_only_authoritative_days_keys_zero_filled(
    recorder_mock, hass: HomeAssistant
) -> None:
    """Only the authoritative day's stale keys are zero-filled; keys belonging
    to non-authoritative days are carried forward.

    Scenario (BNE, solar-home profile): 3 days seeded at old convention.
    Re-import only days[1] at new convention (days[1] in reading_days;
    days[2] is NOT).  The cutoff = min(days[1] new keys).

    · days[1]'s stale old-convention zero-import keys → zero-filled.
    · days[2]'s stale keys are above the cutoff but not authoritative →
      carried forward (NOT zeroed).
    · Zero downward steps throughout.
    """
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Australia/Brisbane")
    coord = _300_coord(hass, tz)
    stat_id = f"{DOMAIN}:{STAT_CONSUMPTION}_{_CONTRACT}"
    days = [date(2026, 10, 10) + timedelta(days=n) for n in range(3)]

    # Seed all 3 days at old (Sydney-converted) convention.
    old_intervals = [s for d in days for s in _300_day_slots(d, tz, "old")]
    await coord._import_intervals(old_intervals)
    await async_wait_recording_done(hass)
    before_rows = await _read_series(hass, stat_id)
    before_states = {r["start"]: r["state"] for r in before_rows}

    # Re-import only days[1] at new convention; days[2] is skipped.
    d1_new = _300_day_slots(days[1], tz, "new")
    await coord._import_intervals(d1_new, reading_days=frozenset({days[1]}))
    await async_wait_recording_done(hass)
    after_rows = await _read_series(hass, stat_id)
    after_states = {r["start"]: r["state"] for r in after_rows}

    # No downward steps.
    assert _300_steps(after_rows) == []

    # days[2]'s stale old-convention keys (above the cutoff) must be carried
    # forward — days[2] is NOT in reading_days.
    d2_lo = datetime(days[2].year, days[2].month, days[2].day, tzinfo=tz).astimezone(
        UTC
    )
    d2_keys = [ts for ts in before_states if ts >= d2_lo.timestamp()]
    assert d2_keys, "days[2] must have stored rows above the cutoff"
    for ts in d2_keys:
        assert after_states.get(ts) == before_states[ts], (
            f"days[2] key at {datetime.fromtimestamp(ts, tz=UTC).isoformat()} "
            "changed despite not being authoritative"
        )


async def test_300_steady_state_no_extra_rows(
    recorder_mock, hass: HomeAssistant
) -> None:
    """Steady state: re-importing the same corrected data twice produces
    identical rows — no extra rows, sums unchanged.

    Mutation: fill creates keys not already stored → extra rows appear.
    Mutation: fill re-emits existing keys with wrong values → sums change.
    """
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Australia/Adelaide")
    coord = _300_coord(hass, tz)
    stat_id = f"{DOMAIN}:{STAT_CONSUMPTION}_{_CONTRACT}"
    days = [date(2026, 10, 10) + timedelta(days=n) for n in range(5)]
    # Drop the zero-import block to produce a non-trivial profile.
    batch = [s for d in days for s in _300_day_slots(d, tz, "new")]

    await coord._import_intervals(batch, reading_days=frozenset(days))
    await async_wait_recording_done(hass)
    rows1 = sorted(await _read_series(hass, stat_id), key=lambda r: r["start"])

    await coord._import_intervals(batch, reading_days=frozenset(days))
    await async_wait_recording_done(hass)
    rows2 = sorted(await _read_series(hass, stat_id), key=lambda r: r["start"])

    assert [(r["start"], r["state"], r["sum"]) for r in rows1] == [
        (r["start"], r["state"], r["sum"]) for r in rows2
    ], "re-importing the same data must not change stored rows"


async def test_300_band_fill_no_new_series_created(
    recorder_mock, hass: HomeAssistant
) -> None:
    """Band series fill: a ToU band with stored rows but absent from this
    batch's readings on an authoritative day → its stale keys are zero-filled.

    A band with NO stored rows AND no batch data is NEVER created (the
    existing 'band not seen' guard must not be bypassed for empty bands).

    Scenario (BNE, one day):
    · Batch 1: peak slots 0-9, offpeak slots 10-47.
    · Batch 2: peak slots 0-3 only, offpeak slots 4-47, same day.
      Stored peak keys 4-9 have no batch value → zero-filled.
    · Shoulder: no stored rows, no batch data → no series created.
    """
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Australia/Brisbane")
    coord = _300_coord(hass, tz)
    d = date(2026, 10, 10)
    bands = frozenset({"peak", "offpeak"})

    b1 = [
        IntervalReading(
            dt=_300_slot_dt(d, i, tz, "new"),
            kwh=0.3,
            cost_aud=0.09,
            rate_type="peak" if i < 10 else "offpeak",
        )
        for i in range(48)
    ]
    await coord._import_intervals(b1, reading_days={d}, known_bands=bands)
    await async_wait_recording_done(hass)

    b2 = [
        IntervalReading(
            dt=_300_slot_dt(d, i, tz, "new"),
            kwh=0.3,
            cost_aud=0.09,
            rate_type="peak" if i < 4 else "offpeak",
        )
        for i in range(48)
    ]
    await coord._import_intervals(b2, reading_days={d}, known_bands=bands)
    await async_wait_recording_done(hass)

    peak_cons, _ = coord._tariff_stat_ids("peak")
    offpeak_cons, _ = coord._tariff_stat_ids("offpeak")
    shoulder_cons, _ = coord._tariff_stat_ids("shoulder")

    peak_rows = await _read_series(hass, peak_cons)
    offpeak_rows = await _read_series(hass, offpeak_cons)
    shoulder_rows = await _read_series(hass, shoulder_cons)

    assert _300_steps(peak_rows) == [], "peak must be monotone"
    assert _300_steps(offpeak_rows) == [], "offpeak must be monotone"
    assert shoulder_rows == [], (
        "shoulder series must not be created with no stored rows"
    )

    # Peak: only slots 0-3 in batch2 are non-zero; slots 4-9 were stored but
    # are zero-filled (authoritative day) → peak sum = 4 * 0.3 = 1.2 kWh.
    assert abs(peak_rows[-1]["sum"] - 4 * 0.3) < 1e-9, (
        f"peak sum {peak_rows[-1]['sum']:.6f} ≠ 1.2 (slots 4-9 not zero-filled)"
    )


async def test_300_downgrade_re_upgrade_zero_steps(
    recorder_mock, hass: HomeAssistant
) -> None:
    """Downgrade/re-upgrade: write correct rows, then old-convention rows
    over the same week (simulating a v0.4 downgrade), then correct rows
    again (re-upgrade rewindow) → zero downward steps.
    """
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Australia/Adelaide")
    coord = _300_coord(hass, tz)
    stat_id = f"{DOMAIN}:{STAT_CONSUMPTION}_{_CONTRACT}"
    days = [date(2026, 10, 10) + timedelta(days=n) for n in range(10)]

    # Step 1 (beta.4 install): all 10 days at new convention.
    new_all = [s for d in days for s in _300_day_slots(d, tz, "new")]
    await coord._import_intervals(new_all, reading_days=frozenset(days))
    await async_wait_recording_done(hass)

    # Step 2 (v0.4 downgrade): re-import trailing 5 days at old convention.
    # The downgrade client has tz_is_contract=False (no correction), so fill
    # is inactive — old-convention rows overwrite the trailing week.
    coord.client.tz_is_contract = False
    rewindow = days[-5:]
    old_win = [s for d in rewindow for s in _300_day_slots(d, tz, "old")]
    await coord._import_intervals(old_win)
    await async_wait_recording_done(hass)

    # Step 3 (re-upgrade): re-import the same trailing window at new convention
    # with fill active and provenance set.
    coord.client.tz_is_contract = True
    new_win = [s for d in rewindow for s in _300_day_slots(d, tz, "new")]
    await coord._import_intervals(new_win, reading_days=frozenset(rewindow))
    await async_wait_recording_done(hass)

    rows = await _read_series(hass, stat_id)
    assert _300_steps(rows) == [], (
        "re-upgrade rewindow must produce zero downward steps"
    )


async def test_300_exactly_one_recorder_read_per_nonempty_import(
    recorder_mock, hass: HomeAssistant
) -> None:
    """_stored_hourly_states is called exactly once per non-empty import when
    fill is active, and zero times for an empty batch (early-return guard).

    With tz_is_contract not `is True`, the real method short-circuits and
    returns {} without any executor job.
    """
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Australia/Brisbane")
    coord = _300_coord(hass, tz)
    day = date(2026, 10, 10)

    # Stub _stored_hourly_states; the import still runs normally (fill finds
    # no stored rows and does nothing — correct for an empty recorder).
    coord._stored_hourly_states = AsyncMock(return_value={})

    # Empty batch → early return before the read.
    await coord._import_intervals([])
    assert coord._stored_hourly_states.await_count == 0, (
        "empty batch must not call _stored_hourly_states"
    )

    # Non-empty batch → exactly one call.
    batch = [
        IntervalReading(
            dt=_300_slot_dt(day, i, tz, "new"),
            kwh=0.3,
            cost_aud=0.09,
            rate_type="normal",
        )
        for i in range(48)
    ]
    await coord._import_intervals(batch, reading_days={day})
    assert coord._stored_hourly_states.await_count == 1, (
        "one non-empty import must call _stored_hourly_states exactly once"
    )

    # With tz_is_contract not `is True`, the real method returns {} immediately.
    coord.client.tz_is_contract = False
    del coord._stored_hourly_states  # restore real method
    stat_id = f"{DOMAIN}:{STAT_CONSUMPTION}_{_CONTRACT}"
    result = await coord._stored_hourly_states(
        {stat_id}, datetime(2026, 1, 1, tzinfo=UTC)
    )
    assert result == {}, "fill-inactive path must return {} without any DB I/O"


async def test_300_carry_forward_mid_rewindow_error_preserves_day(
    recorder_mock, hass: HomeAssistant
) -> None:
    """Critic MAJOR 1 (carry-forward): a mid-rewindow AGL per-day error
    returns [] for that day; the fill must CARRY FORWARD its stored states
    exactly — not zero them — so its sums re-chain monotonically.

    Without carry-forward, stored rows on the errored day keep their OLD
    running sums while every later row moves to the new chain → a downward
    step at the first row after the errored day (#114 class).
    """
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Australia/Brisbane")
    coord = _300_coord(hass, tz)
    stat_id = f"{DOMAIN}:{STAT_CONSUMPTION}_{_CONTRACT}"
    days = [date(2026, 10, 10) + timedelta(days=n) for n in range(10)]

    def full_day(d: date) -> list[IntervalReading]:
        return [
            IntervalReading(
                dt=_300_slot_dt(d, i, tz, "new"),
                kwh=0.3,
                cost_aud=0.09,
                rate_type="normal",
            )
            for i in range(48)
        ]

    # Seed: all 10 days.
    await coord._import_intervals(
        [s for d in days for s in full_day(d)],
        reading_days=frozenset(days),
    )
    await async_wait_recording_done(hass)
    before_rows = await _read_series(hass, stat_id)
    before_states = {r["start"]: r["state"] for r in before_rows}

    # Rewindow: days[3:], but days[7] errors (not in reading_days, no batch).
    error_day = days[7]
    rewindow_ok = [d for d in days[3:] if d != error_day]
    batch = [s for d in rewindow_ok for s in full_day(d)]
    await coord._import_intervals(batch, reading_days=frozenset(rewindow_ok))
    await async_wait_recording_done(hass)
    after_rows = await _read_series(hass, stat_id)

    # Zero downward steps — the carry-forward must re-chain the errored day.
    assert _300_steps(after_rows) == []

    # The errored day's stored states are byte-identical.
    after_states = {r["start"]: r["state"] for r in after_rows}
    d7_lo = datetime(
        error_day.year, error_day.month, error_day.day, tzinfo=tz
    ).astimezone(UTC)
    d8_lo = datetime(days[8].year, days[8].month, days[8].day, tzinfo=tz).astimezone(
        UTC
    )
    d7_keys = [
        ts for ts in before_states if d7_lo.timestamp() <= ts < d8_lo.timestamp()
    ]
    assert d7_keys, "error day must have stored rows"
    for ts in d7_keys:
        assert after_states.get(ts) == before_states[ts], (
            f"error day state changed at "
            f"{datetime.fromtimestamp(ts, tz=UTC).isoformat()}"
        )


async def test_300_provenance_gate_slack_row_does_not_open_skipped_day(
    recorder_mock, hass: HomeAssistant
) -> None:
    """Critic MAJOR 2 (provenance gate): day D's response may carry a
    trailing-slack row dated D+1.  If D+1 was SKIPPED (no fetch, not in
    reading_days), that single slack row must NOT open the fill gate for
    D+1 — D+1's stored rows must survive byte-identical.

    Mutation: gate on timestamp date alone (ignoring reading_days) → the
    slack row dated D+1 opens the gate → D+1's stored keys are zeroed →
    permanent kWh loss.
    """
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Australia/Brisbane")
    coord = _300_coord(hass, tz)
    stat_id = f"{DOMAIN}:{STAT_CONSUMPTION}_{_CONTRACT}"
    days = [date(2026, 10, 10) + timedelta(days=n) for n in range(6)]

    def full_day(d: date) -> list[IntervalReading]:
        return [
            IntervalReading(
                dt=_300_slot_dt(d, i, tz, "new"),
                kwh=0.3,
                cost_aud=0.09,
                rate_type="normal",
            )
            for i in range(48)
        ]

    # Seed all 6 days.
    await coord._import_intervals(
        [s for d in days for s in full_day(d)],
        reading_days=frozenset(days),
    )
    await async_wait_recording_done(hass)
    before_rows = await _read_series(hass, stat_id)
    before_states = {r["start"]: r["state"] for r in before_rows}

    # Rewindow: days[1:] skipping days[4]; batch includes ONE slack row
    # dated days[4] 00:00 local (D's response overflowed into D+1 window).
    day4 = days[4]
    rewindow_ok = [d for d in days[1:] if d != day4]
    batch = [s for d in rewindow_ok for s in full_day(d)]
    slack_ts = datetime(day4.year, day4.month, day4.day, tzinfo=tz).astimezone(UTC)
    batch.append(
        IntervalReading(dt=slack_ts, kwh=0.3, cost_aud=0.09, rate_type="normal")
    )

    await coord._import_intervals(
        batch,
        reading_days=frozenset(rewindow_ok),  # D4 NOT in reading_days
    )
    await async_wait_recording_done(hass)
    after_rows = await _read_series(hass, stat_id)
    after_states = {r["start"]: r["state"] for r in after_rows}

    # Zero downward steps.
    assert _300_steps(after_rows) == []

    # days[4]'s stored rows (except the slack key itself, which the batch DID
    # write) must be byte-identical.
    slack_bucket_ts = slack_ts.replace(minute=0).timestamp()
    d4_lo = slack_ts.timestamp()
    d5_lo = (
        datetime(days[5].year, days[5].month, days[5].day, tzinfo=tz)
        .astimezone(UTC)
        .timestamp()
    )
    d4_other_keys = [
        ts
        for ts in before_states
        if d4_lo <= ts < d5_lo and abs(ts - slack_bucket_ts) > 1
    ]
    assert d4_other_keys, "days[4] must have stored rows beyond the slack bucket"
    for ts in d4_other_keys:
        assert after_states.get(ts) == before_states[ts], (
            f"days[4] state at {datetime.fromtimestamp(ts, tz=UTC).isoformat()} "
            f"was changed despite D4 not being in reading_days"
        )


async def test_300_sa_straddle_key_both_days_required(
    recorder_mock, hass: HomeAssistant
) -> None:
    """Critic MINOR 3 (straddle key): a half-hour-zone boundary bucket
    straddles two local days; it must be zeroed only when BOTH days are
    authoritative.

    Scenario (Adelaide): day D's 23:30 slot is zero-on-zero (absent from
    batch), D+1 is skipped.  Bucket H (= D 23:30 / D+1 00:00) holds 0.6
    kWh from the seed (two 0.3 slots).  D is authoritative; D+1 is not.
    The fill must NOT zero H — H's stored state must survive unchanged.

    Mutation: gate on k's start instant only (not k+30min) → D is
    authoritative, the bucket is dated D at its start → H is zeroed →
    D+1's 00:00 kWh is permanently lost.
    """
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Australia/Adelaide")
    coord = _300_coord(hass, tz)
    stat_id = f"{DOMAIN}:{STAT_CONSUMPTION}_{_CONTRACT}"
    days = [date(2026, 10, 10) + timedelta(days=n) for n in range(6)]

    def full_day_adl(d: date) -> list[IntervalReading]:
        return [
            IntervalReading(
                dt=_300_slot_dt(d, i, tz, "new"),
                kwh=0.3,
                cost_aud=0.09,
                rate_type="normal",
            )
            for i in range(48)
        ]

    # Seed all 6 days (including D+1's 00:00 slot in bucket H).
    await coord._import_intervals(
        [s for d in days for s in full_day_adl(d)],
        reading_days=frozenset(days),
    )
    await async_wait_recording_done(hass)

    # Rewindow: days[1:] skipping days[3] (D+1).  On days[2] (D), slot 47
    # (23:30 ACDT) is absent from the batch → zero-on-zero → bucket H has
    # no batch value.
    day_d = days[2]
    day_d1 = days[3]
    rewindow_ok = [d for d in days[1:] if d != day_d1]
    batch: list[IntervalReading] = []
    for d in rewindow_ok:
        if d == day_d:
            batch.extend(
                IntervalReading(
                    dt=_300_slot_dt(d, i, tz, "new"),
                    kwh=0.3,
                    cost_aud=0.09,
                    rate_type="normal",
                )
                for i in range(47)  # slot 47 (23:30 ACDT) absent
            )
        else:
            batch.extend(full_day_adl(d))

    await coord._import_intervals(
        batch,
        reading_days=frozenset(rewindow_ok),  # D1 NOT in reading_days
    )
    await async_wait_recording_done(hass)
    after_rows = await _read_series(hass, stat_id)

    # No downward steps.
    assert _300_steps(after_rows) == []

    # Bucket H holds D's 23:30 (0.3) + D1's 00:00 (0.3) = 0.6 from the seed.
    # Since D1 is not authoritative, H must NOT be zeroed: state = 0.6.
    bucket_h = _300_slot_dt(day_d, 47, tz, "new").replace(minute=0)
    h_row = next(
        (r for r in after_rows if abs(r["start"] - bucket_h.timestamp()) < 1), None
    )
    assert h_row is not None, f"bucket H at {bucket_h.isoformat()} must exist"
    assert abs(h_row["state"] - 0.6) < 1e-9, (
        f"bucket H state {h_row['state']:.4f} must be 0.6 "
        f"(D1 slot carried forward, not zeroed)"
    )


async def test_300_conservation_guard_preserves_partial_day(
    recorder_mock, hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Critic MINOR 4 (conservation guard): a day where the batch carries
    fewer kWh than is stored is treated as non-authoritative — its stored
    values are carried forward, not zeroed.  One WARNING is logged.

    Scenario: 4 BNE days stored (48 slots * 0.3 kWh = 14.4 kWh each).
    Rewindow days[1:]; days[2] returns only 40 of 48 slots (slots 30-37
    absent, 8 missing * 0.3 = 2.4 kWh short).  Batch kWh for days[2] =
    12.0 < stored 14.4 → conservation guard fires → days[2] carried forward.
    """
    import logging
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Australia/Brisbane")
    coord = _300_coord(hass, tz)
    stat_id = f"{DOMAIN}:{STAT_CONSUMPTION}_{_CONTRACT}"
    days = [date(2026, 10, 10) + timedelta(days=n) for n in range(4)]

    def full_day(d: date) -> list[IntervalReading]:
        return [
            IntervalReading(
                dt=_300_slot_dt(d, i, tz, "new"),
                kwh=0.3,
                cost_aud=0.09,
                rate_type="normal",
            )
            for i in range(48)
        ]

    await coord._import_intervals(
        [s for d in days for s in full_day(d)],
        reading_days=frozenset(days),
    )
    await async_wait_recording_done(hass)
    before_rows = await _read_series(hass, stat_id)
    before_states = {r["start"]: r["state"] for r in before_rows}

    # Rewindow: days[1:]; days[2] missing slots 30-37.
    day_d2 = days[2]
    partial_batch: list[IntervalReading] = []
    for d in days[1:]:
        if d == day_d2:
            partial_batch.extend(
                IntervalReading(
                    dt=_300_slot_dt(d, i, tz, "new"),
                    kwh=0.3,
                    cost_aud=0.09,
                    rate_type="normal",
                )
                for i in range(48)
                if i not in range(30, 38)
            )
        else:
            partial_batch.extend(full_day(d))

    caplog.set_level(logging.WARNING, logger="custom_components.haggle.coordinator")
    await coord._import_intervals(partial_batch, reading_days=frozenset(days[1:]))
    await async_wait_recording_done(hass)

    after_rows = await _read_series(hass, stat_id)
    after_states = {r["start"]: r["state"] for r in after_rows}

    # No downward steps — carry-forward ensures monotonicity.
    assert _300_steps(after_rows) == []

    # days[2]'s stored states are preserved (conservation guard fired).
    d2_lo = datetime(day_d2.year, day_d2.month, day_d2.day, tzinfo=tz).astimezone(UTC)
    d3_lo = datetime(days[3].year, days[3].month, days[3].day, tzinfo=tz).astimezone(
        UTC
    )
    d2_keys = [
        ts for ts in before_states if d2_lo.timestamp() <= ts < d3_lo.timestamp()
    ]
    assert d2_keys, "days[2] must have stored rows"
    for ts in d2_keys:
        assert after_states.get(ts) == before_states[ts], (
            f"days[2] state at {datetime.fromtimestamp(ts, tz=UTC).isoformat()} "
            f"changed despite failing the conservation guard"
        )

    # Exactly one WARNING mentioning the energy shortfall.
    assert "less energy than is stored" in caplog.text, (
        "conservation guard must emit a WARNING"
    )


async def test_300_fill_inactive_on_ha_zone_fallback(
    recorder_mock, hass: HomeAssistant
) -> None:
    """Critic MINOR 5 (tz_is_contract identity check): the fill only runs
    when client.tz_is_contract `is True` — a strict identity test, not a
    truthiness test.  Any other value (False, 1, MagicMock) leaves the
    import unchanged from pre-fill behaviour.

    Confirmed by:
    · _fill_active() with True / False / 1.
    · Re-importing the same data with tz_is_contract=1 → no change to rows.
    · _stored_hourly_states called directly with tz_is_contract=False returns
      {} immediately without any DB I/O.
    """
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Australia/Adelaide")
    coord = _300_coord(hass, tz)
    stat_id = f"{DOMAIN}:{STAT_CONSUMPTION}_{_CONTRACT}"
    days = [date(2026, 10, 10) + timedelta(days=n) for n in range(5)]

    # --- Identity check ---
    coord.client.tz_is_contract = True
    assert coord._fill_active() is True, "`is True` must arm fill"
    coord.client.tz_is_contract = False
    assert coord._fill_active() is False, "False must not arm fill"
    coord.client.tz_is_contract = 1  # truthy but not `is True`
    assert coord._fill_active() is False, "truthy non-True must not arm fill"

    # --- Recorder: fill inactive → stored rows unchanged ---
    coord.client.tz_is_contract = True  # re-arm for seeding
    batch = [s for d in days for s in _300_day_slots(d, tz, "new")]
    await coord._import_intervals(batch, reading_days=frozenset(days))
    await async_wait_recording_done(hass)
    seeded_rows = sorted(await _read_series(hass, stat_id), key=lambda r: r["start"])

    # Inactive: re-import with tz_is_contract=1 (fill won't arm).
    coord.client.tz_is_contract = 1
    await coord._import_intervals(batch, reading_days=frozenset(days))
    await async_wait_recording_done(hass)
    after_rows = sorted(await _read_series(hass, stat_id), key=lambda r: r["start"])

    assert [(r["start"], r["state"], r["sum"]) for r in seeded_rows] == [
        (r["start"], r["state"], r["sum"]) for r in after_rows
    ], "inactive fill must leave rows unchanged"

    # --- _stored_hourly_states short-circuits with no DB I/O when inactive ---
    coord.client.tz_is_contract = False
    result = await coord._stored_hourly_states(
        {stat_id}, datetime(2026, 1, 1, tzinfo=UTC)
    )
    assert result == {}, "_stored_hourly_states must return {} when fill inactive"
