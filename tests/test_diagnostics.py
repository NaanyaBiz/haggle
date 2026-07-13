"""Tests for the haggle diagnostics platform.

The leak tests are the gate: diagnostics files get attached to public
GitHub issues, so the refresh token, account number, contract number, and
SPKI pin values must never appear anywhere in the serialized output.
"""

from __future__ import annotations

import json
import pathlib
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.haggle.const import (
    CONF_ACCOUNT_NUMBER,
    CONF_CONTRACT_NUMBER,
    CONF_PINNED_SPKI_AUTH,
    CONF_PINNED_SPKI_BFF,
    CONF_REFRESH_TOKEN,
    DOMAIN,
)
from custom_components.haggle.coordinator import HaggleCoordinator, HaggleData
from custom_components.haggle.diagnostics import (
    DIAGNOSTICS_SCHEMA_VERSION,
    async_get_config_entry_diagnostics,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_CONTRACT = "9999999999"
_ACCOUNT = "1234567890"
_TOKEN = "v1.supersecrettesttoken"
_SPKI_AUTH = "a" * 64
_SPKI_BFF = "b" * 64

_ENTRY_DATA = {
    CONF_REFRESH_TOKEN: _TOKEN,
    CONF_CONTRACT_NUMBER: _CONTRACT,
    CONF_ACCOUNT_NUMBER: _ACCOUNT,
    CONF_PINNED_SPKI_AUTH: _SPKI_AUTH,
    CONF_PINNED_SPKI_BFF: _SPKI_BFF,
}


def _data(*, has_solar: bool = False) -> HaggleData:
    return HaggleData(
        consumption_period_kwh=95.0,
        consumption_period_cost_aud=35.11,
        bill_projection_aud=87.3812345,
        unit_rate_aud_per_kwh=0.3,
        supply_charge_aud_per_day=1.4,
        latest_cumulative_kwh=1234.5678901,
        active_tariffs=frozenset({"peak", "offpeak"}),
        has_solar=has_solar,
        latest_generation_kwh=31.758,
        latest_generation_credit_aud=5.4,
    )


async def _make_entry(
    hass: HomeAssistant, *, has_solar: bool = False
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=_ENTRY_DATA,
        unique_id=f"{_ACCOUNT}_{_CONTRACT}",
    )
    entry.add_to_hass(hass)
    coordinator = HaggleCoordinator(hass, entry, AsyncMock(), _CONTRACT)
    coordinator.data = _data(has_solar=has_solar)
    coordinator._has_solar = has_solar
    coordinator._active_tou_bands = {"peak", "offpeak"}
    entry.runtime_data = SimpleNamespace(coordinator=coordinator)
    return entry


def _ts(d: date) -> float:
    """UTC-midnight unix timestamp, matching the recorder's `start` column."""
    return datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp()


# Two stored hours: 3 Jul and 4 Jul 2026.
_DEFAULT_ROWS = [
    {"start": _ts(date(2026, 7, 3)), "sum": 1200.0},
    {"start": _ts(date(2026, 7, 4)), "sum": 1234.5678901},
]


def _patched_stats(rows: list | None = None, *, fail: bool = False):
    """Patch the recorder query behind _series_coverage.

    get_instance / statistics_during_period are imported inside the function
    body, so the patch targets the source module — same pattern as the
    coordinator statistics tests. Every requested stat id gets `rows`
    (default: two stored hours, 3 and 4 Jul 2026).
    """
    instance = MagicMock()
    if fail:
        instance.async_add_executor_job = AsyncMock(
            side_effect=RuntimeError("recorder down")
        )
    else:
        use = _DEFAULT_ROWS if rows is None else rows

        async def _exec(func, hass_arg, start, end, ids, *rest):
            return {i: use for i in ids}

        instance.async_add_executor_job = AsyncMock(side_effect=_exec)
    return patch(
        "homeassistant.helpers.recorder.get_instance",
        MagicMock(return_value=instance),
    )


class TestDiagnosticsLeaks:
    async def test_no_sensitive_values_anywhere(self, hass: HomeAssistant) -> None:
        """Token, account, contract, and SPKI values must never serialize."""
        entry = await _make_entry(hass, has_solar=True)
        with _patched_stats():
            result = await async_get_config_entry_diagnostics(hass, entry)

        blob = json.dumps(result)
        assert _TOKEN not in blob
        assert _CONTRACT not in blob
        assert _ACCOUNT not in blob
        assert _SPKI_AUTH not in blob
        assert _SPKI_BFF not in blob

    async def test_anon_refs_are_stable_across_calls(self, hass: HomeAssistant) -> None:
        """Same install → same anon references, so repeat reports correlate."""
        entry = await _make_entry(hass)
        with _patched_stats():
            first = await async_get_config_entry_diagnostics(hass, entry)
            second = await async_get_config_entry_diagnostics(hass, entry)

        assert first["contract_ref"] == second["contract_ref"]
        assert first["account_ref"] == second["account_ref"]
        assert first["contract_ref"].startswith("anon-")
        assert first["contract_ref"] != first["account_ref"]

    async def test_unique_id_and_stat_ids_are_scrubbed(
        self, hass: HomeAssistant
    ) -> None:
        """Identifiers hide inside composite strings — the scrub must catch them."""
        entry = await _make_entry(hass, has_solar=True)
        with _patched_stats():
            result = await async_get_config_entry_diagnostics(hass, entry)

        contract_ref = result["contract_ref"]
        account_ref = result["account_ref"]
        assert result["entry"]["unique_id"] == f"{account_ref}_{contract_ref}"
        assert f"{DOMAIN}:consumption_{contract_ref}" in result["statistics"]
        assert f"{DOMAIN}:generation_{contract_ref}" in result["statistics"]

    async def test_anon_refs_are_keyed_not_bare_hashes(
        self, hass: HomeAssistant
    ) -> None:
        """A bare sha256 of a 10-digit AGL id is brute-forceable (~2^33
        candidates); the refs must be HMAC-keyed so an attacker with the
        diagnostics file cannot enumerate identifiers offline."""
        import hashlib

        entry = await _make_entry(hass)
        with _patched_stats():
            result = await async_get_config_entry_diagnostics(hass, entry)

        bare_contract = "anon-" + hashlib.sha256(_CONTRACT.encode()).hexdigest()[:10]
        bare_account = "anon-" + hashlib.sha256(_ACCOUNT.encode()).hexdigest()[:10]
        assert result["contract_ref"] != bare_contract
        assert result["account_ref"] != bare_account


class TestDiagnosticsSetupFailed:
    async def test_reduced_payload_without_runtime_data(
        self, hass: HomeAssistant
    ) -> None:
        """Setup failures leave runtime_data unset — exactly when diagnostics
        matter; the payload degrades instead of raising."""
        entry = MockConfigEntry(
            domain=DOMAIN,
            data=_ENTRY_DATA,
            unique_id=f"{_ACCOUNT}_{_CONTRACT}",
        )
        entry.add_to_hass(hass)
        # No runtime_data assigned — as after a failed async_setup_entry.

        result = await async_get_config_entry_diagnostics(hass, entry)

        assert result["runtime_available"] is False
        assert result["coordinator"] is None
        assert result["statistics"] == {}
        # The reduced payload is still leak-safe and still useful.
        blob = json.dumps(result)
        assert _TOKEN not in blob
        assert _CONTRACT not in blob
        assert _ACCOUNT not in blob
        assert result["entry"]["pin_present_auth"] is True
        assert result["contract_ref"].startswith("anon-")


class TestDiagnosticsSchema:
    async def test_schema_and_core_blocks(self, hass: HomeAssistant) -> None:
        entry = await _make_entry(hass)
        with _patched_stats():
            result = await async_get_config_entry_diagnostics(hass, entry)

        assert result["schema_version"] == DIAGNOSTICS_SCHEMA_VERSION
        assert result["integration"]["domain"] == DOMAIN
        # Version comes from manifest.json via the HA loader.
        manifest = (
            pathlib.Path(__file__).parent.parent
            / "custom_components"
            / "haggle"
            / "manifest.json"
        )
        manifest_version = json.loads(manifest.read_text())["version"]
        assert result["integration"]["version"] == manifest_version
        assert result["timezone"] == hass.config.time_zone
        assert result["entry"]["pin_present_auth"] is True
        assert result["entry"]["pin_present_bff"] is True
        assert result["coordinator"]["active_tou_bands"] == ["offpeak", "peak"]
        assert result["coordinator"]["data"]["consumption_period_kwh"] == 95.0
        # Floats rounded to 3 dp.
        assert result["coordinator"]["data"]["latest_cumulative_kwh"] == 1234.568

    async def test_token_redacted_not_removed(self, hass: HomeAssistant) -> None:
        """The refresh_token key stays visible as REDACTED (proves intent)."""
        entry = await _make_entry(hass)
        with _patched_stats():
            result = await async_get_config_entry_diagnostics(hass, entry)
        assert result["entry"]["data"][CONF_REFRESH_TOKEN] == "**REDACTED**"

    async def test_statistics_block_shape(self, hass: HomeAssistant) -> None:
        entry = await _make_entry(hass)
        with _patched_stats():
            result = await async_get_config_entry_diagnostics(hass, entry)

        contract_ref = result["contract_ref"]
        series = result["statistics"][f"{DOMAIN}:consumption_{contract_ref}"]
        assert series == {
            "first_date": "2026-07-03",
            "last_date": "2026-07-04",
            "row_count": 2,
            "last_sum": 1234.568,
        }
        # Per-band series present for both stored bands (consumption + cost).
        assert f"{DOMAIN}:consumption_peak_{contract_ref}" in result["statistics"]
        assert f"{DOMAIN}:cost_offpeak_{contract_ref}" in result["statistics"]

    async def test_no_generation_series_without_solar(
        self, hass: HomeAssistant
    ) -> None:
        entry = await _make_entry(hass, has_solar=False)
        with _patched_stats():
            result = await async_get_config_entry_diagnostics(hass, entry)

        assert not any("generation" in k for k in result["statistics"])
        assert result["coordinator"]["has_solar"] is False

    async def test_generation_series_with_solar(self, hass: HomeAssistant) -> None:
        entry = await _make_entry(hass, has_solar=True)
        with _patched_stats():
            result = await async_get_config_entry_diagnostics(hass, entry)

        contract_ref = result["contract_ref"]
        assert f"{DOMAIN}:generation_{contract_ref}" in result["statistics"]
        assert f"{DOMAIN}:generation_credit_{contract_ref}" in result["statistics"]
        assert result["coordinator"]["has_solar"] is True


class TestSagaFields:
    """Fields added after the #128 beta.3 round — each one exists because a
    real support thread needed it and didn't have it."""

    async def test_leading_hole_visible_in_first_date(
        self, hass: HomeAssistant
    ) -> None:
        """#128 shape: healthy last_date, missing early days — first_date is
        the field that exposes it."""
        rows = [
            {"start": _ts(date(2026, 6, 28)), "sum": 5.0},
            {"start": _ts(date(2026, 7, 4)), "sum": 39.75},
        ]
        entry = await _make_entry(hass, has_solar=True)
        with _patched_stats(rows):
            result = await async_get_config_entry_diagnostics(hass, entry)

        contract_ref = result["contract_ref"]
        series = result["statistics"][f"{DOMAIN}:generation_{contract_ref}"]
        assert series["first_date"] == "2026-06-28"  # ← the leading hole
        assert series["last_date"] == "2026-07-04"
        assert series["row_count"] == 2

    async def test_solar_heal_record_surfaced(self, hass: HomeAssistant) -> None:
        """The heal record is a first-class field and not duplicated in
        entry.data."""
        from custom_components.haggle.const import CONF_SOLAR_HEAL

        heal = {"state": "pending", "floor": "2026-06-08", "attempts": 1}
        entry = await _make_entry(hass, has_solar=True)
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_SOLAR_HEAL: heal}
        )
        with _patched_stats():
            result = await async_get_config_entry_diagnostics(hass, entry)

        assert result["solar_heal"] == heal
        assert CONF_SOLAR_HEAL not in result["entry"]["data"]

    async def test_stall_spans_surfaced(self, hass: HomeAssistant) -> None:
        """Stall give-up spans appear top-level, never under entry.data —
        they are the only durable evidence of marker-masked holes (CO-16.4)."""
        from custom_components.haggle.const import CONF_SOLAR_STALL_SPANS

        spans = [
            {
                "start": "2026-06-01",
                "end": "2026-06-07",
                "cycles": 3,
                "gave_up_at": "2026-06-10T00:00:00+00:00",
            }
        ]
        entry = await _make_entry(hass, has_solar=True)
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_SOLAR_STALL_SPANS: spans}
        )
        with _patched_stats():
            result = await async_get_config_entry_diagnostics(hass, entry)

        assert result["stall_give_up_spans"] == spans
        assert CONF_SOLAR_STALL_SPANS not in result["entry"]["data"]

    async def test_stall_spans_none_when_never(self, hass: HomeAssistant) -> None:
        entry = await _make_entry(hass)
        with _patched_stats():
            result = await async_get_config_entry_diagnostics(hass, entry)
        assert result["stall_give_up_spans"] is None

    async def test_solar_heal_none_when_never_armed(self, hass: HomeAssistant) -> None:
        entry = await _make_entry(hass)
        with _patched_stats():
            result = await async_get_config_entry_diagnostics(hass, entry)
        assert result["solar_heal"] is None

    async def test_bill_period_start_and_last_exception(
        self, hass: HomeAssistant
    ) -> None:
        entry = await _make_entry(hass)
        coordinator = entry.runtime_data.coordinator
        coordinator.last_bill_start = date(2026, 6, 24)
        coordinator.last_exception = RuntimeError("HTTP 500 from AGL BFF")
        with _patched_stats():
            result = await async_get_config_entry_diagnostics(hass, entry)

        assert result["coordinator"]["bill_period_start"] == "2026-06-24"
        assert result["coordinator"]["last_exception"] == "HTTP 500 from AGL BFF"

    async def test_recorder_failure_degrades_not_raises(
        self, hass: HomeAssistant
    ) -> None:
        """Diagnostics must produce a file even when the recorder query dies —
        a broken recorder is itself a thing worth reporting."""
        entry = await _make_entry(hass)
        with _patched_stats(fail=True):
            result = await async_get_config_entry_diagnostics(hass, entry)

        contract_ref = result["contract_ref"]
        series = result["statistics"][f"{DOMAIN}:consumption_{contract_ref}"]
        assert series == {
            "first_date": None,
            "last_date": None,
            "row_count": 0,
            "last_sum": None,
        }
