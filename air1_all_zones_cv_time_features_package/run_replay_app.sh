#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

PYTHON_BIN="${PYTHON_BIN:-python3}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"

export AIR1_ALL_ZONES_SKIP_DOTENV=1
export AIR1_ALL_ZONES_DATA_SOURCE=replay
export AIR1_ALL_ZONES_REPLAY_PARQUET="data/air1_all_zones_training_cv.parquet"
export AIR1_ALL_ZONES_PRODUCTION_POINTER="model/production_run.txt"
export AIR1_ALL_ZONES_MAX_AGE_MINUTES=none
export AIR1_ALL_ZONES_TICK_INTERVAL_SEC="${AIR1_ALL_ZONES_TICK_INTERVAL_SEC:-1}"

exec "$PYTHON_BIN" -m uvicorn web_app.main:app --host "$HOST" --port "$PORT"


