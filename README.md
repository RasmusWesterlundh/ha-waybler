# Waybler EV Charging — Home Assistant Integration

A custom Home Assistant integration for [Waybler](https://waybler.com/) EV chargers using the Waybler WebSocket API for real-time updates and a price optimizer for smart charging.

## Features

- **Real-time charging state** via WebSocket — no polling
- **Price optimizer** — automatically starts charging at the cheapest hours using multiple strategies
- Start/stop a charging session from HA
- VAT-aware price limit handling (Waybler API uses excl. VAT values)
- Sensors: session energy (kWh), live power (W), session ID, station state, computed price limit, daily charge time
- Binary sensor: car connected
- Optional manual price limit override

## Entities

| Entity | Type | Description |
|--------|------|-------------|
| `switch.waybler_ev_charger_price_optimization` | Switch | Enable / disable the price optimizer |
| `switch.waybler_ev_charger_charging` | Switch | Start / stop charging (turn on triggers optimizer) |
| `sensor.waybler_ev_charger_station_state` | Sensor | Station state (Busy / EvConnected / NoEv / Available) |
| `sensor.waybler_ev_charger_session_energy` | Sensor | Energy delivered this session (kWh) |
| `sensor.waybler_ev_charger_power` | Sensor | Live power (W) |
| `sensor.waybler_ev_charger_session_id` | Sensor | Active session ID |
| `sensor.waybler_ev_charger_computed_price_limit` | Sensor | Price limit the optimizer last calculated (incl. VAT) |
| `sensor.waybler_ev_charger_charge_time_today` | Sensor | Total charge time today (hours) |
| `binary_sensor.waybler_ev_charger_car_connected` | Binary sensor | True when a car is plugged in |
| `number.waybler_ev_charger_spot_price_limit` | Number | Manual price limit override (disabled by default) |

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
| Spot price sensor *(optional)* | Leave empty to use the price data built into the integration |

### Finding your IDs

Enable debug logging and look for the `ChargeZoneModel` message in the HA log — it lists all stations in your zone with their IDs.

```yaml
# configuration.yaml
logger:
  logs:
    custom_components.waybler: debug
```

## Price Optimizer

When the car connects, the optimizer automatically computes a price limit and starts a session. Configure it under **Settings → Integrations → Waybler → Configure**:

| Option | Description |
|--------|-------------|
| Auto-start when car connects | Enable/disable automatic session start on plug-in |
| Charging strategy | How the price limit is calculated (see below) |
| Target charge hours | *Cheapest hours*: how many hours of charging to cover |
| Price percentile | *Percentile*: only charge when price is in the cheapest N% |
| Fixed price ceiling | *Fixed ceiling*: maximum price to accept (EUR/kWh, excl. VAT) |

### Strategies

- **Cheapest hours** — picks the N cheapest upcoming hours and charges only then
- **Below average** — charges whenever the current price is below today's average
- **Percentile** — charges when the price falls within the cheapest N% of today's prices
- **Fixed ceiling** — charges as long as the spot price is below a fixed value

### Manual override

Enable the `Manual price limit` number entity (disabled by default) and set a value (incl. VAT, SEK/kWh). When set to any value above 0, the optimizer is bypassed and this limit is used directly.

## How it works

On startup the integration opens a persistent WebSocket connection to `wss://api.waybler.com/v7/app/websocket`. The server pushes:

- `ChargeZoneModel` — station state and price schedule for your zone
- `ChargeSessionModel` — active session on connect
- `SessionUpdatedEvent` — live power and energy updates during charging

Write operations (start/stop session, update price limit) use the Waybler REST API. The `spotPriceLimit` API field is excl. VAT — the integration converts all-in prices automatically using the zone's `consumptionVatRate`.

## Requirements

- Home Assistant 2024.1+
- A Waybler account with an active contract

## License

MIT
