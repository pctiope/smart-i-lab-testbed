#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG_ROOT="${PKG_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
MODEL_ROOT="${MODEL_ROOT:-model}"
SUMMARY_JSON="${SUMMARY_JSON:-$MODEL_ROOT/retrain_status.json}"
PRODUCTION_POINTER="${PRODUCTION_POINTER:-$MODEL_ROOT/production_run.txt}"
ARTIFACT_DIR="${ARTIFACT_DIR:-$MODEL_ROOT/artifacts}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

cd "$PKG_ROOT"
mkdir -p "$ARTIFACT_DIR"

mapfile -t resolved < <("$PYTHON_BIN" - "$PKG_ROOT" "${RUN_ID:-}" "$SUMMARY_JSON" "$PRODUCTION_POINTER" "$MODEL_ROOT" <<'PY'
import json
import pathlib
import re
import sys

pkg_root = pathlib.Path(sys.argv[1]).resolve()
requested_run_id = sys.argv[2].strip()
summary_path = (pkg_root / sys.argv[3]).resolve()
production_pointer = (pkg_root / sys.argv[4]).resolve()
model_root = (pkg_root / sys.argv[5]).resolve()


def validate_run_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", value):
        raise SystemExit(f"Invalid run_id: {value!r}")
    return value


def resolve_pointer(raw: str) -> pathlib.Path:
    raw = raw.strip()
    if not raw:
        raise SystemExit("No promoted production pointer is available")
    candidate = pathlib.Path(raw)
    if not candidate.is_absolute():
        candidate = (pkg_root / candidate).resolve()
    return candidate


promotion_status = "manual"
if requested_run_id:
    run_id = validate_run_id(requested_run_id)
    run_dir = model_root / "runs" / run_id
else:
    if not summary_path.is_file():
        raise SystemExit(f"Missing retrain summary: {summary_path}")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    promotion = summary.get("promotion") or {}
    promotion_status = str(promotion.get("status") or "")
    if promotion_status != "promoted":
        raise SystemExit(f"Latest retrain did not promote a model: promotion.status={promotion_status!r}")
    run_dir = resolve_pointer(str(promotion.get("current") or ""))
    manifest_path = run_dir / "manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        run_id = validate_run_id(str(manifest.get("run_id") or run_dir.name))
    else:
        run_id = validate_run_id(run_dir.name)

expected_dir = (model_root / "runs" / run_id).resolve()
run_dir = run_dir.resolve()
if run_dir != expected_dir:
    raise SystemExit(f"Promoted run must live at {expected_dir}, got {run_dir}")
if not run_dir.is_dir():
    raise SystemExit(f"Promoted run directory is missing: {run_dir}")

print(run_id)
print(str(run_dir))
print(promotion_status)
print(str(production_pointer))
PY
)

RUN_ID="${resolved[0]}"
RUN_DIR="${resolved[1]}"
PROMOTION_STATUS="${resolved[2]}"
PRODUCTION_POINTER_PATH="${resolved[3]}"
ARTIFACT_PATH="$PKG_ROOT/$ARTIFACT_DIR/zone5-model-$RUN_ID.tar.gz"
METADATA_PATH="$PKG_ROOT/$ARTIFACT_DIR/zone5-model-$RUN_ID.metadata.json"
ENV_PATH="$PKG_ROOT/$ARTIFACT_DIR/latest.env"

echo "Validating Zone 5 model run $RUN_ID before packaging" >&2
"$PYTHON_BIN" smoke_test.py \
  --candidate-run "$RUN_DIR" \
  --production-pointer "$PRODUCTION_POINTER_PATH" \
  --json >&2

tmp_artifact="$ARTIFACT_PATH.tmp"
rm -f "$tmp_artifact"
tar -C "$PKG_ROOT" -czf "$tmp_artifact" "model/runs/$RUN_ID"
mv "$tmp_artifact" "$ARTIFACT_PATH"
ARTIFACT_SHA256="$(sha256sum "$ARTIFACT_PATH" | awk '{print $1}')"
ARTIFACT_SIZE_BYTES="$(stat -c '%s' "$ARTIFACT_PATH")"

"$PYTHON_BIN" - "$RUN_DIR" "$SUMMARY_JSON" "$ARTIFACT_PATH" "$ARTIFACT_SHA256" "$ARTIFACT_SIZE_BYTES" "$METADATA_PATH" <<'PY'
import json
import os
import pathlib
import socket
import sys
from datetime import datetime, timezone

run_dir = pathlib.Path(sys.argv[1]).resolve()
summary_path = pathlib.Path(sys.argv[2])
artifact_path = pathlib.Path(sys.argv[3]).resolve()
artifact_sha256 = sys.argv[4]
artifact_size_bytes = int(sys.argv[5])
metadata_path = pathlib.Path(sys.argv[6])


def load_json(path: pathlib.Path) -> dict:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


manifest = load_json(run_dir / "manifest.json")
metrics = load_json(run_dir / "tables" / "metrics_zone_5.json")
summary = load_json(summary_path)
test_metrics = (metrics.get("metrics_by_split") or {}).get("test") or {}
cv_metrics = metrics.get("cv_metrics") or {}

payload = {
    "schema_version": 1,
    "run_id": manifest.get("run_id") or run_dir.name,
    "packaged_at": datetime.now(timezone.utc).isoformat(),
    "training_host": os.environ.get("TRAINING_SERVER_ID") or socket.gethostname(),
    "artifact": {
        "filename": artifact_path.name,
        "sha256": artifact_sha256,
        "size_bytes": artifact_size_bytes,
    },
    "manifest": {
        "created_at": manifest.get("created_at"),
        "git_rev": manifest.get("git_rev"),
        "model_contract_version": manifest.get("model_contract_version"),
        "validation_mode": manifest.get("validation_mode"),
        "cv_folds_used": manifest.get("cv_folds_used"),
        "lookback_rows": manifest.get("lookback_rows"),
        "lookback_minutes": manifest.get("lookback_minutes"),
        "target_column": manifest.get("target_column"),
    },
    "metrics": {
        "blind_test": {
            "pr_auc": test_metrics.get("pr_auc"),
            "roc_auc": test_metrics.get("roc_auc"),
            "brier_score": test_metrics.get("brier_score"),
            "bce_log_loss": test_metrics.get("bce_log_loss"),
            "positive_windows": test_metrics.get("positive_windows"),
            "positive_buckets": test_metrics.get("positive_buckets"),
            "positive_events": test_metrics.get("positive_events"),
        },
        "cv": {
            "best_mean_pr_auc": cv_metrics.get("best_mean_pr_auc"),
            "best_mean_roc_auc": cv_metrics.get("best_mean_roc_auc"),
        },
    },
    "promotion": summary.get("promotion") or {},
}

metadata_path.parent.mkdir(parents=True, exist_ok=True)
metadata_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

cat > "$ENV_PATH" <<EOF
RUN_ID=$RUN_ID
PROMOTION_STATUS=$PROMOTION_STATUS
ARTIFACT_PATH=$ARTIFACT_PATH
ARTIFACT_SHA256=$ARTIFACT_SHA256
ARTIFACT_SIZE_BYTES=$ARTIFACT_SIZE_BYTES
METADATA_PATH=$METADATA_PATH
EOF

echo "Packaged Zone 5 model artifact: $ARTIFACT_PATH" >&2
cat "$ENV_PATH"
