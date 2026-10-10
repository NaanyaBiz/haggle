"""Real-recorder tests for the #292 PR2b timezone history re-alignment machine.

These tests use phcc's real in-memory SQLite recorder and drive the
production cycle, so the M1 junction drop is exercised at its real call
sites against genuinely stored old-convention rows (no patched reads).

Must be a separate module from test_tz_realign.py: phcc's recorder_db_url
asserts the recorder fixtures are initialised BEFORE the hass fixture; the
module-level autouse fixture override is the only clean way to guarantee that
ordering without affecting non-recorder tests.

The end-to-end classes drive the production cycle (_async_update_data) on
the real recorder with frozen dates and pin signed residuals: M1 for
consumption (SA, and QLD with the floor in Sydney DST) and generation,
stage order plus the per-cycle request budget, C1 halt / per-day-skip
degradation (pinned against a plain normal poll), the give-up residual,
HTTP-OK empty days, the exception attempt, C10, the per-day floor
attribution, and the M4 NT/WA exclusion residual (RA-19).
"""

from __future__ import annotations

import logging
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

from custom_components.haggle.agl.client import AGLError, AGLRateLimitError
from custom_components.haggle.agl.models import BillPeriod, IntervalReading, PlanRates
from custom_components.haggle.agl.parser import (
    agl_reported_timestamp,
)
from custom_components.haggle.const import (
    CONF_ACCOUNT_NUMBER,
    CONF_CONTRACT_NUMBER,
    CONF_LOCAL_TZ,
    CONF_REFRESH_TOKEN,
    CONF_TZ_REALIGN,
    DOMAIN,
    ISSUE_TZ_HISTORY_BEFORE_REALIGN,
    ISSUE_TZ_REALIGN_GAVE_UP,
    MAX_TZ_REALIGN_ATTEMPTS,
    RETRY_INTERVAL_ON_ERROR,
    REWINDOW_DAYS,
    STAT_CONSUMPTION,
    STAT_GENERATION,
    STAT_GENERATION_CREDIT,
    TZ_REALIGN_COMPLETE,
    TZ_REALIGN_DAYS,
    TZ_REALIGN_DONE,
    TZ_REALIGN_GAVE_UP,
    TZ_REALIGN_GENERATION,
    TZ_REALIGN_PENDING,
    TZ_REALIGN_REASON_UNSUPPORTED_SHIFT,
    TZ_REALIGN_SKIPPED,
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
# End-to-end: the real cycle (_fetch_and_import / _async_update_data) against
# the REAL recorder. Every M1/C1 call site is exercised through the production
# path — no _realign_junction_drop / _stored_state_at patching.
#
# C11 profile, unique per slot AND per day: consumption
#   kwh(d, i) = 0.1 + 0.01 * i + 0.001 * (d.toordinal() % 50)
# so the floor's 00:00 slot is smaller than its 00:30 slot and every hour
# differs from its neighbours; generation exports only on slots 13..37
# (06:30..18:30 local), so its first slot after dawn is slot 13.
# ---------------------------------------------------------------------------

_SYD = ZoneInfo("Australia/Sydney")
_ADL = ZoneInfo("Australia/Adelaide")
_BNE = ZoneInfo("Australia/Brisbane")
_GEN = f"{DOMAIN}:{STAT_GENERATION}_{_CONTRACT}"
_CREDIT = f"{DOMAIN}:{STAT_GENERATION_CREDIT}_{_CONTRACT}"
_CONS = f"{DOMAIN}:{STAT_CONSUMPTION}_{_CONTRACT}"
# A second entry for the plain-normal-poll reference run in the same recorder.
_REF_CONTRACT = "8888888888"
_REF_CONS = f"{DOMAIN}:{STAT_CONSUMPTION}_{_REF_CONTRACT}"
_HISTORY_DAYS = 45


def _local_slots(day: date, tz: ZoneInfo) -> list[datetime]:
    """Every true 30-min slot start (UTC) of ``day``'s local midnight..midnight."""
    nxt = day + timedelta(days=1)
    start = datetime(day.year, day.month, day.day, tzinfo=tz).astimezone(UTC)
    end = datetime(nxt.year, nxt.month, nxt.day, tzinfo=tz).astimezone(UTC)
    out: list[datetime] = []
    while start < end:
        out.append(start)
        start += timedelta(minutes=30)
    return out


def _cons_kwh(day: date, i: int) -> float:
    return round(0.1 + 0.01 * i + 0.001 * (day.toordinal() % 50), 6)


def _gen_kwh(day: date, i: int) -> float:
    if not 13 <= i <= 37:
        return 0.0
    return round(0.2 + 0.01 * i + 0.001 * (day.toordinal() % 50), 6)


def _day_readings(
    day: date, tz: ZoneInfo, *, old: bool, profile=_cons_kwh
) -> list[IntervalReading]:
    """One local day in the corrected (``old=False``) or pre-beta.3 convention."""
    out: list[IntervalReading] = []
    for i, dt in enumerate(_local_slots(day, tz)):
        kwh = profile(day, i)
        if kwh == 0.0:
            continue
        at = agl_reported_timestamp(dt, api_tz=_SYD, contract_tz=tz) if old else dt
        out.append(
            IntervalReading(
                dt=at, kwh=kwh, cost_aud=round(kwh * 0.3, 6), rate_type="normal"
            )
        )
    return out


def _total(days: list[date], tz: ZoneInfo, profile=_cons_kwh) -> float:
    return sum(profile(d, i) for d in days for i in range(len(_local_slots(d, tz))))


def _steps(rows: list[dict]) -> list[tuple[str, float]]:
    ordered = sorted(rows, key=lambda r: r["start"])
    return [
        (datetime.fromtimestamp(b["start"], tz=UTC).isoformat(), a["sum"] - b["sum"])
        for a, b in pairwise(ordered)
        if b["sum"] < a["sum"] - 1e-9
    ]


class _Agl:
    """AGL double serving the CORRECTED convention, with per-day faults."""

    def __init__(self, tz: ZoneInfo) -> None:
        self.tz = tz
        self.halt: date | None = None
        self.fail: date | None = None
        self.empty: date | None = None
        self.boom: date | None = None
        self.cons_days: list[date] = []
        self.solar_days: list[date] = []

    async def hourly(self, _contract: str, day: date) -> list[IntervalReading]:
        self.cons_days.append(day)
        if day == self.halt:
            raise AGLRateLimitError("429")
        if day == self.fail:
            raise AGLError("HTTP 500")
        if day == self.boom:
            raise RuntimeError("unwrapped")
        if day == self.empty:
            return []
        return _day_readings(day, self.tz, old=False)

    async def solar(
        self, _contract: str, day: date, previous: bool = False
    ) -> list[IntervalReading]:
        self.solar_days.append(day)
        return _day_readings(day, self.tz, old=False, profile=_gen_kwh)


def _e2e_coordinator(
    hass: HomeAssistant,
    tz: ZoneInfo,
    *,
    solar: bool = False,
    contract: str = _CONTRACT,
    realign: dict | None = None,
) -> tuple[HaggleCoordinator, _Agl]:
    """An entry + coordinator on the AGL double. ``contract`` gives a second
    entry its own statistic ids in the same recorder; ``realign`` seeds the
    persisted record (a done record makes every cycle a plain normal poll)."""
    data = {
        CONF_REFRESH_TOKEN: "v1.testtoken",
        CONF_CONTRACT_NUMBER: contract,
        CONF_ACCOUNT_NUMBER: _ACCOUNT,
        CONF_LOCAL_TZ: tz.key,
    }
    if realign is not None:
        data[CONF_TZ_REALIGN] = realign
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=data,
        unique_id=f"{_ACCOUNT}_{contract}",
        title="Home",
    )
    entry.add_to_hass(hass)
    agl = _Agl(tz)
    today = datetime.now(UTC).date()
    client = AsyncMock()
    client.tz_is_contract = True
    client.local_tz = tz
    client.async_get_overview = AsyncMock(return_value=[])
    client.async_get_usage_summary = AsyncMock(
        return_value=BillPeriod(
            start=today - timedelta(days=90),
            end=today,
            consumption_kwh=0.0,
            cost_label="$0.00",
            projection_label="",
        )
    )
    client.async_get_plan = AsyncMock(
        return_value=PlanRates(
            product_name="", unit_rates=[], supply_charge_cents_per_day=0.0
        )
    )
    client.async_get_usage_hourly = AsyncMock(side_effect=agl.hourly)
    client.async_get_usage_hourly_previous = AsyncMock(side_effect=agl.hourly)
    client.async_get_solar_hourly = AsyncMock(side_effect=agl.solar)
    coord = HaggleCoordinator(hass, entry, client, contract)
    coord._has_solar = solar
    return coord, agl


def _history(today: date) -> list[date]:
    return [today - timedelta(days=k) for k in range(_HISTORY_DAYS, 0, -1)]


async def _seed_old(
    hass: HomeAssistant,
    coord: HaggleCoordinator,
    tz: ZoneInfo,
    days: list[date],
    *,
    generation: bool = False,
) -> None:
    """Write ``days`` the way a pre-beta.3 release did (old convention)."""
    if generation:
        await coord._import_generation(
            [r for d in days for r in _day_readings(d, tz, old=True, profile=_gen_kwh)],
            fetched_days=days,
        )
    else:
        await coord._import_intervals(
            [r for d in days for r in _day_readings(d, tz, old=True)]
        )
    await async_wait_recording_done(hass)


async def _cycle(hass: HomeAssistant, coord: HaggleCoordinator) -> None:
    with patch("custom_components.haggle.coordinator.BACKFILL_INTER_REQUEST_DELAY", 0):
        await coord._async_update_data()
    await async_wait_recording_done(hass)


def _record(coord: HaggleCoordinator) -> dict:
    return dict(coord.config_entry.data[CONF_TZ_REALIGN])


async def _rows(hass: HomeAssistant, stat_id: str) -> list[dict]:
    return sorted(await _read_series(hass, stat_id), key=lambda r: r["start"])


def _hour_totals(readings: list[IntervalReading]) -> dict[float, float]:
    out: dict[float, float] = {}
    for r in readings:
        key = r.dt.replace(minute=0).timestamp()
        out[key] = out.get(key, 0.0) + r.kwh
    return out


def _dash_days(rows: list[dict], tz: ZoneInfo) -> dict[date, float]:
    """Per-local-day totals as the Energy dashboard bars them: each stored
    hourly row counts on the local date of its start."""
    out: dict[date, float] = {}
    for r in rows:
        day = datetime.fromtimestamp(r["start"], tz=UTC).astimezone(tz).date()
        out[day] = out.get(day, 0.0) + r["state"]
    return out


def _world_days(days: list[date], tz: ZoneInfo, profile=_cons_kwh) -> dict[date, float]:
    """The same per-day bars for a fully corrected history."""
    out: dict[date, float] = {}
    for d in days:
        for key, kwh in _hour_totals(
            _day_readings(d, tz, old=False, profile=profile)
        ).items():
            day = datetime.fromtimestamp(key, tz=UTC).astimezone(tz).date()
            out[day] = out.get(day, 0.0) + kwh
    return out


def _haggle_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.WARNING
        and r.name.startswith("custom_components.haggle")
    ]


class TestEndToEndConsumptionStage:
    """M1 consumption junction + stage completion through the real cycle."""

    @pytest.mark.parametrize(
        ("tz", "now", "phantom"),
        [
            # SA: the old chain stored the floor's 00:30 slot 30 min early, in
            # the straddle hour below the cutoff, so it is already inside the
            # baseline. Without the M1 drop it is counted twice.
            (_ADL, "2026-10-10T03:00:00+00:00", lambda f: _cons_kwh(f, 1)),
            (_ADL, "2026-11-20T03:00:00+00:00", lambda f: _cons_kwh(f, 1)),
            # QLD armed after 2026-11-03 (floor inside Sydney DST): the whole
            # first local hour (slots 0 and 1) sat 1 h early in the old chain.
            (
                _BNE,
                "2026-11-20T03:00:00+00:00",
                lambda f: _cons_kwh(f, 0) + _cons_kwh(f, 1),
            ),
        ],
        ids=["SA-oct", "SA-nov", "QLD-nov"],
    )
    async def test_consumption_stage_exact_at_floor(
        self, hass: HomeAssistant, freezer, tz: ZoneInfo, now: str, phantom
    ) -> None:
        """Signed residual (C11): final - true total == 0, zero steps.

        Pre-fix value (M1 drop removed): +phantom(floor), the named slot(s)
        above. A non-solar entry finishes on the same cycle: consumption
        complete, generation skipped (no generation rows).
        """
        freezer.move_to(now)
        coord, agl = _e2e_coordinator(hass, tz)
        today = datetime.now(UTC).date()
        floor = today - timedelta(days=TZ_REALIGN_DAYS)
        days = _history(today)
        await _seed_old(hass, coord, tz, days)

        await _cycle(hass, coord)

        rows = await _rows(hass, _CONS)
        residual = rows[-1]["sum"] - _total(days, tz)
        assert _steps(rows) == []
        assert residual == pytest.approx(0.0, abs=1e-6), (
            f"residual {residual:+.6f}; M1-removed value would be {phantom(floor):+.6f}"
        )
        # Every hour of the floor day after the junction hour sits at its
        # corrected instant. The junction hour (the first key at/after the
        # cutoff) holds its slots minus the zeroed one(s): those stay counted
        # in the stored old row below the cutoff — exactly the M1 drop.
        got = {r["start"]: r["state"] for r in rows}
        expected = _hour_totals(_day_readings(floor, tz, old=False))
        keys = sorted(expected)
        # SA: keys[0] is the straddle bucket (kept as stored); QLD: none.
        junction = keys[1] if tz is _ADL else keys[0]
        assert got[junction] == pytest.approx(
            expected[junction] - phantom(floor), abs=1e-9
        )
        for key in keys[keys.index(junction) + 1 : -1]:
            assert got[key] == pytest.approx(expected[key], abs=1e-9)
        # Per-day bars: "exact" is SUM-exact, not day-exact. The zeroed
        # junction slot(s) stay counted in the stored old row below the
        # cutoff, which the dashboard dates to the day BEFORE the floor —
        # so the floor day's bar is short by exactly phantom(floor) and
        # floor-1 carries it (on top of its own old-timing shift, like all
        # older history). The day after the floor is exact.
        dash = _dash_days(rows, tz)
        world = _world_days(days, tz)
        prev, nxt = floor - timedelta(days=1), floor + timedelta(days=1)
        assert dash[floor] == pytest.approx(world[floor] - phantom(floor), abs=1e-9)
        assert dash[prev] == pytest.approx(
            world[prev] + phantom(floor) - phantom(prev), abs=1e-9
        )
        assert dash[nxt] == pytest.approx(world[nxt], abs=1e-9)
        record = _record(coord)
        assert record["state"] == TZ_REALIGN_DONE
        assert record["reason"] == TZ_REALIGN_COMPLETE
        assert record["consumption"] == TZ_REALIGN_COMPLETE
        assert record["generation"] == TZ_REALIGN_SKIPPED
        # One un-chunked window, no overlap day on it (retention guard).
        assert agl.cons_days == [floor + timedelta(days=n) for n in range(30)]


class TestEndToEndGenerationStage:
    """M1 generation junction, stage order and per-cycle request budget."""

    @pytest.mark.parametrize(
        ("tz", "junction_day", "solar_normal"),
        [
            # SA fetches one leading overlap day, so the pre-existing beta.3/4
            # rewindow junction sits on today-8; QLD (whole-hour) on today-7.
            (_ADL, 8, REWINDOW_DAYS + 1),
            (_BNE, 7, REWINDOW_DAYS),
        ],
        ids=["SA", "QLD"],
    )
    async def test_generation_stage_exact_at_floor(
        self, hass: HomeAssistant, freezer, tz: ZoneInfo, junction_day, solar_normal
    ) -> None:
        """Signed residuals (C11), frozen at 2026-11-20 (floor in Sydney DST).

        Consumption: final - true total == 0. Generation: final - true total
        == gen slot 13 of today-``junction_day`` EXACTLY — the known,
        pre-existing beta.3/4 rewindow-junction dawn slot (the first
        corrected poll's normal solar rewindow double-counts it; the stage
        neither adds nor removes it). The FLOOR junction adds nothing.
        Pre-fix value (M1 generation drop removed): + gen slot 13 of the
        floor (the first export slot after dawn), i.e. +0.340 more.

        Stage order and budget (C6, one stage per cycle): cycle 1 sweeps
        the 30-day consumption window plus the NORMAL solar rewindow;
        cycle 2 the normal consumption rewindow plus the 30-day solar
        window; never an overlap day on a window (retention guard).
        """
        freezer.move_to("2026-11-20T03:00:00+00:00")
        coord, agl = _e2e_coordinator(hass, tz, solar=True)
        today = datetime.now(UTC).date()
        floor = today - timedelta(days=TZ_REALIGN_DAYS)
        days = _history(today)
        await _seed_old(hass, coord, tz, days)
        await _seed_old(hass, coord, tz, days, generation=True)
        dawn = _gen_kwh(today - timedelta(days=junction_day), 13)

        await _cycle(hass, coord)
        assert (len(agl.cons_days), len(agl.solar_days)) == (30, solar_normal)
        record = _record(coord)
        assert record["state"] == TZ_REALIGN_PENDING
        assert record["series"] == TZ_REALIGN_GENERATION
        assert record["consumption"] == TZ_REALIGN_COMPLETE
        gen1 = (await _rows(hass, _GEN))[-1]["sum"] - _total(days, tz, _gen_kwh)
        assert gen1 == pytest.approx(dawn, abs=1e-6)

        agl.cons_days.clear()
        agl.solar_days.clear()
        await _cycle(hass, coord)
        assert (len(agl.cons_days), len(agl.solar_days)) == (solar_normal, 30)
        assert agl.solar_days == [floor + timedelta(days=n) for n in range(30)]

        gen_rows = await _rows(hass, _GEN)
        cons_rows = await _rows(hass, _CONS)
        assert _steps(gen_rows) == []
        assert _steps(await _rows(hass, _CREDIT)) == []
        assert _steps(cons_rows) == []
        gen_residual = gen_rows[-1]["sum"] - _total(days, tz, _gen_kwh)
        assert gen_residual == pytest.approx(dawn, abs=1e-6), (
            f"generation residual {gen_residual:+.6f}; expected the known "
            f"rewindow dawn slot {dawn:+.6f}; M1-removed value would be "
            f"{dawn + _gen_kwh(floor, 13):+.6f}"
        )
        assert cons_rows[-1]["sum"] - _total(days, tz) == pytest.approx(0.0, abs=1e-6)
        # Floor-day export hours after the junction hour sit at their
        # corrected instants.
        got = {r["start"]: r["state"] for r in gen_rows}
        expected = _hour_totals(_day_readings(floor, tz, old=False, profile=_gen_kwh))
        for key in sorted(expected)[1:]:
            assert got[key] == pytest.approx(expected[key], abs=1e-9)
        # Per-day bars: the zeroed dawn slot stays counted in the old row one
        # hour below the cutoff, which is still dated the floor day (export
        # is zero around midnight), so the floor day's export bar is exact.
        dash = _dash_days(gen_rows, tz)
        world = _world_days(days, tz, _gen_kwh)
        assert dash[floor] == pytest.approx(world[floor], abs=1e-9)
        assert dash[floor + timedelta(days=1)] == pytest.approx(
            world[floor + timedelta(days=1)], abs=1e-9
        )
        record = _record(coord)
        assert record["state"] == TZ_REALIGN_DONE
        assert record["reason"] == TZ_REALIGN_COMPLETE
        assert record["generation"] == TZ_REALIGN_COMPLETE


class TestEndToEndIncompleteStage:
    """C1: an incomplete stage writes no more than a normal poll would."""

    @pytest.mark.parametrize("fault", ["halt", "fail"])
    async def test_incomplete_sweep_degrades_then_completes(
        self, hass: HomeAssistant, freezer, fault: str
    ) -> None:
        """Adelaide, a 429 (halt) or an AGL 500 (per-day skip) on floor+10.

        Cycle 1, skip: the stored rows are IDENTICAL to a plain normal poll
        on the same seed (a second entry whose record is already done), so
        the degraded import is a normal import — overlap-day context
        included. Halt: the ascending sweep stops at floor+10, before any
        normal-range day, so nothing at all is written. Zero steps; the
        halt counts one HALT (fast retry), the skip one ATTEMPT (configured
        cadence). Cycle 2, fault gone: complete and exact.
        """
        freezer.move_to("2026-10-10T03:00:00+00:00")
        coord, agl = _e2e_coordinator(hass, _ADL)
        today = datetime.now(UTC).date()
        floor = today - timedelta(days=TZ_REALIGN_DAYS)
        setattr(agl, fault, floor + timedelta(days=10))
        days = _history(today)
        await _seed_old(hass, coord, _ADL, days)
        before = await _rows(hass, _CONS)

        await _cycle(hass, coord)

        rows = await _rows(hass, _CONS)
        assert _steps(rows) == []
        record = _record(coord)
        assert record["state"] == TZ_REALIGN_PENDING
        assert record["series"] == "consumption"
        if fault == "halt":
            assert rows == before
            assert (record["attempts"], record["halts"]) == (0, 1)
            assert coord.update_interval == RETRY_INTERVAL_ON_ERROR
        else:
            ref, _ref_agl = _e2e_coordinator(
                hass,
                _ADL,
                contract=_REF_CONTRACT,
                realign={
                    "state": TZ_REALIGN_DONE,
                    "zone": _ADL.key,
                    "reason": TZ_REALIGN_COMPLETE,
                },
            )
            await _seed_old(hass, ref, _ADL, days)
            await _cycle(hass, ref)
            normal = await _rows(hass, _REF_CONS)
            assert [(r["start"], r["state"], r["sum"]) for r in rows] == [
                (r["start"], r["state"], r["sum"]) for r in normal
            ]
            assert (record["attempts"], record["halts"]) == (1, 0)
            assert coord.update_interval == timedelta(hours=24)

        setattr(agl, fault, None)
        freezer.tick(timedelta(minutes=30))
        await _cycle(hass, coord)

        rows = await _rows(hass, _CONS)
        assert _steps(rows) == []
        assert rows[-1]["sum"] - _total(days, _ADL) == pytest.approx(0.0, abs=1e-6)
        assert _record(coord)["reason"] == TZ_REALIGN_COMPLETE

    @pytest.mark.parametrize(
        ("tz", "now", "junction_day", "slots"),
        [
            # The first corrected poll's normal rewindow junction: SA (with
            # its overlap day) on today-8, slot 1; QLD on today-7, the whole
            # first hour.
            (_ADL, "2026-10-10T03:00:00+00:00", 8, (1,)),
            (_BNE, "2026-11-20T03:00:00+00:00", 7, (0, 1)),
        ],
        ids=["SA", "QLD"],
    )
    async def test_give_up_keeps_the_rewindow_junction_double_count(
        self, hass: HomeAssistant, freezer, tz, now, junction_day, slots
    ) -> None:
        """A give-up leaves the window in its old timing, monotone, nothing
        missing — but the one slot (SA) / hour (QLD) the first corrected
        poll's trailing-week junction counted twice STAYS counted twice:
        only a complete stage's M1 drop removes it. Signed residual ==
        exactly those named slots of the junction day."""
        from homeassistant.helpers import issue_registry as ir

        freezer.move_to(now)
        coord, agl = _e2e_coordinator(hass, tz)
        today = datetime.now(UTC).date()
        agl.fail = today - timedelta(days=TZ_REALIGN_DAYS - 10)
        days = _history(today)
        await _seed_old(hass, coord, tz, days)

        for _ in range(MAX_TZ_REALIGN_ATTEMPTS):
            await _cycle(hass, coord)

        record = _record(coord)
        assert record["reason"] == TZ_REALIGN_GAVE_UP
        assert record["consumption"] == TZ_REALIGN_GAVE_UP
        rows = await _rows(hass, _CONS)
        assert _steps(rows) == []
        junction = today - timedelta(days=junction_day)
        bump = sum(_cons_kwh(junction, i) for i in slots)
        assert rows[-1]["sum"] - _total(days, tz) == pytest.approx(bump, abs=1e-6)
        issue_id = f"{ISSUE_TZ_REALIGN_GAVE_UP}_{coord.config_entry.entry_id}"
        assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None

    async def test_http_ok_empty_day_is_not_a_failure(
        self, hass: HomeAssistant, freezer
    ) -> None:
        """An HTTP-OK empty day inside the window (zero-on-zero battery day)
        is not a skip: the stage completes on the first cycle."""
        freezer.move_to("2026-10-10T03:00:00+00:00")
        coord, agl = _e2e_coordinator(hass, _ADL)
        today = datetime.now(UTC).date()
        agl.empty = today - timedelta(days=TZ_REALIGN_DAYS - 10)
        await _seed_old(hass, coord, _ADL, _history(today))

        await _cycle(hass, coord)

        assert _steps(await _rows(hass, _CONS)) == []
        record = _record(coord)
        assert record["state"] == TZ_REALIGN_DONE
        assert record["consumption"] == TZ_REALIGN_COMPLETE

    async def test_unexpected_exception_counts_one_attempt(
        self, hass: HomeAssistant, freezer
    ) -> None:
        """A non-AGLError escaping the sweep still counts one attempt (#151
        parity) and propagates; the next clean cycle completes."""
        freezer.move_to("2026-10-10T03:00:00+00:00")
        coord, agl = _e2e_coordinator(hass, _ADL)
        today = datetime.now(UTC).date()
        agl.boom = today - timedelta(days=15)
        days = _history(today)
        await _seed_old(hass, coord, _ADL, days)

        with pytest.raises(RuntimeError):
            await _cycle(hass, coord)
        record = _record(coord)
        assert record["state"] == TZ_REALIGN_PENDING
        assert (record["attempts"], record["halts"]) == (1, 0)

        agl.boom = None
        await _cycle(hass, coord)
        assert _record(coord)["reason"] == TZ_REALIGN_COMPLETE
        rows = await _rows(hass, _CONS)
        assert rows[-1]["sum"] - _total(days, _ADL) == pytest.approx(0.0, abs=1e-6)


class TestEndToEndAllNewConvention:
    async def test_beta3_entry_zero_net_change_hedged_notice(
        self, hass: HomeAssistant, freezer
    ) -> None:
        """C10: an Adelaide entry whose history is ALREADY corrected (set up
        on 0.5.0-beta.3+) is armed and completes with zero net change and
        zero steps — the junction classifier drops nothing — and still gets
        the hedged history notice (accepted)."""
        from homeassistant.helpers import issue_registry as ir

        freezer.move_to("2026-10-10T03:00:00+00:00")
        coord, _agl = _e2e_coordinator(hass, _ADL)
        today = datetime.now(UTC).date()
        days = _history(today)
        await coord._import_intervals(
            [r for d in days for r in _day_readings(d, _ADL, old=False)]
        )
        await async_wait_recording_done(hass)
        seeded = await _rows(hass, _CONS)

        await _cycle(hass, coord)

        rows = await _rows(hass, _CONS)
        assert _steps(rows) == []
        assert rows[-1]["sum"] == pytest.approx(seeded[-1]["sum"], abs=1e-6)
        # The seed batch itself lost only its first day's 00:00 slot (the SA
        # straddle trim of a batch's first bucket); the cycle adds nothing.
        shortfall = _cons_kwh(days[0], 0)
        assert rows[-1]["sum"] - _total(days, _ADL) == pytest.approx(
            -shortfall, abs=1e-6
        )
        record = _record(coord)
        assert record["reason"] == TZ_REALIGN_COMPLETE
        assert record["notice"] is True
        issue_id = f"{ISSUE_TZ_HISTORY_BEFORE_REALIGN}_{coord.config_entry.entry_id}"
        assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None


class TestEndToEndUnsupportedShift:
    """M4 / RA-19: NT and WA are never re-aligned — pin what that costs."""

    @pytest.mark.parametrize(
        ("zone", "junction_day", "cons_slots", "gen_slots"),
        [
            # WA, Sydney DST: 3 h shift. The first local 3 h of consumption
            # and the first 2.5 h of export (06:30-09:00) of the junction day.
            ("Australia/Perth", 7, range(6), range(13, 18)),
            # NT, Sydney DST: 90 min shift; half-hour zone, so the junction
            # is the overlap day, after its straddle-trimmed first slot.
            ("Australia/Darwin", 8, range(1, 4), range(13, 16)),
        ],
        ids=["WA", "NT"],
    )
    async def test_ntwa_first_corrected_poll_double_counts_silently(
        self,
        hass: HomeAssistant,
        freezer,
        caplog: pytest.LogCaptureFixture,
        zone: str,
        junction_day: int,
        cons_slots: range,
        gen_slots: range,
    ) -> None:
        """Old-convention NT/WA history, frozen in Sydney DST, one cycle.

        done(unsupported_shift) with no realign window fetched and NO
        Repairs issue even though shifted rows predate the floor. The first
        corrected poll's trailing-week junction re-imports the junction
        day's leading shift-width of old-timing slots: consumption AND
        generation end permanently high by exactly those named slots, with
        no downward step and NO haggle WARNING — the RA-19 residual.
        """
        from homeassistant.helpers import issue_registry as ir

        tz = ZoneInfo(zone)
        freezer.move_to("2026-11-20T03:00:00+00:00")
        coord, agl = _e2e_coordinator(hass, tz, solar=True)
        today = datetime.now(UTC).date()
        days = _history(today)
        await _seed_old(hass, coord, tz, days)
        await _seed_old(hass, coord, tz, days, generation=True)
        caplog.clear()

        await _cycle(hass, coord)

        assert _record(coord) == {
            "state": TZ_REALIGN_DONE,
            "zone": zone,
            "reason": TZ_REALIGN_REASON_UNSUPPORTED_SHIFT,
        }
        # Normal ranges only: no 30-day realign window on either series.
        horizon = today - timedelta(days=REWINDOW_DAYS + 1)
        assert min(agl.cons_days) >= horizon
        assert min(agl.solar_days) >= horizon
        registry = ir.async_get(hass)
        for kind in (ISSUE_TZ_HISTORY_BEFORE_REALIGN, ISSUE_TZ_REALIGN_GAVE_UP):
            issue_id = f"{kind}_{coord.config_entry.entry_id}"
            assert registry.async_get_issue(DOMAIN, issue_id) is None
        junction = today - timedelta(days=junction_day)
        cons = await _rows(hass, _CONS)
        gen = await _rows(hass, _GEN)
        assert _steps(cons) == []
        assert _steps(gen) == []
        assert cons[-1]["sum"] - _total(days, tz) == pytest.approx(
            sum(_cons_kwh(junction, i) for i in cons_slots), abs=1e-6
        )
        assert gen[-1]["sum"] - _total(days, tz, _gen_kwh) == pytest.approx(
            sum(_gen_kwh(junction, i) for i in gen_slots), abs=1e-6
        )
        assert _haggle_warnings(caplog) == []
