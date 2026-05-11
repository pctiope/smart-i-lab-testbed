from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from air1_all_zones import csv_size_guard
from air1_all_zones.feature_contract import (
    ALL_ZONE_IDS,
    LEGACY_TARGET_COLUMNS,
    SAMPLE_INTERVAL_PANDAS_FREQ,
    TARGET_COLUMN,
    ZONE_ID_COLUMN,
)


@dataclass
class ZoneGroundTruthSnapshot:
    count: float | int | None = None
    occupied: bool | None = None
    timestamp: str | None = None
    age_minutes: float | int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": _json_safe_float(self.count),
            "occupied": self.occupied,
            "timestamp": self.timestamp,
            "age_minutes": _json_safe_float(self.age_minutes),
        }


@dataclass
class GroundTruthSnapshot:
    count: float | None = None
    occupied: bool | None = None
    timestamp: str | None = None
    age_minutes: float | None = None
    by_zone: dict[int, ZoneGroundTruthSnapshot] = field(default_factory=dict)

    def to_event_fields(self) -> dict[str, Any]:
        return {
            "ground_truth_count": _json_safe_float(self.count),
            "ground_truth_occupied": self.occupied,
            "ground_truth_timestamp": self.timestamp,
            "ground_truth_age_minutes": _json_safe_float(self.age_minutes),
            "ground_truth_by_zone": _zone_snapshot_dict(self.by_zone),
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
    if value is None:
        return None
    ts = pd.Timestamp(value)
    if pd.isna(ts):
        return None
    if ts.tzinfo is not None:
        ts = ts.tz_convert(None)
    return ts.floor(SAMPLE_INTERVAL_PANDAS_FREQ)


def _age_minutes(reference_time: pd.Timestamp | None, label_time: pd.Timestamp) -> float | None:
    if reference_time is None:
        return None
    return round((reference_time - label_time).total_seconds() / 60.0, 3)


def _target_to_bool(value: Any) -> bool | None:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return bool(int(round(f)))


def _empty_zone_snapshots() -> dict[int, ZoneGroundTruthSnapshot]:
    return {int(zone_id): ZoneGroundTruthSnapshot() for zone_id in ALL_ZONE_IDS}


def _zone_snapshot_dict(snapshots: dict[int, ZoneGroundTruthSnapshot]) -> dict[str, dict[str, Any]]:
    merged = _empty_zone_snapshots()
    for zone_id, snapshot in snapshots.items():
        if int(zone_id) in merged:
            merged[int(zone_id)] = snapshot
    return {
        str(zone_id): merged[int(zone_id)].to_dict()
        for zone_id in ALL_ZONE_IDS
    }


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
        for legacy_col in LEGACY_TARGET_COLUMNS:
            if TARGET_COLUMN not in frame.columns and legacy_col in frame.columns:
                frame = frame.rename(columns={legacy_col: TARGET_COLUMN})
        required = ["timestamp", "occupancy_count", TARGET_COLUMN]
        missing = [col for col in required if col not in frame.columns]
        if missing:
            raise ValueError(f"missing required columns: {missing}")
        keep = [*required]
        if ZONE_ID_COLUMN in frame.columns:
            keep.append(ZONE_ID_COLUMN)
        cleaned = frame[keep].copy()
        cleaned["timestamp"] = pd.to_datetime(cleaned["timestamp"], errors="coerce")
        cleaned["occupancy_count"] = pd.to_numeric(cleaned["occupancy_count"], errors="coerce")
        cleaned[TARGET_COLUMN] = pd.to_numeric(cleaned[TARGET_COLUMN], errors="coerce")
        cleaned = cleaned.dropna(subset=["timestamp"])
        cleaned["timestamp"] = cleaned["timestamp"].dt.floor(SAMPLE_INTERVAL_PANDAS_FREQ)
        if ZONE_ID_COLUMN in cleaned.columns:
            cleaned[ZONE_ID_COLUMN] = pd.to_numeric(cleaned[ZONE_ID_COLUMN], errors="coerce").astype("Int64")
            cleaned = cleaned.dropna(subset=[ZONE_ID_COLUMN])
            cleaned = cleaned.loc[cleaned[ZONE_ID_COLUMN].astype(int).isin(ALL_ZONE_IDS)].copy()
            cleaned = cleaned.sort_values(["timestamp", ZONE_ID_COLUMN]).drop_duplicates(
                ["timestamp", ZONE_ID_COLUMN],
                keep="last",
            )
        else:
            cleaned = cleaned.sort_values("timestamp").drop_duplicates("timestamp", keep="last")
        return cleaned.reset_index(drop=True)

    @staticmethod
    def _eligible_frame(frame: pd.DataFrame, reference_time: Any | None) -> tuple[pd.DataFrame, pd.Timestamp | None]:
        ref_ts = _to_naive_minute(reference_time) if reference_time is not None else _to_naive_minute(pd.Timestamp.now())
        if ref_ts is None:
            return frame, None
        return frame.loc[frame["timestamp"] <= ref_ts], ref_ts

    @staticmethod
    def _aggregate_snapshot_from_rows(rows: pd.DataFrame, ref_ts: pd.Timestamp | None) -> GroundTruthSnapshot:
        if rows.empty:
            return GroundTruthSnapshot(by_zone=_empty_zone_snapshots())
        latest_ts = pd.Timestamp(rows["timestamp"].max())
        latest_rows = rows.loc[rows["timestamp"] == latest_ts]
        counts = pd.to_numeric(latest_rows["occupancy_count"], errors="coerce").dropna()
        targets = pd.to_numeric(latest_rows[TARGET_COLUMN], errors="coerce").dropna()
        count = float(counts.sum()) if len(counts) else None
        occupied = bool(int(targets.max())) if len(targets) else None
        return GroundTruthSnapshot(
            count=count,
            occupied=occupied,
            timestamp=latest_ts.isoformat(),
            age_minutes=_age_minutes(ref_ts, latest_ts),
        )

    @staticmethod
    def _single_snapshot_from_row(row: pd.Series, ref_ts: pd.Timestamp | None) -> GroundTruthSnapshot:
        gt_ts = pd.Timestamp(row["timestamp"])
        return GroundTruthSnapshot(
            count=_json_safe_float(row["occupancy_count"]),
            occupied=_target_to_bool(row[TARGET_COLUMN]),
            timestamp=gt_ts.isoformat(),
            age_minutes=_age_minutes(ref_ts, gt_ts),
        )

    @staticmethod
    def _zone_snapshots_from_rows(rows: pd.DataFrame, ref_ts: pd.Timestamp | None) -> dict[int, ZoneGroundTruthSnapshot]:
        snapshots = _empty_zone_snapshots()
        if ZONE_ID_COLUMN not in rows.columns or rows.empty:
            return snapshots
        for zone_id in ALL_ZONE_IDS:
            zone_rows = rows.loc[rows[ZONE_ID_COLUMN].astype(int) == int(zone_id)]
            if zone_rows.empty:
                continue
            row = zone_rows.iloc[-1]
            gt_ts = pd.Timestamp(row["timestamp"])
            snapshots[int(zone_id)] = ZoneGroundTruthSnapshot(
                count=_json_safe_float(row["occupancy_count"]),
                occupied=_target_to_bool(row[TARGET_COLUMN]),
                timestamp=gt_ts.isoformat(),
                age_minutes=_age_minutes(ref_ts, gt_ts),
            )
        return snapshots

    def latest(self, reference_time: Any | None = None) -> GroundTruthSnapshot:
        frame = self._load_if_needed()
        if frame is None or frame.empty:
            return GroundTruthSnapshot(by_zone=_empty_zone_snapshots())
        eligible, ref_ts = self._eligible_frame(frame, reference_time)
        if eligible.empty:
            return GroundTruthSnapshot(by_zone=_empty_zone_snapshots())
        if ZONE_ID_COLUMN in eligible.columns:
            aggregate = self._aggregate_snapshot_from_rows(eligible, ref_ts)
            aggregate.by_zone = self._zone_snapshots_from_rows(eligible, ref_ts)
            return aggregate
        aggregate = self._single_snapshot_from_row(eligible.iloc[-1], ref_ts)
        aggregate.by_zone = _empty_zone_snapshots()
        return aggregate

    def latest_by_zone(self, reference_time: Any | None = None) -> dict[int, ZoneGroundTruthSnapshot]:
        frame = self._load_if_needed()
        if frame is None or frame.empty:
            return _empty_zone_snapshots()
        eligible, ref_ts = self._eligible_frame(frame, reference_time)
        return self._zone_snapshots_from_rows(eligible, ref_ts)

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


