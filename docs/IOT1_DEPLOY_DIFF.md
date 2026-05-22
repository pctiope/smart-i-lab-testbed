# IoT1 Deploy Diff — Running vs Hardened

_What changes the moment you cut over from the running code at
`http://10.158.66.30:80` (currently 1541-line `index.js` with SQLi sites) to
the hardened code synced to `smart-i-lab-testbed`. Generated 2026-05-12._

This is the "what will my consumers see differently?" view. For the audit
narrative, see `IOT1_AUDIT.md`. For the install order, see `IOT1_REMEDIATION.md`
and `IOT1_PRE_DEPLOYMENT.md`.

---

## What stays exactly the same

API consumers (CV packages, Digital Twin, anything calling `http://10.158.66.30:80`)
will see **no breaking changes** in:

| Aspect | Verified |
|---|---|
| Host URL and port | `http://10.158.66.30:80` -- the host-side port mapping is `80:3000`, internal is 3000, external is still 80 |
| Auth header | `X-API-KEY: <key>` (case-insensitive on Express) -- old `Authorization: Bearer` is ignored harmlessly if also sent |
| Endpoint paths | All 35 routes unchanged: `/air-1`, `/air-1/:id[/light|/avg]`, `/msr-2/*`, `/smart-plug-v2/*`, `/ag-one/*`, `/zigbee2mqtt/*`, `/sensibo/*`, `/groups/*`, `/transactions`, `/users/*`, `/access/:api_key` |
| Response JSON shape | Same shape for every endpoint -- single object vs array preserved; column names preserved |
| Existing 25 API keys | Continue to work; no rotation needed |
| Time-weighted averages | `time_weight('Linear', ...)` still works (extension already installed) |

---

## What gets stricter

These are tightened by the hardening. Consumers behaving correctly today see no
change; misbehaving callers start getting `400`s or `429`s.

| Behavior | Before | After | Impact |
|---|---|---|---|
| CORS | `Access-Control-Allow-Origin: *` | Only origins in `ALLOWED_ORIGINS` env (defaults to `localhost:5500` + lab IP) | Browsers from disallowed origins blocked. **No effect on Python/curl/etc.** |
| Rate limit on `/access`, `/users*` | none | 100 req / 15 min / IP | Hit only by bursts of admin operations |
| Rate limit on POST/PUT/DELETE (any route) | none | 60 req / min / IP | Hit only by control-spam |
| Body size on POST | `100 KB` (express default) | `10 KB` | None of the existing endpoints take >10KB bodies |
| Request timeout | unbounded | 15 s server-side | A slow `time_weight` query may now return `503` instead of hanging |
| `access_level` validation | regex `/\d/.test()` (accepted `"5abc"`) | strict integer 0/1/2 | Admins can no longer save invalid levels |
| `sensData` parameter on `/<device>/:id/avg` | any string | allow-list per device family | Unknown column names return `400` |
| Sensibo `deviceID` | unchecked | regex `^[A-Za-z0-9._-]+$` | Malformed IDs return `400` |
| `POST_light` RGB / brightness | string compare (accepted `"abc"`) | `Number.isFinite()` + range | Non-numeric inputs return `400` |

---

## What gets fixed (silent behavior changes)

These are bugs that callers might have been working around or unaware of.

| Bug | Effect on callers |
|---|---|
| SQLi vectors (5 in user CRUD, 1 in `/groups` POST/PUT, all Python ingest f-strings) | None for legitimate calls; payloads now get parameterized cleanly |
| `MSR-2 /buzzer` ReferenceError on 400 path (used `device_id` instead of `deviceID`) | Was returning 500 on bad input; now correctly returns 400 |
| `MQTT_RECONNECT_PERMISSION` typo (env var name) | Reconnect period now actually reads from `MQTT_RECONNECT_PERIOD` instead of defaulting silently |
| Mixed callback + async query patterns | Old code occasionally double-sent responses; new code single-path with `await` |
| `Digital_Twin` user silently excluded from `transactions` audit log | Now appears in audit. Expect transaction-log write rate to climb -- consider a maintenance pass if storage is tight |

---

## What gets added

| Feature | What it means for callers |
|---|---|
| `GET /healthz` | Public route; returns 200 if PG pool can serve `SELECT 1`. Wire into your Docker / Kubernetes / external uptime monitor |
| OCI image LABELs | Container metadata visible via `docker inspect`; supports image governance |
| MSR-2 `radar_zone_X_occupancy` column support | Ingest validator accepts both `zone_X` and `radar_zone_X` field name conventions; future `/msr-2/:id/avg?sensData=radar_zone_3_occupancy` works |
| `helmet` security headers | `X-Content-Type-Options: nosniff`, `Strict-Transport-Security`, etc. Browsers handle automatically; CLI/Python clients ignore |

---

## What gets DIFFERENT for operators

This is the section ops cares about. Each item requires action.

| Concern | Before | After | Action |
|---|---|---|---|
| Container image | Node 18-alpine; psycopg2-binary on alpine (broken wheels); root user | Node 22-alpine; python:3.12-slim; non-root USER | `docker compose build --no-cache` after sync |
| Backend container topology | One `backend` container running 3 Python files via `run.sh` (broken `exec &` semantics) | Three single-process containers: `databridge-esp`, `databridge-zigbee`, `databridge-sensibo` | If you have monitoring that references the old name, update it. `docker compose ps` shows the new names |
| Vite dev server in prod | Yes (`vite --host --port 5500`) | No (multi-stage build -> `nginx:1.27-alpine` serves static `dist/`) | Container port mapping for `web` changed from `5500:5500` to `5500:80`. Browser URL is the same |
| Compose `.env` | Required values were empty placeholders; container would silently use undefined vars | Hard-required via `env_file: .env`; container fails fast if missing | Copy `.env.example` -> `.env` and populate (see `IOT1_PRE_DEPLOYMENT.md`) |
| Healthcheck | `wget /access/_probe || true` (always passes) | `wget /healthz` (fails on DB outage) | Healthcheck flap means real DB problem -- look at logs |
| Subscribers' DB connection | Crashes on startup if PG isn't reachable | 30 retries with exponential backoff up to 30s | Containers stay up during transient DB blips |
| MQTT CLIENT_ID | hardcoded literal `Subscriber1` / `Zigbee2MQTT to Database Code` | UUID-suffixed (`ESPSubscriber-<hex>`) | If EMQX ACLs are pinned to the literal client IDs, widen to a prefix |
| Audit / observability | `console.log` only | Same + persistent `transactions` table for every authenticated request including `Digital_Twin` | Storage on `transactions` will grow faster |

---

## Backwards-compatibility paths I left in for safety

In a few places the hardened code accepts old behavior to avoid breaking the
live lab. Worth knowing if you ever wonder "why didn't this become an error":

- **MSR-2 occupancy field names**: ingest accepts both `zone_X_occupancy`
  (schema-native) AND `radar_zone_X_occupancy` (zone5 CV-package convention).
- **Per-device tables without PK on `timestamp`**: 42 tables in the live DB
  have duplicate timestamps and can't get the PK without dedupe. The
  subscriber detects this at INSERT time and falls back to plain INSERT for
  those tables (warning logged once per table).
- **`error_logs` schema mismatch**: migration 005 doesn't try to add the
  `id BIGSERIAL PRIMARY KEY` to the existing 4.5M-row table; it only adds
  the missing `timestamp` column.
- **Old `Authorization: Bearer <key>` header**: not checked, but harmless if
  consumers continue sending it alongside `X-API-KEY`.

---

## Quick reference: what to tell each stakeholder

| Stakeholder | One-liner |
|---|---|
| API consumer dev | "Your existing API keys keep working. Same URL, same shape. Expect 400 on previously-tolerated bad inputs." |
| CV consumer dev (air1 / zone5) | "Read-only consumers are 100% compatible. Recommend `AIR1_API_TIMEOUT=12` (under the server's new 15s cap)." |
| Digital Twin dev | "Same URL. CORS now requires explicit origin allow-listing -- add yours to `ALLOWED_ORIGINS`." |
| Ops / Infra | "Three databridge containers instead of one. New `/healthz` for the healthcheck. New `.env` required. Backup before migrating." |
| Security reviewer | "All SQLi closed. CORS locked. Rate limits added. Read `IOT1_AUDIT.md` for the 46-item closure list." |

---

End of diff.
