"""Sensor entities for haggle.

Entity design per AGL-API-FINDINGS.md section 4.

Cumulative consumption / cost use import_statistics() to feed historical
data into the HA Energy dashboard (data is always for past intervals; a
live state sensor would attribute everything to "now"). Entities backed
by live coordinator data use the standard CoordinatorEntity pattern.

state_class choices:
  - TOTAL_INCREASING for cumulative kWh / cost (monotonic, HA tracks resets)
  - TOTAL for period / today totals (reset at known boundary)
  - unset on MONETARY one-shots (forecast / rate); MEASUREMENT is invalid there
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DATA_BILL_PROJECTION,
    DATA_CONSUMPTION_COST,
    DATA_CONSUMPTION_KWH,
    DATA_CONSUMPTION_PERIOD,
    DATA_GENERATION_CREDIT,
    DATA_GENERATION_KWH,
    DATA_SUPPLY_CHARGE,
    DATA_UNIT_RATE,
    DATA_UNIT_RATE_OFFPEAK,
    DATA_UNIT_RATE_PEAK,
    DATA_UNIT_RATE_SHOULDER,
    DOMAIN,
    TARIFF_OFFPEAK,
    TARIFF_PEAK,
    TARIFF_SHOULDER,
)
from .coordinator import HaggleCoordinator

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import HaggleConfigEntry

SENSOR_DESCRIPTIONS: tuple[SensorEntityDescription, ...] = (
    # --- Energy dashboard sensor (total_increasing) ---
    # The cumulative kWh total (all-time from start of integration) is fed
    # via import_statistics(); this entity reflects the latest known value.
    SensorEntityDescription(
        key=DATA_CONSUMPTION_KWH,
        translation_key="consumption",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=3,
    ),
    # --- Sub-period sensors (reset at billing boundary) ---
    SensorEntityDescription(
        key=DATA_CONSUMPTION_PERIOD,
        translation_key="consumption_period",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
    ),
    SensorEntityDescription(
        key=DATA_CONSUMPTION_COST,
        translation_key="consumption_cost",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement="AUD",
        suggested_display_precision=2,
    ),
    # --- Forecast (monetary, no state_class) ---
    # MONETARY device_class accepts only state_class None or TOTAL. The bill
    # projection is a one-shot forecast (not a cumulative total), so unset.
    SensorEntityDescription(
        key=DATA_BILL_PROJECTION,
        translation_key="bill_projection",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="AUD",
    ),
    # --- Rates (instantaneous prices) ---
    # NOT MONETARY — that device_class is for cumulative amounts ($87.38 of
    # cost so far this period), not unit prices. Keep state_class=MEASUREMENT
    # so HA's recorder tracks min/mean/max in long-term statistics. Removing
    # `device_class` loses the $-chip in the entity card UI; the unit string
    # ("AUD/kWh", "AUD/day") still makes the meaning clear.
    SensorEntityDescription(
        key=DATA_UNIT_RATE,
        translation_key="unit_rate",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="AUD/kWh",
    ),
    SensorEntityDescription(
        key=DATA_SUPPLY_CHARGE,
        translation_key="supply_charge",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="AUD/day",
    ),
)

# Per-tariff unit-rate sensors, keyed by ToU band. Registered only when the
# contract is on a Time-of-Use plan (the band is in coordinator.data
# .active_tariffs) so flat-rate users never see empty peak/offpeak/shoulder
# sensors. Same pattern as `unit_rate`: MEASUREMENT, AUD/kWh, NO device_class
# (MONETARY is for cumulative amounts, not unit prices).
# Solar feed-in sensors, registered only when the contract reports hasSolar
# (coordinator.data.has_solar) so non-solar users never see empty generation
# sensors. Cumulative values are fed via import_statistics like consumption;
# these entities mirror the latest known sums.
SOLAR_DESCRIPTIONS: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key=DATA_GENERATION_KWH,
        translation_key="generation",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=3,
    ),
    # Cumulative AUD credited — a running monetary total, so MONETARY + TOTAL
    # (the one valid state_class pairing for cumulative money).
    SensorEntityDescription(
        key=DATA_GENERATION_CREDIT,
        translation_key="generation_credit",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement="AUD",
        suggested_display_precision=2,
    ),
)

TOU_RATE_DESCRIPTIONS: dict[str, SensorEntityDescription] = {
    TARIFF_PEAK: SensorEntityDescription(
        key=DATA_UNIT_RATE_PEAK,
        translation_key="unit_rate_peak",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="AUD/kWh",
    ),
    TARIFF_OFFPEAK: SensorEntityDescription(
        key=DATA_UNIT_RATE_OFFPEAK,
        translation_key="unit_rate_offpeak",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="AUD/kWh",
    ),
    TARIFF_SHOULDER: SensorEntityDescription(
        key=DATA_UNIT_RATE_SHOULDER,
        translation_key="unit_rate_shoulder",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="AUD/kWh",
    ),
}


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: HaggleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up haggle sensor entities for the entry."""
    coordinator = entry.runtime_data.coordinator
    descriptions = list(SENSOR_DESCRIPTIONS)
    # Add per-tariff rate sensors only for the bands this contract actually
    # uses. active_tariffs is populated by the coordinator's first refresh,
    # which runs before this platform setup.
    active = coordinator.data.active_tariffs if coordinator.data else frozenset()
    descriptions.extend(
        desc for band, desc in TOU_RATE_DESCRIPTIONS.items() if band in active
    )
    # Solar generation sensors only for contracts that report hasSolar.
    if coordinator.data and coordinator.data.has_solar:
        descriptions.extend(SOLAR_DESCRIPTIONS)
    async_add_entities(
        HaggleEnergySensor(coordinator, entry, desc) for desc in descriptions
    )


class HaggleEnergySensor(CoordinatorEntity[HaggleCoordinator], SensorEntity):
    """A sensor backed by the haggle coordinator."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: HaggleCoordinator,
        entry: HaggleConfigEntry,
        description: SensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        # `manufacturer` and `model` here drive HA's "Service info" card.
        # This is an unofficial third-party integration — AGL Energy did not
        # write, sanction, or endorse it. Don't put "AGL" in `manufacturer`
        # even with a qualifier; the surface is too easily mistaken for an
        # official AGL product.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Haggle",
            model="AGL smart-meter (unofficial integration)",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def native_value(self) -> float | None:
        """Return the current sensor value from coordinator data."""
        value = getattr(self.coordinator.data, self.entity_description.key)
        return float(value) if value is not None else None
