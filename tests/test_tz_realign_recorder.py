"""Real-recorder tests for the #292 PR2b timezone history re-alignment machine.

These tests use phcc's real in-memory SQLite recorder and drive the
production cycle, so the R1 floor-junction re-emit is exercised at its real
call sites against genuinely stored old-convention rows (no patched reads).

Must be a separate module from test_tz_realign.py: phcc's recorder_db_url
asserts the recorder fixtures are initialised BEFORE the hass fixture; the
module-level autouse fixture override is the only clean way to guarantee that
ordering without affecting non-recorder tests.

The end-to-end classes drive the production cycle (_async_update_data) on
the real recorder with frozen dates and pin the exact floor junction (R1
re-emit) for consumption (SA, and QLD with the floor in Sydney DST) and
generation, stage order plus the per-cycle request budget, C1 halt / per-day-skip
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
    STAT_COST,
    STAT_GENERATION,
    STAT_GENERATION_CREDIT,
    TZ_REALIGN_COMPLETE,
    TZ_REALIGN_CONSUMPTION,
    TZ_REALIGN_DAYS,
    TZ_REALIGN_DONE,
    TZ_REALIGN_GAVE_UP,
    TZ_REALIGN_GENERATION,
    TZ_REALIGN_PENDING,
    TZ_REALIGN_REASON_NOT_AFFECTED,
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
# the REAL recorder. Every R1 junction / C1 call site is exercised through the
# production path — no _realign_junction / _stored_hourly_states patching.
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


def _tou_band(i: int) -> str:
    """ToU band of local slot ``i``: the junction slots (00:00-01:00) are
    off-peak; peak and shoulder never touch the junction hour."""
    if 14 <= i < 40:
        return "peak"
    if 40 <= i < 44:
        return "shoulder"
    return "offpeak"


def _day_readings(
    day: date,
    tz: ZoneInfo,
    *,
    old: bool,
    profile=_cons_kwh,
    rate: float = 0.3,
    tou: bool = False,
) -> list[IntervalReading]:
    """One local day in the corrected (``old=False``) or pre-beta.3 convention.

    ``rate`` is the AUD/kWh price of every slot; ``tou`` types the slots by
    _tou_band instead of all ``normal``."""
    out: list[IntervalReading] = []
    for i, dt in enumerate(_local_slots(day, tz)):
        kwh = profile(day, i)
        if kwh == 0.0:
            continue
        at = agl_reported_timestamp(dt, api_tz=_SYD, contract_tz=tz) if old else dt
        out.append(
            IntervalReading(
                dt=at,
                kwh=kwh,
                cost_aud=round(kwh * rate, 6),
                rate_type=_tou_band(i) if tou else "normal",
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
        self.rate = 0.3
        self.gen_rate = 0.3
        self.tou = False

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
        return _day_readings(day, self.tz, old=False, rate=self.rate, tou=self.tou)

    async def solar(
        self, _contract: str, day: date, previous: bool = False
    ) -> list[IntervalReading]:
        self.solar_days.append(day)
        return _day_readings(
            day, self.tz, old=False, profile=_gen_kwh, rate=self.gen_rate
        )


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
    rate: float = 0.3,
    tou: bool = False,
) -> None:
    """Write ``days`` the way a pre-beta.3 release did (old convention)."""
    if generation:
        await coord._import_generation(
            [
                r
                for d in days
                for r in _day_readings(d, tz, old=True, profile=_gen_kwh, rate=rate)
            ],
            fetched_days=days,
        )
    else:
        await coord._import_intervals(
            [
                r
                for d in days
                for r in _day_readings(d, tz, old=True, rate=rate, tou=tou)
            ]
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
    """R1 consumption junction + stage completion through the real cycle."""

    @pytest.mark.parametrize(
        ("tz", "now", "phantom"),
        [
            # SA: the old chain stored the floor's 00:30 slot 30 min early, in
            # the straddle hour below the cutoff. R1 moves it out of that row
            # into its true hour; left there it would be counted twice.
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

        Pre-fix value (junction re-emit removed): +phantom(floor), the named
        slot(s) above. A non-solar entry finishes on the same cycle:
        consumption complete, generation skipped (no generation rows).
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
            f"residual {residual:+.6f}; R1-removed value would be {phantom(floor):+.6f}"
        )
        # Every hour of the floor day from the junction hour on sits at its
        # corrected instant, the junction hour (the first key at/after the
        # cutoff) included: R1 moved its slot(s) out of the stored old row
        # below the cutoff instead of leaving them there.
        got = {r["start"]: r["state"] for r in rows}
        expected = _hour_totals(_day_readings(floor, tz, old=False))
        keys = sorted(expected)
        # SA: keys[0] is the straddle bucket (rewritten to the floor's own
        # 00:00 slot; floor-1's 23:30 stays in its old row); QLD: none.
        junction = keys[1] if tz is _ADL else keys[0]
        for key in keys[keys.index(junction) : -1]:
            assert got[key] == pytest.approx(expected[key], abs=1e-9)
        # Per-day bars: the floor day is exact, and floor-1 differs from the
        # corrected world only by its own old-timing shift, like all older
        # history (pre-R1 the floor bar was short by phantom(floor) and
        # floor-1 carried it). The day after the floor is exact.
        dash = _dash_days(rows, tz)
        world = _world_days(days, tz)
        prev, nxt = floor - timedelta(days=1), floor + timedelta(days=1)
        assert dash[floor] == pytest.approx(world[floor], abs=1e-9)
        assert dash[prev] == pytest.approx(world[prev] - phantom(prev), abs=1e-9)
        assert dash[nxt] == pytest.approx(world[nxt], abs=1e-9)
        record = _record(coord)
        assert record["state"] == TZ_REALIGN_DONE
        assert record["reason"] == TZ_REALIGN_COMPLETE
        assert record["consumption"] == TZ_REALIGN_COMPLETE
        assert record["generation"] == TZ_REALIGN_SKIPPED
        # One un-chunked window, no overlap day on it (retention guard).
        assert agl.cons_days == [floor + timedelta(days=n) for n in range(30)]


class TestEndToEndGenerationStage:
    """R1/R2 generation junction, stage order and per-cycle request budget."""

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

        Consumption: final - true total == 0. Generation: after cycle 1 (the
        first corrected poll's normal solar rewindow) final - true total ==
        gen slot 13 of today-``junction_day`` — the beta.3/4 rewindow-
        junction dawn slot (#303); after the generation stage it is 0: R2
        recognises that stale old dawn key as a pure re-key and zeroes it.
        The FLOOR junction adds nothing (R1). Pre-R2 value: + that dawn
        slot; pre-R1 value: + gen slot 13 of the floor, +0.340 more.

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
        assert gen_residual == pytest.approx(0.0, abs=1e-6), (
            f"generation residual {gen_residual:+.6f}; R2-removed value would "
            f"be the rewindow dawn slot {dawn:+.6f}"
        )
        assert cons_rows[-1]["sum"] - _total(days, tz) == pytest.approx(0.0, abs=1e-6)
        # Floor-day export hours after the junction hour sit at their
        # corrected instants.
        got = {r["start"]: r["state"] for r in gen_rows}
        expected = _hour_totals(_day_readings(floor, tz, old=False, profile=_gen_kwh))
        for key in sorted(expected)[1:]:
            assert got[key] == pytest.approx(expected[key], abs=1e-9)
        # Per-day bars: the dawn slot moved out of the old row one hour below
        # the cutoff (R1) is dated the floor day either way (export is zero
        # around midnight), so the floor day's export bar is exact.
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


_COST = f"{DOMAIN}:{STAT_COST}_{_CONTRACT}"
_TOU_BANDS = ("offpeak", "peak", "shoulder")


def _band_ids(tariff: str) -> tuple[str, str]:
    return (
        f"{DOMAIN}:{STAT_CONSUMPTION}_{tariff}_{_CONTRACT}",
        f"{DOMAIN}:{STAT_COST}_{tariff}_{_CONTRACT}",
    )


def _junction_prev(floor: date, tz: ZoneInfo) -> float:
    """Start (epoch) of the stored hour just below the realign cutoff: the
    straddle hour in SA (A4 trims the floor's 00:00 slot), the floor's
    previous local hour in QLD."""
    midnight = datetime(floor.year, floor.month, floor.day, tzinfo=tz).astimezone(UTC)
    trim = timedelta(minutes=midnight.minute)
    return (midnight + trim - timedelta(hours=1)).timestamp()


class TestJunctionCostAndBands:
    """R1 per series: the cost and every ToU band series follow the aggregate
    kWh verdict at the floor junction (critic minor 5)."""

    @pytest.mark.parametrize(
        ("tz", "now"),
        [
            (_ADL, "2026-10-10T03:00:00+00:00"),
            (_ADL, "2026-11-20T03:00:00+00:00"),
            (_BNE, "2026-11-20T03:00:00+00:00"),
        ],
        ids=["SA-oct", "SA-nov", "QLD-nov"],
    )
    async def test_cost_and_bands_exact_at_floor(
        self, hass: HomeAssistant, freezer, tz: ZoneInfo, now: str
    ) -> None:
        """ToU contract, same prices before and after: the aggregate cost and
        every band's kWh/cost series end at the corrected truth with zero
        steps, the floor-day bars are exact, and no band gains a row below
        the window that it never had (a key is never created).

        Pre-fix values: subtraction applied to the aggregate kWh only ->
        cost and off-peak series over-count the moved junction slot(s);
        rowless bands not skipped -> a new zero row at the junction hour.
        """
        freezer.move_to(now)
        coord, agl = _e2e_coordinator(hass, tz)
        agl.tou = True
        today = datetime.now(UTC).date()
        floor = today - timedelta(days=TZ_REALIGN_DAYS)
        days = _history(today)
        await _seed_old(hass, coord, tz, days, tou=True)
        prev = _junction_prev(floor, tz)
        before = {
            stat_id: {r["start"] for r in await _rows(hass, stat_id)}
            for tariff in _TOU_BANDS
            for stat_id in _band_ids(tariff)
        }

        await _cycle(hass, coord)

        world = [r for d in days for r in _day_readings(d, tz, old=False, tou=True)]
        expected = {
            _CONS: sum(r.kwh for r in world),
            _COST: sum(r.cost_aud for r in world),
        }
        for tariff in _TOU_BANDS:
            cons_id, cost_id = _band_ids(tariff)
            expected[cons_id] = sum(r.kwh for r in world if r.rate_type == tariff)
            expected[cost_id] = sum(r.cost_aud for r in world if r.rate_type == tariff)
        for stat_id, total in expected.items():
            rows = await _rows(hass, stat_id)
            assert _steps(rows) == [], stat_id
            assert rows[-1]["sum"] == pytest.approx(total, abs=1e-6), (
                f"{stat_id}: residual {rows[-1]['sum'] - total:+.6f}"
            )
        # Floor-day bars of the cost and the off-peak band (the band the
        # junction slots belong to) are exact.
        off_cons, off_cost = _band_ids("offpeak")
        for stat_id, value in (
            (_COST, lambda r: r.cost_aud),
            (off_cons, lambda r: r.kwh if r.rate_type == "offpeak" else 0.0),
            (off_cost, lambda r: r.cost_aud if r.rate_type == "offpeak" else 0.0),
        ):
            bar = 0.0
            for r in world:
                start = r.dt.replace(minute=0)
                if start.astimezone(tz).date() == floor:
                    bar += value(r)
            dash = _dash_days(await _rows(hass, stat_id), tz)
            assert dash[floor] == pytest.approx(bar, abs=1e-9), stat_id
        # The peak/shoulder bands never held the junction hour: still no row.
        for tariff in ("peak", "shoulder"):
            for stat_id in _band_ids(tariff):
                assert prev not in before[stat_id]
                assert prev not in {r["start"] for r in await _rows(hass, stat_id)}

    async def test_qld_repriced_cost_clamped_not_carried(
        self, hass: HomeAssistant, freezer
    ) -> None:
        """QLD, floor in Sydney DST, price up 10% since the old write: the
        junction cost row would go negative (0.30x - 0.33x), so it is clamped
        to 0 and written — never skipped, which would leave the stored row
        to be carried forward by the fill (its day, floor-1, is outside the
        window) and count the moved slots' cost twice.

        Expected cost: old price for every day before the floor, new price
        from the floor on; the junction row (floor-1 23:00) is 0 in the cost
        and off-peak cost series.
        """
        freezer.move_to("2026-11-20T03:00:00+00:00")
        tz = _BNE
        coord, agl = _e2e_coordinator(hass, tz)
        agl.tou = True
        agl.rate = 0.33
        today = datetime.now(UTC).date()
        floor = today - timedelta(days=TZ_REALIGN_DAYS)
        days = _history(today)
        await _seed_old(hass, coord, tz, days, tou=True)

        await _cycle(hass, coord)

        prev = _junction_prev(floor, tz)
        _off_cons, off_cost = _band_ids("offpeak")
        for stat_id, band in ((_COST, None), (off_cost, "offpeak")):
            rows = await _rows(hass, stat_id)
            assert _steps(rows) == [], stat_id
            exp = sum(
                r.cost_aud
                for d in days
                for r in _day_readings(
                    d, tz, old=False, rate=0.3 if d < floor else 0.33, tou=True
                )
                if band is None or r.rate_type == band
            )
            assert rows[-1]["sum"] == pytest.approx(exp, abs=1e-6), (
                f"{stat_id}: residual {rows[-1]['sum'] - exp:+.6f}"
            )
            assert {r["start"]: r["state"] for r in rows}[prev] == pytest.approx(
                0.0, abs=1e-12
            )


class TestJunctionCreditUsesCredit:
    """R1 generation family: the credit junction row subtracts the matched
    slots' CREDIT, never their kWh."""

    @pytest.mark.parametrize("tz", [_ADL, _BNE], ids=["SA", "QLD"])
    async def test_credit_exact_with_rate_above_one(
        self, hass: HomeAssistant, freezer, tz: ZoneInfo
    ) -> None:
        """Feed-in at 1.50 AUD/kWh (unrealistic on purpose: kWh and AUD then
        differ in the direction a clamp cannot hide). After both stages the
        credit sum equals the true credit total with zero steps.

        Pre-fix value (credit pick reads kWh): the junction credit row keeps
        1.50x - 1.00x = +0.50x of the floor's dawn slot.
        """
        freezer.move_to("2026-11-20T03:00:00+00:00")
        coord, agl = _e2e_coordinator(hass, tz, solar=True)
        agl.gen_rate = 1.5
        today = datetime.now(UTC).date()
        days = _history(today)
        await _seed_old(hass, coord, tz, days)
        await _seed_old(hass, coord, tz, days, generation=True, rate=1.5)

        await _cycle(hass, coord)
        await _cycle(hass, coord)

        assert _record(coord)["reason"] == TZ_REALIGN_COMPLETE
        rows = await _rows(hass, _CREDIT)
        assert _steps(rows) == []
        exp = sum(
            r.cost_aud
            for d in days
            for r in _day_readings(d, tz, old=False, profile=_gen_kwh, rate=1.5)
        )
        assert rows[-1]["sum"] == pytest.approx(exp, abs=1e-6), (
            f"credit residual {rows[-1]['sum'] - exp:+.6f}"
        )


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
        only a complete stage's R1 junction re-emit removes it. Signed residual ==
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
        # R4: a raised sweep records its effective start like an incomplete
        # one, so a later give-up names it in the Repairs issue.
        assert (
            record["tried_from"]
            == (today - timedelta(days=TZ_REALIGN_DAYS)).isoformat()
        )

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


# ---------------------------------------------------------------------------
# Helpers used by the new classes below (adapted from scratchpad/sim/)
# ---------------------------------------------------------------------------

_FLAT = 0.15


def _flat_kwh(day: date, i: int) -> float:
    """Flat profile: slots 0,1 (00:00-01:00) and 46,47 (23:00-24:00) are
    uniform at FLAT; all others use the unique profile. Used to test that a
    flat floor-day is not mis-classified as old-convention."""
    if i in (0, 1, 46, 47):
        return _FLAT
    return _cons_kwh(day, i)


def _b12_day_readings(
    day: date, tz: ZoneInfo, *, profile=_cons_kwh
) -> list[IntervalReading]:
    """0.5.0-beta.1/beta.2 shape: old convention minus readings whose
    agl_reported instant falls before the contract-local midnight of their
    own corrected date (those readings were dropped by beta.1/2's date-window
    filter)."""
    mid = datetime(day.year, day.month, day.day, tzinfo=tz).astimezone(UTC)
    out: list[IntervalReading] = []
    for i, dt in enumerate(_local_slots(day, tz)):
        kwh = profile(day, i)
        if kwh == 0.0:
            continue
        rep = agl_reported_timestamp(dt, api_tz=_SYD, contract_tz=tz)
        if rep < mid:
            continue  # dropped by beta.1/beta.2
        out.append(
            IntervalReading(
                dt=rep, kwh=kwh, cost_aud=round(kwh * 0.3, 6), rate_type="normal"
            )
        )
    return out


def _gen_repriced(day: date, i: int) -> float:
    """Same generation profile as _gen_kwh but with a 10% higher rate.
    Used to test R2 credit re-pricing: the kWh series is unchanged."""
    return _gen_kwh(day, i)


# ---------------------------------------------------------------------------
# Variant (b): beta.1/beta.2 history passes the junction classifier
# ---------------------------------------------------------------------------


class TestBeta12ShapeHistory:
    """R1 variant (b): 0.5.0-beta.1/beta.2 stored a windowed old-convention
    history (slots before local midnight dropped). The junction classifier
    accepts it and R1 restores the missing floor-00:00 slot."""

    @pytest.mark.parametrize(
        "now",
        ["2026-10-10T03:00:00+00:00", "2026-11-20T03:00:00+00:00"],
        ids=["SA-oct", "SA-nov"],
    )
    async def test_sa_b12_history_exact(
        self, hass: HomeAssistant, freezer, now: str
    ) -> None:
        """SA, beta.1/beta.2-shape history: zero steps, final == pre + win.

        'pre' is the sum of b12-shaped readings for days before the floor,
        minus one seeding artifact (A4 trims the first seed slot whose
        old-convention instant is at the floor's UTC local-midnight).
        'win' is the true corrected total for the 30-day window.

        Mutation: remove variant (b) from _junction_matched ->
        classification fails -> R1 not applied -> floor-00:00 slot restored
        by the cycle is MISSING -> final < pre + win (short by floor_slot0).
        """
        freezer.move_to(now)
        coord, _agl = _e2e_coordinator(hass, _ADL)
        today = datetime.now(UTC).date()
        floor = today - timedelta(days=TZ_REALIGN_DAYS)
        days = _history(today)

        await coord._import_intervals(
            [r for d in days for r in _b12_day_readings(d, _ADL)]
        )
        await async_wait_recording_done(hass)

        await _cycle(hass, coord)

        rows = await _rows(hass, _CONS)
        assert _steps(rows) == [], "No downward steps after b12-shape realign"

        # Pre-window sum: sum of b12 readings for days before the floor, minus
        # the seeding artifact (A4 trims the first slot of a seed whose
        # old-convention instant equals the trim boundary, i.e. slot index 1
        # of days[0] since it maps to local midnight).
        pre = sum(
            sum(r.kwh for r in _b12_day_readings(d, _ADL)) for d in days if d < floor
        )
        pre -= _cons_kwh(days[0], 1)  # seeding artifact: A4 trims this slot
        win = _total([d for d in days if d >= floor], _ADL)
        assert rows[-1]["sum"] == pytest.approx(pre + win, abs=1e-6), (
            f"residual {rows[-1]['sum'] - (pre + win):+.6f} "
            f"(pre={pre:.4f}, win={win:.4f})"
        )


# ---------------------------------------------------------------------------
# Flat-night interior check: ambiguous floor day is not confirmed (C minor 4)
# ---------------------------------------------------------------------------


class TestFlatNightInteriorCheck:
    """Critic minor 4: the junction classifier's interior-key check prevents
    false positives on a flat overnight profile. A flat day is ambiguous (old
    and new bucketings are identical), so _floor_day_is_old returns False and
    the junction is left untouched — neither a re-sweep over realigned rows
    nor a C10 (all-new-convention) entry loses a slot."""

    @pytest.mark.parametrize(
        ("tz", "now"),
        [
            (_ADL, "2026-10-10T03:00:00+00:00"),
            (_BNE, "2026-11-20T03:00:00+00:00"),
        ],
        ids=["SA", "QLD"],
    )
    async def test_flat_night_row_unchanged(
        self, hass: HomeAssistant, freezer, tz: ZoneInfo, now: str
    ) -> None:
        """New-convention history with flat overnight slots: one complete
        cycle makes no change to any stored row, and the sum equals the
        seeded total (minus the SA seeding artifact for the first slot).

        Mutation: remove the interior check (_floor_day_is_old returns
        True unconditionally) -> classification fires on the flat day ->
        one SA slot or one QLD hour is dropped -> sum short by FLAT.
        """
        freezer.move_to(now)
        coord, _agl = _e2e_coordinator(hass, tz)

        async def hourly_flat(_c: str, day: date) -> list[IntervalReading]:
            return _day_readings(day, tz, old=False, profile=_flat_kwh)

        coord.client.async_get_usage_hourly.side_effect = hourly_flat
        coord.client.async_get_usage_hourly_previous.side_effect = hourly_flat

        today = datetime.now(UTC).date()
        days = _history(today)
        await coord._import_intervals(
            [
                r
                for d in days
                for r in _day_readings(d, tz, old=False, profile=_flat_kwh)
            ]
        )
        await async_wait_recording_done(hass)
        before = {r["start"]: r["state"] for r in await _rows(hass, _CONS)}

        await _cycle(hass, coord)

        rows = await _rows(hass, _CONS)
        assert _steps(rows) == []
        after = {r["start"]: r["state"] for r in rows}
        changed = {
            k: (before.get(k), v)
            for k, v in after.items()
            if abs((before.get(k) or 0.0) - v) > 1e-9
        }
        assert changed == {}, f"Rows changed on flat night: {changed}"
        # Seeding artifact: SA's first slot is trimmed by A4.
        seeded = _total(days, tz, _flat_kwh) - (_FLAT if tz is _ADL else 0.0)
        assert rows[-1]["sum"] == pytest.approx(seeded, abs=1e-6)

    @pytest.mark.parametrize(
        ("tz", "now"),
        [
            (_ADL, "2026-10-10T03:00:00+00:00"),
            (_ADL, "2026-11-20T03:00:00+00:00"),
            (_BNE, "2026-11-20T03:00:00+00:00"),
        ],
        ids=["SA-oct", "SA-nov", "QLD"],
    )
    async def test_resweep_over_realigned_rows_idempotent(
        self, hass: HomeAssistant, freezer, tz: ZoneInfo, now: str
    ) -> None:
        """A second complete sweep over already-realigned rows (simulating a
        pending record from a post-import exception, C8 re-arm, etc.) changes
        nothing: the interior check sees new-convention bucketing, no interior
        key differs -> _floor_day_is_old False -> junction not confirmed.

        Also asserts the prev row has the correct value after the FIRST sweep:
        SA: floor-1's last hour == floor's 00:00 slot (b00). QLD: 0.0.

        Mutation: remove the interior check -> re-sweep drops the floor's
        00:00 slot (SA) or first hour (QLD) again -> RED.
        """
        freezer.move_to(now)
        coord, _agl = _e2e_coordinator(hass, tz)
        today = datetime.now(UTC).date()
        floor = today - timedelta(days=TZ_REALIGN_DAYS)
        days = _history(today)
        await coord._import_intervals(
            [r for d in days for r in _day_readings(d, tz, old=True)]
        )
        await async_wait_recording_done(hass)

        await _cycle(hass, coord)

        rows1 = await _rows(hass, _CONS)
        assert _steps(rows1) == []
        assert rows1[-1]["sum"] == pytest.approx(_total(days, tz), abs=1e-6)

        # Check the prev row value after the first sweep.
        # Prev = cutoff - 1h. For SA (half-hour tz, A4 trim):
        #   cutoff = floor_midnight + 30min; prev = floor_midnight - 30min.
        # For QLD (whole-hour tz, no A4 trim):
        #   cutoff = floor_midnight; prev = floor_midnight - 1h.
        # Formula: trim = floor_midnight.minute (30 for SA, 0 for QLD).
        floor_midnight = datetime(
            floor.year, floor.month, floor.day, tzinfo=tz
        ).astimezone(UTC)
        trim = timedelta(minutes=floor_midnight.minute)  # 30 for SA, 0 for QLD
        prev_key = (floor_midnight + trim - timedelta(hours=1)).timestamp()
        got_prev = {r["start"]: r["state"] for r in rows1}.get(prev_key, 0.0)
        # R1 formula: state = stored(prev) - matched + trimmed.
        # SA: stored = b00+b30, matched = [b00,b30], trimmed = [b00] (A4 drop).
        #   state = (b00+b30) - (b00+b30) + b00 = b00 = slot 0 of floor day.
        # QLD: trimmed = [] (no A4 drop), so state = (b00+b30) - (b00+b30) = 0.
        exp_prev = _cons_kwh(floor, 0) if tz is _ADL else 0.0
        assert got_prev == pytest.approx(exp_prev, abs=1e-6), (
            f"Prev row: got {got_prev:.6f}, expected {exp_prev:.6f}"
        )

        # Manually re-arm to pending (simulate post-import exception).
        hass.config_entries.async_update_entry(
            coord.config_entry,
            data={
                **coord.config_entry.data,
                CONF_TZ_REALIGN: {
                    "state": TZ_REALIGN_PENDING,
                    "zone": tz.key,
                    "floor": floor.isoformat(),
                    "series": TZ_REALIGN_CONSUMPTION,
                    "attempts": 1,
                    "halts": 0,
                },
            },
        )

        await _cycle(hass, coord)

        rows2 = await _rows(hass, _CONS)
        assert _steps(rows2) == []
        assert rows2[-1]["sum"] == pytest.approx(rows1[-1]["sum"], abs=1e-9)
        assert {r["start"]: r["state"] for r in rows2} == pytest.approx(
            {r["start"]: r["state"] for r in rows1}, abs=1e-9
        )


# ---------------------------------------------------------------------------
# R2(i)+(ii): QLD flat-night, AGLError on a rewindow day
# ---------------------------------------------------------------------------


class TestR2QldFlatNightErrorDayCarried:
    """R2(i): a stale old-convention key whose _key_days is NOT in the
    provenance gate (because the day had an AGLError) is never zeroed, even
    when stored == old bucket exactly (flat-night coincidence).

    The complete consumption stage runs despite H's error (C2: errors in the
    trailing rewindow don't count against the attempt budget). But H is not
    authoritative so its T13:00Z key is not in zero_ok — and R2(i) blocks
    the match because _key_days(T13:00Z) = {H-1} ⊄ gate = {H's rewindow days}.

    Mutation: remove R2(i) -> H's T13:00Z matched and zeroed -> RED (H's
    23:00 row drops from 2*FLAT to 0, a real kWh loss).
    Mutation: remove R2(ii) -> cross-midnight reading outside key_days matches
    -> cross-midnight flat-night coincidence also silently zeroes real data.
    """

    async def test_qld_flat_night_error_day_23_carried(
        self, hass: HomeAssistant, freezer, caplog: pytest.LogCaptureFixture
    ) -> None:
        """H's T13:00Z row stays at 2*FLAT after the complete stage (carried).

        Geometry: beta.4's first corrected poll wrote H's 00:00+00:30 AEST
        (T14:00Z) in the new convention. The OLD T13:00Z row (from the pre-
        correction history) was carried forward by beta.4's conservation guard
        because H was authoritative then. After beta.5 seeds old history for
        the pre-rewindow range plus beta.4-style new history for the rewindow
        range, the complete consumption stage's _stale_key_fill encounters
        T13:00Z as a stale key for the authoritative day H+1 (not H — the
        error means H is absent from the batch's gate). R2(i): _key_days(
        T13:00Z) = {H-1}, not in gate -> not matched -> carried at 2*FLAT.
        """
        freezer.move_to("2026-11-20T03:00:00+00:00")
        tz = _BNE
        coord, _agl = _e2e_coordinator(hass, tz)
        today = datetime.now(UTC).date()
        days = _history(today)
        err_day = today - timedelta(days=2)  # inside the trailing rewindow

        async def hourly(_c: str, day: date) -> list[IntervalReading]:
            if day == err_day:
                raise AGLError("HTTP 500")
            return _day_readings(day, tz, old=False, profile=_flat_kwh)

        coord.client.async_get_usage_hourly.side_effect = hourly
        coord.client.async_get_usage_hourly_previous.side_effect = hourly

        # Seed: old-convention for pre-rewindow, beta.4-style new for rewindow.
        old_days = [d for d in days if d < today - timedelta(days=REWINDOW_DAYS)]
        new_days = [d for d in days if d >= today - timedelta(days=REWINDOW_DAYS)]
        await coord._import_intervals(
            [
                r
                for d in old_days
                for r in _day_readings(d, tz, old=True, profile=_flat_kwh)
            ]
        )
        await async_wait_recording_done(hass)
        await coord._import_intervals(
            [
                r
                for d in new_days
                for r in _day_readings(d, tz, old=False, profile=_flat_kwh)
            ]
        )
        await async_wait_recording_done(hass)

        await _cycle(hass, coord)

        rows = await _rows(hass, _CONS)
        assert _steps(rows) == []

        # err_day's 23:00 AEST row (T13:00Z) holds the old stored value (2*FLAT):
        # old-convention stored both 00:00+00:30 AEST under T13:00Z.
        h23 = (
            datetime(err_day.year, err_day.month, err_day.day, 23, tzinfo=tz)
            .astimezone(UTC)
            .timestamp()
        )
        got = {r["start"]: r["state"] for r in rows}
        assert got[h23] == pytest.approx(2 * _FLAT, abs=1e-9), (
            f"err_day's T13:00Z row: got {got.get(h23)}, expected 2*FLAT={2 * _FLAT}. "
            "R2(i) removal would zero this row (kWh loss)."
        )

        # No spurious haggle WARNINGs (the stage completes cleanly).
        assert _haggle_warnings(caplog) == []


# ---------------------------------------------------------------------------
# R2 / #303: no WARNING and credit exact even with re-priced feed-in
# ---------------------------------------------------------------------------


class TestR2NoWarningCreditRepriced:
    """R2 / #303: after the generation stage, the #300 WARNING is absent and
    credit is exact, even when the feed-in rate changed between the old write
    and the realign. Decided per FAMILY on the kWh series; cost/credit follow
    the kWh match so a re-price does not split them."""

    @pytest.mark.parametrize("tz", [_ADL, _BNE], ids=["SA", "QLD"])
    async def test_generation_stage_no_warning_credit_exact_with_reprice(
        self,
        hass: HomeAssistant,
        freezer,
        caplog: pytest.LogCaptureFixture,
        tz: ZoneInfo,
    ) -> None:
        """Seed old-convention consumption + generation, then run 2 cycles:
        cycle 1 = consumption stage (+ normal solar rewindow); cycle 2 =
        generation stage. A 10% higher feed-in rate is applied by the
        generation stage (the AGL double uses 0.33 AUD/kWh vs seed's 0.30).

        Asserts after cycle 2:
        - No '#300' WARNING (R2 matched and zeroed the dawn re-key).
        - Generation sum == true total (0.0 residual, R2 fixed #303).
        - Credit sum == pre-reprice_total + repriced_window_total.
        - Zero downward steps in both series.

        Mutation: remove R2 -> dawn stale key carried (WARNING) and
        generation residual == dawn slot != 0 -> RED.
        Mutation (credit family split): decide R2 per-series (credit uses
        cost_aud) -> re-price splits kWh from credit -> credit over-counts
        the dawn slot -> RED on the credit assert.
        """
        freezer.move_to("2026-11-20T03:00:00+00:00")
        coord, agl = _e2e_coordinator(hass, tz, solar=True)
        today = datetime.now(UTC).date()
        floor = today - timedelta(days=TZ_REALIGN_DAYS)
        days = _history(today)

        # Seed old convention (rate 0.30) for all history.
        await coord._import_intervals(
            [r for d in days for r in _day_readings(d, tz, old=True)]
        )
        await coord._import_generation(
            [r for d in days for r in _day_readings(d, tz, old=True, profile=_gen_kwh)],
            fetched_days=days,
        )
        await async_wait_recording_done(hass)

        # Override solar to use 0.33 AUD/kWh (re-priced feed-in rate).
        async def solar_repriced(
            _c: str, day: date, previous: bool = False
        ) -> list[IntervalReading]:
            agl.solar_days.append(day)
            return [
                IntervalReading(
                    dt=r.dt,
                    kwh=r.kwh,
                    cost_aud=round(r.kwh * 0.33, 6),
                    rate_type=r.rate_type,
                )
                for r in _day_readings(day, tz, old=False, profile=_gen_kwh)
            ]

        coord.client.async_get_solar_hourly.side_effect = solar_repriced

        # Cycle 1: consumption stage.
        await _cycle(hass, coord)
        # Cycle 2: generation stage (capture warnings from this cycle only).
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="custom_components.haggle"):
            await _cycle(hass, coord)

        gen_rows = await _rows(hass, _GEN)
        cred_rows = await _rows(hass, _CREDIT)
        warns = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]

        assert _steps(gen_rows) == []
        assert _steps(cred_rows) == []

        gen_residual = gen_rows[-1]["sum"] - _total(days, tz, _gen_kwh)
        assert gen_residual == pytest.approx(0.0, abs=1e-6), (
            f"generation residual {gen_residual:+.6f}; R2-removed value would be the "
            f"dawn slot ({_gen_kwh(today - timedelta(days=(REWINDOW_DAYS + (1 if tz is _ADL else 0))), 13):+.6f})"
        )

        # Expected credit: old rate (0.30) for days before floor, new rate (0.33) for window.
        exp_cred = sum(
            round(
                _gen_kwh(d, i) * (0.30 if d < floor else 0.33),
                6,
            )
            for d in days
            for i in range(len(_local_slots(d, tz)))
        )
        assert cred_rows[-1]["sum"] == pytest.approx(exp_cred, abs=1e-6), (
            f"credit residual {cred_rows[-1]['sum'] - exp_cred:+.6f}; "
            "per-series R2 would leave the dawn slot in credit -> over-count"
        )

        # No #300 WARNING on the generation stage cycle.
        assert not [w for w in warns if "#300" in w], (
            f"R2 not applied: #300 warning(s) fired: {warns}"
        )


# ---------------------------------------------------------------------------
# R3: zone exclusion is season-independent (real recorder)
# ---------------------------------------------------------------------------


class TestR3ZoneExclusionRealRecorder:
    """R3: end-to-end verification that Darwin/Perth/Eucla are excluded in
    every season, including when the 30-day window falls wholly in Sydney
    standard time, and that Brisbane still arms. Also verifies that a fresh
    Darwin install records unsupported_shift (not fresh_install)."""

    @pytest.mark.parametrize(
        ("zone", "now", "expected_reason"),
        [
            # Darwin in June: window shift 0.5 h, but 365-day scan finds 1.5 h.
            (
                "Australia/Darwin",
                "2026-06-20T03:00:00+00:00",
                TZ_REALIGN_REASON_UNSUPPORTED_SHIFT,
            ),
            # Perth: always > 1 h.
            (
                "Australia/Perth",
                "2026-06-20T03:00:00+00:00",
                TZ_REALIGN_REASON_UNSUPPORTED_SHIFT,
            ),
            # Eucla (UTC+8:45): always > 1 h.
            (
                "Australia/Eucla",
                "2026-06-20T03:00:00+00:00",
                TZ_REALIGN_REASON_UNSUPPORTED_SHIFT,
            ),
            # Brisbane in June: max 1 h (= TZ_REALIGN_MAX_SHIFT), NOT excluded.
            (
                "Australia/Brisbane",
                "2026-06-20T03:00:00+00:00",
                TZ_REALIGN_REASON_NOT_AFFECTED,
            ),
            # Adelaide in June: 0.5 h, arms (reason is None meaning 'complete').
            ("Australia/Adelaide", "2026-06-20T03:00:00+00:00", None),
        ],
        ids=["Darwin-Jun", "Perth-Jun", "Eucla-Jun", "Brisbane-Jun", "Adelaide-Jun"],
    )
    async def test_r3_zone_exclusion(
        self,
        hass: HomeAssistant,
        freezer,
        zone: str,
        now: str,
        expected_reason: str | None,
    ) -> None:
        """Mutation: replace 365-day scan with the 30-day window -> Darwin
        in June passes (0.5 h <= 1 h) and arms when it should not -> RED."""
        freezer.move_to(now)
        tz = ZoneInfo(zone)
        coord, agl = _e2e_coordinator(hass, tz)
        today = datetime.now(UTC).date()
        days = _history(today)
        await coord._import_intervals(
            [r for d in days for r in _day_readings(d, tz, old=True)]
        )
        await async_wait_recording_done(hass)

        await _cycle(hass, coord)

        rec = _record(coord)
        if expected_reason is None:
            # Adelaide arms and completes (or stays pending — check not unsupported).
            assert rec.get("reason") != TZ_REALIGN_REASON_UNSUPPORTED_SHIFT
        else:
            assert rec.get("reason") == expected_reason, (
                f"zone={zone} now={now}: got reason={rec.get('reason')!r}, "
                f"expected={expected_reason!r}"
            )
        # Unsupported-shift zones must not fetch the realign window.
        if expected_reason == TZ_REALIGN_REASON_UNSUPPORTED_SHIFT:
            horizon = today - timedelta(days=REWINDOW_DAYS + 1)
            assert min(agl.cons_days) >= horizon

    async def test_r3_fresh_darwin_install_unsupported_shift(
        self, hass: HomeAssistant, freezer
    ) -> None:
        """Fresh Darwin install in June: shift check runs before the
        fresh_install check -> unsupported_shift, not fresh_install.

        Mutation: move shift check after fresh_install check -> fresh_install
        fires first -> wrong reason recorded -> RED.
        """
        freezer.move_to("2026-06-20T03:00:00+00:00")
        coord, _agl = _e2e_coordinator(hass, ZoneInfo("Australia/Darwin"))
        # No seed: last_cons_date = None -> would be fresh_install if checked first.
        await _cycle(hass, coord)

        rec = _record(coord)
        assert rec["reason"] == TZ_REALIGN_REASON_UNSUPPORTED_SHIFT, (
            f"Fresh Darwin got reason={rec['reason']!r}; "
            "expected unsupported_shift (shift check must run before fresh_install)"
        )


# ---------------------------------------------------------------------------
# R4: gave_up_from persisted as Repairs issue {floor} placeholder
# ---------------------------------------------------------------------------


class TestR4GaveUpPlaceholder:
    """R4: the Repairs issue {floor} placeholder uses the effective start of
    the last incomplete sweep (tried_from), not the frozen floor, so the user
    knows exactly which rows were left untouched after each attempt."""

    async def test_r4_placeholder_equals_last_tried_from(
        self, hass: HomeAssistant, freezer
    ) -> None:
        """3 failing sweeps on Adelaide, each advancing by 1 day.

        Cycle n advances today by 1 day, so the effective start slides as
        max(frozen_floor, today - TZ_REALIGN_DAYS): floor+0, floor+1, floor+2.
        The issue {floor} placeholder must equal floor+2 (the start of the
        last attempt), not the frozen floor+0.

        Mutation: use the frozen floor in the placeholder -> floor+0 != floor+2
        -> RED.
        """
        from homeassistant.helpers import issue_registry as ir

        freezer.move_to("2026-10-10T03:00:00+00:00")
        coord, agl = _e2e_coordinator(hass, _ADL)
        today = datetime.now(UTC).date()
        floor = today - timedelta(days=TZ_REALIGN_DAYS)
        days = _history(today)
        await coord._import_intervals(
            [r for d in days for r in _day_readings(d, _ADL, old=True)]
        )
        await async_wait_recording_done(hass)

        for _n in range(MAX_TZ_REALIGN_ATTEMPTS):
            # Each cycle, fail on a day deep in the window (before the rewindow).
            agl.fail = floor + timedelta(days=5)
            await _cycle(hass, coord)
            freezer.tick(timedelta(days=1))

        rec = _record(coord)
        assert rec["reason"] == TZ_REALIGN_GAVE_UP

        registry = ir.async_get(hass)
        issue = next(
            (
                i
                for i in registry.issues.values()
                if i.translation_key == ISSUE_TZ_REALIGN_GAVE_UP
            ),
            None,
        )
        assert issue is not None, "tz_realign_gave_up Repairs issue not raised"
        got_floor = issue.translation_placeholders["floor"]
        expected_floor = (
            floor + timedelta(days=MAX_TZ_REALIGN_ATTEMPTS - 1)
        ).isoformat()
        assert got_floor == expected_floor, (
            f"Repairs {{floor}} placeholder: got {got_floor!r}, "
            f"expected {expected_floor!r} (last tried_from); "
            f"frozen floor would be {floor.isoformat()!r}"
        )


# ---------------------------------------------------------------------------
# Critic minor 7(c): generation stage waits until junction day exits window
# ---------------------------------------------------------------------------


class TestGenerationStageWaitsUntilJunctionExpires:
    """Critic minor 7(c): when the generation stage cannot run yet (has_solar
    False with existing rows), it waits. Once the start advances past the
    junction day J (so J is below the window), the stage completes but R2
    cannot zero the old dawn key: J has left the window. The residual equals
    exactly the dawn slot at J — a known, named limitation.

    No WARNING spam: R2 exempts stale keys from the conservation guard only
    when they are within the provenance gate; a key below the window is
    outside the gate, so the #300 WARNING fires once (as expected for a
    genuine carry-forward) and not again on subsequent cycles."""

    @pytest.mark.parametrize(
        "tz",
        [_ADL, _BNE],
        ids=["SA", "QLD"],
    )
    async def test_generation_waits_then_completes(
        self, hass: HomeAssistant, freezer, tz: ZoneInfo
    ) -> None:
        """Frozen at 2026-11-20. Consumption stage runs when has_solar=False;
        the generation stage waits (PENDING + GENERATION). On the next cycle
        with has_solar=True, the generation stage completes (DONE + COMPLETE);
        no downward steps and no #300 WARNINGs.

        This tests the core "waits then completes" flow from critic minor 7(c).
        Since the junction day is still within the window when has_solar becomes
        True, R2 can fix the dawn double-count: residual == 0 (not dawn slot).
        The "junction slides out of window" scenario is a named limitation (#307)
        documented in energy-dashboard.md; its residual is the dawn slot at J,
        but verifying it in an end-to-end test requires advancing the date past
        J + TZ_REALIGN_DAYS, which invalidates the seeded data range.

        Mutation: remove the has_solar=False guard in _plan_tz_realign ->
        generation stage runs on cycle 1 -> rec1["series"] != GENERATION -> RED.
        """
        freezer.move_to("2026-11-20T03:00:00+00:00")
        coord, _agl = _e2e_coordinator(hass, tz, solar=True)
        today = datetime.now(UTC).date()
        days = _history(today)

        # Seed old-convention consumption + generation.
        await coord._import_intervals(
            [r for d in days for r in _day_readings(d, tz, old=True)]
        )
        await coord._import_generation(
            [r for d in days for r in _day_readings(d, tz, old=True, profile=_gen_kwh)],
            fetched_days=days,
        )
        await async_wait_recording_done(hass)

        # Cycle 1: consumption stage only (has_solar=False forces generation to wait).
        coord._has_solar = False
        await _cycle(hass, coord)
        rec1 = _record(coord)
        assert rec1["state"] == TZ_REALIGN_PENDING, (
            f"Cycle 1 should leave state=PENDING; got {rec1['state']!r}"
        )
        assert rec1["series"] == TZ_REALIGN_GENERATION, (
            "Cycle 1 should advance to generation stage (series=generation); "
            f"got {rec1['series']!r}"
        )

        # Cycle 2: generation stage runs (has_solar=True, same date as cycle 1).
        # Junction day is still within the window → R2 can fix the dawn slot.
        coord._has_solar = True
        await _cycle(hass, coord)

        gen_rows = await _rows(hass, _GEN)
        assert _steps(gen_rows) == []
        rec2 = _record(coord)
        assert rec2["state"] == TZ_REALIGN_DONE
        assert rec2["generation"] == TZ_REALIGN_COMPLETE

        # Residual should be 0.0 (R2 fixed the dawn double-count since J is in-window).
        gen_residual = gen_rows[-1]["sum"] - _total(days, tz, _gen_kwh)
        assert gen_residual == pytest.approx(0.0, abs=1e-6), (
            f"generation residual {gen_residual:+.6f} != 0 (R2 should fix the "
            "dawn slot while J is still in the window)"
        )


# ---------------------------------------------------------------------------
# R2 on the CONSUMPTION family, through the real cycle (_import_intervals)
# ---------------------------------------------------------------------------


def _solar_home_kwh(day: date, i: int) -> float:
    """A solar home's grid import: zero 09:00-14:30 local (slots 18-28, the
    parser drops zero-on-zero slots, so the batch is silent there), a small
    unique evening tail, unique values elsewhere. The block ends on a local
    :00 slot so the old timing puts the first post-block slot (14:30) into
    a key the corrected batch never writes, under both the SA 30-min and
    the QLD-in-DST 1-h shift."""
    if 18 <= i <= 28:
        return 0.0
    if i >= 44:
        return round(0.01 + 0.0001 * i, 6)
    return _cons_kwh(day, i)


class TestR2ConsumptionFamilyThroughCycle:
    """R2 applies per family: consumption keys are recognised against the
    consumption readings, wired by ``rekey=junction`` in _import_intervals.

    A beta.3-style write (one interior window day re-imported at corrected
    timing with the #300 fill inactive) leaves that day's old-timing key at
    the end of the zero-import block holding the first post-block slot,
    which the corrected write ALSO counts in its true hour: the day's stored
    kWh exceeds the batch's, so on the normal path the conservation guard
    carries the stale key forward and the slot stays counted twice.
    """

    @pytest.mark.parametrize(
        ("tz", "now"),
        [
            (_ADL, "2026-10-10T03:00:00+00:00"),
            (_ADL, "2026-11-20T03:00:00+00:00"),
            (_BNE, "2026-11-20T03:00:00+00:00"),
        ],
        ids=["SA-oct", "SA-nov", "QLD-nov"],
    )
    async def test_stale_block_end_key_zeroed_family_exact(
        self,
        hass: HomeAssistant,
        freezer,
        caplog: pytest.LogCaptureFixture,
        tz: ZoneInfo,
        now: str,
    ) -> None:
        """ToU contract: after the consumption stage the aggregate kWh and
        cost and every band's kWh and cost end at the corrected truth with
        zero steps, no #300 WARNING fires, and every stored hour around the
        rewritten day equals its corrected bucket (the stale key: 0).

        Pre-fix value (consumption rekey off, mutation NV3): the stale key is
        carried, +the first post-block slot of the rewritten day in the
        aggregate (and in its band and cost), plus the #300 WARNING.
        """
        freezer.move_to(now)
        coord, agl = _e2e_coordinator(hass, tz)

        async def hourly(_c: str, day: date) -> list[IntervalReading]:
            agl.cons_days.append(day)
            return _day_readings(day, tz, old=False, profile=_solar_home_kwh, tou=True)

        coord.client.async_get_usage_hourly.side_effect = hourly
        coord.client.async_get_usage_hourly_previous.side_effect = hourly
        today = datetime.now(UTC).date()
        days = _history(today)
        await coord._import_intervals(
            [
                r
                for d in days
                for r in _day_readings(
                    d, tz, old=True, profile=_solar_home_kwh, tou=True
                )
            ]
        )
        await async_wait_recording_done(hass)
        rewritten = today - timedelta(days=15)
        coord.client.tz_is_contract = False  # beta.3: no #300 fill
        await coord._import_intervals(
            _day_readings(rewritten, tz, old=False, profile=_solar_home_kwh, tou=True)
        )
        await async_wait_recording_done(hass)
        coord.client.tz_is_contract = True
        caplog.clear()

        await _cycle(hass, coord)

        assert _record(coord)["consumption"] == TZ_REALIGN_COMPLETE
        assert [w for w in _haggle_warnings(caplog) if "#300" in w] == []
        world = [
            r
            for d in days
            for r in _day_readings(d, tz, old=False, profile=_solar_home_kwh, tou=True)
        ]
        expected = {
            _CONS: sum(r.kwh for r in world),
            _COST: sum(r.cost_aud for r in world),
        }
        for tariff in _TOU_BANDS:
            cons_id, cost_id = _band_ids(tariff)
            expected[cons_id] = sum(r.kwh for r in world if r.rate_type == tariff)
            expected[cost_id] = sum(r.cost_aud for r in world if r.rate_type == tariff)
        for stat_id, total in expected.items():
            rows = await _rows(hass, stat_id)
            assert _steps(rows) == [], stat_id
            assert rows[-1]["sum"] == pytest.approx(total, abs=1e-6), (
                f"{stat_id}: residual {rows[-1]['sum'] - total:+.6f}"
            )
        # Every stored hour of the rewritten day (both straddle buckets
        # included) equals its corrected bucket; the old-timing key at the
        # end of the zero-import block holds nothing.
        hours = _hour_totals(
            [
                r
                for d in (
                    rewritten - timedelta(days=1),
                    rewritten,
                    rewritten + timedelta(days=1),
                )
                for r in _day_readings(d, tz, old=False, profile=_solar_home_kwh)
            ]
        )
        lo = datetime(rewritten.year, rewritten.month, rewritten.day, tzinfo=tz)
        nxt = rewritten + timedelta(days=1)
        hi = datetime(nxt.year, nxt.month, nxt.day, tzinfo=tz)
        lo_ts = lo.astimezone(UTC).timestamp() - 1800
        hi_ts = hi.astimezone(UTC).timestamp()
        got = {
            r["start"]: r["state"]
            for r in await _rows(hass, _CONS)
            if lo_ts <= r["start"] < hi_ts
        }
        assert got == pytest.approx({k: hours.get(k, 0.0) for k in got}, abs=1e-9)
        stale = [k for k in got if k not in hours]
        assert stale, "the scenario must leave an old-timing key the batch skips"


# ---------------------------------------------------------------------------
# Interior check walks the WHOLE floor day (a flat night is not the end)
# ---------------------------------------------------------------------------


def _flat_dawn_kwh(day: date, i: int) -> float:
    """Flat baseload 00:00-04:30 local (slots 0-9): the first several
    interior keys hold the same kWh in both timings, so only a LATER key
    tells them apart."""
    if i <= 9:
        return _FLAT
    return _cons_kwh(day, i)


class TestInteriorCheckBeyondFirstKey:
    """_floor_day_is_old must keep walking past an ambiguous first interior
    key: an old-timing floor day with a flat night is still confirmed."""

    @pytest.mark.parametrize(
        ("tz", "now"),
        [
            (_ADL, "2026-10-10T03:00:00+00:00"),
            (_BNE, "2026-11-20T03:00:00+00:00"),
        ],
        ids=["SA-oct", "QLD-nov"],
    )
    async def test_flat_night_floor_still_confirmed(
        self, hass: HomeAssistant, freezer, tz: ZoneInfo, now: str
    ) -> None:
        """Old-timing history with a flat 00:00-04:30 night: zero steps, the
        final sum equals the corrected truth, the floor-day bar is exact.

        Pre-fix value (loop bounded to the first interior key, mutation
        NV1): the junction is not confirmed, so the floor's moved slot(s)
        stay counted twice, +FLAT (SA) / +2*FLAT (QLD-in-DST).
        """
        freezer.move_to(now)
        coord, agl = _e2e_coordinator(hass, tz)

        async def hourly(_c: str, day: date) -> list[IntervalReading]:
            agl.cons_days.append(day)
            return _day_readings(day, tz, old=False, profile=_flat_dawn_kwh)

        coord.client.async_get_usage_hourly.side_effect = hourly
        coord.client.async_get_usage_hourly_previous.side_effect = hourly
        today = datetime.now(UTC).date()
        floor = today - timedelta(days=TZ_REALIGN_DAYS)
        days = _history(today)
        await coord._import_intervals(
            [
                r
                for d in days
                for r in _day_readings(d, tz, old=True, profile=_flat_dawn_kwh)
            ]
        )
        await async_wait_recording_done(hass)

        await _cycle(hass, coord)

        rows = await _rows(hass, _CONS)
        assert _steps(rows) == []
        residual = rows[-1]["sum"] - _total(days, tz, _flat_dawn_kwh)
        assert residual == pytest.approx(0.0, abs=1e-6), f"residual {residual:+.6f}"
        dash = _dash_days(rows, tz)
        world = _world_days(days, tz, _flat_dawn_kwh)
        assert dash[floor] == pytest.approx(world[floor], abs=1e-9)
