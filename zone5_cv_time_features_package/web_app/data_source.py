from __future__ import annotations

import logging
import math
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

import pandas as pd

WEB_APP_DIR = Path(__file__).resolve().parent
WORKSPACE_DIR = WEB_APP_DIR.parent
if str(WORKSPACE_DIR) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_DIR))

from zone5 import feature_builder  # noqa: E402
from zone5 import model as training  # noqa: E402

logger = logging.getLogger("zone5_web_app.data_source")


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


def _read_clean_frame(table_path: Path) -> pd.DataFrame:
    if not table_path.is_file():
        raise FileNotFoundError(f"Replay table not found: {table_path}")
    if table_path.suffix.lower() == ".csv":
        from zone5 import csv_size_guard  # noqa: E402

        frame = csv_size_guard.read_csv_parts(table_path)
    elif table_path.suffix.lower() == ".parquet":
        frame = pd.read_parquet(table_path)
    else:
        raise ValueError(f"Replay table must be .csv or .parquet: {table_path}")
    frame = frame.copy()
    if training.TIMESTAMP_COLUMN not in frame.columns:
        raise ValueError(f"Replay table is missing required timestamp column: {table_path}")
    frame[training.TIMESTAMP_COLUMN] = pd.to_datetime(frame[training.TIMESTAMP_COLUMN], errors="coerce").dt.floor(
        training.SAMPLE_INTERVAL_PANDAS_FREQ
    )
    for col in training.RAW_FEATURE_COLUMNS:
        if col not in frame.columns:
            frame[col] = pd.NA
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame = frame.dropna(subset=[training.TIMESTAMP_COLUMN])
    frame = frame.sort_values(training.TIMESTAMP_COLUMN)
    frame = frame.drop_duplicates(subset=[training.TIMESTAMP_COLUMN], keep="last")
    frame = frame.reset_index(drop=True)
    if frame.empty:
        raise ValueError(f"Replay table has no valid rows after cleaning: {table_path}")
    return frame


class ReplayTableDataSource:
    """Replays a fixed CSV or Parquet by advancing one row per get_latest_window() call.

    The web app tick interval controls real-time speed: lower tick_interval_sec
    fast-forwards the replay. reference_time is the simulated current row's
    timestamp, so the freshness check inside predict_zone_5_probability passes.
    """

    def __init__(
        self,
        table_path: Path | str,
        start_timestamp: pd.Timestamp | None = None,
        slack_rows: int = 30,
    ) -> None:
        self._frame = _read_clean_frame(Path(table_path))
        self.table_path = Path(table_path)
        self.slack_rows = int(slack_rows)
        if start_timestamp is None:
            self._cursor = -1
        else:
            ts_series = self._frame[training.TIMESTAMP_COLUMN]
            mask = ts_series >= pd.Timestamp(start_timestamp)
            if not mask.any():
                raise ValueError(
                    f"start_timestamp {start_timestamp} is after all rows in {table_path}"
                )
            self._cursor = int(ts_series[mask].index[0]) - 1

    @property
    def label(self) -> str:
        return f"replay[{self.table_path.name}]"

    @property
    def total_rows(self) -> int:
        return int(len(self._frame))

    @property
    def cursor(self) -> int:
        return int(self._cursor)

    def get_latest_window(self, lookback: int) -> tuple[pd.DataFrame, pd.Timestamp]:
        self._cursor = min(max(self._cursor + 1, lookback - 1), len(self._frame) - 1)
        end = self._cursor + 1
        take = lookback + self.slack_rows
        start = max(0, end - take)
        window = self._frame.iloc[start:end].reset_index(drop=True)
        if len(window) < lookback:
            raise ValueError(
                f"Replay window has {len(window)} rows so far; need {lookback} before first tick. "
                "Choose an earlier start_timestamp or wait for cursor to advance."
            )
        return window, window[training.TIMESTAMP_COLUMN].iloc[-1]


@dataclass
class _RollingCache:
    """Stores the trailing 10-second Zone 5 frame so warm ticks fetch only deltas.

    Cache is grown by `merge`: concat + drop_duplicates(keep="last") so a freshly
    re-fetched bucket supersedes the cached version, and any bucket already in
    cache survives even when it's missing from the delta response (transient
    sensor flap).
    """

    frame: pd.DataFrame
    last_warm_fetch_ts: float = 0.0

    @property
    def last_minute(self) -> pd.Timestamp:
        return pd.Timestamp(self.frame[training.TIMESTAMP_COLUMN].iloc[-1])

    def merge(self, new_frame: pd.DataFrame, max_keep: int) -> None:
        if new_frame is None or new_frame.empty:
            return
        merged = pd.concat([self.frame, new_frame], ignore_index=True)
        merged = merged.sort_values(training.TIMESTAMP_COLUMN)
        merged = merged.drop_duplicates(subset=[training.TIMESTAMP_COLUMN], keep="last")
        if len(merged) > max_keep:
            merged = merged.tail(max_keep)
        self.frame = merged.reset_index(drop=True)


class LiveAir1DataSource:
    """Fetch the trailing `lookback + slack` minutes from the AIR-1 API each tick.

    Reuses the AIR-1 aggregation pipeline and Zone 5 source-spec helpers. CV
    ground truth is tailed separately for audit/display only and is never passed
    as a model input.

    Caching: a `_RollingCache` holds the trailing window between ticks. Cold
    path (first tick or stale cache) fetches `lookback + safety` minutes at
    once. Warm path fetches a small overlap window (`overlap_minutes` back to
    now) and merges. Disable with `cache_enabled=False`.

    Returns wall-clock as the reference_time so `predict_zone_5_probability`
    can detect when the API stops returning fresh rows.
    """

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
        from zone5 import air1_exporter as csv_training  # noqa: E402
        from zone5 import air1_sources  # noqa: E402

        self._air1_sources = air1_sources
        self._csv_training = csv_training

        resolved_url = api_url or os.environ.get("AIR1_API_URL") or self._csv_training.API_URL
        resolved_key = api_key or os.environ.get("AIR1_API_KEY") or self._csv_training.API_KEY
        if not resolved_url or not resolved_key:
            raise RuntimeError(
                "AIR-1 API credentials missing: set AIR1_API_URL and AIR1_API_KEY "
                "(or pass api_url/api_key) before starting the web app."
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
        self._zone_5_source_devices: dict[str, list[str]] | None = None
        self._cache: _RollingCache | None = None
        self._refresh_device_roster()

    @property
    def label(self) -> str:
        suffix = "" if self.cache_enabled else " no-cache"
        sen55_suffix = " sen55" if self.sen55_csv.is_file() else " sen55-missing"
        return f"live[air1_api{suffix}{sen55_suffix}]"

    def _refresh_device_roster(self) -> None:
        all_devices = self._client.get_all_devices()
        if not all_devices:
            raise RuntimeError(
                "AIR-1 API returned no devices. Check ZONE5_RTSP_URL host reachability and AIR1_API_URL."
            )
        source_devices = self._air1_sources.zone_5_source_devices(all_devices)
        if not source_devices.get("air1"):
            expected = self._csv_training.SENSOR_ORDER[self._air1_sources.ZONE_NUM - 1]
            raise RuntimeError(
                f"Zone 5 AIR-1 sensor {expected} is not in the network device list. "
                "Confirm the sensor is online before starting live mode."
            )
        self._zone_5_source_devices = source_devices
        logger.info(
            "AIR-1 roster cached: air1=%s smart_plug=%s mmwave=%s",
            source_devices.get("air1"),
            source_devices.get("smart_plug"),
            source_devices.get("mmwave"),
        )

    def _fetch_window(self, time_start: datetime, time_end: datetime) -> pd.DataFrame:
        """Fetch + aggregate the [time_start, time_end] window into a cleaned 10-second frame."""
        minute_index = self._csv_training.build_local_minute_index(time_start, time_end)
        if not minute_index:
            empty_cols = [training.TIMESTAMP_COLUMN, *training.FEATURE_COLUMNS]
            return pd.DataFrame(columns=empty_cols)

        fetch_started = time.perf_counter()
        # Coupling note: _fetch_adaptive_historical_sources is module-private on
        # csv_training.Air1Device. We reach into it because the public surface
        # only exposes per-device single-shot fetches; the bulk concurrent
        # fetcher is what the historical collectors use too. If csv_training ever
        # promotes this to a public alias, switch to it; if it gets renamed,
        # this line is the silent break-point.
        histories_by_source, failures = self._client._fetch_adaptive_historical_sources(
            self._air1_sources.build_zone_5_source_specs(self._zone_5_source_devices),
            time_start,
            time_end,
        )
        fetch_elapsed = time.perf_counter() - fetch_started
        if fetch_elapsed > self.slow_fetch_warn_sec:
            logger.warning(
                "AIR-1 fetch slow: %.2fs for %s..%s (failures=%d)",
                fetch_elapsed, time_start, time_end, len(failures),
            )
        if failures:
            logger.warning(
                "AIR-1 fetch reported %d failure(s); proceeding with whatever was returned",
                len(failures),
            )

        return feature_builder.build_zone_5_feature_frame_from_histories(
            minute_index=minute_index,
            histories_by_source=histories_by_source,
            time_start=time_start,
            time_end=time_end,
            sen55_csv=self.sen55_csv,
            logger=logger,
        )

    def _merge_sen55_features(self, frame: pd.DataFrame) -> pd.DataFrame:
        return feature_builder.merge_sen55_features(frame, self.sen55_csv, logger=logger)

    def get_latest_window(self, lookback: int) -> tuple[pd.DataFrame, pd.Timestamp]:
        if self._zone_5_source_devices is None:
            self._refresh_device_roster()

        # time_end_utc is what _fetch_window expects (naive UTC: format_api_timestamp
        # appends Z, parse_timestamp_to_local treats naive as UTC and adds LOCAL_OFFSET).
        # time_end_local is the cache-comparison clock: cache.last_minute reads off
        # frame[TIMESTAMP_COLUMN], which the aggregator already converted to local
        # naive. Mixing the two makes the cache appear "ahead of wall clock" and the
        # warm-fetch path returns stale rows forever (see commit message).
        time_end_utc = self._csv_training.sample_floor(datetime.now(timezone.utc).replace(tzinfo=None))
        time_end_local = time_end_utc + self._csv_training.LOCAL_OFFSET
        lookback_minutes = lookback * self._csv_training.SAMPLE_INTERVAL_SECONDS / 60.0
        cold_minutes = lookback_minutes + self.lookback_safety_minutes
        safety_rows = math.ceil(self.lookback_safety_minutes * 60.0 / self._csv_training.SAMPLE_INTERVAL_SECONDS)
        max_keep = lookback + safety_rows + 20

        cold_fetch = (
            not self.cache_enabled
            or self._cache is None
            or self._cache.frame.empty
            or (time_end_local - self._cache.last_minute.to_pydatetime()).total_seconds() / 60.0
            > self.cache_max_age_minutes
        )

        if cold_fetch:
            time_start_utc = time_end_utc - timedelta(minutes=cold_minutes)
            fetched = self._fetch_window(time_start_utc, time_end_utc)
            if self.cache_enabled:
                trimmed = fetched.tail(max_keep).reset_index(drop=True) if len(fetched) > max_keep else fetched
                self._cache = _RollingCache(frame=trimmed, last_warm_fetch_ts=time.time())
            frame = fetched
        else:
            assert self._cache is not None
            delta_start_local = (
                self._cache.last_minute.to_pydatetime()
                - timedelta(minutes=self.overlap_minutes)
            )
            if delta_start_local >= time_end_local:
                # Wall clock has not crossed a new minute since the last fetch.
                frame = self._cache.frame
            else:
                # _fetch_window expects naive UTC bounds; subtract LOCAL_OFFSET back out.
                delta_start_utc = delta_start_local - self._csv_training.LOCAL_OFFSET
                new_frame = self._fetch_window(delta_start_utc, time_end_utc)
                self._cache.merge(new_frame, max_keep=max_keep)
                self._cache.last_warm_fetch_ts = time.time()
                frame = self._cache.frame

        if len(frame) < lookback:
            raise ValueError(
                f"AIR-1 fetch returned only {len(frame)} valid Zone 5 rows for the last "
                f"{cold_minutes} minutes; need {lookback}. "
                "A sensor may be offline or the API may be lagging."
            )

        take = lookback + self.slack_rows
        if len(frame) > take:
            frame = frame.tail(take).reset_index(drop=True)

        return frame, pd.Timestamp.now()


def build_data_source_from_env(env: dict[str, str]) -> DataSource:
    mode = env.get("ZONE5_DATA_SOURCE", "live").strip().lower()
    if mode == "replay":
        replay_path = env.get("ZONE5_REPLAY_TABLE")
        if not replay_path:
            raise RuntimeError("ZONE5_DATA_SOURCE=replay requires ZONE5_REPLAY_TABLE")
        start_raw = env.get("ZONE5_REPLAY_START", "").strip()
        start_ts: pd.Timestamp | None = None
        if start_raw:
            start_ts = pd.Timestamp(start_raw)
        return ReplayTableDataSource(table_path=_workspace_path(replay_path), start_timestamp=start_ts)
    if mode == "live":
        cache_enabled_raw = env.get("ZONE5_LIVE_CACHE_ENABLED", "true").strip().lower()
        cache_enabled = cache_enabled_raw not in {"0", "false", "no", "off"}
        sen55_table = env.get("SEN55_TABLE") or env.get("SEN55_CSV")
        return LiveAir1DataSource(
            lookback_safety_minutes=int(env.get("ZONE5_LIVE_LOOKBACK_SAFETY_MIN", "30")),
            api_timeout=float(env.get("ZONE5_LIVE_API_TIMEOUT_SEC", "30")),
            api_retries=int(env.get("ZONE5_LIVE_API_RETRIES", "1")),
            max_workers=int(env.get("ZONE5_LIVE_MAX_WORKERS", "4")),
            cache_enabled=cache_enabled,
            cache_max_age_minutes=float(env.get("ZONE5_LIVE_CACHE_MAX_AGE_MIN", "30")),
            overlap_minutes=float(env.get("ZONE5_LIVE_CACHE_OVERLAP_MIN", "2")),
            sen55_csv=_workspace_path(sen55_table) if sen55_table else _default_sen55_csv(),
        )
    raise RuntimeError(
        f"Unknown ZONE5_DATA_SOURCE value: {mode!r}; expected 'live' or 'replay'"
    )
