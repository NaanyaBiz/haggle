"""Real-recorder tests for the #292 PR2b timezone history re-alignment machine.

These tests use phcc's real in-memory SQLite recorder to verify that
the junction-drop logic produces a monotone, conservation-correct sum chain
at the floor boundary (M1 exact junction).

Must be a separate module from test_tz_realign.py: phcc's recorder_db_url
asserts the recorder fixtures are initialised BEFORE the hass fixture; the
module-level autouse fixture override is the only clean way to guarantee that
ordering without affecting non-recorder tests.

C11 unique-per-slot profile: kwh = 0.10 + 0.01 * slot_index.

The end-to-end classes drive the production cycle (_async_update_data) on
the real recorder with frozen dates and pin signed residuals: M1 for
consumption (SA, and QLD with the floor in Sydney DST) and generation,
stage order plus the per-cycle request budget, C1 halt / per-day-skip
degradation, HTTP-OK empty days, the exception attempt, and C10.
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

from custom_components.haggle.agl.client import AGLError, AGLRateLimitError
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
    CONF_TZ_REALIGN,
    DOMAIN,
    ISSUE_TZ_HISTORY_BEFORE_REALIGN,
    RETRY_INTERVAL_ON_ERROR,
    REWINDOW_DAYS,
    STAT_CONSUMPTION,
    STAT_GENERATION,
    STAT_GENERATION_CREDIT,
    TZ_REALIGN_COMPLETE,
    TZ_REALIGN_DAYS,
    TZ_REALIGN_DONE,
    TZ_REALIGN_GENERATION,
    TZ_REALIGN_PENDING,
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
    hass: HomeAssistant, tz: ZoneInfo, *, solar: bool = False
) -> tuple[HaggleCoordinator, _Agl]:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_REFRESH_TOKEN: "v1.testtoken",
            CONF_CONTRACT_NUMBER: _CONTRACT,
            CONF_ACCOUNT_NUMBER: _ACCOUNT,
            CONF_LOCAL_TZ: tz.key,
        },
        unique_id=f"{_ACCOUNT}_{_CONTRACT}",
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
    coord = HaggleCoordinator(hass, entry, client, _CONTRACT)
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
        record = _record(coord)
        assert record["state"] == TZ_REALIGN_DONE
        assert record["reason"] == TZ_REALIGN_COMPLETE
        assert record["generation"] == TZ_REALIGN_COMPLETE


class TestEndToEndIncompleteStage:
    """C1: an incomplete stage writes exactly what a normal poll would."""

    @pytest.mark.parametrize("fault", ["halt", "fail"])
    async def test_incomplete_sweep_degrades_then_completes(
        self, hass: HomeAssistant, freezer, fault: str
    ) -> None:
        """Adelaide, a 429 (halt) or an AGL 500 (per-day skip) on floor+10.

        Cycle 1: no stored row before the normal rewindow changes, zero
        steps; the halt counts one HALT (fast retry), the skip one ATTEMPT
        (configured cadence). Cycle 2, fault gone: complete and exact.
        """
        freezer.move_to("2026-10-10T03:00:00+00:00")
        coord, agl = _e2e_coordinator(hass, _ADL)
        today = datetime.now(UTC).date()
        floor = today - timedelta(days=TZ_REALIGN_DAYS)
        setattr(agl, fault, floor + timedelta(days=10))
        days = _history(today)
        await _seed_old(hass, coord, _ADL, days)
        before = {r["start"]: r["state"] for r in await _rows(hass, _CONS)}

        await _cycle(hass, coord)

        rows = await _rows(hass, _CONS)
        # The normal SA rewindow fetches today-8 (overlap) .. yesterday; its
        # straddle bucket reaches back to 23:00 local of today-9.
        horizon = datetime.combine(
            today - timedelta(days=9), datetime.min.time(), UTC
        ).timestamp()
        changed = [
            r["start"]
            for r in rows
            if r["start"] < horizon and abs(before[r["start"]] - r["state"]) > 1e-12
        ]
        assert changed == []
        assert _steps(rows) == []
        record = _record(coord)
        assert record["state"] == TZ_REALIGN_PENDING
        assert record["series"] == "consumption"
        if fault == "halt":
            assert (record["attempts"], record["halts"]) == (0, 1)
            assert coord.update_interval == RETRY_INTERVAL_ON_ERROR
        else:
            assert (record["attempts"], record["halts"]) == (1, 0)
            assert coord.update_interval == timedelta(hours=24)

        setattr(agl, fault, None)
        freezer.tick(timedelta(minutes=30))
        await _cycle(hass, coord)

        rows = await _rows(hass, _CONS)
        assert _steps(rows) == []
        assert rows[-1]["sum"] - _total(days, _ADL) == pytest.approx(0.0, abs=1e-6)
        assert _record(coord)["reason"] == TZ_REALIGN_COMPLETE

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
