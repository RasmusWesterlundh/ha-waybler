"""Config flow for the Waybler integration."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import selector

from .api import WayblerApiClient, WayblerAuthError, WayblerApiError
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
    STRATEGY_FIXED,
    STRATEGY_N_CHEAPEST,
    STRATEGY_PERCENTILE,
)

_LOGGER = logging.getLogger(__name__)

# Defaults from the user's known setup (overridable in UI)
_DEFAULT_STATION_ID = 26580
_DEFAULT_CONTRACT_USER_ID = 121689
_DEFAULT_ZONE_ID = 9322


class WayblerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial setup of the Waybler integration."""

    VERSION = 1

    def __init__(self) -> None:
        self._email: str = ""
        self._password: str = ""
        self._token: str = ""
        self._user_id: int = 0

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1: credentials."""
        errors: dict[str, str] = {}

        if user_input is not None:
            email = user_input[CONF_EMAIL].strip().lower()
            password = user_input[CONF_PASSWORD]

            session = async_get_clientsession(self.hass)
            client = WayblerApiClient(session)
            try:
                token, user_id = await client.login(email, password)
            except WayblerAuthError as err:
                _LOGGER.error("Waybler login auth error: %s", err)
                errors["base"] = "invalid_auth"
            except WayblerApiError as err:
                _LOGGER.error("Waybler login API error: %s", err)
                errors["base"] = "cannot_connect"
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception("Waybler login unexpected error: %s", err)
                errors["base"] = "cannot_connect"
            else:
                self._email = email
                self._password = password
                self._token = token
                self._user_id = user_id

                # Prevent duplicate entries for the same account
                await self.async_set_unique_id(str(user_id))
                self._abort_if_unique_id_configured()

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
        """Step 2: station / contract IDs and optional price sensor."""
        errors: dict[str, str] = {}

        if user_input is not None:
            return self.async_create_entry(
                title=f"Waybler ({self._email})",
                data={
                    CONF_EMAIL: self._email,
                    CONF_PASSWORD: self._password,
                    CONF_TOKEN: self._token,
                    CONF_USER_ID: self._user_id,
                    CONF_STATION_ID: user_input[CONF_STATION_ID],
                    CONF_CONTRACT_USER_ID: user_input[CONF_CONTRACT_USER_ID],
                    CONF_ZONE_ID: user_input[CONF_ZONE_ID],
                    CONF_PRICE_SENSOR: user_input.get(CONF_PRICE_SENSOR, ""),
                },
            )

        schema = vol.Schema(
            {
                vol.Required(CONF_STATION_ID, default=_DEFAULT_STATION_ID): int,
                vol.Required(
                    CONF_CONTRACT_USER_ID, default=_DEFAULT_CONTRACT_USER_ID
                ): int,
                vol.Required(CONF_ZONE_ID, default=_DEFAULT_ZONE_ID): int,
                vol.Optional(CONF_PRICE_SENSOR): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor", multiple=False)
                ),
            }
        )
        return self.async_show_form(
            step_id="station",
            data_schema=schema,
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):  # type: ignore[override]
        """Return the options flow handler."""
        return WayblerOptionsFlow()


class WayblerOptionsFlow(OptionsFlow):
    """Handle options (price sensor, IDs, price optimization) after initial setup."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage Waybler options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        current_data = self.config_entry.data
        current_opts = self.config_entry.options
        strategy = current_opts.get(CONF_OPT_STRATEGY, DEFAULT_OPT_STRATEGY)

        schema = vol.Schema(
            {
                vol.Optional(CONF_PRICE_SENSOR): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor", multiple=False)
                ),
                vol.Optional(
                    CONF_STATION_ID,
                    default=current_data.get(CONF_STATION_ID, _DEFAULT_STATION_ID),
                ): int,
                vol.Optional(
                    CONF_CONTRACT_USER_ID,
                    default=current_data.get(CONF_CONTRACT_USER_ID, _DEFAULT_CONTRACT_USER_ID),
                ): int,
                vol.Optional(
                    CONF_ZONE_ID,
                    default=current_data.get(CONF_ZONE_ID, _DEFAULT_ZONE_ID),
                ): int,
                # Price optimization
                vol.Optional(
                    CONF_OPT_AUTO_START,
                    default=current_opts.get(CONF_OPT_AUTO_START, DEFAULT_OPT_AUTO_START),
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
                    default=current_opts.get(CONF_OPT_MIN_HOURS, DEFAULT_OPT_MIN_HOURS),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=1, max=12, step=0.5, mode=selector.NumberSelectorMode.BOX)
                ),
                vol.Optional(
                    CONF_OPT_PERCENTILE,
                    default=current_opts.get(CONF_OPT_PERCENTILE, DEFAULT_OPT_PERCENTILE),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=10, max=90, step=5, mode=selector.NumberSelectorMode.SLIDER)
                ),
                vol.Optional(
                    CONF_OPT_FIXED_LIMIT,
                    default=current_opts.get(CONF_OPT_FIXED_LIMIT, 2.0),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=0, max=10, step=0.01, mode=selector.NumberSelectorMode.BOX)
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
