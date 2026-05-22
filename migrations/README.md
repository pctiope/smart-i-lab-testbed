# Schema migrations

Versioned SQL files applied in lexical order by `apply.py`. Each file runs at most once;
applied versions are tracked in the `schema_migrations` table.

## Running

```bash
docker compose -f compose.yaml -f compose.dev.yaml up -d timescaledb
python3 migrations/apply.py
```

Or, inside the backend container:

```bash
docker compose exec backend python3 /pytodb/migrations/apply.py
```

## Adding a new migration

1. Pick the next `NNN_` prefix (zero-padded).
2. Write idempotent SQL — use `CREATE TABLE IF NOT EXISTS`, `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`,
   etc. Migrations should be safe to re-run.
3. Test by applying twice to verify no errors.

Per-device sensor tables (`apollo_air_1_<id>` etc.) are still created by the Python
subscriber on first device discovery — they're not in migrations because the table set
grows with the registry.
