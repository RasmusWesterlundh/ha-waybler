"""Waybler spot price limit number entity."""

from __future__ import annotations

import logging

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import WayblerApiError
from .const import DOMAIN
from .coordinator import WayblerCoordinator

_LOGGER = logging.getLogger(__name__)

PRICE_LIMIT_DESCRIPTION = NumberEntityDescription(
    key="spot_price_limit",
    translation_key="spot_price_limit",
    icon="mdi:currency-eur",
    native_min_value=0.0,
    native_max_value=10.0,
    native_step=0.01,
    mode=NumberMode.BOX,
    entity_registry_enabled_default=False,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Waybler number entities."""
    coordinator: WayblerCoordinator = entry.runtime_data
    async_add_entities([WayblerSpotPriceLimit(coordinator, entry)])


class WayblerSpotPriceLimit(
    CoordinatorEntity[WayblerCoordinator], NumberEntity, RestoreEntity
):
    """Number entity for configuring the spot price charging limit (EUR/kWh).

    When set, Waybler will automatically pause charging when the spot price
    exceeds this value. The value is pushed to the active session when changed,
    and passed at session start.
    """

    entity_description = PRICE_LIMIT_DESCRIPTION
    _attr_has_entity_name = True
    _attr_native_value: float | None = None

    def __init__(self, coordinator: WayblerCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_spot_price_limit"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, str(entry.entry_id))},
            "name": "Waybler EV Charger",
            "manufacturer": "Waybler",
            "model": "Rörligt Pris",
        }

    async def async_added_to_hass(self) -> None:
        """Restore last known value on startup."""
        await super().async_added_to_hass()
        if (last_state := await self.async_get_last_state()) is not None:
            try:
                self._attr_native_value = float(last_state.state)
            except (ValueError, TypeError):
                self._attr_native_value = None

    @property
    def native_value(self) -> float | None:
        return self._attr_native_value

    async def async_set_native_value(self, value: float) -> None:
        """Update price limit locally and push to any active session."""
        self._attr_native_value = value
        self.async_write_ha_state()

        # Push to active session if one exists
        try:
            await self.coordinator.async_update_price_limit(value)
        except WayblerApiError as err:
            _LOGGER.debug(
                "Waybler: could not push price limit to session (may be inactive): %s",
                err,
            )
