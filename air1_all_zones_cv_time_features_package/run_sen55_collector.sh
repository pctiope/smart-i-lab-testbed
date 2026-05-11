#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

PYTHON_BIN="${PYTHON_BIN:-python3}"
OUTPUT_CSV="${OUTPUT_CSV:-data/sen55_data.csv}"
OUTPUT_PARQUET="${OUTPUT_PARQUET:-data/sen55_data.parquet}"

exec "$PYTHON_BIN" -m air1_all_zones.sen55_mqtt_collector --output-csv "$OUTPUT_CSV" --output-parquet "$OUTPUT_PARQUET"


