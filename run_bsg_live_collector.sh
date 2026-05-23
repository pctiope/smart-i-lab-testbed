#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

PYTHON_BIN="${PYTHON_BIN:-python3}"
BSG_POLL_MINUTES="${BSG_POLL_MINUTES:-5}"

export PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}"
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

mkdir -p "$ROOT/data" "$ROOT/logs" "$ROOT/stage" "$ROOT/zone5_cv_time_features_package/logs"

exec "$PYTHON_BIN" -u api_ingestion.py \
  --all \
  --poll "$BSG_POLL_MINUTES" \
  --log-path "${BSG_INGESTION_LOG_PATH:-logs/bsg_ingestion.log}"
