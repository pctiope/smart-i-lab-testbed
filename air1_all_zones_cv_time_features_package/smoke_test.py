from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from pathlib import Path
from typing import Any

import pandas as pd

from air1_all_zones import model as training


PACKAGE_ROOT = Path(__file__).resolve().parent
PRODUCTION_POINTER_FILENAME = "production_run.txt"
DEFAULT_PRODUCTION_POINTER = training.DEFAULT_OUTPUT_DIR / PRODUCTION_POINTER_FILENAME


def _package_path(path: str | Path) -> Path:
    resolved = Path(path)
    if not resolved.is_absolute():
        resolved = (PACKAGE_ROOT / resolved).resolve()
    return resolved


def _sha256_of_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_run_dir(candidate: Path) -> Path:
    candidate = _package_path(candidate)
    if (candidate / training.RUN_MANIFEST_FILENAME).is_file():
        return candidate
    pointer = candidate / training.CURRENT_RUN_POINTER
    if pointer.is_file():
        run_id = pointer.read_text(encoding="utf-8").strip()
        if not run_id:
            raise ValueError(f"{pointer} is empty; no model has been trained yet")
        run_dir = candidate / "runs" / run_id
        if not run_dir.is_dir():
            raise FileNotFoundError(f"{pointer} points to {run_id}, but {run_dir} does not exist")
        return run_dir
    if (candidate / "models" / "best_cnn_all_zones.pt").is_file():
        return candidate
    raise FileNotFoundError(
        f"No trained model found at {candidate}. Train first to create model/runs/<run_id>/ "
        f"and {training.CURRENT_RUN_POINTER}; then promote to create {PRODUCTION_POINTER_FILENAME}."
    )


def _validate_manifest(run_dir: Path) -> dict[str, Any] | None:
    manifest_path = run_dir / training.RUN_MANIFEST_FILENAME
    if not manifest_path.is_file():
        return None
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for entry in manifest.get("files") or []:
        relative = entry.get("relative_path")
        expected_sha = entry.get("sha256")
        expected_size = entry.get("size_bytes")
        if not relative or not expected_sha:
            raise ValueError(f"Manifest entry is missing relative_path or sha256: {entry}")
        target = run_dir / relative
        if not target.is_file():
            raise FileNotFoundError(f"Manifest references missing file: {target}")
        if expected_size is not None and int(expected_size) != int(target.stat().st_size):
            raise ValueError(f"Size mismatch for {target}")
        actual_sha = _sha256_of_file(target)
        if actual_sha != expected_sha:
            raise ValueError(f"sha256 mismatch for {target}")
    return manifest


def _load_metrics(run_dir: Path) -> dict[str, Any]:
    metrics_path = run_dir / "tables" / "metrics_all_zones.json"
    if not metrics_path.is_file():
        raise FileNotFoundError(f"Metrics file missing: {metrics_path}")
    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    if "metrics_by_split" not in metrics or "test" not in metrics["metrics_by_split"]:
        raise ValueError(f"metrics.json missing metrics_by_split.test in {metrics_path}")
    return metrics


def _require_cv_target_contract(
    manifest: dict[str, Any] | None,
    scaler_stats: dict[str, Any],
    best_params_payload: dict[str, Any],
    checkpoint: dict[str, Any],
) -> None:
    target_values = {
        (manifest or {}).get("target_column"),
        scaler_stats.get("target_column"),
        checkpoint.get("target_column"),
    }
    bad_targets = sorted(str(value) for value in target_values if value and value != training.TARGET_COLUMN)
    if bad_targets:
        raise ValueError(
            f"candidate is not a CV-target model: expected {training.TARGET_COLUMN!r}, found {bad_targets}"
        )

    feature_columns = list(scaler_stats.get("feature_columns") or [])
    raw_columns = list(scaler_stats.get("raw_feature_columns") or [])
    missing_columns = list(scaler_stats.get("missing_indicator_columns") or [])
    missing_features = [col for col in training.FEATURE_COLUMNS if col not in feature_columns]
    missing_raw = [col for col in training.RAW_FEATURE_COLUMNS if col not in raw_columns]
    missing_indicators = [col for col in training.MISSING_INDICATOR_COLUMNS if col not in missing_columns]
    if missing_features or missing_raw or missing_indicators:
        raise ValueError(
            "candidate feature contract does not match CV live inference; "
            f"missing_features={missing_features}, missing_raw={missing_raw}, "
            f"missing_indicators={missing_indicators}"
        )
    training.require_10_second_model_contract(scaler_stats, best_params_payload, checkpoint)


def _resolve_source_parquet(raw_path: str | None) -> Path:
    if raw_path:
        candidate = _package_path(raw_path)
        if candidate.is_file():
            return candidate
    return training._latest_training_parquet(training.DEFAULT_TRAINING_PARQUET_DIR)


def _build_smoke_frame(
    run_dir: Path,
    lookback: int,
    safety_rows: int = 10,
    fixture_parquet: Path | None = None,
) -> pd.DataFrame:
    scaler_path = run_dir / "tables" / "scaler_stats_all_zones.json"
    scaler = json.loads(scaler_path.read_text(encoding="utf-8"))
    parquet_path = _package_path(fixture_parquet) if fixture_parquet else _resolve_source_parquet(
        scaler.get("source_parquet_path") or scaler.get("source_data_path")
    )
    feature_columns = list(scaler["feature_columns"])
    required = [
        training.TIMESTAMP_COLUMN,
        training.ZONE_ID_COLUMN,
        *training.source_feature_columns(feature_columns),
    ]
    frame = pd.read_parquet(parquet_path)
    for col in required:
        if col not in frame.columns:
            frame[col] = pd.NA
    frame = frame[required].copy()
    frame[training.TIMESTAMP_COLUMN] = pd.to_datetime(frame[training.TIMESTAMP_COLUMN], errors="coerce")
    frame[training.ZONE_ID_COLUMN] = pd.to_numeric(frame[training.ZONE_ID_COLUMN], errors="coerce").astype("Int64")
    frame = frame.dropna(subset=[training.TIMESTAMP_COLUMN, training.ZONE_ID_COLUMN])
    frame = frame.sort_values([training.TIMESTAMP_COLUMN, training.ZONE_ID_COLUMN])
    frame = frame.drop_duplicates(subset=[training.TIMESTAMP_COLUMN, training.ZONE_ID_COLUMN], keep="last")
    take = lookback + safety_rows
    timestamps = pd.Series(pd.to_datetime(frame[training.TIMESTAMP_COLUMN]).dropna().unique()).sort_values()
    if len(timestamps) < take:
        raise ValueError(
            f"Smoke fixture parquet has {len(timestamps)} timestamp groups; need at least {take} (lookback={lookback})"
        )
    selected = set(pd.Timestamp(value) for value in timestamps.iloc[-take:])
    window = frame.loc[pd.to_datetime(frame[training.TIMESTAMP_COLUMN]).isin(selected)].copy()
    return window.sort_values([training.TIMESTAMP_COLUMN, training.ZONE_ID_COLUMN]).reset_index(drop=True)


def _resolve_production_path(pointer_path: Path) -> Path | None:
    pointer_path = _package_path(pointer_path)
    if not pointer_path.is_file():
        return None
    raw = pointer_path.read_text(encoding="utf-8").strip()
    if not raw:
        return None
    if "/" not in raw and "\\" not in raw:
        run_candidate = training.DEFAULT_OUTPUT_DIR / "runs" / raw
        if run_candidate.is_dir():
            return run_candidate
    return _package_path(raw)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Smoke test the packaged AIR-1 all zones CNN deployment.")
    parser.add_argument(
        "--candidate-run",
        type=Path,
        default=training.DEFAULT_OUTPUT_DIR,
        help="Run directory or artifact root. Defaults to model/.",
    )
    parser.add_argument(
        "--production-pointer",
        type=Path,
        default=DEFAULT_PRODUCTION_POINTER,
        help="Path to production_run.txt for non-regression check.",
    )
    parser.add_argument(
        "--min-test-roc-auc",
        type=float,
        default=float(os.environ.get("AIR1_ALL_ZONES_MIN_TEST_ROC_AUC", "0.0")),
        help="Optional blind-test ROC-AUC floor. Default 0 disables this guardrail.",
    )
    parser.add_argument(
        "--min-test-pr-auc",
        type=float,
        default=float(os.environ.get("AIR1_ALL_ZONES_MIN_TEST_PR_AUC", "0.0")),
        help="Primary blind-test PR-AUC floor. Default 0 accepts the first valid CV-target baseline.",
    )
    parser.add_argument(
        "--min-positive-windows",
        type=int,
        default=int(os.environ.get("AIR1_ALL_ZONES_MIN_POSITIVE_WINDOWS", "5")),
        help="Minimum positive blind-test windows required for promotion smoke. Default 5.",
    )
    parser.add_argument(
        "--min-positive-buckets",
        type=int,
        default=int(os.environ.get("AIR1_ALL_ZONES_MIN_POSITIVE_BUCKETS", "5")),
        help="Minimum positive 10-second zone buckets required for promotion smoke. Default 5.",
    )
    parser.add_argument(
        "--min-positive-events",
        type=int,
        default=int(os.environ.get("AIR1_ALL_ZONES_MIN_POSITIVE_EVENTS", "1")),
        help="Minimum contiguous positive per-zone events required for promotion smoke. Default 1.",
    )
    parser.add_argument(
        "--max-test-brier",
        type=float,
        default=float(os.environ.get("AIR1_ALL_ZONES_MAX_TEST_BRIER", "1.0")),
        help="Maximum blind-test Brier score. Default 1.0.",
    )
    parser.add_argument(
        "--max-test-log-loss",
        type=float,
        default=float(os.environ.get("AIR1_ALL_ZONES_MAX_TEST_LOG_LOSS", "10.0")),
        help="Maximum blind-test log loss. Default 10.0.",
    )
    parser.add_argument(
        "--max-regression",
        type=float,
        default=float(os.environ.get("AIR1_ALL_ZONES_MAX_REGRESSION", "0.10")),
        help="Allow at most this fractional drop vs production blind-test PR-AUC.",
    )
    parser.add_argument("--skip-non-regression", action="store_true")
    parser.add_argument("--fixture-parquet", type=Path, default=None)
    parser.add_argument("--json", action="store_true", help="Emit a JSON summary after the human-readable result.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    summary: dict[str, Any] = {"verdict": "fail"}
    try:
        run_dir = _resolve_run_dir(Path(args.candidate_run))
        manifest = _validate_manifest(run_dir)
        metrics = _load_metrics(run_dir)
        scaler_stats, best_params_payload, checkpoint = training._load_artifacts(run_dir)
        _require_cv_target_contract(manifest, scaler_stats, best_params_payload, checkpoint)
        lookback = int(scaler_stats["lookback"])
        fixture = _build_smoke_frame(run_dir, lookback=lookback, fixture_parquet=args.fixture_parquet)
        reference_time = fixture[training.TIMESTAMP_COLUMN].iloc[-1]
        probabilities = training.predict_all_zone_probabilities(
            fixture,
            artifact_dir=run_dir,
            reference_time=reference_time,
        )
        probability = max(probabilities.values())
    except Exception as exc:
        print(f"FAIL: {type(exc).__name__}: {exc}")
        return 1

    test_metrics = metrics["metrics_by_split"]["test"]
    test_roc_auc = float(test_metrics.get("roc_auc", float("nan")))
    test_pr_auc = float(test_metrics.get("pr_auc", float("nan")))
    positive_rate = float(test_metrics.get("positive_rate", float("nan")))
    n_windows = float(test_metrics.get("n_windows", float("nan")))
    positive_windows = test_metrics.get("positive_windows")
    if positive_windows is None and math.isfinite(positive_rate) and math.isfinite(n_windows):
        positive_windows = positive_rate * n_windows
    positive_windows = float(positive_windows) if positive_windows is not None else float("nan")
    positive_buckets = float(test_metrics.get("positive_buckets", float("nan")))
    positive_events = float(test_metrics.get("positive_events", float("nan")))
    brier_score = float(test_metrics.get("brier_score", float("nan")))
    log_loss = float(test_metrics.get("bce_log_loss", float("nan")))
    summary.update(
        {
            "candidate_run_dir": str(run_dir.resolve()),
            "manifest_validated": manifest is not None,
            "run_id": (manifest or {}).get("run_id") or run_dir.name,
            "test_roc_auc": test_roc_auc,
            "test_pr_auc": test_pr_auc,
            "positive_rate": positive_rate,
            "positive_windows": positive_windows,
            "positive_buckets": positive_buckets,
            "positive_events": positive_events,
            "brier_score": brier_score,
            "bce_log_loss": log_loss,
            "smoke_probability": float(probability),
            "smoke_probabilities": {str(zone_id): float(prob) for zone_id, prob in sorted(probabilities.items())},
            "smoke_reference_time": str(reference_time),
        }
    )

    if not math.isfinite(test_pr_auc) or test_pr_auc < args.min_test_pr_auc:
        print(f"FAIL: blind-test PR-AUC {test_pr_auc:.4f} below minimum {args.min_test_pr_auc}")
        return 1
    if args.min_test_roc_auc > 0 and (not math.isfinite(test_roc_auc) or test_roc_auc < args.min_test_roc_auc):
        print(f"FAIL: blind-test ROC-AUC {test_roc_auc:.4f} below minimum {args.min_test_roc_auc}")
        return 1
    if not math.isfinite(positive_windows) or positive_windows < args.min_positive_windows:
        print(
            f"FAIL: blind-test positive windows {positive_windows:.1f} below minimum "
            f"{args.min_positive_windows}"
        )
        return 1
    if not math.isfinite(positive_buckets) or positive_buckets < args.min_positive_buckets:
        print(
            f"FAIL: blind-test positive buckets {positive_buckets:.1f} below minimum "
            f"{args.min_positive_buckets}"
        )
        return 1
    if not math.isfinite(positive_events) or positive_events < args.min_positive_events:
        print(
            f"FAIL: blind-test positive events {positive_events:.1f} below minimum "
            f"{args.min_positive_events}"
        )
        return 1
    if not math.isfinite(brier_score) or brier_score > args.max_test_brier:
        print(f"FAIL: blind-test Brier score {brier_score:.4f} above maximum {args.max_test_brier}")
        return 1
    if not math.isfinite(log_loss) or log_loss > args.max_test_log_loss:
        print(f"FAIL: blind-test log loss {log_loss:.4f} above maximum {args.max_test_log_loss}")
        return 1
    if not (0.0 <= probability <= 1.0 and math.isfinite(probability)):
        print(f"FAIL: smoke prediction out of range: {probability}")
        return 1

    production_test_pr_auc = None
    if not args.skip_non_regression:
        production_path = _resolve_production_path(Path(args.production_pointer))
        if production_path is None:
            summary["non_regression_check"] = "skipped: no promoted production model"
        else:
            try:
                prod_run_dir = _resolve_run_dir(production_path)
                prod_metrics = _load_metrics(prod_run_dir)
                prod_scaler, prod_params, prod_checkpoint = training._load_artifacts(prod_run_dir)
                _require_cv_target_contract(_validate_manifest(prod_run_dir), prod_scaler, prod_params, prod_checkpoint)
            except Exception as exc:
                print(f"FAIL: cannot read production metrics for non-regression: {exc}")
                return 1
            production_test_pr_auc = float(prod_metrics["metrics_by_split"]["test"].get("pr_auc", float("nan")))
            summary["production_test_pr_auc"] = production_test_pr_auc
            if math.isfinite(production_test_pr_auc):
                allowed_floor = production_test_pr_auc * (1.0 - args.max_regression)
                summary["non_regression_floor"] = allowed_floor
                if test_pr_auc < allowed_floor:
                    print(
                        f"FAIL: candidate blind-test PR-AUC {test_pr_auc:.4f} below "
                        f"non-regression floor {allowed_floor:.4f}"
                    )
                    return 1
                summary["non_regression_check"] = "passed"
            else:
                summary["non_regression_check"] = "skipped: production pr_auc not finite"
    else:
        summary["non_regression_check"] = "skipped: --skip-non-regression"

    summary["verdict"] = "pass"
    print(f"PASS: candidate {run_dir}")
    print(f"  blind-test PR-AUC:  {test_pr_auc:.4f}  (min {args.min_test_pr_auc})")
    print(f"  blind-test ROC-AUC: {test_roc_auc:.4f}")
    print(
        f"  positives: {positive_windows:.1f} windows, {positive_buckets:.1f} buckets, "
        f"{positive_events:.1f} events   positive_rate {positive_rate:.4f}"
    )
    print(f"  Brier/log-loss: {brier_score:.4f} / {log_loss:.4f}")
    print(f"  smoke probability: {probability:.6f}  reference_time {reference_time}")
    if production_test_pr_auc is not None and math.isfinite(production_test_pr_auc):
        print(f"  vs. production PR-AUC {production_test_pr_auc:.4f}")
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())


