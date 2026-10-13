"""User-configured ToU windows (#141) on the REAL recorder (``recorder_mock``).

docs/testing.md makes the real statistics engine mandatory for any change to
the cumulative-sum chain; #141 changes which band series a slot's kWh lands
in, so every acceptance criterion that is about stored rows is pinned here:

- AC5 golden: a never-configured entry writes exactly what v0.5.0 wrote.
  ``tests/fixtures/golden_legacy_v050.json`` was generated from the
  v0.5.0-beta.5 code (e8b7a73) BEFORE any #141 change, by running this
  module with ``HAGGLE_REGEN_GOLDEN=1``. The scenario includes AGL type
  flips and a trailing-slack reading on an errored day, so a legacy-path
  change (the partition-exact band fill, the BAND_VOCAB union, a widened
  legacy gate) moves at least one row.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

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
)
from custom_components.haggle.coordinator import HaggleCoordinator
from tests.test_recorder_statistics import _read_series

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


@pytest.fixture(autouse=True)
def _auto_enable_custom_integrations(
    recorder_mock, enable_custom_integrations: None
) -> None:
    """Recorder fixtures must initialise before hass (see
    test_recorder_statistics.py for the ordering rationale)."""


_CONTRACT = "9999999999"
_GOLDEN = Path(__file__).parent / "fixtures" / "golden_legacy_v050.json"
# Every series id a legacy or derived import could touch, so a mutation that
# CREATES a series (e.g. a superoffpeak band in legacy mode) is caught too.
_ALL_BANDS = ("peak", "offpeak", "shoulder", "normal", "superoffpeak")


def _coord(
    hass: HomeAssistant,
    tz: ZoneInfo,
    *,
    options: dict | None = None,
    data: dict | None = None,
) -> HaggleCoordinator:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_REFRESH_TOKEN: "v1.testtoken",
            CONF_CONTRACT_NUMBER: _CONTRACT,
            CONF_ACCOUNT_NUMBER: "1234567890",
            **(data or {}),
        },
        options=options or {},
        unique_id="1234567890_9999999999",
    )
    entry.add_to_hass(hass)
    coord = HaggleCoordinator(hass, entry, AsyncMock(), _CONTRACT)
    coord.client.local_tz = tz
    coord.client.tz_is_contract = True
    return coord


def _all_ids() -> list[str]:
    ids = [f"{DOMAIN}:consumption_{_CONTRACT}", f"{DOMAIN}:cost_{_CONTRACT}"]
    for band in _ALL_BANDS:
        ids += [
            f"{DOMAIN}:consumption_{band}_{_CONTRACT}",
            f"{DOMAIN}:cost_{band}_{_CONTRACT}",
        ]
    return ids


# --- AC5 golden ------------------------------------------------------------

_BNE = ZoneInfo("Australia/Brisbane")  # no DST: offsets stable at any date


def _golden_type(i: int) -> str:
    if 30 <= i < 42:
        return "peak"
    if i < 14:
        return "offpeak"
    return "normal"


def _golden_slot(day: date, i: int, rate_type: str) -> IntervalReading:
    start = datetime(day.year, day.month, day.day, tzinfo=_BNE).astimezone(UTC)
    kwh = 0.1 + 0.01 * (i % 7)
    rate = {"peak": 0.5, "offpeak": 0.2, "shoulder": 0.3}.get(rate_type, 0.3)
    return IntervalReading(
        dt=start + timedelta(minutes=30 * i),
        kwh=kwh,
        cost_aud=round(kwh * rate, 6),
        rate_type=rate_type,
    )


def _golden_day(day: date, flip: dict[int, str] | None = None) -> list[IntervalReading]:
    flip = flip or {}
    return [_golden_slot(day, i, flip.get(i, _golden_type(i))) for i in range(48)]


async def _golden_scenario(hass: HomeAssistant) -> tuple[datetime, HaggleCoordinator]:
    """Four legacy imports on a never-configured entry (fill active)."""
    today = datetime.now(_BNE).date()
    d0 = today - timedelta(days=12)
    coord = _coord(hass, _BNE)
    base = datetime(d0.year, d0.month, d0.day, tzinfo=_BNE).astimezone(UTC)

    # A: four clean typed days (peak opens the legacy gate from the batch).
    days_a = [d0 + timedelta(days=n) for n in range(4)]
    await coord._import_intervals(
        [r for d in days_a for r in _golden_day(d)],
        known_bands=frozenset(),
        reading_days=set(days_a),
    )
    await async_wait_recording_done(hass)

    # B: a rewindow over D0+1..D0+2 with AGL TYPE FLIPS (peak->normal,
    # normal->shoulder), and D0+3's own fetch ERRORED — but D0+2's response
    # carried D0+3's first two slots as trailing slack, re-typed offpeak->peak.
    d1, d2, d3 = d0 + timedelta(days=1), d0 + timedelta(days=2), d0 + timedelta(days=3)
    batch_b = (
        _golden_day(d1, {30: "normal", 31: "normal", 32: "normal", 33: "normal"})
        + _golden_day(d2, {20: "shoulder", 21: "shoulder"})
        + [_golden_slot(d3, 0, "peak"), _golden_slot(d3, 1, "peak")]
    )
    known = frozenset(await coord._get_stored_tou_bands())
    await coord._import_intervals(batch_b, known_bands=known, reading_days={d1, d2})
    await async_wait_recording_done(hass)

    # C: later all-"normal" days; the stored peak rows keep the gate open,
    # so the normal band is written and the others are re-chained empty.
    days_c = [d0 + timedelta(days=5), d0 + timedelta(days=6)]
    known = frozenset(await coord._get_stored_tou_bands())
    await coord._import_intervals(
        [r for d in days_c for r in _golden_day(d, dict.fromkeys(range(48), "normal"))],
        known_bands=known,
        reading_days=set(days_c),
    )
    await async_wait_recording_done(hass)

    # D: gate CLOSED (no known bands, batch all normal): aggregate only.
    d8 = d0 + timedelta(days=8)
    await coord._import_intervals(
        _golden_day(d8, dict.fromkeys(range(48), "normal")),
        known_bands=frozenset(),
        reading_days={d8},
    )
    await async_wait_recording_done(hass)
    return base, coord


async def _snapshot(
    hass: HomeAssistant, base: datetime
) -> dict[str, list[list[float]]]:
    snap: dict[str, list[list[float]]] = {}
    for stat_id in _all_ids():
        rows = await _read_series(hass, stat_id)
        key = stat_id.removeprefix(f"{DOMAIN}:").removesuffix(f"_{_CONTRACT}")
        snap[key] = [
            [
                (datetime.fromtimestamp(row["start"], tz=UTC) - base).total_seconds()
                / 3600,
                round(row["state"], 9),
                round(row["sum"], 9),
            ]
            for row in rows
        ]
    return snap


async def test_ac5_never_configured_entry_matches_v050_golden(
    recorder_mock, hass: HomeAssistant
) -> None:
    """AC5: a never-configured entry writes exactly what v0.5.0 wrote.

    The snapshot covers every consumption/cost series of every band in
    BAND_VOCAB (so a series CREATED by a legacy-path change fails too), with
    state and sum per hour. Not vacuous: import B's trailing-slack slots on
    the errored day D0+3 are re-typed offpeak->peak, which v0.5.0 handles by
    carrying the stored offpeak row forward (D0+3 is not authoritative) — the
    partition-exact band fill would zero it; import D's closed gate would
    open under a widened legacy gate (stored normal / BAND_VOCAB).
    """
    base, _ = await _golden_scenario(hass)
    snap = await _snapshot(hass, base)
    if os.environ.get("HAGGLE_REGEN_GOLDEN") == "1":
        _GOLDEN.write_text(json.dumps(snap, indent=0, sort_keys=True) + "\n")
    golden = json.loads(_GOLDEN.read_text())
    assert sorted(snap) == sorted(golden)
    for key, rows in golden.items():
        got = snap[key]
        assert len(got) == len(rows), (key, len(got), len(rows))
        for want, have in zip(rows, got, strict=True):
            assert want[0] == have[0], (key, want, have)
            assert abs(want[1] - have[1]) < 1e-9, (key, want, have)
            assert abs(want[2] - have[2]) < 1e-9, (key, want, have)


# --- configured-mode helpers -------------------------------------------------

_SYD = ZoneInfo("Australia/Sydney")
_ADL = ZoneInfo("Australia/Adelaide")

# The #126 Ausgrid bill: peak 15-21, shoulder 10-14, off-peak the rest.
_BILL_126 = [
    {"band": "peak", "start": "15:00", "end": "21:00", "days": "all"},
    {"band": "shoulder", "start": "10:00", "end": "14:00", "days": "all"},
]


def _opts(windows: list[dict], residual: str = "offpeak") -> dict:
    return {
        "tariff_bands": {
            "version": 1,
            "mode": "windows",
            "residual": residual,
            "windows": windows,
        }
    }


_FLAT = {"tariff_bands": {"version": 1, "mode": "flat"}}


def _configure(
    hass: HomeAssistant, coord: HaggleCoordinator, options: dict | None
) -> None:
    """Save options and take the cycle snapshot, as _fetch_and_import does."""
    hass.config_entries.async_update_entry(coord.config_entry, options=options or {})
    coord._band_plan = coord._resolve_band_plan()


def _local_midnight(day: date, tz: ZoneInfo) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=tz).astimezone(UTC)


def _day_slots(
    day: date,
    tz: ZoneInfo,
    rate_type: str | dict[int, str] = "normal",
    *,
    scale: float = 1.0,
) -> list[IntervalReading]:
    """Every corrected slot of one contract-local day (46/48/50 on DST days)."""
    start = _local_midnight(day, tz)
    end = _local_midnight(day + timedelta(days=1), tz)
    out: list[IntervalReading] = []
    i = 0
    while start + timedelta(minutes=30 * i) < end:
        kwh = round((0.1 + 0.01 * (i % 7) + 0.001 * day.day) * scale, 6)
        typ = rate_type if isinstance(rate_type, str) else rate_type.get(i, "normal")
        out.append(
            IntervalReading(
                dt=start + timedelta(minutes=30 * i),
                kwh=kwh,
                cost_aud=round(kwh * (0.25 + 0.01 * (i % 3)), 6),
                rate_type=typ,
            )
        )
        i += 1
    return out


def _days(first: date, n: int) -> list[date]:
    return [first + timedelta(days=k) for k in range(n)]


def _hour(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


def _sid(kind: str, band: str | None = None) -> str:
    return (
        f"{DOMAIN}:{kind}_{band}_{_CONTRACT}"
        if band
        else f"{DOMAIN}:{kind}_{_CONTRACT}"
    )


async def _rows(
    hass: HomeAssistant, stat_id: str
) -> dict[datetime, tuple[float, float]]:
    return {
        datetime.fromtimestamp(row["start"], tz=UTC): (row["state"], row["sum"])
        for row in await _read_series(hass, stat_id)
    }


async def _all_rows(hass: HomeAssistant) -> dict[str, dict[datetime, tuple]]:
    return {sid: await _rows(hass, sid) for sid in _all_ids()}


async def _import(
    hass: HomeAssistant,
    coord: HaggleCoordinator,
    readings: list[IntervalReading],
    reading_days: set[date] | None,
    known: frozenset[str] = frozenset(),
) -> None:
    await coord._import_intervals(
        readings, known_bands=known, reading_days=reading_days
    )
    await async_wait_recording_done(hass)


def _assert_chains(snapshot: dict[str, dict[datetime, tuple]]) -> None:
    """Every series: no downward step, and every row is the previous sum plus
    its own state (the chain was re-built, not left with an old sum)."""
    for stat_id, rows in snapshot.items():
        ordered = [rows[k] for k in sorted(rows)]
        for (_, prev_sum), (state, cur_sum) in pairwise(ordered):
            assert state >= -1e-12, (stat_id, state)
            assert cur_sum >= prev_sum - 1e-9, f"downward step in {stat_id}"
            assert abs(cur_sum - prev_sum - state) < 1e-9, (stat_id, prev_sum, cur_sum)


def _assert_partition(
    snapshot: dict[str, dict[datetime, tuple]], keys: set[datetime]
) -> None:
    """Band consumption AND cost states sum to the aggregate at every key."""
    for kind in ("consumption", "cost"):
        agg = snapshot[_sid(kind)]
        for key in keys:
            bands = sum(
                snapshot[_sid(kind, band)].get(key, (0.0, 0.0))[0]
                for band in _ALL_BANDS
            )
            assert abs(bands - agg[key][0]) < 1e-9, (kind, key, bands, agg[key][0])


def _batch_keys(
    readings: list[IntervalReading], trim_first: bool = False
) -> set[datetime]:
    keys = {_hour(r.dt) for r in readings}
    if trim_first:
        keys.discard(min(keys))
    return keys


def _oracle_126(dt: datetime, tz: ZoneInfo) -> str:
    hour = dt.astimezone(tz).hour
    if 15 <= hour < 21:
        return "peak"
    if 10 <= hour < 14:
        return "shoulder"
    if 1 <= hour < 3:
        return "superoffpeak"
    return "offpeak"


# --- AC2: partition on configured imports -------------------------------------


@pytest.mark.parametrize(
    ("dst_day", "slots"),
    [(date(2027, 4, 4), 50), (date(2026, 10, 4), 46)],
    ids=["fall-back", "spring-forward"],
)
async def test_ac2_partition_sydney_dst_days(
    recorder_mock, hass: HomeAssistant, dst_day: date, slots: int
) -> None:
    """AC1+AC2 on Sydney's DST transition days, on the real recorder.

    Windows peak 15-21, shoulder 10-14, superoffpeak 01-03 (spans the
    fall-back fold and the spring-forward gap), off-peak residual. AGL's
    type is a constant "peak" — it must have no influence (AC1). At every
    aggregate key the band states (kWh and cost) sum to the aggregate, and
    each band's kWh at each key equals an independent wall-clock oracle.
    """
    coord = _coord(hass, _SYD)
    _configure(
        hass,
        coord,
        _opts(
            [
                *_BILL_126,
                {
                    "band": "superoffpeak",
                    "start": "01:00",
                    "end": "03:00",
                    "days": "all",
                },
            ]
        ),
    )
    days = _days(dst_day - timedelta(days=1), 3)
    batch = [r for d in days for r in _day_slots(d, _SYD, "peak")]
    assert len(_day_slots(dst_day, _SYD)) == slots
    await _import(hass, coord, batch, set(days))

    snap = await _all_rows(hass)
    _assert_chains(snap)
    _assert_partition(snap, _batch_keys(batch))
    oracle: dict[tuple[str, datetime], float] = {}
    for r in batch:
        key = (_oracle_126(r.dt, _SYD), _hour(r.dt))
        oracle[key] = oracle.get(key, 0.0) + r.kwh
    for band in ("peak", "shoulder", "superoffpeak", "offpeak"):
        rows = snap[_sid("consumption", band)]
        want = {k: v for (b, k), v in oracle.items() if b == band}
        assert set(rows) == set(want), band
        for key, kwh in want.items():
            assert abs(rows[key][0] - kwh) < 1e-9, (band, key)
    # AGL tagged every slot "peak"; none of that leaks into the split.
    assert not snap[_sid("consumption", "normal")]
    # The fold (fall-back) / gap (spring-forward) slots: both 02:xx folds are
    # superoffpeak; spring-forward has only 01:00-02:00 local of it.
    sop = [r for r in batch if _oracle_126(r.dt, _SYD) == "superoffpeak"]
    # 3 days x 4 slots, +2 repeated folds / -2 skipped slots on the DST day.
    assert len(sop) == {50: 3 * 4 + 2, 46: 3 * 4 - 2}[slots]


async def test_ac2_partition_adelaide_half_hour_zone(
    recorder_mock, hass: HomeAssistant
) -> None:
    """AC2 in a half-hour zone: local 15:00 ACST is 05:30Z, so key 05:00Z
    mixes the residual (14:30) and peak (15:00); and a wrapping off-peak
    window 22:00-07:00 with an Anytime residual. The A4 straddle trim drops
    the batch's first bucket in EVERY band series too."""
    coord = _coord(hass, _ADL)
    _configure(
        hass,
        coord,
        _opts(
            [
                {"band": "peak", "start": "15:00", "end": "21:00", "days": "all"},
                {"band": "offpeak", "start": "22:00", "end": "07:00", "days": "all"},
            ],
            residual="normal",
        ),
    )
    days = _days(date(2026, 7, 6), 3)
    batch = [r for d in days for r in _day_slots(d, _ADL)]
    assert batch[0].dt.minute == 30  # local midnight is :30 past the UTC hour
    await _import(hass, coord, batch, set(days))

    snap = await _all_rows(hass)
    _assert_chains(snap)
    keys = _batch_keys(batch, trim_first=True)
    _assert_partition(snap, keys)
    trimmed = min(_batch_keys(batch))
    assert all(trimmed not in snap[sid] for sid in snap), "straddle bucket kept"
    mixed = datetime(2026, 7, 7, 5, tzinfo=UTC)  # 14:30 + 15:00 ACST
    assert snap[_sid("consumption", "peak")][mixed][0] > 0
    assert snap[_sid("consumption", "normal")][mixed][0] > 0
    # 21:30 ACST is 12:00Z: Anytime; 22:00 ACST (12:30Z) opens off-peak.
    straddle_22 = datetime(2026, 7, 7, 12, tzinfo=UTC)
    assert snap[_sid("consumption", "normal")][straddle_22][0] > 0
    assert snap[_sid("consumption", "offpeak")][straddle_22][0] > 0


async def test_ac2_partition_weekday_weekend_windows(
    recorder_mock, hass: HomeAssistant
) -> None:
    """D2: weekday and weekend windows share hours; the LOCAL weekday picks
    the row. Mon 2026-07-06 .. Sun 2026-07-12 (Brisbane)."""
    coord = _coord(hass, _BNE)
    _configure(
        hass,
        coord,
        _opts(
            [
                {"band": "peak", "start": "16:00", "end": "20:00", "days": "weekdays"},
                {
                    "band": "shoulder",
                    "start": "16:00",
                    "end": "20:00",
                    "days": "weekends",
                },
            ]
        ),
    )
    days = _days(date(2026, 7, 6), 7)
    batch = [r for d in days for r in _day_slots(d, _BNE)]
    await _import(hass, coord, batch, set(days))

    snap = await _all_rows(hass)
    _assert_chains(snap)
    _assert_partition(snap, _batch_keys(batch))
    peak_days = {k.astimezone(_BNE).date() for k in snap[_sid("consumption", "peak")]}
    shoulder_days = {
        k.astimezone(_BNE).date() for k in snap[_sid("consumption", "shoulder")]
    }
    assert peak_days == set(days[:5])
    assert shoulder_days == set(days[5:])


async def test_ac2_superoffpeak_lands_in_a_band_and_unused_band_never_created(
    recorder_mock, hass: HomeAssistant
) -> None:
    """M5 + AC4: an EV plan (superoffpeak 00-06, Anytime residual). Derived
    superoffpeak kWh must land in its own band (bucketing over
    TOU_SERIES_TARIFFS would drop it from every band — the partition then
    fails); IDs keep the haggle:consumption_<band>_<contract> shape; and a
    band the plan never uses (peak, shoulder, offpeak) gets no series."""
    coord = _coord(hass, _BNE)
    _configure(
        hass,
        coord,
        _opts(
            [{"band": "superoffpeak", "start": "00:00", "end": "06:00", "days": "all"}],
            residual="normal",
        ),
    )
    days = _days(date(2026, 7, 6), 2)
    batch = [r for d in days for r in _day_slots(d, _BNE, "peak")]
    await _import(hass, coord, batch, set(days))

    snap = await _all_rows(hass)
    _assert_partition(snap, _batch_keys(batch))
    sop = snap[f"{DOMAIN}:consumption_superoffpeak_{_CONTRACT}"]
    assert len(sop) == 2 * 6
    assert (
        abs(
            sum(s for s, _ in sop.values())
            - sum(r.kwh for r in batch if r.dt.astimezone(_BNE).hour < 6)
        )
        < 1e-9
    )
    for band in ("peak", "shoulder", "offpeak"):
        assert not snap[_sid("consumption", band)], band
        assert not snap[_sid("cost", band)], band


async def test_ac2_m1_errored_day_keeps_its_empty_band_partition(
    recorder_mock, hass: HomeAssistant
) -> None:
    """M1 negative: a flat user with NO band history configures windows, and
    the first derived rewindow has an errored mid day. That day's aggregate
    rows carry forward (#300); the bands have no rows there and the fill
    never creates one — so at those keys the bands do NOT sum to the
    aggregate. Documented: carried-forward keys keep their stored partition,
    which is empty for history that never had bands."""
    coord = _coord(hass, _BNE)
    days = _days(date(2026, 7, 1), 10)
    await _import(
        hass, coord, [r for d in days for r in _day_slots(d, _BNE)], set(days)
    )
    assert not (await _all_rows(hass))[_sid("consumption", "offpeak")]

    _configure(hass, coord, _opts(_BILL_126))
    window = days[3:]
    errored = window[2]
    batch = [r for d in window if d != errored for r in _day_slots(d, _BNE)]
    await _import(hass, coord, batch, set(window) - {errored})

    snap = await _all_rows(hass)
    _assert_chains(snap)
    _assert_partition(snap, _batch_keys(batch))
    errored_keys = _batch_keys(_day_slots(errored, _BNE))
    for key in errored_keys:
        assert snap[_sid("consumption")][key][0] > 0  # carried forward
        for band in _ALL_BANDS:
            assert key not in snap[_sid("consumption", band)], (band, key)


# --- AC3 / AC4: transitions never step a sum down ------------------------------


def _assert_before_untouched(
    before: dict[str, dict[datetime, tuple]],
    after: dict[str, dict[datetime, tuple]],
    cutoff: datetime,
) -> None:
    for stat_id, rows in before.items():
        old = {k: v for k, v in rows.items() if k < cutoff}
        new = {k: v for k, v in after[stat_id].items() if k < cutoff}
        assert old == new, f"{stat_id} changed before the cutoff"


def _assert_retired(
    snap: dict[str, dict[datetime, tuple]], band: str, cutoff: datetime
) -> None:
    """A retired band flatlines from the cutoff: rows kept, state 0."""
    for kind in ("consumption", "cost"):
        rows = snap[_sid(kind, band)]
        assert rows, f"{band} {kind} had no history"
        after = [v[0] for k, v in rows.items() if k >= cutoff]
        assert after, f"{band} {kind} was not re-chained"
        assert all(v == 0.0 for v in after), (band, kind)


async def _seed_derived(
    hass: HomeAssistant, coord: HaggleCoordinator, options: dict, days: list[date]
) -> None:
    _configure(hass, coord, options)
    batch = [r for d in days for r in _day_slots(d, _BNE)]
    await _import(hass, coord, batch, set(days))


async def test_ac3_window_change_retires_shoulder_and_never_steps(
    recorder_mock, hass: HomeAssistant
) -> None:
    """The key AC3 test: 14 days under A (peak 15-21, shoulder 10-14), then
    the 7-day rewindow under B (peak 16-20, shoulder removed). Every series
    is monotone and re-chained, rows before the cutoff are byte-identical,
    the partition is exact at every key the rewindow wrote, shoulder
    flatlines from the cutoff and no new series appears.

    Fails if band series bypass the stale-key fill: the old peak row at
    15:00 keeps its A-chain sum above the B-chain 16:00 row (downward step)
    and the stored shoulder rows still count beside the new off-peak ones.
    """
    coord = _coord(hass, _BNE)
    days = _days(date(2026, 7, 1), 14)
    await _seed_derived(hass, coord, _opts(_BILL_126), days)
    before = await _all_rows(hass)
    assert before[_sid("consumption", "shoulder")]

    _configure(
        hass,
        coord,
        _opts([{"band": "peak", "start": "16:00", "end": "20:00", "days": "all"}]),
    )
    window = days[7:]
    batch = [r for d in window for r in _day_slots(d, _BNE)]
    await _import(hass, coord, batch, set(window))

    snap = await _all_rows(hass)
    cutoff = min(_batch_keys(batch))
    _assert_chains(snap)
    _assert_before_untouched(before, snap, cutoff)
    _assert_partition(snap, _batch_keys(batch))
    _assert_retired(snap, "shoulder", cutoff)
    peak_hours = {
        k.astimezone(_BNE).hour
        for k in snap[_sid("consumption", "peak")]
        if k >= cutoff and snap[_sid("consumption", "peak")][k][0] > 0
    }
    assert peak_hours == {16, 17, 18, 19}
    for band in ("normal", "superoffpeak"):
        assert not snap[_sid("consumption", band)], band


async def test_ac3_legacy_to_derived(recorder_mock, hass: HomeAssistant) -> None:
    """Unconfigured -> configured: 14 legacy days typed "normal" with a
    spurious "peak" hour (#224 shape), then a derived rewindow. Anytime is
    retired (flatlines), peak is re-chained from its old sum, off-peak and
    shoulder start at 0, nothing steps, the partition is exact."""
    coord = _coord(hass, _BNE)
    days = _days(date(2026, 7, 1), 14)
    legacy = [r for d in days for r in _day_slots(d, _BNE, {36: "peak", 37: "peak"})]
    await _import(hass, coord, legacy, set(days))
    before = await _all_rows(hass)
    assert before[_sid("consumption", "normal")]
    assert before[_sid("consumption", "peak")]

    _configure(hass, coord, _opts(_BILL_126))
    window = days[7:]
    batch = [r for d in window for r in _day_slots(d, _BNE, {36: "peak"})]
    await _import(hass, coord, batch, set(window))

    snap = await _all_rows(hass)
    cutoff = min(_batch_keys(batch))
    _assert_chains(snap)
    _assert_before_untouched(before, snap, cutoff)
    _assert_partition(snap, _batch_keys(batch))
    _assert_retired(snap, "normal", cutoff)
    for band in ("offpeak", "shoulder"):
        rows = snap[_sid("consumption", band)]
        first = rows[min(rows)]
        assert abs(first[1] - first[0]) < 1e-9, f"{band} must start at 0"


async def test_ac3_derived_to_flat(recorder_mock, hass: HomeAssistant) -> None:
    """Configured -> Flat: every band retires from the cutoff (no new rows
    elsewhere), the aggregate carries on, nothing steps."""
    coord = _coord(hass, _BNE)
    days = _days(date(2026, 7, 1), 14)
    await _seed_derived(hass, coord, _opts(_BILL_126), days)
    before = await _all_rows(hass)

    _configure(hass, coord, _FLAT)
    window = days[7:]
    batch = [r for d in window for r in _day_slots(d, _BNE)]
    await _import(hass, coord, batch, set(window))

    snap = await _all_rows(hass)
    cutoff = min(_batch_keys(batch))
    _assert_chains(snap)
    _assert_before_untouched(before, snap, cutoff)
    for band in ("peak", "shoulder", "offpeak"):
        _assert_retired(snap, band, cutoff)
    for band in ("normal", "superoffpeak"):
        assert not snap[_sid("consumption", band)]
    assert (
        abs(
            snap[_sid("consumption")][max(snap[_sid("consumption")])][1]
            - sum(r.kwh for d in days for r in _day_slots(d, _BNE))
        )
        < 1e-9
    )


async def test_ac3_back_to_legacy_retires_superoffpeak(
    recorder_mock, hass: HomeAssistant
) -> None:
    """Configured (EV: superoffpeak + Anytime) -> back to unconfigured on an
    entry that HAS applied history (ever configured): the legacy type split
    resumes (peak re-opens the gate) and the BAND_VOCAB family retires the
    superoffpeak band v0.5.0 would never touch. Monotone, exact partition."""
    coord = _coord(
        hass,
        _BNE,
        data={
            "tariff_bands_applied": [
                {
                    "fingerprint": "abc",
                    "mode": "derived",
                    "from": "2026-06-30T14:00:00+00:00",
                }
            ]
        },
    )
    days = _days(date(2026, 7, 1), 14)
    await _seed_derived(
        hass,
        coord,
        _opts(
            [{"band": "superoffpeak", "start": "00:00", "end": "06:00", "days": "all"}],
            residual="normal",
        ),
        days,
    )
    before = await _all_rows(hass)

    _configure(hass, coord, None)
    assert coord._band_plan.ever_configured is True
    window = days[7:]
    batch = [r for d in window for r in _day_slots(d, _BNE, {36: "peak", 2: "offpeak"})]
    await _import(hass, coord, batch, set(window))

    snap = await _all_rows(hass)
    cutoff = min(_batch_keys(batch))
    _assert_chains(snap)
    _assert_before_untouched(before, snap, cutoff)
    _assert_partition(snap, _batch_keys(batch))
    _assert_retired(snap, "superoffpeak", cutoff)


async def test_ac3_errored_day_in_first_derived_window(
    recorder_mock, hass: HomeAssistant
) -> None:
    """An errored day inside the first derived window: every series carries
    that day forward — the aggregate AND the legacy bands, so the stored
    (old-basis) partition still holds there — and nothing steps."""
    coord = _coord(hass, _BNE)
    days = _days(date(2026, 7, 1), 14)
    legacy = [r for d in days for r in _day_slots(d, _BNE, {36: "peak"})]
    await _import(hass, coord, legacy, set(days))
    before = await _all_rows(hass)

    _configure(hass, coord, _opts(_BILL_126))
    window = days[7:]
    errored = window[3]
    batch = [r for d in window if d != errored for r in _day_slots(d, _BNE)]
    await _import(hass, coord, batch, set(window) - {errored})

    snap = await _all_rows(hass)
    _assert_chains(snap)
    errored_keys = _batch_keys(_day_slots(errored, _BNE))
    _assert_partition(snap, _batch_keys(batch) | errored_keys)
    for key in errored_keys:
        for sid in (_sid("consumption"), _sid("consumption", "normal")):
            assert snap[sid][key][0] == before[sid][key][0], (sid, key)


async def test_ac3_partition_rule_zeroes_old_band_at_slack_key(
    recorder_mock, hass: HomeAssistant
) -> None:
    """D7's partition-exact band fill: day X's own fetch errored, but day
    X-1's response carried X's first two slots as trailing slack. X is not
    authoritative, so v0.5.0's fill would carry the old band forward at that
    key while the new band takes the batch value (counted twice). For an
    ever-configured entry the old band is zeroed there; a key with no
    aggregate batch value still carries forward."""
    coord = _coord(hass, _BNE)
    days = _days(date(2026, 7, 1), 10)
    legacy = [r for d in days for r in _day_slots(d, _BNE, {0: "peak", 1: "peak"})]
    await _import(hass, coord, legacy, set(days))

    _configure(hass, coord, _opts(_BILL_126))
    window = days[5:9]
    errored = days[9]
    slack = _day_slots(errored, _BNE)[:2]
    batch = [r for d in window for r in _day_slots(d, _BNE)] + slack
    await _import(hass, coord, batch, set(window))

    snap = await _all_rows(hass)
    _assert_chains(snap)
    slack_key = _hour(slack[0].dt)
    _assert_partition(snap, _batch_keys(batch))
    assert snap[_sid("consumption", "peak")][slack_key][0] == 0.0
    later = slack_key + timedelta(hours=1)  # errored day, no batch value
    assert snap[_sid("consumption", "normal")][later][0] > 0


# --- AC6 / AC10 -----------------------------------------------------------------


async def test_ac6_suspended_writes_aggregate_only(
    recorder_mock, hass: HomeAssistant
) -> None:
    """Configured windows on the HA-timezone fallback, past the hold: the
    aggregate is written and NO band row (stored band rows byte-identical)."""
    coord = _coord(hass, _BNE)
    days = _days(date(2026, 7, 1), 4)
    await _seed_derived(hass, coord, _opts(_BILL_126), days)
    before = await _all_rows(hass)

    coord.client.tz_is_contract = False
    coord._address_unparseable = True
    _configure(hass, coord, _opts(_BILL_126))
    assert coord._band_plan.mode == "suspended_tz"
    more = _days(date(2026, 7, 5), 2)
    # AGL-typed readings: a legacy (type-derived) write would open the gate.
    typed = [r for d in more for r in _day_slots(d, _BNE, {36: "peak", 2: "offpeak"})]
    await _import(hass, coord, typed, set(more))

    snap = await _all_rows(hass)
    for band in _ALL_BANDS:
        for kind in ("consumption", "cost"):
            assert snap[_sid(kind, band)] == before[_sid(kind, band)], band
    assert len(snap[_sid("consumption")]) == len(before[_sid("consumption")]) + 48


async def test_ac10_one_state_read_per_import_with_five_bands(
    recorder_mock, hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC10: a derived import still makes exactly ONE `{"state"}` read,
    batched across the aggregate and all five band families."""
    from homeassistant.components.recorder import statistics as stats_mod

    real = stats_mod.statistics_during_period
    state_reads: list[set[str]] = []

    def counting(hass_, start, end, ids, period, units, types):
        if types == {"state"}:
            state_reads.append(set(ids))
        return real(hass_, start, end, ids, period, units, types)

    monkeypatch.setattr(stats_mod, "statistics_during_period", counting)
    coord = _coord(hass, _BNE)
    _configure(
        hass,
        coord,
        _opts(
            [
                *_BILL_126,
                {
                    "band": "superoffpeak",
                    "start": "01:00",
                    "end": "03:00",
                    "days": "all",
                },
            ]
        ),
    )
    day = date(2026, 7, 6)
    await coord._import_intervals(_day_slots(day, _BNE), reading_days={day})
    await coord._import_intervals([])
    assert len(state_reads) == 1
    assert state_reads[0] == set(_all_ids())
