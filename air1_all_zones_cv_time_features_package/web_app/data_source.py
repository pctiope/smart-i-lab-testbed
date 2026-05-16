from __future__ import annotations

import logging
import math
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

import pandas as pd

WEB_APP_DIR = Path(__file__).resolve().parent
WORKSPACE_DIR = WEB_APP_DIR.parent
if str(WORKSPACE_DIR) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_DIR))

from air1_all_zones import feature_builder  # noqa: E402
from air1_all_zones import model as training  # noqa: E402

logger = logging.getLogger("air1_all_zones_web_app.data_source")


def _workspace_path(raw_path: str) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        path = (WORKSPACE_DIR / path).resolve()
    return path


def _default_sen55_csv() -> Path:
    return feature_builder.default_sen55_table(WORKSPACE_DIR, WORKSPACE_DIR / "data")


class DataSource(Protocol):
    def get_latest_window(self, lookback: int) -> tuple[pd.DataFrame, pd.Timestamp]: ...

    @property
    def label(self) -> str: ...


def _tail_by_timestamp(frame: pd.DataFrame, timestamp_count: int) -> pd.DataFrame:
    timestamps = pd.Series(pd.to_datetime(frame[training.TIMESTAMP_COLUMN]).dropna().unique()).sort_values()
    if timestamps.empty:
        return frame.iloc[0:0].copy()
    keep = set(pd.Timestamp(value) for value in timestamps.iloc[-int(timestamp_count):])
    mask = pd.to_datetime(frame[training.TIMESTAMP_COLUMN]).isin(keep)
    return frame.loc[mask].sort_values([training.TIMESTAMP_COLUMN, training.ZONE_ID_COLUMN]).reset_index(drop=True)


def _read_clean_frame(parquet_path: Path) -> pd.DataFrame:
    if not parquet_path.is_file():
        raise FileNotFoundError(f"Parquet not found: {parquet_path}")
    frame = pd.read_parquet(parquet_path).copy()
    required = [training.TIMESTAMP_COLUMN, training.ZONE_ID_COLUMN]
    missing_required = [col for col in required if col not in frame.columns]
    if missing_required:
        raise ValueError(f"Parquet is missing required all-zones columns {missing_required}: {parquet_path}")
    frame[training.TIMESTAMP_COLUMN] = pd.to_datetime(frame[training.TIMESTAMP_COLUMN], errors="coerce").dt.floor(
        training.SAMPLE_INTERVAL_PANDAS_FREQ
    )
    frame[training.ZONE_ID_COLUMN] = pd.to_numeric(frame[training.ZONE_ID_COLUMN], errors="coerce").astype("Int64")
    for col in training.RAW_FEATURE_COLUMNS:
        if col not in frame.columns:
            frame[col] = pd.NA
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame = frame.dropna(subset=[training.TIMESTAMP_COLUMN, training.ZONE_ID_COLUMN])
    frame = frame.sort_values([training.TIMESTAMP_COLUMN, training.ZONE_ID_COLUMN])
    frame = frame.drop_duplicates(subset=[training.TIMESTAMP_COLUMN, training.ZONE_ID_COLUMN], keep="last")
    frame = frame.reset_index(drop=True)
    if frame.empty:
        raise ValueError(f"Parquet has no valid all-zones rows after cleaning: {parquet_path}")
    return frame


class LiveParquetTailDataSource:
    """Tails the newest all-zones parquet under a directory."""

    def __init__(self, parquet_dir: Path | str, slack_rows: int = 5) -> None:
        self.parquet_dir = Path(parquet_dir)
        self.slack_rows = int(slack_rows)
        self._cached_path: Path | None = None
        self._cached_mtime: float = 0.0
        self._cached_frame: pd.DataFrame | None = None

    @property
    def label(self) -> str:
        return f"live[parquet_tail:{self.parquet_dir}]"

    def _newest_parquet(self) -> Path:
        candidates = sorted(self.parquet_dir.glob("*.parquet"), key=lambda p: p.stat().st_mtime)
        if not candidates:
            raise FileNotFoundError(f"No *.parquet under {self.parquet_dir}")
        return candidates[-1]

    def get_latest_window(self, lookback: int) -> tuple[pd.DataFrame, pd.Timestamp]:
        path = self._newest_parquet()
        mtime = path.stat().st_mtime
        if path != self._cached_path or mtime > self._cached_mtime or self._cached_frame is None:
            self._cached_frame = _read_clean_frame(path)
            self._cached_path = path
            self._cached_mtime = mtime
        frame = self._cached_frame
        take = int(lookback) + self.slack_rows
        unique_timestamps = pd.to_datetime(frame[training.TIMESTAMP_COLUMN]).nunique()
        if unique_timestamps < take:
            raise ValueError(
                f"{path.name} has {unique_timestamps} timestamp groups; need at least {take} "
                f"(lookback={lookback}+slack={self.slack_rows})"
            )
        return _tail_by_timestamp(frame, take), pd.Timestamp.now()


class ReplayParquetDataSource:
    """Replays a fixed all-zones parquet by advancing one timestamp group per tick."""

    def __init__(
        self,
        parquet_path: Path | str,
        start_timestamp: pd.Timestamp | None = None,
        slack_rows: int = 30,
    ) -> None:
        self._frame = _read_clean_frame(Path(parquet_path))
        self.parquet_path = Path(parquet_path)
        self.slack_rows = int(slack_rows)
        self._timestamps = pd.Series(
            pd.to_datetime(self._frame[training.TIMESTAMP_COLUMN]).dropna().unique()
        ).sort_values().reset_index(drop=True)
        if start_timestamp is None:
            self._cursor = -1
        else:
            mask = self._timestamps >= pd.Timestamp(start_timestamp).floor(training.SAMPLE_INTERVAL_PANDAS_FREQ)
            if not mask.any():
                raise ValueError(f"start_timestamp {start_timestamp} is after all rows in {parquet_path}")
            self._cursor = int(mask[mask].index[0]) - 1

    @property
    def label(self) -> str:
        return f"replay[{self.parquet_path.name}]"

    @property
    def total_rows(self) -> int:
        return int(len(self._frame))

    @property
    def cursor(self) -> int:
        return int(self._cursor)

    def get_latest_window(self, lookback: int) -> tuple[pd.DataFrame, pd.Timestamp]:
        self._cursor = min(max(self._cursor + 1, int(lookback) - 1), len(self._timestamps) - 1)
        end = self._cursor + 1
        take = int(lookback) + self.slack_rows
        if end < int(lookback):
            raise ValueError(
                f"Replay has {end} timestamp group(s) so far; need {lookback} before first tick. "
                "Choose an earlier start_timestamp or wait for cursor to advance."
            )
        selected = set(pd.Timestamp(value) for value in self._timestamps.iloc[max(0, end - take):end])
        window = self._frame.loc[pd.to_datetime(self._frame[training.TIMESTAMP_COLUMN]).isin(selected)].copy()
        window = window.sort_values([training.TIMESTAMP_COLUMN, training.ZONE_ID_COLUMN]).reset_index(drop=True)
        latest_ts = pd.Timestamp(self._timestamps.iloc[self._cursor])
        return window, latest_ts


@dataclass
class _RollingCache:
    frame: pd.DataFrame
    last_warm_fetch_ts: float = 0.0

    @property
    def last_timestamp(self) -> pd.Timestamp:
        return pd.Timestamp(self.frame[training.TIMESTAMP_COLUMN].max())

    def merge(self, new_frame: pd.DataFrame, max_keep_timestamps: int) -> None:
        if new_frame is None or new_frame.empty:
            return
        merged = pd.concat([self.frame, new_frame], ignore_index=True, sort=False)
        merged = merged.sort_values([training.TIMESTAMP_COLUMN, training.ZONE_ID_COLUMN])
        merged = merged.drop_duplicates(subset=[training.TIMESTAMP_COLUMN, training.ZONE_ID_COLUMN], keep="last")
        merged = _tail_by_timestamp(merged, max_keep_timestamps)
        self.frame = merged.reset_index(drop=True)


class LiveAir1DataSource:
    """Fetch trailing AIR-1 + shared SEN55 features and return long-form zone rows."""

    def __init__(
        self,
        *,
        lookback_safety_minutes: int = 30,
        slack_rows: int = 15,
        api_url: str | None = None,
        api_key: str | None = None,
        api_timeout: float = 30.0,
        api_retries: int = 1,
        max_workers: int = 4,
        slow_fetch_warn_sec: float = 10.0,
        cache_enabled: bool = True,
        cache_max_age_minutes: float = 30.0,
        overlap_minutes: float = 2.0,
        sen55_csv: Path | str | None = None,
    ) -> None:
        from air1_all_zones import export_parquet as exporter_module  # noqa: E402

        self._exporter_module = exporter_module
        self._csv_training = exporter_module.csv_training

        resolved_url = api_url or os.environ.get("AIR1_API_URL") or self._csv_training.API_URL
        resolved_key = api_key or os.environ.get("AIR1_API_KEY") or self._csv_training.API_KEY
        if not resolved_url or not resolved_key:
            raise RuntimeError(
                "AIR-1 API credentials missing: set AIR1_API_URL and AIR1_API_KEY before starting live mode."
            )

        self._client = self._csv_training.Air1Device(
            resolved_url,
            resolved_key,
            api_timeout=float(api_timeout),
            api_retries=int(api_retries),
            max_workers=int(max_workers),
            verbose_progress=False,
            timing_summary=False,
        )
        self.lookback_safety_minutes = int(lookback_safety_minutes)
        self.slack_rows = int(slack_rows)
        self.slow_fetch_warn_sec = float(slow_fetch_warn_sec)
        self.cache_enabled = bool(cache_enabled)
        self.cache_max_age_minutes = float(cache_max_age_minutes)
        self.overlap_minutes = float(overlap_minutes)
        self.sen55_csv = Path(sen55_csv) if sen55_csv is not None else _default_sen55_csv()
        self._source_devices: dict[str, list[str]] | None = None
        self._cache: _RollingCache | None = None
        # Defer the AIR-1 API roster lookup so a temporary API outage cannot
        # prevent the dashboard and YOLO video routes from starting.

    @property
    def label(self) -> str:
        cache_suffix = "" if self.cache_enabled else " no-cache"
        sen55_suffix = " sen55" if self.sen55_csv.is_file() else " sen55-missing"
        return f"live[air1_api all_zones{cache_suffix}{sen55_suffix}]"

    def _refresh_device_roster(self) -> None:
        all_devices = self._client.get_all_devices()
        if not all_devices:
            raise RuntimeError("AIR-1 API returned no devices. Check AIR1_API_URL and network reachability.")
        source_devices = self._exporter_module.all_zones_source_devices(all_devices)
        if not source_devices.get("air1"):
            raise RuntimeError("None of the expected 15 AIR-1 sensors is present in the device list.")
        available = set(source_devices["air1"])
        missing = [device_id for device_id in self._csv_training.SENSOR_ORDER if device_id not in available]
        self._source_devices = source_devices
        logger.info(
            "AIR-1 all-zones roster cached: available=%d missing=%d missing_ids=%s",
            len(source_devices["air1"]),
            len(missing),
            missing,
        )

    def _fetch_window(self, time_start: datetime, time_end: datetime) -> pd.DataFrame:
        minute_index = self._csv_training.build_local_minute_index(time_start, time_end)
        if not minute_index:
            return pd.DataFrame(columns=[training.TIMESTAMP_COLUMN, training.ZONE_ID_COLUMN, *training.RAW_FEATURE_COLUMNS])

        fetch_started = time.perf_counter()
        assert self._source_devices is not None
        histories_by_source, failures = self._client._fetch_adaptive_historical_sources(
            self._exporter_module.build_all_zones_source_specs(self._source_devices),
            time_start,
            time_end,
        )
        fetch_elapsed = time.perf_counter() - fetch_started
        if fetch_elapsed > self.slow_fetch_warn_sec:
            logger.warning(
                "AIR-1 fetch slow: %.2fs for %s..%s (failures=%d)",
                fetch_elapsed,
                time_start,
                time_end,
                len(failures),
            )
        if failures:
            logger.warning("AIR-1 fetch reported %d failure(s); proceeding with returned rows", len(failures))

        return feature_builder.build_all_zones_feature_frame_from_histories(
            minute_index=minute_index,
            histories_by_source=histories_by_source,
            time_start=time_start,
            time_end=time_end,
            sen55_csv=self.sen55_csv,
            logger=logger,
        )

    def get_latest_window(self, lookback: int) -> tuple[pd.DataFrame, pd.Timestamp]:
        if self._source_devices is None:
            self._refresh_device_roster()

        time_end_utc = self._csv_training.sample_floor(datetime.now(timezone.utc).replace(tzinfo=None))
        time_end_local = time_end_utc + self._csv_training.LOCAL_OFFSET
        lookback_minutes = int(lookback) * self._csv_training.SAMPLE_INTERVAL_SECONDS / 60.0
        cold_minutes = lookback_minutes + self.lookback_safety_minutes
        safety_timestamps = math.ceil(self.lookback_safety_minutes * 60.0 / self._csv_training.SAMPLE_INTERVAL_SECONDS)
        max_keep_timestamps = int(lookback) + safety_timestamps + 20

        cold_fetch = (
            not self.cache_enabled
            or self._cache is None
            or self._cache.frame.empty
            or (time_end_local - self._cache.last_timestamp.to_pydatetime()).total_seconds() / 60.0
            > self.cache_max_age_minutes
        )

        if cold_fetch:
            time_start_utc = time_end_utc - timedelta(minutes=cold_minutes)
            fetched = self._fetch_window(time_start_utc, time_end_utc)
            frame = _tail_by_timestamp(fetched, max_keep_timestamps)
            if self.cache_enabled:
                self._cache = _RollingCache(frame=frame, last_warm_fetch_ts=time.time())
        else:
            assert self._cache is not None
            delta_start_local = self._cache.last_timestamp.to_pydatetime() - timedelta(minutes=self.overlap_minutes)
            if delta_start_local < time_end_local:
                delta_start_utc = delta_start_local - self._csv_training.LOCAL_OFFSET
                new_frame = self._fetch_window(delta_start_utc, time_end_utc)
                self._cache.merge(new_frame, max_keep_timestamps=max_keep_timestamps)
                self._cache.last_warm_fetch_ts = time.time()
            frame = self._cache.frame

        timestamp_count = pd.to_datetime(frame[training.TIMESTAMP_COLUMN]).nunique() if not frame.empty else 0
        if timestamp_count < int(lookback):
            raise ValueError(
                f"AIR-1 fetch returned only {timestamp_count} timestamp group(s) for the last "
                f"{cold_minutes:.1f} minutes; need {lookback}."
            )

        return _tail_by_timestamp(frame, int(lookback) + self.slack_rows), pd.Timestamp.now()


def build_data_source_from_env(env: dict[str, str]) -> DataSource:
    mode = env.get("AIR1_ALL_ZONES_DATA_SOURCE", "live").strip().lower()
    if mode == "replay":
        replay_path = env.get("AIR1_ALL_ZONES_REPLAY_PARQUET")
        if not replay_path:
            raise RuntimeError("AIR1_ALL_ZONES_DATA_SOURCE=replay requires AIR1_ALL_ZONES_REPLAY_PARQUET")
        start_raw = env.get("AIR1_ALL_ZONES_REPLAY_START", "").strip()
        start_ts = pd.Timestamp(start_raw) if start_raw else None
        return ReplayParquetDataSource(parquet_path=_workspace_path(replay_path), start_timestamp=start_ts)
    if mode == "parquet_tail":
        parquet_dir = env.get("AIR1_ALL_ZONES_LIVE_PARQUET_DIR")
        if parquet_dir:
            return LiveParquetTailDataSource(parquet_dir=_workspace_path(parquet_dir))
        return LiveParquetTailDataSource(parquet_dir=training.DEFAULT_TRAINING_PARQUET_DIR)
    if mode == "live":
        cache_enabled_raw = env.get("AIR1_ALL_ZONES_LIVE_CACHE_ENABLED", "true").strip().lower()
        cache_enabled = cache_enabled_raw not in {"0", "false", "no", "off"}
        sen55_table = env.get("SEN55_TABLE") or env.get("SEN55_CSV")
        return LiveAir1DataSource(
            lookback_safety_minutes=int(env.get("AIR1_ALL_ZONES_LIVE_LOOKBACK_SAFETY_MIN", "30")),
            api_timeout=float(env.get("AIR1_ALL_ZONES_LIVE_API_TIMEOUT_SEC", "30")),
            api_retries=int(env.get("AIR1_ALL_ZONES_LIVE_API_RETRIES", "1")),
            max_workers=int(env.get("AIR1_ALL_ZONES_LIVE_MAX_WORKERS", "4")),
            cache_enabled=cache_enabled,
            cache_max_age_minutes=float(env.get("AIR1_ALL_ZONES_LIVE_CACHE_MAX_AGE_MIN", "30")),
            overlap_minutes=float(env.get("AIR1_ALL_ZONES_LIVE_CACHE_OVERLAP_MIN", "2")),
            sen55_csv=_workspace_path(sen55_table) if sen55_table else _default_sen55_csv(),
        )
    raise RuntimeError(
        f"Unknown AIR1_ALL_ZONES_DATA_SOURCE value: {mode!r}; expected 'live', 'parquet_tail', or 'replay'"
    )
