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

from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import Platform, UnitOfEnergy
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DATA_BILL_PROJECTION,
    DATA_CONSUMPTION_COST,
    DATA_CONSUMPTION_KWH,
    DATA_CONSUMPTION_PERIOD,
    DATA_FEED_IN_RATE,
    DATA_GENERATION_CREDIT,
    DATA_GENERATION_KWH,
    DATA_GENERATION_PERIOD,
    DATA_GENERATION_PERIOD_CREDIT,
    DATA_SUPPLY_CHARGE,
    DATA_UNIT_RATE,
    DATA_UNIT_RATE_OFFPEAK,
    DATA_UNIT_RATE_PEAK,
    DATA_UNIT_RATE_SHOULDER,
    DATA_UNIT_RATE_SUPEROFFPEAK,
    DOMAIN,
    TARIFF_OFFPEAK,
    TARIFF_PEAK,
    TARIFF_SHOULDER,
    TARIFF_SUPEROFFPEAK,
)
from .coordinator import HaggleCoordinator

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import HaggleConfigEntry

SENSOR_DESCRIPTIONS: tuple[SensorEntityDescription, ...] = (
    # --- Cumulative kWh total (device-card value, NOT an Energy source) ---
    # The all-time cumulative total is fed to the Energy dashboard via
    # import_statistics() (haggle:consumption_<contract>), which places every
    # kWh in its true hour. This entity is only the latest known value for the
    # device card. It carries NO device_class/state_class ON PURPOSE so it
    # cannot be picked as an Energy-dashboard source: its state moves once per
    # 24 h poll, so HA would attribute a whole day's kWh to the poll hour on the
    # wrong day (#147, #137). De-listing also stops it emitting long-term
    # statistics — existing installs see a one-time HA `state_class_removed`
    # repair; resolving it (not merely dismissing) clears the now-stale
    # statistic from the Energy picker (see CHANGELOG / docs/energy-dashboard.md).
    SensorEntityDescription(
        key=DATA_CONSUMPTION_KWH,
        translation_key="consumption",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=3,
    ),
    # --- Sub-period kWh total (device-card value, NOT an Energy source) ---
    # "This period" total, reset at the billing boundary. Like the cumulative
    # total above, it carries NO device_class/state_class: it also advances
    # only once per daily poll, so as an Energy source it would mis-place a
    # whole day's kWh on the poll hour (#147). The Energy-dashboard source is
    # always the haggle:… statistics.
    SensorEntityDescription(
        key=DATA_CONSUMPTION_PERIOD,
        translation_key="consumption_period",
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
    # Cumulative generation total — device-card value, NOT an Energy source.
    # Same rationale as the consumption sensor above: fed to the Energy
    # dashboard via import_statistics() (haggle:generation_<contract>) with
    # true hourly placement, so this entity carries NO device_class/state_class
    # to keep it out of the "Return to grid" source picker (its once-per-poll
    # state would mis-place a whole day's export). De-listing stops long-term
    # statistics — one-time `state_class_removed` repair to resolve (not just
    # dismiss) so the stale statistic leaves the Energy picker (#147).
    SensorEntityDescription(
        key=DATA_GENERATION_KWH,
        translation_key="generation",
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
    # --- Bill-period solar total (device-card value, NOT an Energy source) ---
    # "Sold this period", reset at the billing boundary. Like the other kWh
    # totals it carries NO device_class/state_class so it can't be picked as a
    # "Return to grid" source (its once-per-poll state would mis-place a day's
    # export — #147). `unknown` until the generation series has backfilled to
    # the trailing rewindow — a mid-backfill partial could never match the AGL
    # app's "Sold To Grid" tile (#128).
    SensorEntityDescription(
        key=DATA_GENERATION_PERIOD,
        translation_key="generation_period",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
    ),
    SensorEntityDescription(
        key=DATA_GENERATION_PERIOD_CREDIT,
        translation_key="generation_period_credit",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement="AUD",
        suggested_display_precision=2,
    ),
    # Feed-in tariff is a unit price, not a cumulative amount: MEASUREMENT +
    # AUD/kWh, NO device_class (same rule as the other rate sensors).
    SensorEntityDescription(
        key=DATA_FEED_IN_RATE,
        translation_key="feed_in_rate",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="AUD/kWh",
        suggested_display_precision=4,
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
    # Only ever registered for a configured entry whose windows use the band
    # (#141): tariff_windows.desired_rate_bands never offers it in legacy.
    TARIFF_SUPEROFFPEAK: SensorEntityDescription(
        key=DATA_UNIT_RATE_SUPEROFFPEAK,
        translation_key="unit_rate_superoffpeak",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="AUD/kWh",
    ),
}
# Rate-sensor key -> band, for the `rate_source` attribute.
_RATE_KEY_BANDS: dict[str, str] = {
    desc.key: band for band, desc in TOU_RATE_DESCRIPTIONS.items()
}


def _remove_unwanted_rate_entities(
    hass: HomeAssistant, entry: HaggleConfigEntry, desired: frozenset[str]
) -> None:
    """Drop registry entries for rate sensors a configured entry no longer
    has (D8) — e.g. the spurious "Unit rate (peak)" of #224 once a user
    configures Flat or an EV window. Runs BEFORE async_add_entities, so it
    cannot race the entities being added. A renamed entity id or customisation
    on a removed sensor is lost (CHANGELOG)."""
    registry = er.async_get(hass)
    for band, desc in TOU_RATE_DESCRIPTIONS.items():
        if band in desired:
            continue
        entity_id = registry.async_get_entity_id(
            Platform.SENSOR, DOMAIN, f"{entry.entry_id}_{desc.key}"
        )
        if entity_id is not None:
            registry.async_remove(entity_id)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HaggleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up haggle sensor entities for the entry."""
    coordinator = entry.runtime_data.coordinator
    descriptions = list(SENSOR_DESCRIPTIONS)
    # Per-tariff rate sensors: legacy entries register the bands this
    # contract has been seen using (active_tariffs, populated by the first
    # refresh, which runs before this platform setup) — v0.5.0 behaviour; a
    # configured entry registers its configured rate bands (#141). The SAME
    # function drives the coordinator's reload comparator (M4).
    active = coordinator.data.active_tariffs if coordinator.data else frozenset()
    kind, desired = coordinator.desired_rate_sensors(active)
    if kind == "configured":
        _remove_unwanted_rate_entities(hass, entry, desired)
    coordinator.rate_bands_registered = (kind, desired)
    descriptions.extend(
        desc for band, desc in TOU_RATE_DESCRIPTIONS.items() if band in desired
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

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose the covered billing period for the "this period" sensors.

        `period_start` is the nominal bill start; `covered_from` is the
        earliest day actually included in the total. They differ only for
        generation_period / generation_period_credit, and only when the
        billing period is longer than the local backfill floor (a quarterly
        bill) — see _get_generation_period_totals's documented limitation.
        `truncated` is included (True) only in that case; omitted otherwise
        so it doesn't show up as noise on every normal cycle.
        consumption_period is sourced from AGL's own bill-summary total,
        which is never locally truncated, so period_start always equals
        covered_from there and `truncated` never appears.
        Returns None (no attributes) for every other sensor, and while the
        underlying "this period" value has not yet published (still None).

        Per-band rate sensors expose `rate_source` (#141 AC8): "configured"
        (entered in Options), "plan" (the plan-text heuristic, #90) or
        "none" (unavailable).
        """
        data = self.coordinator.data
        key = self.entity_description.key
        if key in _RATE_KEY_BANDS:
            return {"rate_source": data.rate_sources.get(_RATE_KEY_BANDS[key], "none")}
        if key == DATA_CONSUMPTION_PERIOD:
            period_start = data.consumption_period_start
            covered_from = data.consumption_period_covered_from
            if period_start is None or covered_from is None:
                return None
            return {
                "period_start": period_start.isoformat(),
                "covered_from": covered_from.isoformat(),
            }
        if key in (DATA_GENERATION_PERIOD, DATA_GENERATION_PERIOD_CREDIT):
            gen_period_start = data.generation_period_start
            gen_covered_from = data.generation_period_covered_from
            if gen_period_start is None or gen_covered_from is None:
                return None
            attrs: dict[str, Any] = {
                "period_start": gen_period_start.isoformat(),
                "covered_from": gen_covered_from.isoformat(),
            }
            if data.generation_period_truncated:
                attrs["truncated"] = True
            return attrs
        return None
