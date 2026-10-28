"""Waybler API client."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import aiohttp

from .const import (
    API_BASE_URL,
    API_LOGIN_PATH,
    API_REFRESH_PATH,
    API_SESSION_PATH,
    API_SESSIONS_CHARGE_PATH,
    DISCOVERY_TIMEOUT,
    JWT_USER_ID_CLAIM,
    REQUEST_TIMEOUT,
    WS_APP_UUID,
    WS_URL,
)

_LOGGER = logging.getLogger(__name__)


class WayblerApiError(Exception):
    """Raised on non-auth API errors."""


class WayblerAuthError(WayblerApiError):
    """Raised when authentication fails."""


class WayblerTermsNotAcceptedError(WayblerApiError):
    """Raised when the Waybler account has not accepted the current terms."""


class WayblerCarNotConnectedError(WayblerApiError):
    """Raised when trying to start a session but no car is connected."""


@dataclass
class SessionData:
    """Live charging session data, sourced from WebSocket messages."""

    session_id: int
    status: str        # e.g. "Charging", "Stopped"
    power_w: float     # live power in Watts
    energy_wh: float   # energy delivered this session in Wh
    spot_price_limit: float | None

    @property
    def is_active(self) -> bool:
        """Return True when the session is actively charging."""
        return self.status == "Charging"


@dataclass
class PriceEntry:
    """A single hourly price entry from the Waybler priceList."""

    starts_at: datetime   # timezone-aware, from the "at" field
    price: float          # consumptionFee.total — inc. VAT, in zone currency
    currency: str         # e.g. "SEK"


@dataclass(frozen=True)
class WayblerStation:
    """A charging station discovered from the Waybler zone model."""

    zone_id: int
    zone_name: str
    contract_user_id: int
    station_id: int
    station_name: str
    group_name: str
    state: str

    @property
    def key(self) -> str:
        """Stable key used in config-flow selections."""
        return f"{self.zone_id}:{self.contract_user_id}:{self.station_id}"

    @property
    def label(self) -> str:
        """Human-readable label for the station picker."""
        parts = [
            self.station_name or f"Station {self.station_id}",
            self.group_name,
            self.zone_name,
        ]
        # Preserve order while dropping blanks and duplicates.
        unique = list(dict.fromkeys(part for part in parts if part))
        return " · ".join(unique)


@dataclass
class CoordinatorData:
    """Data held by the coordinator, driven by WebSocket pushes."""

    active_session: SessionData | None
    car_connected: bool | None   # True when car is physically present; None = unknown
    station_state: str | None    # "EvConnected", "Busy", "NoEv", …
    price_schedule: list[PriceEntry] = field(default_factory=list)
    is_variable_price_zone: bool = False
    price_currency: str = ""
    price_vat_rate: float = 0.25   # consumptionVatRate from zone model (e.g. 0.25 = 25%)
    computed_price_limit: float | None = None
    charge_time_today_h: float = 0.0


def _decode_jwt_user_id(token: str) -> int:
    """Decode the user ID from a Waybler JWT without verifying signature."""
    try:
        payload_b64 = token.split(".")[1]
        # Restore base64 padding
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return int(payload[JWT_USER_ID_CLAIM])
    except (IndexError, KeyError, ValueError, json.JSONDecodeError) as err:
        raise WayblerApiError(f"Could not decode user ID from JWT: {err}") from err


def _stations_from_zone(zone: dict[str, Any]) -> list[WayblerStation]:
    """Flatten a ChargeZoneModel into selectable stations."""
    try:
        zone_id = int(zone["zoneId"])
        contract_user_id = int(zone["contractUserId"])
    except (KeyError, TypeError, ValueError):
        return []

    zone_name = str(zone.get("name") or "")
    stations: list[WayblerStation] = []

    for group in zone.get("stationGroups") or []:
        group_name = str(group.get("name") or "")
        for station in group.get("stations") or []:
            try:
                station_id = int(station["stationId"])
            except (KeyError, TypeError, ValueError):
                continue
            stations.append(
                WayblerStation(
                    zone_id=zone_id,
                    zone_name=zone_name,
                    contract_user_id=contract_user_id,
                    station_id=station_id,
                    station_name=str(station.get("name") or ""),
                    group_name=group_name,
                    state=str(station.get("state") or ""),
                )
            )

    return stations


class WayblerApiClient:
    """Async client for the Waybler v7 API."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        """Initialise with a shared aiohttp session."""
        self._session = session

    @property
    def session(self) -> aiohttp.ClientSession:
        """Expose the underlying aiohttp session (used by the coordinator for WebSocket)."""
        return self._session

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    async def login(self, email: str, password: str) -> tuple[str, int]:
        """Log in and return (token, user_id)."""
        url = f"{API_BASE_URL}{API_LOGIN_PATH}"
        try:
            async with self._session.post(
                url,
                json={"email": email, "password": password},
                headers={"Content-Type": "application/json; charset=utf-8"},
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            ) as resp:
                status = resp.status
                raw = await resp.text()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise WayblerApiError(f"Login request failed: {err}") from err

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as err:
            raise WayblerApiError(
                f"Login returned a non-JSON response (HTTP {status})"
            ) from err

        if not isinstance(data, dict):
            raise WayblerApiError("Login returned an unexpected response")

        if data.get("result") != "Ok":
            raise WayblerAuthError(
                f"Login failed: result={data.get('result')}, "
                f"email={data.get('email')}"
            )

        token = data.get("token")
        if not token:
            raise WayblerApiError("Login response did not contain a token")
        return token, _decode_jwt_user_id(token)

    async def refresh_token(self, current_token: str) -> str:
        """Refresh a token and return the new one.

        Raises WayblerAuthError if the token is expired/invalid.
        """
        data = await self._get(API_REFRESH_PATH, token=current_token)
        if data.get("result") != "Ok":
            raise WayblerAuthError("Token refresh failed")
        token = data.get("token")
        if not token:
            raise WayblerApiError("Token refresh returned no token")
        return token

    async def async_get_token(
        self, email: str, password: str, current_token: str = ""
    ) -> str:
        """Return a usable token, refreshing first and logging in as a fallback."""
        if current_token:
            try:
                return await self.refresh_token(current_token)
            except WayblerApiError as err:
                _LOGGER.debug(
                    "Waybler token refresh failed (%s); logging in again", err
                )

        token, _ = await self.login(email, password)
        return token

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    async def discover_stations(
        self, token: str, timeout: float = DISCOVERY_TIMEOUT
    ) -> list[WayblerStation]:
        """Read the account's zones and stations from the Waybler WebSocket."""
        ws_url = f"{WS_URL}?jwt={token}&app-uuid={WS_APP_UUID}"
        stations: list[WayblerStation] = []
        terms_accepted = True

        try:
            async with asyncio.timeout(timeout):
                async with self._session.ws_connect(
                    ws_url,
                    heartbeat=30,
                    receive_timeout=timeout,
                ) as ws:
                    async for msg in ws:
                        if msg.type is aiohttp.WSMsgType.TEXT:
                            try:
                                data = json.loads(msg.data)
                            except json.JSONDecodeError:
                                continue
                            model_type = data.get("modelType")
                            if model_type == "ChargeZoneModel":
                                stations.extend(_stations_from_zone(data))
                            elif model_type == "UserTermsModel":
                                terms_accepted = bool(
                                    data.get("accepted", True)
                                )
                            elif model_type == "WebsocketInitMessage":
                                break
                        elif msg.type in (
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.ERROR,
                        ):
                            break
        except TimeoutError as err:
            raise WayblerApiError(
                "Timed out while reading zones from Waybler"
            ) from err
        except aiohttp.ClientError as err:
            raise WayblerApiError(f"Waybler WebSocket error: {err}") from err

        if not terms_accepted:
            raise WayblerTermsNotAcceptedError(
                "The Waybler account has not accepted the current terms"
            )
        return stations

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    async def start_session(
        self,
        token: str,
        user_id: int,
        station_id: int,
        contract_user_id: int,
        spot_price_limit: float | None = None,
    ) -> int:
        """Start a charging session. Returns the new session ID."""
        path = API_SESSIONS_CHARGE_PATH.format(user_id=user_id)
        payload: dict[str, Any] = {
            "modelType": "CreateChargeSessionRequest",
            "stationId": station_id,
            "contractUserId": contract_user_id,
        }
        if spot_price_limit is not None:
            payload["spotPriceLimit"] = round(spot_price_limit, 4)

        data = await self._put(path, payload, token=token)

        if data.get("result") != "Ok":
            contract_user_state = data.get("contractUserId", "")
            if contract_user_state == "TermsNotAccepted":
                raise WayblerTermsNotAcceptedError(
                    "Terms not accepted for this contract user"
                )
            raise WayblerCarNotConnectedError(
                f"Start session failed: result={data.get('result')}, "
                f"contractUserId={contract_user_state}"
            )

        session_id = data.get("sessionId")
        if not session_id:
            raise WayblerApiError("Start session returned Ok but no sessionId")
        return session_id

    async def stop_session(self, token: str, user_id: int, session_id: int) -> None:
        """Stop a charging session."""
        path = API_SESSION_PATH.format(user_id=user_id, session_id=session_id)
        data = await self._delete(path, token=token)
        if data.get("result") != "Ok":
            raise WayblerApiError(f"Stop session failed: {data}")

    async def update_price_limit(
        self,
        token: str,
        user_id: int,
        session_id: int,
        spot_price_limit: float,
    ) -> None:
        """Update the spot price limit on an active session."""
        path = API_SESSION_PATH.format(user_id=user_id, session_id=session_id)
        payload = {"spotPriceLimit": round(spot_price_limit, 4)}
        data = await self._post(path, payload, token=token)
        if data.get("result") != "Ok":
            raise WayblerApiError(f"Update price limit failed: {data}")

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        token: str | None = None,
    ) -> dict[str, Any]:
        url = f"{API_BASE_URL}{path}"
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        _LOGGER.debug("Waybler %s %s", method, path)
        try:
            async with self._session.request(
                method,
                url,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            ) as resp:
                status = resp.status
                raw = await resp.text()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise WayblerApiError(f"{method} {path} failed: {err}") from err

        _LOGGER.debug("Waybler %s %s → HTTP %s", method, path, status)

        if status in (401, 403):
            raise WayblerAuthError(f"Unauthorized on {method} {path}")
        if status >= 500:
            raise WayblerApiError(f"{method} {path} returned HTTP {status}")
        if not raw:
            raise WayblerApiError(
                f"Empty response on {method} {path} (HTTP {status})"
            )

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as err:
            raise WayblerApiError(
                f"Non-JSON response on {method} {path} (HTTP {status})"
            ) from err

        # Some Waybler endpoints return a 200 with an error result.
        if isinstance(data, dict) and data.get("result") == "Unauthorized":
            raise WayblerAuthError(f"Unauthorized on {method} {path}")

        return data

    async def _get(self, path: str, token: str | None = None) -> dict[str, Any]:
        return await self._request("GET", path, token=token)

    async def _post(
        self,
        path: str,
        payload: dict,
        token: str | None = None,
    ) -> dict[str, Any]:
        return await self._request("POST", path, payload=payload, token=token)

    async def _put(self, path: str, payload: dict, token: str) -> dict[str, Any]:
        return await self._request("PUT", path, payload=payload, token=token)

    async def _delete(self, path: str, token: str) -> dict[str, Any]:
        return await self._request("DELETE", path, token=token)
