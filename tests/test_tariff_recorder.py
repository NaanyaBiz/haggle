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
