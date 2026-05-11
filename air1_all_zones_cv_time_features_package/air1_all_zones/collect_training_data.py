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

from air1_all_zones import air1_exporter as csv_training
from air1_all_zones import build_cv_training_data as cv_training
from air1_all_zones import csv_size_guard
from air1_all_zones import export_parquet
from air1_all_zones import feature_builder
from air1_all_zones import model as training
from air1_all_zones import promote_model
from air1_all_zones.feature_contract import ZONE_ID_COLUMN


PACKAGE_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PACKAGE_ROOT / "data"
DEFAULT_LIVE_BACKFILL_SECONDS = 120.0


def _nonnegative_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"Expected a number, got '{value}'.") from error
    if parsed < 0:
        raise argparse.ArgumentTypeError(f"Expected a value greater than or equal to 0, got {value}.")
    return parsed


def _default_sen55_csv() -> Path:
    return feature_builder.default_sen55_table(PACKAGE_ROOT, DATA_DIR)


def _default_cv_labels_csv() -> Path:
    return DATA_DIR / "cv_occupancy_all_air1_10sec.csv"


def _json_safe(value: Any) -> Any:
    return cv_training._json_safe(value)


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


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


def _filter_labels_to_time_window(raw_labels: pd.DataFrame | None, time_start: datetime, time_end: datetime) -> pd.DataFrame | None:
    if raw_labels is None or raw_labels.empty:
        return raw_labels
    timestamp_col = "timestamp" if "timestamp" in raw_labels.columns else "minute_start"
    if timestamp_col not in raw_labels.columns:
        return raw_labels
    start_local, end_local = csv_training.requested_local_bounds(time_start, time_end)
    normalized = raw_labels.copy()
    timestamps = normalized[timestamp_col].map(cv_training._to_local_naive_timestamp)
    timestamps = pd.to_datetime(timestamps, errors="coerce").dt.floor(training.SAMPLE_INTERVAL_PANDAS_FREQ)
    mask = timestamps.notna()
    if start_local is not None:
        mask = mask & (timestamps >= pd.Timestamp(start_local))
    if end_local is not None:
        mask = mask & (timestamps < pd.Timestamp(end_local))
    normalized = normalized.loc[mask].copy()
    normalized[timestamp_col] = timestamps.loc[mask]
    return normalized


def _build_sensor_feature_frame(
    exporter: export_parquet.AllZonesParquetExporter,
    devices: list[str],
    time_start: datetime,
    time_end: datetime,
    sen55_csv: Path,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    minute_index = csv_training.build_local_minute_index(time_start, time_end)
    if not minute_index:
        raise ValueError("No completed local 10-second rows fall inside the requested time range")

    source_devices = export_parquet.all_zones_source_devices(devices)
    if not source_devices["air1"]:
        raise RuntimeError("No expected AIR-1 sensors are available in the checked device set.")

    histories_by_source, failures = exporter._fetch_adaptive_historical_sources(
        export_parquet.build_all_zones_source_specs(source_devices),
        time_start,
        time_end,
    )
    aggregation_started = time.perf_counter()
    feature_frame = feature_builder.build_all_zones_feature_frame_from_histories(
        minute_index=minute_index,
        histories_by_source=histories_by_source,
        time_start=time_start,
        time_end=time_end,
        sen55_csv=sen55_csv,
    )
    metrics = {
        "source_devices": source_devices,
        "failed_gap_count": len(failures),
        "failed_gaps": [export_parquet._failure_to_metadata(failure) for failure in failures],
        "aggregation_elapsed_seconds": time.perf_counter() - aggregation_started,
        "fetch_metrics": exporter.last_fetch_metrics or {},
        "sen55_csv": str(sen55_csv.resolve()),
        "sen55_csv_found": sen55_csv.is_file(),
    }
    return feature_frame, metrics


def _coerce_joined_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    prepared = _normalize_join_key_columns(frame)
    prepared = prepared.drop_duplicates(["timestamp", ZONE_ID_COLUMN], keep="last")
    ordered_columns = cv_training._ordered_combined_columns(prepared)
    return prepared[ordered_columns].reset_index(drop=True)


def _normalize_join_key_columns(frame: pd.DataFrame) -> pd.DataFrame:
    prepared = frame.copy()
    prepared["timestamp"] = prepared["timestamp"].map(cv_training._to_local_naive_timestamp)
    prepared = prepared.dropna(subset=["timestamp"])
    prepared["timestamp"] = pd.to_datetime(prepared["timestamp"], errors="coerce").dt.floor(
        training.SAMPLE_INTERVAL_PANDAS_FREQ
    )
    prepared[ZONE_ID_COLUMN] = pd.to_numeric(prepared[ZONE_ID_COLUMN], errors="coerce").astype("Int64")
    prepared = prepared.dropna(subset=["timestamp", ZONE_ID_COLUMN])
    prepared = prepared.sort_values(["timestamp", ZONE_ID_COLUMN])
    return prepared.reset_index(drop=True)


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
    return pd.Timestamp(normalized.max()).floor(training.SAMPLE_INTERVAL_PANDAS_FREQ)


def _row_keys(frame: pd.DataFrame) -> set[tuple[int, int]]:
    if frame.empty:
        return set()
    ts_values = pd.to_datetime(frame["timestamp"], errors="coerce").astype("int64")
    zones = pd.to_numeric(frame[ZONE_ID_COLUMN], errors="coerce").fillna(-1).astype(int)
    return set(zip(ts_values.tolist(), zones.tolist()))


def _append_rows_to_csv(output_csv: Path, new_rows: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, int]]:
    new_rows = _coerce_joined_frame(new_rows)
    existing = _read_existing_training_csv(output_csv)
    existing_keys = _row_keys(existing)
    new_keys = _row_keys(new_rows)
    combined = _merge_training_rows_by_key(existing, new_rows)
    _write_training_csv(combined, output_csv)
    return combined, {
        "existing_rows": int(len(existing)),
        "fetched_rows": int(len(new_rows)),
        "inserted_rows": int(len(new_keys - existing_keys)),
        "updated_rows": int(len(new_keys.intersection(existing_keys))),
        "csv_rows": int(len(combined)),
    }


def _last_present_value(values: pd.Series) -> Any:
    present = values.mask(values == "").dropna()
    return present.iloc[-1] if not present.empty else pd.NA


def _merge_training_rows_by_key(existing: pd.DataFrame, new_rows: pd.DataFrame) -> pd.DataFrame:
    if existing.empty:
        return _coerce_joined_frame(new_rows)
    if new_rows.empty:
        return _coerce_joined_frame(existing)
    combined = pd.concat([existing, new_rows], ignore_index=True, sort=False)
    combined = _normalize_join_key_columns(combined)
    ordered_columns = cv_training._ordered_combined_columns(combined)
    merged_rows: list[dict[str, Any]] = []
    for (timestamp, zone_id), group in combined.groupby(["timestamp", ZONE_ID_COLUMN], sort=True):
        merged_row: dict[str, Any] = {"timestamp": timestamp, ZONE_ID_COLUMN: zone_id}
        for column in ordered_columns:
            if column in {"timestamp", ZONE_ID_COLUMN}:
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
    output_parquet: Path,
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
        keep = ["timestamp", ZONE_ID_COLUMN, *[col for col in training.RAW_FEATURE_COLUMNS if col in joined.columns]]
        cleaned_features = joined[keep].copy()
    if cleaned_labels is None:
        label_columns = [col for col in cv_training.CV_LABEL_COLUMNS if col in joined.columns]
        if label_columns:
            cleaned_labels = joined.loc[joined[cv_training.CV_TARGET_COLUMN].notna(), label_columns].drop_duplicates(["timestamp", ZONE_ID_COLUMN])
        else:
            cleaned_labels = pd.DataFrame(columns=cv_training.CV_LABEL_COLUMNS)
    if cv_training.CV_TARGET_COLUMN not in joined.columns:
        joined[cv_training.CV_TARGET_COLUMN] = pd.NA
    audit_columns = [
        col for col in joined.columns
        if col not in ["timestamp", *training.FEATURE_COLUMNS, cv_training.CV_TARGET_COLUMN]
    ]
    metadata = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "collector": "air1_all_zones.collect_training_data",
        "collector_mode": collector_mode,
        "requested_time_range_utc": requested_time_range,
        "row_count": int(len(joined)),
        "timestamp_count": int(pd.to_datetime(joined["timestamp"], errors="coerce").nunique()) if not joined.empty else 0,
        "labeled_rows": int(joined[cv_training.CV_TARGET_COLUMN].notna().sum()),
        "unlabeled_rows": int(joined[cv_training.CV_TARGET_COLUMN].isna().sum()),
        "target_column": cv_training.CV_TARGET_COLUMN,
        "grouping_column": ZONE_ID_COLUMN,
        "model_feature_columns": training.FEATURE_COLUMNS,
        "raw_model_feature_columns": training.RAW_FEATURE_COLUMNS,
        "missing_indicator_columns": training.MISSING_INDICATOR_COLUMNS,
        "audit_columns_not_model_inputs": audit_columns,
        "sample_interval_seconds": training.SAMPLE_INTERVAL_SECONDS,
        "window_rule": "windows are built within each zone_id; timestamp T is the completed 10-second bucket [T, T+10s)",
        "aggregation": {
            "air1": "per-zone per-10-second mean",
            "sen55": "shared per-10-second mean from existing SEN55 table",
            "cv_occupancy_count": "per-zone per-10-second median joined by timestamp and zone_id",
        },
        "label_scope": "per_zone",
        "occupied_rule": (
            f"{cv_training.CV_TARGET_COLUMN}=1 when the per-zone median CV occupancy_count >= {occupied_threshold:g}; "
            f"{cv_training.CV_TARGET_COLUMN}=0 when the per-zone median CV occupancy_count < {occupied_threshold:g}; "
            f"{cv_training.CV_TARGET_COLUMN}=null when no CV label exists for that timestamp + zone_id"
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
            "parquet_path": cv_training._portable_path(output_parquet),
            "parquet_sha256": cv_training._sha256_of_file(output_parquet),
            "metadata_path": cv_training._portable_path(metadata_path),
        },
        "join": {
            "key": "timestamp + zone_id",
            "how": "left one-to-one from zone rows to per-zone CV labels",
            "feature_rows": int(len(cleaned_features)),
            "label_rows": int(len(cleaned_labels)),
            "joined_rows": int(len(joined)),
        },
        "raw_feature_null_counts": {
            col: int(joined[col].isna().sum()) if col in joined.columns else int(len(joined))
            for col in training.RAW_FEATURE_COLUMNS
        },
        "fetch": fetch_metadata or {},
        "elapsed_seconds": elapsed_seconds,
        "columns": list(joined.columns),
    }
    if live_append is not None:
        metadata["live_append"] = live_append
    return metadata


def _write_parquet_and_metadata_from_csv(
    *,
    output_csv: Path,
    output_parquet: Path,
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
    output_parquet.parent.mkdir(parents=True, exist_ok=True)
    joined.to_parquet(output_parquet, index=False)
    metadata = _joined_frame_metadata(
        joined=joined,
        cleaned_features=None,
        cleaned_labels=None,
        sen55_csv=sen55_csv,
        cv_labels=cv_labels,
        output_csv=output_csv,
        output_parquet=output_parquet,
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


def _promote_after_retrain(args: argparse.Namespace) -> dict[str, Any]:
    production_pointer = Path(args.production_pointer)
    previous = _read_pointer_text(production_pointer)
    cmd = [
        sys.executable,
        "-m",
        "air1_all_zones.promote_model",
        "--candidate-run",
        str(args.retrain_output_dir),
        "--production-pointer",
        str(production_pointer),
    ]
    if args.promote_skip_smoke:
        cmd.append("--skip-smoke")
    if args.promote_skip_non_regression_smoke:
        cmd.append("--skip-non-regression-smoke")

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
    exporter = export_parquet.AllZonesParquetExporter(
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
    features, fetch_metadata = _build_sensor_feature_frame(exporter, devices, time_start, time_end, sen55_csv)
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
    output_parquet: Path,
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
    output_parquet.parent.mkdir(parents=True, exist_ok=True)
    joined = _coerce_joined_frame(joined)
    csv_size_guard.write_dataframe_rolling_atomic(joined, output_csv)
    joined.to_parquet(output_parquet, index=False)
    metadata = _joined_frame_metadata(
        joined=joined,
        cleaned_features=cleaned_features,
        cleaned_labels=cleaned_labels,
        sen55_csv=sen55_csv,
        cv_labels=cv_labels,
        output_csv=output_csv,
        output_parquet=output_parquet,
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


def _live_window_bounds(args: argparse.Namespace, latest_local_timestamp: pd.Timestamp | None, now_utc: datetime | None = None) -> tuple[datetime, datetime] | None:
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
    resume_start_utc = latest_utc - timedelta(seconds=backfill_seconds) if backfill_seconds > 0 else latest_utc + csv_training.SAMPLE_INTERVAL
    time_start_utc = max(horizon_start_utc, resume_start_utc)
    if time_end_utc <= time_start_utc:
        return None
    return time_start_utc, time_end_utc


def live_append_training_data(args: argparse.Namespace) -> dict[str, Any]:
    if args.append_every_sec <= 0:
        raise ValueError("--append-every-sec must be greater than 0")
    if args.parquet_rebuild_every_hours <= 0:
        raise ValueError("--parquet-rebuild-every-hours must be greater than 0")
    last_rebuild_mono = time.monotonic()
    latest_metadata: dict[str, Any] = {}
    latest_fetch_metadata: dict[str, Any] | None = None
    latest_requested_range: dict[str, Any] | None = None
    rebuild_interval_sec = float(args.parquet_rebuild_every_hours) * 3600.0

    def rebuild(reason: str) -> dict[str, Any]:
        live_info = {
            "enabled": True,
            "append_every_sec": float(args.append_every_sec),
            "backfill_sec": float(args.backfill_sec),
            "parquet_rebuild_every_hours": float(args.parquet_rebuild_every_hours),
            "rebuild_reason": reason,
        }
        metadata = _write_parquet_and_metadata_from_csv(
            output_csv=args.output_csv,
            output_parquet=args.output_parquet,
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
        if args.retrain_after_parquet:
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
                bootstrap_fallback = _retrain_bootstrap_fallback_enabled(args)
                bootstrap_fallback_policy["enabled"] = bool(bootstrap_fallback)
                cv_folds_policy = _retrain_cv_folds_policy(args)
                train_result = training.train_all_zones_from_csv(
                    parquet_path=args.output_parquet,
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
                )
                metadata["live_append"]["retrain_after_parquet"] = {
                    "status": "ok",
                    "reason": reason,
                    "bootstrap_fallback": bootstrap_fallback_policy,
                    "cv_folds": cv_folds_policy,
                    "result": train_result,
                }
                if args.promote_after_retrain:
                    metadata["live_append"]["promote_after_retrain"] = _promote_after_retrain(args)
                cv_training._write_json(metadata, args.metadata)
                print(f"Retrained model after Parquet rebuild: run_id={train_result.get('run_id')}")
            except Exception as exc:
                metadata["live_append"]["retrain_after_parquet"] = {
                    "status": "error",
                    "reason": reason,
                    "bootstrap_fallback": bootstrap_fallback_policy,
                    "cv_folds": cv_folds_policy,
                    "error": f"{type(exc).__name__}: {exc}",
                }
                cv_training._write_json(metadata, args.metadata)
                print(f"Model retraining after Parquet rebuild failed: {type(exc).__name__}: {exc}")
        print(f"Rebuilt {args.output_parquet} ({metadata['row_count']} rows, reason={reason})")
        return metadata

    print(
        "Starting live append collector: "
        f"csv={args.output_csv} parquet={args.output_parquet} append_every={args.append_every_sec}s "
        f"backfill={args.backfill_sec}s parquet_rebuild_every={args.parquet_rebuild_every_hours}h"
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
                    f"Appended all-zone rows for {time_start}..{time_end}: "
                    f"fetched={append_summary['fetched_rows']} inserted={append_summary['inserted_rows']} "
                    f"updated={append_summary['updated_rows']} csv_rows={append_summary['csv_rows']} elapsed={elapsed:.2f}s"
                )
                latest_metadata = {"collector_mode": "live_append", "row_count": int(len(combined)), "append": append_summary, "requested_time_range_utc": latest_requested_range}
            due = time.monotonic() - last_rebuild_mono >= rebuild_interval_sec
            missing_parquet = not args.output_parquet.is_file()
            if csv_size_guard.has_csv_rows(args.output_csv) and (due or missing_parquet):
                latest_metadata = rebuild("interval" if due else "missing_parquet")
                last_rebuild_mono = time.monotonic()
            time.sleep(float(args.append_every_sec))
    except KeyboardInterrupt:
        print("Live collector interrupted; rebuilding Parquet before exit.")
        if csv_size_guard.has_csv_rows(args.output_csv):
            latest_metadata = rebuild("shutdown")
        return latest_metadata


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect a long-form all-zones AIR-1 + SEN55 training dataset with nullable per-zone CV labels."
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
        help="Per-zone CV label source table (.csv or .parquet). Default: data/cv_occupancy_all_air1_10sec.csv.",
    )
    parser.add_argument("--output-csv", type=Path, default=cv_training.DEFAULT_OUTPUT_CSV)
    parser.add_argument("--output-parquet", type=Path, default=cv_training.DEFAULT_OUTPUT_PARQUET)
    parser.add_argument("--metadata", type=Path, default=cv_training.DEFAULT_METADATA_JSON)
    parser.add_argument("--occupied-threshold", type=float, default=1.0)
    parser.add_argument("--require-cv-labels", action="store_true")
    parser.add_argument(
        "--live-append",
        action="store_true",
        help=(
            "Run continuously: append new deduplicated timestamp + zone_id rows to --output-csv every interval, "
            "and rebuild --output-parquet/--metadata on the configured cadence and graceful shutdown."
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
            "In live append mode, refetch this many recent seconds on every cycle so late SEN55 and per-zone CV "
            "labels can fill existing rows. Use 0 to disable. Default: 120."
        ),
    )
    parser.add_argument(
        "--parquet-rebuild-every-hours",
        type=csv_training.positive_float,
        default=1.0,
        help="Live append Parquet/metadata rebuild cadence in hours. Default: 1.",
    )
    retrain_group = parser.add_mutually_exclusive_group()
    retrain_group.add_argument(
        "--retrain-after-parquet",
        dest="retrain_after_parquet",
        action="store_true",
        default=_env_flag("RETRAIN_AFTER_PARQUET", False),
        help="Retrain from the 10-second training Parquet after each live Parquet rebuild. Default: disabled.",
    )
    retrain_group.add_argument(
        "--no-retrain-after-parquet",
        dest="retrain_after_parquet",
        action="store_false",
        help="Disable automatic retraining after live Parquet rebuilds.",
    )
    parser.add_argument("--retrain-output-dir", type=Path, default=training.DEFAULT_OUTPUT_DIR)
    parser.add_argument("--retrain-n-trials", type=int, default=training.DEFAULT_N_TRIALS)
    parser.add_argument("--retrain-optuna-jobs", type=int, default=None)
    parser.add_argument("--retrain-max-epochs", type=int, default=training.DEFAULT_MAX_EPOCHS)
    parser.add_argument("--retrain-seed", type=int, default=training.DEFAULT_SEED)
    parser.add_argument("--retrain-allow-bad-lines", action="store_true")
    parser.add_argument("--retrain-allow-degenerate-validation", action="store_true")
    parser.add_argument(
        "--retrain-bootstrap-fallback",
        choices=["auto", "always", "never"],
        default="auto",
        help=(
            "Control first-model bootstrap fallback during inline retraining. "
            "auto enables it only when production_run.txt is missing or empty. Default: auto."
        ),
    )
    parser.add_argument(
        "--retrain-cv-folds",
        choices=["auto", "1", "2", "3"],
        default="auto",
        help=(
            "Strict rolling CV folds for inline retraining. auto progresses production from 1 to 2 to 3 folds. "
            "Default: auto."
        ),
    )
    parser.add_argument(
        "--min-strict-date-coverage",
        type=float,
        default=training.STRICT_DATE_MIN_COVERAGE,
        help="Minimum fraction of a full 10-second day required for strict CV dates. Default: 0.75.",
    )
    promote_group = parser.add_mutually_exclusive_group()
    promote_group.add_argument(
        "--promote-after-retrain",
        dest="promote_after_retrain",
        action="store_true",
        default=_env_flag("PROMOTE_AFTER_RETRAIN", False),
        help=(
            "Run air1_all_zones.promote_model after each successful live retrain. "
            "Promotion updates production_run.txt only when the candidate passes the promotion checks. "
            "Default: disabled."
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
        default=promote_model.DEFAULT_PRODUCTION_POINTER,
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
    parser.add_argument("--chunk-days", type=csv_training.positive_float, default=csv_training.DEFAULT_CHUNK_DAYS)
    parser.add_argument("--min-chunk-hours", type=csv_training.positive_float, default=csv_training.DEFAULT_MIN_CHUNK_HOURS)
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
            output_parquet=args.output_parquet,
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
        print(f"Error collecting all-zone training data: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(_json_safe(metadata), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
