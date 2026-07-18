"""Tests for custom_components/haggle/sensor.py.

Focus: the base sensor set is always registered, and per-tariff ToU rate
sensors are registered ONLY for the bands in coordinator.data.active_tariffs
(so flat-rate contracts never get empty peak/offpeak/shoulder sensors).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.haggle.const import (
    CONF_ACCOUNT_NUMBER,
    CONF_CONTRACT_NUMBER,
    CONF_REFRESH_TOKEN,
    DATA_UNIT_RATE_OFFPEAK,
    DATA_UNIT_RATE_PEAK,
    DATA_UNIT_RATE_SHOULDER,
    DOMAIN,
)
from custom_components.haggle.coordinator import HaggleCoordinator, HaggleData
from custom_components.haggle.sensor import (
    SENSOR_DESCRIPTIONS,
    TOU_RATE_DESCRIPTIONS,
    HaggleEnergySensor,
    async_setup_entry,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_CONTRACT = "9999999999"
_BASE_KEYS = {d.key for d in SENSOR_DESCRIPTIONS}


def _data(
    active: frozenset[str], has_solar: bool = False, **rates: float | None
) -> HaggleData:
    return HaggleData(
        consumption_period_kwh=0.0,
        consumption_period_cost_aud=0.0,
        bill_projection_aud=None,
        unit_rate_aud_per_kwh=0.3,
        supply_charge_aud_per_day=1.0,
        latest_cumulative_kwh=0.0,
        active_tariffs=active,
        unit_rate_peak_aud_per_kwh=rates.get("peak"),
        unit_rate_offpeak_aud_per_kwh=rates.get("offpeak"),
        unit_rate_shoulder_aud_per_kwh=rates.get("shoulder"),
        has_solar=has_solar,
    )


def _make_entry_with_coordinator(
    hass: HomeAssistant, data: HaggleData | None
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_REFRESH_TOKEN: "v1.tok",
            CONF_CONTRACT_NUMBER: _CONTRACT,
            CONF_ACCOUNT_NUMBER: "1234567890",
        },
        unique_id="1234567890_9999999999",
    )
    entry.add_to_hass(hass)
    coordinator = HaggleCoordinator(hass, entry, AsyncMock(), _CONTRACT)
    coordinator.data = data  # type: ignore[assignment]
    entry.runtime_data = SimpleNamespace(coordinator=coordinator)
    return entry


async def _setup_keys(hass: HomeAssistant, data: HaggleData | None) -> list[str]:
    """Run async_setup_entry and return the keys of the entities it registered."""
    entry = _make_entry_with_coordinator(hass, data)
    captured: list[HaggleEnergySensor] = []

    def _add(entities) -> None:
        captured.extend(entities)

    await async_setup_entry(hass, entry, _add)  # type: ignore[arg-type]
    return [e.entity_description.key for e in captured]


class TestConditionalRegistration:
    async def test_flat_rate_registers_only_base_sensors(
        self, hass: HomeAssistant
    ) -> None:
        keys = await _setup_keys(hass, _data(frozenset()))
        assert set(keys) == _BASE_KEYS
        assert DATA_UNIT_RATE_PEAK not in keys
        assert DATA_UNIT_RATE_OFFPEAK not in keys
        assert DATA_UNIT_RATE_SHOULDER not in keys

    async def test_no_coordinator_data_registers_only_base(
        self, hass: HomeAssistant
    ) -> None:
        keys = await _setup_keys(hass, None)
        assert set(keys) == _BASE_KEYS

    async def test_two_active_bands_register_their_rate_sensors(
        self, hass: HomeAssistant
    ) -> None:
        keys = await _setup_keys(hass, _data(frozenset({"peak", "offpeak"})))
        assert DATA_UNIT_RATE_PEAK in keys
        assert DATA_UNIT_RATE_OFFPEAK in keys
        assert DATA_UNIT_RATE_SHOULDER not in keys  # shoulder inactive
        assert set(keys) >= _BASE_KEYS

    async def test_all_three_bands_register(self, hass: HomeAssistant) -> None:
        keys = await _setup_keys(
            hass, _data(frozenset({"peak", "offpeak", "shoulder"}))
        )
        assert {
            DATA_UNIT_RATE_PEAK,
            DATA_UNIT_RATE_OFFPEAK,
            DATA_UNIT_RATE_SHOULDER,
        } <= set(keys)
        assert len(keys) == len(SENSOR_DESCRIPTIONS) + 3


class TestTouRateNativeValue:
    async def test_native_value_none_when_rate_unknown(
        self, hass: HomeAssistant
    ) -> None:
        """A ToU band with no resolved rate reads unavailable (None), not 0.0."""
        entry = _make_entry_with_coordinator(
            hass, _data(frozenset({"peak"}), peak=None)
        )
        sensor = HaggleEnergySensor(
            entry.runtime_data.coordinator, entry, TOU_RATE_DESCRIPTIONS["peak"]
        )
        assert sensor.native_value is None

    async def test_native_value_returns_rate(self, hass: HomeAssistant) -> None:
        entry = _make_entry_with_coordinator(
            hass, _data(frozenset({"peak"}), peak=0.419)
        )
        sensor = HaggleEnergySensor(
            entry.runtime_data.coordinator, entry, TOU_RATE_DESCRIPTIONS["peak"]
        )
        assert sensor.native_value == 0.419


class TestSolarRegistration:
    async def test_solar_contract_registers_generation_sensors(
        self, hass: HomeAssistant
    ) -> None:
        from custom_components.haggle.const import (
            DATA_FEED_IN_RATE,
            DATA_GENERATION_CREDIT,
            DATA_GENERATION_KWH,
            DATA_GENERATION_PERIOD,
            DATA_GENERATION_PERIOD_CREDIT,
        )

        keys = await _setup_keys(hass, _data(frozenset(), has_solar=True))
        assert DATA_GENERATION_KWH in keys
        assert DATA_GENERATION_CREDIT in keys
        assert DATA_GENERATION_PERIOD in keys
        assert DATA_GENERATION_PERIOD_CREDIT in keys
        assert DATA_FEED_IN_RATE in keys
        assert set(keys) >= _BASE_KEYS

    async def test_non_solar_contract_has_no_generation_sensors(
        self, hass: HomeAssistant
    ) -> None:
        from custom_components.haggle.const import (
            DATA_FEED_IN_RATE,
            DATA_GENERATION_CREDIT,
            DATA_GENERATION_KWH,
            DATA_GENERATION_PERIOD,
            DATA_GENERATION_PERIOD_CREDIT,
        )

        keys = await _setup_keys(hass, _data(frozenset()))
        assert DATA_GENERATION_KWH not in keys
        assert DATA_GENERATION_CREDIT not in keys
        assert DATA_GENERATION_PERIOD not in keys
        assert DATA_GENERATION_PERIOD_CREDIT not in keys
        assert DATA_FEED_IN_RATE not in keys
        assert set(keys) == _BASE_KEYS


class TestSolarDescriptions:
    def test_period_sensors_mirror_consumption_period_pattern(self) -> None:
        """Bill-period totals reset at rollover: TOTAL, never TOTAL_INCREASING."""
        from homeassistant.components.sensor import (
            SensorDeviceClass,
            SensorStateClass,
        )

        from custom_components.haggle.const import (
            DATA_GENERATION_PERIOD,
            DATA_GENERATION_PERIOD_CREDIT,
        )
        from custom_components.haggle.sensor import SOLAR_DESCRIPTIONS

        by_key = {d.key: d for d in SOLAR_DESCRIPTIONS}
        period = by_key[DATA_GENERATION_PERIOD]
        assert period.state_class is SensorStateClass.TOTAL
        assert period.device_class is SensorDeviceClass.ENERGY
        credit = by_key[DATA_GENERATION_PERIOD_CREDIT]
        assert credit.state_class is SensorStateClass.TOTAL
        assert credit.device_class is SensorDeviceClass.MONETARY

    def test_feed_in_rate_is_a_unit_price_not_monetary(self) -> None:
        """Unit prices: MEASUREMENT + AUD/kWh, no device_class (repo rule)."""
        from homeassistant.components.sensor import SensorStateClass

        from custom_components.haggle.const import DATA_FEED_IN_RATE
        from custom_components.haggle.sensor import SOLAR_DESCRIPTIONS

        desc = {d.key: d for d in SOLAR_DESCRIPTIONS}[DATA_FEED_IN_RATE]
        assert desc.device_class is None
        assert desc.state_class is SensorStateClass.MEASUREMENT
        assert desc.native_unit_of_measurement == "AUD/kWh"

    def test_cumulative_kwh_sensors_are_not_energy_sources(self) -> None:
        """#147: the cumulative consumption / solar-generation totals must NOT
        carry device_class/state_class. With them, HA lists these once-per-poll
        entities as Energy-dashboard sources and attributes a whole day's kWh to
        the poll hour (wrong day, AGL lag). The real sources are the
        import_statistics() series; these entities are device-card values only.
        """
        from homeassistant.const import UnitOfEnergy

        from custom_components.haggle.const import (
            DATA_CONSUMPTION_KWH,
            DATA_GENERATION_KWH,
        )
        from custom_components.haggle.sensor import (
            SENSOR_DESCRIPTIONS,
            SOLAR_DESCRIPTIONS,
        )

        cons = {d.key: d for d in SENSOR_DESCRIPTIONS}[DATA_CONSUMPTION_KWH]
        gen = {d.key: d for d in SOLAR_DESCRIPTIONS}[DATA_GENERATION_KWH]
        for desc in (cons, gen):
            assert desc.device_class is None
            assert desc.state_class is None
            assert desc.native_unit_of_measurement == UnitOfEnergy.KILO_WATT_HOUR

    async def test_period_sensor_unknown_until_backfill_caught_up(
        self, hass: HomeAssistant
    ) -> None:
        """generation_period_kwh None → sensor unavailable, never a partial."""
        from custom_components.haggle.const import DATA_GENERATION_PERIOD
        from custom_components.haggle.sensor import SOLAR_DESCRIPTIONS

        entry = _make_entry_with_coordinator(hass, _data(frozenset(), has_solar=True))
        desc = {d.key: d for d in SOLAR_DESCRIPTIONS}[DATA_GENERATION_PERIOD]
        sensor = HaggleEnergySensor(entry.runtime_data.coordinator, entry, desc)
        assert sensor.native_value is None
