#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

PYTHON_BIN="${PYTHON_BIN:-python3}"
export PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}"
DURATION_MIN="${DURATION_MIN:-1440}"
APPEND_EVERY_SEC="${APPEND_EVERY_SEC:-10}"
BACKFILL_SEC="${BACKFILL_SEC:-120}"
PARQUET_REBUILD_EVERY_HOURS="${PARQUET_REBUILD_EVERY_HOURS:-1}"
RETRAIN_AFTER_PARQUET="${RETRAIN_AFTER_PARQUET:-0}"
PROMOTE_AFTER_RETRAIN="${PROMOTE_AFTER_RETRAIN:-0}"
RETRAIN_BOOTSTRAP_FALLBACK="${RETRAIN_BOOTSTRAP_FALLBACK:-auto}"
RETRAIN_CV_FOLDS="${RETRAIN_CV_FOLDS:-auto}"
COLLECTOR_EXTRA_ARGS=()
if [[ "$RETRAIN_AFTER_PARQUET" == "1" ]]; then
  COLLECTOR_EXTRA_ARGS+=(--retrain-after-parquet)
else
  COLLECTOR_EXTRA_ARGS+=(--no-retrain-after-parquet)
fi
if [[ "$RETRAIN_AFTER_PARQUET" == "1" && "$PROMOTE_AFTER_RETRAIN" == "1" ]]; then
  COLLECTOR_EXTRA_ARGS+=(--promote-after-retrain)
else
  COLLECTOR_EXTRA_ARGS+=(--no-promote-after-retrain)
fi

export AIR1_API_URL="${AIR1_API_URL:-http://10.158.66.30:80}"
export AIR1_API_KEY="${AIR1_API_KEY:-9c5c3569-cfe7-42ae-bf00-e86ae08519ef}"

exec "$PYTHON_BIN" -m air1_all_zones.collect_training_data \
  --live-append \
  --duration-min "$DURATION_MIN" \
  --append-every-sec "$APPEND_EVERY_SEC" \
  --backfill-sec "$BACKFILL_SEC" \
  --parquet-rebuild-every-hours "$PARQUET_REBUILD_EVERY_HOURS" \
  --retrain-bootstrap-fallback "$RETRAIN_BOOTSTRAP_FALLBACK" \
  --retrain-cv-folds "$RETRAIN_CV_FOLDS" \
  "${COLLECTOR_EXTRA_ARGS[@]}"


