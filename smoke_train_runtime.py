"""Run a live smoke check for the migrated Zone 5 training path.

This script targets the local smart_ilab.duckdb file. It builds the migrated
Zone 5 training input from Silver tables, extracts the latest smoke window,
checks the expected contract, and writes a JSON report for inspection.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

os.environ.pop("DUCKDB_READ_ONLY", None)

import zone5_training_migrated as migrated


def _to_builtin(value):
    if isinstance(value, dict):
        return {key: _to_builtin(inner) for key, inner in value.items()}
    if isinstance(value, list):
        return [_to_builtin(inner) for inner in value]
    if isinstance(value, tuple):
        return [_to_builtin(inner) for inner in value]
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if pd.isna(value):
        return None
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            return str(value)
    return value


def build_runtime_report(lookback: int, safety_rows: int, rebuild: bool, persist_output: bool) -> dict[str, object]:
    training_input = migrated.build_zone5_training_input_from_silver(rebuild=rebuild)
    smoke_frame = migrated.build_zone5_smoke_frame_from_silver(lookback=lookback, safety_rows=safety_rows)
    quality = migrated.training_input_quality_report(smoke_frame)

    expected_columns = ["timestamp", *migrated.RAW_FEATURE_COLUMNS]
    actual_columns = list(smoke_frame.columns)
    missing_columns = [column for column in expected_columns if column not in actual_columns]
    unexpected_columns = [column for column in actual_columns if column not in expected_columns]
    expected_rows = int(lookback) + int(safety_rows)

    report: dict[str, object] = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "training_input_table": migrated.SILVER_TRAINING_INPUT,
        "training_input_rows": int(len(training_input)),
        "smoke_rows": int(len(smoke_frame)),
        "expected_smoke_rows": expected_rows,
        "expected_columns": expected_columns,
        "actual_columns": actual_columns,
        "missing_columns": missing_columns,
        "unexpected_columns": unexpected_columns,
        "timestamps_monotonic_increasing": bool(smoke_frame["timestamp"].is_monotonic_increasing),
        "timestamps_unique": bool(smoke_frame["timestamp"].is_unique),
        "quality_report": quality,
        "smoke_tail": _to_builtin(smoke_frame.tail(min(5, len(smoke_frame))).to_dict(orient="records")),
        "status": "ok",
        "notes": [],
    }

    if report["smoke_rows"] != report["expected_smoke_rows"]:
        report["status"] = "error"
        report["notes"].append(
            f"Smoke row count mismatch: expected {expected_rows}, found {len(smoke_frame)}"
        )
    if missing_columns:
        report["status"] = "error"
        report["notes"].append(f"Missing expected columns: {missing_columns}")
    if not report["timestamps_monotonic_increasing"]:
        report["status"] = "error"
        report["notes"].append("Smoke timestamps are not monotonic increasing")
    if not report["timestamps_unique"]:
        report["status"] = "error"
        report["notes"].append("Smoke timestamps are not unique")

    if persist_output:
        last_row = smoke_frame.tail(1).copy()
        output = pd.DataFrame(
            [
                {
                    "timestamp": last_row.iloc[0]["timestamp"],
                    "model_run_id": f"runtime_smoke_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
                    "probability": 0.0,
                    "reference_rows": len(smoke_frame),
                }
            ]
        )
        silver_output = migrated.write_training_output_to_silver(output, rebuild=False, copy_to_gold=True)
        report["persisted_output_rows"] = int(len(silver_output))
        report["gold_output_rows"] = int(len(migrated.load_zone5_training_output(layer="gold")))

    return _to_builtin(report)


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke check the migrated Zone 5 runtime against local DuckDB")
    parser.add_argument("--lookback", type=int, default=12, help="Number of lookback rows to require before the safety window")
    parser.add_argument("--safety-rows", type=int, default=3, help="Additional tail rows to include in the smoke window")
    parser.add_argument("--rebuild", action="store_true", help="Rebuild silver.zone5_training_input before checking")
    parser.add_argument("--persist-output", action="store_true", help="Write a smoke output row to silver and promote it to gold")
    parser.add_argument("--report-path", default="runtime_smoke_report.json", help="Path to write the JSON smoke report")
    args = parser.parse_args()

    report_path = Path(args.report_path)
    try:
        report = build_runtime_report(
            lookback=args.lookback,
            safety_rows=args.safety_rows,
            rebuild=args.rebuild,
            persist_output=args.persist_output,
        )
    except Exception as exc:
        report = {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "status": "error",
            "error": str(exc),
            "notes": ["Runtime smoke failed before completing the Zone 5 training contract checks."],
        }

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())