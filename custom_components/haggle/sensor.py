"""Sensor entities for haggle.

Cumulative kWh totals for grid import / export, exposed with the field
combinations the HA Energy dashboard expects:

  device_class             = ENERGY
  state_class              = TOTAL_INCREASING   (monotonic; HA tracks resets)
  native_unit_of_measurement = kWh

`TOTAL_INCREASING` is correct for retailer-side cumulative reads (the
underlying meter register only ever goes up). If we ever surface
half-hourly intervals as standalone deltas, those would be a separate
entity with `state_class=MEASUREMENT`.
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

from .const import DATA_GRID_EXPORT_KWH, DATA_GRID_IMPORT_KWH, DOMAIN
from .coordinator import HaggleCoordinator

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import HaggleConfigEntry

ENERGY_SENSORS: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key=DATA_GRID_IMPORT_KWH,
        translation_key="grid_import",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=3,
    ),
    SensorEntityDescription(
        key=DATA_GRID_EXPORT_KWH,
        translation_key="grid_export",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=3,
    ),
)


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: HaggleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up haggle sensor entities."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities(
        HaggleEnergySensor(coordinator, entry, desc) for desc in ENERGY_SENSORS
    )


class HaggleEnergySensor(CoordinatorEntity[HaggleCoordinator], SensorEntity):
    """A cumulative kWh sensor backed by AGL portal data."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: HaggleCoordinator,
        entry: HaggleConfigEntry,
        description: SensorEntityDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="AGL Australia",
            model="Neighbourhood portal",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def native_value(self) -> float | None:
        """Return the latest cumulative kWh value, or None if unknown."""
        data = self.coordinator.data
        if not data:
            return None
        value = data.get(self.entity_description.key)
        return float(value) if value is not None else None
