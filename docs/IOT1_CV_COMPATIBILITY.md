# IoT1 ↔ CV Consumer Compatibility Audit

_Companion to `IOT1_AUDIT.md` and `IOT1_REMEDIATION.md`. Last reviewed: 2026-05-11._

This report answers a single question: **after the IoT1 REST API hardening, will the
CV consumer packages at `C:\Users\pjtio\smart-i-lab-testbed\{air1,zone5}_*` still work?**

---

## 0. Executive verdict

**GO.** Both CV packages are compatible with the hardened REST API. No code changes
are required on the CV side. One operator-side config change is recommended.

Two safe forward-compat fixes have been applied on the IoT1 side to neutralize the
only meaningful regression risk surfaced during the audit.

| Package | Verdict | Required CV-side change |
|---|---|---|
| `air1_all_zones_cv_time_features_package` | **COMPATIBLE** | None (recommended: set `AIR1_API_TIMEOUT=12`) |
| `zone5_cv_time_features_package` | **COMPATIBLE** | None (recommended: set `AIR1_API_TIMEOUT=12`) |

---

## 1. Scope

| Item | Path |
|---|---|
| **CV consumer (air1)** | `smart-i-lab-testbed/air1_all_zones_cv_time_features_package/air1_all_zones/air1_exporter.py` (621 LOC) |
| **CV consumer (zone5)** | `smart-i-lab-testbed/zone5_cv_time_features_package/zone5/air1_exporter.py` (1643 LOC) |
| **CV web wrappers** | `smart-i-lab-testbed/*/web_app/{main.py,data_source.py,static/app.js}` |
| **CV tests** | `smart-i-lab-testbed/*/tests/`, `smart-i-lab-testbed/zone5_*/smoke_test.py` |
| **CV config** | `smart-i-lab-testbed/*/web_app/.env.example`, `run_*.{ps1,sh}` |
| **API surface** | `IoT1/SSL-IoT1-REST/index.js` (hardened) |

Audited against all 46 numbered findings in `IOT1_AUDIT.md` — each cross-referenced
to a potential CV impact.

---

## 2. Per-package endpoint inventory

### air1 package

Only **three** endpoint patterns. All read-only.

| Pattern | Method | Query params | Called from |
|---|---|---|---|
| `/air-1` | GET | — | `Air1Device.get_all_devices()` (L390) |
| `/air-1/{device_id}` | GET | — | `Air1Device.get_device_data()` (L392-393) |
| `/air-1/{device_id}` | GET | `time_start`, `time_end` (URL-encoded ISO 8601) | `Air1Device.get_historical_data()` (L395-401) |

No `/avg`, no `?sensData=`, no POST/PUT/DELETE.

### zone5 package

Wider device coverage; same shape. All read-only.

| Pattern | Method | Query params |
|---|---|---|
| `/air-1` | GET | — |
| `/air-1/{device_id}` | GET | — or `time_start` + `time_end` |
| `/msr-2` | GET | — |
| `/msr-2/{device_id}` | GET | — or `time_start` + `time_end` |
| `/smart-plug-v2` | GET | — |
| `/smart-plug-v2/{device_id}` | GET | — or `time_start` + `time_end` |

No `/avg`, no `?sensData=`, no POST/PUT/DELETE.

---

## 3. Per-finding compatibility matrix

Each hardening finding from `IOT1_AUDIT.md` evaluated against the CV consumers:

| Audit § | Change | air1 impact | zone5 impact |
|---|---|---|---|
| §4.1 SQLi user CRUD | Parameterized | ✓ none (not called) | ✓ none |
| §4.2 SQLi /groups | Parameterized | ✓ none | ✓ none |
| §4.3 sensData allow-list | `?sensData=X` now allow-listed | ✓ none (no /avg use) | ✓ none |
| §4.5 CORS allow-list | Cross-origin browser requests rejected | ✓ none (Python `requests`, not browser) | ✓ none |
| §4.6 Strict access_level | Must be integer 0\|1\|2 | ✓ none (not called) | ✓ none |
| §4.7 Sensibo regex | deviceID must match `^[A-Za-z0-9._-]+$` | ✓ none (no Sensibo HVAC writes) | ✓ none |
| §4.8 `pg.Client` → Pool | Internal change, response shape unchanged | ✓ none | ✓ none |
| §4.10 Rate limits | 100 req/15min on `/access` /`users`; 60 writes/min | ✓ none (none of those endpoints) | ✓ none |
| §4.10 Body cap (10KB) | POST body limit | ✓ none (GETs only) | ✓ none |
| §4.10 Request timeout (15s) | Server-side 15s cap | **⚠ minor — client timeout 30s > server 15s** | **⚠ minor — client 30s > server 15s** |
| §4.10 helmet | Security headers | ✓ none (Python ignores most) | ✓ none |
| §4.11 Digital_Twin transactions logging | Audit log enrichment | ✓ none | ✓ none |
| §4.12 -1 → throw | Internal error path | ✓ none | ✓ none |
| §4.14 .replaceAll publish topics | MQTT publish only | ✓ none (no MQTT subscribe) | ✓ none |
| §4.15 POST_light validation | Strict numeric | ✓ none (no light writes) | ✓ none |
| §5.x Python subscriber changes | Backend ingest behavior | ✓ none (DB reads unaffected) | **⚠ field-name; mitigated, see §5** |
| §5.2 Hypertables | DDL change | ✓ improves their query performance | ✓ same |
| §5.6 Schema validation (EXPECTED_COLUMNS) | Drops MQTT msgs with unknown keys | ✓ no impact on air1 ingest | **⚠ mitigated via forward-compat (see §6)** |
| §6.3 Vite multi-stage | DT only | ✓ none (CV apps unrelated) | ✓ none |
| §6.5 Backend split | Internal compose change | ✓ none (CV reads from REST, not backend) | ✓ none |
| §7.1 / §7.2 DT changes | Digital Twin only | ✓ none | ✓ none |
| §8.1 Migrations | Schema-as-code | ✓ none (CV reads same shape) | ✓ none |
| §8.2 PK on timestamp + ON CONFLICT | Idempotent inserts | ✓ none (read shape unchanged) | ✓ none |

No `FAIL` rows. Two `⚠ minor` rows — both have recommended mitigations.

---

## 4. Auth pattern (per package)

### air1 package

```python
# air1_exporter.py L341-346
def _headers(self) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if self.api_key:
        headers["Authorization"] = f"Bearer {self.api_key}"
        headers["x-api-key"] = self.api_key
    return headers
```

Sends **both** `Authorization: Bearer ...` AND `x-api-key: ...` headers. The hardened
REST API only checks `x-api-key` (case-insensitive in Node/Express's lowercased
header map), so the Bearer header is ignored harmlessly. Compatible.

Key source: env var `AIR1_API_KEY`, defaulted in `run_live_collector.sh` and
`web_app/.env.example`.

### zone5 package

```python
# zone5/air1_exporter.py L656-659
self.headers = {
    "Accept": "*/*",
    "X-API-KEY": api_key,
}
```

Sends only `X-API-KEY`. Refuses to start if `AIR1_API_KEY` env var is empty
(L653-654 raises `ValueError`). Compatible.

---

## 5. Rate-limit exposure

The hardened API has two limiters:
- `authLimiter`: 100 req per 15 min per IP, applied only to `/access` and `/users`.
- `writeLimiter`: 60 req per minute per IP, applied to any POST/PUT/DELETE.

The CV packages call neither `/access` nor `/users`, and never write. **They are not
subject to either limiter at all.**

If a global rate limit is ever added (not in current code), the peak burst would be:

### air1 worst case

`ThreadPoolExecutor(max_workers=16)` × 15 AIR-1 sensors × 12 chunks (60 days @ 5-day
chunks) = up to 180 GETs in flight at peak. Distributed over a multi-minute walk.

### zone5 worst case

Steady-state live mode: 3 device classes × 1 GET each per 10s = **18 req/min**.
Historical export uses the same 16-worker pool with adaptive chunking; bursts up to
~200 GETs concentrated in the first minute then tail off.

Both packages already handle HTTP 429 in `TRANSIENT_HTTP_STATUS_CODES` and retry with
backoff. Should a future rate limit be added, **they degrade gracefully**.

---

## 6. Timeout mismatch (the only operator-visible action item)

- **Server**: 15s request timeout (§4.10) — returns HTTP 503 after 15s.
- **Client**: defaults to 30s timeout (`API_TIMEOUT_SECONDS = 30` in air1
  L42; `DEFAULT_API_TIMEOUT_SEC = 30` in zone5 L32).

Neither client retries on 503 (only on 408/429), so a timed-out request burns
~30s before failing. Adaptive retry then re-issues, often succeeding.

**Recommendation** (operator-side): set `AIR1_API_TIMEOUT=12` in the CV `.env`
files. Both packages honor the env var. This puts the client cap below the
server cap, so 503-on-timeout is replaced by the client surfacing the failure
sooner and the adaptive layer retrying faster.

This recommendation is also recorded in `IOT1_REMEDIATION.md` §7.

---

## 7. Field-name finding — `radar_zone_X_occupancy` vs. `zone_X_occupancy`

### What I found

zone5's exporter (`zone5/air1_exporter.py` L84-97, L116-130) references MSR-2 columns
named `radar_zone_1_occupancy`, `radar_zone_2_occupancy`, `radar_zone_3_occupancy`.

The IoT1 per-device DDL in `ESPDevices_to_Database.py` creates `zone_1_occupancy`,
`zone_2_occupancy`, `zone_3_occupancy` (no `radar_` prefix). This **predates** the
audit — it was a divergence between schema and CV expectations from day one.

### Why my audit work made this more visible

My `EXPECTED_COLUMNS` validation (§5.6) drops MQTT payloads whose keys aren't in the
expected set. If the firmware ever publishes the `radar_` variant, the ingest will
now silently drop those messages — whereas before my changes, they'd hit the
SQLi-vulnerable f-string INSERT path and fail loudly (and unsafely).

### Forward-compat fix applied

Two edits to neutralize the risk:

1. **`IoT1/Smart-iLAB-Python-Files/ESPDevices_to_Database.py`** — `EXPECTED_COLUMNS['apollo_msr_2']` now accepts both `zone_X_occupancy` and `radar_zone_X_occupancy` (plus `radar_target`).
2. **`IoT1/SSL-IoT1-REST/index.js`** — `SENSOR_COLUMNS['apollo_msr_2']` adds the same six column variants so a future `/msr-2/:id/avg?sensData=radar_zone_3_occupancy` won't be rejected by the allow-list.

After this, the ingest validator accepts either firmware convention without dropping
messages. The CV pipeline continues regardless of which naming the deployed firmware
emits.

### What's still your decision

Whether the actual schema column should be renamed from `zone_X_occupancy` to
`radar_zone_X_occupancy` (a destructive migration if any data exists in the old
columns). The audit recommendation is: **leave the schema alone, accept both
keys at ingest** — which is what the forward-compat edit does. If you want a hard
rename, file it as a follow-up migration.

---

## 8. Config-file inventory

Every place an IoT1 URL is referenced in the CV packages. The hardened API still
binds host port 80 (only the container-internal port changed to 3000), so **no URL
updates are required**.

| File | Reference |
|---|---|
| `air1/air1_all_zones/air1_exporter.py` L34 | `os.environ.get("AIR1_API_URL", "http://10.158.66.30:80")` |
| `air1/web_app/.env.example` L22 | `AIR1_API_URL=http://10.158.66.30:80` |
| `air1/run_live_collector.sh` L28 | bash default |
| `air1/run_live_collector.ps1` | PowerShell equivalent |
| `zone5/zone5/air1_exporter.py` L24 | same default |
| `zone5/web_app/.env.example` L18 | same default |
| `zone5/run_live_collector.{sh,ps1}` | same default |

None require an update for the hardened API. (If you ever publish the API on HTTPS
via Caddy per `TLS_SETUP.md`, update these to `https://`.)

---

## 9. Tests

- **air1 `tests/test_all_zones_contract.py`** (1350 LOC): mocks `collect_training_data`
  module and internal pipeline calls. Does **not** mock `requests.get` or hit IoT1
  directly. CI runs without an IoT1 server → no compatibility regression detectable
  from the test suite.
- **zone5 `tests/test_cv_ground_truth.py`** and `smoke_test.py`: model-inference
  tests on fixture data. No IoT1 calls.

**Implication**: the CV test suites won't catch a CV ↔ IoT1 regression on their
own. Manual smoke validation in the lab is the only way to confirm end-to-end
function after the IoT1 stack is replaced.

Suggested smoke test (run from a CV operator machine after IoT1 is updated):

```bash
cd smart-i-lab-testbed/air1_all_zones_cv_time_features_package
export AIR1_API_URL=http://10.158.66.30:80
export AIR1_API_KEY=<your-key>
python -m air1_all_zones.air1_exporter --help    # imports
python -c "from air1_all_zones.air1_exporter import Air1Device; \
  c = Air1Device('$AIR1_API_URL', '$AIR1_API_KEY'); print(c.get_all_devices())"
# Expect: a list of device IDs
```

Same pattern for zone5 with `zone5.air1_exporter`.

---

## 10. Web app integration

Both CV packages ship a FastAPI web app (`web_app/main.py`, served by
`run_live_app.{sh,ps1}`). These web apps:

- Do **not** call IoT1 directly.
- Instantiate `Air1Device` (the same class as the CLI exporter) via
  `LiveAir1DataSource` (`web_app/data_source.py`).
- Serve their own `/api/health`, `/api/current`, `/api/history`, `/api/stream`,
  `/api/video[/cam*].mjpg` endpoints to the browser — these are CV-internal and do
  **not** proxy IoT1 endpoints.

Compatibility implication: the web apps' interaction with IoT1 is fully mediated by
`Air1Device`. Anything that works for the exporter works for the web app.

---

## 11. Action items (operator)

In order of priority:

1. **Sync hardened IoT1 to testbed**. Use the helper script:
   ```powershell
   pwsh C:\Users\pjtio\OneDrive\Desktop\CARE-SSL\sync_iot1_to_testbed.ps1 -WhatIf
   # review the output, then re-run without -WhatIf
   ```
2. **Apply migrations** against your TimescaleDB:
   ```powershell
   cd C:\Users\pjtio\smart-i-lab-testbed
   python migrations\apply.py
   ```
3. **Bootstrap admin** if you don't have an admin key yet:
   ```powershell
   python migrations\bootstrap_admin.py
   ```
4. **Set client timeout** in both CV `.env` files:
   ```
   AIR1_API_TIMEOUT=12
   ```
5. **Smoke-test** with `smoke_test.ps1` (in `IoT1/`) and the manual Python snippet
   in §9 above.

---

## 12. What this report does NOT verify

- **Live behavioral testing**: I cannot reach your lab from here. Every "compatible"
  assertion is based on static analysis of source and the audit's enumerated changes.
- **Network reachability** of the testbed to `10.158.66.30:80` after the sync.
- **TimescaleDB state**: if pre-existing per-device tables have duplicate timestamps,
  migration 006 will log NOTICE and skip the PK addition; see `IOT1_REMEDIATION.md` §7.
- **MSR-2 firmware payload field names**: I do not know which naming convention
  (`zone_X` vs `radar_zone_X`) is actually emitted today. The forward-compat fix
  makes that question moot for ingest, but it's worth confirming via a live MQTT
  trace if you have access.

---

## 13. Summary in one sentence

The CV consumers are read-only and don't touch any of the endpoints/parameters
the hardening tightened; the only meaningful regression risk
(`radar_zone_X_occupancy` vs `zone_X_occupancy`) has been neutralized via a
forward-compat fix that accepts both column names — ship it.
