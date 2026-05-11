#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

PYTHON_BIN="${PYTHON_BIN:-python3}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"

export ZONE5_SKIP_DOTENV=1
export ZONE5_DATA_SOURCE=replay
export ZONE5_REPLAY_TABLE="${ZONE5_REPLAY_TABLE:-data/zone5_training_cv.csv}"
export ZONE5_PRODUCTION_POINTER="model/production_run.txt"
export ZONE5_MAX_AGE_MINUTES=none
export ZONE5_TICK_INTERVAL_SEC="${ZONE5_TICK_INTERVAL_SEC:-1}"

exec "$PYTHON_BIN" -m uvicorn web_app.main:app --host "$HOST" --port "$PORT"
