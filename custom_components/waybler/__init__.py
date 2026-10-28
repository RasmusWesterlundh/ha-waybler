"""Waybler EV Charging integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import WayblerApiClient, WayblerApiError, WayblerAuthError
from .const import CONF_EMAIL, CONF_PASSWORD, CONF_TOKEN, DOMAIN, PLATFORMS
from .coordinator import WayblerCoordinator

_LOGGER = logging.getLogger(__name__)

type WayblerConfigEntry = ConfigEntry[WayblerCoordinator]


async def _async_reload_entry(
    hass: HomeAssistant, entry: WayblerConfigEntry
) -> None:
    """Reload the integration when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_setup_entry(hass: HomeAssistant, entry: WayblerConfigEntry) -> bool:
    """Set up Waybler from a config entry."""
    session = async_get_clientsession(hass)
    client = WayblerApiClient(session)

    email = entry.data.get(CONF_EMAIL, "")
    password = entry.data.get(CONF_PASSWORD, "")
    if not email or not password:
        raise ConfigEntryAuthFailed("Waybler: no credentials available")

    try:
        token = await client.async_get_token(
            email, password, entry.data.get(CONF_TOKEN, "")
        )
    except WayblerAuthError as err:
        raise ConfigEntryAuthFailed(
            f"Waybler: login failed — check credentials: {err}"
        ) from err
    except WayblerApiError as err:
        raise ConfigEntryNotReady(f"Waybler: API unavailable: {err}") from err

    if token != entry.data.get(CONF_TOKEN):
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_TOKEN: token}
        )

    coordinator = WayblerCoordinator(hass, entry, client, token)

    # Perform the initial refresh (returns the pre-set empty data — entities will
    # populate once the WebSocket delivers its first WebsocketInitMessage).
    await coordinator.async_config_entry_first_refresh()

    # Start the persistent WebSocket connection.
    coordinator.async_start_websocket()
    _LOGGER.info("Waybler: WebSocket task scheduled, integration setup complete")

    entry.runtime_data = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: WayblerConfigEntry) -> bool:
    """Unload a config entry."""
    coordinator: WayblerCoordinator = entry.runtime_data
    coordinator.async_stop_websocket()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
