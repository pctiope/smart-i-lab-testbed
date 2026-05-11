#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

PYTHON_BIN="${PYTHON_BIN:-python3}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"

export ZONE5_DATA_SOURCE="${ZONE5_DATA_SOURCE:-live}"
export ZONE5_PRODUCTION_POINTER="${ZONE5_PRODUCTION_POINTER:-model/production_run.txt}"
export ZONE5_MJPEG_TARGET_FPS="${ZONE5_MJPEG_TARGET_FPS:-15}"

exec "$PYTHON_BIN" -m uvicorn web_app.main:app --host "$HOST" --port "$PORT"
