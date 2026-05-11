#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

PYTHON_BIN="${PYTHON_BIN:-python3}"
if [[ -d ".python-packages" ]]; then
  export PYTHONPATH="$PWD/.python-packages:$PWD${PYTHONPATH:+:$PYTHONPATH}"
fi
export PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}"
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-1}"
export MKL_NUM_THREADS="${MKL_NUM_THREADS:-1}"
export OPENBLAS_NUM_THREADS="${OPENBLAS_NUM_THREADS:-1}"
export NUMEXPR_NUM_THREADS="${NUMEXPR_NUM_THREADS:-1}"

SOURCE_PARQUET="${SOURCE_PARQUET:-data/air1_all_zones_training_cv.parquet}"
SOURCE_CSV="${SOURCE_CSV:-data/air1_all_zones_training_cv.csv}"
SNAPSHOT_SOURCE="${SNAPSHOT_SOURCE:-csv}"
SOURCE_METADATA="${SOURCE_METADATA:-data/air1_all_zones_training_cv.metadata.json}"
SNAPSHOT_DIR="${SNAPSHOT_DIR:-data/training_snapshots}"
LOCK_FILE="${LOCK_FILE:-model/retrain.lock}"
SUMMARY_JSON="${SUMMARY_JSON:-model/retrain_status.json}"
OUTPUT_DIR="${OUTPUT_DIR:-model}"
RETRAIN_N_TRIALS="${RETRAIN_N_TRIALS:-50}"
RETRAIN_MAX_EPOCHS="${RETRAIN_MAX_EPOCHS:-20}"
RETRAIN_OPTUNA_JOBS="${RETRAIN_OPTUNA_JOBS:-1}"
RETRAIN_BOOTSTRAP_FALLBACK="${RETRAIN_BOOTSTRAP_FALLBACK:-auto}"
RETRAIN_CV_FOLDS="${RETRAIN_CV_FOLDS:-auto}"
PROMOTE_AFTER_RETRAIN="${PROMOTE_AFTER_RETRAIN:-1}"

TRAINER_ARGS=(
  --source-parquet "$SOURCE_PARQUET"
  --source-csv "$SOURCE_CSV"
  --snapshot-source "$SNAPSHOT_SOURCE"
  --source-metadata "$SOURCE_METADATA"
  --snapshot-dir "$SNAPSHOT_DIR"
  --lock-file "$LOCK_FILE"
  --summary-json "$SUMMARY_JSON"
  --output-dir "$OUTPUT_DIR"
  --n-trials "$RETRAIN_N_TRIALS"
  --max-epochs "$RETRAIN_MAX_EPOCHS"
  --bootstrap-fallback "$RETRAIN_BOOTSTRAP_FALLBACK"
  --cv-folds "$RETRAIN_CV_FOLDS"
)

TRAINER_ARGS+=(--optuna-jobs "$RETRAIN_OPTUNA_JOBS")
if [[ "${RETRAIN_ALLOW_BAD_LINES:-0}" == "1" ]]; then
  TRAINER_ARGS+=(--allow-bad-lines)
fi
if [[ "${RETRAIN_ALLOW_DEGENERATE_VALIDATION:-0}" == "1" ]]; then
  TRAINER_ARGS+=(--allow-degenerate-validation)
fi
if [[ "$PROMOTE_AFTER_RETRAIN" == "0" ]]; then
  TRAINER_ARGS+=(--no-promote)
fi
if [[ "${PROMOTE_SKIP_SMOKE:-0}" == "1" ]]; then
  TRAINER_ARGS+=(--promote-skip-smoke)
fi
if [[ "${PROMOTE_SKIP_NON_REGRESSION_SMOKE:-0}" == "1" ]]; then
  TRAINER_ARGS+=(--promote-skip-non-regression-smoke)
fi

exec "$PYTHON_BIN" -m air1_all_zones.retrain_once "${TRAINER_ARGS[@]}"
