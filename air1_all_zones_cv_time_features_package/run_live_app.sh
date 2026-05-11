#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

PYTHON_BIN="${PYTHON_BIN:-python3}"
HOST="${HOST:-192.168.10.17}"
PORT="${PORT:-8002}"

export AIR1_ALL_ZONES_DATA_SOURCE="${AIR1_ALL_ZONES_DATA_SOURCE:-live}"
export AIR1_ALL_ZONES_PRODUCTION_POINTER="${AIR1_ALL_ZONES_PRODUCTION_POINTER:-model/production_run.txt}"
export AIR1_ALL_ZONES_MJPEG_TARGET_FPS="${AIR1_ALL_ZONES_MJPEG_TARGET_FPS:-15}"

exec "$PYTHON_BIN" -m uvicorn web_app.main:app --host "$HOST" --port "$PORT"


