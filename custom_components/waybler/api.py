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
    JWT_USER_ID_CLAIM,
    REQUEST_TIMEOUT,
)

_LOGGER = logging.getLogger(__name__)


class WayblerApiError(Exception):
    """Raised on non-auth API errors."""


class WayblerAuthError(WayblerApiError):
    """Raised when authentication fails."""


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
        """Log in via subprocess curl and return (token, user_id)."""
        body = json.dumps({"email": email, "password": password})
        url = f"{API_BASE_URL}{API_LOGIN_PATH}"
        cmd = [
            "curl", "-s",
            "-X", "POST",
            url,
            "-H", "Content-Type: application/json; charset=utf-8",
            "--data-raw", body,
        ]
        _LOGGER.error(
            "Waybler login REQUEST: url=%s | headers=[Content-Type: application/json; charset=utf-8] | body=%s",
            url, body,
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        except Exception as err:
            raise WayblerApiError(f"curl subprocess failed: {err}") from err

        raw = stdout.decode().strip()
        err_out = stderr.decode().strip()
        _LOGGER.error(
            "Waybler login RESPONSE: returncode=%s | stdout=%s | stderr=%s",
            proc.returncode, raw, err_out,
        )

        if not raw:
            raise WayblerApiError("curl returned empty response")

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as err:
            raise WayblerApiError(f"curl response not JSON: {raw!r}") from err

        _LOGGER.error("Waybler login PARSED JSON: %s", data)

        if data.get("result") != "Ok":
            raise WayblerAuthError(
                f"Login failed: result={data.get('result')}, "
                f"email={data.get('email')}, password={data.get('password')}"
            )

        token = data["token"]
        user_id = _decode_jwt_user_id(token)
        return token, user_id

    async def refresh_token(self, current_token: str) -> str:
        """Refresh a token and return the new one.

        Raises WayblerAuthError if the token is expired/invalid.
        """
        data = await self._get(API_REFRESH_PATH, token=current_token)
        if data.get("result") != "Ok":
            raise WayblerAuthError("Token refresh failed")
        return data["token"]

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
                raise WayblerApiError("Terms not accepted for this contract user")
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
    # HTTP helpers — all requests via curl subprocess
    # ------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        token: str | None = None,
    ) -> dict[str, Any]:
        url = f"{API_BASE_URL}{path}"
        cmd = ["curl", "-s", "-X", method, url,
               "-H", "Content-Type: application/json; charset=utf-8"]
        if token:
            cmd += ["-H", f"Authorization: Bearer {token}"]
        if payload is not None:
            cmd += ["--data-raw", json.dumps(payload)]

        _LOGGER.debug("Waybler %s %s", method, path)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=REQUEST_TIMEOUT)
        except Exception as err:
            raise WayblerApiError(f"curl failed on {method} {path}: {err}") from err

        raw = stdout.decode().strip()
        _LOGGER.debug("Waybler %s %s → rc=%s body=%s", method, path, proc.returncode, raw)

        if proc.returncode != 0 or not raw:
            raise WayblerApiError(f"curl error on {method} {path}: rc={proc.returncode} stderr={stderr.decode().strip()}")

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as err:
            raise WayblerApiError(f"Non-JSON response on {method} {path}: {raw!r}") from err

        # Map HTTP-like error signals in the response
        # curl -s doesn't give us the HTTP status code unless we ask
        # Re-run with -w to get status if needed — for now detect auth errors by result field
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
        authenticated: bool = True,
    ) -> dict[str, Any]:
        return await self._request("POST", path, payload=payload, token=token)

    async def _put(self, path: str, payload: dict, token: str) -> dict[str, Any]:
        return await self._request("PUT", path, payload=payload, token=token)

    async def _delete(self, path: str, token: str) -> dict[str, Any]:
        return await self._request("DELETE", path, token=token)


