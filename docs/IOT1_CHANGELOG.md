# IoT1 Remediation Changelog

Human-readable rollup of every change shipped as part of the IoT1 security
remediation. Pair this with `IOT1_AUDIT.md` (the "why") and `IOT1_REMEDIATION.md`
(the agent handoff with full troubleshooting). For deep audit reasoning see
`IOT1_CV_COMPATIBILITY.md` for the CV consumer compatibility check.

**Net result**: all 46 audit findings closed across 5 batches. Stack is
production-deployable from a static-analysis perspective. Live verification
deferred to the user's lab.

---

## Releases (in deploy order)

### v0.5 — Forward-compat fix
- Accept both `zone_X_occupancy` and `radar_zone_X_occupancy` MSR-2 firmware
  payload variants in the ingest schema validator (`ESPDevices_to_Database.py`).
- Same column set added to `SENSOR_COLUMNS['apollo_msr_2']` for future
  `/msr-2/:id/avg?sensData=…` queries.
- Documented `AIR1_API_TIMEOUT=12` recommendation for CV consumers.

### v0.4 — Criticals (4 audit findings → 0 open)

**Files**: `SSL-IoT1-REST/index.js`, `Smart-iLAB-Python-Files/*.py`,
`Smart-iLab_DigitalTwin/Dockerfile` + new `nginx.conf`, `compose.yaml`

- **§4.1 SQLi user CRUD + transactions**: 5 callsites parameterized; mixed
  callback handlers converted to `await`.
- **§4.2 SQLi /groups POST + PUT**: column-name allow-list, parameterized
  values; `pg-format` `%s` literal substitution removed.
- **§5.1 SQLi Python ingest**: `psycopg2.sql.SQL` + `Identifier` +
  `Placeholder`; table-name regex allow-list per file.
- **§6.3 Vite dev server in prod**: multi-stage Dockerfile, build with
  `node:22-alpine` → serve with `nginx:1.27-alpine`. Compose port `5500:80`.
- **§5.11/§8.3 Batched ingest**: in-memory queue, 1s flush, bounded to 5000
  with drop-on-overflow.
- **§4.9 Callback→await**: full sweep through remaining handlers.
- **README**: replaced "future setup script" with migrations runner instructions.

**Ops impact**: SQL injection vectors closed. DT bundle is now static and
served by nginx. Subscriber batches inserts for higher throughput.

### v0.3 — Highs (18 audit findings → 0 open)

**Files**: `index.js`, `package.json`, all three subscribers, all three
`Dockerfile`s, new `migrations/` directory, new `.dockerignore` per build
context, `compose.yaml` + `compose.dev.yaml`, DT `index.html`, `main.js`

- **§4.3 sensData allow-list**: `SENSOR_COLUMNS` dict per device type.
- **§4.6 Strict access_level**: integer + range check.
- **§4.7 Sensibo deviceID regex**: prevents URL/JSON injection into HA.
- **§4.8 pg.Client → Pool**: 20 conns, timeouts, error handler.
- **§5.2 Hypertables**: `create_hypertable()` after each `CREATE TABLE`.
- **§5.3 DB retry**: exponential backoff up to 30 attempts.
- **§5.4 UUID CLIENT_ID**: per-instance MQTT identifier.
- **§5.5 Discovery refresh**: 60s background thread re-polls registries.
- **§5.6 Payload validation**: `EXPECTED_COLUMNS` drops unknown-field
  payloads with a warning.
- **§6.4 alpine → slim**: Python base swapped; `psycopg2-binary` wheels work.
- **§6.6 Non-root USER**: `node` / `ssl` (uid 1000).
- **§6.7 Pinned tags + Node 22**: `node:18-alpine` → `node:22-alpine`.
- **§6.9 `.dockerignore`**: one per build context.
- **§7.1 Drop CDN three.js**: bundled imports via Vite.
- **§7.2 VITE_API_URL**: API URL injected at build time.
- **§8.1 Migrations layer**: 6 versioned `.sql` files + `apply.py` runner.
- **§8.2 Ingest idempotency**: PK on `timestamp` + `ON CONFLICT DO NOTHING`.
- **§6.5 Backend split**: three single-process containers
  (`databridge-{esp,zigbee,sensibo}`).

**Ops impact**: Compose now needs `.env` populated. Run
`python migrations/apply.py` after first deploy. Backend container split,
old `backend` service name gone.

### v0.2 — Mediums (15 of 18 shipped; 3 deferred to v0.4)

**Files**: `index.js`, `package.json`, all three subscribers, `compose.yaml`,
new `compose.dev.yaml`, new `.env.example`, DT `index.html`, new
`TLS_SETUP.md`, new `Caddyfile.example`

- **§4.10 helmet + rate-limit + body cap + req timeout**.
- **§4.11 Stop excluding Digital_Twin** from `transactions`.
- **§4.12 `RETURN_access_level` throws** instead of returning `-1`.
- **§4.14 `.replace` → `.replaceAll`** in MQTT publish topics.
- **§4.15 `Number.isFinite()`** numeric validation (7 sites).
- **§4.19 `sanitizePgError()`** strips schema-revealing detail from logs.
- **§5.7 Removed Zigbee unsubscribe/resubscribe race**.
- **§5.8 Bare `except:` → `except Exception:`** (4 sites).
- **§5.9 Pre-declared `database_table_name`** in Sensibo.
- **§5.10 `on_disconnect` no longer writes to DB**.
- **§6.11 Compose hardening**: restart/healthcheck/env_file/depends_on.
- **§6.12 Dev compose**: TimescaleDB + EMQX overlay.
- **§7.3 CSP** meta tag on DT.
- **§8.4 Auth cache**: 5-min TTL Map cache.
- **§8.5 TLS docs**: Caddyfile stub + setup playbook.

**Deferred to v0.4**: §5.11 (batching), §8.3 (backpressure), §4.9
(callback→await) — coupled to SQLi work, so shipped in the same batch.

**Ops impact**: `npm install` needed in `SSL-IoT1-REST/` (helmet,
express-rate-limit). Rate limits enforced; Digital Twin user now appears in
transactions table.

### v0.1 — Lows (8 of 9 shipped; 1 deferred to v0.3)

**Files**: `index.js`, `requirements.txt`, REST `Dockerfile`, `compose.yaml`,
all three `Dockerfile`s (rename + LABEL), new `OAS_DRIFT.md`, new
`.github/workflows/audit.yml`

- Removed dead imports, `var` → `const`, deleted commented-out sample SQL.
- `HOST_PORT` fallback to 3000 in `app.listen`.
- Bumped `urllib3` 2.3.0 → 2.5.0 (CVE fixes); removed duplicate `dotenv`
  package.
- REST API `EXPOSE 80` → `3000`; compose maps `80:3000`.
- `dockerfile` → `Dockerfile` (case-correct) in all three contexts.
- OCI `LABEL` metadata on all images.
- OAS drift report — 4 device families undocumented in the spec.
- CI workflow: `npm audit` × 2 + `pip-audit` + spectral OAS lint.

**Deferred**: README setup-script doc (was blocked on §8.1 migrations
existing; fulfilled in v0.4 README update).

### v0.0 — Quick wins (8 issues, ~30 min total)

**Files**: `index.js`, `requirements.txt`, `compose.yaml`, `.gitignore`,
both Node `Dockerfile`s

- Added `'use strict'` + declared implicit globals.
- Fixed `MQTT_RECONNECT_PERMISSION` typo (was reading undefined env var).
- Fixed `device_id` → `deviceID` typos in MSR-2 buzzer 400 paths.
- CORS `'*'` → `ALLOWED_ORIGINS` env allow-list.
- `requirements.txt` re-encoded UTF-16 → UTF-8 (BOM stripped).
- Compose empty `ports: ""` → real mappings; `HOST_PORT` default added.
- `.gitignore` replaced (was just `README.md`) with proper Node + Python
  + IDE + OS rules.
- `npm install` → `npm ci` in both Node `Dockerfile`s.

---

## What's new since the audit

Beyond the audit findings themselves, the remediation shipped operational
plumbing the audit did not call out:

- **`/healthz` endpoint** with DB ping (replaced the always-passing
  `wget /access/_probe || true` healthcheck).
- **`migrations/bootstrap_admin.py`** — generates and inserts the first
  admin user with a UUID API key. Idempotent.
- **`smoke_test.ps1`** at the IoT1 root — 16-check post-deploy verifier
  (health, auth, all read endpoints, 3 SQLi regressions, validation,
  CORS rejection).
- **`compose.dev.yaml`** overlay — brings up TimescaleDB + EMQX so the
  whole stack can run on a clean host.
- **`sync_iot1_to_testbed.ps1`** — copies hardened files into
  `smart-i-lab-testbed` (the lab's production copy), preserving the CV
  packages there.
- **`IOT1_CV_COMPATIBILITY.md`** — proves both CV consumer packages still
  work against the hardened API.

---

## Install order (clean slate)

```powershell
# 1. Sync hardened files into testbed (or work directly in CARE-SSL/IoT1)
pwsh C:\Users\pjtio\OneDrive\Desktop\CARE-SSL\sync_iot1_to_testbed.ps1
#    review the diff, then re-run without -WhatIf if dry-run was satisfactory

# 2. From the IoT1 directory:
cd C:\Users\pjtio\smart-i-lab-testbed   # or wherever you deploy

# 3. Populate .env from .env.example
Copy-Item .env.example .env
notepad .env

# 4. Install JS deps
cd SSL-IoT1-REST; npm ci; cd ..
cd Smart-iLab_DigitalTwin; npm ci; cd ..

# 5. Bring up the stack
docker compose -f compose.yaml -f compose.dev.yaml up -d --build

# 6. Apply migrations
python migrations\apply.py

# 7. Bootstrap admin and capture the printed API key
python migrations\bootstrap_admin.py

# 8. Smoke-test
.\smoke_test.ps1 -ApiKey "<the key from step 7>" -BaseUrl "http://localhost"

# 9. Update CV-side timeouts
Add-Content -Path "..\air1_all_zones_cv_time_features_package\web_app\.env" "AIR1_API_TIMEOUT=12"
Add-Content -Path "..\zone5_cv_time_features_package\web_app\.env" "AIR1_API_TIMEOUT=12"
```

---

## Behavior changes ops should know

- **`backend` container is gone**, replaced by three single-process
  containers (`databridge-esp`, `databridge-zigbee`, `databridge-sensibo`).
  Any monitoring/log forwarding referencing the old name needs updating.
- **REST API listens internally on 3000** (was 80). Host binding is still
  `80:3000` so external clients see no change.
- **CORS fails closed**. If `ALLOWED_ORIGINS` is empty, every cross-origin
  request is rejected. Set this to include the Digital Twin's origin.
- **Rate limits enforced**: 100 req/15min per IP on `/access`/`/users`;
  60 writes/min per IP on any POST/PUT/DELETE.
- **Auth cache TTL = 5 min**. If you change a user's `access_level`, the
  change takes effect for callers within 5 minutes (or immediately for the
  next PUT/DELETE on `/users`, which clears the cache).
- **`Digital_Twin` user now appears in `transactions`** — was previously
  skipped. Transaction table write rate goes up; consider partitioning if
  it grows fast.
- **`compose.yaml` requires `.env`** — `docker compose up` fails until you
  copy from `.env.example`.
- **MSR-2 occupancy fields**: ingest accepts both `zone_X_occupancy` and
  `radar_zone_X_occupancy`. No drop, regardless of firmware convention.

---

## Files at a glance (deliverables)

```
CARE-SSL/
├── IOT1_AUDIT.md                       # 46 findings (v2)
├── IOT1_REMEDIATION.md                 # Agent handoff
├── IOT1_CV_COMPATIBILITY.md            # CV consumer audit
├── IOT1_CHANGELOG.md                   # This file
├── sync_iot1_to_testbed.ps1            # Lab-deploy helper
└── IoT1/
    ├── .env.example
    ├── compose.yaml
    ├── compose.dev.yaml
    ├── TLS_SETUP.md
    ├── Caddyfile.example
    ├── smoke_test.ps1
    ├── README.md                       # Updated with migrations setup
    ├── .github/workflows/audit.yml
    ├── migrations/
    │   ├── 001..006_*.sql
    │   ├── apply.py
    │   └── bootstrap_admin.py
    ├── SSL-IoT1-REST/
    │   ├── Dockerfile                  # node:22-alpine, USER node
    │   ├── .dockerignore
    │   ├── index.js                    # All security fixes
    │   ├── package.json                # +helmet, +express-rate-limit
    │   └── OAS_DRIFT.md
    ├── Smart-iLab_DigitalTwin/
    │   ├── Dockerfile                  # multi-stage: node→nginx
    │   ├── .dockerignore
    │   ├── nginx.conf                  # static-serve config
    │   ├── .env.example                # VITE_API_URL
    │   ├── index.html                  # CSP header, importmap removed
    │   └── src/main.js                 # bundled `three`
    └── Smart-iLAB-Python-Files/
        ├── Dockerfile                  # python:3.12-slim, USER ssl
        ├── .dockerignore
        ├── requirements.txt            # UTF-8, urllib3 2.5.0
        ├── ESPDevices_to_Database.py   # SQLi fix, batching, hypertables
        ├── SensiboAirPro_to_Database.py
        └── Zigbee2MQTT_to_Database.py
```

End of changelog.
