#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

PYTHON_BIN="${PYTHON_BIN:-python3}"
OUTPUT_CSV="${OUTPUT_CSV:-data/sen55_data.csv}"

exec "$PYTHON_BIN" -m zone5.sen55_mqtt_collector --output-csv "$OUTPUT_CSV"
