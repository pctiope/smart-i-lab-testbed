#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

cat <<'EOF'
This package-local live collector wrapper is archived in the desktop repo copy.

It builds CSV training artifacts and is no longer the active path.
Use the repository-root DuckDB Bronze/Silver/Gold pipeline instead:
  python api_ingestion.py --all --initialize
  python api_ingestion.py --all --poll 5
  python -c "from bronze2silver_preprocess import run_zone5_training_preprocess; run_zone5_training_preprocess(rebuild=True)"
EOF
exit 1

PYTHON_BIN="${PYTHON_BIN:-python3}"
export PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}"
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-1}"
export MKL_NUM_THREADS="${MKL_NUM_THREADS:-1}"
export OPENBLAS_NUM_THREADS="${OPENBLAS_NUM_THREADS:-1}"
export NUMEXPR_NUM_THREADS="${NUMEXPR_NUM_THREADS:-1}"
DURATION_MIN="${DURATION_MIN:-1440}"
APPEND_EVERY_SEC="${APPEND_EVERY_SEC:-10}"
BACKFILL_SEC="${BACKFILL_SEC:-120}"
SNAPSHOT_REFRESH_EVERY_HOURS="${SNAPSHOT_REFRESH_EVERY_HOURS:-${PARQUET_REBUILD_EVERY_HOURS:-1}}"
RETRAIN_AFTER_SNAPSHOT="${RETRAIN_AFTER_SNAPSHOT:-${RETRAIN_AFTER_PARQUET:-0}}"
PROMOTE_AFTER_RETRAIN="${PROMOTE_AFTER_RETRAIN:-0}"
RETRAIN_BOOTSTRAP_FALLBACK="${RETRAIN_BOOTSTRAP_FALLBACK:-auto}"
RETRAIN_CV_FOLDS="${RETRAIN_CV_FOLDS:-auto}"
COLLECTOR_EXTRA_ARGS=()
if [[ "$RETRAIN_AFTER_SNAPSHOT" == "1" ]]; then
  COLLECTOR_EXTRA_ARGS+=(--retrain-after-snapshot)
else
  COLLECTOR_EXTRA_ARGS+=(--no-retrain-after-snapshot)
fi
if [[ "$PROMOTE_AFTER_RETRAIN" == "0" ]]; then
  COLLECTOR_EXTRA_ARGS+=(--no-promote-after-retrain)
fi

export AIR1_API_URL="${AIR1_API_URL:-http://10.158.66.30:80}"
export AIR1_API_KEY="${AIR1_API_KEY:-9c5c3569-cfe7-42ae-bf00-e86ae08519ef}"

exec "$PYTHON_BIN" -m zone5.collect_training_data \
  --live-append \
  --duration-min "$DURATION_MIN" \
  --append-every-sec "$APPEND_EVERY_SEC" \
  --backfill-sec "$BACKFILL_SEC" \
  --snapshot-refresh-every-hours "$SNAPSHOT_REFRESH_EVERY_HOURS" \
  --retrain-bootstrap-fallback "$RETRAIN_BOOTSTRAP_FALLBACK" \
  --retrain-cv-folds "$RETRAIN_CV_FOLDS" \
  "${COLLECTOR_EXTRA_ARGS[@]}"
