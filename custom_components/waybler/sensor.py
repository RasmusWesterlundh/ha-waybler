"""Waybler sensor entities."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import CoordinatorData, WayblerCoordinator

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class WayblerSensorDescription(SensorEntityDescription):
    """Typed sensor description for Waybler."""


SENSOR_DESCRIPTIONS: tuple[WayblerSensorDescription, ...] = (
    WayblerSensorDescription(
        key="session_id",
        translation_key="session_id",
        icon="mdi:identifier",
        native_unit_of_measurement=None,
    ),
    WayblerSensorDescription(
        key="session_energy_kwh",
        translation_key="session_energy_kwh",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        icon="mdi:lightning-bolt",
    ),
    WayblerSensorDescription(
        key="session_power_w",
        translation_key="session_power_w",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        icon="mdi:flash",
    ),
    WayblerSensorDescription(
        key="station_state",
        translation_key="station_state",
        icon="mdi:ev-station",
        native_unit_of_measurement=None,
    ),
    WayblerSensorDescription(
        key="computed_price_limit",
        translation_key="computed_price_limit",
        icon="mdi:currency-eur",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=None,   # unit set dynamically from currency
    ),
    WayblerSensorDescription(
        key="charge_time_today",
        translation_key="charge_time_today",
        icon="mdi:clock-outline",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="h",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Waybler sensor entities."""
    coordinator: WayblerCoordinator = entry.runtime_data
    async_add_entities(
        WayblerSensor(coordinator, entry, description)
        for description in SENSOR_DESCRIPTIONS
    )


class WayblerSensor(CoordinatorEntity[WayblerCoordinator], SensorEntity):
    """A single Waybler sensor."""

    entity_description: WayblerSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: WayblerCoordinator,
        entry: ConfigEntry,
        description: WayblerSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, str(entry.entry_id))},
            "name": "Waybler EV Charger",
            "manufacturer": "Waybler",
            "model": "Rörligt Pris",
        }

    @property
    def native_value(self) -> Any:
        """Return the sensor value."""
        data: CoordinatorData | None = self.coordinator.data
        if data is None:
            return None

        key = self.entity_description.key
        active = data.active_session

        if key == "session_id":
            return active.session_id if active else None
        if key == "session_energy_kwh":
            return round(active.energy_wh / 1000, 3) if active else None
        if key == "session_power_w":
            return round(active.power_w, 1) if active else None
        if key == "station_state":
            return data.station_state
        if key == "computed_price_limit":
            return round(data.computed_price_limit, 4) if data.computed_price_limit is not None else None
        if key == "charge_time_today":
            return round(data.charge_time_today_h, 3)
        return None

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Return the unit — dynamic for computed_price_limit (uses zone currency)."""
        if self.entity_description.key == "computed_price_limit":
            data: CoordinatorData | None = self.coordinator.data
            return data.price_currency if data and data.price_currency else None
        return self.entity_description.native_unit_of_measurement
