from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import pandas as pd


ROOT = Path(__file__).resolve().parent
PACKAGE_ROOT = ROOT / "zone5_cv_time_features_package"
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from zone5 import promote_model  # noqa: E402
from zone5 import training  # noqa: E402

import train_zone5_migrated  # noqa: E402


DEFAULT_LOCK_FILE = PACKAGE_ROOT / "model" / "retrain.lock"
DEFAULT_SUMMARY_JSON = PACKAGE_ROOT / "model" / "retrain_status.json"
DEFAULT_OUTPUT_DIR = PACKAGE_ROOT / "model"
DEFAULT_PRODUCTION_POINTER = PACKAGE_ROOT / "model" / "production_run.txt"
DEFAULT_MIN_POSITIVE_WINDOWS = 5
DEFAULT_MIN_POSITIVE_BUCKETS = 5
DEFAULT_MIN_POSITIVE_EVENTS = 1


def _contract_path(path: str | Path) -> Path:
    resolved = Path(path)
    if resolved.is_absolute():
        return resolved
    if resolved.parts and resolved.parts[0] == PACKAGE_ROOT.name:
        return (ROOT / resolved).resolve()
    return (PACKAGE_ROOT / resolved).resolve()


def _json_safe(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(child) for child in value]
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            return str(value)
    return value


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(json.dumps(_json_safe(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp_path, path)


def _read_pointer_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


@contextmanager
def _exclusive_lock(lock_file: Path, *, wait: bool) -> Iterator[bool]:
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_file.open("w", encoding="utf-8")
    try:
        flags = fcntl.LOCK_EX
        if not wait:
            flags |= fcntl.LOCK_NB
        try:
            fcntl.flock(handle.fileno(), flags)
        except BlockingIOError:
            yield False
            return
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()}\nstarted_at={datetime.now(timezone.utc).isoformat()}\n")
        handle.flush()
        yield True
    finally:
        handle.close()


def _bootstrap_fallback_policy(mode: str, production_pointer: Path) -> dict[str, Any]:
    if mode == "always":
        enabled = True
        reason = "explicit_bootstrap_fallback_always"
    elif mode == "never":
        enabled = False
        reason = "explicit_bootstrap_fallback_never"
    elif mode == "auto":
        pointer_text = _read_pointer_text(production_pointer)
        enabled = not bool(pointer_text)
        reason = "production_pointer_missing_or_empty" if enabled else "production_pointer_exists"
    else:
        raise ValueError(f"Unsupported bootstrap fallback mode: {mode}")
    return {
        "mode": mode,
        "enabled": bool(enabled),
        "reason": reason,
        "production_pointer": str(production_pointer),
    }


def _cv_folds_policy(mode: str, production_pointer: Path) -> dict[str, Any]:
    if mode != "auto":
        folds = training.validate_cv_folds(mode)
        return {
            "mode": mode,
            "cv_folds": int(folds),
            "reason": "explicit_retrain_cv_folds",
            "production_pointer": str(production_pointer),
        }

    pointer_text = _read_pointer_text(production_pointer)
    if not pointer_text:
        reason = "production_pointer_empty" if production_pointer.is_file() else "production_pointer_missing"
        return {
            "mode": "auto",
            "cv_folds": 1,
            "reason": reason,
            "production_pointer": str(production_pointer),
        }

    production_run = promote_model._resolve_production_path(production_pointer)
    if production_run is None:
        raise ValueError(f"{production_pointer} points to {pointer_text!r}, but no production run could be resolved")
    production_payload = promote_model._load_run_policy_payload(promote_model._resolve_run_dir(production_run))
    folds, reason = promote_model._next_required_strict_cv_folds(production_payload)
    return {
        "mode": "auto",
        "cv_folds": int(folds),
        "reason": reason,
        "production_pointer": str(production_pointer),
        "production_run": str(production_run),
        "production_validation_mode": promote_model._validation_mode(production_payload),
        "production_cv_folds_used": promote_model._cv_folds_used(production_payload),
    }


def _trim_process_output(text: str, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    return text[-limit:]


def _promote(args: argparse.Namespace) -> dict[str, Any]:
    production_pointer = _contract_path(args.production_pointer)
    previous = _read_pointer_text(production_pointer)
    cmd = [
        sys.executable,
        "-m",
        "zone5.promote_model",
        "--candidate-run",
        str(_contract_path(args.output_dir)),
        "--production-pointer",
        str(production_pointer),
        "--min-positive-windows",
        str(args.min_positive_windows),
        "--min-positive-buckets",
        str(args.min_positive_buckets),
        "--min-positive-events",
        str(args.min_positive_events),
    ]
    if args.promote_skip_smoke:
        cmd.append("--skip-smoke")
    if args.promote_skip_non_regression_smoke:
        cmd.append("--skip-non-regression-smoke")

    result = subprocess.run(cmd, cwd=PACKAGE_ROOT, text=True, capture_output=True, check=False)
    stdout = result.stdout.strip()
    stderr = result.stderr.strip()
    if stdout:
        print(stdout)
    if stderr:
        print(stderr, file=sys.stderr)

    current = _read_pointer_text(production_pointer)
    if result.returncode == 0:
        status = "promoted" if current and current != previous else "unchanged"
    else:
        status = "failed"
    summary: dict[str, Any] = {
        "status": status,
        "returncode": int(result.returncode),
        "candidate_run": str(_contract_path(args.output_dir)),
        "production_pointer": str(production_pointer),
        "previous": previous or None,
        "current": current or None,
        "command": cmd,
    }
    if stdout:
        summary["stdout_tail"] = _trim_process_output(stdout)
    if stderr:
        summary["stderr_tail"] = _trim_process_output(stderr)
    return summary


def _build_training_snapshot(args: argparse.Namespace) -> dict[str, Any]:
    return train_zone5_migrated.build_training_snapshot(
        output_dir=_contract_path(args.output_dir),
        rebuild_training_input=bool(args.rebuild_training_input),
        occupied_threshold=float(args.occupied_threshold),
        read_only_live=bool(args.read_only_live),
    )


def _blind_test_positive_windows_by_lookback(
    frame: pd.DataFrame,
    *,
    cv_folds: int,
    bootstrap_fallback: bool,
    cv_folds_policy: dict[str, Any],
    allow_degenerate_validation: bool,
    min_strict_date_coverage: float,
) -> dict[str, Any]:
    plan = training.select_cv_lookback_plan(
        frame,
        cv_folds=cv_folds,
        bootstrap_fallback=bootstrap_fallback,
        allow_degenerate_validation=allow_degenerate_validation,
        cv_folds_policy=cv_folds_policy,
        min_strict_date_coverage=min_strict_date_coverage,
    )
    test_frame = plan["blind_splits"]["test"]
    evidence = training.blind_test_evidence_by_lookback(test_frame, plan["lookback_candidates"])
    return {
        "plan": plan,
        **evidence,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one Zone 5 BSG retrain cycle with the legacy promotion contract.")
    parser.add_argument("--read-only-live", action="store_true", help="Read training snapshots without taking the live DuckDB writer lock")
    parser.add_argument("--rebuild-training-input", action="store_true", help="Rebuild silver.zone5_training_input before snapshotting")
    parser.add_argument("--occupied-threshold", type=float, default=1.0)
    parser.add_argument("--lock-file", type=Path, default=DEFAULT_LOCK_FILE)
    parser.add_argument("--wait-for-lock", action="store_true")
    parser.add_argument("--summary-json", type=Path, default=DEFAULT_SUMMARY_JSON)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--n-trials", type=int, default=training.DEFAULT_N_TRIALS)
    parser.add_argument("--optuna-jobs", type=int, default=None)
    parser.add_argument("--max-epochs", type=int, default=training.DEFAULT_MAX_EPOCHS)
    parser.add_argument("--seed", type=int, default=training.DEFAULT_SEED)
    parser.add_argument("--allow-degenerate-validation", action="store_true")
    parser.add_argument("--bootstrap-fallback", choices=["auto", "always", "never"], default="auto")
    parser.add_argument("--cv-folds", choices=["auto", "1", "2", "3"], default="auto")
    promote_group = parser.add_mutually_exclusive_group()
    promote_group.add_argument("--promote", dest="promote", action="store_true", default=True)
    promote_group.add_argument("--no-promote", dest="promote", action="store_false")
    parser.add_argument("--production-pointer", type=Path, default=DEFAULT_PRODUCTION_POINTER)
    parser.add_argument("--promote-skip-smoke", action="store_true")
    parser.add_argument("--promote-skip-non-regression-smoke", action="store_true")
    parser.add_argument(
        "--min-positive-windows",
        type=int,
        default=int(os.environ.get("ZONE5_MIN_POSITIVE_WINDOWS", str(DEFAULT_MIN_POSITIVE_WINDOWS))),
    )
    parser.add_argument(
        "--min-positive-buckets",
        type=int,
        default=int(os.environ.get("ZONE5_MIN_POSITIVE_BUCKETS", str(DEFAULT_MIN_POSITIVE_BUCKETS))),
    )
    parser.add_argument(
        "--min-positive-events",
        type=int,
        default=int(os.environ.get("ZONE5_MIN_POSITIVE_EVENTS", str(DEFAULT_MIN_POSITIVE_EVENTS))),
    )
    parser.add_argument(
        "--min-strict-date-coverage",
        type=float,
        default=training.STRICT_DATE_MIN_COVERAGE,
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    lock_file = _contract_path(args.lock_file)
    summary_path = _contract_path(args.summary_json)
    with _exclusive_lock(lock_file, wait=bool(args.wait_for_lock)) as locked:
        if not locked:
            payload = {
                "status": "skipped_locked",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "lock_file": str(lock_file),
            }
            _write_json(summary_path, payload)
            print(f"Retrain skipped because another retrain holds {lock_file}")
            return 0

        started_at = datetime.now(timezone.utc).isoformat()
        try:
            snapshot = _build_training_snapshot(args)
            snapshot_path = Path(snapshot["snapshot_path"])
            production_pointer = _contract_path(args.production_pointer)
            bootstrap_policy = _bootstrap_fallback_policy(str(args.bootstrap_fallback), production_pointer)
            cv_policy = _cv_folds_policy(str(args.cv_folds), production_pointer)
            print(
                "Starting Zone 5 BSG retrain: "
                f"snapshot={snapshot_path} cv_folds={cv_policy['cv_folds']} "
                f"bootstrap_fallback={bootstrap_policy['enabled']} read_only_live={bool(args.read_only_live)}"
            )
            evidence_gate_enabled = any(
                int(value) > 0
                for value in [args.min_positive_windows, args.min_positive_buckets, args.min_positive_events]
            )
            if args.promote and evidence_gate_enabled:
                frame, _path, _format = training.load_zone_5_training_data(parquet_path=snapshot_path)
                preflight = _blind_test_positive_windows_by_lookback(
                    frame,
                    cv_folds=int(cv_policy["cv_folds"]),
                    bootstrap_fallback=bool(bootstrap_policy["enabled"]),
                    cv_folds_policy=cv_policy,
                    allow_degenerate_validation=args.allow_degenerate_validation,
                    min_strict_date_coverage=args.min_strict_date_coverage,
                )
                evidence_failures = []
                if int(preflight["max_positive_windows"]) < int(args.min_positive_windows):
                    evidence_failures.append("positive_windows")
                if int(preflight["positive_buckets"]) < int(args.min_positive_buckets):
                    evidence_failures.append("positive_buckets")
                if int(preflight["positive_events"]) < int(args.min_positive_events):
                    evidence_failures.append("positive_events")
                if preflight["plan"]["lookback_candidates"] and evidence_failures:
                    payload = {
                        "status": "skipped_not_promotable_yet",
                        "started_at": started_at,
                        "finished_at": datetime.now(timezone.utc).isoformat(),
                        "snapshot": snapshot,
                        "bootstrap_fallback": bootstrap_policy,
                        "cv_folds": cv_policy,
                        "min_positive_windows": int(args.min_positive_windows),
                        "min_positive_buckets": int(args.min_positive_buckets),
                        "min_positive_events": int(args.min_positive_events),
                        "evidence_failures": evidence_failures,
                        "positive_windows_by_lookback": preflight["positive_windows_by_lookback"],
                        "positive_buckets": preflight["positive_buckets"],
                        "positive_events": preflight["positive_events"],
                        "lookback_candidates": preflight["plan"]["lookback_candidates"],
                        "validation_mode": preflight["plan"]["split_policy"].get("validation_mode"),
                        "split_policy": preflight["plan"]["split_policy"],
                    }
                    _write_json(summary_path, payload)
                    print(
                        "Retrain skipped: blind-test evidence below promotion minimum "
                        f"({', '.join(evidence_failures)})"
                    )
                    return 0

            train_result = training.train_zone_5_from_csv(
                parquet_path=snapshot_path,
                output_dir=_contract_path(args.output_dir),
                n_trials=args.n_trials,
                optuna_jobs=args.optuna_jobs,
                max_epochs=args.max_epochs,
                seed=args.seed,
                allow_degenerate_validation=args.allow_degenerate_validation,
                bootstrap_fallback=bool(bootstrap_policy["enabled"]),
                cv_folds=int(cv_policy["cv_folds"]),
                cv_folds_policy=cv_policy,
                min_strict_date_coverage=args.min_strict_date_coverage,
            )
            promotion = _promote(args) if args.promote else {"status": "disabled"}
            payload = {
                "status": "ok",
                "started_at": started_at,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "snapshot": snapshot,
                "bootstrap_fallback": bootstrap_policy,
                "cv_folds": cv_policy,
                "train_result": train_result,
                "promotion": promotion,
            }
            _write_json(summary_path, payload)
            print(f"Retrain finished: run_id={train_result.get('run_id')} promotion={promotion.get('status')}")
            return 0
        except Exception as exc:
            payload = {
                "status": "error",
                "started_at": started_at,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "error": f"{type(exc).__name__}: {exc}",
                "lock_file": str(lock_file),
            }
            _write_json(summary_path, payload)
            print(f"Retrain failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
