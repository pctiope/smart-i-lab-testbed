"""
zone5_training_migrated.py
==========================

SQL-backed migration helpers for the legacy Zone 5 training pipeline.

The migrated runtime path is DuckDB-only and reads from Bronze/Silver/Gold
tables rather than CSV files.

The legacy pipeline previously stored intermediate artifacts in CSV files:
  - cv_occupancy_zone5_10sec.csv
  - sen55_data.csv
  - zone5_training_cv.csv

This module migrates those artifacts into DuckDB tables under the existing
bronze/silver/gold layout:
  - silver.zone5_cv_labels
  - silver.zone5_sen55
  - silver.zone5_training_input
  - silver.zone5_training_output
  - gold.zone5_training_output

Runtime reads come from DuckDB tables through DataLoader and query_table().
Runtime writes go to DuckDB tables via upsert_table_dataframe().
"""

from __future__ import annotations

import math
from datetime import datetime
from importlib import import_module as _im
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from dataloader import DataLoader


_storage = _im("CSV Training Data Code")

training_table_name = _storage.training_table_name
upsert_table_dataframe = _storage.upsert_table_dataframe
query_table = _storage.query_table

PIPELINE_NAME = "zone5"

ZONE5_SMART_PLUG_DEVICE_ID = "9d88e7"
ZONE5_MMWAVE_DEVICE_ID = "89f464"
ZONE5_MMWAVE_FIELD_CANDIDATES = [
    "zone_1_occupancy",
    "zone_2_occupancy",
    "zone_3_occupancy",
    "radar_zone_1_occupancy",
    "radar_zone_2_occupancy",
    "radar_zone_3_occupancy",
    "detection_target",
    "moving_target",
    "still_target",
    "radar_target",
    "target",
]

RAW_FEATURE_COLUMNS = [
    "temp_s5",
    "rh_s5",
    "co2_s5",
    "pm25_s5",
    "power_s5",
    "mmwave_s5",
    "sen55_pm1_0",
    "sen55_pm2_5",
    "sen55_pm4_0",
    "sen55_pm10_0",
    "sen55_temperature",
    "sen55_humidity",
    "sen55_voc",
    "sen55_nox",
]
MISSING_INDICATOR_COLUMNS = [f"{column}_missing" for column in RAW_FEATURE_COLUMNS]
TIME_FEATURE_COLUMNS = ["hour_sin", "hour_cos", "dow_sin", "dow_cos"]
FEATURE_COLUMNS = [*RAW_FEATURE_COLUMNS, *TIME_FEATURE_COLUMNS]
TARGET_COLUMN = "zone_occupied"
LEGACY_TARGET_COLUMNS = ["cv_is_occupied", "is_occupied"]
SAMPLE_INTERVAL_SECONDS = 10

SILVER_CV_LABELS = training_table_name(PIPELINE_NAME, "cv_labels", layer="silver")
SILVER_SEN55 = training_table_name(PIPELINE_NAME, "sen55", layer="silver")
SILVER_TRAINING_INPUT = training_table_name(PIPELINE_NAME, "training_input", layer="silver")
SILVER_TRAINING_OUTPUT = training_table_name(PIPELINE_NAME, "training_output", layer="silver")
GOLD_TRAINING_OUTPUT = training_table_name(PIPELINE_NAME, "training_output", layer="gold")


def _loader() -> DataLoader:
    return DataLoader()


def _normalize_frame(frame: pd.DataFrame, *, sort_columns: list[str] | None = None) -> pd.DataFrame:
    normalized = frame.copy()
    if "timestamp" in normalized.columns:
        normalized["timestamp"] = pd.to_datetime(normalized["timestamp"], errors="coerce")
    sort_keys = [column for column in (sort_columns or ["timestamp"]) if column in normalized.columns]
    if sort_keys:
        normalized = normalized.sort_values(sort_keys).reset_index(drop=True)
    return normalized


def _empty_training_frame() -> pd.DataFrame:
    base_columns = ["timestamp", *RAW_FEATURE_COLUMNS, *MISSING_INDICATOR_COLUMNS, *TIME_FEATURE_COLUMNS, TARGET_COLUMN]
    return pd.DataFrame(columns=base_columns)


def _floor_timestamp_series(values: pd.Series) -> pd.Series:
    return pd.to_datetime(values, errors="coerce").dt.floor(f"{SAMPLE_INTERVAL_SECONDS}s")


def _prepare_timestamped_frame(frame: pd.DataFrame, *, source_label: str) -> pd.DataFrame:
    prepared = frame.copy()
    if "timestamp" not in prepared.columns:
        raise ValueError(f"{source_label} is missing required timestamp column")
    prepared["timestamp"] = _floor_timestamp_series(prepared["timestamp"])
    prepared = prepared.dropna(subset=["timestamp"]).sort_values("timestamp")
    return prepared.reset_index(drop=True)


def _prepare_air1_zone5_frame(frame: pd.DataFrame) -> pd.DataFrame:
    prepared = _prepare_timestamped_frame(frame, source_label="silver.air_1")
    rename_map = {
        "temperature_s5": "temp_s5",
        "humidity_s5": "rh_s5",
        "pm_2_5_s5": "pm25_s5",
    }
    prepared = prepared.rename(columns={k: v for k, v in rename_map.items() if k in prepared.columns})
    required = ["temp_s5", "rh_s5", "co2_s5", "pm25_s5"]
    missing = [column for column in required if column not in prepared.columns]
    if missing:
        raise ValueError(f"silver.air_1 is missing required Zone 5 columns: {missing}")
    return prepared[["timestamp", *required]].drop_duplicates("timestamp", keep="last").reset_index(drop=True)


def _prepare_power_zone5_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=["timestamp", "power_s5"])
    prepared = _prepare_timestamped_frame(frame, source_label="silver.smart_plug_v2")
    if "device_id" in prepared.columns:
        prepared = prepared.loc[prepared["device_id"].astype(str) == ZONE5_SMART_PLUG_DEVICE_ID].copy()
    if prepared.empty:
        return pd.DataFrame(columns=["timestamp", "power_s5"])
    value_column = "power"
    if value_column not in prepared.columns:
        for candidate in ("power_w", "active_power", "value"):
            if candidate in prepared.columns:
                value_column = candidate
                break
    if value_column not in prepared.columns:
        raise ValueError("silver.smart_plug_v2 is missing a power column for Zone 5")
    prepared["power_s5"] = pd.to_numeric(prepared[value_column], errors="coerce")
    grouped = prepared.groupby("timestamp", as_index=False).agg(power_s5=("power_s5", "mean"))
    return grouped.sort_values("timestamp").reset_index(drop=True)


def _prepare_mmwave_zone5_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=["timestamp", "mmwave_s5"])
    prepared = _prepare_timestamped_frame(frame, source_label="silver.msr_2")
    if "device_id" in prepared.columns:
        prepared = prepared.loc[prepared["device_id"].astype(str) == ZONE5_MMWAVE_DEVICE_ID].copy()
    if prepared.empty:
        return pd.DataFrame(columns=["timestamp", "mmwave_s5"])
    available = [column for column in ZONE5_MMWAVE_FIELD_CANDIDATES if column in prepared.columns]
    if not available:
        raise ValueError("silver.msr_2 is missing Zone 5 mmWave occupancy columns")
    normalized_occupancy = prepared[available].copy()

    for column in normalized_occupancy.columns:
        series = normalized_occupancy[column]
        if pd.api.types.is_bool_dtype(series):
            normalized_occupancy[column] = series.astype("int64")
            continue

        text = series.astype("string").str.strip().str.lower()
        bool_text = text.map({"true": 1, "false": 0})
        normalized_occupancy[column] = series.where(bool_text.isna(), bool_text)

    occupancy = normalized_occupancy.apply(pd.to_numeric, errors="coerce").max(axis=1)
    prepared = prepared.assign(mmwave_s5=occupancy)
    grouped = prepared.groupby("timestamp", as_index=False).agg(mmwave_s5=("mmwave_s5", "max"))
    return grouped.sort_values("timestamp").reset_index(drop=True)


def _prepare_sen55_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=["timestamp", "sen55_pm1_0", "sen55_pm2_5", "sen55_pm4_0", "sen55_pm10_0", "sen55_temperature", "sen55_humidity", "sen55_voc", "sen55_nox"])
    prepared = _prepare_timestamped_frame(frame, source_label=SILVER_SEN55)
    rename_map = {
        "pm1_0": "sen55_pm1_0",
        "pm2_5": "sen55_pm2_5",
        "pm4_0": "sen55_pm4_0",
        "pm10_0": "sen55_pm10_0",
        "temperature": "sen55_temperature",
        "humidity": "sen55_humidity",
        "voc": "sen55_voc",
        "nox": "sen55_nox",
    }
    prepared = prepared.rename(columns={k: v for k, v in rename_map.items() if k in prepared.columns})
    keep = ["timestamp", *rename_map.values()]
    for column in rename_map.values():
        if column not in prepared.columns:
            prepared[column] = np.nan
    return prepared[keep].drop_duplicates("timestamp", keep="last").reset_index(drop=True)


def _prepare_cv_labels_frame(frame: pd.DataFrame, *, occupied_threshold: float) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=["timestamp", "occupancy_count", TARGET_COLUMN])
    prepared = _prepare_timestamped_frame(frame, source_label=SILVER_CV_LABELS)
    if "occupancy_count" not in prepared.columns and "median_count" in prepared.columns:
        prepared["occupancy_count"] = pd.to_numeric(prepared["median_count"], errors="coerce")
    else:
        prepared["occupancy_count"] = pd.to_numeric(prepared.get("occupancy_count"), errors="coerce")
    target_source = None
    if TARGET_COLUMN in prepared.columns:
        target_source = TARGET_COLUMN
    else:
        for candidate in LEGACY_TARGET_COLUMNS:
            if candidate in prepared.columns:
                target_source = candidate
                break
    if target_source is not None:
        prepared[TARGET_COLUMN] = pd.to_numeric(prepared[target_source], errors="coerce")
    else:
        prepared[TARGET_COLUMN] = np.where(
            prepared["occupancy_count"].notna(),
            (prepared["occupancy_count"] >= float(occupied_threshold)).astype(float),
            np.nan,
        )
    grouped = prepared.groupby("timestamp", as_index=False).agg(
        occupancy_count=("occupancy_count", "median"),
        zone_occupied=(TARGET_COLUMN, "last"),
    )
    return grouped.sort_values("timestamp").reset_index(drop=True)


def _add_missing_indicators(frame: pd.DataFrame) -> pd.DataFrame:
    prepared = frame.copy()
    for column in RAW_FEATURE_COLUMNS:
        if column not in prepared.columns:
            prepared[column] = np.nan
        prepared[column] = pd.to_numeric(prepared[column], errors="coerce")
        prepared[f"{column}_missing"] = prepared[column].isna().astype(int)
    return prepared


def _add_time_features(frame: pd.DataFrame) -> pd.DataFrame:
    prepared = frame.copy()
    timestamps = pd.to_datetime(prepared["timestamp"], errors="coerce")
    hour = (
        timestamps.dt.hour.astype(float)
        + timestamps.dt.minute.astype(float) / 60.0
        + timestamps.dt.second.astype(float) / 3600.0
    )
    hour_angle = 2.0 * math.pi * hour / 24.0
    dow = timestamps.dt.dayofweek.astype(float) + hour / 24.0
    dow_angle = 2.0 * math.pi * dow / 7.0
    prepared["hour_sin"] = np.sin(hour_angle)
    prepared["hour_cos"] = np.cos(hour_angle)
    prepared["dow_sin"] = np.sin(dow_angle)
    prepared["dow_cos"] = np.cos(dow_angle)
    return prepared


def _ordered_training_columns(frame: pd.DataFrame) -> list[str]:
    preferred = [
        "timestamp",
        *RAW_FEATURE_COLUMNS,
        *MISSING_INDICATOR_COLUMNS,
        *TIME_FEATURE_COLUMNS,
        TARGET_COLUMN,
        "occupancy_count",
    ]
    return [column for column in preferred if column in frame.columns] + [
        column for column in frame.columns if column not in preferred
    ]


def build_zone5_training_input_from_silver(
    *,
    rebuild: bool = False,
    occupied_threshold: float = 1.0,
    persist: bool = True,
) -> pd.DataFrame:
    """
    Build the migrated Zone 5 training input exclusively from DuckDB BSG tables.

    Sources
    -------
    - silver.air_1           -> temp_s5, rh_s5, co2_s5, pm25_s5
    - silver.smart_plug_v2   -> power_s5 for the Zone 5 smart plug
    - silver.msr_2           -> mmwave_s5 for the Zone 5 mmWave device
    - silver.zone5_sen55     -> package-local SEN55 aggregates
    - silver.zone5_cv_labels -> Zone 5 occupancy labels
    """
    loader = _loader()
    air1 = _prepare_air1_zone5_frame(loader.load_all(device_type="air-1", layer="silver"))
    if air1.empty:
        result = _empty_training_frame()
        if persist:
            upsert_table_dataframe(result, SILVER_TRAINING_INPUT, key_columns=["timestamp"], rebuild=rebuild)
        return result

    power = _prepare_power_zone5_frame(loader.load_all(device_type="smart-plug-v2", layer="silver"))
    mmwave = _prepare_mmwave_zone5_frame(loader.load_all(device_type="msr-2", layer="silver"))
    sen55 = _prepare_sen55_frame(loader.load_training_table(PIPELINE_NAME, "sen55", layer="silver"))
    labels = _prepare_cv_labels_frame(loader.load_training_table(PIPELINE_NAME, "cv_labels", layer="silver"), occupied_threshold=occupied_threshold)

    joined = air1.copy()
    for frame in (power, mmwave, sen55, labels):
        if not frame.empty:
            joined = joined.merge(frame, on="timestamp", how="left", validate="one_to_one")

    joined = _add_missing_indicators(joined)
    joined = _add_time_features(joined)
    if TARGET_COLUMN not in joined.columns:
        joined[TARGET_COLUMN] = np.nan
    joined = joined[_ordered_training_columns(joined)].sort_values("timestamp").drop_duplicates("timestamp", keep="last")
    if persist:
        upsert_table_dataframe(joined, SILVER_TRAINING_INPUT, key_columns=["timestamp"], rebuild=rebuild)
        return load_migrated_table(SILVER_TRAINING_INPUT)
    return joined.reset_index(drop=True)


def build_zone5_smoke_frame_from_silver(*, lookback: int, safety_rows: int = 10) -> pd.DataFrame:
    """Build the smoke-test input window from silver.zone5_training_input without any CSV dependency."""
    training_input = load_zone5_training_input()
    if training_input.empty:
        raise ValueError(f"{SILVER_TRAINING_INPUT} is empty; build the training input first")
    required = ["timestamp", *RAW_FEATURE_COLUMNS]
    for column in required:
        if column not in training_input.columns:
            training_input[column] = np.nan
    training_input = _prepare_timestamped_frame(training_input[required], source_label=SILVER_TRAINING_INPUT)
    take = int(lookback) + int(safety_rows)
    if len(training_input) < take:
        raise ValueError(
            f"{SILVER_TRAINING_INPUT} has {len(training_input)} valid rows; need at least {take} (lookback={lookback})"
        )
    return training_input.tail(take).reset_index(drop=True)


def training_input_quality_report(frame: pd.DataFrame) -> dict[str, Any]:
    """Summarize the schema, dtypes, and null/non-finite counts for a training frame."""
    report = {
        "rows": int(len(frame)),
        "columns": list(frame.columns),
        "dtypes": {column: str(dtype) for column, dtype in frame.dtypes.items()},
        "null_counts": {column: int(frame[column].isna().sum()) for column in frame.columns},
        "nan_counts": {},
        "non_finite_counts": {},
    }
    for column in frame.columns:
        series = frame[column]
        if pd.api.types.is_numeric_dtype(series):
            numeric = pd.to_numeric(series, errors="coerce")
            report["nan_counts"][column] = int(numeric.isna().sum())
            finite_mask = np.isfinite(numeric.to_numpy(dtype=float, na_value=np.nan))
            report["non_finite_counts"][column] = int((~finite_mask & numeric.notna().to_numpy()).sum())
    return report


def load_migrated_table(table_name: str, *, order_by: str | None = "timestamp") -> pd.DataFrame:
    return query_table(table_name, order_by=order_by)


def write_training_output_to_silver(
    frame: pd.DataFrame,
    *,
    rebuild: bool = False,
    copy_to_gold: bool = False,
    key_columns: list[str] | None = None,
) -> pd.DataFrame:
    """Persist model output into silver, optionally copying the output dump to gold."""
    output_keys = key_columns or [column for column in ("timestamp", "model_run_id") if column in frame.columns]
    if not output_keys:
        output_keys = ["timestamp"] if "timestamp" in frame.columns else []
    upsert_table_dataframe(frame, SILVER_TRAINING_OUTPUT, key_columns=output_keys, rebuild=rebuild)
    if copy_to_gold:
        copy_training_output_to_gold(rebuild=rebuild)
    return load_migrated_table(SILVER_TRAINING_OUTPUT)


def copy_training_output_to_gold(*, rebuild: bool = False) -> pd.DataFrame:
    """Copy only the training output dump from silver to gold."""
    silver_output = load_migrated_table(SILVER_TRAINING_OUTPUT)
    if silver_output.empty:
        return silver_output
    output_keys = [column for column in ("timestamp", "model_run_id") if column in silver_output.columns]
    if not output_keys and "timestamp" in silver_output.columns:
        output_keys = ["timestamp"]
    upsert_table_dataframe(silver_output, GOLD_TRAINING_OUTPUT, key_columns=output_keys, rebuild=rebuild)
    return load_migrated_table(GOLD_TRAINING_OUTPUT)


def load_zone5_training_input() -> pd.DataFrame:
    return _loader().load_training_table(PIPELINE_NAME, "training_input", layer="silver")


def load_zone5_training_output(layer: str = "silver") -> pd.DataFrame:
    return _loader().load_training_table(PIPELINE_NAME, "training_output", layer=layer)