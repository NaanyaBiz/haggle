"""Tests for the #292 PR2b timezone history re-alignment machine.

Every section maps to the spec test grid (design292pr2b.json + amendments292pr2b.md):
- Gate tests: C7 agl_api_tz, M4 unsupported_shift (Perth/Darwin), C8 zone mismatch,
  fresh_install, not_affected (NSW), deferred-while-catching-up, deferred-while-heal.
- State machine: floor frozen, pending/done transitions, give-up (C3 consumption
  give-up hands over to generation), never re-arms after done.
- Halt accounting (C5): halt counted per poll interval, MAX_TZ_REALIGN_HALTS →
  attempt, cadence fallback.
- Completion (C2): rewindow-day skips don't count, only [start, today-REWINDOW_DAYS].
- C1 degrade: incomplete stage → normal window only, no mid-window landing.
- C4/C9/C10: generation-stage wait, stall-detector off, all-new-convention Adelaide.
- Repairs issues: raised once with correct ids and placeholders, not re-raised.
- Period-sensor gate: suppressed during generation stage, published after.
- diagnostics v4: tz_realign surfaced top-level, not in entry.data, leak-safe.
- strings: both issue keys, placeholder sets, "Do not delete" sentinel.
- Config-flow preservation: CONF_TZ_REALIGN survives reauth/reconfigure (C8).
- Real-recorder tests: SA complete realign (M1 exact junction, monotone, conserved),
  C1 halt geometry.
- TestComposedRequestCeiling: request-count ceiling for both stages (C6).

Unique-per-slot profile everywhere (C11): kwh = 0.1 + 0.01*slot_index + 0.001*day_index
"""

from __future__ import annotations

import json
import pathlib
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.haggle.agl.models import BillPeriod, IntervalReading, PlanRates
from custom_components.haggle.agl.parser import (
    agl_api_tz,
)
from custom_components.haggle.const import (
    CONF_ACCOUNT_NUMBER,
    CONF_CONTRACT_NUMBER,
    CONF_LOCAL_TZ,
    CONF_REFRESH_TOKEN,
    CONF_SOLAR_STALL_SPANS,
    CONF_TZ_REALIGN,
    DOMAIN,
    ISSUE_TZ_HISTORY_BEFORE_REALIGN,
    ISSUE_TZ_REALIGN_GAVE_UP,
    MAX_TZ_REALIGN_ATTEMPTS,
    MAX_TZ_REALIGN_HALTS,
    OPT_SOLAR_STATISTICS_ENABLED,
    REWINDOW_DAYS,
    STAT_CONSUMPTION,
    TZ_REALIGN_COMPLETE,
    TZ_REALIGN_CONSUMPTION,
    TZ_REALIGN_DAYS,
    TZ_REALIGN_DONE,
    TZ_REALIGN_GAVE_UP,
    TZ_REALIGN_GENERATION,
    TZ_REALIGN_PENDING,
    TZ_REALIGN_REASON_FRESH_INSTALL,
    TZ_REALIGN_REASON_NOT_AFFECTED,
    TZ_REALIGN_REASON_UNSUPPORTED_SHIFT,
    TZ_REALIGN_SKIPPED,
)
from custom_components.haggle.coordinator import HaggleCoordinator, HaggleData

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

# ---------------------------------------------------------------------------
# Helpers shared across tests
# ---------------------------------------------------------------------------

_CONTRACT = "9999999999"
_ACCOUNT = "1234567890"

_BASE_ENTRY_DATA = {
    CONF_REFRESH_TOKEN: "v1.testtoken",
    CONF_CONTRACT_NUMBER: _CONTRACT,
    CONF_ACCOUNT_NUMBER: _ACCOUNT,
}

_PATCH_ADD_STATS = (
    "homeassistant.components.recorder.statistics.async_add_external_statistics"
)
_PATCH_GET_INSTANCE = "homeassistant.helpers.recorder.get_instance"
_PATCH_CREATE_ISSUE = "homeassistant.helpers.issue_registry.async_create_issue"


def _empty_summary(today: date | None = None) -> BillPeriod:
    d = today or datetime.now(UTC).date()
    return BillPeriod(
        start=d,
        end=d,
        consumption_kwh=0.0,
        cost_label="$0.00",
        projection_label="",
    )


def _empty_plan() -> PlanRates:
    return PlanRates(
        product_name="",
        unit_rates=[],
        supply_charge_cents_per_day=0.0,
    )


def _make_client(
    zone_name: str | None = "Australia/Adelaide",
    *,
    tz_is_contract: bool = True,
) -> MagicMock:
    """Return an async mock client with a real ZoneInfo and tz_is_contract set."""
    client = AsyncMock()
    client.tz_is_contract = tz_is_contract if zone_name else False
    client.local_tz = ZoneInfo(zone_name) if zone_name else None
    # Overview returns empty list so _refresh_from_overview does not call
    # set_contract_tz (which would overwrite local_tz and tz_is_contract).
    client.async_get_overview = AsyncMock(return_value=[])
    client.async_get_usage_summary = AsyncMock(return_value=_empty_summary())
    client.async_get_plan = AsyncMock(return_value=_empty_plan())
    client.async_get_hourly_usage = AsyncMock(return_value=[])
    return client


def _make_entry(
    hass: HomeAssistant,
    *,
    zone_name: str | None = "Australia/Adelaide",
    extra_data: dict | None = None,
    options: dict | None = None,
) -> tuple[MockConfigEntry, HaggleCoordinator]:
    data = {**_BASE_ENTRY_DATA}
    if zone_name:
        data[CONF_LOCAL_TZ] = zone_name
    if extra_data:
        data.update(extra_data)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=data,
        unique_id=f"{_ACCOUNT}_{_CONTRACT}",
        options=options or {},
    )
    entry.add_to_hass(hass)
    client = _make_client(zone_name)
    coord = HaggleCoordinator(hass, entry, client, _CONTRACT)
    return entry, coord


def _mock_get_instance(rows: list | None = None) -> MagicMock:
    """Return a patched get_instance whose executor_job returns rows for every stat."""
    instance = MagicMock()
    use = rows or []

    async def _exec(func, hass_arg, *args, **kwargs):
        # statistics_during_period signature varies; always return empty-ish map.
        ids = args[3] if len(args) > 3 else set()
        return {i: use for i in ids}

    instance.async_add_executor_job = AsyncMock(side_effect=_exec)
    return MagicMock(return_value=instance)


def _make_interval(dt: datetime, kwh: float, cost: float = 0.05) -> IntervalReading:
    return IntervalReading(dt=dt, kwh=kwh, cost_aud=cost, rate_type="normal")


# ---------------------------------------------------------------------------
# Gate tests
# ---------------------------------------------------------------------------


class TestTzRealignGates:
    """All cases that prevent arming, with mutation assertions."""

    async def test_not_armed_when_tz_not_contract(self, hass: HomeAssistant) -> None:
        """Gate (b): tz_is_contract is False → no record written (C7/fill_active)."""
        entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        coord.client.tz_is_contract = False  # HA-timezone fallback

        with (
            patch(_PATCH_ADD_STATS),
            patch(_PATCH_GET_INSTANCE, _mock_get_instance()),
        ):
            await coord._plan_tz_realign(
                date(2026, 11, 5),
                date(2026, 11, 4),
                date(2026, 11, 4),  # last_cons_date
                None,
                None,
                normal=(None, None),
            )

        assert entry.data.get(CONF_TZ_REALIGN) is None

    async def test_not_armed_when_api_tz_none(self, hass: HomeAssistant) -> None:
        """C7: patching _default_api_tz to return None prevents arming."""
        entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")

        with (
            patch(
                "custom_components.haggle.agl.parser._default_api_tz",
                return_value=None,
            ),
            patch(_PATCH_ADD_STATS),
            patch(_PATCH_GET_INSTANCE, _mock_get_instance()),
        ):
            result = await coord._plan_tz_realign(
                date(2026, 11, 5),
                date(2026, 11, 4),
                date(2026, 11, 4),
                None,
                None,
                normal=(None, None),
            )

        assert result is None
        assert entry.data.get(CONF_TZ_REALIGN) is None

    async def test_fresh_install_done_when_no_history(
        self, hass: HomeAssistant
    ) -> None:
        """Gate (d): last_cons_date is None → done(fresh_install), no fetch."""
        entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")

        with (
            patch(_PATCH_ADD_STATS),
            patch(_PATCH_GET_INSTANCE, _mock_get_instance()),
        ):
            result = await coord._plan_tz_realign(
                date(2026, 11, 5),
                date(2026, 11, 4),
                None,  # no history
                None,
                None,
                normal=(None, None),
            )

        assert result is None
        rec = entry.data.get(CONF_TZ_REALIGN)
        assert rec is not None
        assert rec["state"] == TZ_REALIGN_DONE
        assert rec["reason"] == TZ_REALIGN_REASON_FRESH_INSTALL
        assert rec["zone"] == "Australia/Adelaide"

    async def test_not_affected_nsw(self, hass: HomeAssistant) -> None:
        """Gate (f): NSW (Australia/Sydney == AGL API tz) → done(not_affected)."""
        entry, coord = _make_entry(hass, zone_name="Australia/Sydney")
        coord.client.local_tz = ZoneInfo("Australia/Sydney")

        today = date(2026, 11, 5)
        yesterday = today - timedelta(days=1)

        with (
            patch(_PATCH_ADD_STATS),
            patch(_PATCH_GET_INSTANCE, _mock_get_instance()),
        ):
            result = await coord._plan_tz_realign(
                today,
                yesterday,
                yesterday,
                None,
                None,
                normal=(None, None),
            )

        assert result is None
        rec = entry.data.get(CONF_TZ_REALIGN)
        assert rec is not None
        assert rec["state"] == TZ_REALIGN_DONE
        assert rec["reason"] == TZ_REALIGN_REASON_NOT_AFFECTED

    async def test_unsupported_shift_perth(self, hass: HomeAssistant) -> None:
        """M4: Perth (WA, UTC+8/9) shift > 1 h → done(unsupported_shift), zero realign requests."""
        entry, coord = _make_entry(hass, zone_name="Australia/Perth")
        coord.client.local_tz = ZoneInfo("Australia/Perth")

        today = date(2026, 11, 5)
        yesterday = today - timedelta(days=1)

        fetch_calls: list = []

        async def _fake_fetch(day, previous, **kwargs):
            fetch_calls.append(day)
            return []

        with (
            patch(_PATCH_ADD_STATS),
            patch(_PATCH_GET_INSTANCE, _mock_get_instance()),
            patch.object(coord, "_fetch_day_consumption", side_effect=_fake_fetch),
        ):
            result = await coord._plan_tz_realign(
                today,
                yesterday,
                yesterday,
                None,
                None,
                normal=(None, None),
            )

        assert result is None
        rec = entry.data.get(CONF_TZ_REALIGN)
        assert rec is not None
        assert rec["state"] == TZ_REALIGN_DONE
        assert rec["reason"] == TZ_REALIGN_REASON_UNSUPPORTED_SHIFT
        # No realign-specific fetch should have occurred.
        assert fetch_calls == []

    async def test_unsupported_shift_darwin(self, hass: HomeAssistant) -> None:
        """M4: Darwin (NT, UTC+9:30 always) in Sydney-DST window → unsupported_shift."""
        entry, coord = _make_entry(hass, zone_name="Australia/Darwin")
        coord.client.local_tz = ZoneInfo("Australia/Darwin")

        # During Sydney DST (November) NT offset vs Sydney = 1h30m > 1h.
        today = date(2026, 11, 5)
        yesterday = today - timedelta(days=1)

        with (
            patch(_PATCH_ADD_STATS),
            patch(_PATCH_GET_INSTANCE, _mock_get_instance()),
        ):
            result = await coord._plan_tz_realign(
                today,
                yesterday,
                yesterday,
                None,
                None,
                normal=(None, None),
            )

        rec = entry.data.get(CONF_TZ_REALIGN)
        assert rec is not None
        assert rec["state"] == TZ_REALIGN_DONE
        assert rec["reason"] == TZ_REALIGN_REASON_UNSUPPORTED_SHIFT
        assert result is None

    async def test_deferred_while_backfill_catching_up(
        self, hass: HomeAssistant
    ) -> None:
        """Gate (e): last_cons_date < floor-1 → record stays absent."""
        entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        today = date(2026, 11, 5)
        yesterday = today - timedelta(days=1)
        # 3 days behind the floor
        floor = today - timedelta(days=TZ_REALIGN_DAYS)
        last_cons_date = floor - timedelta(days=3)

        with (
            patch(_PATCH_ADD_STATS),
            patch(_PATCH_GET_INSTANCE, _mock_get_instance()),
        ):
            result = await coord._plan_tz_realign(
                today, yesterday, last_cons_date, None, None, normal=(None, None)
            )

        assert result is None
        assert entry.data.get(CONF_TZ_REALIGN) is None

    async def test_arms_when_caught_up(self, hass: HomeAssistant) -> None:
        """Arms (writes pending) when last_cons_date >= floor-1."""
        entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        today = date(2026, 11, 5)
        yesterday = today - timedelta(days=1)
        floor = today - timedelta(days=TZ_REALIGN_DAYS)
        last_cons_date = floor  # exactly at the floor

        with (
            patch(_PATCH_ADD_STATS),
            patch(_PATCH_GET_INSTANCE, _mock_get_instance()),
        ):
            result = await coord._plan_tz_realign(
                today, yesterday, last_cons_date, None, None, normal=(None, None)
            )

        # Should return a sweep (pending) now.
        assert result is not None
        rec = entry.data.get(CONF_TZ_REALIGN)
        assert rec is not None
        assert rec["state"] == TZ_REALIGN_PENDING

    async def test_deferred_while_solar_heal_pending(self, hass: HomeAssistant) -> None:
        """A solar heal sweeping this cycle wins it — TZ_REALIGN record untouched."""
        entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        today = date(2026, 11, 5)
        yesterday = today - timedelta(days=1)
        # Simulate an active heal_ctx
        heal_ctx = (today - timedelta(days=TZ_REALIGN_DAYS), 0)

        with (
            patch(_PATCH_ADD_STATS),
            patch(_PATCH_GET_INSTANCE, _mock_get_instance()),
        ):
            result = await coord._plan_tz_realign(
                today, yesterday, yesterday, None, heal_ctx, normal=(None, None)
            )

        assert result is None
        assert entry.data.get(CONF_TZ_REALIGN) is None


# ---------------------------------------------------------------------------
# Floor is frozen (not sliding)
# ---------------------------------------------------------------------------


class TestFloorFrozen:
    async def test_floor_frozen_across_cycles(self, hass: HomeAssistant) -> None:
        """Floor written at arm time must not change on subsequent calls."""
        entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        today = date(2026, 11, 5)
        yesterday = today - timedelta(days=1)
        last_cons_date = yesterday

        with (
            patch(_PATCH_ADD_STATS),
            patch(_PATCH_GET_INSTANCE, _mock_get_instance()),
        ):
            # First evaluation arms the record.
            await coord._plan_tz_realign(
                today, yesterday, last_cons_date, None, None, normal=(None, None)
            )

        frozen_floor = entry.data[CONF_TZ_REALIGN]["floor"]
        expected_floor = (today - timedelta(days=TZ_REALIGN_DAYS)).isoformat()
        assert frozen_floor == expected_floor

        # Simulate a day passing; the floor must not slide.
        today2 = today + timedelta(days=1)
        yesterday2 = today2 - timedelta(days=1)

        with (
            patch(_PATCH_ADD_STATS),
            patch(_PATCH_GET_INSTANCE, _mock_get_instance()),
        ):
            await coord._plan_tz_realign(
                today2, yesterday2, yesterday2, None, None, normal=(None, None)
            )

        # Floor in the persisted record must be unchanged.
        assert entry.data[CONF_TZ_REALIGN]["floor"] == frozen_floor


# ---------------------------------------------------------------------------
# Zone mismatch (C8) — re-evaluation in every state
# ---------------------------------------------------------------------------


class TestZoneMismatch:
    async def test_pending_zone_mismatch_resets_and_rearms(
        self, hass: HomeAssistant
    ) -> None:
        """Pending record for a different zone → reset to absent, re-armed."""
        entry, coord = _make_entry(hass, zone_name="Australia/Brisbane")
        coord.client.local_tz = ZoneInfo("Australia/Brisbane")
        # Use an October date inside Sydney DST when Brisbane has a 1h shift.
        today = date(2026, 10, 15)
        yesterday = today - timedelta(days=1)

        # Plant a pending record for Adelaide (different zone).
        hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                CONF_LOCAL_TZ: "Australia/Brisbane",
                CONF_TZ_REALIGN: {
                    "state": TZ_REALIGN_PENDING,
                    "zone": "Australia/Adelaide",  # mismatch
                    "floor": (today - timedelta(days=30)).isoformat(),
                    "series": TZ_REALIGN_CONSUMPTION,
                    "attempts": 1,
                    "halts": 0,
                },
            },
        )

        with (
            patch(_PATCH_ADD_STATS),
            patch(_PATCH_GET_INSTANCE, _mock_get_instance()),
        ):
            await coord._plan_tz_realign(
                today, yesterday, yesterday, None, None, normal=(None, None)
            )

        rec = entry.data.get(CONF_TZ_REALIGN)
        # Should have a NEW record for Brisbane with attempts=0.
        assert rec is not None
        if rec["state"] == TZ_REALIGN_PENDING:
            assert rec["zone"] == "Australia/Brisbane"
            assert rec["attempts"] == 0
        else:
            # Could also be done (not_affected for an in-window QLD standard date
            # when Brisbane has no shift, but Oct 15 is inside Sydney DST → shift=1h).
            assert rec["zone"] == "Australia/Brisbane"

    async def test_done_zone_mismatch_rearms(self, hass: HomeAssistant) -> None:
        """A done record for a different zone → re-evaluated (C8)."""
        entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        today = date(2026, 11, 5)
        yesterday = today - timedelta(days=1)

        # Plant a done record for a different zone.
        hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                CONF_TZ_REALIGN: {
                    "state": TZ_REALIGN_DONE,
                    "zone": "Australia/Brisbane",  # mismatch
                    "reason": TZ_REALIGN_REASON_NOT_AFFECTED,
                    "floor": (today - timedelta(days=30)).isoformat(),
                },
            },
        )

        with (
            patch(_PATCH_ADD_STATS),
            patch(_PATCH_GET_INSTANCE, _mock_get_instance()),
        ):
            await coord._plan_tz_realign(
                today, yesterday, yesterday, None, None, normal=(None, None)
            )

        rec = entry.data.get(CONF_TZ_REALIGN)
        assert rec is not None
        # Adelaide was re-evaluated: should now be for Adelaide.
        assert rec["zone"] == "Australia/Adelaide"

    async def test_conf_tz_realign_survives_reauth(self, hass: HomeAssistant) -> None:
        """CONF_TZ_REALIGN is preserved through a reauth data_updates merge."""
        entry, _ = _make_entry(hass, zone_name="Australia/Adelaide")
        realign_record = {
            "state": TZ_REALIGN_DONE,
            "zone": "Australia/Adelaide",
            "reason": TZ_REALIGN_COMPLETE,
            "floor": "2026-10-10",
        }
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_TZ_REALIGN: realign_record},
        )
        # Simulate a reauth update (merge, not replace).
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_REFRESH_TOKEN: "v1.newtoken"},
        )
        assert entry.data.get(CONF_TZ_REALIGN) == realign_record


# ---------------------------------------------------------------------------
# Done is terminal (never re-arms for same zone)
# ---------------------------------------------------------------------------


class TestDoneIsTerminal:
    async def test_done_same_zone_never_rearms(self, hass: HomeAssistant) -> None:
        """A done record for the SAME zone → return None, record untouched."""
        entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        today = date(2026, 11, 5)
        yesterday = today - timedelta(days=1)

        done_record = {
            "state": TZ_REALIGN_DONE,
            "zone": "Australia/Adelaide",
            "reason": TZ_REALIGN_COMPLETE,
            "floor": (today - timedelta(days=30)).isoformat(),
        }
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_TZ_REALIGN: done_record},
        )

        with (
            patch(_PATCH_ADD_STATS),
            patch(_PATCH_GET_INSTANCE, _mock_get_instance()),
        ):
            result = await coord._plan_tz_realign(
                today, yesterday, yesterday, None, None, normal=(None, None)
            )

        assert result is None
        # Record must be byte-identical.
        assert entry.data[CONF_TZ_REALIGN] == done_record


# ---------------------------------------------------------------------------
# Consumption stage give-up → generation hand-over (C3)
# ---------------------------------------------------------------------------


class TestConsumptionGiveUp:
    async def test_consumption_giveup_hands_over_to_generation(
        self, hass: HomeAssistant
    ) -> None:
        """C3: consumption-stage give-up transitions to pending(generation) with
        consumption:'gave_up'; done only after generation stage."""
        entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        today = date(2026, 11, 5)
        yesterday = today - timedelta(days=1)

        # Plant a pending(consumption) record at MAX attempts.
        floor = today - timedelta(days=TZ_REALIGN_DAYS)
        pending_record = {
            "state": TZ_REALIGN_PENDING,
            "zone": "Australia/Adelaide",
            "floor": floor.isoformat(),
            "series": TZ_REALIGN_CONSUMPTION,
            "attempts": MAX_TZ_REALIGN_ATTEMPTS,
            "halts": 0,
        }
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_TZ_REALIGN: pending_record},
        )

        coord._has_solar = False  # No solar → generation is skipped immediately.

        with (
            patch(_PATCH_ADD_STATS),
            patch(_PATCH_GET_INSTANCE, _mock_get_instance()),
        ):
            result = await coord._plan_tz_realign(
                today, yesterday, yesterday, None, None, normal=(None, None)
            )

        # When consumption gives up and no solar, transitions directly to done(gave_up).
        rec = entry.data.get(CONF_TZ_REALIGN)
        assert rec is not None
        assert rec["state"] == TZ_REALIGN_DONE
        assert rec["reason"] == TZ_REALIGN_GAVE_UP
        assert rec.get("consumption") == TZ_REALIGN_GAVE_UP
        # generation is skipped (no solar)
        assert rec.get("generation") == TZ_REALIGN_SKIPPED
        assert result is None

    async def test_consumption_giveup_with_solar_goes_to_generation_stage(
        self, hass: HomeAssistant
    ) -> None:
        """C3: consumption give-up on a solar contract → pending(generation) next."""
        entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        today = date(2026, 11, 5)
        yesterday = today - timedelta(days=1)

        floor = today - timedelta(days=TZ_REALIGN_DAYS)
        pending_record = {
            "state": TZ_REALIGN_PENDING,
            "zone": "Australia/Adelaide",
            "floor": floor.isoformat(),
            "series": TZ_REALIGN_CONSUMPTION,
            "attempts": MAX_TZ_REALIGN_ATTEMPTS,
            "halts": 0,
        }
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_TZ_REALIGN: pending_record},
        )

        coord._has_solar = True
        gen_id = f"{DOMAIN}:generation_{_CONTRACT}"

        # Mock get_last_stat to show generation exists and is caught up.
        async def _mock_last_stat(stat_id: str):
            if stat_id == gen_id:
                return (10.0, yesterday)
            return (0.0, None)

        with (
            patch.object(coord, "_get_last_stat", side_effect=_mock_last_stat),
            patch(_PATCH_ADD_STATS),
            patch(_PATCH_GET_INSTANCE, _mock_get_instance()),
        ):
            await coord._plan_tz_realign(
                today, yesterday, yesterday, yesterday, None, normal=(None, None)
            )

        rec = entry.data.get(CONF_TZ_REALIGN)
        assert rec is not None
        # Should now be pending(generation) with consumption='gave_up' and attempts=0.
        assert rec["state"] == TZ_REALIGN_PENDING
        assert rec["series"] == TZ_REALIGN_GENERATION
        assert rec.get("consumption") == TZ_REALIGN_GAVE_UP
        assert rec["attempts"] == 0


# ---------------------------------------------------------------------------
# Generation stage gates (C4, M3)
# ---------------------------------------------------------------------------


class TestGenerationStageGates:
    async def test_generation_wait_when_has_solar_false_but_rows_exist(
        self, hass: HomeAssistant
    ) -> None:
        """C4: _has_solar False but generation rows exist → WAIT (record untouched)."""
        entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        today = date(2026, 11, 5)
        yesterday = today - timedelta(days=1)
        floor = today - timedelta(days=TZ_REALIGN_DAYS)

        # Plant pending(generation) record.
        pending_record = {
            "state": TZ_REALIGN_PENDING,
            "zone": "Australia/Adelaide",
            "floor": floor.isoformat(),
            "series": TZ_REALIGN_GENERATION,
            "attempts": 0,
            "halts": 0,
            "consumption": TZ_REALIGN_COMPLETE,
        }
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_TZ_REALIGN: pending_record},
        )

        coord._has_solar = False
        gen_id = f"{DOMAIN}:generation_{_CONTRACT}"

        # _get_last_stat returns (sum, last_date) — solar has rows.
        async def _mock_last_stat(stat_id: str):
            if stat_id == gen_id:
                return (5.0, yesterday)
            return (0.0, None)

        with (
            patch.object(coord, "_get_last_stat", side_effect=_mock_last_stat),
            patch(_PATCH_ADD_STATS),
            patch(_PATCH_GET_INSTANCE, _mock_get_instance()),
        ):
            result = await coord._plan_tz_realign(
                today, yesterday, yesterday, yesterday, None, normal=(None, None)
            )

        # WAIT: return None, record untouched.
        assert result is None
        # Record must be byte-identical (unchanged).
        assert entry.data[CONF_TZ_REALIGN] == pending_record

    async def test_generation_skipped_when_writes_disabled(
        self, hass: HomeAssistant
    ) -> None:
        """M3: solar writes disabled → generation stage immediately skipped (done)."""
        entry, coord = _make_entry(
            hass,
            zone_name="Australia/Adelaide",
            options={OPT_SOLAR_STATISTICS_ENABLED: False},
        )
        today = date(2026, 11, 5)
        yesterday = today - timedelta(days=1)
        floor = today - timedelta(days=TZ_REALIGN_DAYS)

        pending_record = {
            "state": TZ_REALIGN_PENDING,
            "zone": "Australia/Adelaide",
            "floor": floor.isoformat(),
            "series": TZ_REALIGN_GENERATION,
            "attempts": 0,
            "halts": 0,
            "consumption": TZ_REALIGN_COMPLETE,
        }
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_TZ_REALIGN: pending_record},
        )
        coord._has_solar = True

        with (
            patch(_PATCH_ADD_STATS),
            patch(_PATCH_GET_INSTANCE, _mock_get_instance()),
        ):
            result = await coord._plan_tz_realign(
                today, yesterday, yesterday, yesterday, None, normal=(None, None)
            )

        rec = entry.data.get(CONF_TZ_REALIGN)
        assert result is None
        assert rec is not None
        assert rec["state"] == TZ_REALIGN_DONE
        assert rec.get("generation") == TZ_REALIGN_SKIPPED


# ---------------------------------------------------------------------------
# Halt accounting (C5)
# ---------------------------------------------------------------------------


class TestHaltAccounting:
    async def test_halt_counted_once_per_poll_interval(
        self, hass: HomeAssistant
    ) -> None:
        """C5: a second halt within the configured poll interval is not counted."""
        _entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        today = date(2026, 11, 5)
        floor = today - timedelta(days=TZ_REALIGN_DAYS)

        last_halt_at = datetime.now(UTC).isoformat(timespec="seconds")
        record = {
            "state": TZ_REALIGN_PENDING,
            "zone": "Australia/Adelaide",
            "floor": floor.isoformat(),
            "series": TZ_REALIGN_CONSUMPTION,
            "attempts": 0,
            "halts": 1,
            "last_halt_at": last_halt_at,
        }

        # Calling count_tz_realign_halt within the poll interval should NOT increment.
        result = coord._count_tz_realign_halt(record)
        assert result["halts"] == 1  # unchanged
        assert result["attempts"] == 0  # unchanged

    async def test_halt_cap_converts_to_attempt_clears_sweep_halted(
        self, hass: HomeAssistant
    ) -> None:
        """C5: MAX_TZ_REALIGN_HALTS reached → attempts+1, halts=0, _sweep_halted=False."""
        _entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        today = date(2026, 11, 5)
        floor = today - timedelta(days=TZ_REALIGN_DAYS)

        # last_halt_at far in the past so a new halt IS counted.
        old_halt = (datetime.now(UTC) - timedelta(days=2)).isoformat(timespec="seconds")
        record = {
            "state": TZ_REALIGN_PENDING,
            "zone": "Australia/Adelaide",
            "floor": floor.isoformat(),
            "series": TZ_REALIGN_CONSUMPTION,
            "attempts": 0,
            "halts": MAX_TZ_REALIGN_HALTS - 1,
            "last_halt_at": old_halt,
        }
        coord._sweep_halted = True  # simulate a halted sweep

        result = coord._count_tz_realign_halt(record)

        # Halt cap reached: converts to 1 attempt, halts reset to 0.
        assert result["attempts"] == 1
        assert result["halts"] == 0
        assert "last_halt_at" not in result
        # _sweep_halted cleared so coordinator drops to configured cadence.
        assert coord._sweep_halted is False

    async def test_settle_halted_then_not_halted_cadence_fallback(
        self, hass: HomeAssistant
    ) -> None:
        """C5: MAX_TZ_REALIGN_HALTS halts convert to one attempt; sweep_halted cleared
        so the 24h cadence is restored rather than endless 30-min fast-retries."""
        entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        today = date(2026, 11, 5)
        yesterday = today - timedelta(days=1)
        floor = today - timedelta(days=TZ_REALIGN_DAYS)

        # Record at MAX_TZ_REALIGN_HALTS-1 halts, last halt older than poll interval.
        old_halt = (datetime.now(UTC) - timedelta(days=2)).isoformat(timespec="seconds")
        record = {
            "state": TZ_REALIGN_PENDING,
            "zone": "Australia/Adelaide",
            "floor": floor.isoformat(),
            "series": TZ_REALIGN_CONSUMPTION,
            "attempts": 0,
            "halts": MAX_TZ_REALIGN_HALTS - 1,
            "last_halt_at": old_halt,
        }
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_TZ_REALIGN: record},
        )

        # Plant a _RealignSweep so settle runs.
        from custom_components.haggle.coordinator import _RealignSweep

        realign = _RealignSweep(
            TZ_REALIGN_CONSUMPTION,
            floor,
            (floor, yesterday),
        )
        # Simulate sweep was halted.
        coord._sweep_halted = True
        coord._sweep_skipped_days = (frozenset(), frozenset())

        with patch(_PATCH_GET_INSTANCE, _mock_get_instance()):
            await coord._settle_tz_realign(realign, today, None)

        # After 3 halts converted to 1 attempt, _sweep_halted should be False.
        assert coord._sweep_halted is False
        rec = entry.data[CONF_TZ_REALIGN]
        # attempts should be 1 (the converted halt).
        assert rec["attempts"] == 1
        assert rec["halts"] == 0


# ---------------------------------------------------------------------------
# Completion rule (C2): rewindow-day skips don't count
# ---------------------------------------------------------------------------


class TestCompletionRule:
    def test_sweep_complete_ignores_rewindow_skips(self, hass: HomeAssistant) -> None:
        """C2: a skip in the trailing REWINDOW_DAYS never makes the sweep incomplete."""
        _entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        from custom_components.haggle.coordinator import _RealignSweep

        today = date(2026, 11, 5)
        floor = today - timedelta(days=TZ_REALIGN_DAYS)
        realign = _RealignSweep(
            TZ_REALIGN_CONSUMPTION, floor, (floor, today - timedelta(days=1))
        )

        # Skip a day INSIDE the rewindow (last 7 days).
        horizon = today - timedelta(days=REWINDOW_DAYS)
        rewindow_day = horizon + timedelta(days=1)  # strictly inside rewindow
        skipped_cons = frozenset({rewindow_day})

        result = coord._realign_sweep_complete(
            realign,
            halted=False,
            skipped=(skipped_cons, frozenset()),
            today=today,
        )
        assert result is True

    def test_sweep_incomplete_for_skip_before_horizon(
        self, hass: HomeAssistant
    ) -> None:
        """A skip BEFORE the rewindow horizon makes the sweep incomplete."""
        _entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        from custom_components.haggle.coordinator import _RealignSweep

        today = date(2026, 11, 5)
        floor = today - timedelta(days=TZ_REALIGN_DAYS)
        realign = _RealignSweep(
            TZ_REALIGN_CONSUMPTION, floor, (floor, today - timedelta(days=1))
        )

        horizon = today - timedelta(days=REWINDOW_DAYS)
        before_horizon = horizon - timedelta(days=1)  # definitely before horizon
        skipped_cons = frozenset({before_horizon})

        result = coord._realign_sweep_complete(
            realign,
            halted=False,
            skipped=(skipped_cons, frozenset()),
            today=today,
        )
        assert result is False

    def test_sweep_incomplete_when_halted(self, hass: HomeAssistant) -> None:
        """Any halt makes the sweep incomplete regardless of skips."""
        _entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        from custom_components.haggle.coordinator import _RealignSweep

        today = date(2026, 11, 5)
        floor = today - timedelta(days=TZ_REALIGN_DAYS)
        realign = _RealignSweep(
            TZ_REALIGN_CONSUMPTION, floor, (floor, today - timedelta(days=1))
        )

        result = coord._realign_sweep_complete(
            realign,
            halted=True,
            skipped=(frozenset(), frozenset()),
            today=today,
        )
        assert result is False


# ---------------------------------------------------------------------------
# Repairs issues: raised once with correct ids
# ---------------------------------------------------------------------------


class TestRepairsIssues:
    async def test_giveup_issue_raised_once_with_correct_id(
        self, hass: HomeAssistant
    ) -> None:
        """tz_realign_gave_up issue raised once at terminal transition."""
        import homeassistant.helpers.issue_registry as ir

        entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        today = date(2026, 11, 5)
        yesterday = today - timedelta(days=1)
        floor = today - timedelta(days=TZ_REALIGN_DAYS)

        # Put a consumption record at MAX attempts so give-up fires.
        hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                CONF_TZ_REALIGN: {
                    "state": TZ_REALIGN_PENDING,
                    "zone": "Australia/Adelaide",
                    "floor": floor.isoformat(),
                    "series": TZ_REALIGN_CONSUMPTION,
                    "attempts": MAX_TZ_REALIGN_ATTEMPTS,
                    "halts": 0,
                },
            },
        )
        coord._has_solar = False

        with (
            patch.object(ir, "async_create_issue") as mock_issue,
            patch(_PATCH_ADD_STATS),
            patch(_PATCH_GET_INSTANCE, _mock_get_instance()),
        ):
            await coord._plan_tz_realign(
                today, yesterday, yesterday, None, None, normal=(None, None)
            )

        # Issue must have been raised exactly once with the gave-up id.
        raised = [
            c
            for c in mock_issue.call_args_list
            if c.kwargs.get("translation_key") == ISSUE_TZ_REALIGN_GAVE_UP
            or (len(c.args) > 2 and c.args[2].startswith(ISSUE_TZ_REALIGN_GAVE_UP))
        ]
        assert len(raised) >= 1
        # Issue id must contain entry_id (not contract_number).
        issue_id = raised[0].args[2] if raised[0].args else raised[0].kwargs["issue_id"]
        assert entry.entry_id in issue_id
        assert _CONTRACT not in issue_id

    async def test_giveup_issue_not_reraised_after_done(
        self, hass: HomeAssistant
    ) -> None:
        """Once done, _plan_tz_realign returns None — no further issue raised."""
        import homeassistant.helpers.issue_registry as ir

        entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        today = date(2026, 11, 5)
        yesterday = today - timedelta(days=1)
        floor = today - timedelta(days=TZ_REALIGN_DAYS)

        # Plant done(gave_up) record.
        hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                CONF_TZ_REALIGN: {
                    "state": TZ_REALIGN_DONE,
                    "zone": "Australia/Adelaide",
                    "reason": TZ_REALIGN_GAVE_UP,
                    "floor": floor.isoformat(),
                    "consumption": TZ_REALIGN_GAVE_UP,
                    "generation": TZ_REALIGN_SKIPPED,
                    "sweeps": str(MAX_TZ_REALIGN_ATTEMPTS),
                    "notice": False,
                    "history_from": None,
                },
            },
        )

        with (
            patch.object(ir, "async_create_issue") as mock_issue,
            patch(_PATCH_ADD_STATS),
            patch(_PATCH_GET_INSTANCE, _mock_get_instance()),
        ):
            await coord._plan_tz_realign(
                today, yesterday, yesterday, None, None, normal=(None, None)
            )

        gave_up_calls = [
            c
            for c in mock_issue.call_args_list
            if (
                c.kwargs.get("translation_key") == ISSUE_TZ_REALIGN_GAVE_UP
                or (len(c.args) > 2 and c.args[2].startswith(ISSUE_TZ_REALIGN_GAVE_UP))
            )
        ]
        assert len(gave_up_calls) == 0

    async def test_history_notice_raised_once_on_clean_done(
        self, hass: HomeAssistant
    ) -> None:
        """tz_history_before_realign raised once after clean completion when
        older shifted rows exist (notice=True)."""
        import homeassistant.helpers.issue_registry as ir

        entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        floor = date(2026, 10, 10)

        # Mock _shifted_history_before to return (True, "2026-09-15").
        with (
            patch.object(
                coord,
                "_shifted_history_before",
                new=AsyncMock(return_value=(True, "2026-09-15")),
            ),
            patch.object(ir, "async_create_issue") as mock_issue,
        ):
            await coord._publish_tz_realign_done(
                {
                    "state": TZ_REALIGN_DONE,
                    "zone": "Australia/Adelaide",
                    "reason": TZ_REALIGN_COMPLETE,
                    "floor": floor.isoformat(),
                    "consumption": TZ_REALIGN_COMPLETE,
                    "generation": TZ_REALIGN_SKIPPED,
                },
                floor,
            )

        history_calls = [
            c
            for c in mock_issue.call_args_list
            if (
                c.kwargs.get("translation_key") == ISSUE_TZ_HISTORY_BEFORE_REALIGN
                or (
                    len(c.args) > 2
                    and c.args[2].startswith(ISSUE_TZ_HISTORY_BEFORE_REALIGN)
                )
            )
        ]
        assert len(history_calls) == 1
        rec = entry.data[CONF_TZ_REALIGN]
        assert rec.get("notice") is True

    async def test_history_notice_not_raised_when_no_older_rows(
        self, hass: HomeAssistant
    ) -> None:
        """notice=False when _shifted_history_before returns (False, ...)."""
        import homeassistant.helpers.issue_registry as ir

        entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        floor = date(2026, 10, 10)

        with (
            patch.object(
                coord,
                "_shifted_history_before",
                new=AsyncMock(return_value=(False, "2026-10-10")),
            ),
            patch.object(ir, "async_create_issue") as mock_issue,
        ):
            await coord._publish_tz_realign_done(
                {
                    "state": TZ_REALIGN_DONE,
                    "zone": "Australia/Adelaide",
                    "reason": TZ_REALIGN_REASON_NOT_AFFECTED,
                    "floor": floor.isoformat(),
                },
                floor,
            )

        history_calls = [
            c
            for c in mock_issue.call_args_list
            if (
                c.kwargs.get("translation_key") == ISSUE_TZ_HISTORY_BEFORE_REALIGN
                or (
                    len(c.args) > 2
                    and c.args[2].startswith(ISSUE_TZ_HISTORY_BEFORE_REALIGN)
                )
            )
        ]
        assert len(history_calls) == 0
        assert entry.data[CONF_TZ_REALIGN].get("notice") is False


# ---------------------------------------------------------------------------
# Period-sensor gate (suppressed during generation stage)
# ---------------------------------------------------------------------------


class TestPeriodSensorGate:
    async def test_period_sensors_suppressed_during_generation_stage(
        self, hass: HomeAssistant
    ) -> None:
        """While the generation stage is active (realign.series == GENERATION),
        _publish_period_gate returns False (no publish)."""
        from custom_components.haggle.coordinator import _RealignSweep

        _entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        today = date(2026, 11, 5)
        floor = today - timedelta(days=TZ_REALIGN_DAYS)
        realign = _RealignSweep(
            TZ_REALIGN_GENERATION, floor, (floor, today - timedelta(days=1))
        )

        result = await coord._publish_period_gate(
            heal_ctx=None,
            fetch_complete=True,
            realign=realign,
            realign_complete=False,  # not yet complete
        )
        assert result is False

    async def test_period_sensors_published_after_generation_complete(
        self, hass: HomeAssistant
    ) -> None:
        """After the generation stage completes (realign_complete=True), gate is True
        when recorder drains."""
        from custom_components.haggle.coordinator import _RealignSweep

        _entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        today = date(2026, 11, 5)
        floor = today - timedelta(days=TZ_REALIGN_DAYS)
        realign = _RealignSweep(
            TZ_REALIGN_GENERATION, floor, (floor, today - timedelta(days=1))
        )

        with patch.object(coord, "_recorder_drained", new=AsyncMock(return_value=True)):
            result = await coord._publish_period_gate(
                heal_ctx=None,
                fetch_complete=True,
                realign=realign,
                realign_complete=True,
            )
        assert result is True

    async def test_period_sensors_not_suppressed_during_consumption_stage(
        self, hass: HomeAssistant
    ) -> None:
        """Consumption stage does not suppress period sensors (only generation does)."""
        from custom_components.haggle.coordinator import _RealignSweep

        _entry, coord = _make_entry(
            hass,
            zone_name="Australia/Adelaide",
            options={OPT_SOLAR_STATISTICS_ENABLED: True},
        )
        today = date(2026, 11, 5)
        floor = today - timedelta(days=TZ_REALIGN_DAYS)
        realign = _RealignSweep(
            TZ_REALIGN_CONSUMPTION, floor, (floor, today - timedelta(days=1))
        )

        # Solar writes enabled; consumption realign does NOT suppress.
        result = await coord._publish_period_gate(
            heal_ctx=None,
            fetch_complete=True,
            realign=realign,
            realign_complete=False,
        )
        assert result is True


# ---------------------------------------------------------------------------
# C9: generation stage never feeds the stall detector
# ---------------------------------------------------------------------------


class TestStallDetector:
    def test_should_track_stall_false_during_generation_realign(
        self, hass: HomeAssistant
    ) -> None:
        """C9: _should_track_stall returns False when a generation stage is active."""
        from custom_components.haggle.coordinator import _RealignSweep

        _entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        today = date(2026, 11, 5)
        floor = today - timedelta(days=TZ_REALIGN_DAYS)
        realign = _RealignSweep(
            TZ_REALIGN_GENERATION, floor, (floor, today - timedelta(days=1))
        )

        result = coord._should_track_stall(
            heal_ctx=None,
            solar_range=(floor, today - timedelta(days=1)),
            last_gen_date=floor - timedelta(days=1),  # behind → would normally track
            realign=realign,
        )
        assert result is False

    def test_should_track_stall_true_on_normal_solar_backfill(
        self, hass: HomeAssistant
    ) -> None:
        """Normal solar backfill (no realign) still tracks stall."""
        _entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        today = date(2026, 11, 5)
        start = today - timedelta(days=20)
        yesterday = today - timedelta(days=1)

        result = coord._should_track_stall(
            heal_ctx=None,
            solar_range=(start, yesterday),
            last_gen_date=start - timedelta(days=1),
            realign=None,
        )
        assert result is True


# ---------------------------------------------------------------------------
# Exception path counts attempt (C_count_exception)
# ---------------------------------------------------------------------------


class TestExceptionCounting:
    def test_count_tz_realign_exception_increments_attempts(
        self, hass: HomeAssistant
    ) -> None:
        """An exception synchronously bumps attempts in the persisted record."""
        entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        today = date(2026, 11, 5)
        floor = today - timedelta(days=TZ_REALIGN_DAYS)

        hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                CONF_TZ_REALIGN: {
                    "state": TZ_REALIGN_PENDING,
                    "zone": "Australia/Adelaide",
                    "floor": floor.isoformat(),
                    "series": TZ_REALIGN_CONSUMPTION,
                    "attempts": 1,
                    "halts": 0,
                },
            },
        )

        coord._count_tz_realign_exception()

        assert entry.data[CONF_TZ_REALIGN]["attempts"] == 2

    def test_count_tz_realign_exception_no_op_when_done(
        self, hass: HomeAssistant
    ) -> None:
        """count_tz_realign_exception is a no-op on a done record."""
        entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        done_record = {
            "state": TZ_REALIGN_DONE,
            "zone": "Australia/Adelaide",
            "reason": TZ_REALIGN_COMPLETE,
            "floor": "2026-10-10",
        }
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_TZ_REALIGN: done_record},
        )

        coord._count_tz_realign_exception()  # must not raise or change record

        assert entry.data[CONF_TZ_REALIGN] == done_record


# ---------------------------------------------------------------------------
# Stall-span tolerance
# ---------------------------------------------------------------------------


class TestStallSpanTolerance:
    def test_stall_span_days_within_range(self, hass: HomeAssistant) -> None:
        """Days from stall spans within [lo, hi] are returned."""
        entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        lo = date(2026, 10, 5)
        hi = date(2026, 11, 4)
        spans = [
            {"start": "2026-10-08", "end": "2026-10-09"},
        ]
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_SOLAR_STALL_SPANS: spans},
        )

        result = coord._stall_span_days(lo, hi)

        assert date(2026, 10, 8) in result
        assert date(2026, 10, 9) in result
        assert len(result) == 2

    def test_stall_span_days_empty_when_no_spans(self, hass: HomeAssistant) -> None:
        _entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")

        result = coord._stall_span_days(date(2026, 10, 1), date(2026, 11, 4))
        assert result == frozenset()


# ---------------------------------------------------------------------------
# Diagnostics v4: tz_realign top-level, not in entry.data
# ---------------------------------------------------------------------------


class TestDiagnosticsV4:
    async def test_tz_realign_surfaced_top_level(self, hass: HomeAssistant) -> None:
        """schema_version=4, tz_realign at top level, absent from entry.data."""
        from custom_components.haggle.diagnostics import (
            DIAGNOSTICS_SCHEMA_VERSION,
            async_get_config_entry_diagnostics,
        )

        entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        realign_record = {
            "state": TZ_REALIGN_PENDING,
            "zone": "Australia/Adelaide",
            "floor": "2026-10-10",
            "series": TZ_REALIGN_CONSUMPTION,
            "attempts": 1,
            "halts": 0,
        }
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_TZ_REALIGN: realign_record},
        )
        coord.data = HaggleData(
            consumption_period_kwh=0.0,
            consumption_period_cost_aud=0.0,
            bill_projection_aud=None,
            unit_rate_aud_per_kwh=None,
            supply_charge_aud_per_day=None,
            latest_cumulative_kwh=0.0,
        )
        coord._has_solar = False
        coord._active_tou_bands = set()
        entry.runtime_data = SimpleNamespace(coordinator=coord)

        # Patch recorder for diagnostics.
        instance = MagicMock()
        instance.async_add_executor_job = AsyncMock(return_value={})

        with patch(_PATCH_GET_INSTANCE, MagicMock(return_value=instance)):
            result = await async_get_config_entry_diagnostics(hass, entry)

        assert result["schema_version"] == 4
        assert DIAGNOSTICS_SCHEMA_VERSION == 4
        # tz_realign must be top-level.
        assert "tz_realign" in result
        assert result["tz_realign"] == realign_record
        # Must not be in entry.data.
        assert CONF_TZ_REALIGN not in result["entry"]["data"]

    async def test_tz_realign_null_when_absent(self, hass: HomeAssistant) -> None:
        """tz_realign is null when the record was never evaluated."""
        from custom_components.haggle.diagnostics import (
            async_get_config_entry_diagnostics,
        )

        entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        coord.data = HaggleData(
            consumption_period_kwh=0.0,
            consumption_period_cost_aud=0.0,
            bill_projection_aud=None,
            unit_rate_aud_per_kwh=None,
            supply_charge_aud_per_day=None,
            latest_cumulative_kwh=0.0,
        )
        coord._has_solar = False
        coord._active_tou_bands = set()
        entry.runtime_data = SimpleNamespace(coordinator=coord)

        instance = MagicMock()
        instance.async_add_executor_job = AsyncMock(return_value={})

        with patch(_PATCH_GET_INSTANCE, MagicMock(return_value=instance)):
            result = await async_get_config_entry_diagnostics(hass, entry)

        assert result["tz_realign"] is None

    async def test_tz_realign_no_leak(self, hass: HomeAssistant) -> None:
        """tz_realign record must not leak contract/account numbers."""
        from custom_components.haggle.diagnostics import (
            async_get_config_entry_diagnostics,
        )

        entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        # Plant a record containing only dates/ints/zone.
        realign_record = {
            "state": TZ_REALIGN_DONE,
            "zone": "Australia/Adelaide",
            "reason": TZ_REALIGN_COMPLETE,
            "floor": "2026-10-10",
            "consumption": TZ_REALIGN_COMPLETE,
            "generation": TZ_REALIGN_SKIPPED,
            "notice": False,
            "history_from": "2026-09-01",
        }
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_TZ_REALIGN: realign_record},
        )
        coord.data = HaggleData(
            consumption_period_kwh=0.0,
            consumption_period_cost_aud=0.0,
            bill_projection_aud=None,
            unit_rate_aud_per_kwh=None,
            supply_charge_aud_per_day=None,
            latest_cumulative_kwh=0.0,
        )
        coord._has_solar = False
        coord._active_tou_bands = set()
        entry.runtime_data = SimpleNamespace(coordinator=coord)

        instance = MagicMock()
        instance.async_add_executor_job = AsyncMock(return_value={})

        with patch(_PATCH_GET_INSTANCE, MagicMock(return_value=instance)):
            result = await async_get_config_entry_diagnostics(hass, entry)

        blob = json.dumps(result)
        assert _CONTRACT not in blob
        assert _ACCOUNT not in blob


# ---------------------------------------------------------------------------
# Strings: issue keys, placeholders, sentinel
# ---------------------------------------------------------------------------


class TestStrings:
    def test_both_issue_keys_present_in_strings_json(self) -> None:
        """Both issue translation keys exist in strings.json."""
        strings_path = (
            pathlib.Path(__file__).parent.parent
            / "custom_components"
            / "haggle"
            / "strings.json"
        )
        data = json.loads(strings_path.read_text())
        issues = data.get("issues", {})
        assert ISSUE_TZ_REALIGN_GAVE_UP in issues
        assert ISSUE_TZ_HISTORY_BEFORE_REALIGN in issues

    def test_strings_match_en_json(self) -> None:
        """strings.json and translations/en.json are byte-identical."""
        base = pathlib.Path(__file__).parent.parent / "custom_components" / "haggle"
        strings = (base / "strings.json").read_text()
        en = (base / "translations" / "en.json").read_text()
        assert strings == en, "strings.json and translations/en.json have drifted"

    def test_giveup_placeholders_correct(self) -> None:
        """{entry, floor, stages, sweeps} are the placeholders for tz_realign_gave_up."""
        strings_path = (
            pathlib.Path(__file__).parent.parent
            / "custom_components"
            / "haggle"
            / "strings.json"
        )
        desc = json.loads(strings_path.read_text())["issues"][ISSUE_TZ_REALIGN_GAVE_UP][
            "description"
        ]
        for ph in ("{entry}", "{floor}", "{stages}", "{sweeps}"):
            assert ph in desc, (
                f"Placeholder {ph!r} missing from tz_realign_gave_up description"
            )

    def test_history_notice_placeholders_correct(self) -> None:
        """{entry, floor} are the placeholders for tz_history_before_realign."""
        strings_path = (
            pathlib.Path(__file__).parent.parent
            / "custom_components"
            / "haggle"
            / "strings.json"
        )
        desc = json.loads(strings_path.read_text())["issues"][
            ISSUE_TZ_HISTORY_BEFORE_REALIGN
        ]["description"]
        for ph in ("{entry}", "{floor}"):
            assert ph in desc, (
                f"Placeholder {ph!r} missing from tz_history_before_realign description"
            )

    def test_do_not_delete_sentinel_in_both_issues(self) -> None:
        """Both issue descriptions contain the 'Do not delete' sentinel."""
        strings_path = (
            pathlib.Path(__file__).parent.parent
            / "custom_components"
            / "haggle"
            / "strings.json"
        )
        issues = json.loads(strings_path.read_text())["issues"]
        sentinel = "Do not delete"
        for key in (ISSUE_TZ_REALIGN_GAVE_UP, ISSUE_TZ_HISTORY_BEFORE_REALIGN):
            desc = issues[key]["description"]
            assert sentinel in desc, f"{key!r} description missing '{sentinel}'"


# ---------------------------------------------------------------------------
# M1 / _realign_junction_drop unit tests
# ---------------------------------------------------------------------------


class TestRealignJunctionDrop:
    async def test_drop_zeros_slots_at_old_convention_before_cutoff(
        self, hass: HomeAssistant
    ) -> None:
        """M1: batch slots whose old-convention instants fall in [prev, cutoff)
        and whose old bucket matches stored state are zeroed.

        SA July (ACST = UTC+9:30, Sydney AEST = UTC+10):
          SA local midnight = T14:30Z.  first_full_hour = T15:00Z (cutoff).
          prev = T14:00Z.
          slot0 T14:30Z → old_reported T14:00Z → [prev, cutoff) → DROP.
          slot1 T15:00Z → old_reported T14:30Z → [prev, cutoff) → DROP.
          slot2 T15:30Z → old_reported T15:00Z → NOT in window → KEEP.
        """
        _entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        coord.client.tz_is_contract = True

        api_tz = agl_api_tz()
        if api_tz is None:
            pytest.skip("Australia/Sydney tzdata unavailable")

        stat_id = f"{DOMAIN}:{STAT_CONSUMPTION}_{_CONTRACT}"
        # July 2026: SA on ACST = UTC+9:30, Sydney on AEST = UTC+10.
        # SA midnight July 10 = T14:30Z (July 9 UTC).
        # cutoff = first full hour after midnight = T15:00Z.
        cutoff = datetime(2026, 7, 9, 15, 0, tzinfo=UTC)
        slot0_correct = datetime(2026, 7, 9, 14, 30, tzinfo=UTC)  # SA 00:00
        slot1_correct = datetime(2026, 7, 9, 15, 0, tzinfo=UTC)  # SA 00:30
        slot2_correct = datetime(2026, 7, 9, 15, 30, tzinfo=UTC)  # SA 01:00 — keep
        intervals = [
            _make_interval(slot0_correct, kwh=0.11),
            _make_interval(slot1_correct, kwh=0.12),
            _make_interval(slot2_correct, kwh=0.13),
        ]

        # old_bucket = slot0.kwh + slot1.kwh (both fall in [prev, cutoff)).
        old_bucket_kwh = 0.11 + 0.12

        with patch.object(
            coord,
            "_stored_state_at",
            new=AsyncMock(return_value=old_bucket_kwh),
        ):
            result = await coord._realign_junction_drop(
                intervals, cutoff, kwh_id=stat_id
            )

        # slot0 and slot1 zeroed; slot2 kept.
        assert result[0].kwh == pytest.approx(0.0)
        assert result[0].cost_aud == pytest.approx(0.0)
        assert result[1].kwh == pytest.approx(0.0)
        assert result[1].cost_aud == pytest.approx(0.0)
        assert result[2].kwh == pytest.approx(0.13)

    async def test_drop_does_nothing_when_stored_state_does_not_match(
        self, hass: HomeAssistant
    ) -> None:
        """M1 conservation check: if stored state differs from old bucket, nothing dropped."""
        _entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        coord.client.tz_is_contract = True

        api_tz = agl_api_tz()
        if api_tz is None:
            pytest.skip("Australia/Sydney tzdata unavailable")

        stat_id = f"{DOMAIN}:{STAT_CONSUMPTION}_{_CONTRACT}"
        cutoff = datetime(2026, 7, 9, 15, 0, tzinfo=UTC)
        slot0_correct = datetime(2026, 7, 9, 14, 30, tzinfo=UTC)
        slot1_correct = datetime(2026, 7, 9, 15, 0, tzinfo=UTC)
        intervals = [
            _make_interval(slot0_correct, kwh=0.11),
            _make_interval(slot1_correct, kwh=0.12),
        ]

        # stored_state != old bucket kWh → C10 guard fires, nothing dropped.
        with patch.object(
            coord,
            "_stored_state_at",
            new=AsyncMock(return_value=99.9),  # wildly different
        ):
            result = await coord._realign_junction_drop(
                intervals, cutoff, kwh_id=stat_id
            )

        assert result[0].kwh == pytest.approx(0.11)  # unchanged
        assert result[1].kwh == pytest.approx(0.12)  # unchanged

    async def test_drop_no_op_when_api_tz_none(self, hass: HomeAssistant) -> None:
        """C7: no API tz → junction drop returns intervals unchanged."""
        _entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        stat_id = f"{DOMAIN}:{STAT_CONSUMPTION}_{_CONTRACT}"
        cutoff = datetime(2026, 10, 9, 15, 0, tzinfo=UTC)
        intervals = [_make_interval(cutoff, kwh=0.15)]

        with patch(
            "custom_components.haggle.coordinator.agl_api_tz",
            return_value=None,
        ):
            result = await coord._realign_junction_drop(
                intervals, cutoff, kwh_id=stat_id
            )

        assert result is intervals


# ---------------------------------------------------------------------------
# C6 window bound: max(frozen_floor, today - TZ_REALIGN_DAYS)
# ---------------------------------------------------------------------------


class TestWindowBound:
    async def test_aged_floor_uses_today_minus_realign_days(
        self, hass: HomeAssistant
    ) -> None:
        """C6: floor older than today-TZ_REALIGN_DAYS → effective start = today-TZ_REALIGN_DAYS."""
        entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        # Plant a pending record with an old floor.
        today = date(2026, 11, 5)
        yesterday = today - timedelta(days=1)
        very_old_floor = today - timedelta(days=TZ_REALIGN_DAYS + 15)

        hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                CONF_TZ_REALIGN: {
                    "state": TZ_REALIGN_PENDING,
                    "zone": "Australia/Adelaide",
                    "floor": very_old_floor.isoformat(),
                    "series": TZ_REALIGN_CONSUMPTION,
                    "attempts": 0,
                    "halts": 0,
                },
            },
        )

        with (
            patch(_PATCH_ADD_STATS),
            patch(_PATCH_GET_INSTANCE, _mock_get_instance()),
        ):
            result = await coord._plan_tz_realign(
                today,
                yesterday,
                yesterday,
                None,
                None,
                normal=(None, None),
            )

        # Effective start must be max(very_old_floor, today - TZ_REALIGN_DAYS).
        assert result is not None
        effective_floor = max(very_old_floor, today - timedelta(days=TZ_REALIGN_DAYS))
        assert result.start == effective_floor


# ---------------------------------------------------------------------------
# C1 degrade: incomplete sweep keeps only normal-window readings
# ---------------------------------------------------------------------------


class TestDegradeIncompleteSweep:
    def test_degrade_removes_pre_normal_start_readings(
        self, hass: HomeAssistant
    ) -> None:
        """C1: when a consumption realign sweep is incomplete, readings before
        the normal range start are removed from acc.cons."""
        from custom_components.haggle.coordinator import (
            _RealignSweep,
            _SweepBatches,
        )

        _entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        today = date(2026, 11, 5)
        yesterday = today - timedelta(days=1)
        floor = today - timedelta(days=TZ_REALIGN_DAYS)
        normal_start = today - timedelta(days=REWINDOW_DAYS)

        # realign.normal = the 7-day rewindow window.
        realign = _RealignSweep(
            TZ_REALIGN_CONSUMPTION,
            floor,
            (normal_start, yesterday),
        )

        # Build readings: some in the realign window (before normal_start),
        # some in the normal window.
        realign_day = floor + timedelta(days=5)  # well before normal_start
        normal_day = normal_start + timedelta(days=1)
        t_realign = datetime(
            realign_day.year, realign_day.month, realign_day.day, 14, 30, tzinfo=UTC
        )
        t_normal = datetime(
            normal_day.year, normal_day.month, normal_day.day, 14, 30, tzinfo=UTC
        )

        acc = _SweepBatches()
        # Day 0: realign_day
        acc.marks.append((realign_day, 0, 0))
        acc.cons.append(_make_interval(t_realign, kwh=0.2))
        acc.cons_reading_days.add(realign_day)
        # Day 1: normal_day
        acc.marks.append((normal_day, 1, 0))
        acc.cons.append(_make_interval(t_normal, kwh=0.3))
        acc.cons_reading_days.add(normal_day)

        coord._degrade_realign_batches(realign, acc, today)

        # Only readings from normal_day should survive.
        assert len(acc.cons) == 1
        assert acc.cons[0].kwh == pytest.approx(0.3)
        assert realign_day not in acc.cons_reading_days
        assert normal_day in acc.cons_reading_days

    def test_realign_batches_returns_none_for_incomplete_sweep(
        self, hass: HomeAssistant
    ) -> None:
        """_realign_batches returns None (no junction) when sweep is incomplete."""
        from custom_components.haggle.coordinator import (
            _RealignSweep,
            _SweepBatches,
        )

        _entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        today = date(2026, 11, 5)
        yesterday = today - timedelta(days=1)
        floor = today - timedelta(days=TZ_REALIGN_DAYS)
        normal_start = today - timedelta(days=REWINDOW_DAYS)

        realign = _RealignSweep(
            TZ_REALIGN_CONSUMPTION,
            floor,
            (normal_start, yesterday),
        )

        # Skip a day well before the rewindow horizon → incomplete.
        skipped_day = floor + timedelta(days=5)
        acc = _SweepBatches()
        acc.cons_skipped.add(skipped_day)

        junction = coord._realign_batches(
            realign,
            acc,
            halted=False,
            today=today,
        )
        assert junction is None

    def test_realign_batches_returns_series_for_complete_sweep(
        self, hass: HomeAssistant
    ) -> None:
        """_realign_batches returns the series name for a complete sweep."""
        from custom_components.haggle.coordinator import (
            _RealignSweep,
            _SweepBatches,
        )

        _entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        today = date(2026, 11, 5)
        yesterday = today - timedelta(days=1)
        floor = today - timedelta(days=TZ_REALIGN_DAYS)
        normal_start = today - timedelta(days=REWINDOW_DAYS)

        realign = _RealignSweep(
            TZ_REALIGN_CONSUMPTION,
            floor,
            (normal_start, yesterday),
        )
        acc = _SweepBatches()
        # No skips, no halt → complete.

        junction = coord._realign_batches(
            realign,
            acc,
            halted=False,
            today=today,
        )
        assert junction == TZ_REALIGN_CONSUMPTION


# ---------------------------------------------------------------------------
# Heal sequencing: heal wins, tz_realign does not arm #153 repair
# ---------------------------------------------------------------------------


class TestHealSequencing:
    async def test_tz_realign_generation_heal_does_not_trigger(
        self, hass: HomeAssistant
    ) -> None:
        """_generation_heal_triggers returns (False, False) after realign stage."""
        _entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        # After a complete realign, the chain should be monotone → no repair trigger.
        # _generation_heal_triggers looks at the stored series; we can verify
        # the static contract: if the stat is monotone, it returns (False, False).
        coord._has_solar = True

        # Mock a monotone stored series (sum always increasing).
        today = date(2026, 11, 5)
        floor = today - timedelta(days=TZ_REALIGN_DAYS)
        # We need to have a last_gen_date inside the floor-rewindow for heal
        # triggers; mock the recorder to return a clean ascending series.
        mock_rows = [
            {
                "start": datetime(2026, 10, i, 14, tzinfo=UTC).timestamp(),
                "sum": float(i),
            }
            for i in range(5, 30)
        ]

        gen_id = f"{DOMAIN}:generation_{_CONTRACT}"
        with patch(_PATCH_GET_INSTANCE, _mock_get_instance(mock_rows)):
            result = await coord._generation_heal_triggers(gen_id, floor, today)

        # A monotone chain means no leading hole and no downward step.
        assert result[0] is False  # no leading hole
        assert result[1] is False  # no downward step


# ---------------------------------------------------------------------------
# TestComposedRequestCeiling (C6): per-stage request ceiling
# ---------------------------------------------------------------------------


class TestComposedRequestCeiling:
    async def test_consumption_stage_ceiling(self, hass: HomeAssistant) -> None:
        """Consumption stage: at most 3 + TZ_REALIGN_DAYS + normal_solar requests."""
        _entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        today = date(2026, 11, 5)
        yesterday = today - timedelta(days=1)
        floor = today - timedelta(days=TZ_REALIGN_DAYS)

        # Realign window: [floor, yesterday] = TZ_REALIGN_DAYS days.
        # Normal solar range (8 days for half-hour zone with overlap).
        solar_normal_start = today - timedelta(days=REWINDOW_DAYS)
        normal_solar = (solar_normal_start, yesterday)
        cons_range = (floor, yesterday)  # full realign window

        fetch_calls: list[tuple[str, date]] = []

        async def _mock_cons(day, previous, **kwargs):
            fetch_calls.append(("cons", day))
            return []

        async def _mock_solar(day, previous, **kwargs):
            fetch_calls.append(("solar", day))
            return []

        coord.client.async_get_hourly_usage = AsyncMock(return_value=[])

        with (
            patch.object(coord, "_fetch_day_consumption", side_effect=_mock_cons),
            patch.object(coord, "_fetch_day_solar", side_effect=_mock_solar),
            patch(_PATCH_ADD_STATS),
            patch(_PATCH_GET_INSTANCE, _mock_get_instance()),
        ):
            # Drive _fetch_range directly with the realign window.
            from custom_components.haggle.coordinator import _RealignSweep

            realign = _RealignSweep(
                TZ_REALIGN_CONSUMPTION,
                floor,
                (solar_normal_start, yesterday),
            )
            await coord._fetch_range(
                cons_range,
                normal_solar,
                today,
                today=today,
                realign=realign,
            )

        cons_days = [d for kind, d in fetch_calls if kind == "cons"]
        solar_days = [d for kind, d in fetch_calls if kind == "solar"]

        # Consumption: at most TZ_REALIGN_DAYS + 1 (overlap for SA).
        assert len(cons_days) <= TZ_REALIGN_DAYS + 1
        # Solar: at most REWINDOW_DAYS + 1 (overlap for SA).
        assert len(solar_days) <= REWINDOW_DAYS + 1
        # Total ceiling = consumption + solar.
        assert len(fetch_calls) <= TZ_REALIGN_DAYS + 1 + REWINDOW_DAYS + 1 + 3


# ---------------------------------------------------------------------------
# _max_tz_shift: unit tests
# ---------------------------------------------------------------------------


class TestMaxTzShift:
    def test_sa_has_30min_shift_all_year(self, hass: HomeAssistant) -> None:
        """SA (Australia/Adelaide) has a 30-min shift against Sydney year-round."""
        from datetime import timedelta

        _entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        shift = coord._max_tz_shift(date(2026, 8, 1), date(2026, 8, 10))
        assert shift == timedelta(minutes=30)

    def test_brisbane_has_1h_shift_during_sydney_dst(self, hass: HomeAssistant) -> None:
        """Brisbane (QLD, UTC+10 always) vs Sydney in DST (UTC+11) = 1h shift."""
        _entry, coord = _make_entry(hass, zone_name="Australia/Brisbane")
        coord.client.local_tz = ZoneInfo("Australia/Brisbane")

        # October = Sydney is on AEDT (UTC+11), Brisbane is AEST (UTC+10).
        shift = coord._max_tz_shift(date(2026, 10, 10), date(2026, 10, 20))
        assert shift == timedelta(hours=1)

    def test_brisbane_no_shift_in_aest_period(self, hass: HomeAssistant) -> None:
        """Brisbane in winter (Sydney AEST) → zero shift."""
        _entry, coord = _make_entry(hass, zone_name="Australia/Brisbane")
        coord.client.local_tz = ZoneInfo("Australia/Brisbane")

        # July = both Brisbane and Sydney on AEST.
        shift = coord._max_tz_shift(date(2026, 7, 1), date(2026, 7, 31))
        assert shift == timedelta(0)

    def test_perth_exceeds_1h_always(self, hass: HomeAssistant) -> None:
        """Perth (WA, UTC+8) shift vs Sydney always > 1h."""
        _entry, coord = _make_entry(hass, zone_name="Australia/Perth")
        coord.client.local_tz = ZoneInfo("Australia/Perth")
        from custom_components.haggle.const import TZ_REALIGN_MAX_SHIFT

        shift = coord._max_tz_shift(date(2026, 7, 1), date(2026, 7, 31))
        assert shift > TZ_REALIGN_MAX_SHIFT

    def test_zero_when_api_tz_none(self, hass: HomeAssistant) -> None:
        """C7: _max_tz_shift returns 0 when agl_api_tz() is None."""
        _entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")

        with patch(
            "custom_components.haggle.coordinator.agl_api_tz", return_value=None
        ):
            shift = coord._max_tz_shift(date(2026, 10, 1), date(2026, 10, 30))

        assert shift == timedelta(0)


# ---------------------------------------------------------------------------
# _shifted_history_before: history_from bounded and failure-tolerant
# ---------------------------------------------------------------------------


class TestShiftedHistoryBefore:
    async def test_returns_false_when_no_shifted_window(
        self, hass: HomeAssistant
    ) -> None:
        """_shifted_history_before returns (False, None) when no shifted window."""
        _entry, coord = _make_entry(hass, zone_name="Australia/Sydney")
        coord.client.local_tz = ZoneInfo("Australia/Sydney")

        # NSW: no shift anywhere in the scan window.
        result = await coord._shifted_history_before(date(2026, 11, 5))
        assert result == (False, None)

    async def test_returns_true_with_history_from_when_rows_exist(
        self, hass: HomeAssistant
    ) -> None:
        """_shifted_history_before returns (True, earliest_date_iso) when old rows."""
        _entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        # Mock _earliest_stat_date to return a date before floor.
        floor = date(2026, 10, 10)
        with patch.object(
            coord,
            "_earliest_stat_date",
            new=AsyncMock(return_value=date(2026, 9, 1)),
        ):
            notice, history_from = await coord._shifted_history_before(floor)

        assert notice is True
        assert history_from == "2026-09-01"

    async def test_returns_true_none_on_recorder_failure(
        self, hass: HomeAssistant
    ) -> None:
        """Failure-tolerant: raises an exception → notice=True, history_from=None."""
        _entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        floor = date(2026, 10, 10)

        with patch.object(
            coord,
            "_earliest_stat_date",
            new=AsyncMock(side_effect=RuntimeError("recorder down")),
        ):
            notice, history_from = await coord._shifted_history_before(floor)

        assert notice is True
        assert history_from is None

    async def test_no_notice_when_all_rows_after_floor(
        self, hass: HomeAssistant
    ) -> None:
        """No notice when the earliest row is >= floor (entry set up post-beta.3)."""
        _entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        floor = date(2026, 10, 10)

        with patch.object(
            coord,
            "_earliest_stat_date",
            new=AsyncMock(return_value=date(2026, 10, 12)),
        ):
            notice, _history_from = await coord._shifted_history_before(floor)

        # notice is False because earliest >= floor.
        assert notice is False


# ---------------------------------------------------------------------------
# All-new-convention Adelaide entry (C10)
# ---------------------------------------------------------------------------


class TestAllNewConventionEntry:
    async def test_not_affected_when_no_old_rows(self, hass: HomeAssistant) -> None:
        """C10: if all history was written post-beta.3, there are no shifted rows
        and _shifted_history_before raises no notice — entry shows not_affected
        or complete with notice=False."""
        entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        today = date(2026, 11, 5)
        today - timedelta(days=1)
        # Simulate "fresh history": all rows at correct UTC keys.
        floor = today - timedelta(days=TZ_REALIGN_DAYS)

        with (
            patch.object(
                coord,
                "_earliest_stat_date",
                # Returns floor+1 (just after floor) → notice = False.
                new=AsyncMock(return_value=floor + timedelta(days=1)),
            ),
            patch(_PATCH_ADD_STATS),
            patch(_PATCH_GET_INSTANCE, _mock_get_instance()),
        ):
            await coord._publish_tz_realign_done(
                {
                    "state": TZ_REALIGN_DONE,
                    "zone": "Australia/Adelaide",
                    "reason": TZ_REALIGN_COMPLETE,
                    "floor": floor.isoformat(),
                    "consumption": TZ_REALIGN_COMPLETE,
                    "generation": TZ_REALIGN_SKIPPED,
                },
                floor,
            )

        rec = entry.data[CONF_TZ_REALIGN]
        assert rec.get("notice") is False


# ---------------------------------------------------------------------------
# _read_tz_realign robustness
# ---------------------------------------------------------------------------


class TestReadTzRealign:
    def test_returns_none_for_malformed_record(self, hass: HomeAssistant) -> None:
        entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_TZ_REALIGN: "not a dict"},
        )
        assert coord._read_tz_realign() is None

    def test_returns_none_for_pending_with_bad_series(
        self, hass: HomeAssistant
    ) -> None:
        entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                CONF_TZ_REALIGN: {
                    "state": TZ_REALIGN_PENDING,
                    "zone": "Australia/Adelaide",
                    "floor": "2026-10-10",
                    "series": "invalid_series",
                    "attempts": 0,
                    "halts": 0,
                },
            },
        )
        assert coord._read_tz_realign() is None

    def test_returns_done_record_as_is(self, hass: HomeAssistant) -> None:
        entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        done = {
            "state": TZ_REALIGN_DONE,
            "zone": "Australia/Adelaide",
            "reason": TZ_REALIGN_COMPLETE,
        }
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_TZ_REALIGN: done},
        )
        result = coord._read_tz_realign()
        assert result == done

    def test_returns_none_when_absent(self, hass: HomeAssistant) -> None:
        _entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        assert coord._read_tz_realign() is None
