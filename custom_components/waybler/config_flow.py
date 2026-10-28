"""Config flow for the Waybler integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    WayblerApiClient,
    WayblerApiError,
    WayblerAuthError,
    WayblerStation,
    WayblerTermsNotAcceptedError,
)
from .const import (
    CONF_CONTRACT_USER_ID,
    CONF_EMAIL,
    CONF_OPT_AUTO_START,
    CONF_OPT_FIXED_LIMIT,
    CONF_OPT_MIN_HOURS,
    CONF_OPT_PERCENTILE,
    CONF_OPT_STRATEGY,
    CONF_PASSWORD,
    CONF_PRICE_SENSOR,
    CONF_STATION_ID,
    CONF_TOKEN,
    CONF_USER_ID,
    CONF_ZONE_ID,
    DEFAULT_OPT_AUTO_START,
    DEFAULT_OPT_MIN_HOURS,
    DEFAULT_OPT_PERCENTILE,
    DEFAULT_OPT_STRATEGY,
    DOMAIN,
    STRATEGIES,
)

_LOGGER = logging.getLogger(__name__)

CONF_STATION = "station"


class WayblerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial setup and reconfiguration of the Waybler integration."""

    VERSION = 1

    def __init__(self) -> None:
        self._email: str = ""
        self._password: str = ""
        self._token: str = ""
        self._user_id: int = 0
        self._stations: dict[str, WayblerStation] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1: log in and discover the account's charging stations."""
        errors: dict[str, str] = {}

        if user_input is not None:
            email = user_input[CONF_EMAIL].strip().lower()
            password = user_input[CONF_PASSWORD]

            client = WayblerApiClient(async_get_clientsession(self.hass))
            try:
                token, user_id = await client.login(email, password)

                # Prevent duplicate entries for the same account before doing
                # the (slower) WebSocket discovery.
                await self.async_set_unique_id(str(user_id))
                self._abort_if_unique_id_configured()

                stations = await client.discover_stations(token)
            except WayblerTermsNotAcceptedError:
                errors["base"] = "terms_not_accepted"
            except WayblerAuthError:
                errors["base"] = "invalid_auth"
            except WayblerApiError as err:
                _LOGGER.error("Waybler setup failed: %s", err)
                errors["base"] = "cannot_connect"
            except AbortFlow:
                raise
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error during Waybler setup")
                errors["base"] = "unknown"
            else:
                if not stations:
                    errors["base"] = "no_stations"
                else:
                    self._email = email
                    self._password = password
                    self._token = token
                    self._user_id = user_id
                    self._stations = {
                        station.key: station for station in stations
                    }

                    if len(stations) == 1:
                        return self._async_create_entry(stations[0])

                    return await self.async_step_station()

        schema = vol.Schema(
            {
                vol.Required(CONF_EMAIL): str,
                vol.Required(CONF_PASSWORD): str,
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_station(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2: choose a station when the account has more than one."""
        if user_input is not None:
            station = self._stations.get(user_input[CONF_STATION])
            if station is None:
                return self.async_abort(reason="no_stations")
            return self._async_create_entry(station)

        return self.async_show_form(
            step_id="station",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_STATION): vol.In(
                        self._station_options()
                    )
                }
            ),
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Re-discover stations and fix the selected station for an existing entry."""
        entry = self._get_entry()
        if entry is None:
            return self.async_abort(reason="unknown")

        if user_input is not None:
            station = self._stations.get(user_input[CONF_STATION])
            if station is None:
                return self.async_abort(reason="no_stations")
            return self._async_update_entry(entry, station)

        email = entry.data.get(CONF_EMAIL)
        password = entry.data.get(CONF_PASSWORD)
        if not email or not password:
            return self.async_abort(reason="invalid_auth")

        client = WayblerApiClient(async_get_clientsession(self.hass))
        try:
            token = await client.async_get_token(
                email,
                password,
                entry.data.get(CONF_TOKEN, ""),
            )
            stations = await client.discover_stations(token)
        except WayblerTermsNotAcceptedError:
            return self.async_abort(reason="terms_not_accepted")
        except WayblerAuthError:
            return self.async_abort(reason="invalid_auth")
        except WayblerApiError as err:
            _LOGGER.error("Waybler reconfiguration failed: %s", err)
            return self.async_abort(reason="cannot_connect")

        if not stations:
            return self.async_abort(reason="no_stations")

        self._stations = {station.key: station for station in stations}
        if len(stations) == 1:
            return self._async_update_entry(entry, stations[0])

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_STATION): vol.In(
                        self._station_options()
                    )
                }
            ),
        )

    def _get_entry(self) -> ConfigEntry | None:
        """Return the entry being reconfigured, if any."""
        entry_id = self.context.get("entry_id")
        if not entry_id:
            return None
        return self.hass.config_entries.async_get_entry(entry_id)

    def _station_options(self) -> dict[str, str]:
        """Return station keys mapped to their display labels."""
        return {
            station.key: station.label for station in self._stations.values()
        }

    def _async_create_entry(self, station: WayblerStation) -> ConfigFlowResult:
        """Create a config entry for the selected station."""
        return self.async_create_entry(
            title=f"Waybler ({self._email})",
            data={
                CONF_EMAIL: self._email,
                CONF_PASSWORD: self._password,
                CONF_TOKEN: self._token,
                CONF_USER_ID: self._user_id,
                CONF_STATION_ID: station.station_id,
                CONF_CONTRACT_USER_ID: station.contract_user_id,
                CONF_ZONE_ID: station.zone_id,
            },
        )

    def _async_update_entry(
        self, entry: ConfigEntry, station: WayblerStation
    ) -> ConfigFlowResult:
        """Update an existing entry with the selected station."""
        # Drop station IDs that older versions stored in options; the config
        # entry data is now the single source of truth.
        options = {
            key: value
            for key, value in entry.options.items()
            if key
            not in (
                CONF_STATION_ID,
                CONF_CONTRACT_USER_ID,
                CONF_ZONE_ID,
            )
        }
        return self.async_update_reload_and_abort(
            entry,
            data={
                **entry.data,
                CONF_STATION_ID: station.station_id,
                CONF_CONTRACT_USER_ID: station.contract_user_id,
                CONF_ZONE_ID: station.zone_id,
            },
            options=options,
            reason="reconfigure_successful",
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):  # type: ignore[override]
        """Return the options flow handler."""
        return WayblerOptionsFlow()


class WayblerOptionsFlow(OptionsFlow):
    """Handle price optimisation options after initial setup.

    The charging station is changed with the Reconfigure flow, which
    rediscovers the account's zones and stations.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage Waybler options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        current_opts = self.config_entry.options
        strategy = current_opts.get(CONF_OPT_STRATEGY, DEFAULT_OPT_STRATEGY)

        schema = vol.Schema(
            {
                vol.Optional(CONF_PRICE_SENSOR): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor", multiple=False)
                ),
                # Price optimization
                vol.Optional(
                    CONF_OPT_AUTO_START,
                    default=current_opts.get(
                        CONF_OPT_AUTO_START, DEFAULT_OPT_AUTO_START
                    ),
                ): bool,
                vol.Optional(
                    CONF_OPT_STRATEGY,
                    default=strategy,
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=STRATEGIES,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                        translation_key="opt_strategy",
                    )
                ),
                vol.Optional(
                    CONF_OPT_MIN_HOURS,
                    default=current_opts.get(
                        CONF_OPT_MIN_HOURS, DEFAULT_OPT_MIN_HOURS
                    ),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1,
                        max=12,
                        step=0.5,
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Optional(
                    CONF_OPT_PERCENTILE,
                    default=current_opts.get(
                        CONF_OPT_PERCENTILE, DEFAULT_OPT_PERCENTILE
                    ),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=10,
                        max=90,
                        step=5,
                        mode=selector.NumberSelectorMode.SLIDER,
                    )
                ),
                vol.Optional(
                    CONF_OPT_FIXED_LIMIT,
                    default=current_opts.get(CONF_OPT_FIXED_LIMIT, 2.0),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0,
                        max=10,
                        step=0.01,
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
