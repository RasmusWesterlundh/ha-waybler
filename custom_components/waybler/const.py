"""Constants for the Waybler integration."""

DOMAIN = "waybler"

# API
API_BASE_URL = "https://api.waybler.com/v7"
API_LOGIN_PATH = "/app/authenticate/login"
API_REFRESH_PATH = "/app/authenticate/refresh"
API_SESSIONS_CHARGE_PATH = "/{user_id}/sessions/charge"
API_SESSION_PATH = "/{user_id}/sessions/{session_id}"

# WebSocket
WS_URL = "wss://api.waybler.com/v7/app/websocket"
WS_APP_UUID = "8d0a2cfa-4373-43e2-951a-8bff7c25d4d7"

# Request timeout (seconds)
REQUEST_TIMEOUT = 15

# Coordinator poll interval (seconds)
DEFAULT_SCAN_INTERVAL = 30

# Config entry keys
CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_USER_ID = "user_id"
CONF_STATION_ID = "station_id"
CONF_CONTRACT_USER_ID = "contract_user_id"
CONF_ZONE_ID = "zone_id"
CONF_PRICE_SENSOR = "price_sensor"

# Runtime data keys (stored in coordinator / options, never in the main config)
CONF_TOKEN = "token"

# JWT claim key for user ID
JWT_USER_ID_CLAIM = "http://schemas.microsoft.com/ws/2008/06/identity/claims/userdata"

# Platforms
PLATFORMS = ["binary_sensor", "number", "sensor", "switch"]
