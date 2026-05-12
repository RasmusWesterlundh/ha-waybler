"""Waybler binary sensor — car connected indicator."""

from __future__ import annotations

import logging

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import CoordinatorData, WayblerCoordinator

_LOGGER = logging.getLogger(__name__)

CAR_CONNECTED_DESCRIPTION = BinarySensorEntityDescription(
    key="car_connected",
    translation_key="car_connected",
    device_class=BinarySensorDeviceClass.PLUG,
    icon="mdi:car-electric",
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Waybler binary sensor."""
    coordinator: WayblerCoordinator = entry.runtime_data
    async_add_entities([WayblerCarConnected(coordinator, entry)])


class WayblerCarConnected(CoordinatorEntity[WayblerCoordinator], BinarySensorEntity):
    """Binary sensor indicating whether a car is physically connected to the charger.

    True when ``station_state`` is ``"EvConnected"`` (car present, no session)
    or ``"Busy"`` (session in progress).  None when state is unknown.
    """

    entity_description = CAR_CONNECTED_DESCRIPTION
    _attr_has_entity_name = True

    def __init__(self, coordinator: WayblerCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_car_connected"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, str(entry.entry_id))},
            "name": "Waybler EV Charger",
            "manufacturer": "Waybler",
            "model": "Rörligt Pris",
        }

    @property
    def is_on(self) -> bool | None:
        """Return True if car is connected (inferred from active session).

        Returns None when the state cannot be determined.
        """
        data: CoordinatorData | None = self.coordinator.data
        if data is None:
            return None
        return data.car_connected
