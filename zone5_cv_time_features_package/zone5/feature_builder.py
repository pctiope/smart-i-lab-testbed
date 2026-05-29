from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from zone5 import air1_exporter as csv_training
from zone5 import csv_size_guard
from zone5.feature_contract import (
    RAW_FEATURE_COLUMNS,
    SAMPLE_INTERVAL_PANDAS_FREQ,
    TIMESTAMP_COLUMN,
    ZONE_NUM,
    floor_timestamp_series_to_sample,
)
from zone5.local_time import to_zone5_local_naive_timestamp


BASE_ZONE_5_FEATURE_COLUMNS = [
    "temp_s5",
    "rh_s5",
    "co2_s5",
    "pm25_s5",
    "power_s5",
    "mmwave_s5",
]
SEN55_VALUE_FIELDS = [
    "pm1_0",
    "pm2_5",
    "pm4_0",
    "pm10_0",
    "temperature",
    "humidity",
    "voc",
    "nox",
]
SEN55_FEATURE_COLUMNS = [f"sen55_{field}" for field in SEN55_VALUE_FIELDS]


def default_sen55_table(package_root: Path, data_dir: Path) -> Path:
    return data_dir / "sen55_data.csv"


def default_sen55_csv(package_root: Path, data_dir: Path) -> Path:
    return default_sen55_table(package_root, data_dir)


def to_local_naive_timestamp(value: Any) -> pd.Timestamp:
    return to_zone5_local_naive_timestamp(value)


def normalize_timestamp_column(frame: pd.DataFrame, source_label: str) -> pd.DataFrame:
    if "timestamp" not in frame.columns and "minute_start" in frame.columns:
        frame = frame.rename(columns={"minute_start": "timestamp"})
    if "timestamp" not in frame.columns:
        raise ValueError(f"{source_label} is missing required timestamp column")
    normalized = frame.copy()
    normalized["timestamp"] = normalized["timestamp"].map(to_local_naive_timestamp)
    normalized = normalized.dropna(subset=["timestamp"])
    normalized["timestamp"] = floor_timestamp_series_to_sample(normalized["timestamp"])
    return normalized


def aggregate_sen55_by_sample(raw_sen55: pd.DataFrame) -> pd.DataFrame:
    if "timestamp" not in raw_sen55.columns and "minute_start" in raw_sen55.columns:
        raw_sen55 = raw_sen55.rename(columns={"minute_start": "timestamp"})
    if "timestamp" not in raw_sen55.columns:
        raise ValueError("SEN55 is missing required timestamp column")

    frame = raw_sen55.copy()
    frame["sample_timestamp"] = frame["timestamp"].map(to_local_naive_timestamp)
    frame = frame.dropna(subset=["sample_timestamp"])
    if "sensor_id" in frame.columns:
        frame["sensor_id"] = frame["sensor_id"].fillna("").astype(str)
        frame = frame.sort_values("sample_timestamp")
        frame = frame.drop_duplicates(subset=["sample_timestamp", "sensor_id"], keep="last")
    else:
        frame = frame.sort_values("sample_timestamp")
        frame = frame.drop_duplicates(subset=["sample_timestamp"], keep="last")
    frame["timestamp"] = pd.to_datetime(frame["sample_timestamp"], errors="coerce").dt.floor(
        SAMPLE_INTERVAL_PANDAS_FREQ
    )
    rename_map = {field: f"sen55_{field}" for field in SEN55_VALUE_FIELDS if field in frame.columns}
    frame = frame.rename(columns=rename_map)
    for col in SEN55_FEATURE_COLUMNS:
        if col not in frame.columns:
            frame[col] = pd.NA
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    if frame.empty:
        return pd.DataFrame(columns=[TIMESTAMP_COLUMN, *SEN55_FEATURE_COLUMNS])
    return (
        frame.groupby(TIMESTAMP_COLUMN, as_index=False)[SEN55_FEATURE_COLUMNS]
        .mean()
        .sort_values(TIMESTAMP_COLUMN)
        .reset_index(drop=True)
    )


def aggregate_sen55_by_minute(raw_sen55: pd.DataFrame) -> pd.DataFrame:
    return aggregate_sen55_by_sample(raw_sen55)


def read_sen55_table(path: Path | str) -> pd.DataFrame:
    sen55_path = Path(path)
    suffix = sen55_path.suffix.lower()
    if suffix == ".parquet":
        return pd.read_parquet(sen55_path)
    if suffix == ".csv":
        return csv_size_guard.read_csv_parts(sen55_path)
    raise ValueError(f"Unsupported SEN55 table format for {sen55_path}; expected .csv or .parquet")


def build_zone_5_base_feature_frame(
    minute_index: list[datetime],
    air_by_minute: dict[datetime, dict[str, dict[int, float]]],
    power_by_minute: dict[datetime, dict[int, float]],
    mmwave_by_minute: dict[datetime, dict[int, int]],
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for minute_key in minute_index:
        air_row = air_by_minute.get(minute_key, {})
        rows.append(
            {
                TIMESTAMP_COLUMN: minute_key,
                "temp_s5": air_row.get("temps", {}).get(ZONE_NUM),
                "rh_s5": air_row.get("rhs", {}).get(ZONE_NUM),
                "co2_s5": air_row.get("co2s", {}).get(ZONE_NUM),
                "pm25_s5": air_row.get("pm25s", {}).get(ZONE_NUM),
                "power_s5": power_by_minute.get(minute_key, {}).get(ZONE_NUM),
                "mmwave_s5": mmwave_by_minute.get(minute_key, {}).get(ZONE_NUM),
            }
        )

    frame = pd.DataFrame.from_records(rows, columns=[TIMESTAMP_COLUMN, *BASE_ZONE_5_FEATURE_COLUMNS])
    frame[TIMESTAMP_COLUMN] = pd.to_datetime(frame[TIMESTAMP_COLUMN], errors="coerce")
    for column in BASE_ZONE_5_FEATURE_COLUMNS:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame


def ensure_raw_feature_columns(frame: pd.DataFrame) -> pd.DataFrame:
    prepared = frame.copy()
    for col in RAW_FEATURE_COLUMNS:
        if col not in prepared.columns:
            prepared[col] = pd.NA
        prepared[col] = pd.to_numeric(prepared[col], errors="coerce")
    if TIMESTAMP_COLUMN in prepared.columns:
        prepared = prepared.sort_values(TIMESTAMP_COLUMN)
        prepared = prepared.drop_duplicates(subset=[TIMESTAMP_COLUMN], keep="last")
        prepared = prepared.reset_index(drop=True)
    return prepared


def merge_sen55_features(
    frame: pd.DataFrame,
    sen55_csv: Path | str,
    *,
    logger: Any | None = None,
) -> pd.DataFrame:
    merged = frame.copy()
    sen55_path = Path(sen55_csv)
    if (sen55_path.suffix.lower() == ".csv" and csv_size_guard.has_csv_data(sen55_path)) or sen55_path.is_file():
        try:
            sen55_by_minute = aggregate_sen55_by_sample(read_sen55_table(sen55_path))
            merged = merged.merge(sen55_by_minute, on=TIMESTAMP_COLUMN, how="left")
        except Exception as exc:
            if logger is not None:
                logger.warning("Could not read SEN55 table %s for live features: %s", sen55_path, exc)
    for col in SEN55_FEATURE_COLUMNS:
        if col not in merged.columns:
            merged[col] = pd.NA
        merged[col] = pd.to_numeric(merged[col], errors="coerce")
    return ensure_raw_feature_columns(merged)


def build_zone_5_feature_frame(
    *,
    minute_index: list[datetime],
    air_by_minute: dict[datetime, dict[str, dict[int, float]]],
    power_by_minute: dict[datetime, dict[int, float]],
    mmwave_by_minute: dict[datetime, dict[int, int]],
    sen55_csv: Path | str,
    logger: Any | None = None,
) -> pd.DataFrame:
    base = build_zone_5_base_feature_frame(minute_index, air_by_minute, power_by_minute, mmwave_by_minute)
    return merge_sen55_features(base, sen55_csv, logger=logger)


def build_zone_5_feature_frame_from_histories(
    *,
    minute_index: list[datetime],
    histories_by_source: dict[str, Any],
    time_start: datetime,
    time_end: datetime,
    sen55_csv: Path | str,
    logger: Any | None = None,
) -> pd.DataFrame:
    air_by_minute = csv_training.aggregate_air1_by_minute(
        histories_by_source.get("air1", {}),
        time_start,
        time_end,
    )
    power_by_minute = csv_training.aggregate_smart_plug_by_minute(
        histories_by_source.get("smart_plug", {}),
        time_start,
        time_end,
    )
    mmwave_by_minute = csv_training.aggregate_mmwave_by_minute(
        histories_by_source.get("mmwave", {}),
        time_start,
        time_end,
    )
    return build_zone_5_feature_frame(
        minute_index=minute_index,
        air_by_minute=air_by_minute,
        power_by_minute=power_by_minute,
        mmwave_by_minute=mmwave_by_minute,
        sen55_csv=sen55_csv,
        logger=logger,
    )
