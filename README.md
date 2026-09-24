# Loxone Buderus WPS / Rego1000 CAN Bridge

A local Python bridge between a Buderus WPS heat pump using a Rego1000 controller and Loxone.

The bridge reads selected Rego1000 registers and observed CAN states through SocketCAN, exposes them through a small HTTP API, and keeps the production CAN interface in **listen-only mode** except for short, controlled read windows.

> **Current safety model:** telemetry and RTR reads only. The production code contains no CAN data-frame write helper and no HTTP endpoint that writes WPS parameters.

## Tested setup

This project was developed against:

- Buderus WPS 10-1
- Rego1000 program version 3.6.0
- SocketCAN at 125 kbit/s
- 29-bit extended CAN identifiers
- Linux / Debian / DietPi
- Python 3
- Loxone Miniserver as HTTP client

Other Rego1000 installations may expose different registers, firmware behaviour, or value mappings. Verify them on your own system before relying on them.

## Architecture

```text
Buderus WPS / Rego1000
        │
        │ CAN
        ▼
SocketCAN (can0)
        │
        ▼
wps_can_bridge.py
  ├─ passive CAN listener
  ├─ controlled RTR read windows
  ├─ compact Loxone HTTP API
  ├─ optional KM200 UI enrichment
  └─ buffered change logging
        │
        ▼
      Loxone
```

## Safety boundary

The resting CAN interface is configured as `LISTEN-ONLY`.

Some useful Rego1000 values are not broadcast often enough for reliable telemetry. For those values the bridge briefly leaves listen-only mode, sends only **RTR read requests with DLC 0**, reads the response, and restores listen-only mode in a `finally` path.

The production code deliberately does not provide:

- CAN data-frame writes
- HTTP endpoints for changing WPS parameters
- cyclic writes of setpoints or persistent controller settings

Before adding any future write function, first determine whether the target register is a volatile runtime command or a persistent EEPROM/flash-backed parameter. Persistent values must never be rewritten cyclically.

## Exposed values

The validated telemetry currently includes values such as:

- domestic-hot-water temperature and setpoint
- outdoor temperature
- heating supply setpoint
- compressor state
- additional-heater state
- heating-circuit and heat-carrier pump states
- brine-pump state
- brine inlet temperature (GT10)
- brine outlet temperature (GT11)
- calculated brine delta
- heating-season state
- heating-curve parallel-offset raw values
- Extra-DHW state and related Rego1000 values
- technical weekly-DHW program registers used for further verification

Unknown registers and CAN identifiers are kept diagnostic rather than being assigned guessed meanings.

## HTTP API

Default port: `8097`.

The bind address is automatically derived from the host's primary IPv4 address unless overridden.

| Endpoint | Purpose |
|---|---|
| `GET /health` | compact bridge and CAN health |
| `GET /status` | detailed runtime status |
| `GET /loxone` | validated telemetry for Loxone |
| `GET /loxone/ui` | compact numeric UI view |
| `GET /discover` | passive discovery/diagnostic data |
| `GET /logging/status` | buffered logger status |
| `GET /circulation/status` | controlled read-only circulation diagnosis |

The `/debug/read-circulation-*` POST endpoints are also read-only: they trigger explicit RTR reads and do not write controller values.

## Optional KM200 integration

`GET /loxone/ui` can enrich the CAN data with values from a separate KM200 bridge.

By default the bridge looks for:

```text
http://<primary-local-ip>:8095/loxone
```

Override this assumption with `WPS_KM200_URL`, or set `WPS_KM200_URL=` to disable KM200 enrichment entirely. If the KM200 endpoint is unavailable, the CAN bridge itself continues to run; UI fields that depend on KM200 are reported accordingly.

The KM200 integration is optional and is not part of the CAN transport.

## Configuration

Configuration is done with environment variables.

| Variable | Default | Purpose |
|---|---|---|
| `WPS_CAN_IF` | `can0` | SocketCAN interface |
| `WPS_CAN_BITRATE` | `125000` | CAN bitrate |
| `WPS_BIND_IP` | auto-detected | HTTP bind address |
| `WPS_PORT` | `8097` | HTTP port |
| `WPS_CHANGE_LOG_DIR` | `/opt/wps-can-bridge/logs` | runtime log directory |
| `WPS_KM200_URL` | local host on port 8095 | optional KM200 Loxone endpoint |
| `WPS_KM200_TIMEOUT_S` | `1.0` | KM200 HTTP timeout |

A systemd-friendly template is included as `wps-can-bridge.env.example`.

The supplied unit works with the automatic defaults and does not require a configuration file. To use persistent overrides, copy the required values to `/etc/default/wps-can-bridge` and add a systemd drop-in:

```ini
[Service]
EnvironmentFile=-/etc/default/wps-can-bridge
```

Then run `systemctl daemon-reload` and restart the service.

## Installation

Example layout:

```text
/opt/wps-can-bridge/
├── wps_can_bridge.py
├── wps-can-bridge.service
└── logs/
```

Required host components:

- Python 3
- SocketCAN support
- `iproute2` / the `ip` command
- a supported CAN adapter

The supplied service unit runs as root because the bridge switches the SocketCAN interface between listen-only and active RTR-read mode. If you use another privilege model, it must still permit the required `ip link` operations without granting unnecessary write access elsewhere.

Install the unit according to your distribution, then verify:

```bash
systemctl status wps-can-bridge.service
curl http://<bridge-host>:8097/health
```

After controlled reads, also verify that the CAN interface has returned to listen-only mode.

## Logging

Runtime change logs are intentionally excluded from Git.

The logger:

- records changes instead of every raw frame
- buffers writes to reduce storage wear
- rotates data by day
- removes old files after the configured retention period
- stores low-rate operating snapshots for later analysis

## Loxone

For normal integration use `GET /loxone` or the presentation-oriented `GET /loxone/ui`.

The bridge does not require Loxone credentials and does not open a connection to the Miniserver. Loxone simply polls the HTTP endpoint.

## Known limits

- Register mappings are specific to the tested Rego1000 environment unless stated otherwise.
- Direct-state CAN mappings marked provisional in the source should be verified against real state changes before being treated as universal.
- Two physical brine loops cannot be distinguished if the WPS only exposes the common GT10/GT11 sensors.
- Active control over CAN is intentionally not implemented yet.

## Safety notice

Heating controls can affect equipment operation, hot-water temperatures, frost protection, and other safety-relevant functions. Treat unknown registers as unknown. Do not infer writable values from read-only observations.

## License

MIT License. See [LICENSE](LICENSE).
