"""Sensor entities for haggle.

Entity design per AGL-API-FINDINGS.md section 4.

Cumulative consumption / cost use import_statistics() to feed historical
data into the HA Energy dashboard (data is always for past intervals; a
live state sensor would attribute everything to "now"). Entities backed
by live coordinator data use the standard CoordinatorEntity pattern.

state_class choices:
  - TOTAL_INCREASING for cumulative kWh / cost (monotonic, HA tracks resets)
  - TOTAL for period / today totals (reset at known boundary)
  - MEASUREMENT for instantaneous / forecast values
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

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
    DATA_SUPPLY_CHARGE,
    DATA_UNIT_RATE,
    DOMAIN,
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
    # --- Forecast / rates (monetary, measurement) ---
    SensorEntityDescription(
        key=DATA_BILL_PROJECTION,
        translation_key="bill_projection",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="AUD",
    ),
    SensorEntityDescription(
        key=DATA_UNIT_RATE,
        translation_key="unit_rate",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="AUD/kWh",
    ),
    SensorEntityDescription(
        key=DATA_SUPPLY_CHARGE,
        translation_key="supply_charge",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="AUD/day",
    ),
)


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: HaggleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up haggle sensor entities for the entry."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities(
        HaggleEnergySensor(coordinator, entry, desc) for desc in SENSOR_DESCRIPTIONS
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
        data: Any = self.coordinator.data
        if data is None:
            return None
        # HaggleData is a dataclass; fall back gracefully if a stub/mock
        # returns a plain dict during tests.
        if hasattr(data, self.entity_description.key):
            value = getattr(data, self.entity_description.key)
        elif isinstance(data, dict):
            value = data.get(self.entity_description.key)
        else:
            return None
        return float(value) if value is not None else None
