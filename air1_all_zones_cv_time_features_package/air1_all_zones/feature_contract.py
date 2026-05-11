from __future__ import annotations

import math
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd


SCRIPT_DIR = Path(__file__).resolve().parent
PACKAGE_ROOT = SCRIPT_DIR.parent

TIMESTAMP_COLUMN = "timestamp"
ZONE_ID_COLUMN = "zone_id"
TARGET_COLUMN = "occupied"
LEGACY_TARGET_COLUMNS = ["zone_occupied", "cv_is_occupied", "is_occupied"]

SENSOR_ORDER = [
    "88e4c8",
    "88e590",
    "89e8d8",
    "889720",
    "87f510",
    "2da640",
    "89ea14",
    "889b88",
    "889938",
    "88e85c",
    "89e548",
    "88970c",
    "2deb24",
    "89e5f0",
    "cc8f24",
]
ALL_ZONE_IDS = tuple(range(1, len(SENSOR_ORDER) + 1))
DEVICE_TO_ZONE_ID = {device_id: idx + 1 for idx, device_id in enumerate(SENSOR_ORDER)}

AIR1_FEATURE_COLUMNS = ["temp", "rh", "co2", "pm25"]
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
RAW_FEATURE_COLUMNS = [*AIR1_FEATURE_COLUMNS, *SEN55_FEATURE_COLUMNS]
MISSING_INDICATOR_COLUMNS = [f"{col}_missing" for col in RAW_FEATURE_COLUMNS]
TIME_FEATURE_COLUMNS = ["hour_sin", "hour_cos", "dow_sin", "dow_cos"]
FEATURE_COLUMNS = [*RAW_FEATURE_COLUMNS, *MISSING_INDICATOR_COLUMNS, *TIME_FEATURE_COLUMNS]

INPUT_CHANNEL_COUNT = len(FEATURE_COLUMNS)
SAMPLE_INTERVAL_SECONDS = 10
SAMPLE_INTERVAL = timedelta(seconds=SAMPLE_INTERVAL_SECONDS)
SAMPLE_INTERVAL_PANDAS_FREQ = f"{SAMPLE_INTERVAL_SECONDS}s"
LOOKBACK_MINUTES_CHOICES = [15, 60, 180]


def floor_datetime_to_sample(value: datetime) -> datetime:
    """Floor a datetime to the configured AIR-1 all-zones sample boundary."""
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
        if col not in TIME_FEATURE_COLUMNS and col not in MISSING_INDICATOR_COLUMNS
    ]


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
