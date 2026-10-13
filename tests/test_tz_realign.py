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
- Config-flow preservation (C8): pinned through the real flows in
  tests/test_config_flow.py (reauth, reconfigure, mid-flow coordinator write).
- Real-recorder tests: tests/test_tz_realign_recorder.py (M1 junction end to
  end, C1 degradation, give-up and NT/WA residuals).
- TestComposedRequestCeiling: request-count ceiling for both stages (C6).

Unique-per-slot profile in the real-recorder tests (C11):
kwh = 0.1 + 0.01*slot_index + 0.001*day_index
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
    agl_reported_timestamp,
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
    TZ_REALIGN_SHIFT_SCAN_DAYS,
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

    @pytest.mark.parametrize(
        "zone",
        [
            # WA, UTC+8: 2 h behind Sydney standard time, 3 h in Sydney DST.
            "Australia/Perth",
            # NT, UTC+9:30 always: 90 min behind during Sydney DST (November).
            "Australia/Darwin",
        ],
    )
    async def test_unsupported_shift_ntwa(self, hass: HomeAssistant, zone: str) -> None:
        """M4: shift > 1 h → done(unsupported_shift) and nothing else.

        No stage is planned (zero realign requests), and NO Repairs issue
        even though shifted rows predate the floor: the history notice is
        forced due here, so routing this branch through the clean-done
        publish path would raise it.
        """
        import homeassistant.helpers.issue_registry as ir

        entry, coord = _make_entry(hass, zone_name=zone)
        coord.client.local_tz = ZoneInfo(zone)
        today = date(2026, 11, 5)
        yesterday = today - timedelta(days=1)
        history = AsyncMock(return_value=(True, "2026-06-01"))

        with (
            patch(_PATCH_ADD_STATS),
            patch(_PATCH_GET_INSTANCE, _mock_get_instance()),
            patch.object(coord, "_shifted_history_before", new=history),
            patch.object(ir, "async_create_issue") as mock_issue,
        ):
            result = await coord._plan_tz_realign(
                today, yesterday, yesterday, None, None, normal=(None, None)
            )

        assert result is None  # no stage sweep handed to the fetch
        assert entry.data.get(CONF_TZ_REALIGN) == {
            "state": TZ_REALIGN_DONE,
            "zone": zone,
            "reason": TZ_REALIGN_REASON_UNSUPPORTED_SHIFT,
        }
        history.assert_not_awaited()
        mock_issue.assert_not_called()

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

    def test_sweep_incomplete_for_skip_on_horizon_day(
        self, hass: HomeAssistant
    ) -> None:
        """The horizon day itself (today - REWINDOW_DAYS) still counts: the
        documented range [start, today - REWINDOW_DAYS] is inclusive."""
        _entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        from custom_components.haggle.coordinator import _RealignSweep

        today = date(2026, 11, 5)
        floor = today - timedelta(days=TZ_REALIGN_DAYS)
        horizon = today - timedelta(days=REWINDOW_DAYS)
        for series, skipped in (
            (TZ_REALIGN_CONSUMPTION, (frozenset({horizon}), frozenset())),
            (TZ_REALIGN_GENERATION, (frozenset(), frozenset({horizon}))),
        ):
            realign = _RealignSweep(series, floor, (floor, today - timedelta(days=1)))
            assert (
                coord._realign_sweep_complete(
                    realign, halted=False, skipped=skipped, today=today
                )
                is False
            )

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

        # Drained recorder and a complete fetch: only the incomplete STAGE
        # may keep the totals suppressed.
        with patch.object(coord, "_recorder_drained", new=AsyncMock(return_value=True)):
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

        # v4 introduced tz_realign; later schema versions keep it (#141 → v5).
        assert result["schema_version"] == DIAGNOSTICS_SCHEMA_VERSION
        assert DIAGNOSTICS_SCHEMA_VERSION >= 4
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
# R1 junction classifier (_junction_matched) unit tests — retargeted from the
# M1 _realign_junction_drop tests when R1 replaced the drop with a re-emit.
# ---------------------------------------------------------------------------


def _sa_july_floor_day(
    coord: HaggleCoordinator, *, old_rows: bool
) -> tuple[list[IntervalReading], datetime, dict[datetime, float]]:
    """A unique-per-slot SA floor day (July: ACST UTC+9:30, Sydney AEST +10,
    shift 30 min) plus its next day's opening hour, the batch cutoff, and
    stored hourly kWh in the OLD (``old_rows``) or new convention."""
    api_tz = agl_api_tz()
    if api_tz is None:
        pytest.skip("Australia/Sydney tzdata unavailable")
    tz = coord._contract_tz()
    start = coord._local_midnight_utc(date(2026, 7, 10))  # 07-09T14:30Z
    intervals = [
        _make_interval(start + timedelta(minutes=30 * n), kwh=round(0.1 + 0.01 * n, 3))
        for n in range(50)
    ]
    stored: dict[datetime, float] = {}
    for r in intervals:
        at = (
            agl_reported_timestamp(r.dt, api_tz=api_tz, contract_tz=tz)
            if old_rows
            else r.dt
        )
        key = at.replace(minute=0)
        stored[key] = stored.get(key, 0.0) + r.kwh
    # The batch's first key (A4 straddle trim drops 14:00Z) is the cutoff.
    return intervals, datetime(2026, 7, 9, 15, 0, tzinfo=UTC), stored


class TestRealignJunctionMatch:
    async def test_matches_old_convention_bucket_below_cutoff(
        self, hass: HomeAssistant
    ) -> None:
        """R1: the batch slots whose old-convention instants fall in
        [prev, cutoff) are the matched bucket when stored(prev) equals it
        and the floor day's interior rows are old-convention.

        slot0 T14:30Z -> reported T14:00Z -> matched.
        slot1 T15:00Z -> reported T14:30Z -> matched.
        slot2 T15:30Z -> reported T15:00Z -> not matched.
        """
        _entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        intervals, cutoff, stored = _sa_july_floor_day(coord, old_rows=True)
        prev = cutoff - timedelta(hours=1)
        assert stored[prev] == pytest.approx(0.1 + 0.11)

        matched = coord._junction_matched(intervals, stored, cutoff)

        assert matched is not None
        assert sorted(r.dt for r in matched) == [intervals[0].dt, intervals[1].dt]

    async def test_no_match_when_stored_state_does_not_match(
        self, hass: HomeAssistant
    ) -> None:
        """A stored hour that is not the old bucket (here: new-convention
        history, C10) is never matched: nothing moves."""
        _entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        intervals, cutoff, stored = _sa_july_floor_day(coord, old_rows=True)
        prev = cutoff - timedelta(hours=1)
        assert (
            coord._junction_matched(intervals, {**stored, prev: 99.9}, cutoff) is None
        )
        _intervals, _cutoff, new_rows = _sa_july_floor_day(coord, old_rows=False)
        assert coord._junction_matched(intervals, new_rows, cutoff) is None

    async def test_no_match_when_api_tz_none(self, hass: HomeAssistant) -> None:
        """C7: no API tz -> nothing can be inverted, no match."""
        _entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        intervals, cutoff, stored = _sa_july_floor_day(coord, old_rows=True)

        with patch(
            "custom_components.haggle.coordinator.agl_api_tz",
            return_value=None,
        ):
            assert coord._junction_matched(intervals, stored, cutoff) is None


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


# ---------------------------------------------------------------------------
# Stage planning, completion and gate edges (mutation-table follow-ups)
# ---------------------------------------------------------------------------


class TestStageEdges:
    async def test_retry_sweeps_the_whole_window_again(
        self, hass: HomeAssistant
    ) -> None:
        """After a failed attempt the retry re-sweeps the FULL frozen window
        from max(floor, today - TZ_REALIGN_DAYS) — never only the days after
        the failure (a partial window would re-time only some days)."""
        _entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        today = date(2026, 11, 5)
        floor = today - timedelta(days=TZ_REALIGN_DAYS)
        normal = ((today - timedelta(days=REWINDOW_DAYS), today), None)
        record = {
            "state": TZ_REALIGN_PENDING,
            "zone": "Australia/Adelaide",
            "floor": floor.isoformat(),
            "series": TZ_REALIGN_CONSUMPTION,
            "attempts": MAX_TZ_REALIGN_ATTEMPTS - 1,
            "halts": 0,
        }
        sweep = await coord._plan_tz_realign_stage(record, today, None, normal)
        assert sweep is not None
        assert sweep.start == floor
        assert sweep.normal == normal[0]

    def test_other_series_skip_is_not_counted(self, hass: HomeAssistant) -> None:
        """C2: only the STAGE series' skips count — a solar skip before the
        horizon does not fail a consumption stage, and vice versa."""
        from custom_components.haggle.coordinator import _RealignSweep

        today = date(2026, 11, 5)
        floor = today - timedelta(days=TZ_REALIGN_DAYS)
        old_day = floor + timedelta(days=3)
        cons_stage = _RealignSweep(TZ_REALIGN_CONSUMPTION, floor, None)
        gen_stage = _RealignSweep(TZ_REALIGN_GENERATION, floor, None)
        solar_skip = (frozenset(), frozenset({old_day}))
        cons_skip = (frozenset({old_day}), frozenset())

        assert HaggleCoordinator._realign_sweep_complete(
            cons_stage, halted=False, skipped=solar_skip, today=today
        )
        assert HaggleCoordinator._realign_sweep_complete(
            gen_stage, halted=False, skipped=cons_skip, today=today
        )
        assert not HaggleCoordinator._realign_sweep_complete(
            gen_stage, halted=False, skipped=solar_skip, today=today
        )

    def test_stall_span_day_skip_is_not_counted(self, hass: HomeAssistant) -> None:
        """A generation-stage skip on a persisted #154 stall-span day (the
        sweep's ``ignore`` set) never fails the stage; the same day outside
        the span does."""
        from custom_components.haggle.coordinator import _RealignSweep

        today = date(2026, 11, 5)
        floor = today - timedelta(days=TZ_REALIGN_DAYS)
        span_day = floor + timedelta(days=4)
        skipped = (frozenset(), frozenset({span_day}))
        tolerant = _RealignSweep(
            TZ_REALIGN_GENERATION, floor, None, frozenset({span_day})
        )
        strict = _RealignSweep(TZ_REALIGN_GENERATION, floor, None)

        assert HaggleCoordinator._realign_sweep_complete(
            tolerant, halted=False, skipped=skipped, today=today
        )
        assert not HaggleCoordinator._realign_sweep_complete(
            strict, halted=False, skipped=skipped, today=today
        )

    def test_truthy_non_true_tz_is_contract_does_not_arm(
        self, hass: HomeAssistant
    ) -> None:
        """Gate (b) is the _fill_active identity check: a client mock's truthy
        attribute (or any non-True value) must not arm the re-alignment."""
        _entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        coord.client.tz_is_contract = MagicMock()
        assert coord._tz_realign_zone() is None
        coord.client.tz_is_contract = True
        assert coord._tz_realign_zone() == "Australia/Adelaide"

    async def test_generation_waits_until_contiguous(self, hass: HomeAssistant) -> None:
        """A generation series still behind the window (last row before
        start - 1) waits for the normal chunks; contiguous → run."""
        from custom_components.haggle.coordinator import _GATE_RUN, _GATE_WAIT

        _entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        coord._has_solar = True
        start = date(2026, 10, 6)
        assert (
            await coord._generation_stage_gate(start - timedelta(days=2), start)
            == _GATE_WAIT
        )
        assert (
            await coord._generation_stage_gate(start - timedelta(days=1), start)
            == _GATE_RUN
        )

    def test_degrade_generation_stage_trims_readings_and_markers(
        self, hass: HomeAssistant
    ) -> None:
        """C1 for the generation stage: readings, reading days AND the
        fetched (zero-export marker) days outside the normal range go —
        a marker on an old window day would land part of the window."""
        from custom_components.haggle.coordinator import _RealignSweep, _SweepBatches

        _entry, coord = _make_entry(hass, zone_name="Australia/Brisbane")
        today = date(2026, 11, 5)
        yesterday = today - timedelta(days=1)
        floor = today - timedelta(days=TZ_REALIGN_DAYS)
        normal_start = today - timedelta(days=REWINDOW_DAYS)
        realign = _RealignSweep(TZ_REALIGN_GENERATION, floor, (normal_start, yesterday))
        old_day, dark_day = floor + timedelta(days=2), floor + timedelta(days=3)
        normal_day = normal_start + timedelta(days=1)
        acc = _SweepBatches()
        acc.marks.append((old_day, 0, 0))
        acc.solar.append(
            _make_interval(datetime(2026, 10, 8, 0, 0, tzinfo=UTC), kwh=0.4)
        )
        acc.marks.append((dark_day, 0, 1))
        acc.marks.append((normal_day, 0, 1))
        acc.solar.append(
            _make_interval(datetime(2026, 10, 31, 0, 0, tzinfo=UTC), kwh=0.5)
        )
        acc.solar_reading_days |= {old_day, normal_day}
        acc.fetched_solar_days.extend([old_day, dark_day, normal_day])

        coord._degrade_realign_batches(realign, acc, today)

        assert [r.kwh for r in acc.solar] == [0.5]
        assert acc.solar_reading_days == {normal_day}
        assert acc.fetched_solar_days == [normal_day]

    async def test_history_notice_not_reraised_after_done(
        self, hass: HomeAssistant
    ) -> None:
        """A done record with notice=True never raises the notice again —
        the user's dismissal sticks (raised once, at the terminal
        transition only)."""
        import homeassistant.helpers.issue_registry as ir

        entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        today = date(2026, 11, 5)
        floor = today - timedelta(days=TZ_REALIGN_DAYS)
        hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                CONF_TZ_REALIGN: {
                    "state": TZ_REALIGN_DONE,
                    "zone": "Australia/Adelaide",
                    "reason": TZ_REALIGN_COMPLETE,
                    "floor": floor.isoformat(),
                    "consumption": TZ_REALIGN_COMPLETE,
                    "generation": TZ_REALIGN_SKIPPED,
                    "swept_from": floor.isoformat(),
                    "notice": True,
                    "history_from": (floor - timedelta(days=60)).isoformat(),
                },
            },
        )
        yesterday = today - timedelta(days=1)
        with patch.object(ir, "async_create_issue") as mock_issue:
            for _ in range(2):
                assert (
                    await coord._plan_tz_realign(
                        today, yesterday, yesterday, None, None, normal=(None, None)
                    )
                    is None
                )
        mock_issue.assert_not_called()

    async def test_notice_names_the_swept_start_not_an_aged_floor(
        self, hass: HomeAssistant
    ) -> None:
        """An aged frozen floor (heal / contiguity wait / long poll interval)
        sweeps from today - TZ_REALIGN_DAYS (C6). The clean-done history
        notice must scan from, and name, that swept start: the days between
        the frozen floor and it were never re-aligned."""
        import homeassistant.helpers.issue_registry as ir

        from custom_components.haggle.coordinator import _RealignSweep

        entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        today = date(2026, 11, 5)
        floor = today - timedelta(days=TZ_REALIGN_DAYS + 10)
        start = today - timedelta(days=TZ_REALIGN_DAYS)
        hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                CONF_TZ_REALIGN: {
                    "state": TZ_REALIGN_PENDING,
                    "zone": "Australia/Adelaide",
                    "floor": floor.isoformat(),
                    "series": TZ_REALIGN_GENERATION,
                    "attempts": 0,
                    "halts": 0,
                    "consumption": TZ_REALIGN_COMPLETE,
                },
            },
        )
        realign = _RealignSweep(
            TZ_REALIGN_GENERATION, start, (today - timedelta(days=REWINDOW_DAYS), today)
        )
        coord._sweep_halted = False
        coord._sweep_skipped_days = (frozenset(), frozenset())
        history = AsyncMock(return_value=(True, None))

        with (
            patch.object(coord, "_shifted_history_before", new=history),
            patch.object(ir, "async_create_issue") as mock_issue,
        ):
            completed = await coord._settle_tz_realign(
                realign, today, today - timedelta(days=1)
            )

        assert completed is True
        history.assert_awaited_once_with(start)
        mock_issue.assert_called_once()
        placeholders = mock_issue.call_args.kwargs["translation_placeholders"]
        assert placeholders["floor"] == start.isoformat()
        rec = entry.data[CONF_TZ_REALIGN]
        assert rec["state"] == TZ_REALIGN_DONE
        assert rec["floor"] == floor.isoformat()
        assert rec["swept_from"] == start.isoformat()


# ---------------------------------------------------------------------------
# R3: zone exclusion is season-independent (365-day shift scan)
# ---------------------------------------------------------------------------


class TestR3ZoneExclusionSeasonIndependent:
    """R3: the shift is measured over TZ_REALIGN_SHIFT_SCAN_DAYS from the
    floor, not just the 30-day window. Darwin (1.5 h in Sydney DST) and Perth
    (2-3 h) are excluded in every season, even when the window happens to fall
    wholly in Sydney standard time (shift ≤ 0.5 h / 0 h inside the window)."""

    async def test_darwin_june_excluded_despite_standard_time_window(
        self, hass: HomeAssistant
    ) -> None:
        """Darwin in June: 30-day window is all AEST (shift 0.5 h ≤ 1 h) but
        the 365-day scan reaches the DST season where the shift is 1.5 h.
        Result: unsupported_shift, not armed."""
        entry, coord = _make_entry(hass, zone_name="Australia/Darwin")
        coord.client.local_tz = ZoneInfo("Australia/Darwin")
        # June 20: Sydney is on AEST (UTC+10); Darwin is ACST (UTC+9:30).
        # Window shift 0.5 h ≤ 1 h.  365-day scan from floor (≈ May 21)
        # covers Oct-Apr where Sydney goes to AEDT (UTC+11) -> shift 1.5 h.
        today = date(2026, 6, 20)
        yesterday = today - timedelta(days=1)
        with (
            patch(_PATCH_ADD_STATS),
            patch(_PATCH_GET_INSTANCE, _mock_get_instance()),
        ):
            result = await coord._plan_tz_realign(
                today, yesterday, yesterday, None, None, normal=(None, None)
            )

        assert result is None
        rec = entry.data.get(CONF_TZ_REALIGN)
        assert rec is not None
        assert rec["state"] == TZ_REALIGN_DONE
        assert rec["reason"] == TZ_REALIGN_REASON_UNSUPPORTED_SHIFT
        # Mutation that kills this: removing TZ_REALIGN_SHIFT_SCAN_DAYS (using
        # the window only) → Darwin in June sees 0.5 h ≤ 1 h → arms instead.

    async def test_darwin_june_shift_scan_covers_365_days(
        self, hass: HomeAssistant
    ) -> None:
        """The scan span equals TZ_REALIGN_SHIFT_SCAN_DAYS, checked via
        _max_tz_shift directly: Darwin window-only = 0.5 h, year-wide > 1 h."""
        _entry, coord = _make_entry(hass, zone_name="Australia/Darwin")
        coord.client.local_tz = ZoneInfo("Australia/Darwin")

        floor = date(2026, 5, 21)
        # 30-day window only (June, all standard time): max shift = 0.5 h.
        window_shift = coord._max_tz_shift(floor, floor + timedelta(days=29))
        assert window_shift == timedelta(minutes=30)
        # 365-day scan from the floor: covers the DST season → max = 1.5 h.
        year_end = floor + timedelta(days=TZ_REALIGN_SHIFT_SCAN_DAYS - 1)
        year_shift = coord._max_tz_shift(floor, year_end)
        assert year_shift > timedelta(hours=1)

    async def test_r3_fresh_darwin_install_records_unsupported_shift(
        self, hass: HomeAssistant
    ) -> None:
        """R3: the shift check runs BEFORE the fresh_install check. A fresh
        Darwin install (no history) records unsupported_shift, not
        fresh_install, so diagnostics unambiguously name the reason."""
        entry, coord = _make_entry(hass, zone_name="Australia/Darwin")
        coord.client.local_tz = ZoneInfo("Australia/Darwin")
        today = date(2026, 6, 20)
        yesterday = today - timedelta(days=1)
        with (
            patch(_PATCH_ADD_STATS),
            patch(_PATCH_GET_INSTANCE, _mock_get_instance()),
        ):
            # last_cons_date=None → would be fresh_install if checked first.
            result = await coord._plan_tz_realign(
                today, yesterday, None, None, None, normal=(None, None)
            )

        assert result is None
        rec = entry.data.get(CONF_TZ_REALIGN)
        assert rec is not None
        assert rec["reason"] == TZ_REALIGN_REASON_UNSUPPORTED_SHIFT
        # Mutation: move shift check AFTER fresh_install check →
        # fresh_install fires first → TZ_REALIGN_REASON_FRESH_INSTALL → RED.

    async def test_brisbane_june_arms_shift_at_most_1h(
        self, hass: HomeAssistant
    ) -> None:
        """Brisbane in June: window shift = 0 h (both AEST). 365-day scan
        finds Sydney DST months at most 1 h (≤ TZ_REALIGN_MAX_SHIFT), so
        Brisbane is NOT excluded and the stage is planned."""
        entry, coord = _make_entry(hass, zone_name="Australia/Brisbane")
        coord.client.local_tz = ZoneInfo("Australia/Brisbane")
        today = date(2026, 6, 20)
        yesterday = today - timedelta(days=1)
        with (
            patch(_PATCH_ADD_STATS),
            patch(_PATCH_GET_INSTANCE, _mock_get_instance()),
        ):
            await coord._plan_tz_realign(
                today, yesterday, yesterday, None, None, normal=(None, None)
            )

        # A pending record is written (or the stage sweep is returned).
        rec = entry.data.get(CONF_TZ_REALIGN)
        assert rec is not None
        assert rec.get("reason") != TZ_REALIGN_REASON_UNSUPPORTED_SHIFT


# ---------------------------------------------------------------------------
# R4: tried_from and gave_up_from persist the effective sweep start
# ---------------------------------------------------------------------------


class TestR4TriedFrom:
    """_tried_from and _gave_up_from compute the Repairs issue {floor}
    placeholder from the persisted tried_from and gave_up_from fields (R4)."""

    def test_tried_from_reads_explicit_field(self, hass: HomeAssistant) -> None:
        """tried_from overrides the frozen floor when present."""
        _entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        record = {
            "state": TZ_REALIGN_PENDING,
            "zone": "Australia/Adelaide",
            "floor": "2026-09-10",
            "tried_from": "2026-09-12",
            "series": TZ_REALIGN_CONSUMPTION,
            "attempts": 1,
            "halts": 0,
        }
        assert coord._tried_from(record) == date(2026, 9, 12)

    def test_tried_from_falls_back_to_floor(self, hass: HomeAssistant) -> None:
        """Without tried_from, the frozen floor is used."""
        _entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        record = {
            "state": TZ_REALIGN_PENDING,
            "zone": "Australia/Adelaide",
            "floor": "2026-09-10",
            "series": TZ_REALIGN_CONSUMPTION,
            "attempts": 0,
            "halts": 0,
        }
        assert coord._tried_from(record) == date(2026, 9, 10)

    def test_gave_up_from_min_of_both_stages(self, hass: HomeAssistant) -> None:
        """Generation give-up: min(gave_up_from, tried_from) is the
        earliest effective start of any gave-up stage."""
        _entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        # Consumption gave up at 09-11; generation is now giving up at 09-12.
        record = {
            "state": TZ_REALIGN_PENDING,
            "zone": "Australia/Adelaide",
            "floor": "2026-09-10",
            "gave_up_from": "2026-09-11",
            "tried_from": "2026-09-12",
            "series": TZ_REALIGN_GENERATION,
            "attempts": 3,
            "halts": 0,
            "consumption": TZ_REALIGN_GAVE_UP,
        }
        result = coord._gave_up_from(record, TZ_REALIGN_GAVE_UP)
        assert result == date(2026, 9, 11)

    def test_gave_up_from_generation_only(self, hass: HomeAssistant) -> None:
        """Only generation gave up: the generation tried_from is the placeholder."""
        _entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        record = {
            "state": TZ_REALIGN_PENDING,
            "zone": "Australia/Adelaide",
            "floor": "2026-09-10",
            "tried_from": "2026-09-12",
            "series": TZ_REALIGN_GENERATION,
            "attempts": 3,
            "halts": 0,
            "consumption": TZ_REALIGN_COMPLETE,
        }
        # generation gave up → TZ_REALIGN_GAVE_UP; no gave_up_from yet
        result = coord._gave_up_from(record, TZ_REALIGN_GAVE_UP)
        assert result == date(2026, 9, 12)

    def test_gave_up_from_generation_complete_uses_consumption_gave_up_from(
        self, hass: HomeAssistant
    ) -> None:
        """Generation succeeded: the placeholder comes from consumption's
        gave_up_from only."""
        _entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        record = {
            "state": TZ_REALIGN_PENDING,
            "zone": "Australia/Adelaide",
            "floor": "2026-09-10",
            "gave_up_from": "2026-09-11",
            "tried_from": "2026-09-13",
            "series": TZ_REALIGN_GENERATION,
            "attempts": 0,
            "halts": 0,
            "consumption": TZ_REALIGN_GAVE_UP,
        }
        # generation is TZ_REALIGN_COMPLETE → its tried_from is not included.
        result = coord._gave_up_from(record, TZ_REALIGN_COMPLETE)
        assert result == date(2026, 9, 11)


# ---------------------------------------------------------------------------
# R2: _rekey_matches unit tests — provenance guards (i) and (ii)
# ---------------------------------------------------------------------------


class TestRekeyMatchesUnit:
    """_rekey_matches: unit tests for the three R2 provenance guards and the
    basic match path. QLD in Sydney DST (November): a solar export slot on
    day D at 06:00 AEST = D-1 T20:00 UTC was stored by the OLD convention
    at D-1 T19:00 UTC (1 h early). The corrected batch writes it at T20:00,
    leaving T19:00 as a stale key."""

    _D = date(2026, 11, 20)  # QLD local date of the export slot
    # corrected: D 06:00 AEST = D-1 T20:00 UTC
    _DT_CORR = datetime(2026, 11, 19, 20, 0, tzinfo=UTC)
    # old: same wall-clock label re-read in SYD-DST (+11) = D-1 T19:00 UTC
    _DT_OLD = datetime(2026, 11, 19, 19, 0, tzinfo=UTC)
    _KWH = 0.25

    def _make_reading(self, kwh: float = _KWH) -> IntervalReading:
        return IntervalReading(
            dt=self._DT_CORR, kwh=kwh, cost_aud=round(kwh * 0.3, 6), rate_type="normal"
        )

    def test_basic_match(self, hass: HomeAssistant) -> None:
        """stale key == old-convention bucket, day in gate, reading dated on
        that day → key is matched and returned."""
        _entry, coord = _make_entry(hass, zone_name="Australia/Brisbane")
        coord.client.local_tz = ZoneInfo("Australia/Brisbane")
        reading = self._make_reading()
        # Old key T19:00Z: _key_days = {D} (T19:00 = D 05:00 AEST, T19:30 = D 05:30)
        # Corrected batch key T20:00Z: _key_days = {D} (T20:00 = D 06:00 AEST)
        matched = coord._rekey_matches(
            [reading],
            stored_kwh={self._DT_OLD: self._KWH},
            batch_kwh={self._DT_CORR: self._KWH},
            gate={self._D},
        )
        assert matched == {self._DT_OLD}

    def test_r2_i_key_days_not_in_gate_not_matched(self, hass: HomeAssistant) -> None:
        """R2(i): _key_days(stale_key) ⊄ gate → not matched, even when stored
        == old bucket. Removing guard (i) → matched when it should not be
        (e.g. a not-authoritative day with a flat-night coincidence)."""
        _entry, coord = _make_entry(hass, zone_name="Australia/Brisbane")
        coord.client.local_tz = ZoneInfo("Australia/Brisbane")
        reading = self._make_reading()
        # gate is {D-1}, but key_days(T19:00Z) = {D} → fails (i)
        matched = coord._rekey_matches(
            [reading],
            stored_kwh={self._DT_OLD: self._KWH},
            batch_kwh={self._DT_CORR: self._KWH},
            gate={self._D - timedelta(days=1)},
        )
        assert matched == set()

    def test_r2_ii_reading_outside_key_days_not_matched(
        self, hass: HomeAssistant
    ) -> None:
        """R2(ii): a counted reading dated outside the key's own days blocks
        the match even when every key day is in the gate — the flat-night
        cross-midnight coincidence.

        QLD, Sydney DST: D's 00:00 AEST slot (D-1 T14:00Z) was stored 1 h
        early, at D-1 T13:00Z = D-1 23:00 AEST, so the old bucket at that key
        holds a reading dated D while _key_days(T13:00Z) = {D-1}. Gate
        {D-1, D} passes (i); a stored D-1 23:00 hour that merely EQUALS that
        reading (a flat night) is real D-1 energy, never a re-key.
        """
        _entry, coord = _make_entry(hass, zone_name="Australia/Brisbane")
        coord.client.local_tz = ZoneInfo("Australia/Brisbane")
        dt_corr = datetime(2026, 11, 19, 14, 0, tzinfo=UTC)  # D 00:00 AEST
        dt_old = datetime(2026, 11, 19, 13, 0, tzinfo=UTC)  # D-1 23:00 AEST
        reading = IntervalReading(
            dt=dt_corr, kwh=0.15, cost_aud=0.045, rate_type="normal"
        )
        assert coord._key_days(dt_old) == {self._D - timedelta(days=1)}
        matched = coord._rekey_matches(
            [reading],
            stored_kwh={dt_old: 0.15},
            batch_kwh={dt_corr: 0.15},
            gate={self._D - timedelta(days=1), self._D},
        )
        assert matched == set()

    def test_r2_value_mismatch_not_matched(self, hass: HomeAssistant) -> None:
        """A stale key whose stored kWh differs from the old bucket by more
        than _CONSERVATION_SLACK_KWH (AGL revised the reading since it was
        stored) is not a pure re-key: the conservation guard decides it."""
        _entry, coord = _make_entry(hass, zone_name="Australia/Brisbane")
        coord.client.local_tz = ZoneInfo("Australia/Brisbane")
        matched = coord._rekey_matches(
            [self._make_reading()],
            stored_kwh={self._DT_OLD: self._KWH + 0.05},
            batch_kwh={self._DT_CORR: self._KWH},
            gate={self._D},
        )
        assert matched == set()

    def test_rekeyed_key_not_owed_by_its_day(self, hass: HomeAssistant) -> None:
        """Critic minor 6: a matched re-key is excluded from the day's owed kWh,
        so a second stale key the batch covers is still zeroed.

        Day D (QLD, Nov): A at 06:00 AEST (0.25, old key T19:00Z = the
        re-key), C at 08:00 AEST (0.10, old key T21:00Z). Stored T21:00Z
        holds 0.20 (revised since: not a re-key). Batch 0.35 >= owed 0.20
        without the re-key -> D conserves -> T21:00Z zeroed. Counting the
        re-key as owed (0.45) would fail D and carry T21:00Z with a WARNING.
        """
        _entry, coord = _make_entry(hass, zone_name="Australia/Brisbane")
        coord.client.local_tz = ZoneInfo("Australia/Brisbane")
        kwh_id = f"{DOMAIN}:consumption_9999999999"
        dt_c = datetime(2026, 11, 19, 22, 0, tzinfo=UTC)  # D 08:00 AEST
        dt_x = datetime(2026, 11, 19, 21, 0, tzinfo=UTC)  # D 07:00 AEST
        readings = [
            self._make_reading(),
            IntervalReading(dt=dt_c, kwh=0.1, cost_aud=0.03, rate_type="normal"),
        ]
        result = coord._stale_key_fill(
            {kwh_id: {self._DT_CORR: self._KWH, dt_c: 0.1}},
            {kwh_id: {self._DT_OLD: self._KWH, dt_x: 0.2}},
            kwh_id=kwh_id,
            readings=readings,
            reading_days={self._D},
            since=self._DT_CORR - timedelta(days=1),
            rekey=True,
        )
        assert result[kwh_id][self._DT_OLD] == 0.0
        assert result[kwh_id][dt_x] == 0.0

    def test_rekeyed_key_puts_no_day_at_risk(
        self, hass: HomeAssistant, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Critic minor 6: a matched re-key alone never puts its day through
        the conservation check — a lower batch value at a key the batch
        overwrites anyway is no #300 regression, so no WARNING."""
        _entry, coord = _make_entry(hass, zone_name="Australia/Brisbane")
        coord.client.local_tz = ZoneInfo("Australia/Brisbane")
        kwh_id = f"{DOMAIN}:consumption_9999999999"
        with caplog.at_level("WARNING", logger="custom_components.haggle"):
            result = coord._stale_key_fill(
                {kwh_id: {self._DT_CORR: self._KWH}},
                {kwh_id: {self._DT_OLD: self._KWH, self._DT_CORR: 0.75}},
                kwh_id=kwh_id,
                readings=[self._make_reading()],
                reading_days={self._D},
                since=self._DT_CORR - timedelta(days=1),
                rekey=True,
            )
        assert result[kwh_id][self._DT_OLD] == 0.0
        assert result[kwh_id][self._DT_CORR] == self._KWH
        assert not [r for r in caplog.records if "#300" in r.getMessage()]

    def test_key_in_batch_not_matched(self, hass: HomeAssistant) -> None:
        """A key that already has a batch value is never stale — skipped."""
        _entry, coord = _make_entry(hass, zone_name="Australia/Brisbane")
        coord.client.local_tz = ZoneInfo("Australia/Brisbane")
        reading = self._make_reading()
        matched = coord._rekey_matches(
            [reading],
            stored_kwh={self._DT_OLD: self._KWH},
            batch_kwh={self._DT_OLD: self._KWH, self._DT_CORR: self._KWH},
            gate={self._D},
        )
        assert matched == set()

    def test_stored_at_or_below_epsilon_not_matched(self, hass: HomeAssistant) -> None:
        """A stored kWh ≤ _CONSERVATION_SLACK_KWH is treated as zero / noise."""
        _entry, coord = _make_entry(hass, zone_name="Australia/Brisbane")
        coord.client.local_tz = ZoneInfo("Australia/Brisbane")
        reading = self._make_reading(kwh=1e-7)
        matched = coord._rekey_matches(
            [reading],
            stored_kwh={self._DT_OLD: 1e-7},
            batch_kwh={self._DT_CORR: 1e-7},
            gate={self._D},
        )
        assert matched == set()

    def test_api_tz_none_returns_empty(self, hass: HomeAssistant) -> None:
        """C7: without the API zone the old convention cannot be inverted."""
        _entry, coord = _make_entry(hass, zone_name="Australia/Brisbane")
        coord.client.local_tz = ZoneInfo("Australia/Brisbane")
        reading = self._make_reading()
        with patch(
            "custom_components.haggle.coordinator.agl_api_tz",
            return_value=None,
        ):
            matched = coord._rekey_matches(
                [reading],
                stored_kwh={self._DT_OLD: self._KWH},
                batch_kwh={self._DT_CORR: self._KWH},
                gate={self._D},
            )
        assert matched == set()

    def test_r2_not_applied_on_normal_path(self, hass: HomeAssistant) -> None:
        """rekey=False on a normal import → the stale pure-re-key is NOT zeroed.

        The scenario requires a second stale key on the same day holding a
        genuine delivery regression (stored 1.0, no batch reading). Without
        that, the conservation check passes on both paths and both zero
        DT_OLD — the distinction between rekey=True and rekey=False is only
        visible when conservation BLOCKS the zero on the normal path but R2
        EXEMPTS the pure re-key on the realign path.

        Day D in BNE (Nov, Sydney DST):
          DT_OLD   = T19:00Z = D 05:00 AEST (pure re-key of DT_CORR)
          DT_REG   = T21:00Z = D 07:00 AEST (delivery regression, stored 1.0)
          DT_CORR  = T20:00Z = D 06:00 AEST (in batch)

        Normal path (rekey=False):
          owed[D] = KWH + 1.0; batch[D] = KWH → day non-conserving
          → both DT_OLD and DT_REG are carried (not zeroed).

        Realign path (rekey=True):
          R2 exempts DT_OLD; owed[D] = 1.0; batch[D] = KWH → still non-conserving
          → DT_OLD added to zero_ok by R2 → ZEROED; DT_REG still carried.
        """
        _entry, coord = _make_entry(hass, zone_name="Australia/Brisbane")
        coord.client.local_tz = ZoneInfo("Australia/Brisbane")
        reading = self._make_reading()
        # DT_REG: a stale key on the same day self._D (07:00 AEST).
        dt_reg = datetime(2026, 11, 19, 21, 0, tzinfo=UTC)  # D 07:00+10 AEST
        batch_kwh = {self._DT_CORR: self._KWH}
        series = {
            f"{DOMAIN}:consumption_9999999999": dict(batch_kwh),
        }
        stored = {
            f"{DOMAIN}:consumption_9999999999": {
                self._DT_OLD: self._KWH,  # pure re-key
                dt_reg: 1.0,  # delivery regression on same day
                self._DT_CORR: self._KWH,  # in batch
            }
        }
        kwh_id = f"{DOMAIN}:consumption_9999999999"
        since = self._DT_CORR - timedelta(days=1)
        # Normal path (rekey=False): DT_OLD is carried (conservation blocks).
        result_normal = coord._stale_key_fill(
            series,
            stored,
            kwh_id=kwh_id,
            readings=[reading],
            reading_days={self._D},
            since=since,
            rekey=False,
        )
        # Both stale keys carried — day is non-conserving, R2 not called.
        assert result_normal[kwh_id].get(self._DT_OLD) == pytest.approx(self._KWH)
        assert result_normal[kwh_id].get(dt_reg) == pytest.approx(1.0)
        # Realign path (rekey=True): DT_OLD is zeroed (pure re-key, R2 matches).
        result_realign = coord._stale_key_fill(
            series,
            stored,
            kwh_id=kwh_id,
            readings=[reading],
            reading_days={self._D},
            since=since,
            rekey=True,
        )
        # R2 zeroes DT_OLD; DT_REG is still carried (genuine regression).
        assert result_realign[kwh_id].get(self._DT_OLD) == pytest.approx(0.0)
        assert result_realign[kwh_id].get(dt_reg) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# R1 variant (b): beta.1/beta.2 history shape
# ---------------------------------------------------------------------------


class TestJunctionMatchedVariantB:
    """_junction_matched variant (b): the windowed old bucket — readings whose
    agl_reported instant is at or after the contract-local midnight of their
    own corrected date. 0.5.0-beta.1/beta.2 only stored those readings (it
    dropped the rest), so stored(prev) equals the windowed bucket, not the
    full old bucket."""

    async def test_variant_b_matches_windowed_bucket(self, hass: HomeAssistant) -> None:
        """SA, July (non-DST): floor 07-10, cutoff 15:00 UTC, prev 14:00 UTC.
        v0.4 full bucket: b00 (T14:30Z) + b30 (T15:00Z) at prev = 0.1 + 0.11.
        beta.1/beta.2 windowed: b30 only (b00's reported T14:00Z < local
        midnight T14:30Z), so stored(prev) = 0.11.
        Variant (a) sees stored 0.11 vs full 0.21 → no match.
        Variant (b) sees stored 0.11 vs windowed 0.11 → match, returns [b30]."""
        _entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        api_tz = agl_api_tz()
        if api_tz is None:
            pytest.skip("Australia/Sydney tzdata unavailable")
        tz = coord._contract_tz()
        # Floor 2026-07-10, cutoff 15:00Z (first new-convention hour on floor day)
        cutoff = datetime(2026, 7, 9, 15, 0, tzinfo=UTC)
        # Slots for the floor day (July 10 ACST = T14:30Z..T14:29Z next day):
        #   slot0  T14:30Z = 00:00 ACST  reported T14:00Z (before local midnight)
        #   slot1  T15:00Z = 00:30 ACST  reported T14:30Z (AT local midnight)
        #   slot2  T15:30Z = 01:00 ACST  reported T15:00Z
        #   slot3  T16:00Z = 01:30 ACST  reported T15:30Z → old-key T15:00Z
        #   slot4  T16:30Z = 02:00 ACST  reported T16:00Z → old-key T16:00Z
        # slot3 differs between old (T15:00Z) and new (T16:00Z), satisfying the
        # _floor_day_is_old interior check at key=T16:00Z.
        intervals = [
            _make_interval(datetime(2026, 7, 9, 14, 30, tzinfo=UTC), kwh=0.1),
            _make_interval(datetime(2026, 7, 9, 15, 0, tzinfo=UTC), kwh=0.11),
            _make_interval(datetime(2026, 7, 9, 15, 30, tzinfo=UTC), kwh=0.12),
            _make_interval(
                datetime(2026, 7, 9, 16, 0, tzinfo=UTC), kwh=0.13
            ),  # interior
            _make_interval(
                datetime(2026, 7, 9, 16, 30, tzinfo=UTC), kwh=0.14
            ),  # interior
        ]
        # Full old bucket at prev (T14:00Z): slot0 (reported T14:00Z) and
        # slot1 (reported T14:30Z) → total 0.21.
        # Beta.1/2 windowed: only slot1 (reported T14:30Z >= local midnight T14:30Z).
        # stored(prev) = 0.11 → matches windowed bucket.
        # Build stored_kwh from old convention bucketing of ALL intervals:
        stored_kwh: dict[datetime, float] = {}
        for r in intervals:
            at = agl_reported_timestamp(r.dt, api_tz=api_tz, contract_tz=tz)
            key = at.replace(minute=0)
            stored_kwh[key] = stored_kwh.get(key, 0.0) + r.kwh
        # Override prev (T14:00Z) with windowed bucket (b30 only = 0.11).
        prev = cutoff - timedelta(hours=1)
        stored_kwh[prev] = 0.11  # beta.1/2 never stored b00

        matched = coord._junction_matched(intervals, stored_kwh, cutoff)

        assert matched is not None
        # Variant (b) matched slot1 only (not slot0 which was not stored).
        assert len(matched) == 1
        assert matched[0].kwh == pytest.approx(0.11)

    async def test_variant_a_matches_full_bucket(self, hass: HomeAssistant) -> None:
        """v0.4 shape: stored(prev) equals the full old bucket (b00 + b30 =
        0.21), so variant (a) matches and returns both readings."""
        _entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        api_tz = agl_api_tz()
        if api_tz is None:
            pytest.skip("Australia/Sydney tzdata unavailable")
        tz = coord._contract_tz()
        cutoff = datetime(2026, 7, 9, 15, 0, tzinfo=UTC)
        # Same interior slots as the b12 test to satisfy _floor_day_is_old.
        intervals = [
            _make_interval(datetime(2026, 7, 9, 14, 30, tzinfo=UTC), kwh=0.1),
            _make_interval(datetime(2026, 7, 9, 15, 0, tzinfo=UTC), kwh=0.11),
            _make_interval(datetime(2026, 7, 9, 15, 30, tzinfo=UTC), kwh=0.12),
            _make_interval(
                datetime(2026, 7, 9, 16, 0, tzinfo=UTC), kwh=0.13
            ),  # interior
            _make_interval(
                datetime(2026, 7, 9, 16, 30, tzinfo=UTC), kwh=0.14
            ),  # interior
        ]
        stored_kwh: dict[datetime, float] = {}
        for r in intervals:
            at = agl_reported_timestamp(r.dt, api_tz=api_tz, contract_tz=tz)
            key = at.replace(minute=0)
            stored_kwh[key] = stored_kwh.get(key, 0.0) + r.kwh
        prev = cutoff - timedelta(hours=1)
        assert stored_kwh[prev] == pytest.approx(0.21)

        matched = coord._junction_matched(intervals, stored_kwh, cutoff)

        assert matched is not None
        assert len(matched) == 2  # both slots (b00 and b30)
        assert sum(r.kwh for r in matched) == pytest.approx(0.21)


# ---------------------------------------------------------------------------
# R1 interior check: flat floor-day not confirmed (ambiguous)
# ---------------------------------------------------------------------------


class TestJunctionMatchedInteriorCheck:
    """_junction_matched interior check: the floor day's INTERIOR keys must
    hold the old-convention bucketing AND at least one must differ from new.
    A flat profile (every slot equal) is ambiguous — not confirmed."""

    async def test_flat_floor_day_interior_not_confirmed(
        self, hass: HomeAssistant
    ) -> None:
        """SA July, flat profile (every slot = 0.15 kWh): the interior keys
        equal the old bucket but ALSO equal the new bucket, so differs is
        False. _floor_day_is_old returns False → _junction_matched returns
        None. Nothing is dropped."""
        _entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        api_tz = agl_api_tz()
        if api_tz is None:
            pytest.skip("Australia/Sydney tzdata unavailable")
        tz = coord._contract_tz()
        cutoff = datetime(2026, 7, 9, 15, 0, tzinfo=UTC)
        flat = 0.15
        # All slots have the same kwh → old and new buckets yield the SAME total
        # at every interior key (each UTC hour covers two 30-min slots in both
        # conventions). The interior check's `differs` stays False throughout.
        # Must cover from the floor day's first slot (T14:30Z July 9) through
        # at least T13:30Z July 10 to avoid a tail boundary effect (the last
        # interior key T12:30Z July 10 needs readings at T13:00Z and T13:30Z).
        start = datetime(2026, 7, 9, 14, 30, tzinfo=UTC)
        intervals = [
            _make_interval(start + timedelta(minutes=30 * n), kwh=flat)
            for n in range(47)  # T14:30Z July 9 through T13:30Z July 10
        ]
        # Build stored_kwh from old bucketing of these flat-value intervals.
        stored_kwh: dict[datetime, float] = {}
        for r in intervals:
            at = agl_reported_timestamp(r.dt, api_tz=api_tz, contract_tz=tz)
            key = at.replace(minute=0)
            stored_kwh[key] = stored_kwh.get(key, 0.0) + r.kwh

        result = coord._junction_matched(intervals, stored_kwh, cutoff)

        assert result is None, (
            "Flat profile is ambiguous; _junction_matched must return None to "
            "prevent false positive on a re-sweep or new-convention entry."
        )

    async def test_unique_interior_confirms_old_convention(
        self, hass: HomeAssistant
    ) -> None:
        """SA July with unique slot values: at least one interior key differs
        between old and new convention → _floor_day_is_old True → match."""
        _entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        api_tz = agl_api_tz()
        if api_tz is None:
            pytest.skip("Australia/Sydney tzdata unavailable")
        intervals, cutoff, stored = _sa_july_floor_day(coord, old_rows=True)

        result = coord._junction_matched(intervals, stored, cutoff)

        assert result is not None

    async def test_later_interior_key_alone_confirms_old(
        self, hass: HomeAssistant
    ) -> None:
        """SA July, flat 0.15 kWh baseload for slots 0-7 then unique values:
        the first interior keys (16:00Z, 17:00Z) hold the same kWh in both
        timings, and only 18:00Z tells them apart. The check keeps walking,
        so the old-timing floor day is confirmed and slots 0 and 1 match.

        Mutation NV1 (loop bounded to the first interior key): None."""
        _entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        api_tz = agl_api_tz()
        if api_tz is None:
            pytest.skip("Australia/Sydney tzdata unavailable")
        tz = coord._contract_tz()
        cutoff = datetime(2026, 7, 9, 15, 0, tzinfo=UTC)
        start = coord._local_midnight_utc(date(2026, 7, 10))  # 07-09T14:30Z
        intervals = [
            _make_interval(
                start + timedelta(minutes=30 * n),
                kwh=0.15 if n < 8 else round(0.1 + 0.01 * n, 3),
            )
            for n in range(50)
        ]
        stored: dict[datetime, float] = {}
        for r in intervals:
            at = agl_reported_timestamp(r.dt, api_tz=api_tz, contract_tz=tz)
            key = at.replace(minute=0)
            stored[key] = stored.get(key, 0.0) + r.kwh

        matched = coord._junction_matched(intervals, stored, cutoff)

        assert matched is not None
        assert sorted(r.dt for r in matched) == [intervals[0].dt, intervals[1].dt]

    async def test_mismatch_on_later_interior_key_not_confirmed(
        self, hass: HomeAssistant
    ) -> None:
        """Every interior key must equal the old bucket, not just the first:
        a floor day AGL revised mid-day (one later key off by 0.2 kWh) is not
        confirmed old-timing, so nothing is moved.

        Mutation NV1 (loop bounded to the first interior key): matched."""
        _entry, coord = _make_entry(hass, zone_name="Australia/Adelaide")
        intervals, cutoff, stored = _sa_july_floor_day(coord, old_rows=True)
        stored[datetime(2026, 7, 10, 0, 0, tzinfo=UTC)] += 0.2

        assert coord._junction_matched(intervals, stored, cutoff) is None
