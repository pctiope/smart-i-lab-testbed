from __future__ import annotations

import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from zone5 import csv_size_guard
from zone5.feature_contract import SAMPLE_INTERVAL_PANDAS_FREQ
from zone5.local_time import to_zone5_local_naive_timestamp, zone5_local_now


@dataclass
class GroundTruthSnapshot:
    count: float | None = None
    occupied: bool | None = None
    timestamp: str | None = None
    age_minutes: float | None = None

    def to_event_fields(self) -> dict[str, Any]:
        return {
            "ground_truth_count": _json_safe_float(self.count),
            "ground_truth_occupied": self.occupied,
            "ground_truth_timestamp": self.timestamp,
            "ground_truth_age_minutes": _json_safe_float(self.age_minutes),
        }


def _json_safe_float(value: Any) -> float | int | None:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return int(f) if f.is_integer() else f


def _to_naive_minute(value: Any) -> pd.Timestamp | None:
    ts = to_zone5_local_naive_timestamp(value)
    if pd.isna(ts):
        return None
    return ts.floor(SAMPLE_INTERVAL_PANDAS_FREQ)


class CvGroundTruthTailer:
    def __init__(self, table_path: Path | str | None = None, *, parquet_path: Path | str | None = None) -> None:
        if table_path is None:
            if parquet_path is None:
                raise TypeError("CvGroundTruthTailer requires a table_path")
            table_path = parquet_path
        self.table_path = Path(table_path)
        self._cached_mtime: float = 0.0
        self._cached_frame: pd.DataFrame | None = None
        self._last_error: str | None = None
        self._last_read_at: float = 0.0

    @property
    def parquet_path(self) -> Path:
        return self.table_path

    @parquet_path.setter
    def parquet_path(self, value: Path | str) -> None:
        self.table_path = Path(value)

    @property
    def label(self) -> str:
        return f"cv_ground_truth[{self.table_path}]"

    def _load_if_needed(self) -> pd.DataFrame | None:
        if not self.table_path.is_file():
            self._last_error = f"CV ground-truth table not found: {self.table_path}"
            self._cached_frame = None
            self._cached_mtime = 0.0
            return None
        try:
            mtime = self.table_path.stat().st_mtime
        except OSError as exc:
            self._last_error = f"cannot stat CV ground-truth table: {exc}"
            return self._cached_frame
        if self._cached_frame is not None and mtime <= self._cached_mtime:
            return self._cached_frame
        try:
            if self.table_path.suffix.lower() == ".csv":
                frame = csv_size_guard.read_csv_parts(self.table_path)
            else:
                frame = pd.read_parquet(self.table_path)
            cleaned = self._clean_frame(frame)
        except Exception as exc:
            self._last_error = f"cannot read CV ground-truth table: {type(exc).__name__}: {exc}"
            return self._cached_frame
        self._cached_frame = cleaned
        self._cached_mtime = mtime
        self._last_error = None
        self._last_read_at = time.time()
        return cleaned

    @staticmethod
    def _clean_frame(frame: pd.DataFrame) -> pd.DataFrame:
        if "timestamp" not in frame.columns and "minute_start" in frame.columns:
            frame = frame.rename(columns={"minute_start": "timestamp"})
        if "cv_is_occupied" not in frame.columns and "is_occupied" in frame.columns:
            frame = frame.rename(columns={"is_occupied": "cv_is_occupied"})
        required = ["timestamp", "occupancy_count", "cv_is_occupied"]
        missing = [col for col in required if col not in frame.columns]
        if missing:
            raise ValueError(f"missing required columns: {missing}")
        cleaned = frame[required].copy()
        cleaned["timestamp"] = cleaned["timestamp"].map(to_zone5_local_naive_timestamp)
        cleaned["occupancy_count"] = pd.to_numeric(cleaned["occupancy_count"], errors="coerce")
        cleaned["cv_is_occupied"] = pd.to_numeric(cleaned["cv_is_occupied"], errors="coerce")
        cleaned = cleaned.dropna(subset=required)
        cleaned["timestamp"] = pd.to_datetime(cleaned["timestamp"], errors="coerce").dt.floor(
            SAMPLE_INTERVAL_PANDAS_FREQ
        )
        cleaned["cv_is_occupied"] = cleaned["cv_is_occupied"].round().astype(int)
        cleaned = cleaned.sort_values("timestamp").drop_duplicates("timestamp", keep="last")
        return cleaned.reset_index(drop=True)

    def latest(self, reference_time: Any | None = None) -> GroundTruthSnapshot:
        frame = self._load_if_needed()
        if frame is None or frame.empty:
            return GroundTruthSnapshot()
        ref_ts = _to_naive_minute(reference_time) if reference_time is not None else zone5_local_now(
            SAMPLE_INTERVAL_PANDAS_FREQ
        )
        if ref_ts is None:
            eligible = frame
        else:
            eligible = frame.loc[frame["timestamp"] <= ref_ts]
        if eligible.empty:
            return GroundTruthSnapshot()
        row = eligible.iloc[-1]
        gt_ts = pd.Timestamp(row["timestamp"])
        age_minutes = None
        if ref_ts is not None:
            age_minutes = round((ref_ts - gt_ts).total_seconds() / 60.0, 3)
        return GroundTruthSnapshot(
            count=float(row["occupancy_count"]),
            occupied=bool(int(row["cv_is_occupied"])),
            timestamp=gt_ts.isoformat(),
            age_minutes=age_minutes,
        )

    def latest_event_fields(self, reference_time: Any | None = None) -> dict[str, Any]:
        return self.latest(reference_time=reference_time).to_event_fields()

    def status(self) -> dict[str, Any]:
        latest = self.latest(reference_time=None)
        return {
            "path": str(self.table_path),
            "exists": self.table_path.is_file(),
            "cached_rows": 0 if self._cached_frame is None else int(len(self._cached_frame)),
            "last_read_at": self._last_read_at,
            "last_error": self._last_error,
            "latest": latest.to_event_fields(),
        }
