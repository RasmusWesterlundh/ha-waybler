"""Data update coordinator for the Waybler integration."""

from __future__ import annotations

import asyncio
import json
import logging

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .api import CoordinatorData, SessionData, WayblerApiClient, WayblerApiError
from .const import (
    CONF_CONTRACT_USER_ID,
    CONF_STATION_ID,
    CONF_TOKEN,
    CONF_USER_ID,
    CONF_ZONE_ID,
    DOMAIN,
    WS_APP_UUID,
    WS_URL,
)

_LOGGER = logging.getLogger(__name__)

# Statuses that mean the session is not charging
_INACTIVE_STATUSES = {"Stopped", "Finished", "Error"}

_EMPTY_DATA = CoordinatorData(active_session=None, car_connected=None, station_state=None)


class WayblerCoordinator(DataUpdateCoordinator[CoordinatorData]):
    """Manages WebSocket connection and holds Waybler runtime state.

    All live data (session status, power, energy, car connected) is sourced from
    the WebSocket.  REST is used only for the four write operations:
      login, start session, update price limit, stop session.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: WayblerApiClient,
        token: str,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=None,   # WebSocket pushes; no polling
        )
        self._client = client
        self._token = token
        self._entry = entry

        self._user_id: int = entry.data[CONF_USER_ID]
        self._station_id: int = entry.data[CONF_STATION_ID]
        self._contract_user_id: int = entry.data[CONF_CONTRACT_USER_ID]
        self._zone_id: int = entry.data[CONF_ZONE_ID]

        # WebSocket background task
        self._ws_task: asyncio.Task | None = None

        # Cached WS state (zone + last session message)
        self._station_state: str | None = None
        self._last_session_msg: dict | None = None
        self._active_session_id: int | None = None

        # Pre-populate data so entities start in "unknown" rather than "unavailable"
        self.data = _EMPTY_DATA

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def token(self) -> str:
        return self._token

    @property
    def user_id(self) -> int:
        return self._user_id

    @property
    def station_id(self) -> int:
        return self._station_id

    @property
    def contract_user_id(self) -> int:
        return self._contract_user_id

    @property
    def zone_id(self) -> int:
        return self._zone_id

    @property
    def active_session_id(self) -> int | None:
        return self._active_session_id

    # ------------------------------------------------------------------
    # DataUpdateCoordinator — called once on startup (no polling)
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> CoordinatorData:
        """Return current cached data.  WebSocket drives all live updates."""
        return self.data if self.data is not None else _EMPTY_DATA

    # ------------------------------------------------------------------
    # WebSocket lifecycle
    # ------------------------------------------------------------------

    def async_start_websocket(self) -> None:
        """Start the WebSocket background task (idempotent)."""
        if self._ws_task and not self._ws_task.done():
            return
        self._ws_task = self.hass.async_create_background_task(
            self._ws_run(), "waybler_websocket"
        )

    def async_stop_websocket(self) -> None:
        """Cancel the WebSocket background task."""
        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()
        self._ws_task = None

    async def _ws_run(self) -> None:
        """WebSocket listener loop — reconnects on any failure."""
        retry_delay = 5
        _LOGGER.info("Waybler WS task started (station_id=%s, user_id=%s)", self._station_id, self._user_id)
        while True:
            try:
                await self._ws_connect()
                retry_delay = 5
            except asyncio.CancelledError:
                _LOGGER.warning("Waybler WS task cancelled")
                return
            except Exception as err:
                _LOGGER.warning(
                    "Waybler WS disconnected: %s — reconnecting in %ds", err, retry_delay
                )  # Keep at WARNING — actionable
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 300)

    async def _ws_connect(self) -> None:
        """Open one WebSocket connection and read messages until it closes."""
        ws_url = f"{WS_URL}?jwt={self._token}&app-uuid={WS_APP_UUID}"
        _LOGGER.info("Waybler WS connecting to %s", WS_URL)
        try:
            async with self._client.session.ws_connect(
                ws_url,
                heartbeat=30,
                receive_timeout=120,
            ) as ws:
                _LOGGER.info("Waybler WS connected successfully")
                msg_count = 0
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        msg_count += 1
                        try:
                            parsed = json.loads(msg.data)
                            # Log first 5 messages at DEBUG to aid future debugging
                            if msg_count <= 5:
                                _LOGGER.debug(
                                    "Waybler WS msg #%d raw (first 600 chars): %s",
                                    msg_count, msg.data[:600],
                                )
                            self._handle_ws_message(parsed)
                        except Exception as err:
                            _LOGGER.warning(
                                "Waybler WS message parse error: %s — raw: %s", err, msg.data[:300]
                            )
                    elif msg.type in (
                        aiohttp.WSMsgType.ERROR,
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.CLOSED,
                    ):
                        _LOGGER.warning("Waybler WS closed: type=%s data=%s", msg.type, msg.data)
                        break
                _LOGGER.info("Waybler WS loop exited after %d messages", msg_count)
        except Exception as err:
                _LOGGER.warning("Waybler WS connection error: %s", err)

    # ------------------------------------------------------------------
    # WebSocket message handlers
    # ------------------------------------------------------------------

    @callback
    def _handle_ws_message(self, msg: dict) -> None:
        """Dispatch an incoming WebSocket message by modelType."""
        model_type = msg.get("modelType") or msg.get("type", "")
        _LOGGER.debug("Waybler WS dispatch: modelType=%r keys=%s", model_type, list(msg.keys()))

        if model_type == "WebsocketInitMessage":
            # This arrives LAST as an end-of-init marker with no payload.
            # Individual zone/session models are sent as separate messages before it.
            # Do NOT reset state here — just log and ignore.
            _LOGGER.info("Waybler WS init complete (WebsocketInitMessage received)")
        elif model_type == "ChargeZoneModel":
            self._apply_zone_model(msg)
        elif model_type == "ChargeSessionModel":
            self._apply_session_model(msg)
        elif model_type == "SessionUpdatedEvent":
            # Key is "session", NOT "chargeSessionModel"
            self._apply_session_model(msg.get("session"))
        else:
            _LOGGER.debug("Waybler WS unknown modelType=%r", model_type)

    def _apply_zone_model(self, zone: dict) -> None:
        """Extract our station's state from a ChargeZoneModel and push an update."""
        station_state: str | None = None
        found_ids: list = []
        for group in zone.get("stationGroups") or []:
            for station in group.get("stations") or []:
                sid = station.get("stationId")
                found_ids.append(sid)
                # Compare as int to be safe against string/int mismatch from config
                if int(sid) == int(self._station_id):
                    station_state = station.get("state")
                    break
        _LOGGER.debug(
            "Waybler WS zone: looking for stationId=%s, found ids=%s, matched state=%r",
            self._station_id, found_ids, station_state,
        )
        self._station_state = station_state
        self._push_coordinator_update()

    def _apply_session_model(self, session: dict | None) -> None:
        """Cache the latest session message and push a coordinator update."""
        if session is None:
            _LOGGER.debug("Waybler WS session model: None (no active session)")
            self._last_session_msg = None
            self._active_session_id = None
        elif session.get("status") in _INACTIVE_STATUSES:
            _LOGGER.info(
                "Waybler session ended: status=%r sessionId=%s",
                session.get("status"), session.get("sessionId"),
            )
            self._last_session_msg = None
            self._active_session_id = None
        else:
            _LOGGER.debug(
                "Waybler WS session model: ACTIVE status=%r sessionId=%s power=%s energy=%s",
                session.get("status"), session.get("sessionId"),
                session.get("power"), session.get("chargedEnergy"),
            )
            self._last_session_msg = session
            self._active_session_id = session.get("sessionId")
        self._push_coordinator_update()

    @callback
    def _push_coordinator_update(self) -> None:
        """Build a CoordinatorData snapshot and notify all registered entities."""
        session = self._last_session_msg
        active: SessionData | None = None

        if session and session.get("status") not in _INACTIVE_STATUSES:
            active = SessionData(
                session_id=session.get("sessionId", 0),
                status=session.get("status", ""),
                power_w=float(session.get("power") or 0),
                energy_wh=float(session.get("chargedEnergy") or 0),
                spot_price_limit=session.get("spotPriceLimit"),
            )

        car_connected: bool | None = None
        if self._station_state is not None:
            car_connected = self._station_state == "Busy"

        _LOGGER.debug(
            "Waybler coordinator update: active=%s station_state=%r car_connected=%s listeners=%d",
            f"session {active.session_id}" if active else "None",
            self._station_state,
            car_connected,
            len(self._listeners),
        )

        self.async_set_updated_data(
            CoordinatorData(
                active_session=active,
                car_connected=car_connected,
                station_state=self._station_state,
            )
        )

    # ------------------------------------------------------------------
    # Session control — called by switch / number entities
    # ------------------------------------------------------------------

    async def async_start_session(self, spot_price_limit: float | None = None) -> int:
        """Start a charging session via REST. Returns the new session ID."""
        session_id = await self._client.start_session(
            self._token,
            self._user_id,
            self._station_id,
            self._contract_user_id,
            spot_price_limit,
        )
        self._active_session_id = session_id
        _LOGGER.info("Waybler charging session started: %d", session_id)
        # WS will push the real state shortly; no manual coordinator update needed
        return session_id

    async def async_stop_session(self) -> None:
        """Stop the active charging session via REST."""
        session_id = self._active_session_id
        if session_id is None and self.data and self.data.active_session:
            session_id = self.data.active_session.session_id
        if session_id is None:
            _LOGGER.warning("Waybler: stop_session called but no active session ID known")
            return
        await self._client.stop_session(self._token, self._user_id, session_id)
        self._active_session_id = None
        _LOGGER.info("Waybler charging session %d stopped", session_id)

    async def async_update_price_limit(self, spot_price_limit: float) -> None:
        """Update the spot price limit on the active session via REST."""
        session_id = self._active_session_id
        if session_id is None and self.data and self.data.active_session:
            session_id = self.data.active_session.session_id
        if session_id is None:
            _LOGGER.debug("Waybler: update_price_limit called with no active session")
            return
        await self._client.update_price_limit(
            self._token, self._user_id, session_id, spot_price_limit
        )

