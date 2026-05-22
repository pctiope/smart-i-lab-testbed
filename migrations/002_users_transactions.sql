-- Auth + audit log.
CREATE TABLE IF NOT EXISTS users (
    user_name    TEXT PRIMARY KEY,
    api_key      TEXT UNIQUE NOT NULL,
    access_level INTEGER NOT NULL CHECK (access_level BETWEEN 0 AND 2)
);

CREATE TABLE IF NOT EXISTS transactions (
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    user_name TEXT,
    type      TEXT,
    uri       TEXT,
    success   BOOLEAN
);

CREATE INDEX IF NOT EXISTS transactions_timestamp_idx ON transactions (timestamp DESC);
CREATE INDEX IF NOT EXISTS transactions_user_idx ON transactions (user_name);
