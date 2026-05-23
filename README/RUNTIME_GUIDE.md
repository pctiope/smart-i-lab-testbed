# Runtime Guide

This guide consolidates the local DuckDB initialization flow, live API ingestion commands, the migrated Zone 5 runtime smoke command, and the current verified test results.

## Prerequisites

- Workspace root: `d:\AI 231`
- Python environment: `.\.venv\Scripts\python.exe`
- Local DuckDB file: `smart_ilab.duckdb`
- Live API endpoint and key are taken from [api_ingestion.py](../api_ingestion.py)

Run all commands from the workspace root.

## Database Initialization

Initialize Bronze, Silver, and Gold locally from the live Smart i-Lab API:

```powershell
.\.venv\Scripts\python.exe api_ingestion.py --all --initialize
```

What this does:

1. Ensures the DuckDB layout exists.
2. Compares local Bronze timestamps to the API.
3. Fetches missing history from the API.
4. Rebuilds Bronze from the Parquet store.
5. Runs Bronze to Silver and Silver to Gold for the six device types.

Useful variants:

```powershell
.\.venv\Scripts\python.exe api_ingestion.py --all --lookback 48
.\.venv\Scripts\python.exe api_ingestion.py --all --full-history
.\.venv\Scripts\python.exe api_ingestion.py --device-type air-1 --initialize
```

## Live Running

Start continuous live ingestion:

```powershell
.\.venv\Scripts\python.exe api_ingestion.py --all --poll 5
```

That polls every 5 minutes and refreshes downstream layers when new data arrives.

Helper script for `cmd.exe`:

```bat
run_zone5_live.cmd
```

That helper starts the same `--all --poll 5` live poller and writes logs under `logs\zone5_live_poll.log`.

Important:

- `run_zone5_live.cmd` keeps the writable DuckDB file open while polling.
- Any second workflow that writes to DuckDB will fail until the live poller is stopped.

Monitor live API versus local Bronze state without modifying code paths:

```powershell
.\.venv\Scripts\python.exe TEST\test_live.py --all
.\.venv\Scripts\python.exe TEST\test_live.py --all --check-only
```

## Zone 5 Runtime Smoke

Run the migrated Zone 5 runtime smoke check against the local DuckDB:

```powershell
.\.venv\Scripts\python.exe smoke_train_runtime.py --rebuild --lookback 12 --safety-rows 3 --persist-output --report-path runtime_smoke_report.json
```

What the smoke runner checks:

1. Rebuilds `silver.zone5_training_input` from live Silver tables.
2. Extracts the latest smoke window from the migrated training input.
3. Confirms the expected contract columns are present.
4. Confirms the smoke window row count matches `lookback + safety_rows`.
5. Confirms timestamps are unique and monotonic.
6. Writes a JSON quality report.
7. Optionally writes a single smoke output row to Silver and promotes it to Gold.

Generated artifact:

- `runtime_smoke_report.json`

Current runtime caveat:

- `silver.zone5_cv_labels` and `silver.zone5_sen55` are not produced by `api_ingestion.py --all --initialize` today.
- The runtime smoke can be fully populated by seeding those support tables from current live BSG sources with the helper below.

### Live Support-Table Seeding

Confirmed current Zone 5 live device IDs:

- Smart plug: `9d88e7`
- mmWave: `89f464`
- AG-One surrogate source for SEN55-style fields: `6f31cc`

Seed the missing support tables and rebuild the training input:

```powershell
.\.venv\Scripts\python.exe seed_zone5_live_support_tables.py --rebuild --rebuild-training-input
```

What this does:

1. Seeds `silver.zone5_sen55` from live `silver.ag_one` rows.
2. Seeds `silver.zone5_cv_labels` from a smoke-only mmWave occupancy surrogate using `silver.msr_2`.
3. Rebuilds `silver.zone5_training_input`.

Important limitation:

- The seeded `silver.zone5_cv_labels` table is a runtime smoke surrogate, not a replacement for real CV person-count labels.

## Direct Preprocess Commands

If you need to rerun the generic device layers without a fresh API ingest:

```powershell
.\.venv\Scripts\python.exe bronze2silver_preprocess.py
.\.venv\Scripts\python.exe silver2gold_preprocess.py
```

Zone 5 migrated runtime build is exposed through Python helpers:

```powershell
.\.venv\Scripts\python.exe -c "from bronze2silver_preprocess import run_zone5_training_preprocess; run_zone5_training_preprocess(rebuild=True)"
.\.venv\Scripts\python.exe -c "from silver2gold_preprocess import run_zone5_training_postprocess; run_zone5_training_postprocess(rebuild=True)"
```

## Zone 5 Full Training

Run the full migrated Zone 5 training flow from DuckDB Silver tables with the root trainer:

```powershell
.\.venv\Scripts\python.exe train_zone5_migrated.py --mode test --output-dir test_runs\zone5_full_train_test --rebuild --n-trials 50 --max-epochs 20 --cv-folds 3 --report-path test_runs\zone5_full_train_test\train_report.json
```

That command:

1. Rebuilds `silver.zone5_training_input` from DuckDB.
2. Filters to labeled rows.
3. Writes a Parquet snapshot under the chosen output directory.
4. Runs the full Zone 5 trainer from that snapshot.
5. Keeps all artifacts and `current_run.txt` inside the chosen output directory.

Use a live output directory only when you intentionally want the active model artifacts there:

```powershell
.\.venv\Scripts\python.exe train_zone5_migrated.py --mode live --output-dir model --rebuild --n-trials 50 --max-epochs 20 --cv-folds 3 --report-path model\train_report.json
```

The only intended behavioral difference between test and live runs is the output directory.

Helper script for `cmd.exe` test training:

```bat
run_zone5_test_train.cmd
```

That helper:

1. Rebuilds the seeded Zone 5 support tables.
2. Rebuilds `silver.zone5_training_input`.
3. Runs the migrated full training flow in test mode.

Important:

- Stop `run_zone5_live.cmd` before running `run_zone5_test_train.cmd`.
- The test helper writes to DuckDB, so it cannot run concurrently with the live poller against the same `smart_ilab.duckdb` file.

## Verified Test Results

These checks were run successfully in the project virtualenv:

```powershell
.\.venv\Scripts\python.exe -m py_compile dataloader.py zone5_training_migrated.py TEST\test_training_migrated.py TEST\test_train.py TEST\test_pipeline.py
.\.venv\Scripts\python.exe -m unittest TEST.test_training_migrated -v
.\.venv\Scripts\python.exe -m unittest TEST.test_train -v
.\.venv\Scripts\python.exe -m unittest TEST.test_pipeline -v
```

Verified outcomes:

- `TEST/test_training_migrated.py`: 4 tests passed.
- `TEST/test_train.py`: 2 tests passed.
- `TEST/test_pipeline.py`: 51 tests passed.
- Live runtime smoke on 2026-05-23 passed against the local DuckDB with `training_input_rows=45545`, `smoke_rows=15`, and no missing or unexpected smoke-frame columns.
- Live runtime smoke with `--persist-output` also passed and initialized `silver.zone5_training_output` and `gold.zone5_training_output` with 1 smoke output row each.
- After fixing the full-table loader path and seeding support tables, the current live smoke sample has zero nulls across all 15 required smoke-frame columns.
- The fully populated live smoke tail now includes `power_s5`, `mmwave_s5`, and seeded `sen55_*` features for the latest 15-row window.
- The migrated Zone 5 runtime path is SQL-only; CSV helper APIs were removed.
- The migrated full-training entrypoint is `train_zone5_migrated.py`; it trains from DuckDB-backed Silver input snapshots and supports isolated test output directories.
- The Windows bootstrap path was repaired by replacing runtime console arrow characters with ASCII in the live ingest and downstream preprocessors.

## Current Live Status

The local live poller was started with:

```powershell
.\.venv\Scripts\python.exe -u api_ingestion.py --all --poll 5
```

Observed live status during startup:

- `air-1` was already up to date and its Bronze, Silver, and Gold layers were available locally.
- The poller advanced into `msr-2` incremental collection from `2026-05-19T20:01:21+00:00`.
- The current long-running part of the live refresh is `msr-2` backfill across 17 devices.

## Troubleshooting

If initialization fails because DuckDB is locked, identify the orphaned process and stop it before rerunning initialization.

Example PowerShell command:

```powershell
Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" | Select-Object ProcessId, CommandLine
Stop-Process -Id <PID> -Force
```

If the live runtime smoke fails, inspect the generated JSON report first. The most likely causes are:

1. `silver.zone5_training_input` does not have enough rows for the requested smoke window.
2. One of the required live Silver source tables is missing.
3. The Zone 5-specific smart plug or mmWave device IDs do not exist in the current local data.