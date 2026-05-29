from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from zone5 import feature_builder
from zone5 import feature_contract as training
from zone5 import csv_size_guard


PACKAGE_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PACKAGE_ROOT / "data"

DEFAULT_FEATURES_PARQUET = DATA_DIR / "zone5_training_features.parquet"
DEFAULT_LABELS_CSV = DATA_DIR / "cv_occupancy_zone5_10sec.csv"
DEFAULT_LABELS_PARQUET = DATA_DIR / "cv_occupancy_zone5_10sec.parquet"
DEFAULT_OUTPUT_CSV = DATA_DIR / "zone5_training_cv.csv"
DEFAULT_METADATA_JSON = DATA_DIR / "zone5_training_cv.metadata.json"

CV_TARGET_COLUMN = training.TARGET_COLUMN
LEGACY_CV_TARGET_COLUMNS = ["cv_is_occupied", "is_occupied"]

CV_LABEL_COLUMNS = [
    "timestamp",
    "occupancy_count",
    CV_TARGET_COLUMN,
    "sample_count",
    "min_count",
    "max_count",
    "mean_count",
    "median_count",
    "last_count",
    "first_message_time",
    "last_message_time",
    "source_topic",
]

SEN55_VALUE_FIELDS = feature_builder.SEN55_VALUE_FIELDS
SEN55_FEATURE_COLUMNS = feature_builder.SEN55_FEATURE_COLUMNS


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, Path):
        return _portable_path(value)
    if isinstance(value, (datetime, pd.Timestamp)):
        return None if pd.isna(value) else value.isoformat(sep=" ")
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        value = float(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if pd.isna(value):
        return None
    return value


def _write_json(data: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_json_safe(data), indent=2, sort_keys=True), encoding="utf-8")


def _sha256_of_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _portable_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(PACKAGE_ROOT.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def _read_table(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"Input file not found: {path}")
    suffix = path.suffix.lower()
    if suffix == ".parquet":
        return pd.read_parquet(path)
    if suffix == ".csv":
        return csv_size_guard.read_csv_parts(path)
    raise ValueError(f"Unsupported input format for {path}; expected .parquet or .csv")


def _to_local_naive_timestamp(value: Any) -> pd.Timestamp:
    return feature_builder.to_local_naive_timestamp(value)


def _normalize_timestamp_column(frame: pd.DataFrame, source_label: str) -> pd.DataFrame:
    return feature_builder.normalize_timestamp_column(frame, source_label)


def _add_missing_indicators(frame: pd.DataFrame) -> pd.DataFrame:
    prepared = frame.copy()
    for col in training.RAW_FEATURE_COLUMNS:
        if col not in prepared.columns:
            prepared[col] = np.nan
        prepared[col] = pd.to_numeric(prepared[col], errors="coerce")
        prepared[f"{col}_missing"] = prepared[col].isna().astype(int)
    return prepared


def _clean_features(raw_features: pd.DataFrame) -> pd.DataFrame:
    frame = _normalize_timestamp_column(raw_features, "features")
    frame = _add_missing_indicators(frame)
    keep_columns = ["timestamp", *training.RAW_FEATURE_COLUMNS, *training.MISSING_INDICATOR_COLUMNS]
    frame = frame[keep_columns].copy()
    frame = frame.sort_values("timestamp").drop_duplicates("timestamp", keep="last")
    return frame.reset_index(drop=True)


def aggregate_sen55_by_minute(raw_sen55: pd.DataFrame) -> pd.DataFrame:
    return feature_builder.aggregate_sen55_by_sample(raw_sen55)


def _clean_labels(raw_labels: pd.DataFrame, occupied_threshold: float) -> pd.DataFrame:
    if raw_labels.empty:
        return pd.DataFrame(columns=CV_LABEL_COLUMNS)
    frame = _normalize_timestamp_column(raw_labels, "CV labels")
    for legacy_col in LEGACY_CV_TARGET_COLUMNS:
        if CV_TARGET_COLUMN not in frame.columns and legacy_col in frame.columns:
            frame = frame.rename(columns={legacy_col: CV_TARGET_COLUMN})

    if "occupancy_count" not in frame.columns and "median_count" in frame.columns:
        frame["occupancy_count"] = frame["median_count"]
    if "occupancy_count" not in frame.columns:
        raise ValueError("CV labels are missing required column: occupancy_count")

    for col in [
        "occupancy_count",
        "sample_count",
        "min_count",
        "max_count",
        "mean_count",
        "median_count",
        "last_count",
    ]:
        if col not in frame.columns:
            frame[col] = np.nan
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    for col in ["first_message_time", "last_message_time", "source_topic"]:
        if col not in frame.columns:
            frame[col] = None

    grouped = (
        frame.sort_values("timestamp")
        .groupby("timestamp", as_index=False)
        .agg(
            occupancy_count=("occupancy_count", "median"),
            sample_count=("sample_count", "sum"),
            min_count=("min_count", "min"),
            max_count=("max_count", "max"),
            mean_count=("mean_count", "mean"),
            median_count=("median_count", "median"),
            last_count=("last_count", "last"),
            first_message_time=("first_message_time", "first"),
            last_message_time=("last_message_time", "last"),
            source_topic=("source_topic", "last"),
        )
    )
    grouped[CV_TARGET_COLUMN] = np.where(
        grouped["occupancy_count"].notna(),
        (grouped["occupancy_count"] >= float(occupied_threshold)).astype(float),
        np.nan,
    )
    return grouped[CV_LABEL_COLUMNS].sort_values("timestamp").reset_index(drop=True)


def _time_bounds(frame: pd.DataFrame) -> dict[str, Any]:
    if frame.empty:
        return {"rows": 0, "start": None, "end": None}
    timestamps = pd.to_datetime(frame["timestamp"])
    return {
        "rows": int(len(frame)),
        "start": timestamps.min().isoformat(sep=" "),
        "end": timestamps.max().isoformat(sep=" "),
    }


def _ordered_combined_columns(frame: pd.DataFrame) -> list[str]:
    preferred = [
        "timestamp",
        *training.RAW_FEATURE_COLUMNS,
        *training.MISSING_INDICATOR_COLUMNS,
        CV_TARGET_COLUMN,
        "occupancy_count",
        "sample_count",
        "min_count",
        "max_count",
        "mean_count",
        "median_count",
        "last_count",
        "first_message_time",
        "last_message_time",
        "source_topic",
    ]
    return [col for col in preferred if col in frame.columns] + [
        col for col in frame.columns if col not in preferred
    ]


def _write_outputs(frame: pd.DataFrame, csv_path: Path) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    csv_size_guard.write_dataframe_rolling_atomic(frame, csv_path)


def combine_feature_label_frames(
    raw_features: pd.DataFrame,
    raw_labels: pd.DataFrame | None = None,
    occupied_threshold: float = 1.0,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    features = _clean_features(raw_features)
    labels = (
        _clean_labels(raw_labels, occupied_threshold=occupied_threshold)
        if raw_labels is not None
        else pd.DataFrame(columns=CV_LABEL_COLUMNS)
    )
    joined = features.merge(labels, on="timestamp", how="left", validate="one_to_one")
    joined = _add_missing_indicators(joined)
    if CV_TARGET_COLUMN not in joined.columns:
        joined[CV_TARGET_COLUMN] = np.nan
    joined = joined[_ordered_combined_columns(joined)].sort_values("timestamp").reset_index(drop=True)
    return joined, features, labels


def build_cv_training_data(
    features_path: str | Path = DEFAULT_FEATURES_PARQUET,
    labels_path: str | Path = DEFAULT_LABELS_CSV,
    output_path: str | Path | None = None,
    metadata_path: str | Path = DEFAULT_METADATA_JSON,
    occupied_threshold: float = 1.0,
    output_csv_path: str | Path = DEFAULT_OUTPUT_CSV,
    allow_missing_labels: bool = True,
) -> dict[str, Any]:
    features_path = Path(features_path)
    labels_path = Path(labels_path)
    output_csv_path = Path(output_csv_path)
    metadata_path = Path(metadata_path)

    raw_features = _read_table(features_path)
    if labels_path.is_file():
        raw_labels = _read_table(labels_path)
        labels_found = True
    elif allow_missing_labels:
        raw_labels = None
        labels_found = False
    else:
        raise FileNotFoundError(f"CV labels not found: {labels_path}")

    joined, features, labels = combine_feature_label_frames(
        raw_features,
        raw_labels,
        occupied_threshold=occupied_threshold,
    )

    _write_outputs(joined, output_csv_path)

    raw_feature_null_counts = {
        col: int(joined[col].isna().sum())
        for col in training.RAW_FEATURE_COLUMNS
    }
    metadata = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "target_column": CV_TARGET_COLUMN,
        "model_feature_columns": training.FEATURE_COLUMNS,
        "raw_model_feature_columns": training.RAW_FEATURE_COLUMNS,
        "engineered_time_feature_columns": training.TIME_FEATURE_COLUMNS,
        "missing_indicator_columns": training.MISSING_INDICATOR_COLUMNS,
        "sample_interval_seconds": training.SAMPLE_INTERVAL_SECONDS,
        "audit_columns_not_model_inputs": [
            col
            for col in joined.columns
            if col not in ["timestamp", *training.FEATURE_COLUMNS, CV_TARGET_COLUMN]
        ],
        "window_rule": "timestamp T represents the completed 10-second bucket [T, T+10s)",
        "aggregation": {
            "air1": "per-10-second mean",
            "smart_plug": "per-10-second mean",
            "mmwave": "per-10-second max/any occupied with stale states left null",
            "sen55": "per-10-second mean",
            "cv_occupancy_count": "per-10-second median",
        },
        "occupied_rule": (
            f"{CV_TARGET_COLUMN}=1 when median CV occupancy_count >= {occupied_threshold:g}; "
            f"{CV_TARGET_COLUMN}=0 when median CV occupancy_count < {occupied_threshold:g}; "
            f"{CV_TARGET_COLUMN}=null when no CV label exists for that bucket"
        ),
        "join": {
            "key": "timestamp floored to one completed 10-second bucket",
            "how": "left",
            "feature_rows": int(len(features)),
            "label_rows": int(len(labels)),
            "joined_rows": int(len(joined)),
            "labeled_rows": int(joined[CV_TARGET_COLUMN].notna().sum()),
            "unlabeled_rows": int(joined[CV_TARGET_COLUMN].isna().sum()),
            "feature_time_bounds": _time_bounds(features),
            "label_time_bounds": _time_bounds(labels),
            "joined_time_bounds": _time_bounds(joined),
        },
        "inputs": {
            "features_path": _portable_path(features_path),
            "features_sha256": _sha256_of_file(features_path),
            "labels_path": _portable_path(labels_path),
            "labels_found": labels_found,
            "labels_sha256": _sha256_of_file(labels_path),
        },
        "outputs": {
            "csv_path": _portable_path(output_csv_path),
            "csv_sha256": _sha256_of_file(output_csv_path),
            "csv_parts": csv_size_guard.csv_parts_metadata(output_csv_path, PACKAGE_ROOT),
            "metadata_path": _portable_path(metadata_path),
        },
        "raw_feature_null_counts": raw_feature_null_counts,
        "columns": list(joined.columns),
    }
    _write_json(metadata, metadata_path)

    return metadata


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build one null-preserving 10-second Zone 5 CV training CSV.")
    parser.add_argument("--features", type=Path, default=DEFAULT_FEATURES_PARQUET, help="Zone 5 feature parquet/CSV.")
    parser.add_argument("--labels", type=Path, default=DEFAULT_LABELS_CSV, help="CV occupancy CSV/Parquet.")
    parser.add_argument("--output-csv", type=Path, default=DEFAULT_OUTPUT_CSV, help="Joined training CSV.")
    parser.add_argument(
        "--metadata",
        type=Path,
        default=DEFAULT_METADATA_JSON,
        help="Metadata JSON for the joined training data.",
    )
    parser.add_argument(
        "--occupied-threshold",
        type=float,
        default=1.0,
        help="Occupied means median CV occupancy_count >= this value. Default: 1.",
    )
    parser.add_argument(
        "--require-labels",
        action="store_true",
        help="Fail if the CV label file is missing instead of writing rows with null targets.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    metadata = build_cv_training_data(
        features_path=args.features,
        labels_path=args.labels,
        output_csv_path=args.output_csv,
        metadata_path=args.metadata,
        occupied_threshold=args.occupied_threshold,
        allow_missing_labels=not args.require_labels,
    )
    print(json.dumps(_json_safe(metadata), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
