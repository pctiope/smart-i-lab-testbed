# Smart i-LAB IoT1 — Deployment Status

_Last updated: 2026-05-23 (cutover not yet executed)._

This is the single source of truth for "where are we in the rollout?". Update
this file whenever a checkbox state changes. Append to "History" rather than
overwriting.

---

## Where we are

```
[done]  [done]  [done]  [done]  [TODO]   [TODO]    [TODO]
audit   fix     sync    docs    backup   migrate   deploy   smoke
        code    files                                       test
```

The audit, the remediation code, the file sync to testbed, and the supporting
docs are **complete**. Nothing has been applied to the live database. Nothing
has been deployed to the running container at `:80`.

---

## What's done

- [x] **Security audit** — 46 findings cataloged in `IOT1_AUDIT.md`.
- [x] **Remediation code** — 5 batches (quick wins, lows, mediums, highs,
  criticals + forward-compat). Lives in
  `C:\Users\pjtio\OneDrive\Desktop\CARE-SSL\IoT1\`.
- [x] **CV compatibility verified** — air1 / zone5 packages don't touch any
  endpoints that got stricter. See `IOT1_CV_COMPATIBILITY.md`.
- [x] **Synced to testbed** — last sync 2026-05-12 ~00:38. The hardened
  files are in this directory; the CV packages are preserved.
- [x] **Backups exist** — `_backup_<timestamp>/` folders for every sync that
  overwrote files.
- [x] **`.env` populated** — with the lab credentials, in
  `smart-i-lab-testbed/.env` (gitignored).
- [x] **`npm ci` complete** — both Node projects' `node_modules` are present
  in testbed.
- [x] **Vite build verified** — `Smart-iLab_DigitalTwin/dist/` exists and
  loads bundled three.js.
- [x] **DB reachability confirmed** — PG 17.2 + timescaledb + toolkit live at
  `10.158.66.30:5432`. The PG role is `postgres` (the credentials image labels
  it "admin", referring to privilege class). Credentials in the gitignored
  `.env` file.
- [x] **Migration 005 fixed** — handles the existing `error_logs` schema
  (no `timestamp` column on the live table).
- [x] **Subscriber INSERT made defensive** — falls back to plain INSERT for
  per-device tables that lack `PRIMARY KEY (timestamp)`. 42 of 126 tables
  currently fall into this bucket.
- [x] **`bootstrap_admin.py --skip-if-any-admin-exists`** added so the
  script is safe to leave in a deploy pipeline.
- [x] **`dedupe_per_device.py`** added — emits per-table dedupe SQL the
  operator can review.
- [x] **Technical overview deck added** — Beamer source and compiled PDF live in
  `docs/presentations/smart-ilab-testbed-overview/`.

---

## What's pending

These all require touching production infrastructure. **Do them in order.**

- [ ] **1. Take a DB backup.** Even though nothing planned is destructive,
  migration 006 alters constraints on 80+ hypertables.
  ```bash
  pg_dump -h 10.158.66.30 -U postgres -d postgres -F c -f iot1_pre_cutover_$(date +%Y%m%d).dump
  ```

- [ ] **2. Apply migrations.** From the testbed root:
  ```powershell
  python migrations\apply.py
  ```
  Expected: 001-005 are mostly no-ops against existing schema; 005 adds the
  `timestamp` column to `error_logs`; 006 adds `PRIMARY KEY (timestamp)` to
  ~84 of 126 per-device tables and skips 42 with NOTICE messages.

- [ ] **3. Deploy the hardened code.** Replace whatever's serving
  `http://10.158.66.30:80` with the testbed's `SSL-IoT1-REST` container.
  Subscribers (`databridge-esp` / `-zigbee` / `-sensibo`) are now three
  separate containers, not one.

- [ ] **4. Smoke-test with an existing admin key.**
  ```powershell
  .\smoke_test.ps1 -ApiKey "<existing admin api_key>" -BaseUrl "http://10.158.66.30"
  ```
  Use an existing key from the 25 already in the `users` table -- do **not**
  bootstrap a new admin unless you specifically need one.

- [ ] **5. Tail subscriber logs for 30 minutes.** Verify ingest rates,
  no crash loops, and that the expected "Table X lacks PK(timestamp);
  falling back to plain INSERT" warnings only fire for the 42 dirty tables.

- [ ] **6. Update CV consumer timeouts (optional).** Set
  `AIR1_ALL_ZONES_LIVE_API_TIMEOUT_SEC=12` and `ZONE5_LIVE_API_TIMEOUT_SEC=12`
  in the respective CV `.env` files to stay below the server's new 15s cap.

- [ ] **7. (Deferred) Dedupe the 42 dirty per-device tables.** When
  convenient:
  ```powershell
  python migrations\dedupe_per_device.py
  ```
  This produces a `.sql` file at the IoT1 root. Review, then either:
  ```bash
  psql -h 10.158.66.30 -U postgres -d postgres -f dedupe_*.sql
  ```
  or re-run with `--apply` and type the confirmation phrase.

  After all 42 are dedup'd, re-run `python migrations\apply.py` to let
  migration 006 add the missing PKs.

- [ ] **8. (Optional) Generate a Home Assistant long-lived access token.**
  Only needed for the Sensibo subscriber. From HA UI -> Profile -> Security
  -> Create Token. Paste into `.env` `HOME_ASSISTANT_TOKEN=`.

- [ ] **9. (Optional) Pin Docker base-image digests.**
  ```bash
  docker pull node:22-alpine
  docker inspect --format='{{index .RepoDigests 0}}' node:22-alpine
  # Edit Dockerfile: FROM node:22-alpine@sha256:<digest>
  # Repeat for python:3.12-slim and nginx:1.27-alpine
  ```

---

## Risks tracked

- **42 per-device tables with duplicate timestamps.** Mitigated by the
  subscriber fall-back; cleaned up properly via step 7.
- **`transactions` table column type mismatch.** Live table uses
  `timestamp without time zone`; my migration declares `TIMESTAMPTZ`. Tests
  fine in practice but worth a future migration to convert + backfill.
- **Backwards-compat for `Authorization: Bearer` header.** The hardened
  API only checks `X-API-KEY`. The `Bearer` header from the air1 CV package
  is ignored harmlessly today, but if you ever drop X-API-KEY and rely only
  on Bearer, you'll get 401s.

---

## History

- 2026-05-11 — Audit produced (46 findings).
- 2026-05-11 — Remediation batches 1-5 shipped.
- 2026-05-12 — Sync to testbed (initial). Migrations 005 fix, subscriber
  fallback, dedupe orchestrator, bootstrap-admin flag added.
- 2026-05-12 — Docs consolidated into `testbed/docs/`. This STATUS file
  written. **Next action**: step 1 (backup) is up to the operator.
- 2026-05-23 — Technical overview presentation added under
  `docs/presentations/smart-ilab-testbed-overview/`. IoT1 production cutover
  remains pending; the next deployment action is still step 1 (backup).

(Append future events here as deployment progresses.)
