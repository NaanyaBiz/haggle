"""Coordinator-cycle tests for user-configured ToU windows (#141).

These drive the real ``_fetch_and_import`` with the AGL client and the
recorder boundary mocked — the cycle-level wiring the real-recorder tests
(test_tariff_recorder.py) do not reach: the per-cycle mode snapshot, the D6
hold/suspend bound, Repairs issues, the applied-settings history, rate
sources (AC8) and the rate-sensor reload comparator (M4).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.haggle.agl.models import (
    BillPeriod,
    Contract,
    IntervalReading,
    PlanRates,
)
from custom_components.haggle.const import (
    BACKFILL_DAYS,
    CONF_ACCOUNT_NUMBER,
    CONF_CONTRACT_NUMBER,
    CONF_REFRESH_TOKEN,
    CONF_TARIFF_BANDS_APPLIED,
    DOMAIN,
    MAX_BAND_HISTORY,
    TARIFF_TZ_HOLD_MAX_CYCLES,
)
from custom_components.haggle.coordinator import HaggleCoordinator

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_CONTRACT = "9999999999"
_BNE = ZoneInfo("Australia/Brisbane")

_WINDOWS = {
    "version": 1,
    "mode": "windows",
    "residual": "offpeak",
    "windows": [
        {"band": "peak", "start": "15:00", "end": "21:00", "days": "all"},
        {"band": "shoulder", "start": "10:00", "end": "14:00", "days": "all"},
    ],
    "rates": {"peak": 0.4417},
}
_FLAT = {"version": 1, "mode": "flat"}
_INVALID = {"version": 9, "mode": "windows"}


def _plan() -> PlanRates:
    return PlanRates(
        product_name="",
        unit_rates=[{"type": "c/kWh", "price": 30.0}],
        tou_unit_rates={"peak": 50.0, "offpeak": 20.0},
    )


def _summary() -> BillPeriod:
    today = datetime.now(UTC).date()
    return BillPeriod(
        start=today,
        end=today,
        consumption_kwh=0.0,
        cost_label="$0",
        projection_label="",
    )


def _coord(
    hass: HomeAssistant,
    options: dict | None = None,
    *,
    tz_is_contract: Any = True,
    data: dict | None = None,
    contracts: list[Contract] | None = None,
) -> HaggleCoordinator:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Home",
        data={
            CONF_REFRESH_TOKEN: "v1.t",
            CONF_CONTRACT_NUMBER: _CONTRACT,
            CONF_ACCOUNT_NUMBER: "1234567890",
            **(data or {}),
        },
        options={"tariff_bands": options} if options is not None else {},
        unique_id="1234567890_9999999999",
    )
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_usage_summary.return_value = _summary()
    client.async_get_plan.return_value = _plan()
    client.async_get_overview.return_value = contracts or []
    client.tz_is_contract = tz_is_contract
    client.local_tz = _BNE
    client.set_contract_tz = MagicMock()
    return HaggleCoordinator(hass, entry, client, _CONTRACT)


def _set_options(hass: HomeAssistant, coord: HaggleCoordinator, raw: dict | None):
    hass.config_entries.async_update_entry(
        coord.config_entry, options={"tariff_bands": raw} if raw is not None else {}
    )


@pytest.fixture(autouse=True)
def _stub_recorder_reads():
    """No recorder here: the stored-band seed and resume point are stubbed."""
    with (
        patch.object(
            HaggleCoordinator,
            "_get_stored_tou_bands",
            new_callable=AsyncMock,
            return_value=set(),
        ),
        patch.object(
            HaggleCoordinator,
            "_get_last_stat",
            new_callable=AsyncMock,
            return_value=(None, None),
        ),
    ):
        yield


def _issue(hass: HomeAssistant, key: str, coord: HaggleCoordinator):
    return ir.async_get(hass).async_get_issue(
        DOMAIN, f"{key}_{coord.config_entry.entry_id}"
    )


async def _cycle(coord: HaggleCoordinator, fetch: Any = None) -> AsyncMock:
    mock = AsyncMock(return_value=True, side_effect=fetch)
    with patch.object(coord, "_fetch_range", mock):
        coord.data = await coord._fetch_and_import()
    return mock


# --- mode table ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("options", "tz", "mode"),
    [
        (None, True, "legacy_type"),
        (None, False, "legacy_type"),
        (_WINDOWS, True, "derived"),
        (_FLAT, True, "flat"),
        (_INVALID, True, "invalid"),
        (_INVALID, False, "invalid"),
        (_WINDOWS, False, "held_tz"),
        (_FLAT, False, "held_tz"),
        # identity check: a truthy non-True value is NOT the contract zone
        (_WINDOWS, 1, "held_tz"),
        (_WINDOWS, MagicMock(), "held_tz"),
    ],
)
async def test_mode_table(hass: HomeAssistant, options, tz, mode) -> None:
    coord = _coord(hass, options, tz_is_contract=tz)
    await _cycle(coord)
    assert coord._band_plan.mode == mode
    assert coord._band_plan.ever_configured is (options is not None)
    if mode == "invalid":
        assert coord._band_plan.error == "unsupported_version"


async def test_never_configured_entry_with_history_is_ever_configured(
    hass: HomeAssistant,
) -> None:
    coord = _coord(
        hass,
        None,
        data={CONF_TARIFF_BANDS_APPLIED: [{"fingerprint": "x", "mode": "flat"}]},
    )
    await _cycle(coord)
    assert coord._band_plan.mode == "legacy_type"
    assert coord._band_plan.ever_configured is True


# --- D6 hold / suspend -----------------------------------------------------------


async def test_hold_bounded_then_aggregate_only_then_recovers(
    hass: HomeAssistant,
) -> None:
    """AC6: configured + zone unknown → the consumption import is HELD (no
    consumption range, so no request and no rows; the resume point stays
    behind) for TARIFF_TZ_HOLD_MAX_CYCLES cycles, then suspended (aggregate
    only) with a Repairs issue; once the zone resolves the bands derive
    again and the issue is deleted."""
    coord = _coord(hass, _WINDOWS, tz_is_contract=False)
    for n in range(TARIFF_TZ_HOLD_MAX_CYCLES):
        fetch = await _cycle(coord)
        assert coord._band_plan.mode == "held_tz", n
        fetch.assert_not_called()  # no consumption (and no solar) range
        assert _issue(hass, "tariff_bands_suspended", coord) is None
    fetch = await _cycle(coord)
    assert coord._band_plan.mode == "suspended_tz"
    assert fetch.call_args.args[0] is not None  # the aggregate is fetched again
    assert _issue(hass, "tariff_bands_suspended", coord) is not None

    coord.client.tz_is_contract = True
    await _cycle(coord)
    assert coord._band_plan.mode == "derived"
    assert coord._tz_hold_cycles == 0
    assert _issue(hass, "tariff_bands_suspended", coord) is None


async def test_hold_resume_point_refetches_the_held_days(hass: HomeAssistant) -> None:
    """The held cycles write nothing, so the next un-held cycle's range
    starts from the unchanged resume point (here: first install → the
    backfill floor), re-fetching every day the hold skipped."""
    coord = _coord(hass, _WINDOWS, tz_is_contract=False)
    await _cycle(coord)
    coord.client.tz_is_contract = True
    fetch = await _cycle(coord)
    today = datetime.now(UTC).date()
    assert fetch.call_args.args[0][0] == today - timedelta(days=BACKFILL_DAYS)


async def test_unparseable_address_suspends_immediately(hass: HomeAssistant) -> None:
    contract = Contract(
        contract_number=_CONTRACT,
        account_number="1234567890",
        address="PO Box 1, NOWHERE",
        fuel_type="electricityContract",
        status="active",
    )
    coord = _coord(hass, _WINDOWS, tz_is_contract=False, contracts=[contract])
    fetch = await _cycle(coord)
    assert coord._address_unparseable is True
    assert coord._band_plan.mode == "suspended_tz"
    assert fetch.call_args.args[0] is not None
    assert _issue(hass, "tariff_bands_suspended", coord) is not None


async def test_generation_is_not_held(hass: HomeAssistant) -> None:
    coord = _coord(hass, _WINDOWS, tz_is_contract=False)
    coord._has_solar = True
    with patch.object(
        coord,
        "_prepare_solar_cycle",
        new_callable=AsyncMock,
        return_value=((date(2026, 7, 1), date(2026, 7, 7)), None, None),
    ):
        fetch = await _cycle(coord)
    assert coord._band_plan.mode == "held_tz"
    assert fetch.call_args.args[0] is None
    assert fetch.call_args.args[1] == (date(2026, 7, 1), date(2026, 7, 7))


# --- Repairs -----------------------------------------------------------------


async def test_legacy_issue_only_with_band_series_and_deleted_on_configure(
    hass: HomeAssistant,
) -> None:
    coord = _coord(hass)
    await _cycle(coord)
    assert _issue(hass, "tariff_bands_legacy", coord) is None  # no bands

    coord._active_tou_bands = {"peak"}
    await _cycle(coord)
    issue = _issue(hass, "tariff_bands_legacy", coord)
    assert issue is not None
    assert issue.severity == ir.IssueSeverity.WARNING
    assert issue.is_fixable is False
    assert issue.learn_more_url.endswith("docs/energy-dashboard.md")
    await _cycle(coord)  # idempotent: still exactly one
    assert _issue(hass, "tariff_bands_legacy", coord) is not None

    _set_options(hass, coord, _FLAT)
    await _cycle(coord)
    assert _issue(hass, "tariff_bands_legacy", coord) is None


async def test_invalid_issue_created_and_deleted(hass: HomeAssistant) -> None:
    coord = _coord(hass, _INVALID)
    await _cycle(coord)
    assert _issue(hass, "tariff_bands_invalid", coord) is not None
    _set_options(hass, coord, _WINDOWS)
    await _cycle(coord)
    assert _issue(hass, "tariff_bands_invalid", coord) is None


# --- applied history ------------------------------------------------------------


def _importing(coord: HaggleCoordinator, cutoff: datetime):
    async def fetch(*_args: Any, **_kwargs: Any) -> bool:
        coord._last_cons_cutoff = cutoff
        return True

    return fetch


async def test_applied_history_once_per_fingerprint_and_bounded(
    hass: HomeAssistant,
) -> None:
    cutoff = datetime(2026, 7, 1, 14, tzinfo=UTC)
    coord = _coord(hass, _WINDOWS)
    await _cycle(coord, _importing(coord, cutoff))
    history = coord.config_entry.data[CONF_TARIFF_BANDS_APPLIED]
    assert history == [
        {
            "fingerprint": coord._band_plan.fingerprint,
            "mode": "derived",
            "from": cutoff.isoformat(),
        }
    ]
    # Same settings, and a rate-only change: no new entry.
    await _cycle(coord, _importing(coord, cutoff + timedelta(days=1)))
    _set_options(hass, coord, {**_WINDOWS, "rates": {"peak": 0.5}})
    await _cycle(coord, _importing(coord, cutoff + timedelta(days=2)))
    assert len(coord.config_entry.data[CONF_TARIFF_BANDS_APPLIED]) == 1
    # A cycle with no consumption import records nothing.
    _set_options(hass, coord, _FLAT)
    await _cycle(coord)
    assert len(coord.config_entry.data[CONF_TARIFF_BANDS_APPLIED]) == 1
    await _cycle(coord, _importing(coord, cutoff))
    assert coord.config_entry.data[CONF_TARIFF_BANDS_APPLIED][-1]["mode"] == "flat"
    # Back to legacy is recorded on an entry with history.
    _set_options(hass, coord, None)
    await _cycle(coord, _importing(coord, cutoff))
    assert coord.config_entry.data[CONF_TARIFF_BANDS_APPLIED][-1] == {
        "fingerprint": "legacy_type",
        "mode": "legacy_type",
        "from": cutoff.isoformat(),
    }
    # Bounded.
    for n in range(MAX_BAND_HISTORY + 3):
        start = f"{n % 24:02d}:00"
        end = f"{n % 24:02d}:30"
        _set_options(
            hass,
            coord,
            {**_WINDOWS, "windows": [{"band": "peak", "start": start, "end": end}]},
        )
        await _cycle(coord, _importing(coord, cutoff))
    assert len(coord.config_entry.data[CONF_TARIFF_BANDS_APPLIED]) == MAX_BAND_HISTORY


async def test_never_configured_entry_never_records_history(
    hass: HomeAssistant,
) -> None:
    coord = _coord(hass)
    await _cycle(coord, _importing(coord, datetime(2026, 7, 1, tzinfo=UTC)))
    assert CONF_TARIFF_BANDS_APPLIED not in coord.config_entry.data


# --- AC8 rate sources ------------------------------------------------------------


@pytest.mark.parametrize(
    ("options", "rates", "sources"),
    [
        (
            None,
            {"peak": 0.5, "offpeak": 0.2, "shoulder": None, "superoffpeak": None},
            {
                "peak": "plan",
                "offpeak": "plan",
                "shoulder": "none",
                "superoffpeak": "none",
            },
        ),
        (
            _WINDOWS,
            {"peak": 0.4417, "offpeak": 0.2, "shoulder": None, "superoffpeak": None},
            {
                "peak": "configured",
                "offpeak": "plan",
                "shoulder": "none",
                "superoffpeak": "none",
            },
        ),
        (
            _FLAT,
            {"peak": None, "offpeak": None, "shoulder": None, "superoffpeak": None},
            {b: "none" for b in ("peak", "offpeak", "shoulder", "superoffpeak")},
        ),
    ],
)
async def test_rate_sources(hass: HomeAssistant, options, rates, sources) -> None:
    coord = _coord(hass, options)
    await _cycle(coord)
    data = coord.data
    assert data.unit_rate_peak_aud_per_kwh == rates["peak"]
    assert data.unit_rate_offpeak_aud_per_kwh == rates["offpeak"]
    assert data.unit_rate_shoulder_aud_per_kwh == rates["shoulder"]
    assert data.unit_rate_superoffpeak_aud_per_kwh == rates["superoffpeak"]
    assert data.rate_sources == sources


# --- m3: per-cycle snapshot -------------------------------------------------------


async def test_options_saved_mid_fetch_do_not_split_the_cycle(
    hass: HomeAssistant,
) -> None:
    """m3: an Options save DURING the awaited fetch must not change that
    cycle's attribution, its rate sources or its recorded basis — the
    import, HaggleData and the history all use the snapshot."""
    coord = _coord(hass, _WINDOWS)
    added: list[str] = []
    day = date(2026, 7, 6)
    start = datetime(2026, 7, 5, 14, tzinfo=UTC)
    readings = [
        IntervalReading(start + timedelta(minutes=30 * i), 0.5, 0.1, "normal")
        for i in range(48)
    ]
    executor = MagicMock()
    executor.async_add_executor_job = AsyncMock(return_value={})

    async def fetch(*_args: Any, **_kwargs: Any) -> bool:
        _set_options(hass, coord, _FLAT)  # saved while the cycle is mid-fetch
        await coord._import_intervals(readings, reading_days={day})
        return True

    with (
        patch(
            "homeassistant.helpers.recorder.get_instance",
            return_value=executor,
        ),
        patch(
            "homeassistant.components.recorder.statistics.async_add_external_statistics",
            side_effect=lambda _h, meta, _rows: added.append(meta["statistic_id"]),
        ),
    ):
        await _cycle(coord, fetch)
    assert f"{DOMAIN}:consumption_peak_{_CONTRACT}" in added
    assert coord._band_plan.mode == "derived"
    assert coord.data.rate_sources["peak"] == "configured"
    assert coord.config_entry.data[CONF_TARIFF_BANDS_APPLIED][-1]["mode"] == "derived"
    # The NEXT cycle picks the save up.
    await _cycle(coord)
    assert coord._band_plan.mode == "flat"


# --- M4 rate-sensor reload comparator ----------------------------------------------


async def test_reload_on_any_rate_sensor_difference_for_configured(
    hass: HomeAssistant,
) -> None:
    coord = _coord(hass, _WINDOWS)
    await _cycle(coord)
    assert coord.desired_rate_sensors(set()) == (
        "configured",
        frozenset({"peak", "shoulder", "offpeak"}),
    )
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        # Registered set equal → no reload (loop-safe).
        coord.rate_bands_registered = coord.desired_rate_sensors(set())
        await _cycle(coord)
        reload.assert_not_called()
        # A configured band already in _active_tou_bands (the #224 spurious
        # peak) gives no growth — the comparator still catches the difference.
        coord.rate_bands_registered = ("legacy", frozenset({"peak"}))
        coord._active_tou_bands = {"peak"}
        coord._prev_active_tou_bands = {"peak"}
        await _cycle(coord)
        assert reload.call_count == 1
        # Removal: the user narrows to an EV window.
        coord.rate_bands_registered = coord.desired_rate_sensors(set())
        _set_options(
            hass,
            coord,
            {
                "version": 1,
                "mode": "windows",
                "residual": "normal",
                "windows": [{"band": "superoffpeak", "start": "00:00", "end": "06:00"}],
            },
        )
        await _cycle(coord)
        assert reload.call_count == 2
        # Configured → legacy kind switch.
        coord.rate_bands_registered = coord.desired_rate_sensors(set())
        _set_options(hass, coord, None)
        await _cycle(coord)
        assert reload.call_count == 3


async def test_legacy_keeps_growth_semantics(hass: HomeAssistant) -> None:
    coord = _coord(hass)
    await _cycle(coord)
    coord.rate_bands_registered = ("legacy", frozenset())
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        await _cycle(coord)
        reload.assert_not_called()
        coord._active_tou_bands = {"peak"}
        await _cycle(coord)
        assert reload.call_count == 1
        await _cycle(coord)  # no further growth → no further reload
        assert reload.call_count == 1
