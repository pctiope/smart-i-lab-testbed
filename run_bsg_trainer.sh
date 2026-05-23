#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

PYTHON_BIN="${PYTHON_BIN:-python3}"
INGESTION_SERVICE="${BSG_INGESTION_SERVICE:-zone5-live-collector.service}"
MANAGE_INGESTION="${BSG_MANAGE_SYSTEMD_INGESTION:-0}"
INGESTION_WAS_ACTIVE=0

export PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}"
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-1}"
export MKL_NUM_THREADS="${MKL_NUM_THREADS:-1}"
export OPENBLAS_NUM_THREADS="${OPENBLAS_NUM_THREADS:-1}"
export NUMEXPR_NUM_THREADS="${NUMEXPR_NUM_THREADS:-1}"
export SMART_ILAB_DUCKDB_PATH="${SMART_ILAB_DUCKDB_PATH:-$ROOT/data/smart_ilab.duckdb}"

if [[ -z "${SMART_ILAB_BASE_URL:-}" && -n "${AIR1_API_URL:-}" ]]; then
  export SMART_ILAB_BASE_URL="$AIR1_API_URL"
fi
if [[ -z "${SMART_ILAB_API_KEY:-}" && -n "${AIR1_API_KEY:-}" ]]; then
  export SMART_ILAB_API_KEY="$AIR1_API_KEY"
fi

PYTHONPATH_PARTS=(
  "$ROOT/.python-packages"
  "$ROOT"
  "$ROOT/zone5_cv_time_features_package/.python-packages"
  "$ROOT/zone5_cv_time_features_package"
)
for path_part in "${PYTHONPATH_PARTS[@]}"; do
  if [[ -d "$path_part" ]]; then
    export PYTHONPATH="$path_part${PYTHONPATH:+:$PYTHONPATH}"
  fi
done

mkdir -p "$ROOT/data" "$ROOT/logs" "$ROOT/stage" "$ROOT/zone5_cv_time_features_package/model" "$ROOT/zone5_cv_time_features_package/logs"

can_manage_ingestion() {
  [[ "$MANAGE_INGESTION" == "1" ]] || return 1
  command -v systemctl >/dev/null 2>&1 || return 1
  systemctl --user show "$INGESTION_SERVICE" >/dev/null 2>&1 || return 1
}

restart_ingestion_if_needed() {
  if [[ "$INGESTION_WAS_ACTIVE" == "1" ]]; then
    systemctl --user start "$INGESTION_SERVICE" >/dev/null 2>&1 || true
  fi
}

if can_manage_ingestion; then
  if systemctl --user is-active --quiet "$INGESTION_SERVICE"; then
    INGESTION_WAS_ACTIVE=1
    systemctl --user stop "$INGESTION_SERVICE"
  fi
  trap restart_ingestion_if_needed EXIT
fi

if [[ "${BSG_SEED_SUPPORT_TABLES:-1}" == "1" ]]; then
  "$PYTHON_BIN" -u seed_zone5_live_support_tables.py \
    --rebuild \
    --rebuild-training-input \
    --log-path "${ZONE5_SUPPORT_TABLE_LOG_PATH:-logs/zone5_support_tables.log}"
fi

restart_ingestion_if_needed
INGESTION_WAS_ACTIVE=0
trap - EXIT

TRAINER_ARGS=(
  --read-only-live
  --lock-file "${LOCK_FILE:-zone5_cv_time_features_package/model/retrain.lock}"
  --summary-json "${SUMMARY_JSON:-zone5_cv_time_features_package/model/retrain_status.json}"
  --output-dir "${OUTPUT_DIR:-zone5_cv_time_features_package/model}"
  --n-trials "${RETRAIN_N_TRIALS:-50}"
  --max-epochs "${RETRAIN_MAX_EPOCHS:-20}"
  --bootstrap-fallback "${RETRAIN_BOOTSTRAP_FALLBACK:-auto}"
  --cv-folds "${RETRAIN_CV_FOLDS:-auto}"
  --production-pointer "${PRODUCTION_POINTER:-zone5_cv_time_features_package/model/production_run.txt}"
)

TRAINER_ARGS+=(--optuna-jobs "${RETRAIN_OPTUNA_JOBS:-1}")
if [[ "${RETRAIN_ALLOW_DEGENERATE_VALIDATION:-0}" == "1" ]]; then
  TRAINER_ARGS+=(--allow-degenerate-validation)
fi
if [[ "${PROMOTE_AFTER_RETRAIN:-1}" == "0" ]]; then
  TRAINER_ARGS+=(--no-promote)
fi
if [[ "${PROMOTE_SKIP_SMOKE:-0}" == "1" ]]; then
  TRAINER_ARGS+=(--promote-skip-smoke)
fi
if [[ "${PROMOTE_SKIP_NON_REGRESSION_SMOKE:-0}" == "1" ]]; then
  TRAINER_ARGS+=(--promote-skip-non-regression-smoke)
fi

exec "$PYTHON_BIN" -u bsg_retrain_once.py "${TRAINER_ARGS[@]}"
