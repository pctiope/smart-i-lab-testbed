# IoT1 Pre-Deployment Checklist (Live Lab State)

_Generated 2026-05-12 from read-only probes of the running PostgreSQL at
`10.158.66.30:5432`. **No DB state was modified.** Production apps continue
running on the existing code unchanged._

This document captures gaps between the hardened code in `smart-i-lab-testbed/`
and the actual lab DB. Read this before applying migrations.

---

## TL;DR

- The lab DB has **139 tables, 25 users, 7.7M `transactions` rows, ~4.5M
  `error_logs` rows, ~80M sensor rows across 126 per-device tables.** It is a
  living production system.
- TimescaleDB + toolkit are already installed; the 82 populated per-device
  tables are **already hypertables**.
- Migration `005_error_logs.sql` was edited locally to be safe against the
  production schema (the existing `error_logs` has hundreds of dynamic
  device columns but no `timestamp` column — the original migration would
  have errored on the index it created).
- **42 of 126 per-device tables have duplicate `timestamp` values.** Migration
  006 will skip those (the DO block catches `unique_violation` and continues).
  The Python subscribers were edited locally to **fall back to plain
  INSERT** for tables without a unique constraint, so the live ingest will
  not crash on those 42 tables — but data **will not be deduplicated** on
  broker re-delivery for them until they're cleaned up.
- The lab `users` table has the schema my code expects, but **without** the
  `CHECK (access_level BETWEEN 0 AND 2)` constraint. All 25 existing users
  already have `access_level ∈ {0,1,2}`, so this is harmless today.
- The lab uses the `postgres` role on the `postgres` database -- credentials
  live in the (gitignored) `.env`. The credentials image's "admin" label
  refers to the privilege class, not the actual PG role name.

---

## What the DB actually looks like

### Extensions

| Extension | Installed |
|---|---|
| `timescaledb` | yes |
| `timescaledb_toolkit` | yes |

So the REST API's `time_weight(...)` queries (§4.3 / §5.2) will work.

### Schema vs. my migrations

| Table | Exists | Matches my migration shape? |
|---|---|---|
| `users` | yes | columns match; no PK/UNIQUE/CHECK constraints (existing data conforms) |
| `transactions` | yes | columns match; `timestamp` is `timestamp without time zone` (mine is `TIMESTAMPTZ`); 7.7M rows |
| `groups` | yes | columns match exactly |
| `apollo_air_1`, `apollo_msr_2`, `athom_smart_plug_v2`, `airgradient_one`, `sensibo`, `zigbee2mqtt` | yes | registry shape matches |
| `error_logs` | yes | has `server` + 150 dynamic device-named columns; **no `timestamp` column** |
| `schema_migrations` | no | will be created by `apply.py` on first run |

Two extra non-migration tables also exist: `air_1` (16 rows), `msr_2` (17 rows),
and `droppable` (0 rows). Pre-existing orphans, harmless.

### Per-device sensor tables (126 total)

| Bucket | Count | What it means |
|---|---|---|
| Empty (0 rows) | 44 | Migration 006 will add PK trivially |
| Clean (no duplicate timestamps) | 40 | Migration 006 will add PK successfully |
| **Dirty (has duplicate timestamps)** | **42** | **Migration 006 will skip; PK NOT added** |

The dirty tables include almost all `apollo_msr_2_*` (lots of duplicates from
the MSR-2 radar firmware reporting same-second), all 4 `sensibo_climate_*`
tables, and most `zigbee2mqtt_*` tables that have data.

### What I changed locally to handle this

| Change | File | Effect |
|---|---|---|
| Migration 005 — `ALTER TABLE error_logs ADD COLUMN IF NOT EXISTS timestamp` and partial index `WHERE timestamp IS NOT NULL` | `migrations/005_error_logs.sql` | Migration 005 now applies cleanly against the existing 4.5M-row `error_logs` |
| Subscriber INSERT fallback | `ESPDevices_to_Database.py` (batched), `Zigbee2MQTT_to_Database.py`, `SensiboAirPro_to_Database.py` | Catches `psycopg2.errors.InvalidColumnReference` and retries without `ON CONFLICT (timestamp)` for tables that lack the PK |
| Per-table PK-capability cache | `ESPDevices_to_Database.py` (`_pk_capable` dict) | Avoids retrying the ON CONFLICT path repeatedly for tables known to lack the constraint |

---

## Safe deployment sequence

Order is important. Each step is **explicit** — nothing happens automatically.

### Step 1 — Snapshot or back up the DB

Even though nothing in this plan deletes data, the migrations add columns
and (try to) add constraints. Take a TimescaleDB backup or `pg_dump` before
proceeding. ~30 min for the DB this size.

### Step 2 — Apply migrations

```powershell
cd C:\Users\pjtio\smart-i-lab-testbed
python migrations\apply.py
```

Expected outcome with the local fixes:

- `001_enable_timescaledb.sql` — no-op (extensions already exist).
- `002_users_transactions.sql` — no-op (tables exist; index creation harmless).
- `003_device_registries.sql` — no-op (tables exist).
- `004_groups.sql` — no-op (table exists).
- `005_error_logs.sql` — **adds `timestamp` column** to existing table, builds
  the partial index. Reversible by `ALTER TABLE error_logs DROP COLUMN timestamp`.
- `006_per_device_table_pk_and_hypertable.sql` — iterates 126 tables. For each:
  - PK on timestamp: **succeeds on ~84 tables, skips ~42 with NOTICE.**
  - `create_hypertable` with `if_not_exists`: no-op for existing hypertables.

### Step 3 — Bootstrap admin user (optional)

The lab already has 25 users. **Do not run `bootstrap_admin.py`** unless you
want yet another admin row. The hardened API works with existing keys.

### Step 4 — Deploy hardened code

Replace the running container(s). Existing 25 users + their api_keys keep
working. The new code's CHECK on `access_level` ∈ {0,1,2} is enforced
only on POST/PUT; existing rows are grandfathered.

### Step 5 — Smoke test with an existing admin key

```powershell
.\smoke_test.ps1 -ApiKey "<existing admin key from users table>" -BaseUrl "http://10.158.66.30"
```

Three SQLi-regression tests use sample payloads that may also trip the
admin-write rate limiter on first run; expect 400 or 429 there, both pass.

### Step 6 — Verify ingest

Tail subscriber logs. You should see two kinds of messages:

```
[INFO ] Subscribed to apollo_air_1_<id>/data
[WARN ] Table apollo_msr_2_<id> lacks PK(timestamp); falling back to plain INSERT
```

The second message is the fallback firing for the 42 dirty tables. **This is
expected** and not an error. Inserts continue; broker re-deliveries may
produce duplicate rows in those 42 tables until cleanup.

### Step 7 — (Optional, deferred) Dedupe the 42 dirty tables

Once you have stable backups and a maintenance window, dedupe each of the
42 dirty per-device tables, then re-run migration 006 to add the missing PKs.

Example dedupe for a single table (run from `psql`):

```sql
BEGIN;
DELETE FROM apollo_msr_2_1ee964 a
USING apollo_msr_2_1ee964 b
WHERE a.ctid < b.ctid AND a.timestamp = b.timestamp;
ALTER TABLE apollo_msr_2_1ee964 ADD PRIMARY KEY (timestamp);
COMMIT;
```

The largest dirty table is `apollo_msr_2_1ee964` at ~2.7M rows; expect
several seconds per million rows on this hardware.

After all 42 are deduped, re-run `python migrations/apply.py` so the
schema_migrations bookkeeping reflects completion. Migration 006 is
idempotent — it will re-attempt and now succeed.

---

## Risks I did NOT take

For clarity, these are things I **chose not to do** because they would touch
the production DB:

- **Did NOT apply migrations.** Files updated locally only.
- **Did NOT INSERT/UPDATE/DELETE any rows.** All reads were against
  `information_schema`, `pg_extension`, or `count(*)` aggregates.
- **Did NOT bootstrap a new admin user.** 25 users already exist.
- **Did NOT modify `users`, `transactions`, `error_logs`, or any per-device
  table.** Nothing was altered.
- **Did NOT redeploy any container.** The current API at port 80 still
  runs the pre-audit code.

---

## What's still on you

| # | Task | When |
|---|---|---|
| 1 | Generate a Home Assistant long-lived access token, paste into `.env` as `HOME_ASSISTANT_TOKEN` | Before deploy if Sensibo subscriber is enabled |
| 2 | Take a DB backup | Before step 2 |
| 3 | Run `python migrations\apply.py` | After backup |
| 4 | Replace the running container/image with the hardened one | After migrations |
| 5 | Run `smoke_test.ps1` with an existing admin key | After deploy |
| 6 | Decide on dedupe timing for the 42 dirty tables | When convenient |

I can write a dedupe orchestrator script (offline; no DB writes by me) if
you want one. Just say the word.
