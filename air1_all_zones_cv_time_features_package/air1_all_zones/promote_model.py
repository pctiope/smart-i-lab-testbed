from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import torch

from air1_all_zones import model as training


PACKAGE_ROOT = Path(__file__).resolve().parent.parent
PRODUCTION_POINTER_FILENAME = "production_run.txt"
DEFAULT_PRODUCTION_POINTER = training.DEFAULT_OUTPUT_DIR / PRODUCTION_POINTER_FILENAME


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(tmp_path, path)


def _package_path(path: str | Path) -> Path:
    resolved = Path(path)
    if not resolved.is_absolute():
        resolved = (PACKAGE_ROOT / resolved).resolve()
    return resolved


def _to_package_relative(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(PACKAGE_ROOT.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


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
        f"and {training.CURRENT_RUN_POINTER}; promotion will then create {PRODUCTION_POINTER_FILENAME}."
    )


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


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_run_payload(run_dir: Path) -> dict[str, Any]:
    manifest_path = run_dir / training.RUN_MANIFEST_FILENAME
    manifest = _load_json(manifest_path) if manifest_path.is_file() else {}
    metrics = _load_json(run_dir / "tables" / "metrics_all_zones.json")
    scaler, params, checkpoint = training._load_artifacts(run_dir)
    training.require_10_second_model_contract(scaler, params, checkpoint)
    target_values = {
        manifest.get("target_column"),
        scaler.get("target_column"),
        checkpoint.get("target_column"),
    }
    bad_targets = sorted(str(value) for value in target_values if value and value != training.TARGET_COLUMN)
    if bad_targets:
        raise ValueError(
            f"{run_dir} is not comparable to the CV target {training.TARGET_COLUMN!r}; "
            f"found target(s): {bad_targets}"
        )
    return {"manifest": manifest, "metrics": metrics, "scaler": scaler, "params": params, "checkpoint": checkpoint}


def _load_run_policy_payload(run_dir: Path) -> dict[str, Any]:
    manifest_path = run_dir / training.RUN_MANIFEST_FILENAME
    tables_dir = run_dir / "tables"
    return {
        "manifest": _load_json(manifest_path) if manifest_path.is_file() else {},
        "metrics": _load_json(tables_dir / "metrics_all_zones.json")
        if (tables_dir / "metrics_all_zones.json").is_file()
        else {},
        "scaler": _load_json(tables_dir / "scaler_stats_all_zones.json")
        if (tables_dir / "scaler_stats_all_zones.json").is_file()
        else {},
        "params": _load_json(tables_dir / "best_params_all_zones.json")
        if (tables_dir / "best_params_all_zones.json").is_file()
        else {},
    }


def _metadata_sources(payload: dict[str, Any]) -> list[dict[str, Any]]:
    metrics = payload.get("metrics") or {}
    manifest = payload.get("manifest") or {}
    scaler = payload.get("scaler") or {}
    params = payload.get("params") or {}
    checkpoint = payload.get("checkpoint") or {}
    sources: list[dict[str, Any]] = []
    for source in [metrics, manifest, scaler, params, checkpoint]:
        policy = source.get("split_policy")
        if isinstance(policy, dict):
            sources.append(policy)
    sources.extend(source for source in [metrics, manifest, scaler, params, checkpoint] if isinstance(source, dict))
    return sources


def _blind_test_metrics(payload: dict[str, Any]) -> dict[str, float]:
    metrics_by_split = payload["metrics"].get("metrics_by_split") or {}
    test_metrics = metrics_by_split.get("blind_test") or metrics_by_split.get("test") or {}
    n_windows = float(test_metrics.get("n_windows", float("nan")))
    positive_rate = float(test_metrics.get("positive_rate", float("nan")))
    positive_windows = test_metrics.get("positive_windows")
    if positive_windows is None and math.isfinite(n_windows) and math.isfinite(positive_rate):
        positive_windows = positive_rate * n_windows
    return {
        "pr_auc": float(test_metrics.get("pr_auc", float("nan"))),
        "roc_auc": float(test_metrics.get("roc_auc", float("nan"))),
        "brier_score": float(test_metrics.get("brier_score", float("nan"))),
        "bce_log_loss": float(test_metrics.get("bce_log_loss", float("nan"))),
        "positive_rate": positive_rate,
        "n_windows": n_windows,
        "positive_windows": float(positive_windows) if positive_windows is not None else float("nan"),
        "positive_buckets": float(test_metrics.get("positive_buckets", float("nan"))),
        "positive_events": float(test_metrics.get("positive_events", float("nan"))),
    }


def _mean_cv_pr_auc(payload: dict[str, Any]) -> float:
    cv_metrics = payload["metrics"].get("cv_metrics") or {}
    return float(cv_metrics.get("best_mean_pr_auc", float("nan")))


def _source_data_path(payload: dict[str, Any]) -> Path | None:
    for source in [payload.get("scaler") or {}, payload.get("metrics") or {}, payload.get("manifest") or {}]:
        for key in ["source_csv_path", "source_data_path", "source_parquet_path"]:
            raw = source.get(key)
            if raw:
                return _package_path(raw)
    return None


def _source_data_format(payload: dict[str, Any], path: Path) -> str:
    for source in [payload.get("scaler") or {}, payload.get("metrics") or {}, payload.get("manifest") or {}]:
        raw = source.get("source_data_format")
        if raw:
            return str(raw).lower()
    if path.suffix.lower() == ".csv":
        return "csv"
    return "parquet"


def _split_policy(payload: dict[str, Any]) -> dict[str, Any]:
    for source in [payload.get("metrics") or {}, payload.get("manifest") or {}, payload.get("scaler") or {}]:
        policy = source.get("split_policy")
        if isinstance(policy, dict):
            return policy
    return {}


def _candidate_blind_test_frame(payload: dict[str, Any]) -> pd.DataFrame | None:
    source_path = _source_data_path(payload)
    if source_path is None:
        return None
    source_format = _source_data_format(payload, source_path)
    if source_format == "csv":
        frame, _path, _format = training.load_all_zones_training_data(csv_path=source_path)
    else:
        frame, _path, _format = training.load_all_zones_training_data(parquet_path=source_path)

    policy = _split_policy(payload)
    bounds = policy.get("blind_test_bounds") if isinstance(policy.get("blind_test_bounds"), dict) else {}
    start_raw = bounds.get("start")
    end_raw = bounds.get("end")
    if start_raw and end_raw:
        timestamps = pd.to_datetime(frame[training.TIMESTAMP_COLUMN])
        start = pd.Timestamp(start_raw)
        end = pd.Timestamp(end_raw)
        test_frame = frame.loc[(timestamps >= start) & (timestamps <= end)].copy().reset_index(drop=True)
    else:
        test_frame = training.blind_test_split(frame)["test"]
    if test_frame.empty:
        raise ValueError("candidate blind-test frame is empty")
    return test_frame


def _candidate_blind_test_evidence(payload: dict[str, Any]) -> dict[str, float]:
    metrics = _blind_test_metrics(payload)
    if math.isfinite(metrics["positive_buckets"]) and math.isfinite(metrics["positive_events"]):
        return metrics

    test_frame = _candidate_blind_test_frame(payload)
    if test_frame is None:
        return metrics
    evidence = training.label_evidence_counts(test_frame)
    return {
        **metrics,
        "positive_buckets": float(evidence["positive_buckets"]),
        "positive_events": float(evidence["positive_events"]),
    }


def _evaluate_run_on_frame(payload: dict[str, Any], frame: pd.DataFrame) -> dict[str, float]:
    scaler = payload["scaler"]
    checkpoint = payload["checkpoint"]
    feature_columns = list(scaler["feature_columns"])
    lookback = int(scaler.get("lookback_rows") or scaler["lookback"])
    fill_values = dict(
        scaler.get("feature_fill_values")
        or checkpoint.get("feature_fill_values")
        or {col: 0.0 for col in training.RAW_FEATURE_COLUMNS}
    )
    prepared = training.apply_training_preprocessing(frame, fill_values)
    missing = [col for col in feature_columns if col not in prepared.columns]
    if missing:
        raise ValueError(f"candidate blind-test frame cannot produce required feature columns: {missing}")
    for col in feature_columns:
        prepared[col] = pd.to_numeric(prepared[col], errors="coerce")
        prepared[col] = (prepared[col] - float(scaler["means"][col])) / float(scaler["stds"][col])

    split_windows = training.build_split_windows({"test": prepared}, lookback)
    y_true = split_windows["test"]["y"]
    if y_true.size == 0:
        raise ValueError(f"candidate blind-test frame produced no labeled windows for lookback={lookback}")

    params = dict(checkpoint.get("params") or payload["params"]["params"])
    device = torch.device("cpu")
    model = training.TunableZoneOccupancyCNN(params=params, input_channels=len(feature_columns)).to(device)
    state_dict = checkpoint.get("model_state_dict", checkpoint)
    model.load_state_dict(state_dict)
    scores = training.predict_scores(
        model,
        split_windows["test"]["X"],
        batch_size=int(params.get("batch_size", 64)),
        device=device,
    )
    return {key: float(value) for key, value in training.split_metrics(y_true, scores).items()}


def _production_metrics_on_candidate_blind_test(
    candidate_payload: dict[str, Any],
    production_payload: dict[str, Any],
) -> dict[str, float] | None:
    candidate_frame = _candidate_blind_test_frame(candidate_payload)
    if candidate_frame is None:
        return None
    return _evaluate_run_on_frame(production_payload, candidate_frame)


def _validation_mode(payload: dict[str, Any]) -> str | None:
    metrics = payload.get("metrics") or {}
    split_policy = metrics.get("split_policy") or {}
    manifest = payload.get("manifest") or {}
    manifest_policy = manifest.get("split_policy") or {}
    return (
        metrics.get("validation_mode")
        or split_policy.get("validation_mode")
        or manifest.get("validation_mode")
        or manifest_policy.get("validation_mode")
    )


def _cv_folds_used(payload: dict[str, Any]) -> int | None:
    for source in _metadata_sources(payload):
        for key in ["cv_folds_used", "strict_cv_folds_used", "cv_folds"]:
            if key not in source:
                continue
            try:
                return int(source[key])
            except (TypeError, ValueError):
                continue
    return None


def _strict_cv_folds_used(payload: dict[str, Any]) -> int | None:
    if _validation_mode(payload) != training.STRICT_VALIDATION_MODE:
        return None
    return _cv_folds_used(payload)


def _next_required_strict_cv_folds(production_payload: dict[str, Any] | None) -> tuple[int, str]:
    if production_payload is None:
        return 1, "no production pointer exists; first strict candidate must prove 1 fold"

    validation_mode = _validation_mode(production_payload)
    if validation_mode == training.BOOTSTRAP_VALIDATION_MODE:
        return 1, "production is bootstrap fallback; next strict candidate must prove 1 fold"
    if validation_mode != training.STRICT_VALIDATION_MODE:
        raise ValueError(f"production validation mode {validation_mode!r} is not supported for progressive promotion")

    production_folds = _strict_cv_folds_used(production_payload)
    if production_folds is None:
        raise ValueError("production strict validation metadata does not record cv_folds_used")
    next_folds = min(int(production_folds) + 1, training.MAX_STRICT_CV_FOLDS)
    if next_folds == int(production_folds):
        return next_folds, f"production already uses {production_folds} strict fold(s); staying at {next_folds}"
    return next_folds, f"production uses {production_folds} strict fold(s); next candidate must prove {next_folds}"


def _strict_one_fold_improvement_replacement(
    candidate_payload: dict[str, Any],
    production_payload: dict[str, Any] | None,
    candidate_cv_folds: int | None,
    candidate_pr_auc: float,
    min_pr_auc_delta: float,
) -> tuple[bool, str | None, float | None]:
    if production_payload is None:
        return False, None, None
    if _validation_mode(candidate_payload) != training.STRICT_VALIDATION_MODE:
        return False, None, None
    if _validation_mode(production_payload) != training.STRICT_VALIDATION_MODE:
        return False, None, None
    if candidate_cv_folds != 1 or _strict_cv_folds_used(production_payload) != 1:
        return False, None, None

    production_pr_auc = _blind_test_metrics(production_payload)["pr_auc"]
    if not math.isfinite(candidate_pr_auc):
        return (
            False,
            "same-level strict 1-fold replacement requires finite candidate reported blind-test PR-AUC",
            None,
        )
    if not math.isfinite(production_pr_auc):
        return (
            False,
            "same-level strict 1-fold replacement requires finite production reported blind-test PR-AUC",
            None,
        )

    required = production_pr_auc + float(min_pr_auc_delta)
    if candidate_pr_auc > required:
        return (
            True,
            "same-level strict 1-fold replacement allowed because candidate reported "
            f"blind-test PR-AUC {candidate_pr_auc:.4f} exceeds production reported "
            f"blind-test PR-AUC {production_pr_auc:.4f} + delta {min_pr_auc_delta:g}",
            production_pr_auc,
        )
    return (
        False,
        "same-level strict 1-fold replacement requires candidate reported blind-test "
        f"PR-AUC {candidate_pr_auc:.4f} to exceed production reported blind-test "
        f"PR-AUC {production_pr_auc:.4f} + delta {min_pr_auc_delta:g}",
        None,
    )


def _uses_degenerate_validation(payload: dict[str, Any]) -> bool:
    metrics = payload.get("metrics") or {}
    manifest = payload.get("manifest") or {}
    params = payload.get("params") or {}
    scaler = payload.get("scaler") or {}
    return any(
        bool(source.get("allow_degenerate_validation"))
        for source in [params, metrics, manifest, scaler]
    )


def _run_smoke(candidate_run: Path, production_pointer: Path, args: argparse.Namespace) -> int:
    cmd = [
        sys.executable,
        str(PACKAGE_ROOT / "smoke_test.py"),
        "--candidate-run",
        str(candidate_run),
        "--production-pointer",
        str(production_pointer),
        "--min-test-pr-auc",
        str(args.min_test_pr_auc),
        "--min-positive-windows",
        str(args.min_positive_windows),
        "--min-positive-buckets",
        str(args.min_positive_buckets),
        "--min-positive-events",
        str(args.min_positive_events),
        "--max-test-brier",
        str(args.max_test_brier),
        "--max-test-log-loss",
        str(args.max_test_log_loss),
    ]
    if args.skip_non_regression_smoke:
        cmd.append("--skip-non-regression")
    print(f"Running smoke test: {' '.join(cmd)}")
    return subprocess.run(cmd, check=False).returncode


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Promote a valid CV-target AIR-1 all zones run to production.")
    parser.add_argument(
        "--candidate-run",
        type=Path,
        default=training.DEFAULT_OUTPUT_DIR,
        help="Run directory or artifact root with current_run.txt. Default: model/.",
    )
    parser.add_argument(
        "--production-pointer",
        type=Path,
        default=DEFAULT_PRODUCTION_POINTER,
        help="production_run.txt path to update atomically.",
    )
    parser.add_argument("--skip-smoke", action="store_true", help="Skip the smoke test.")
    parser.add_argument(
        "--skip-non-regression-smoke",
        action="store_true",
        help="Pass --skip-non-regression to smoke_test.py. Promotion still enforces CV-target comparison.",
    )
    parser.add_argument(
        "--min-test-pr-auc",
        type=float,
        default=float(os.environ.get("AIR1_ALL_ZONES_MIN_TEST_PR_AUC", "0.0")),
        help="Minimum candidate blind-test PR-AUC. Default 0 accepts the first valid baseline.",
    )
    parser.add_argument(
        "--min-mean-cv-pr-auc",
        type=float,
        default=float(os.environ.get("AIR1_ALL_ZONES_MIN_MEAN_CV_PR_AUC", "0.0")),
        help="Minimum rolling-CV mean PR-AUC. Default 0.",
    )
    parser.add_argument(
        "--min-pr-auc-delta",
        type=float,
        default=float(os.environ.get("AIR1_ALL_ZONES_MIN_PR_AUC_DELTA", "0.0")),
        help="Required blind-test PR-AUC improvement over an existing CV-target production run. Default 0.",
    )
    parser.add_argument(
        "--min-positive-windows",
        type=int,
        default=int(os.environ.get("AIR1_ALL_ZONES_MIN_POSITIVE_WINDOWS", "5")),
        help="Minimum positive blind-test windows. Default 5.",
    )
    parser.add_argument(
        "--min-positive-buckets",
        type=int,
        default=int(os.environ.get("AIR1_ALL_ZONES_MIN_POSITIVE_BUCKETS", "5")),
        help="Minimum positive 10-second zone buckets. Default 5.",
    )
    parser.add_argument(
        "--min-positive-events",
        type=int,
        default=int(os.environ.get("AIR1_ALL_ZONES_MIN_POSITIVE_EVENTS", "1")),
        help="Minimum contiguous positive per-zone events. Default 1.",
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
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        candidate_run = _resolve_run_dir(args.candidate_run)
        candidate_payload = _load_run_payload(candidate_run)
    except Exception as exc:
        print(f"FAIL: candidate validation failed: {type(exc).__name__}: {exc}")
        return 1

    candidate_metrics = _blind_test_metrics(candidate_payload)
    candidate_pr_auc = candidate_metrics["pr_auc"]
    candidate_cv_pr_auc = _mean_cv_pr_auc(candidate_payload)
    candidate_evidence = _candidate_blind_test_evidence(candidate_payload)
    positive_windows = candidate_evidence["positive_windows"]
    production_pointer = _package_path(args.production_pointer)
    production_run = _resolve_production_path(production_pointer)
    production_payload: dict[str, Any] | None = None
    one_fold_reported_production_pr_auc: float | None = None
    if production_run is not None:
        try:
            production_payload = _load_run_payload(_resolve_run_dir(production_run))
        except Exception as exc:
            print(f"FAIL: existing production model is not a comparable CV-target run: {exc}")
            return 1

    candidate_validation_mode = _validation_mode(candidate_payload)
    if candidate_validation_mode == training.BOOTSTRAP_VALIDATION_MODE and production_run is not None:
        print(
            "FAIL: bootstrap fallback candidates can only be promoted as the first production model; "
            f"{production_pointer} already points to a production run."
        )
        return 1
    if _uses_degenerate_validation(candidate_payload):
        print("FAIL: degenerate validation candidates are smoke/dev-only and cannot be promoted")
        return 1
    try:
        required_strict_cv_folds, required_reason = _next_required_strict_cv_folds(production_payload)
    except Exception as exc:
        print(f"FAIL: progressive validation policy could not read production metadata: {exc}")
        return 1
    if candidate_validation_mode != training.BOOTSTRAP_VALIDATION_MODE:
        candidate_cv_folds = _strict_cv_folds_used(candidate_payload)
        if candidate_cv_folds is None:
            print(
                "FAIL: strict production candidates must use rolling_calendar validation "
                "and record cv_folds_used metadata."
            )
            return 1
        if candidate_cv_folds < required_strict_cv_folds:
            (
                one_fold_allowed,
                one_fold_reason,
                reported_production_pr_auc,
            ) = _strict_one_fold_improvement_replacement(
                candidate_payload,
                production_payload,
                candidate_cv_folds,
                candidate_pr_auc,
                float(args.min_pr_auc_delta),
            )
            if one_fold_allowed:
                one_fold_reported_production_pr_auc = reported_production_pr_auc
                print(f"INFO: {one_fold_reason}; continuing with remaining promotion gates")
            else:
                suffix = f"; {one_fold_reason}" if one_fold_reason else ""
                print(
                    f"FAIL: candidate uses {candidate_cv_folds} strict validation fold(s), "
                    f"but progressive policy requires at least {required_strict_cv_folds}: "
                    f"{required_reason}{suffix}"
                )
                return 1
    if not math.isfinite(candidate_pr_auc) or candidate_pr_auc < args.min_test_pr_auc:
        print(f"FAIL: candidate blind-test PR-AUC {candidate_pr_auc:.4f} below {args.min_test_pr_auc}")
        return 1
    if not math.isfinite(candidate_cv_pr_auc) or candidate_cv_pr_auc < args.min_mean_cv_pr_auc:
        print(f"FAIL: candidate mean CV PR-AUC {candidate_cv_pr_auc:.4f} below {args.min_mean_cv_pr_auc}")
        return 1
    if not math.isfinite(positive_windows) or positive_windows < args.min_positive_windows:
        print(f"FAIL: candidate positive windows {positive_windows:.1f} below {args.min_positive_windows}")
        return 1
    if (
        not math.isfinite(candidate_evidence["positive_buckets"])
        or candidate_evidence["positive_buckets"] < args.min_positive_buckets
    ):
        print(
            f"FAIL: candidate positive buckets {candidate_evidence['positive_buckets']:.1f} "
            f"below {args.min_positive_buckets}"
        )
        return 1
    if (
        not math.isfinite(candidate_evidence["positive_events"])
        or candidate_evidence["positive_events"] < args.min_positive_events
    ):
        print(
            f"FAIL: candidate positive events {candidate_evidence['positive_events']:.1f} "
            f"below {args.min_positive_events}"
        )
        return 1
    if not math.isfinite(candidate_metrics["brier_score"]) or candidate_metrics["brier_score"] > args.max_test_brier:
        print(f"FAIL: candidate Brier score {candidate_metrics['brier_score']:.4f} above {args.max_test_brier}")
        return 1
    if not math.isfinite(candidate_metrics["bce_log_loss"]) or candidate_metrics["bce_log_loss"] > args.max_test_log_loss:
        print(f"FAIL: candidate log loss {candidate_metrics['bce_log_loss']:.4f} above {args.max_test_log_loss}")
        return 1

    previous_text = production_pointer.read_text(encoding="utf-8").strip() if production_pointer.is_file() else ""
    if production_payload is not None:
        if one_fold_reported_production_pr_auc is not None:
            production_pr_auc = one_fold_reported_production_pr_auc
            comparison_label = "production reported blind-test"
        else:
            try:
                same_window_metrics = _production_metrics_on_candidate_blind_test(candidate_payload, production_payload)
            except Exception as exc:
                print(f"FAIL: cannot score existing production on candidate blind-test window: {exc}")
                return 1
            if same_window_metrics is None:
                print("FAIL: cannot score existing production on candidate blind-test window: candidate source missing")
                return 1
            production_pr_auc = float(same_window_metrics["pr_auc"])
            comparison_label = "production on candidate blind-test"
        required = production_pr_auc + float(args.min_pr_auc_delta)
        if not math.isfinite(production_pr_auc):
            print("FAIL: existing production PR-AUC is not finite; refusing automatic comparison")
            return 1
        if candidate_pr_auc <= required:
            print(
                f"SKIP: candidate blind-test PR-AUC {candidate_pr_auc:.4f} is not better than "
                f"{comparison_label} {production_pr_auc:.4f} + delta {args.min_pr_auc_delta:g}"
            )
            return 0

    if not args.skip_smoke:
        rc = _run_smoke(candidate_run, production_pointer, args)
        if rc != 0:
            print(f"FAIL: smoke test exited {rc}; refusing to promote")
            return rc

    pointer_value = _to_package_relative(candidate_run)
    _atomic_write_text(production_pointer, pointer_value + "\n")
    print(f"Promoted CV-target run to production: {production_pointer}")
    print(f"  previous: {previous_text if previous_text else '(none)'}")
    print(f"  current:  {pointer_value}")
    print(f"  blind-test PR-AUC: {candidate_pr_auc:.4f}; mean CV PR-AUC: {candidate_cv_pr_auc:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())


