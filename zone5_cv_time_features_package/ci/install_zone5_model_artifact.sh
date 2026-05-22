#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG_ROOT="${PKG_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
MODEL_ROOT="${MODEL_ROOT:-model}"
PRODUCTION_POINTER="${PRODUCTION_POINTER:-$MODEL_ROOT/production_run.txt}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8000/api/health}"
HEALTH_TIMEOUT_SEC="${HEALTH_TIMEOUT_SEC:-180}"
HEALTH_POLL_SEC="${HEALTH_POLL_SEC:-5}"

: "${ARTIFACT_PATH:?ARTIFACT_PATH is required}"
: "${ARTIFACT_SHA256:?ARTIFACT_SHA256 is required}"
: "${RUN_ID:?RUN_ID is required}"

cd "$PKG_ROOT"

case "$RUN_ID" in
  *[!A-Za-z0-9._-]*|"")
    echo "Invalid RUN_ID: $RUN_ID" >&2
    exit 1
    ;;
esac

actual_sha256="$(sha256sum "$ARTIFACT_PATH" | awk '{print $1}')"
if [[ "$actual_sha256" != "$ARTIFACT_SHA256" ]]; then
  echo "Artifact checksum mismatch for $ARTIFACT_PATH" >&2
  echo "expected: $ARTIFACT_SHA256" >&2
  echo "actual:   $actual_sha256" >&2
  exit 1
fi

tmp_dir="$(mktemp -d)"
stage_dir=""
cleanup() {
  rm -rf "$tmp_dir"
  if [[ -n "$stage_dir" && -d "$stage_dir" ]]; then
    rm -rf "$stage_dir"
  fi
}
trap cleanup EXIT

tar -xzf "$ARTIFACT_PATH" -C "$tmp_dir"
source_dir="$tmp_dir/model/runs/$RUN_ID"
if [[ ! -d "$source_dir" ]]; then
  source_dir="$tmp_dir/$RUN_ID"
fi
if [[ ! -d "$source_dir" ]]; then
  echo "Artifact does not contain model/runs/$RUN_ID" >&2
  exit 1
fi

for required in \
  "manifest.json" \
  "models/best_cnn_zone_5.pt" \
  "tables/best_params_zone_5.json" \
  "tables/scaler_stats_zone_5.json" \
  "tables/metrics_zone_5.json"
do
  if [[ ! -s "$source_dir/$required" ]]; then
    echo "Artifact is missing required file: $required" >&2
    exit 1
  fi
done

mkdir -p "$MODEL_ROOT/runs"
target_dir="$MODEL_ROOT/runs/$RUN_ID"
if [[ -d "$target_dir" ]]; then
  validate_dir="$target_dir"
else
  stage_dir="$MODEL_ROOT/runs/.$RUN_ID.incoming.$$"
  rm -rf "$stage_dir"
  cp -a "$source_dir" "$stage_dir"
  validate_dir="$stage_dir"
fi

export PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}"
if [[ -d ".python-packages" ]]; then
  export PYTHONPATH="$PWD/.python-packages:$PWD${PYTHONPATH:+:$PYTHONPATH}"
else
  export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"
fi

echo "Validating installed Zone 5 model candidate $RUN_ID" >&2
"$PYTHON_BIN" smoke_test.py \
  --candidate-run "$validate_dir" \
  --production-pointer "$PRODUCTION_POINTER" \
  --json

if [[ ! -d "$target_dir" ]]; then
  mv "$stage_dir" "$target_dir"
  stage_dir=""
fi

pointer_path="$PRODUCTION_POINTER"
pointer_dir="$(dirname "$pointer_path")"
mkdir -p "$pointer_dir"
previous_pointer=""
if [[ -f "$pointer_path" ]]; then
  previous_pointer="$(tr -d '\r\n' < "$pointer_path" || true)"
fi

tmp_pointer="$pointer_path.tmp.$$"
printf 'model/runs/%s\n' "$RUN_ID" > "$tmp_pointer"
mv "$tmp_pointer" "$pointer_path"

if [[ -n "$HEALTH_URL" ]]; then
  if ! "$PYTHON_BIN" - "$RUN_ID" "$HEALTH_URL" "$HEALTH_TIMEOUT_SEC" "$HEALTH_POLL_SEC" <<'PY'
import json
import sys
import time
import urllib.request

run_id = sys.argv[1]
url = sys.argv[2]
timeout = float(sys.argv[3])
interval = float(sys.argv[4])
deadline = time.time() + timeout
last_error = ""

while time.time() <= deadline:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        loaded = ((payload.get("inference") or {}).get("model_run_id"))
        if loaded == run_id:
            print(f"Health check confirmed model_run_id={run_id}")
            raise SystemExit(0)
        last_error = f"health model_run_id={loaded!r}, expected {run_id!r}"
    except Exception as exc:
        last_error = str(exc)
    time.sleep(interval)

print(f"Timed out waiting for live app to load promoted model: {last_error}", file=sys.stderr)
raise SystemExit(1)
PY
  then
    if [[ -n "$previous_pointer" ]]; then
      printf '%s\n' "$previous_pointer" > "$tmp_pointer"
      mv "$tmp_pointer" "$pointer_path"
      echo "Restored previous production pointer after failed health verification: $previous_pointer" >&2
    fi
    exit 1
  fi
fi

echo "Installed Zone 5 production model $RUN_ID"
