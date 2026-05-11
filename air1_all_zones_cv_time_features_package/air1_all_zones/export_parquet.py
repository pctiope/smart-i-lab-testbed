from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from air1_all_zones import air1_exporter as csv_training
from air1_all_zones import build_cv_training_data as cv_training
from air1_all_zones import feature_builder
from air1_all_zones.feature_contract import ALL_ZONE_IDS, RAW_FEATURE_COLUMNS, SAMPLE_INTERVAL_SECONDS, TIMESTAMP_COLUMN

SCRIPT_DIR = Path(__file__).resolve().parent
PACKAGE_ROOT = SCRIPT_DIR.parent
DEFAULT_OUTPUT_DIR = PACKAGE_ROOT / "data"
DEFAULT_CV_LABELS_PATH = DEFAULT_OUTPUT_DIR / "cv_occupancy_all_air1_10sec.parquet"
DEFAULT_CV_TRAINING_CSV_PATH = DEFAULT_OUTPUT_DIR / "air1_all_zones_training_cv.csv"
DEFAULT_CV_TRAINING_PATH = DEFAULT_OUTPUT_DIR / "air1_all_zones_training_cv.parquet"
DEFAULT_CV_TRAINING_METADATA_PATH = DEFAULT_OUTPUT_DIR / "air1_all_zones_training_cv.metadata.json"

LONG_FEATURE_COLUMNS = [TIMESTAMP_COLUMN, "zone_id", *RAW_FEATURE_COLUMNS]


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(val) for key, val in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (datetime, pd.Timestamp)):
        if pd.isna(value):
            return None
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "item"):
        try:
            return _json_safe(value.item())
        except (TypeError, ValueError):
            pass
    return value


def _failure_to_metadata(failure: dict[str, Any]) -> dict[str, Any]:
    metadata_keys = [
        "source_key",
        "source_name",
        "endpoint",
        "device_id",
        "device_description",
        "fetch_start",
        "fetch_end",
        "attempts",
        "request_elapsed_seconds",
        "failure",
        "retryable_failure",
        "initial_chunk_number",
        "initial_chunk_count",
    ]
    return {key: _json_safe(failure[key]) for key in metadata_keys if key in failure}


def all_zones_source_devices(devices: list[str] | tuple[str, ...] | set[str]) -> dict[str, list[str]]:
    return csv_training.all_zones_source_devices(devices)


def build_all_zones_source_specs(source_devices: dict[str, list[str]]) -> list[dict[str, Any]]:
    return csv_training.build_all_zones_source_specs(source_devices)


def build_all_zones_training_frame(
    minute_index: list[datetime],
    air_by_minute: dict[datetime, dict[str, dict[int, float]]],
) -> pd.DataFrame:
    return feature_builder.build_all_zones_base_feature_frame(minute_index, air_by_minute)


def all_zones_output_paths(output_dir: str | Path, time_start: datetime, time_end: datetime) -> tuple[Path, Path]:
    output_path = Path(output_dir)
    start_str = time_start.strftime("%Y%m%d_%H%M%S")
    end_str = time_end.strftime("%Y%m%d_%H%M%S")
    parquet_path = output_path / f"air1_all_zones_{start_str}_to_{end_str}.parquet"
    metadata_path = parquet_path.with_suffix(".metadata.json")
    return parquet_path, metadata_path


def write_all_zones_parquet(frame: pd.DataFrame, parquet_path: str | Path) -> float:
    path = Path(parquet_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp.parquet")
    started_at = time.perf_counter()
    try:
        frame.to_parquet(tmp_path, index=False, engine="pyarrow")
        os.replace(tmp_path, path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise
    return time.perf_counter() - started_at


def write_metadata_json(metadata: dict[str, Any], metadata_path: str | Path) -> float:
    path = Path(metadata_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp.json")
    started_at = time.perf_counter()
    try:
        tmp_path.write_text(json.dumps(_json_safe(metadata), indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp_path, path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise
    return time.perf_counter() - started_at


def maybe_build_cv_training_from_export(
    feature_parquet_path: str | Path,
    *,
    cv_labels_path: str | Path = DEFAULT_CV_LABELS_PATH,
    cv_training_csv_path: str | Path = DEFAULT_CV_TRAINING_CSV_PATH,
    cv_training_path: str | Path = DEFAULT_CV_TRAINING_PATH,
    cv_training_metadata_path: str | Path = DEFAULT_CV_TRAINING_METADATA_PATH,
    occupied_threshold: float = 1.0,
    require_cv_labels: bool = False,
) -> dict[str, Any] | None:
    labels_path = Path(cv_labels_path)
    if not labels_path.is_file():
        message = (
            f"CV labels not found: {labels_path}. "
            "The combined training output will keep all zone rows with null occupied targets. "
            "Run `python -m air1_all_zones.occupancy_mqtt_aggregator` while collecting CV labels."
        )
        if require_cv_labels:
            raise FileNotFoundError(message)
        print("\n" + message)

    metadata = cv_training.build_cv_training_data(
        features_path=feature_parquet_path,
        labels_path=labels_path,
        output_csv_path=cv_training_csv_path,
        output_path=cv_training_path,
        metadata_path=cv_training_metadata_path,
        occupied_threshold=occupied_threshold,
        allow_missing_labels=not require_cv_labels,
    )
    print(f"CV-labeled long-form rows: {metadata.get('join', {}).get('joined_rows')}")
    return metadata


def _timestamp_range(frame: pd.DataFrame) -> dict[str, str | None]:
    if frame.empty or TIMESTAMP_COLUMN not in frame.columns:
        return {"start": None, "end": None}
    timestamps = pd.to_datetime(frame[TIMESTAMP_COLUMN], errors="coerce").dropna()
    if timestamps.empty:
        return {"start": None, "end": None}
    return {"start": timestamps.min().isoformat(), "end": timestamps.max().isoformat()}


def _fetch_settings(exporter: "AllZonesParquetExporter") -> dict[str, Any]:
    return {
        "chunk_days": exporter.chunk_days,
        "min_chunk_hours": exporter.min_chunk_hours,
        "api_timeout": exporter.api_timeout,
        "api_retries": exporter.api_retries,
        "max_workers": exporter.max_workers,
        "progress_every": exporter.progress_every,
        "verbose_progress": exporter.verbose_progress,
        "timing_summary": exporter.timing_summary,
    }


def _build_metadata(
    exporter: "AllZonesParquetExporter",
    frame: pd.DataFrame,
    parquet_path: Path,
    time_start: datetime,
    time_end: datetime,
    source_devices: dict[str, list[str]],
    failures: list[dict[str, Any]],
    timing_metrics: dict[str, Any],
) -> dict[str, Any]:
    return {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "zones": list(ALL_ZONE_IDS),
        "parquet_path": str(parquet_path.resolve()),
        "columns": list(frame.columns),
        "sample_interval_seconds": SAMPLE_INTERVAL_SECONDS,
        "row_count": int(len(frame)),
        "timestamp_count": int(pd.to_datetime(frame[TIMESTAMP_COLUMN], errors="coerce").nunique()) if not frame.empty else 0,
        "requested_time_range_utc": {"start": _json_safe(time_start), "end": _json_safe(time_end)},
        "row_time_range_local": _timestamp_range(frame),
        "fetch_settings": _fetch_settings(exporter),
        "source_devices": source_devices,
        "failed_gap_count": len(failures),
        "failed_gaps": [_failure_to_metadata(failure) for failure in failures],
        "timing_metrics": timing_metrics,
    }


class AllZonesParquetExporter(csv_training.Air1Device):
    def export_all_zones_historical_to_parquet(
        self,
        devices: list[str] | tuple[str, ...] | set[str],
        time_start: datetime,
        time_end: datetime,
        output_dir: str | Path = DEFAULT_OUTPUT_DIR,
        sen55_csv: str | Path | None = None,
    ) -> dict[str, Any] | None:
        export_started_at = time.perf_counter()
        minute_index = csv_training.build_local_minute_index(time_start, time_end)
        if not minute_index:
            print("No local 10-second rows fall inside the requested time range")
            return None

        source_devices = all_zones_source_devices(devices)
        if not source_devices["air1"]:
            print("None of the expected AIR-1 sensors are available in the checked device set.")
            return None

        histories_by_source, failures = self._fetch_adaptive_historical_sources(
            build_all_zones_source_specs(source_devices),
            time_start,
            time_end,
        )

        aggregation_started_at = time.perf_counter()
        air_by_minute = csv_training.aggregate_air1_by_minute(
            histories_by_source.get("air1", {}),
            time_start,
            time_end,
        )
        if sen55_csv is None:
            frame = build_all_zones_training_frame(minute_index, air_by_minute)
        else:
            frame = feature_builder.build_all_zones_feature_frame(
                minute_index=minute_index,
                air_by_minute=air_by_minute,
                sen55_csv=sen55_csv,
            )
        aggregation_elapsed_seconds = time.perf_counter() - aggregation_started_at

        parquet_path, metadata_path = all_zones_output_paths(output_dir, time_start, time_end)
        parquet_write_elapsed_seconds = write_all_zones_parquet(frame, parquet_path)
        timing_metrics = {
            "total_elapsed_seconds": time.perf_counter() - export_started_at,
            "historical_fetch_elapsed_seconds": (self.last_fetch_metrics or {}).get("elapsed_seconds", 0.0),
            "aggregation_elapsed_seconds": aggregation_elapsed_seconds,
            "parquet_write_elapsed_seconds": parquet_write_elapsed_seconds,
            "failed_gap_count": len(failures),
            "fetch_metrics": self.last_fetch_metrics or {},
        }
        metadata = _build_metadata(
            exporter=self,
            frame=frame,
            parquet_path=parquet_path,
            time_start=time_start,
            time_end=time_end,
            source_devices=source_devices,
            failures=failures,
            timing_metrics=timing_metrics,
        )
        write_metadata_json(metadata, metadata_path)
        self._print_availability(frame)
        self._print_failed_historical_request_summary(failures)
        return {"parquet_path": str(parquet_path.resolve()), "metadata_path": str(metadata_path.resolve()), "row_count": int(len(frame))}

    @staticmethod
    def _print_availability(frame: pd.DataFrame) -> None:
        print("\nDATA AVAILABILITY BY FEATURE:")
        print("-" * 50)
        row_count = len(frame)
        for column in RAW_FEATURE_COLUMNS:
            available_count = int(frame[column].notna().sum()) if column in frame.columns else 0
            pct = (available_count / row_count) * 100 if row_count else 0.0
            print(f"  {column}: {available_count}/{row_count} ({pct:.1f}%)")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export all AIR-1 zones to a long-form AIR-1 + SEN55 Parquet.")
    parser.add_argument("--time-start", type=csv_training.parse_cli_datetime_utc, default=csv_training.parse_cli_datetime_utc("2026-03-28T03:00:00Z"))
    parser.add_argument("--time-end", type=csv_training.parse_cli_datetime_utc, default=None)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--sen55-table", "--sen55-csv", dest="sen55_csv", type=Path, default=DEFAULT_OUTPUT_DIR / "sen55_data.csv")
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
    cv_group = parser.add_mutually_exclusive_group()
    cv_group.add_argument("--cv-join", dest="cv_join", action="store_true", default=True)
    cv_group.add_argument("--no-cv-join", dest="cv_join", action="store_false")
    parser.add_argument("--cv-labels", type=Path, default=DEFAULT_CV_LABELS_PATH)
    parser.add_argument("--cv-training-output", type=Path, default=DEFAULT_CV_TRAINING_PATH)
    parser.add_argument("--cv-training-csv-output", type=Path, default=DEFAULT_CV_TRAINING_CSV_PATH)
    parser.add_argument("--cv-training-metadata", type=Path, default=DEFAULT_CV_TRAINING_METADATA_PATH)
    parser.add_argument("--cv-occupied-threshold", type=float, default=1.0)
    parser.add_argument("--require-cv-labels", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.time_end is None:
        args.time_end = csv_training.add_one_calendar_month(args.time_start)
    if args.time_end <= args.time_start:
        print("--time-end must be after --time-start")
        return 2

    exporter = AllZonesParquetExporter(
        csv_training.API_URL,
        csv_training.API_KEY,
        api_timeout=args.api_timeout,
        api_retries=args.api_retries,
        max_workers=args.max_workers,
        chunk_days=args.chunk_days,
        min_chunk_hours=args.min_chunk_hours,
        progress_every=args.progress_every,
        verbose_progress=args.verbose_progress,
        timing_summary=args.timing_summary,
    )
    devices = exporter.get_all_devices()
    print(f"\nFound {len(devices)} total AIR-1 devices on network: {devices}")
    if not devices:
        print("No AIR-1 devices found")
        return 1

    working_devices, missing_devices = csv_training.check_expected_air1_sensors(exporter, devices, args.max_workers)
    print(f"\nSummary: {len(working_devices)}/15 expected AIR-1 sensors are active and have data")
    if missing_devices:
        print(f"Missing or inactive sensors: {missing_devices}")

    try:
        result = exporter.export_all_zones_historical_to_parquet(
            working_devices,
            args.time_start,
            args.time_end,
            output_dir=args.output_dir,
            sen55_csv=args.sen55_csv,
        )
    except Exception as error:
        print(f"Error in all-zones Parquet export: {error}")
        import traceback

        traceback.print_exc()
        return 1
    if not result:
        print("\nFailed to export all-zone data")
        return 1

    parquet_path = Path(result["parquet_path"])
    if args.cv_join:
        try:
            maybe_build_cv_training_from_export(
                parquet_path,
                cv_labels_path=args.cv_labels,
                cv_training_csv_path=args.cv_training_csv_output,
                cv_training_path=args.cv_training_output,
                cv_training_metadata_path=args.cv_training_metadata,
                occupied_threshold=args.cv_occupied_threshold,
                require_cv_labels=args.require_cv_labels,
            )
        except Exception as error:
            print(f"\nFailed to build CV-labeled training data: {error}")
            if args.require_cv_labels:
                return 1
    print(f"\nSuccessfully exported all-zone training data to: {parquet_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
