# IoT1

## Project Description

A testbed developed for the Smart i-LAB Project's IoT Platform located at the University of the Philippines - Diliman Campus' Electrical and Electronics Engineering Institute (EEEI) Room 308. This is implemented through RESTful API standard web-based access points for sensor data acquisition and device controls supported by a TimescaleDB (PostgreSQL-based) database, Python databridge APIs, an EMQX Broker (MQTT-based communication protocol), and Home Assistant configuration and device access tools. Included with the project is a web-based Digital visualization or _Digital Twin_ of the Smart i-LAB, reflecting the status of, as well as providing some level of basic control to, some of the functionalities within the lab. This functions both as an aid to remote experimentation/testing and serves as a prime example of the capabilities/functionalities of the testbed since the Digital Twin will be entirely dependent on the RESTful API's endpoints for its core functionalities.

## How to Use the Repository (Subject to Changes)

### Pre-Requisites
- Docker
  - Docker CLI or Docker Desktop
  - Engine and Compose
- Database
  - PostgreSQL with the TimescaleDB and timescaledb_toolkit extensions
  - Schema is managed by `migrations/apply.py`. Run it once against a fresh database
    (and after every code update) to apply versioned `.sql` files in `migrations/`:
    ```bash
    # From the IoT1 directory, with .env populated:
    python3 -m pip install psycopg2-binary python-dotenv
    python3 migrations/apply.py
    ```
    What it creates:
    - `users`, `transactions`, `groups`, `error_logs` — auth, audit, grouping, error logging
    - `apollo_air_1`, `apollo_msr_2`, `athom_smart_plug_v2`, `airgradient_one`,
      `sensibo`, `zigbee2mqtt` — device registry tables (one row per device)
    - Per-device sensor tables (`apollo_air_1_<id>`, etc.) are created lazily by the
      Python subscribers on first device discovery and converted to TimescaleDB
      hypertables with `PRIMARY KEY (timestamp)` automatically.
    - Migration `006_per_device_table_pk_and_hypertable.sql` retrofits existing per-device
      tables that were created by older versions of the subscriber.
  - Note: Errors *WILL* occur if device naming conventions don't follow the
    `{device-name-separated-by-dashes}-{id}` pattern (see "Configured Devices" below).
- EMQX
  - EMQX Open Source
  - Necessary setup made (with username, password, etc)
  - Note: Paid version with a PostgreSQL database integration exists rendering some Python API databridges obsolete at the potential cost of TimeScaleDB functionalities (used in some REST API endpoints) and data distribution control
- Configured Devices
  - Integrated Devices:
    - apollo air 1
    - apollo msr 2
    - athom smart plug v2
    - airgradient one
    - sensibo air pro
    - zigbee devices
  - Configuration References:
    - [ESP Devices](https://github.com/Julius-Ipac/Smart-iLAB)
    - Zigbee2MQTT(placeholder)
  - Specifications
    - Naming: {device-name-separated-by-dashes}-{id}
      - '-' replaced by '_' in database
      - append '/data' to MQTT topic name in Python API Bridge and for the device's MQTT publish configuration
      - append '/{actuator}' (e.g. light, buzzer, etc) to device subscriptions for actuator commands coming from REST API

### Docker Compose

The project runs on Linux Ubuntu 24.04.2 LTS but works on any host with Docker
(see [why Docker](https://www.docker.com/why-docker/)). Three services are shipped:
the Express REST API (`webapp`), the Digital Twin (`web`, nginx-served static bundle),
and three single-process Python subscribers (`databridge-esp`, `databridge-zigbee`,
`databridge-sensibo`).

Production usage (external Postgres/EMQX/Home Assistant):

```bash
cp .env.example .env       # fill in DB/MQTT/HA values
docker compose up --build -d
python3 migrations/apply.py
```

Local development (with Postgres + EMQX colocated):

```bash
cp .env.example .env
docker compose -f compose.yaml -f compose.dev.yaml up --build -d
python3 migrations/apply.py
```

For TLS termination in front of the REST API and Digital Twin, see
[`TLS_SETUP.md`](TLS_SETUP.md).

## Zone 5 Occupancy Monitor — Vite Frontend

A browser-based live dashboard for the Zone 5 CNN-1D occupancy model, located at [zone5_cv_time_features_package/web_app_vite/smart-ilab-zone5/](zone5_cv_time_features_package/web_app_vite/smart-ilab-zone5/).

### Stack

| Tool | Role |
|---|---|
| [Vite 8](https://vitejs.dev/) | Dev server + production bundler |
| [Tailwind CSS v4](https://tailwindcss.com/) | Utility-first styling via `@tailwindcss/vite` plugin |
| [Plotly.js](https://plotly.com/javascript/) | Historical predictions, MSE, and sensor sparkline charts |

### What the dashboard shows

- **Occupancy probability** — live EMA-smoothed CNN-1D model output streamed via SSE (`/api/stream`), displayed as a large 4-decimal readout with OCCUPIED / VACANT / ELEVATED badges
- **CV ground-truth panel** — person count and occupied/vacant label from the YOLO camera feed, used to validate model predictions in real time
- **Historical predictions chart** — scrolling time-series of model probability overlaid on CV ground-truth occupancy bands (teal = occupied, red = unoccupied), polled every 10 s from `/api/history`
- **MSE chart** — per-tick squared error between predicted probability and binary ground truth, with rolling average
- **Environment conditions panel** — CO₂, temperature, humidity, VOC, PM2.5, NOx, and motion from four devices (AIR-1, MSR-2, Sensibo HVAC, AirGradient One), each with Plotly sparklines; collapsed by default
- **Split-pane layout** — draggable resize handle between the YOLO video feed and the readout panel; width persisted in `localStorage`

### Pre-requisites

- Node.js 18+ and npm
- Zone 5 Python backend running and reachable (provides `/api/stream`, `/api/history`, `/api/health`, `/api/video.mjpg`)
- Smart I-Lab IoT REST API reachable (provides `/env-api/*` sensor readings)

### Setup

**1. Install dependencies**

```bash
cd zone5_cv_time_features_package/web_app_vite/smart-ilab-zone5
npm install
```

**2. Configure environment**

Create a `.env` file in `smart-ilab-zone5/` (never committed — see `.gitignore`):

```dotenv
# API key injected into every proxied request as x-api-key
VITE_API_KEY=your-api-key-here
VITE_INFERENCE_API_URL=smart-ilab-inference-zone5-api-url
VITE_ILAB_API_URL=your-ilab-api-url
```

The dev-server proxy targets are set in [vite.config.js](zone5_cv_time_features_package/web_app_vite/smart-ilab-zone5/vite.config.js):

| Prefix | Forwarded to | Purpose |
|---|---|---|
| `/api` | `VITE_INFERENCE_API_URL` | Zone 5 occupancy backend |
| `/env-api` | `VITE_ILAB_API_URL` | Smart I-Lab IoT REST API |

Update these addresses in `vite.config.js` if your backend is on a different host.

**3. Run the dev server**

```bash
npm run dev
```

The dashboard opens at `http://localhost:5173` by default.

**4. Production build**

```bash
npm run build   # outputs to dist/
npm run preview # serves dist/ locally for a final check
```

The build is configured with manual chunk splitting so Plotly ships as a separate chunk and the Rollup 5 MB warning is not triggered.

### Proxy and API key

All requests that begin with `/api` or `/env-api` are forwarded by the Vite dev server. The `x-api-key` header is injected automatically from `VITE_API_KEY` on every proxied request — you do not need to handle it in the frontend code.

---

## Limitations
*To add: Customizability issues particularly in naming or structures and possible errors to look out for due to lack of complete error catching, limitations set by the developers of the tools used, etc* 
### Database
- Specific naming standard, also has to be in sync with the databridge
### REST API
- `time_weight` queries depend on the `timescaledb_toolkit` extension (installed by
  migration 001). On plain Postgres they will fail with `function ... does not exist`.
### EMQX
- Update Frequency: Limited to the update frequency set during device configuration
  - The project was implemented with a 0.1Hz data update frequency + datapoints produced by changes in device actuator changes
- For more information on other limitations such as connection and topic limits, refer to [this](https://docs.emqx.com/en/emqx/latest/getting-started/restrictions.html).
### Python API Bridges
- The data distribution algorithm for MQTT devices only works with the specific naming
  standard above. Topic and table names are derived deterministically and validated
  against an allow-list at runtime — messages on unknown topics are dropped with a
  warning log.
- ESP algorithm converts from MQTT topic `device_name_id/data` to database table
  `device_name_id`; subscribed device types are listed in the `TOPIC` array.
- Zigbee algorithm converts from MQTT topic `zigbee2mqtt/<id>/set` to database table
  `zigbee2mqtt_<id>`; devices are read from the `zigbee2mqtt` registry table.
- Subscribers re-poll the registry every 60s, so new devices get subscribed without a
  container restart.
