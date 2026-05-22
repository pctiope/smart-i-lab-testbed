-- Catch-all error log used by Python subscribers when ingest fails.
-- Per-device columns are added dynamically by the subscribers; this seeds the
-- table and ensures the structural columns exist.

CREATE TABLE IF NOT EXISTS error_logs (
    id        BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ DEFAULT NOW(),
    server    TEXT
);

-- The existing lab DB (PG 17.2) has an error_logs table populated by the
-- pre-audit subscribers, with hundreds of dynamic device-named columns but no
-- `timestamp` column. Backfill the structural columns idempotently so that
-- the index below succeeds and future inserts can write timestamps.
ALTER TABLE error_logs ADD COLUMN IF NOT EXISTS timestamp TIMESTAMPTZ;
ALTER TABLE error_logs ADD COLUMN IF NOT EXISTS server TEXT;
-- (`id BIGSERIAL PRIMARY KEY` is only created when the table is newly built;
-- adding a serial PK to an existing 4.5M-row table is intentionally NOT done
-- here -- that requires a backfill strategy the operator should choose.)

CREATE INDEX IF NOT EXISTS error_logs_timestamp_idx ON error_logs (timestamp DESC) WHERE timestamp IS NOT NULL;
