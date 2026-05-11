# Waybler EV Charging — Home Assistant Integration

A custom Home Assistant integration for [Waybler](https://waybler.com/) EV chargers using the Waybler WebSocket API for real-time updates.

## Features

- **Real-time charging state** via WebSocket — no polling
- Start/stop a charging session from HA
- Set a spot price limit on an active session
- Sensors: session energy (kWh), live power (W), session ID, station state
- Binary sensor: car connected
- Number entity: spot price limit

## Entities

| Entity | Type | Description |
|--------|------|-------------|
| `switch.waybler_ev_charger_charging` | Switch | Start / stop charging |
| `sensor.waybler_ev_charger_session_energy` | Sensor | Energy delivered this session (kWh) |
| `sensor.waybler_ev_charger_session_power` | Sensor | Live power (W) |
| `sensor.waybler_ev_charger_session_id` | Sensor | Active session ID |
| `sensor.waybler_ev_charger_station_state` | Sensor | Station state (Busy / NoEv / Available) |
| `binary_sensor.waybler_ev_charger_car_connected` | Binary sensor | True when a car is connected |
| `number.waybler_ev_charger_spot_price_limit` | Number | Spot price limit (SEK/kWh) |

## Installation via HACS

1. Open HACS → Integrations → ⋮ → Custom repositories
2. Add `https://github.com/RasmusWesterlundh/ha-waybler` as an **Integration**
3. Search for "Waybler" and install
4. Restart Home Assistant
5. Go to **Settings → Devices & Services → Add Integration** → search "Waybler"

## Manual Installation

Copy `custom_components/waybler/` into your HA `config/custom_components/` directory and restart.

## Configuration

The config flow will prompt for:

| Field | Where to find it |
|-------|-----------------|
| Email & password | Your Waybler account credentials |
| Station ID | From the Waybler app — your parking spot's station ID |
| Contract user ID | From the Waybler app — your contract user ID |
| Zone ID | From the Waybler app — your zone ID |
| Spot price sensor *(optional)* | A HA sensor entity with a numeric electricity price |

### Finding your IDs

The easiest way is to enable debug logging for this integration and look for the `ChargeZoneModel` message in the HA log — it lists all stations in your zone with their IDs.

```yaml
# configuration.yaml
logger:
  logs:
    custom_components.waybler: debug
```

## How it works

On startup the integration opens a persistent WebSocket connection to `wss://api.waybler.com/v7/app/websocket`. The server pushes:

- `ChargeZoneModel` — station state for your parking spot (Busy / NoEv / Available)
- `ChargeSessionModel` — initial active session on connect
- `SessionUpdatedEvent` — live power and energy updates during charging

Write operations (start/stop session, update price limit) use the Waybler REST API.

## Requirements

- Home Assistant 2024.1+
- A Waybler account with an active contract

## License

MIT
