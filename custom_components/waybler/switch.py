"""Waybler charging switch entity."""

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
from .const import CONF_PRICE_SENSOR, DOMAIN
from .coordinator import WayblerCoordinator

_LOGGER = logging.getLogger(__name__)

CHARGING_SWITCH = SwitchEntityDescription(
    key="charging",
    translation_key="charging",
    icon="mdi:ev-station",
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Waybler switch."""
    coordinator: WayblerCoordinator = entry.runtime_data
    async_add_entities([WayblerChargingSwitch(coordinator, entry)])


class WayblerChargingSwitch(CoordinatorEntity[WayblerCoordinator], SwitchEntity):
    """Switch that starts/stops a Waybler charging session."""

    entity_description = CHARGING_SWITCH
    _attr_has_entity_name = True

    def __init__(self, coordinator: WayblerCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_charging"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, str(entry.entry_id))},
            "name": "Waybler EV Charger",
            "manufacturer": "Waybler",
            "model": "Rörligt Pris",
        }

    @property
    def is_on(self) -> bool:
        """Return True when a charging session is active."""
        return (
            self.coordinator.data is not None
            and self.coordinator.data.active_session is not None
            and self.coordinator.data.active_session.is_active
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Start a charging session, using the configured price sensor if set."""
        price_sensor = self._entry.data.get(CONF_PRICE_SENSOR, "")
        spot_price_limit: float | None = None

        if price_sensor:
            state = self.hass.states.get(price_sensor)
            if state and state.state not in ("unavailable", "unknown", ""):
                try:
                    spot_price_limit = float(state.state)
                except ValueError:
                    _LOGGER.warning(
                        "Waybler: price sensor %s has non-numeric state '%s', "
                        "starting without price limit",
                        price_sensor,
                        state.state,
                    )

        try:
            await self.coordinator.async_start_session(spot_price_limit)
        except WayblerCarNotConnectedError:
            _LOGGER.warning(
                "Waybler: could not start session — car may not be connected"
            )
            pn_create(
                self.hass,
                "Waybler could not start charging — make sure the car is plugged in.",
                title="Waybler EV Charging",
                notification_id="waybler_car_not_connected",
            )
        except WayblerApiError as err:
            _LOGGER.error("Waybler: failed to start session: %s", err)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop the active charging session."""
        try:
            await self.coordinator.async_stop_session()
        except WayblerApiError as err:
            _LOGGER.error("Waybler: failed to stop session: %s", err)
