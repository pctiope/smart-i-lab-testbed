# Zone 5 BSG Docker Compose

This stack is the parallel Docker path for the BSG migration. It keeps the
staging Zone 5 web app and Vite frontend on the existing parallel ports while
moving collection and training to the root DuckDB Bronze/Silver/Gold pipeline.

Run commands from the repository root:

```bash
docker compose -f compose.zone5-bsg.yaml config --quiet
docker compose -f compose.zone5-bsg.yaml up -d --build live-app vite-frontend bsg-ingestion
```

Default staging ports:

- backend: `http://127.0.0.1:8005`
- Vite frontend: `http://127.0.0.1:8016`

Required environment:

- `SMART_ILAB_BASE_URL`
- `SMART_ILAB_API_KEY`

The BSG containers persist DuckDB/Parquet data under `./data`, logs under
`./logs`, and Zone 5 model artifacts under
`./zone5_cv_time_features_package/model`. The live app mounts that same model
directory, so BSG training can write artifacts without changing the dashboard's
production-pointer contract. The compose stack sets `SMART_ILAB_DUCKDB_PATH`
to `/workspace/data/smart_ilab.duckdb` so DuckDB and its WAL stay on the
writable data volume when containers run as the host user.

Initialize and validate the BSG path:

```bash
docker compose -f compose.zone5-bsg.yaml run --rm bsg-init
docker compose -f compose.zone5-bsg.yaml run --rm bsg-support-tables
docker compose -f compose.zone5-bsg.yaml run --rm bsg-smoke
```

Run a production-shape BSG trainer into the existing Zone 5 model directory:

```bash
docker compose -f compose.zone5-bsg.yaml run --rm bsg-trainer
```

`bsg-trainer` uses `--read-only-live`, so it reads Parquet snapshots and avoids
competing with the live ingestion process for the writable DuckDB file. Run
`bsg-support-tables` first so the real CV-label and SEN55 support-table
snapshots exist.

The old package-local `live-collector` and `trainer` services are intentionally
not part of this stack. BSG replaces those lower data plumbing services; the
Zone 5 model code, mmWave recency contract, optional SEN55 handling, artifact
layout, live app, and dashboard path stay above it.
