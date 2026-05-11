#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

PYTHON_BIN="${PYTHON_BIN:-python3}"
OUTPUT_DIR="model"
PREV=""
for ARG in "$@"; do
  if [[ "$PREV" == "--output-dir" ]]; then
    OUTPUT_DIR="$ARG"
    PREV=""
    continue
  fi
  case "$ARG" in
    --output-dir=*)
      OUTPUT_DIR="${ARG#--output-dir=}"
      ;;
    --output-dir)
      PREV="--output-dir"
      ;;
    *)
      PREV=""
      ;;
  esac
done

"$PYTHON_BIN" -m zone5.training --csv data/zone5_training_cv.csv "$@"

if [[ "${ZONE5_SKIP_PROMOTE:-0}" != "1" && "$OUTPUT_DIR" == "model" ]]; then
  "$PYTHON_BIN" -m zone5.promote_model --candidate-run "$OUTPUT_DIR"
fi
