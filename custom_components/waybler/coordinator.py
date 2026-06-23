"""Data update coordinator for the Waybler integration."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import datetime, timedelta

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_track_point_in_time, async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .api import (
    CoordinatorData,
    PriceEntry,
    SessionData,
    WayblerApiClient,
    WayblerApiError,
    WayblerAuthError,
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
    CONF_STATION_ID,
    CONF_TOKEN,
    CONF_USER_ID,
    CONF_ZONE_ID,
    DEFAULT_OPT_AUTO_START,
    DEFAULT_OPT_MIN_HOURS,
    DEFAULT_OPT_PERCENTILE,
    DEFAULT_OPT_STRATEGY,
    DOMAIN,
    WS_APP_UUID,
    WS_URL,
)
from .price_optimizer import compute_price_limit, filter_upcoming

_LOGGER = logging.getLogger(__name__)

# Statuses that mean the session is not charging
_INACTIVE_STATUSES = {"Stopped", "Finished", "Error"}

# Maximum number of hours ahead to consider when computing a price limit.
# Prevents "infinite deferral" where tomorrow's much-cheaper prices push the
# computed ceiling so low that today never charges.
_OPT_MAX_WINDOW_HOURS = 24

# Periodic backstop in case a transient WS ordering issue misses a trigger.
_SAFETY_RECONCILE_INTERVAL_MIN = 5

# Hard reliability fallback: every 15 minutes, re-auth and force a fresh
# websocket init sequence to resync station/session state.
_PERIODIC_REAUTH_RESYNC_INTERVAL_MIN = 15

# Guardrail: require at least one eligible hour in the near-term horizon.
# If a strategy computes an overly strict limit, lift it to the cheapest price
# in the next few hours to avoid deferring charging indefinitely.
_GUARDRAIL_SOON_WINDOW_HOURS = 6
_GUARDRAIL_MIN_ELIGIBLE_HOURS = 1

# Station states observed when the car cable is physically connected.
_PLUGGED_STATES = {"Busy", "EvConnected", "CableConnected"}

# Connected states that indicate "car plugged, no active charging session".
_PLUGGED_NO_SESSION_STATES = {"EvConnected", "CableConnected"}

_EMPTY_DATA = CoordinatorData(
    active_session=None,
    car_connected=None,
    station_state=None,
    price_schedule=[],
    is_variable_price_zone=False,
    price_currency="",
    price_vat_rate=0.25,
    computed_price_limit=None,
    charge_time_today_h=0.0,
)


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

        # Price optimization state
        self._price_schedule: list[PriceEntry] = []
        self._is_variable_price_zone: bool = False
        self._price_currency: str = ""
        self._price_vat_rate: float = 0.25   # from zone model consumptionVatRate
        self._computed_price_limit: float | None = None

        # Charge time tracking
        self._charging_start: datetime | None = None
        self._charge_seconds_today: float = 0.0
        self._midnight_unsub: Callable | None = None
        self._safety_reconcile_unsub: Callable | None = None
        self._periodic_reauth_unsub: Callable | None = None

        # Guard: prevent concurrent optimization tasks
        self._optimization_running: bool = False

        # Runtime flag — cleared when user manually stops charging, reset on new car connection
        self._optimization_enabled: bool = True

        # Tracks whether a session model arrived during the current WS init sequence.
        # Used to detect stale _last_session_msg after a reconnect where the session
        # had already ended on the Waybler backend (no Finished message was delivered).
        self._session_seen_in_ws_init: bool = False

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

    @property
    def charge_time_today_h(self) -> float:
        """Total hours spent in Charging state today (excludes Waiting)."""
        elapsed = self._charge_seconds_today
        if self._charging_start is not None:
            elapsed += (dt_util.utcnow() - self._charging_start).total_seconds()
        return elapsed / 3600.0

    # ------------------------------------------------------------------
    # Auth helpers
    # ------------------------------------------------------------------

    async def async_refresh_and_save_token(self) -> None:
        """Refresh the JWT token, falling back to a fresh login if refresh fails.

        Persists the new token to the config entry and updates the in-memory
        ``_token`` used by subsequent REST and WebSocket calls.

        Raises ``ConfigEntryAuthFailed`` if both refresh and login fail so that
        HA can surface a re-auth notification to the user.
        """
        try:
            new_token = await self._client.refresh_token(self._token)
            _LOGGER.info("Waybler token refreshed successfully")
        except WayblerAuthError:
            _LOGGER.warning("Waybler token refresh failed — attempting fresh login")
            try:
                email = self._entry.data[CONF_EMAIL]
                password = self._entry.data[CONF_PASSWORD]
                new_token, _ = await self._client.login(email, password)
                _LOGGER.info("Waybler re-login succeeded")
            except WayblerApiError as login_err:
                _LOGGER.error("Waybler re-login failed: %s", login_err)
                raise ConfigEntryAuthFailed(f"Waybler authentication failed: {login_err}") from login_err

        self._token = new_token
        self.hass.config_entries.async_update_entry(
            self._entry,
            data={**self._entry.data, CONF_TOKEN: new_token},
        )

    # ------------------------------------------------------------------
    # Charge time tracking helpers
    # ------------------------------------------------------------------

    def _schedule_midnight_reset(self) -> None:
        """Schedule a callback at the next local midnight to reset today's charge time."""
        if self._midnight_unsub is not None:
            self._midnight_unsub()
            self._midnight_unsub = None

        now = dt_util.now()
        midnight = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

        @callback
        def _handle_midnight_reset(now_: datetime) -> None:  # noqa: ARG001
            _LOGGER.debug("Waybler: midnight reset of charge_seconds_today")
            self._charge_seconds_today = 0.0
            self._charging_start = None
            self._schedule_midnight_reset()
            self._push_coordinator_update()

        self._midnight_unsub = async_track_point_in_time(
            self.hass, _handle_midnight_reset, midnight
        )

    def _schedule_safety_reconcile(self) -> None:
        """Run a low-frequency reconcile pass as a safety backstop."""
        if self._safety_reconcile_unsub is not None:
            self._safety_reconcile_unsub()
            self._safety_reconcile_unsub = None

        @callback
        def _handle_safety_tick(now_: datetime) -> None:  # noqa: ARG001
            self.hass.async_create_task(self._async_safety_reconcile())

        self._safety_reconcile_unsub = async_track_time_interval(
            self.hass,
            _handle_safety_tick,
            timedelta(minutes=_SAFETY_RECONCILE_INTERVAL_MIN),
        )

    def _schedule_periodic_reauth_and_resync(self) -> None:
        """Schedule a fixed timer to re-auth and force WS state resync."""
        if self._periodic_reauth_unsub is not None:
            self._periodic_reauth_unsub()
            self._periodic_reauth_unsub = None

        @callback
        def _handle_periodic_reauth_tick(now_: datetime) -> None:  # noqa: ARG001
            self.hass.async_create_task(self._async_periodic_reauth_and_resync())

        self._periodic_reauth_unsub = async_track_time_interval(
            self.hass,
            _handle_periodic_reauth_tick,
            timedelta(minutes=_PERIODIC_REAUTH_RESYNC_INTERVAL_MIN),
        )

    async def _async_safety_reconcile(self) -> None:
        """Periodic no-op-safe reconcile to recover from missed WS trigger paths."""
        if self._station_state not in _PLUGGED_STATES:
            return
        if self._optimization_running:
            return
        self._reconcile_ws_state()

    async def _async_periodic_reauth_and_resync(self) -> None:
        """Re-authenticate and force a fresh WS init snapshot every fixed interval."""
        _LOGGER.info("Waybler periodic re-auth/resync tick: starting")

        try:
            await self.async_refresh_and_save_token()
        except ConfigEntryAuthFailed as err:
            _LOGGER.error("Waybler periodic re-auth/resync: auth failed: %s", err)
            return

        await self._async_restart_websocket_connection()
        self._reconcile_ws_state()

    async def _async_restart_websocket_connection(self) -> None:
        """Restart the WS listener task to force a fresh init/state sync."""
        old_task = self._ws_task
        if old_task is not None and not old_task.done():
            old_task.cancel()
            try:
                await old_task
            except asyncio.CancelledError:
                pass
            except Exception as err:  # pragma: no cover - defensive logging
                _LOGGER.debug("Waybler periodic WS restart: previous task ended with %s", err)

        self._ws_task = self.hass.async_create_background_task(
            self._ws_run(), "waybler_websocket"
        )
        _LOGGER.info("Waybler periodic re-auth/resync: websocket restarted")

    @staticmethod
    def _entered_plugged_no_session_state(prev_state: str | None, new_state: str | None) -> bool:
        """Return True when station just transitioned into a plugged-no-session state."""
        return (
            new_state in _PLUGGED_NO_SESSION_STATES
            and prev_state not in _PLUGGED_NO_SESSION_STATES
        )

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
        self._schedule_midnight_reset()
        self._schedule_safety_reconcile()
        self._schedule_periodic_reauth_and_resync()
        self._ws_task = self.hass.async_create_background_task(
            self._ws_run(), "waybler_websocket"
        )

    def async_stop_websocket(self) -> None:
        """Cancel the WebSocket background task."""
        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()
        self._ws_task = None
        if self._midnight_unsub is not None:
            self._midnight_unsub()
            self._midnight_unsub = None
        if self._safety_reconcile_unsub is not None:
            self._safety_reconcile_unsub()
            self._safety_reconcile_unsub = None
        if self._periodic_reauth_unsub is not None:
            self._periodic_reauth_unsub()
            self._periodic_reauth_unsub = None

    async def _ws_run(self) -> None:
        """WebSocket listener loop — reconnects on any failure."""
        retry_delay = 5
        _LOGGER.info("Waybler WS task started (station_id=%s, user_id=%s)", self._station_id, self._user_id)
        while True:
            try:
                # Refresh token before every (re)connect so stale tokens don't
                # cause a silent infinite retry loop.
                try:
                    await self.async_refresh_and_save_token()
                except ConfigEntryAuthFailed:
                    _LOGGER.error("Waybler WS: authentication failed — stopping WebSocket task")
                    return
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
        # Reset per-connection init tracking so stale-session detection works correctly
        self._session_seen_in_ws_init = False
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
            _LOGGER.info("Waybler WS init complete (WebsocketInitMessage received)")
            # Bug fix: if no session model arrived during init but we still have
            # _last_session_msg in memory, that data is stale (the session ended on
            # the Waybler backend while we were disconnected / HA was restarting).
            if not self._session_seen_in_ws_init and self._last_session_msg is not None:
                _LOGGER.info(
                    "Waybler WS init: no session received — clearing stale session (id=%s)",
                    self._active_session_id,
                )
                self._last_session_msg = None
                self._active_session_id = None
                self._push_coordinator_update()
                self._reconcile_ws_state()
        elif model_type == "ChargeZoneModel":
            self._apply_zone_model(msg)
        elif model_type == "ChargeSessionModel":
            self._apply_session_model(msg)
        elif model_type == "SessionUpdatedEvent":
            # Key is "session", NOT "chargeSessionModel"
            self._apply_session_model(msg.get("session"))
        elif model_type == "StationUpdatedEvent":
            # Carries live station-state transitions (e.g. EvConnected → Busy)
            station = msg.get("station") or {}
            sid = station.get("stationId")
            if sid is not None and int(sid) == int(self._station_id):
                prev_state = self._station_state
                self._station_state = station.get("state")
                _LOGGER.debug(
                    "Waybler WS StationUpdatedEvent: stationId=%s state %r → %r",
                    sid, prev_state, self._station_state,
                )
                self._push_coordinator_update()
                # Trigger price-optimised auto-start when car just plugged in
                if (
                    self._entered_plugged_no_session_state(prev_state, self._station_state)
                    and (self.data is None or self.data.active_session is None)
                ):
                    self._optimization_enabled = True  # reset on new car connection
                    self._reconcile_ws_state()
                # Bug fix: Waybler keeps a Waiting session alive across car disconnects.
                # When the car reconnects, the station goes Available → Busy (skipping
                # EvConnected entirely), so the normal optimization trigger never fires.
                # Refresh the price limit so the session reflects current market prices.
                elif (
                    self._station_state == "Busy"
                    and prev_state not in ("Busy", "EvConnected")
                    and self._last_session_msg
                    and self._last_session_msg.get("status") == "Waiting"
                ):
                    _LOGGER.info(
                        "Waybler WS StationUpdatedEvent: car reconnected to Waiting session"
                        " — refreshing price limit"
                    )
                    self._reconcile_ws_state()
        else:
            _LOGGER.debug("Waybler WS unknown modelType=%r", model_type)

    def _apply_zone_model(self, zone: dict) -> None:
        """Extract our station's state and price schedule from a ChargeZoneModel."""
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
        prev_state = self._station_state
        self._station_state = station_state

        # Parse price schedule from zone level
        self._is_variable_price_zone = bool(zone.get("isVariablePriceZone", False))
        self._price_currency = zone.get("currency", "")
        self._price_vat_rate = float(zone.get("consumptionVatRate", self._price_vat_rate))
        price_entries: list[PriceEntry] = []
        for entry in zone.get("priceList") or []:
            try:
                starts_at_raw = entry.get("at") or entry.get("startsAt") or ""
                starts_at = dt_util.parse_datetime(starts_at_raw)
                if starts_at is None:
                    continue
                consumption = entry.get("consumptionFee") or {}
                price = float(consumption.get("total", consumption.get("price", 0.0)))
                price_entries.append(
                    PriceEntry(
                        starts_at=starts_at,
                        price=price,
                        currency=self._price_currency,
                    )
                )
            except (TypeError, ValueError, KeyError) as err:
                _LOGGER.debug("Waybler: skipping price entry %s — %s", entry, err)
        self._price_schedule = price_entries
        _LOGGER.debug(
            "Waybler WS zone: isVariablePriceZone=%s currency=%s price_entries=%d",
            self._is_variable_price_zone, self._price_currency, len(price_entries),
        )

        self._push_coordinator_update()

        # Trigger price-optimised auto-start if car just became plugged in via zone model
        if (
            self._entered_plugged_no_session_state(prev_state, self._station_state)
            and (self.data is None or self.data.active_session is None)
        ):
            self._optimization_enabled = True  # reset on new car connection
            self._reconcile_ws_state()

        # Bug fix: when fresh price data arrives (zone model on WS reconnect), refresh
        # the price limit for any Waiting session so it reflects current market prices.
        # This covers HA restarts and WS reconnects where car stays plugged in.
        if (
            self._last_session_msg
            and self._last_session_msg.get("status") == "Waiting"
            and self._price_schedule
        ):
            self._reconcile_ws_state()

    def _apply_session_model(self, session: dict | None) -> None:
        """Cache the latest session message and push a coordinator update."""
        if session is None:
            _LOGGER.debug("Waybler WS session model: None (no active session)")
            self._accumulate_charge_time()
            self._last_session_msg = None
            self._active_session_id = None
            self._push_coordinator_update()
            self._reconcile_ws_state()
            return
        elif session.get("status") in _INACTIVE_STATUSES:
            _LOGGER.info(
                "Waybler session ended: status=%r sessionId=%s",
                session.get("status"), session.get("sessionId"),
            )
            self._accumulate_charge_time()
            self._last_session_msg = None
            self._active_session_id = None
            self._push_coordinator_update()
            self._reconcile_ws_state()
            return
        else:
            _LOGGER.debug(
                "Waybler WS session model: ACTIVE status=%r sessionId=%s power=%s energy=%s",
                session.get("status"), session.get("sessionId"),
                session.get("power"), session.get("chargedEnergy"),
            )
            prev_status = (self._last_session_msg or {}).get("status")
            new_status = session.get("status")

            # Track entry into / exit from Charging state
            if new_status == "Charging" and prev_status != "Charging":
                self._charging_start = dt_util.utcnow()
            elif new_status != "Charging" and prev_status == "Charging":
                self._accumulate_charge_time()

            self._last_session_msg = session
            self._active_session_id = session.get("sessionId")
            # Mark that a session was received during this WS init sequence
            self._session_seen_in_ws_init = True
        self._push_coordinator_update()  # reached only for active session updates

    def _accumulate_charge_time(self) -> None:
        """Add any in-progress Charging interval to the daily accumulator."""
        if self._charging_start is not None:
            self._charge_seconds_today += (
                dt_util.utcnow() - self._charging_start
            ).total_seconds()
            self._charging_start = None

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

        # Plugged states from Waybler include EvConnected/CableConnected/Busy.
        car_connected: bool | None = None
        if self._station_state is not None:
            car_connected = self._station_state in _PLUGGED_STATES

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
                price_schedule=list(self._price_schedule),
                is_variable_price_zone=self._is_variable_price_zone,
                price_currency=self._price_currency,
                price_vat_rate=self._price_vat_rate,
                computed_price_limit=self._computed_price_limit,
                charge_time_today_h=self.charge_time_today_h,
            )
        )

    def _reconcile_ws_state(self) -> None:
        """Re-evaluate charging actions from the current WS snapshot."""
        if self._station_state in _PLUGGED_NO_SESSION_STATES:
            # Backend says the car is plugged but there is no active session.
            # If we still cache an active session, it is stale and blocks auto-start.
            if self.data is not None and self.data.active_session is not None:
                stale = self.data.active_session
                _LOGGER.warning(
                    "Waybler WS reconcile: station=%s with stale cached session id=%s status=%s — clearing",
                    self._station_state,
                    stale.session_id,
                    stale.status,
                )
                self._accumulate_charge_time()
                self._last_session_msg = None
                self._active_session_id = None
                self._push_coordinator_update()

            if self._optimization_enabled and (self.data is None or self.data.active_session is None):
                self._optimization_enabled = True  # reset on new car connection
                self.hass.async_create_task(self._async_run_price_optimization())
            return

        if (
            self._station_state == "Busy"
            and self._last_session_msg
            and self._last_session_msg.get("status") == "Waiting"
            and self._active_session_id is not None
        ):
            _LOGGER.debug(
                "Waybler WS reconcile: Busy station with Waiting session — refreshing price limit"
            )
            self.hass.async_create_task(self._async_update_waiting_session_limit())

    def _compute_guarded_limit(
        self,
        *,
        strategy: str,
        remaining: float,
        min_hours: float,
        percentile_value: int,
        fixed_limit: float | None,
    ) -> tuple[float | None, list[PriceEntry]]:
        """Compute strategy limit plus near-term anti-deferral guardrails."""
        now = dt_util.now()
        upcoming = filter_upcoming(self._price_schedule, now)
        window_end = now + timedelta(hours=_OPT_MAX_WINDOW_HOURS)
        upcoming = [p for p in upcoming if p.starts_at <= window_end]

        if not upcoming and strategy != "fixed":
            return None, []

        limit = compute_price_limit(
            prices=upcoming,
            strategy=strategy,
            remaining_hours=remaining,
            min_hours=min_hours,
            percentile_value=percentile_value,
            fixed_limit=fixed_limit,
        )

        if (
            limit is not None
            and strategy != "fixed"
            and remaining > 0
        ):
            soon_end = now + timedelta(hours=_GUARDRAIL_SOON_WINDOW_HOURS)
            soon_prices = [p for p in upcoming if p.starts_at <= soon_end]
            if soon_prices:
                eligible_soon = sum(1 for p in soon_prices if p.price <= limit)
                if eligible_soon < _GUARDRAIL_MIN_ELIGIBLE_HOURS:
                    fallback_limit = min(p.price for p in soon_prices)
                    if fallback_limit > limit:
                        _LOGGER.warning(
                            "Waybler guardrail: computed limit %.4f yields no eligible hours in next %dh; "
                            "lifting to %.4f",
                            limit,
                            _GUARDRAIL_SOON_WINDOW_HOURS,
                            fallback_limit,
                        )
                        limit = fallback_limit

        return limit, upcoming

    # ------------------------------------------------------------------
    # Session control — called by switch / number entities
    # ------------------------------------------------------------------

    async def async_start_session(self, spot_price_limit: float | None = None) -> int:
        """Start a charging session via REST. Returns the new session ID."""
        try:
            session_id = await self._client.start_session(
                self._token,
                self._user_id,
                self._station_id,
                self._contract_user_id,
                spot_price_limit,
            )
        except WayblerAuthError:
            await self.async_refresh_and_save_token()
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

    @property
    def optimization_enabled(self) -> bool:
        return self._optimization_enabled

    def set_optimization_enabled(self, enabled: bool) -> None:
        """Enable or disable price optimization without affecting an active session."""
        self._optimization_enabled = enabled
        self._push_coordinator_update()

    async def async_trigger_optimization(self) -> None:
        """Manually trigger a price-optimized session start."""
        self._optimization_enabled = True
        await self._async_run_price_optimization(manual=True)

    async def async_stop_session(self) -> None:
        session_id = self._active_session_id
        if session_id is None and self.data and self.data.active_session:
            session_id = self.data.active_session.session_id
        if session_id is None:
            _LOGGER.warning("Waybler: stop_session called but no active session ID known")
            return
        try:
            await self._client.stop_session(self._token, self._user_id, session_id)
        except WayblerAuthError:
            await self.async_refresh_and_save_token()
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
        try:
            await self._client.update_price_limit(
                self._token, self._user_id, session_id, spot_price_limit
            )
        except WayblerAuthError:
            await self.async_refresh_and_save_token()
            await self._client.update_price_limit(
                self._token, self._user_id, session_id, spot_price_limit
            )

    # ------------------------------------------------------------------
    # Price optimization
    # ------------------------------------------------------------------

    async def _async_run_price_optimization(self, manual: bool = False) -> None:
        """Compute the optimal price limit and start a session.

        Called automatically on car connection, or manually via async_trigger_optimization.
        When manual=True the auto_start config flag is ignored.
        """
        if self._optimization_running:
            _LOGGER.debug("Waybler: price optimization already in progress — skipping")
            return
        self._optimization_running = True
        try:
            opts = self._entry.options
            if not manual:
                auto_start: bool = opts.get(CONF_OPT_AUTO_START, DEFAULT_OPT_AUTO_START)
                if not auto_start:
                    _LOGGER.debug("Waybler: auto-start disabled via options — skipping optimization")
                    return
            if not self._optimization_enabled:
                _LOGGER.debug("Waybler: price optimization disabled by user — skipping")
                return

            # Check if a manual price limit is set (number entity) — if so, use it directly
            manual_limit = self._get_manual_price_limit()
            if manual_limit is not None:
                _LOGGER.info("Waybler: using manual price limit %.4f (excl. VAT)", manual_limit / (1.0 + self._price_vat_rate))
                api_limit = round(manual_limit / (1.0 + self._price_vat_rate), 4)
                self._computed_price_limit = manual_limit
                try:
                    await self.async_start_session(spot_price_limit=api_limit)
                    self._push_coordinator_update()
                except WayblerApiError as err:
                    _LOGGER.error("Waybler: could not start manual-price session: %s", err)
                return

            strategy: str = opts.get(CONF_OPT_STRATEGY, DEFAULT_OPT_STRATEGY)
            min_hours: float = opts.get(CONF_OPT_MIN_HOURS, DEFAULT_OPT_MIN_HOURS)
            pct: int = int(opts.get(CONF_OPT_PERCENTILE, DEFAULT_OPT_PERCENTILE))
            fixed_limit: float | None = opts.get(CONF_OPT_FIXED_LIMIT)

            remaining = max(0.0, min_hours - self.charge_time_today_h)
            limit, upcoming = self._compute_guarded_limit(
                strategy=strategy,
                remaining=remaining,
                min_hours=min_hours,
                percentile_value=pct,
                fixed_limit=fixed_limit,
            )

            if not upcoming and strategy != "fixed":
                _LOGGER.warning(
                    "Waybler price optimization: no upcoming price entries — cannot compute limit"
                )
                return

            _LOGGER.info(
                "Waybler price optimization: strategy=%s remaining=%.1fh limit=%s currency=%s",
                strategy, remaining, limit, self._price_currency,
            )

            if limit is None:
                _LOGGER.info("Waybler: target already met or no limit computed — not starting session")
                return

            self._computed_price_limit = limit
            # spotPriceLimit in the Waybler API is excl. VAT;
            # priceList totals are incl. VAT — divide to convert.
            api_limit = round(limit / (1.0 + self._price_vat_rate), 4)

            try:
                await self.async_start_session(spot_price_limit=api_limit)
                self.hass.bus.async_fire(
                    "waybler_price_optimized",
                    {
                        "station_id": self._station_id,
                        "strategy": strategy,
                        "spot_price_limit": limit,  # all-in incl. VAT; API receives excl-VAT value
                        "currency": self._price_currency,
                        "remaining_hours": remaining,
                    },
                )
                self._push_coordinator_update()
            except ConfigEntryAuthFailed:
                _LOGGER.error("Waybler: authentication failed during price-optimized start")
                from homeassistant.components.persistent_notification import (
                    async_create as pn_create,
                )
                pn_create(
                    self.hass,
                    "Waybler EV charger: authentication failed during price optimization. "
                    "Please re-authenticate via Settings → Integrations.",
                    title="Waybler: Re-authentication required",
                    notification_id="waybler_auth_failed",
                )
            except WayblerApiError as err:
                _LOGGER.error("Waybler: could not start price-optimized session: %s", err)
        finally:
            self._optimization_running = False

    async def _async_update_waiting_session_limit(self) -> None:
        """Recompute and push a fresh price limit to an active Waiting session.

        Called when new price data arrives (zone model on WS reconnect) or when
        the car reconnects directly to an existing Waiting session (Available → Busy).
        This ensures the session's price ceiling always reflects current market prices
        rather than a stale limit computed at the original session-start time.
        """
        if self._optimization_running:
            return
        if not (self._last_session_msg and self._last_session_msg.get("status") == "Waiting"):
            _LOGGER.debug("Waybler: _async_update_waiting_session_limit: no Waiting session — skipped")
            return
        if self._active_session_id is None:
            return

        opts = self._entry.options
        strategy: str = opts.get(CONF_OPT_STRATEGY, DEFAULT_OPT_STRATEGY)
        min_hours: float = opts.get(CONF_OPT_MIN_HOURS, DEFAULT_OPT_MIN_HOURS)
        pct: int = int(opts.get(CONF_OPT_PERCENTILE, DEFAULT_OPT_PERCENTILE))
        fixed_limit: float | None = opts.get(CONF_OPT_FIXED_LIMIT)

        remaining = max(0.0, min_hours - self.charge_time_today_h)
        limit, upcoming = self._compute_guarded_limit(
            strategy=strategy,
            remaining=remaining,
            min_hours=min_hours,
            percentile_value=pct,
            fixed_limit=fixed_limit,
        )

        if not upcoming and strategy != "fixed":
            _LOGGER.debug("Waybler: can't refresh Waiting session limit — no upcoming prices")
            return

        if limit is None:
            _LOGGER.debug("Waybler: Waiting session limit update: computed None — skipping")
            return

        if limit == self._computed_price_limit:
            _LOGGER.debug(
                "Waybler: Waiting session %s limit unchanged (%.4f) — no update",
                self._active_session_id, limit,
            )
            return

        _LOGGER.info(
            "Waybler: refreshing Waiting session %s price limit %s → %.4f (strategy=%s)",
            self._active_session_id,
            f"{self._computed_price_limit:.4f}" if self._computed_price_limit else "None",
            limit,
            strategy,
        )
        self._computed_price_limit = limit
        api_limit = round(limit / (1.0 + self._price_vat_rate), 4)
        try:
            await self.async_update_price_limit(api_limit)
            self._push_coordinator_update()
        except WayblerApiError as err:
            _LOGGER.warning(
                "Waybler: could not refresh Waiting session %s price limit: %s",
                self._active_session_id, err,
            )

    def _get_manual_price_limit(self) -> float | None:
        """Return the manual price limit (incl. VAT) if set, enabled and non-zero, else None."""
        from homeassistant.helpers import entity_registry as er
        registry = er.async_get(self.hass)
        entry = registry.async_get_entity_id("number", DOMAIN, f"{self._entry.entry_id}_spot_price_limit")
        if entry is None:
            return None
        # Ignore if entity is disabled
        reg_entry = registry.async_get(entry)
        if reg_entry is not None and reg_entry.disabled_by is not None:
            return None
        state = self.hass.states.get(entry)
        if state is None or state.state in ("unavailable", "unknown", "", "None"):
            return None
        try:
            value = float(state.state)
        except (ValueError, TypeError):
            return None
        # Treat 0 as "not set"
        return value if value > 0 else None
