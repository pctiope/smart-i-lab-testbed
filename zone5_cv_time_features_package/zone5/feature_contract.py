from __future__ import annotations

import math
from pathlib import Path
from datetime import datetime, timedelta

import numpy as np
import pandas as pd


SCRIPT_DIR = Path(__file__).resolve().parent
PACKAGE_ROOT = SCRIPT_DIR.parent

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
AIR1_ZONE_5_FEATURE_COLUMNS = ["temp_s5", "rh_s5", "co2_s5", "pm25_s5"]
POWER_FEATURE_COLUMNS = ["power_s5"]
MMWAVE_FEATURE_COLUMNS = ["mmwave_s5"]
SEN55_FEATURE_COLUMNS = [
    "sen55_pm1_0",
    "sen55_pm2_5",
    "sen55_pm4_0",
    "sen55_pm10_0",
    "sen55_temperature",
    "sen55_humidity",
    "sen55_voc",
    "sen55_nox",
]
CORE_FEATURE_COLUMNS = [
    *AIR1_ZONE_5_FEATURE_COLUMNS,
    *POWER_FEATURE_COLUMNS,
    *MMWAVE_FEATURE_COLUMNS,
]
CORE_FEATURE_MIN_PRESENT_FRACTIONS = {
    **{col: 0.80 for col in AIR1_ZONE_5_FEATURE_COLUMNS},
    "power_s5": 0.80,
    "mmwave_s5": 0.95,
}
MISSING_INDICATOR_COLUMNS = [f"{col}_missing" for col in RAW_FEATURE_COLUMNS]
MMWAVE_RECENCY_FRACTION_WINDOWS_MINUTES = [1, 3, 5]
MMWAVE_RECENCY_FRACTION_COLUMNS = [
    f"mmwave_s5_recent_{minutes}m_fraction"
    for minutes in MMWAVE_RECENCY_FRACTION_WINDOWS_MINUTES
]
MMWAVE_MINUTES_SINCE_LAST_OCCUPIED_COLUMN = "mmwave_s5_minutes_since_last_occupied"
MMWAVE_RECENCY_FEATURE_COLUMNS = [
    *MMWAVE_RECENCY_FRACTION_COLUMNS,
    MMWAVE_MINUTES_SINCE_LAST_OCCUPIED_COLUMN,
]
MMWAVE_RECENCY_NO_PRIOR_MINUTES = 15.0
TIME_FEATURE_COLUMNS = ["hour_sin", "hour_cos", "dow_sin", "dow_cos"]
LEGACY_MISSINGNESS_DECOUPLED_FEATURE_COLUMNS = [*RAW_FEATURE_COLUMNS, *TIME_FEATURE_COLUMNS]
FEATURE_COLUMNS = [*RAW_FEATURE_COLUMNS, *MMWAVE_RECENCY_FEATURE_COLUMNS, *TIME_FEATURE_COLUMNS]
TARGET_COLUMN = "zone_occupied"
LEGACY_TARGET_COLUMNS = ["cv_is_occupied", "is_occupied"]
TIMESTAMP_COLUMN = "timestamp"
ZONE_NUM = 5
INPUT_CHANNEL_COUNT = len(FEATURE_COLUMNS)
LEGACY_MISSINGNESS_DECOUPLED_CONTRACT_VERSION = "zone5_missingness_decoupled_v1"
MODEL_CONTRACT_VERSION = "zone5_mmwave_recency_v1"
SUPPORTED_MODEL_FEATURE_COLUMNS_BY_CONTRACT = {
    LEGACY_MISSINGNESS_DECOUPLED_CONTRACT_VERSION: LEGACY_MISSINGNESS_DECOUPLED_FEATURE_COLUMNS,
    MODEL_CONTRACT_VERSION: FEATURE_COLUMNS,
}
SAMPLE_INTERVAL_SECONDS = 10
SAMPLE_INTERVAL = timedelta(seconds=SAMPLE_INTERVAL_SECONDS)
SAMPLE_INTERVAL_PANDAS_FREQ = f"{SAMPLE_INTERVAL_SECONDS}s"
LOOKBACK_MINUTES_CHOICES = [15, 60, 180]


def floor_datetime_to_sample(value: datetime) -> datetime:
    """Floor a datetime to the configured Zone 5 sample boundary."""
    second = (value.second // SAMPLE_INTERVAL_SECONDS) * SAMPLE_INTERVAL_SECONDS
    return value.replace(second=second, microsecond=0)


def floor_timestamp_series_to_sample(values: pd.Series) -> pd.Series:
    timestamps = pd.to_datetime(values, errors="coerce")
    return timestamps.dt.floor(SAMPLE_INTERVAL_PANDAS_FREQ)


def lookback_rows_for_minutes(minutes: int | float) -> int:
    return int(math.ceil(float(minutes) * 60.0 / SAMPLE_INTERVAL_SECONDS))


LOOKBACK_ROWS_BY_MINUTES = {
    int(minutes): lookback_rows_for_minutes(minutes)
    for minutes in LOOKBACK_MINUTES_CHOICES
}


def source_feature_columns(feature_columns: list[str] | tuple[str, ...] | None = None) -> list[str]:
    """Return columns that must be present in raw CSV/Parquet inputs."""
    columns = list(feature_columns or FEATURE_COLUMNS)
    return [
        col
        for col in columns
        if col not in TIME_FEATURE_COLUMNS
        and col not in MMWAVE_RECENCY_FEATURE_COLUMNS
        and col not in MISSING_INDICATOR_COLUMNS
    ]


def add_mmwave_recency_features(frame: pd.DataFrame) -> pd.DataFrame:
    """Add past-only mmWave recency features from the raw mmwave_s5 stream."""
    if TIMESTAMP_COLUMN not in frame.columns:
        raise ValueError(f"Frame is missing required timestamp column: {TIMESTAMP_COLUMN}")
    enriched = frame.copy()
    row_order = pd.Series(np.arange(len(enriched)), index=enriched.index)
    timestamps = pd.to_datetime(enriched[TIMESTAMP_COLUMN], errors="coerce")
    mmwave = pd.to_numeric(
        enriched["mmwave_s5"] if "mmwave_s5" in enriched.columns else pd.Series(np.nan, index=enriched.index),
        errors="coerce",
    )
    occupied = (mmwave > 0.0).astype(float)
    occupied[mmwave.isna()] = np.nan

    defaults = {
        **{col: 0.0 for col in MMWAVE_RECENCY_FRACTION_COLUMNS},
        MMWAVE_MINUTES_SINCE_LAST_OCCUPIED_COLUMN: float(MMWAVE_RECENCY_NO_PRIOR_MINUTES),
    }
    for col, value in defaults.items():
        enriched[col] = value

    work = pd.DataFrame(
        {
            "_row_order": row_order.to_numpy(),
            "_timestamp": timestamps.to_numpy(),
            "_occupied": occupied.to_numpy(dtype=float),
        },
        index=enriched.index,
    )
    work = work.dropna(subset=["_timestamp"]).sort_values(["_timestamp", "_row_order"])
    if work.empty:
        return enriched

    occupied_sorted = work["_occupied"]
    derived = pd.DataFrame(index=work.index)
    for minutes, column in zip(MMWAVE_RECENCY_FRACTION_WINDOWS_MINUTES, MMWAVE_RECENCY_FRACTION_COLUMNS):
        rows = lookback_rows_for_minutes(minutes)
        derived[column] = (
            occupied_sorted.rolling(window=rows, min_periods=1)
            .mean()
            .fillna(0.0)
            .clip(0.0, 1.0)
        )

    occupied_timestamps = work["_timestamp"].where(occupied_sorted == 1.0)
    last_occupied = occupied_timestamps.ffill()
    minutes_since = (work["_timestamp"] - last_occupied).dt.total_seconds() / 60.0
    derived[MMWAVE_MINUTES_SINCE_LAST_OCCUPIED_COLUMN] = (
        minutes_since.fillna(float(MMWAVE_RECENCY_NO_PRIOR_MINUTES))
        .clip(lower=0.0, upper=float(MMWAVE_RECENCY_NO_PRIOR_MINUTES))
    )

    for col in MMWAVE_RECENCY_FEATURE_COLUMNS:
        enriched.loc[derived.index, col] = derived[col].astype(float)
        enriched[col] = pd.to_numeric(enriched[col], errors="coerce").fillna(float(defaults[col]))
    return enriched


def add_time_features(frame: pd.DataFrame) -> pd.DataFrame:
    """Add deterministic daily and weekly cycle features from TIMESTAMP_COLUMN."""
    if TIMESTAMP_COLUMN not in frame.columns:
        raise ValueError(f"Frame is missing required timestamp column: {TIMESTAMP_COLUMN}")
    enriched = frame.copy()
    timestamps = pd.to_datetime(enriched[TIMESTAMP_COLUMN], errors="coerce")
    hour = (
        timestamps.dt.hour.astype(float)
        + timestamps.dt.minute.astype(float) / 60.0
        + timestamps.dt.second.astype(float) / 3600.0
    )
    hour_angle = 2.0 * math.pi * hour / 24.0
    dow = timestamps.dt.dayofweek.astype(float) + hour / 24.0
    dow_angle = 2.0 * math.pi * dow / 7.0
    enriched["hour_sin"] = np.sin(hour_angle)
    enriched["hour_cos"] = np.cos(hour_angle)
    enriched["dow_sin"] = np.sin(dow_angle)
    enriched["dow_cos"] = np.cos(dow_angle)
    return enriched
