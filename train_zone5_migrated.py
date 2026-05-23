from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
LEGACY_ZONE5_PACKAGE_CANDIDATES = (
    ROOT / "Legacy" / "zone5_cv_time_features_package",
    ROOT / "zone5_cv_time_features_package",
)
DEFAULT_LIVE_OUTPUT_DIR = ROOT / "zone5_cv_time_features_package" / "model"
DEFAULT_TEST_OUTPUT_DIR = ROOT / "test_runs" / "zone5_full_train_test"


def _json_safe(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(inner) for key, inner in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(inner) for inner in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            return str(value)
    return value


def _legacy_training_module():
    package_root = next((path for path in LEGACY_ZONE5_PACKAGE_CANDIDATES if path.exists()), None)
    if package_root is None:
        searched = ", ".join(str(path) for path in LEGACY_ZONE5_PACKAGE_CANDIDATES)
        raise FileNotFoundError(f"Could not find zone5 training package. Searched: {searched}")
    if str(package_root) not in sys.path:
        sys.path.insert(0, str(package_root))
    return importlib.import_module("zone5.training")


def _migrated_module(*, read_only: bool = False):
    if read_only:
        os.environ["DUCKDB_READ_ONLY"] = "1"
    else:
        os.environ.pop("DUCKDB_READ_ONLY", None)

    for module_name in ("zone5_training_migrated", "dataloader", "CSV Training Data Code"):
        if module_name in sys.modules:
            del sys.modules[module_name]
    return importlib.import_module("zone5_training_migrated")


def _snapshot_path(output_dir: Path) -> Path:
    snapshot_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8]
    return output_dir / "training_input_snapshots" / f"zone5_training_input_{snapshot_id}.parquet"


def _required_read_only_snapshot_paths() -> list[Path]:
    training_root = ROOT / "data" / "_training_tables" / "silver"
    return [
        training_root / "zone5_sen55.parquet",
        training_root / "zone5_cv_labels.parquet",
    ]


def build_training_snapshot(
    *,
    output_dir: str | Path,
    rebuild_training_input: bool = False,
    occupied_threshold: float = 1.0,
    read_only_live: bool = False,
) -> dict[str, Any]:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    if read_only_live:
        missing_snapshots = [path for path in _required_read_only_snapshot_paths() if not path.exists()]
        if missing_snapshots:
            missing_text = ", ".join(str(path) for path in missing_snapshots)
            raise ValueError(
                "read-only-live mode requires seeded support-table snapshots. "
                f"Missing: {missing_text}. "
                "Run seed_zone5_live_support_tables.py --rebuild --rebuild-training-input once with the live poller stopped."
            )

    migrated = _migrated_module(read_only=read_only_live)

    training_input = migrated.build_zone5_training_input_from_silver(
        rebuild=rebuild_training_input and not read_only_live,
        occupied_threshold=occupied_threshold,
        persist=not read_only_live,
    )
    if training_input.empty:
        raise ValueError(f"{migrated.SILVER_TRAINING_INPUT} is empty; cannot launch full training")
    if migrated.TARGET_COLUMN not in training_input.columns:
        raise ValueError(f"{migrated.SILVER_TRAINING_INPUT} is missing target column {migrated.TARGET_COLUMN}")

    labeled = training_input.loc[training_input[migrated.TARGET_COLUMN].notna()].copy()
    if labeled.empty:
        raise ValueError(f"{migrated.SILVER_TRAINING_INPUT} has no labeled rows in {migrated.TARGET_COLUMN}")

    snapshot_path = _snapshot_path(output_path)
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    labeled.to_parquet(snapshot_path, index=False)

    return {
        "snapshot_path": snapshot_path,
        "snapshot_rows": int(len(labeled)),
        "source_table": migrated.SILVER_TRAINING_INPUT,
        "target_column": migrated.TARGET_COLUMN,
        "training_input_rows": int(len(training_input)),
        "labeled_rows": int(len(labeled)),
        "null_target_rows": int(training_input[migrated.TARGET_COLUMN].isna().sum()),
        "read_only_live": read_only_live,
    }


def train_zone5_from_silver(
    *,
    output_dir: str | Path,
    rebuild_training_input: bool = False,
    occupied_threshold: float = 1.0,
    n_trials: int = 50,
    optuna_jobs: int | None = None,
    max_epochs: int = 20,
    seed: int = 42,
    allow_degenerate_validation: bool = False,
    bootstrap_fallback: bool = False,
    cv_folds: int = 3,
    min_strict_date_coverage: float = 0.75,
    read_only_live: bool = False,
) -> dict[str, Any]:
    snapshot = build_training_snapshot(
        output_dir=output_dir,
        rebuild_training_input=rebuild_training_input,
        occupied_threshold=occupied_threshold,
        read_only_live=read_only_live,
    )

    legacy_training = _legacy_training_module()
    result = legacy_training.train_zone_5_from_csv(
        parquet_path=snapshot["snapshot_path"],
        output_dir=output_dir,
        n_trials=n_trials,
        optuna_jobs=optuna_jobs,
        max_epochs=max_epochs,
        seed=seed,
        allow_degenerate_validation=allow_degenerate_validation,
        bootstrap_fallback=bootstrap_fallback,
        cv_folds=cv_folds,
        min_strict_date_coverage=min_strict_date_coverage,
    )

    return {
        "mode": "train_zone5_from_silver",
        "output_dir": str(Path(output_dir).resolve()),
        "source_table": snapshot["source_table"],
        "snapshot_path": str(Path(snapshot["snapshot_path"]).resolve()),
        "training_input_rows": snapshot["training_input_rows"],
        "labeled_rows": snapshot["labeled_rows"],
        "null_target_rows": snapshot["null_target_rows"],
        "legacy_training_result": result,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run full Zone 5 training from DuckDB Silver tables")
    parser.add_argument(
        "--mode",
        choices=("live", "test"),
        default="test",
        help="Choose the default output root. Test mode writes under test_runs; live mode writes under model.",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Override the artifact output directory. If omitted, depends on --mode.",
    )
    parser.add_argument("--rebuild", action="store_true", help="Rebuild silver.zone5_training_input before training")
    parser.add_argument("--occupied-threshold", type=float, default=1.0, help="Threshold used when deriving zone_occupied from occupancy_count")
    parser.add_argument("--n-trials", type=int, default=50, help="Number of Optuna trials")
    parser.add_argument("--optuna-jobs", type=int, default=None, help="Parallel Optuna jobs")
    parser.add_argument("--max-epochs", type=int, default=20, help="Maximum epochs per trial")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    parser.add_argument("--cv-folds", type=int, choices=(1, 2, 3), default=3, help="Strict rolling CV folds")
    parser.add_argument("--allow-degenerate-validation", action="store_true", help="Allow single-class validation windows for smoke/bootstrap scenarios")
    parser.add_argument("--bootstrap-fallback", action="store_true", help="Allow bootstrap fallback when strict validation has no viable lookbacks")
    parser.add_argument(
        "--min-strict-date-coverage",
        type=float,
        default=0.75,
        help="Minimum fraction of a full 10-second day required for strict CV date eligibility",
    )
    parser.add_argument("--report-path", default=None, help="Optional JSON report path to write the training result")
    parser.add_argument(
        "--read-only-live",
        action="store_true",
        help="Build the training snapshot without writing DuckDB tables so training can run while api_ingestion.py --poll is active. Requires prior seeded support-table snapshots.",
    )
    parser.add_argument(
        "--single-day-bootstrap",
        action="store_true",
        help="Allow a smoke/bootstrap training run when the snapshot only covers one calendar day by reusing that day for provisional evaluation.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_dir = Path(args.output_dir) if args.output_dir else (
        DEFAULT_LIVE_OUTPUT_DIR if args.mode == "live" else DEFAULT_TEST_OUTPUT_DIR
    )

    result = train_zone5_from_silver(
        output_dir=output_dir,
        rebuild_training_input=args.rebuild,
        occupied_threshold=args.occupied_threshold,
        n_trials=args.n_trials,
        optuna_jobs=args.optuna_jobs,
        max_epochs=args.max_epochs,
        seed=args.seed,
        allow_degenerate_validation=args.allow_degenerate_validation,
        bootstrap_fallback=args.bootstrap_fallback or args.single_day_bootstrap,
        cv_folds=args.cv_folds,
        min_strict_date_coverage=args.min_strict_date_coverage,
        read_only_live=args.read_only_live,
    )

    payload = json.dumps(_json_safe(result), indent=2, sort_keys=True)
    if args.report_path:
        report_path = Path(args.report_path)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
