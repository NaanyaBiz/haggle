"""Tests for the haggle diagnostics platform.

The leak tests are the gate: diagnostics files get attached to public
GitHub issues, so the refresh token, account number, contract number, and
SPKI pin values must never appear anywhere in the serialized output.
"""

from __future__ import annotations

import json
import pathlib
from datetime import date
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

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


def _patched_last_stat():
    return patch.object(
        HaggleCoordinator,
        "_get_last_stat",
        new=AsyncMock(return_value=(1234.568, date(2026, 7, 4))),
    )


class TestDiagnosticsLeaks:
    async def test_no_sensitive_values_anywhere(self, hass: HomeAssistant) -> None:
        """Token, account, contract, and SPKI values must never serialize."""
        entry = await _make_entry(hass, has_solar=True)
        with _patched_last_stat():
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
        with _patched_last_stat():
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
        with _patched_last_stat():
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
        with _patched_last_stat():
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
        with _patched_last_stat():
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
        with _patched_last_stat():
            result = await async_get_config_entry_diagnostics(hass, entry)
        assert result["entry"]["data"][CONF_REFRESH_TOKEN] == "**REDACTED**"

    async def test_statistics_block_shape(self, hass: HomeAssistant) -> None:
        entry = await _make_entry(hass)
        with _patched_last_stat():
            result = await async_get_config_entry_diagnostics(hass, entry)

        contract_ref = result["contract_ref"]
        series = result["statistics"][f"{DOMAIN}:consumption_{contract_ref}"]
        assert series == {"last_date": "2026-07-04", "last_sum": 1234.568}
        # Per-band series present for both stored bands (consumption + cost).
        assert f"{DOMAIN}:consumption_peak_{contract_ref}" in result["statistics"]
        assert f"{DOMAIN}:cost_offpeak_{contract_ref}" in result["statistics"]

    async def test_no_generation_series_without_solar(
        self, hass: HomeAssistant
    ) -> None:
        entry = await _make_entry(hass, has_solar=False)
        with _patched_last_stat():
            result = await async_get_config_entry_diagnostics(hass, entry)

        assert not any("generation" in k for k in result["statistics"])
        assert result["coordinator"]["has_solar"] is False

    async def test_generation_series_with_solar(self, hass: HomeAssistant) -> None:
        entry = await _make_entry(hass, has_solar=True)
        with _patched_last_stat():
            result = await async_get_config_entry_diagnostics(hass, entry)

        contract_ref = result["contract_ref"]
        assert f"{DOMAIN}:generation_{contract_ref}" in result["statistics"]
        assert f"{DOMAIN}:generation_credit_{contract_ref}" in result["statistics"]
        assert result["coordinator"]["has_solar"] is True
