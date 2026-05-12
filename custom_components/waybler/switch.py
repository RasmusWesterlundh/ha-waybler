"""Waybler switch entities."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.persistent_notification import async_create as pn_create
from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import WayblerApiError, WayblerCarNotConnectedError
from .const import DOMAIN
from .coordinator import WayblerCoordinator

_LOGGER = logging.getLogger(__name__)

_DEVICE_INFO = lambda entry: {  # noqa: E731
    "identifiers": {(DOMAIN, str(entry.entry_id))},
    "name": "Waybler EV Charger",
    "manufacturer": "Waybler",
    "model": "Rörligt Pris",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Waybler switches."""
    coordinator: WayblerCoordinator = entry.runtime_data
    async_add_entities([
        WayblerPriceOptimizationSwitch(coordinator, entry),
        WayblerChargingSwitch(coordinator, entry),
    ])


class WayblerPriceOptimizationSwitch(CoordinatorEntity[WayblerCoordinator], SwitchEntity):
    """Switch that enables/disables the price optimizer.

    When off, the optimizer will not auto-start sessions on car connection and
    will not restart after a manual stop. Does not affect an already-running session.
    """

    entity_description = SwitchEntityDescription(
        key="price_optimization",
        translation_key="price_optimization",
        icon="mdi:brain",
    )
    _attr_has_entity_name = True

    def __init__(self, coordinator: WayblerCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_price_optimization"
        self._attr_device_info = _DEVICE_INFO(entry)

    @property
    def is_on(self) -> bool:
        return self.coordinator.optimization_enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.coordinator.set_optimization_enabled(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.coordinator.set_optimization_enabled(False)


class WayblerChargingSwitch(CoordinatorEntity[WayblerCoordinator], SwitchEntity):
    """Switch indicating whether a charging session is active.

    Turn on  → triggers price-optimized session start (optimizer handles manual
               price override if number.manual_price_limit is set).
    Turn off → stops the session and disables the optimizer so it does not
               immediately restart until the car is reconnected.
    """

    entity_description = SwitchEntityDescription(
        key="charging",
        translation_key="charging",
        icon="mdi:ev-station",
    )
    _attr_has_entity_name = True

    def __init__(self, coordinator: WayblerCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_charging"
        self._attr_device_info = _DEVICE_INFO(entry)

    @property
    def is_on(self) -> bool:
        """True whenever a session exists (Charging or Waiting for price)."""
        return (
            self.coordinator.data is not None
            and self.coordinator.data.active_session is not None
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Trigger the optimizer (which respects any manual price limit set)."""
        try:
            await self.coordinator.async_trigger_optimization()
        except WayblerCarNotConnectedError:
            _LOGGER.warning("Waybler: could not start session — car may not be connected")
            pn_create(
                self.hass,
                "Waybler could not start charging — make sure the car is plugged in.",
                title="Waybler EV Charging",
                notification_id="waybler_car_not_connected",
            )
        except WayblerApiError as err:
            _LOGGER.error("Waybler: failed to start session: %s", err)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop the session and disable the optimizer until the car reconnects."""
        self.coordinator.set_optimization_enabled(False)
        try:
            await self.coordinator.async_stop_session()
        except WayblerApiError as err:
            _LOGGER.error("Waybler: failed to stop session: %s", err)
