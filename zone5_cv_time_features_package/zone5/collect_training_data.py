from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from zone5 import air1_exporter as csv_training
from zone5 import air1_sources
from zone5 import build_cv_training_data as cv_training
from zone5 import csv_size_guard
from zone5 import dataset as cv_dataset
from zone5 import feature_builder
from zone5.feature_contract import (
    FEATURE_COLUMNS,
    MISSING_INDICATOR_COLUMNS,
    RAW_FEATURE_COLUMNS,
    SAMPLE_INTERVAL,
    SAMPLE_INTERVAL_PANDAS_FREQ,
    SAMPLE_INTERVAL_SECONDS,
    TIME_FEATURE_COLUMNS,
)


PACKAGE_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PACKAGE_ROOT / "data"
ZONE_NUM = 5
DEFAULT_LIVE_BACKFILL_SECONDS = 120.0
DEFAULT_RETRAIN_OUTPUT_DIR = PACKAGE_ROOT / "model"
DEFAULT_PRODUCTION_POINTER = DEFAULT_RETRAIN_OUTPUT_DIR / "production_run.txt"
DEFAULT_RETRAIN_N_TRIALS = 50
DEFAULT_RETRAIN_MAX_EPOCHS = 20
DEFAULT_RETRAIN_SEED = 42
DEFAULT_MIN_POSITIVE_WINDOWS = 5
DEFAULT_MIN_POSITIVE_BUCKETS = 5
DEFAULT_MIN_POSITIVE_EVENTS = 1
MIN_STRICT_CV_FOLDS = 1
MAX_STRICT_CV_FOLDS = 3


def _nonnegative_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"Expected a number, got '{value}'.") from error
    if parsed < 0:
        raise argparse.ArgumentTypeError(f"Expected a value greater than or equal to 0, got {value}.")
    return parsed


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _validate_cv_folds(value: int | str) -> int:
    folds = int(value)
    if folds < MIN_STRICT_CV_FOLDS or folds > MAX_STRICT_CV_FOLDS:
        raise ValueError(f"cv_folds must be between {MIN_STRICT_CV_FOLDS} and {MAX_STRICT_CV_FOLDS}; got {value}.")
    return folds


def _default_sen55_csv() -> Path:
    return feature_builder.default_sen55_table(PACKAGE_ROOT, DATA_DIR)


def _default_cv_labels_csv() -> Path:
    return DATA_DIR / "cv_occupancy_zone5_10sec.csv"


def _json_safe(value: Any) -> Any:
    return cv_training._json_safe(value)


def _resolve_time_window(args: argparse.Namespace) -> tuple[datetime, datetime]:
    if args.time_start is not None or args.time_end is not None:
        if args.time_start is None or args.time_end is None:
            raise ValueError("Pass both --time-start and --time-end, or pass only --duration-min.")
        return args.time_start, args.time_end
    duration = float(args.duration_min)
    if duration <= 0:
        raise ValueError("--duration-min must be greater than 0")
    end_utc = csv_training.sample_floor(datetime.now(timezone.utc).replace(tzinfo=None))
    start_utc = end_utc - timedelta(minutes=duration)
    return start_utc, end_utc


def _read_optional_table(path: Path) -> pd.DataFrame | None:
    if not path.is_file():
        return None
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return csv_size_guard.read_csv_parts(path)
    if suffix == ".parquet":
        return pd.read_parquet(path)
    raise ValueError(f"Unsupported table format for {path}; expected .csv or .parquet")


def _filter_labels_to_time_window(
    raw_labels: pd.DataFrame | None,
    time_start: datetime,
    time_end: datetime,
) -> pd.DataFrame | None:
    if raw_labels is None or raw_labels.empty:
        return raw_labels
    timestamp_col = "timestamp" if "timestamp" in raw_labels.columns else "minute_start"
    if timestamp_col not in raw_labels.columns:
        return raw_labels

    start_local, end_local = csv_training.requested_local_bounds(time_start, time_end)
    normalized = raw_labels.copy()
    timestamps = normalized[timestamp_col].map(cv_training._to_local_naive_timestamp)
    timestamps = pd.to_datetime(timestamps, errors="coerce").dt.floor(SAMPLE_INTERVAL_PANDAS_FREQ)
    mask = timestamps.notna()
    if start_local is not None:
        mask = mask & (timestamps >= pd.Timestamp(start_local))
    if end_local is not None:
        mask = mask & (timestamps < pd.Timestamp(end_local))
    normalized = normalized.loc[mask].copy()
    normalized[timestamp_col] = timestamps.loc[mask]
    return normalized


def _build_sensor_feature_frame(
    exporter: air1_sources.Zone5Air1Client,
    devices: list[str],
    time_start: datetime,
    time_end: datetime,
    sen55_csv: Path,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    minute_index = csv_training.build_local_minute_index(time_start, time_end)
    if not minute_index:
        raise ValueError("No completed local 10-second rows fall inside the requested time range")

    source_devices = air1_sources.zone_5_source_devices(devices)
    if not source_devices["air1"]:
        expected_device = csv_training.SENSOR_ORDER[ZONE_NUM - 1]
        raise RuntimeError(f"Zone 5 AIR-1 sensor {expected_device} is not available in the checked device set.")

    histories_by_source, failures = exporter._fetch_adaptive_historical_sources(
        air1_sources.build_zone_5_source_specs(source_devices),
        time_start,
        time_end,
    )

    aggregation_started = time.perf_counter()
    feature_frame = feature_builder.build_zone_5_feature_frame_from_histories(
        minute_index=minute_index,
        histories_by_source=histories_by_source,
        time_start=time_start,
        time_end=time_end,
        sen55_csv=sen55_csv,
    )

    metrics = {
        "source_devices": source_devices,
        "failed_gap_count": len(failures),
        "failed_gaps": [air1_sources.failure_to_metadata(failure) for failure in failures],
        "aggregation_elapsed_seconds": time.perf_counter() - aggregation_started,
        "fetch_metrics": exporter.last_fetch_metrics or {},
        "sen55_csv": str(sen55_csv.resolve()),
        "sen55_csv_found": sen55_csv.is_file(),
    }
    return feature_frame, metrics


def _coerce_joined_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    prepared = frame.copy()
    prepared["timestamp"] = prepared["timestamp"].map(cv_training._to_local_naive_timestamp)
    prepared = prepared.dropna(subset=["timestamp"])
    prepared["timestamp"] = pd.to_datetime(prepared["timestamp"], errors="coerce").dt.floor(
        SAMPLE_INTERVAL_PANDAS_FREQ
    )
    prepared = prepared.dropna(subset=["timestamp"])
    prepared = prepared.sort_values("timestamp").drop_duplicates("timestamp", keep="last")
    ordered_columns = cv_training._ordered_combined_columns(prepared)
    return prepared[ordered_columns].reset_index(drop=True)


def _read_existing_training_csv(path: Path) -> pd.DataFrame:
    if not csv_size_guard.has_csv_data(path):
        return pd.DataFrame()
    return _coerce_joined_frame(csv_size_guard.read_csv_parts(path))


def _write_training_csv(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    csv_size_guard.write_dataframe_rolling_atomic(_coerce_joined_frame(frame), path)


def _latest_csv_timestamp(path: Path) -> pd.Timestamp | None:
    if not csv_size_guard.has_csv_data(path):
        return None
    try:
        timestamps = csv_size_guard.read_csv_parts(path, usecols=["timestamp"])["timestamp"]
    except (ValueError, pd.errors.EmptyDataError):
        return None
    normalized = timestamps.map(cv_training._to_local_naive_timestamp).dropna()
    if normalized.empty:
        return None
    return pd.Timestamp(normalized.max()).floor(SAMPLE_INTERVAL_PANDAS_FREQ)


def _append_rows_to_csv(output_csv: Path, new_rows: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, int]]:
    new_rows = _coerce_joined_frame(new_rows)
    existing = _read_existing_training_csv(output_csv)
    existing_ts = set()
    if not existing.empty:
        existing_ts = set(pd.to_datetime(existing["timestamp"]).astype("int64").tolist())
    new_ts = set()
    if not new_rows.empty:
        new_ts = set(pd.to_datetime(new_rows["timestamp"]).astype("int64").tolist())
    combined = _merge_training_rows_by_timestamp(existing, new_rows)
    _write_training_csv(combined, output_csv)
    return combined, {
        "existing_rows": int(len(existing)),
        "fetched_rows": int(len(new_rows)),
        "inserted_rows": int(len(new_ts - existing_ts)),
        "updated_rows": int(len(new_ts.intersection(existing_ts))),
        "csv_rows": int(len(combined)),
    }


def _last_present_value(values: pd.Series) -> Any:
    present = values.mask(values == "").dropna()
    return present.iloc[-1] if not present.empty else pd.NA


def _merge_training_rows_by_timestamp(existing: pd.DataFrame, new_rows: pd.DataFrame) -> pd.DataFrame:
    if existing.empty:
        return _coerce_joined_frame(new_rows)
    if new_rows.empty:
        return _coerce_joined_frame(existing)

    combined = pd.concat([existing, new_rows], ignore_index=True, sort=False)
    combined["timestamp"] = combined["timestamp"].map(cv_training._to_local_naive_timestamp)
    combined = combined.dropna(subset=["timestamp"])
    combined["timestamp"] = pd.to_datetime(combined["timestamp"], errors="coerce").dt.floor(
        SAMPLE_INTERVAL_PANDAS_FREQ
    )
    combined = combined.dropna(subset=["timestamp"])
    combined = combined.sort_values("timestamp").reset_index(drop=True)
    if combined.empty:
        return combined

    ordered_columns = cv_training._ordered_combined_columns(combined)
    merged_rows: list[dict[str, Any]] = []
    for timestamp, group in combined.groupby("timestamp", sort=True):
        merged_row: dict[str, Any] = {"timestamp": timestamp}
        for column in ordered_columns:
            if column == "timestamp":
                continue
            merged_row[column] = _last_present_value(group[column]) if column in group.columns else pd.NA
        merged_rows.append(merged_row)
    return _coerce_joined_frame(pd.DataFrame(merged_rows, columns=ordered_columns))


def _joined_frame_metadata(
    *,
    joined: pd.DataFrame,
    cleaned_features: pd.DataFrame | None,
    cleaned_labels: pd.DataFrame | None,
    sen55_csv: Path,
    cv_labels: Path,
    output_csv: Path,
    metadata_path: Path,
    occupied_threshold: float,
    requested_time_range: dict[str, Any] | None,
    fetch_metadata: dict[str, Any] | None,
    elapsed_seconds: float | None,
    collector_mode: str,
    live_append: dict[str, Any] | None = None,
) -> dict[str, Any]:
    joined = _coerce_joined_frame(joined)
    if cleaned_features is None:
        cleaned_features = joined[["timestamp", *[col for col in RAW_FEATURE_COLUMNS if col in joined.columns]]].copy()
    if cleaned_labels is None:
        label_columns = [col for col in cv_training.CV_LABEL_COLUMNS if col in joined.columns]
        if label_columns:
            label_mask = pd.Series(False, index=joined.index)
            for col in [cv_training.CV_TARGET_COLUMN, "occupancy_count"]:
                if col in joined.columns:
                    label_mask = label_mask | joined[col].notna()
            cleaned_labels = joined.loc[label_mask, label_columns].copy()
        else:
            cleaned_labels = pd.DataFrame(columns=cv_training.CV_LABEL_COLUMNS)

    if cv_training.CV_TARGET_COLUMN not in joined.columns:
        joined[cv_training.CV_TARGET_COLUMN] = pd.NA

    raw_feature_null_counts = {
        col: int(joined[col].isna().sum()) if col in joined.columns else int(len(joined))
        for col in RAW_FEATURE_COLUMNS
    }
    audit_columns = [
        col
        for col in joined.columns
        if col not in ["timestamp", *FEATURE_COLUMNS, cv_training.CV_TARGET_COLUMN]
    ]
    metadata = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "collector": "zone5.collect_training_data",
        "collector_mode": collector_mode,
        "requested_time_range_utc": requested_time_range,
        "row_count": int(len(joined)),
        "labeled_rows": int(joined[cv_training.CV_TARGET_COLUMN].notna().sum()),
        "unlabeled_rows": int(joined[cv_training.CV_TARGET_COLUMN].isna().sum()),
        "target_column": cv_training.CV_TARGET_COLUMN,
        "model_feature_columns": FEATURE_COLUMNS,
        "raw_model_feature_columns": RAW_FEATURE_COLUMNS,
        "engineered_time_feature_columns": TIME_FEATURE_COLUMNS,
        "missing_indicator_columns": MISSING_INDICATOR_COLUMNS,
        "audit_columns_not_model_inputs": audit_columns,
        "sample_interval_seconds": SAMPLE_INTERVAL_SECONDS,
        "window_rule": "timestamp T represents the completed 10-second bucket [T, T+10s)",
        "aggregation": {
            "air1": "per-10-second mean",
            "smart_plug": "per-10-second mean",
            "mmwave": f"per-10-second max/any occupied; max stale gap {csv_training.MMWAVE_MAX_STALE_MINUTES} minutes",
            "sen55": "per-10-second mean from existing SEN55 table",
            "cv_occupancy_count": "per-10-second median",
        },
        "occupied_rule": (
            f"{cv_training.CV_TARGET_COLUMN}=1 when median CV occupancy_count >= {occupied_threshold:g}; "
            f"{cv_training.CV_TARGET_COLUMN}=0 when median CV occupancy_count < {occupied_threshold:g}; "
            f"{cv_training.CV_TARGET_COLUMN}=null when no CV label exists for that bucket"
        ),
        "inputs": {
            "sen55_csv": cv_training._portable_path(sen55_csv),
            "sen55_csv_found": sen55_csv.is_file(),
            "sen55_csv_sha256": cv_training._sha256_of_file(sen55_csv),
            "cv_labels": cv_training._portable_path(cv_labels),
            "cv_labels_found": cv_labels.is_file(),
            "cv_labels_sha256": cv_training._sha256_of_file(cv_labels),
        },
        "outputs": {
            "csv_path": cv_training._portable_path(output_csv),
            "csv_sha256": cv_training._sha256_of_file(output_csv),
            "csv_parts": csv_size_guard.csv_parts_metadata(output_csv, PACKAGE_ROOT),
            "metadata_path": cv_training._portable_path(metadata_path),
        },
        "join": {
            "how": "left",
            "feature_rows": int(len(cleaned_features)),
            "label_rows": int(len(cleaned_labels)),
            "joined_rows": int(len(joined)),
        },
        "raw_feature_null_counts": raw_feature_null_counts,
        "fetch": fetch_metadata or {},
        "elapsed_seconds": elapsed_seconds,
        "columns": list(joined.columns),
    }
    if live_append is not None:
        metadata["live_append"] = live_append
    return metadata


def _write_metadata_from_csv(
    *,
    output_csv: Path,
    metadata_path: Path,
    sen55_csv: Path,
    cv_labels: Path,
    occupied_threshold: float,
    requested_time_range: dict[str, Any] | None,
    fetch_metadata: dict[str, Any] | None,
    elapsed_seconds: float | None,
    collector_mode: str,
    live_append: dict[str, Any] | None = None,
) -> dict[str, Any]:
    joined = _read_existing_training_csv(output_csv)
    metadata = _joined_frame_metadata(
        joined=joined,
        cleaned_features=None,
        cleaned_labels=None,
        sen55_csv=sen55_csv,
        cv_labels=cv_labels,
        output_csv=output_csv,
        metadata_path=metadata_path,
        occupied_threshold=occupied_threshold,
        requested_time_range=requested_time_range,
        fetch_metadata=fetch_metadata,
        elapsed_seconds=elapsed_seconds,
        collector_mode=collector_mode,
        live_append=live_append,
    )
    cv_training._write_json(metadata, metadata_path)
    return metadata


def _initial_snapshot_refresh_mono(path: Path) -> float:
    now_mono = time.monotonic()
    try:
        snapshot_age_seconds = max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return now_mono
    return now_mono - snapshot_age_seconds


def _read_pointer_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def _retrain_bootstrap_fallback_enabled(args: argparse.Namespace) -> bool:
    mode = str(getattr(args, "retrain_bootstrap_fallback", "auto"))
    if mode == "always":
        return True
    if mode == "never":
        return False
    if mode == "auto":
        return not bool(_read_pointer_text(Path(args.production_pointer)))
    raise ValueError(f"Unsupported --retrain-bootstrap-fallback mode: {mode}")


def _retrain_cv_folds_policy(args: argparse.Namespace) -> dict[str, Any]:
    mode = str(getattr(args, "retrain_cv_folds", "auto"))
    production_pointer = Path(args.production_pointer)
    if mode != "auto":
        folds = _validate_cv_folds(mode)
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

    from zone5 import promote_model

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


def _promote_after_retrain(args: argparse.Namespace) -> dict[str, Any]:
    production_pointer = Path(args.production_pointer)
    previous = _read_pointer_text(production_pointer)
    cmd = [
        sys.executable,
        "-m",
        "zone5.promote_model",
        "--candidate-run",
        str(args.retrain_output_dir),
        "--production-pointer",
        str(production_pointer),
        "--min-positive-windows",
        str(getattr(args, "min_positive_windows", DEFAULT_MIN_POSITIVE_WINDOWS)),
        "--min-positive-buckets",
        str(getattr(args, "min_positive_buckets", DEFAULT_MIN_POSITIVE_BUCKETS)),
        "--min-positive-events",
        str(getattr(args, "min_positive_events", DEFAULT_MIN_POSITIVE_EVENTS)),
    ]
    if args.promote_skip_smoke:
        cmd.append("--skip-smoke")
    if args.promote_skip_non_regression_smoke:
        cmd.append("--skip-non-regression-smoke")
    if getattr(args, "force_promote", False):
        cmd.append("--force-promote")

    result = subprocess.run(
        cmd,
        cwd=PACKAGE_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
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
    summary = {
        "status": status,
        "returncode": int(result.returncode),
        "candidate_run": str(Path(args.retrain_output_dir)),
        "production_pointer": str(production_pointer),
        "previous": previous or None,
        "current": current or None,
        "command": cmd,
    }
    if stdout:
        summary["stdout_tail"] = _trim_process_output(stdout)
    if stderr:
        summary["stderr_tail"] = _trim_process_output(stderr)
    print(f"Auto-promotion after retrain {status}: returncode={result.returncode}")
    return summary


def _fetch_joined_training_frame(
    *,
    time_start: datetime,
    time_end: datetime,
    sen55_csv: Path,
    cv_labels: Path,
    occupied_threshold: float,
    require_cv_labels: bool,
    api_timeout: float,
    api_retries: int,
    max_workers: int,
    chunk_days: float,
    min_chunk_hours: float,
    progress_every: int,
    verbose_progress: bool,
    timing_summary: bool,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame | None, dict[str, Any], float]:
    if time_end <= time_start:
        raise ValueError("--time-end must be after --time-start")

    exporter = air1_sources.Zone5Air1Client(
        csv_training.API_URL,
        csv_training.API_KEY,
        api_timeout=api_timeout,
        api_retries=api_retries,
        max_workers=max_workers,
        chunk_days=chunk_days,
        min_chunk_hours=min_chunk_hours,
        progress_every=progress_every,
        verbose_progress=verbose_progress,
        timing_summary=timing_summary,
    )

    started = time.perf_counter()
    devices = exporter.get_all_devices()
    if not devices:
        raise RuntimeError("No AIR-1 devices found")

    features, fetch_metadata = _build_sensor_feature_frame(
        exporter=exporter,
        devices=devices,
        time_start=time_start,
        time_end=time_end,
        sen55_csv=sen55_csv,
    )
    raw_labels = _read_optional_table(cv_labels)
    if raw_labels is None and require_cv_labels:
        raise FileNotFoundError(f"CV labels not found: {cv_labels}")
    raw_labels_for_window = _filter_labels_to_time_window(raw_labels, time_start, time_end)

    joined, cleaned_features, cleaned_labels = cv_training.combine_feature_label_frames(
        features,
        raw_labels_for_window,
        occupied_threshold=occupied_threshold,
    )
    return joined, cleaned_features, cleaned_labels, raw_labels, fetch_metadata, time.perf_counter() - started


def collect_training_data(
    *,
    time_start: datetime,
    time_end: datetime,
    sen55_csv: Path,
    cv_labels: Path,
    output_csv: Path,
    metadata_path: Path,
    occupied_threshold: float,
    require_cv_labels: bool,
    api_timeout: float,
    api_retries: int,
    max_workers: int,
    chunk_days: float,
    min_chunk_hours: float,
    progress_every: int,
    verbose_progress: bool,
    timing_summary: bool,
) -> dict[str, Any]:
    joined, cleaned_features, cleaned_labels, raw_labels, fetch_metadata, elapsed = _fetch_joined_training_frame(
        time_start=time_start,
        time_end=time_end,
        sen55_csv=sen55_csv,
        cv_labels=cv_labels,
        occupied_threshold=occupied_threshold,
        require_cv_labels=require_cv_labels,
        api_timeout=api_timeout,
        api_retries=api_retries,
        max_workers=max_workers,
        chunk_days=chunk_days,
        min_chunk_hours=min_chunk_hours,
        progress_every=progress_every,
        verbose_progress=verbose_progress,
        timing_summary=timing_summary,
    )
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    joined = _coerce_joined_frame(joined)
    csv_size_guard.write_dataframe_rolling_atomic(joined, output_csv)
    metadata = _joined_frame_metadata(
        joined=joined,
        cleaned_features=cleaned_features,
        cleaned_labels=cleaned_labels,
        sen55_csv=sen55_csv,
        cv_labels=cv_labels,
        output_csv=output_csv,
        metadata_path=metadata_path,
        occupied_threshold=occupied_threshold,
        requested_time_range={"start": time_start, "end": time_end},
        fetch_metadata=fetch_metadata,
        elapsed_seconds=elapsed,
        collector_mode="one_shot",
    )
    metadata["inputs"]["cv_labels_found"] = raw_labels is not None
    cv_training._write_json(metadata, metadata_path)
    return metadata


def _live_window_bounds(
    args: argparse.Namespace,
    latest_local_timestamp: pd.Timestamp | None,
    now_utc: datetime | None = None,
) -> tuple[datetime, datetime] | None:
    if now_utc is None:
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    elif now_utc.tzinfo is not None:
        now_utc = now_utc.astimezone(timezone.utc).replace(tzinfo=None)

    time_end_utc = csv_training.sample_floor(now_utc)
    horizon_start_utc = time_end_utc - timedelta(minutes=float(args.duration_min))
    if latest_local_timestamp is None:
        if args.time_start is not None and args.time_end is not None:
            return args.time_start, args.time_end
        return horizon_start_utc, time_end_utc

    latest_utc = latest_local_timestamp.to_pydatetime() - csv_training.LOCAL_OFFSET
    backfill_seconds = float(getattr(args, "backfill_sec", DEFAULT_LIVE_BACKFILL_SECONDS))
    if backfill_seconds > 0:
        resume_start_utc = latest_utc - timedelta(seconds=backfill_seconds)
    else:
        resume_start_utc = latest_utc + SAMPLE_INTERVAL
    time_start_utc = max(horizon_start_utc, resume_start_utc)
    if time_end_utc <= time_start_utc:
        return None
    return time_start_utc, time_end_utc


def live_append_training_data(args: argparse.Namespace) -> dict[str, Any]:
    if args.append_every_sec <= 0:
        raise ValueError("--append-every-sec must be greater than 0")
    if args.snapshot_refresh_every_hours <= 0:
        raise ValueError("--snapshot-refresh-every-hours must be greater than 0")

    last_refresh_mono = _initial_snapshot_refresh_mono(args.metadata)
    latest_metadata: dict[str, Any] = {}
    latest_fetch_metadata: dict[str, Any] | None = None
    latest_requested_range: dict[str, Any] | None = None
    refresh_interval_sec = float(args.snapshot_refresh_every_hours) * 3600.0

    def refresh_snapshot_metadata(reason: str) -> dict[str, Any]:
        live_info = {
            "enabled": True,
            "append_every_sec": float(args.append_every_sec),
            "backfill_sec": float(args.backfill_sec),
            "snapshot_refresh_every_hours": float(args.snapshot_refresh_every_hours),
            "retrain_after_snapshot": bool(args.retrain_after_snapshot),
            "refresh_reason": reason,
        }
        metadata = _write_metadata_from_csv(
            output_csv=args.output_csv,
            metadata_path=args.metadata,
            sen55_csv=args.sen55_csv,
            cv_labels=args.cv_labels,
            occupied_threshold=args.occupied_threshold,
            requested_time_range=latest_requested_range,
            fetch_metadata=latest_fetch_metadata,
            elapsed_seconds=None,
            collector_mode="live_append",
            live_append=live_info,
        )
        if args.retrain_after_snapshot:
            bootstrap_fallback_policy: dict[str, Any] = {
                "mode": str(args.retrain_bootstrap_fallback),
                "enabled": None,
                "production_pointer": str(Path(args.production_pointer)),
            }
            cv_folds_policy: dict[str, Any] = {
                "mode": str(getattr(args, "retrain_cv_folds", "auto")),
                "cv_folds": None,
                "production_pointer": str(Path(args.production_pointer)),
            }
            try:
                from zone5 import training

                bootstrap_fallback = _retrain_bootstrap_fallback_enabled(args)
                bootstrap_fallback_policy["enabled"] = bool(bootstrap_fallback)
                cv_folds_policy = _retrain_cv_folds_policy(args)
                evidence_gate_enabled = any(
                    int(value) > 0
                    for value in [args.min_positive_windows, args.min_positive_buckets, args.min_positive_events]
                )
                if args.promote_after_retrain and evidence_gate_enabled:
                    frame, _path, _format = training.load_zone_5_training_data(
                        csv_path=args.output_csv,
                        allow_bad_lines=args.retrain_allow_bad_lines,
                    )
                    plan = training.select_cv_lookback_plan(
                        frame,
                        cv_folds=int(cv_folds_policy["cv_folds"]),
                        bootstrap_fallback=bootstrap_fallback,
                        allow_degenerate_validation=args.retrain_allow_degenerate_validation,
                        cv_folds_policy=cv_folds_policy,
                        min_strict_date_coverage=args.min_strict_date_coverage,
                        blind_test_date=args.retrain_blind_test_date,
                    )
                    evidence = training.blind_test_evidence_by_lookback(
                        plan["blind_splits"]["test"],
                        plan["lookback_candidates"],
                    )
                    evidence_failures = []
                    if int(evidence["max_positive_windows"]) < int(args.min_positive_windows):
                        evidence_failures.append("positive_windows")
                    if int(evidence["positive_buckets"]) < int(args.min_positive_buckets):
                        evidence_failures.append("positive_buckets")
                    if int(evidence["positive_events"]) < int(args.min_positive_events):
                        evidence_failures.append("positive_events")
                    if plan["lookback_candidates"] and evidence_failures:
                        metadata["live_append"]["retrain_after_snapshot"] = {
                            "status": "skipped_not_promotable_yet",
                            "reason": reason,
                            "bootstrap_fallback": bootstrap_fallback_policy,
                            "cv_folds": cv_folds_policy,
                            "min_positive_windows": int(args.min_positive_windows),
                            "min_positive_buckets": int(args.min_positive_buckets),
                            "min_positive_events": int(args.min_positive_events),
                            "evidence_failures": evidence_failures,
                            "positive_windows_by_lookback": evidence["positive_windows_by_lookback"],
                            "positive_buckets": evidence["positive_buckets"],
                            "positive_events": evidence["positive_events"],
                            "lookback_candidates": plan["lookback_candidates"],
                            "validation_mode": plan["split_policy"].get("validation_mode"),
                        }
                        cv_training._write_json(metadata, args.metadata)
                        print(
                            "Model retraining after live snapshot skipped: blind-test evidence below "
                            f"promotion minimum ({', '.join(evidence_failures)})"
                        )
                        print(f"Updated {args.metadata} ({metadata['row_count']} rows, reason={reason})")
                        return metadata
                train_result = training.train_zone_5_from_csv(
                    csv_path=args.output_csv,
                    output_dir=args.retrain_output_dir,
                    n_trials=args.retrain_n_trials,
                    optuna_jobs=args.retrain_optuna_jobs,
                    max_epochs=args.retrain_max_epochs,
                    seed=args.retrain_seed,
                    allow_bad_lines=args.retrain_allow_bad_lines,
                    allow_degenerate_validation=args.retrain_allow_degenerate_validation,
                    bootstrap_fallback=bootstrap_fallback,
                    cv_folds=int(cv_folds_policy["cv_folds"]),
                    cv_folds_policy=cv_folds_policy,
                    min_strict_date_coverage=args.min_strict_date_coverage,
                    blind_test_date=args.retrain_blind_test_date,
                )
                metadata["live_append"]["retrain_after_snapshot"] = {
                    "status": "ok",
                    "reason": reason,
                    "bootstrap_fallback": bootstrap_fallback_policy,
                    "cv_folds": cv_folds_policy,
                    "result": train_result,
                }
                if args.promote_after_retrain:
                    metadata["live_append"]["promote_after_retrain"] = _promote_after_retrain(args)
                cv_training._write_json(metadata, args.metadata)
                print(f"Retrained model after live snapshot: run_id={train_result.get('run_id')}")
            except Exception as exc:
                metadata["live_append"]["retrain_after_snapshot"] = {
                    "status": "error",
                    "reason": reason,
                    "bootstrap_fallback": bootstrap_fallback_policy,
                    "cv_folds": cv_folds_policy,
                    "error": f"{type(exc).__name__}: {exc}",
                }
                cv_training._write_json(metadata, args.metadata)
                print(f"Model retraining after live snapshot failed: {type(exc).__name__}: {exc}")
        print(f"Updated {args.metadata} ({metadata['row_count']} rows, reason={reason})")
        return metadata

    print(
        "Starting live append collector: "
        f"csv={args.output_csv} metadata={args.metadata} append_every={args.append_every_sec}s "
        f"backfill={args.backfill_sec}s snapshot_refresh_every={args.snapshot_refresh_every_hours}h"
    )
    try:
        while True:
            latest_local = _latest_csv_timestamp(args.output_csv)
            bounds = _live_window_bounds(args, latest_local)
            if bounds is None:
                print("No completed new 10-second bucket to append yet.")
            else:
                time_start, time_end = bounds
                latest_requested_range = {"start": time_start, "end": time_end}
                joined, _features, _labels, _raw_labels, fetch_metadata, elapsed = _fetch_joined_training_frame(
                    time_start=time_start,
                    time_end=time_end,
                    sen55_csv=args.sen55_csv,
                    cv_labels=args.cv_labels,
                    occupied_threshold=args.occupied_threshold,
                    require_cv_labels=args.require_cv_labels,
                    api_timeout=args.api_timeout,
                    api_retries=args.api_retries,
                    max_workers=args.max_workers,
                    chunk_days=args.chunk_days,
                    min_chunk_hours=args.min_chunk_hours,
                    progress_every=args.progress_every,
                    verbose_progress=args.verbose_progress,
                    timing_summary=args.timing_summary,
                )
                latest_fetch_metadata = fetch_metadata
                combined, append_summary = _append_rows_to_csv(args.output_csv, joined)
                print(
                    f"Appended Zone 5 rows for {time_start}..{time_end}: "
                    f"fetched={append_summary['fetched_rows']} inserted={append_summary['inserted_rows']} "
                    f"updated={append_summary['updated_rows']} csv_rows={append_summary['csv_rows']} "
                    f"elapsed={elapsed:.2f}s"
                )
                latest_metadata = {
                    "collector_mode": "live_append",
                    "row_count": int(len(combined)),
                    "append": append_summary,
                    "backfill_sec": float(args.backfill_sec),
                    "requested_time_range_utc": latest_requested_range,
                }

            due = time.monotonic() - last_refresh_mono >= refresh_interval_sec
            missing_metadata = not args.metadata.is_file()
            if csv_size_guard.has_csv_rows(args.output_csv) and (due or missing_metadata):
                latest_metadata = refresh_snapshot_metadata("interval" if due else "missing_metadata")
                last_refresh_mono = time.monotonic()

            time.sleep(float(args.append_every_sec))
    except KeyboardInterrupt:
        print("Live collector interrupted; finalizing live metadata before exit.")
        if csv_size_guard.has_csv_rows(args.output_csv):
            latest_metadata = refresh_snapshot_metadata("shutdown")
        return latest_metadata


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect one 10-second wide Zone 5 training dataset with CV labels kept nullable."
    )
    parser.add_argument(
        "--duration-min",
        type=float,
        default=240.0,
        help="Trailing completed 10-second bucket duration to collect when --time-start/--time-end are omitted. Default: 240.",
    )
    parser.add_argument("--time-start", type=csv_training.parse_cli_datetime_utc, default=None)
    parser.add_argument("--time-end", type=csv_training.parse_cli_datetime_utc, default=None)
    parser.add_argument(
        "--sen55-table",
        "--sen55-csv",
        dest="sen55_csv",
        type=Path,
        default=_default_sen55_csv(),
        help="SEN55 source table (.csv or .parquet). Default: data/sen55_data.csv. --sen55-csv is kept as an alias.",
    )
    parser.add_argument(
        "--cv-labels",
        type=Path,
        default=_default_cv_labels_csv(),
        help="CV label source table (.csv or .parquet). Default: data/cv_occupancy_zone5_10sec.csv.",
    )
    parser.add_argument("--output-csv", type=Path, default=cv_training.DEFAULT_OUTPUT_CSV)
    parser.add_argument("--metadata", type=Path, default=cv_training.DEFAULT_METADATA_JSON)
    parser.add_argument("--occupied-threshold", type=float, default=1.0)
    parser.add_argument("--require-cv-labels", action="store_true")
    parser.add_argument(
        "--live-append",
        action="store_true",
        help=(
            "Run continuously: append new deduplicated 10-second rows to --output-csv every interval, "
            "and refresh --metadata on the configured cadence and graceful shutdown."
        ),
    )
    parser.add_argument(
        "--append-every-sec",
        type=csv_training.positive_float,
        default=10.0,
        help="Live append polling interval in seconds. Default: 10.",
    )
    parser.add_argument(
        "--backfill-sec",
        type=_nonnegative_float,
        default=DEFAULT_LIVE_BACKFILL_SECONDS,
        help=(
            "In live append mode, refetch this many recent seconds on every cycle so late SEN55 and CV "
            "labels can fill existing rows. Use 0 to disable. Default: 120."
        ),
    )
    parser.add_argument(
        "--snapshot-refresh-every-hours",
        dest="snapshot_refresh_every_hours",
        type=csv_training.positive_float,
        default=float(os.environ.get("SNAPSHOT_REFRESH_EVERY_HOURS", os.environ.get("PARQUET_REBUILD_EVERY_HOURS", "1"))),
        help="Live append metadata refresh cadence in hours. Default: 1.",
    )
    parser.add_argument(
        "--parquet-rebuild-every-hours",
        dest="snapshot_refresh_every_hours",
        type=csv_training.positive_float,
        help=argparse.SUPPRESS,
    )
    retrain_group = parser.add_mutually_exclusive_group()
    retrain_group.add_argument(
        "--retrain-after-snapshot",
        dest="retrain_after_snapshot",
        action="store_true",
        default=_env_flag("RETRAIN_AFTER_SNAPSHOT", _env_flag("RETRAIN_AFTER_PARQUET", False)),
        help="Retrain from the latest 10-second training table after each live snapshot refresh. Default: disabled.",
    )
    retrain_group.add_argument(
        "--no-retrain-after-snapshot",
        dest="retrain_after_snapshot",
        action="store_false",
        help="Disable automatic retraining after live snapshot refreshes.",
    )
    retrain_group.add_argument(
        "--retrain-after-parquet",
        dest="retrain_after_snapshot",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    retrain_group.add_argument(
        "--no-retrain-after-parquet",
        dest="retrain_after_snapshot",
        action="store_false",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--retrain-output-dir", type=Path, default=DEFAULT_RETRAIN_OUTPUT_DIR)
    parser.add_argument("--retrain-n-trials", type=int, default=DEFAULT_RETRAIN_N_TRIALS)
    parser.add_argument("--retrain-optuna-jobs", type=int, default=None)
    parser.add_argument("--retrain-max-epochs", type=int, default=DEFAULT_RETRAIN_MAX_EPOCHS)
    parser.add_argument("--retrain-seed", type=int, default=DEFAULT_RETRAIN_SEED)
    parser.add_argument(
        "--min-positive-windows",
        type=int,
        default=int(os.environ.get("ZONE5_MIN_POSITIVE_WINDOWS", str(DEFAULT_MIN_POSITIVE_WINDOWS))),
        help="Minimum positive blind-test windows needed before running a promotable inline retrain. Default: 5.",
    )
    parser.add_argument(
        "--min-positive-buckets",
        type=int,
        default=int(os.environ.get("ZONE5_MIN_POSITIVE_BUCKETS", str(DEFAULT_MIN_POSITIVE_BUCKETS))),
        help="Minimum positive 10-second blind-test buckets needed before running a promotable inline retrain. Default: 5.",
    )
    parser.add_argument(
        "--min-positive-events",
        type=int,
        default=int(os.environ.get("ZONE5_MIN_POSITIVE_EVENTS", str(DEFAULT_MIN_POSITIVE_EVENTS))),
        help="Minimum contiguous positive blind-test events needed before running a promotable inline retrain. Default: 1.",
    )
    parser.add_argument(
        "--min-strict-date-coverage",
        type=float,
        default=cv_dataset.STRICT_DATE_MIN_COVERAGE,
        help="Minimum fraction of a full 10-second day required for strict CV dates. Default: 0.75.",
    )
    parser.add_argument(
        "--retrain-blind-test-date",
        default=None,
        help=(
            "Hold out exactly this local calendar date during live retraining (YYYY-MM-DD) "
            "and exclude later rows from the retrain."
        ),
    )
    parser.add_argument("--retrain-allow-bad-lines", action="store_true")
    parser.add_argument("--retrain-allow-degenerate-validation", action="store_true")
    parser.add_argument(
        "--retrain-bootstrap-fallback",
        choices=["auto", "always", "never"],
        default="auto",
        help=(
            "Control first-model bootstrap fallback during live retraining. "
            "auto enables it only when production_run.txt is missing or empty. Default: auto."
        ),
    )
    parser.add_argument(
        "--retrain-cv-folds",
        choices=["auto", "1", "2", "3"],
        default="auto",
        help=(
            "Strict rolling CV folds for live retraining. auto progresses production from 1 to 2 to 3 folds. "
            "Default: auto."
        ),
    )
    promote_group = parser.add_mutually_exclusive_group()
    promote_group.add_argument(
        "--promote-after-retrain",
        dest="promote_after_retrain",
        action="store_true",
        default=True,
        help=(
            "Run zone5.promote_model after each successful live retrain. "
            "Promotion updates production_run.txt only when the candidate passes the promotion checks. "
            "Default: enabled."
        ),
    )
    promote_group.add_argument(
        "--no-promote-after-retrain",
        dest="promote_after_retrain",
        action="store_false",
        help="Disable automatic promotion after live retraining.",
    )
    parser.add_argument(
        "--production-pointer",
        type=Path,
        default=DEFAULT_PRODUCTION_POINTER,
        help="production_run.txt path used by automatic promotion. Default: model/production_run.txt.",
    )
    parser.add_argument(
        "--promote-skip-smoke",
        action="store_true",
        help="Pass --skip-smoke to automatic promotion. Intended only for controlled tests.",
    )
    parser.add_argument(
        "--promote-skip-non-regression-smoke",
        action="store_true",
        help="Pass --skip-non-regression-smoke to automatic promotion.",
    )
    parser.add_argument(
        "--force-promote",
        action="store_true",
        help="Pass --force-promote to automatic promotion after candidate gates pass.",
    )
    parser.add_argument("--chunk-days", type=csv_training.positive_float, default=csv_training.DEFAULT_CHUNK_DAYS)
    parser.add_argument(
        "--min-chunk-hours",
        type=csv_training.positive_float,
        default=csv_training.DEFAULT_MIN_CHUNK_HOURS,
    )
    parser.add_argument("--api-timeout", type=csv_training.positive_float, default=csv_training.API_TIMEOUT_SECONDS)
    parser.add_argument("--api-retries", type=csv_training.nonnegative_int, default=csv_training.API_RETRIES)
    parser.add_argument("--max-workers", type=csv_training.positive_int, default=csv_training.MAX_PARALLEL_API_REQUESTS)
    parser.add_argument("--progress-every", type=csv_training.positive_int, default=csv_training.DEFAULT_PROGRESS_EVERY)
    parser.add_argument("--verbose-progress", action="store_true")
    timing_group = parser.add_mutually_exclusive_group()
    timing_group.add_argument("--timing-summary", dest="timing_summary", action="store_true", default=True)
    timing_group.add_argument("--no-timing-summary", dest="timing_summary", action="store_false")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.live_append:
            metadata = live_append_training_data(args)
            print(json.dumps(_json_safe(metadata), indent=2, sort_keys=True))
            return 0
        time_start, time_end = _resolve_time_window(args)
        metadata = collect_training_data(
            time_start=time_start,
            time_end=time_end,
            sen55_csv=args.sen55_csv,
            cv_labels=args.cv_labels,
            output_csv=args.output_csv,
            metadata_path=args.metadata,
            occupied_threshold=args.occupied_threshold,
            require_cv_labels=args.require_cv_labels,
            api_timeout=args.api_timeout,
            api_retries=args.api_retries,
            max_workers=args.max_workers,
            chunk_days=args.chunk_days,
            min_chunk_hours=args.min_chunk_hours,
            progress_every=args.progress_every,
            verbose_progress=args.verbose_progress,
            timing_summary=args.timing_summary,
        )
    except Exception as exc:
        print(f"Error collecting Zone 5 training data: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(_json_safe(metadata), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
