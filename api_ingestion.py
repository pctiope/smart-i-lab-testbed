"""
api_ingestion.py — Smart i-Lab REST API → Bronze layer (Parquet + DuckDB)
=========================================================================

Pipeline steps
--------------
1. Ensure the bronze/silver/gold schemas exist and migrate legacy flat table names.
2. Fetch the latest reading timestamp from the API for each device type.
3. Compare with the latest timestamp in the bronze DuckDB table.
4. If bronze is stale or missing:
    a. Fetch historical or incremental data from the API.
    b. Write to the Hive-partitioned Parquet store.
    c. Rebuild the bronze DuckDB table from the full Parquet store.
5. If silver or gold are stale or missing, run the bronze-to-silver and silver-to-gold Python pipelines.
6. During live polling, only new incoming data is appended, then downstream layers are refreshed if needed.

CLI usage
---------
    python api_ingestion.py                         # air-1 only, one-shot
    python api_ingestion.py --all                   # all 6 device types
    python api_ingestion.py --all --initialize      # init schemas + full history + bronze/silver/gold
    python api_ingestion.py --all --lookback 48     # bootstrap: last 48 h
    python api_ingestion.py --all --full-history    # bootstrap: all API-retained history
    python api_ingestion.py --all --poll 5          # poll every 5 minutes
    python api_ingestion.py --all --force-rebuild   # force full reinit
"""

import argparse
import logging
import os
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from importlib import import_module as _im
from pathlib import Path
from urllib.parse import quote

import pandas as pd
import requests

# ── Storage layer ─────────────────────────────────────────────────────────────
_storage = _im("CSV Training Data Code")
_b2s = _im("bronze2silver_preprocess")
_s2g = _im("silver2gold_preprocess")

DEVICE_TYPES                = _storage.DEVICE_TYPES
DEVICE_TO_POSITION          = _storage.DEVICE_TO_POSITION

# Zigbee2MQTT devices that are confirmed to have historical sensor data
# (excludes groups like 'tables', 'ambient_front', and actuator-only devices
#  like H_R*/SP*/V_C* whose DB tables are not set up for historical queries)
ZIGBEE_DATA_DEVICES = [
    "top_lights_switch",
    "table_lights_switch",
    "room_lights_switch",
    "front_lights_switch",
    "table_right",
    "aqara_driver_1",
]
DEFAULT_STAGE_MAX_AGE_SECONDS = _storage.DEFAULT_STAGE_MAX_AGE_SECONDS
DEFAULT_STAGE_MAX_ROWS      = _storage.DEFAULT_STAGE_MAX_ROWS

build_bronze_from_parquet   = _storage.build_bronze_from_parquet
ensure_database_layout      = _storage.ensure_database_layout
flush_staged_data           = _storage.flush_staged_data
get_latest_stored_timestamp = _storage.get_latest_stored_timestamp
save_dataframe              = _storage.save_dataframe
stage_and_maybe_flush       = _storage.stage_and_maybe_flush
_silver_table               = _storage._silver_table
_gold_table                 = _storage._gold_table
_table_exists               = _storage._table_exists
run_bronze_to_silver        = _b2s.run_bronze_to_silver
run_silver_to_gold          = _s2g.run_silver_to_gold

# ── API credentials ───────────────────────────────────────────────────────────
API_BASE_URL_ENV = "SMART_ILAB_BASE_URL"
API_KEY_ENV = "SMART_ILAB_API_KEY"
FULL_HISTORY_START = datetime(2000, 1, 1, tzinfo=timezone.utc)
API_LOCAL_TIMEZONE = timezone(timedelta(hours=8), "Asia/Singapore")
LOGGER = logging.getLogger("api_ingestion")


def configure_logging(log_path: str | None = None) -> None:
    if LOGGER.handlers:
        return

    LOGGER.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)
    LOGGER.addHandler(console)

    if log_path:
        log_file = Path(log_path)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(formatter)
        LOGGER.addHandler(file_handler)


def _log(message: str, level: int = logging.INFO) -> None:
    if LOGGER.handlers:
        LOGGER.log(level, message)
    else:
        print(message)


# =============================================================================
# API client
# =============================================================================

class SmartILabAPIClient:
    def __init__(self, api_url: str, api_key: str):
        self.api_url = api_url.rstrip("/")
        self.headers = {"Accept": "*/*", "X-API-KEY": api_key}

    # ── Device listing ────────────────────────────────────────────────────────

    def get_all_devices(self, device_type: str) -> list[str]:
        try:
            r = requests.get(f"{self.api_url}/{device_type}", headers=self.headers, timeout=30)
            r.raise_for_status()
            payload = r.json()
            return payload if isinstance(payload, list) else list(payload)
        except Exception as exc:
            _log(f"[{device_type}] Failed to list devices: {exc}", logging.ERROR)
            return []

    # ── Single-device fetch ───────────────────────────────────────────────────

    def get_device_data(
        self,
        device_type: str,
        device_id: str,
        time_start: datetime | None = None,
        time_end:   datetime | None = None,
    ):
        url    = f"{self.api_url}/{device_type}/{device_id}"
        params = []
        if time_start:
            params.append(f"time_start={quote(self._fmt(time_start))}")
        if time_end:
            params.append(f"time_end={quote(self._fmt(time_end))}")
        if params:
            url = f"{url}?{'&'.join(params)}"
        try:
            r = requests.get(url, headers=self.headers, timeout=60)
            r.raise_for_status()
            return r.json()
        except requests.HTTPError as exc:
            # 4xx errors (groups, actuators without history tables) are expected
            # for many device types — log at debug level only, don't flood output
            if exc.response is not None and exc.response.status_code < 500:
                return None   # silently skip groups and no-history devices
            _log(f"[{device_type}/{device_id}] Server error: {exc}", logging.ERROR)
            return None
        except Exception as exc:
            _log(f"[{device_type}/{device_id}] Fetch error: {exc}", logging.ERROR)
            return None

    # ── Latest timestamp from API (windowed probe) ────────────────────────────

    def get_latest_api_timestamp(self, device_type: str) -> datetime | None:
        """
        Return the most recent reading timestamp available from the API.

        Strategy
        --------
        The API endpoint requires explicit time_start/time_end params — a
        no-parameter call returns an empty body.  We probe with a short
        window and widen it if empty, trying known-good devices first.

        Returns a tz-naive local (Asia/Singapore) datetime, or None.
        """
        devices = self.get_all_devices(device_type)
        if not devices:
            return None

        # Prefer devices in the known sensor map; fall back to full list
        if device_type == "air-1":
            ordered = [d for d in DEVICE_TO_POSITION if d in devices]
            ordered += [d for d in devices if d not in DEVICE_TO_POSITION]
        elif device_type == "zigbee2mqtt":
            # Only probe data-bearing devices; skip groups and actuators
            ordered = [d for d in ZIGBEE_DATA_DEVICES if d in devices]
        else:
            ordered = devices

        now = datetime.now(timezone.utc)
        # Probe windows: 2 h → 24 h → 72 h → 168 h (1 week)
        for lookback_h in (2, 24, 72, 168):
            t0 = now - timedelta(hours=lookback_h)
            for device_id in ordered:
                data = self.get_device_data(device_type, device_id,
                                            time_start=t0, time_end=now)
                if not data:
                    continue
                readings = data if isinstance(data, list) else [data]
                if not readings:
                    continue
                # Take the last (most recent) reading
                last = readings[-1]
                if isinstance(last, dict) and "timestamp" in last:
                    return self._to_local(last["timestamp"])
        return None

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _fmt(ts: datetime) -> str:
        return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    @staticmethod
    def _to_local(ts_value: str) -> datetime | None:
        try:
            parsed = pd.to_datetime(ts_value, utc=True)
            return parsed.tz_convert("Asia/Singapore").tz_localize(None).to_pydatetime()
        except Exception as exc:
            _log(f"Timestamp parse error '{ts_value}': {exc}", logging.ERROR)
            return None

    # ── Historical collect → DataFrame ────────────────────────────────────────

    def collect_historical_to_df(
        self,
        device_type: str,
        time_start:  datetime,
        time_end:    datetime,
    ) -> pd.DataFrame:
        devices = self.get_all_devices(device_type)
        if not devices:
            return pd.DataFrame()
        if device_type == "air-1":
            return self._collect_air1_wide(devices, time_start, time_end)
        if device_type == "zigbee2mqtt":
            # Restrict to devices known to have historical sensor data
            devices = [d for d in ZIGBEE_DATA_DEVICES if d in devices]
        return self._collect_generic(device_type, devices, time_start, time_end)

    def _collect_air1_wide(
        self,
        devices: list[str],
        time_start: datetime,
        time_end:   datetime,
    ) -> pd.DataFrame:
        rows: dict = defaultdict(lambda: {"temps": {}, "rhs": {}, "co2s": {}, "pm25s": {}})
        fallback_positions: dict[str, int] = {}
        known_sensor_count = len(DEVICE_TO_POSITION)
        _log(f"\n{'='*60}\nCOLLECTING air-1 ({len(devices)} sensors)\n{'='*60}")

        for index, device_id in enumerate(devices, start=1):
            _log(f"[air-1] [{index}/{len(devices)}] Fetching sensor {device_id}")
            if device_id in DEVICE_TO_POSITION:
                pos = DEVICE_TO_POSITION[device_id]
            else:
                pos = fallback_positions.setdefault(
                    device_id,
                    known_sensor_count + len(fallback_positions) + 1,
                )
                _log(f"  [air-1] {device_id} not in sensor map - assigning fallback slot s{pos}")
            history = self.get_device_data("air-1", device_id, time_start, time_end)
            if not history:
                _log(f"  [air-1] {device_id} returned no history")
                continue
            reading_count = len(history) if isinstance(history, list) else 1
            _log(f"  [air-1] {device_id} mapped to s{pos} with {reading_count} readings")
            for reading in (history if isinstance(history, list) else [history]):
                if not isinstance(reading, dict) or "timestamp" not in reading:
                    continue
                local_dt = self._to_local(reading["timestamp"])
                if local_dt is None:
                    continue
                ts = local_dt.replace(microsecond=0)
                for field, store in [
                    ("temperature", "temps"),
                    ("humidity",    "rhs"),
                    ("co2",         "co2s"),
                    ("pm_2_5",      "pm25s"),
                ]:
                    if reading.get(field) is not None:
                        rows[ts][store][pos] = reading[field]

        records = []
        max_position = known_sensor_count
        if rows:
            max_position = max(
                known_sensor_count,
                max(
                    max((positions.keys()), default=known_sensor_count)
                    for positions in (
                        data[store_name]
                        for data in rows.values()
                        for store_name in ("temps", "rhs", "co2s", "pm25s")
                    )
                ),
            )
        for ts, data in sorted(rows.items()):
            rec = {"timestamp": ts}
            for i in range(1, max_position + 1):
                rec[f"temp_s{i}"]  = data["temps"].get(i)
                rec[f"rh_s{i}"]    = data["rhs"].get(i)
                rec[f"co2_s{i}"]   = data["co2s"].get(i)
                rec[f"pm25_s{i}"]  = data["pm25s"].get(i)
            records.append(rec)

        df = pd.DataFrame(records)
        if not df.empty:
            df = df.sort_values("timestamp").reset_index(drop=True)
        _log(f"[air-1] Collected {len(df)} wide rows")
        return df

    def _collect_generic(
        self,
        device_type: str,
        devices:     list[str],
        time_start:  datetime,
        time_end:    datetime,
    ) -> pd.DataFrame:
        records = []
        _log(f"\n{'='*60}\nCOLLECTING {device_type.upper()} ({len(devices)} devices)\n{'='*60}")

        for index, device_id in enumerate(devices, start=1):
            _log(f"[{device_type}] [{index}/{len(devices)}] Fetching device {device_id}")
            history = self.get_device_data(device_type, device_id, time_start, time_end)
            if not history:
                _log(f"[{device_type}] {device_id} returned no history")
                continue
            readings = history if isinstance(history, list) else [history]
            _log(f"[{device_type}] {device_id} returned {len(readings)} readings")
            for reading in pd.json_normalize(readings).to_dict(orient="records"):
                reading["device_id"]   = device_id
                reading["device_type"] = device_type
                if reading.get("timestamp") is not None:
                    local_dt = self._to_local(reading["timestamp"])
                    if local_dt is None:
                        continue
                    reading["timestamp"] = local_dt
                records.append(reading)

        df = pd.DataFrame(records)
        if not df.empty and "timestamp" in df.columns:
            df["timestamp"] = pd.to_datetime(df["timestamp"])
            df = df.sort_values(["timestamp", "device_id"]).reset_index(drop=True)
        _log(f"[{device_type}] Collected {len(df)} rows")
        return df


# =============================================================================
# Timestamp comparison and reinit decision
# =============================================================================

def check_needs_update(device_type: str, client: SmartILabAPIClient) -> tuple[bool, str]:
    """
    Compare latest API timestamp vs latest bronze DB timestamp.

    Returns (needs_update: bool, reason: str).
    """
    api_ts = client.get_latest_api_timestamp(device_type)
    if api_ts is None:
        return False, "Could not fetch API timestamp — skipping"

    db_ts = get_latest_stored_timestamp(device_type, layer="bronze")
    if db_ts is None:
        return True, f"No bronze data yet (API latest: {api_ts})"

    # Truncate to second for comparison (API timestamps are second-precision)
    api_ts_s = api_ts.replace(microsecond=0)
    db_ts_s  = db_ts.replace(microsecond=0)

    if api_ts_s <= db_ts_s:
        return False, f"Already up to date (DB: {db_ts_s}, API: {api_ts_s})"

    return True, f"DB stale — DB latest: {db_ts_s}, API latest: {api_ts_s}"


def _stored_timestamp_to_utc(ts_value: datetime) -> datetime:
    """Convert stored API-local timestamps back to UTC for API query windows."""
    parsed = pd.to_datetime(ts_value).to_pydatetime()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=API_LOCAL_TIMEZONE)
    return parsed.astimezone(timezone.utc)


# =============================================================================
# Ingest + bronze reinit
# =============================================================================

def ingest_and_rebuild_bronze(
    device_type:          str,
    client:               SmartILabAPIClient,
    lookback_hours:       int  = 24,
    history_start:        datetime | None = None,
    stage_max_rows:       int  = DEFAULT_STAGE_MAX_ROWS,
    stage_max_age_seconds: int = DEFAULT_STAGE_MAX_AGE_SECONDS,
    flush_force:          bool = False,
) -> tuple[int, int]:
    """
    Fetch new data from API → Parquet store → rebuild bronze DuckDB table.
    Returns (parquet_rows_written, bronze_rows_flushed).
    """
    now       = datetime.now(timezone.utc)
    latest_db = get_latest_stored_timestamp(device_type, layer="bronze")
    if latest_db is None:
        latest_db = get_latest_stored_timestamp(device_type, layer="parquet")

    if latest_db is None:
        if history_start is not None:
            fetch_from = history_start
            _log(f"[{device_type}] Bootstrap: fetching full history from {fetch_from.isoformat()}")
        else:
            fetch_from = now - timedelta(hours=lookback_hours)
            _log(f"[{device_type}] Bootstrap: fetching last {lookback_hours} h")
    else:
        fetch_from = _stored_timestamp_to_utc(latest_db) + timedelta(seconds=1)
        _log(f"[{device_type}] Incremental fetch from {fetch_from.isoformat()}")

    if fetch_from >= now:
        _log(f"[{device_type}] Already up to date.")
        return 0, 0

    df = client.collect_historical_to_df(device_type, fetch_from, now)
    if df.empty:
        _log(f"[{device_type}] No new rows from API.")
        return 0, 0

    # ── Write to Parquet store ────────────────────────────────────────────────
    df_ts = df.copy()
    df_ts["timestamp"] = pd.to_datetime(df_ts["timestamp"])
    for day_val, group in df_ts.groupby(df_ts["timestamp"].dt.date):
        partition_dt = datetime(day_val.year, day_val.month, day_val.day)
        _log(f"[{device_type}] Writing parquet partition for {partition_dt.date()} with {len(group)} rows")
        save_dataframe(group.reset_index(drop=True), device_type, partition_dt)

    # ── Rebuild bronze DuckDB table from full Parquet store ───────────────────
    _log(f"[{device_type}] Rebuilding bronze table from Parquet store ...")
    counts = build_bronze_from_parquet([device_type], rebuild=True)
    bronze_rows = counts.get(device_type, 0)

    # ── Also stage → flush for the live batch ─────────────────────────────────
    flushed = stage_and_maybe_flush(df_ts, device_type, max_rows=stage_max_rows,
                                    max_age_seconds=stage_max_age_seconds)
    if flush_force:
        flushed += flush_staged_data(device_type, force=True)

    _log(f"[{device_type}] Done - {len(df)} parquet rows, bronze table: {bronze_rows} rows total")
    return len(df), flushed


def downstream_needs_update(device_type: str) -> tuple[bool, str]:
    """Check whether silver/gold are missing or older than bronze."""
    bronze_ts = get_latest_stored_timestamp(device_type, layer="bronze")
    silver_name = _silver_table(device_type)
    gold_name = _gold_table(device_type)

    if bronze_ts is None:
        return False, "No bronze data yet"
    if not _table_exists(silver_name):
        return True, f"Missing silver table {silver_name}"
    if not _table_exists(gold_name):
        return True, f"Missing gold table {gold_name}"

    silver_ts = get_latest_stored_timestamp(device_type, layer="silver")
    gold_ts = get_latest_stored_timestamp(device_type, layer="gold")
    bronze_ts_s = bronze_ts.replace(microsecond=0)
    silver_ts_s = silver_ts.replace(microsecond=0) if silver_ts is not None else None
    gold_ts_s = gold_ts.replace(microsecond=0) if gold_ts is not None else None

    if silver_ts_s is None or silver_ts_s < bronze_ts_s:
        return True, f"Silver stale — bronze: {bronze_ts_s}, silver: {silver_ts_s}"
    if gold_ts_s is None or gold_ts_s < silver_ts_s:
        return True, f"Gold stale — silver: {silver_ts_s}, gold: {gold_ts_s}"
    return False, "Silver and gold up to date"


def run_downstream_layers(device_type: str, rebuild: bool = False) -> None:
    """Run bronze->silver then silver->gold for a single device type."""
    _log(f"[{device_type}] Running bronze -> silver ...")
    run_bronze_to_silver(device_type, rebuild=rebuild)
    _log(f"[{device_type}] Running silver -> gold ...")
    run_silver_to_gold(device_type, rebuild=rebuild)


# =============================================================================
# Entry point
# =============================================================================

def main():
    def _parse_history_start(value: str) -> datetime:
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(value, fmt)
                return parsed.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        raise argparse.ArgumentTypeError(
            "history start must be one of: YYYY-MM-DD, YYYY-MM-DD HH:MM, YYYY-MM-DD HH:MM:SS, YYYY-MM-DDTHH:MM:SS"
        )

    parser = argparse.ArgumentParser(description="Smart i-Lab API → Bronze/Silver/Gold ingestion")
    parser.add_argument("--device-type", default="air-1", choices=DEVICE_TYPES,
                        help="Device type to ingest (default: air-1)")
    parser.add_argument("--all",          action="store_true",  help="Ingest all 6 device types")
    parser.add_argument("--initialize",   action="store_true",
                        help="Initialize schemas/layout, backfill historical data for empty bronze, then build silver and gold")
    parser.add_argument("--lookback",     type=int, default=24, help="Bootstrap lookback in hours (default: 24)")
    parser.add_argument("--full-history", action="store_true",
                        help="When bootstrapping an empty layer, fetch all API-retained history starting from 2000-01-01 UTC")
    parser.add_argument("--history-start", type=_parse_history_start,
                        help="When bootstrapping an empty layer, fetch from this UTC timestamp instead of using --lookback")
    parser.add_argument("--poll",         type=int, default=0,  help="Poll every N minutes; 0 = one-shot")
    parser.add_argument("--force-rebuild", action="store_true", help="Force bronze table rebuild even if timestamps match")
    parser.add_argument("--stage-max-rows",         type=int, default=DEFAULT_STAGE_MAX_ROWS)
    parser.add_argument("--stage-max-age-seconds",  type=int, default=DEFAULT_STAGE_MAX_AGE_SECONDS)
    parser.add_argument("--log-path", default="api_ingestion.log", help="Path to the ingestion log file")
    args = parser.parse_args()

    if args.full_history and args.history_start is not None:
        parser.error("--full-history and --history-start cannot be used together")

    bootstrap_start = args.history_start
    if bootstrap_start is None and (args.full_history or args.initialize):
        bootstrap_start = FULL_HISTORY_START

    base_url = os.getenv(API_BASE_URL_ENV)
    if not base_url:
        parser.error(f"Missing required environment variable: {API_BASE_URL_ENV}")
    api_key = os.getenv(API_KEY_ENV)
    if not api_key:
        parser.error(f"Missing required environment variable: {API_KEY_ENV}")

    device_types  = DEVICE_TYPES if args.all else [args.device_type]
    flush_at_exit = args.poll == 0  # always flush on one-shot runs
    client        = SmartILabAPIClient(base_url, api_key)
    configure_logging(args.log_path)
    _log(f"Starting api_ingestion with device_types={device_types}, initialize={args.initialize}, poll={args.poll}, force_rebuild={args.force_rebuild}, log_path={args.log_path}")
    ensure_database_layout()
    _log("Database layout ensured")

    def run_once():
        total_parquet = total_flushed = 0
        for device_type in device_types:
            parquet_rows = 0
            flushed_rows = 0
            needs_update, reason = check_needs_update(device_type, client)
            _log(f"\n[{device_type}] {reason}")
            downstream_update, downstream_reason = downstream_needs_update(device_type)
            if downstream_update:
                _log(f"[{device_type}] {downstream_reason}")

            if needs_update or args.force_rebuild or args.initialize:
                _log(f"[{device_type}] Starting ingest cycle")
                parquet_rows, flushed_rows = ingest_and_rebuild_bronze(
                    device_type=device_type,
                    client=client,
                    lookback_hours=args.lookback,
                    history_start=bootstrap_start,
                    stage_max_rows=args.stage_max_rows,
                    stage_max_age_seconds=args.stage_max_age_seconds,
                    flush_force=flush_at_exit,
                )
                total_parquet += parquet_rows
                total_flushed += flushed_rows
            else:
                _log(f"[{device_type}] Skipping ingest.")

            if (
                args.initialize
                or args.force_rebuild
                or parquet_rows > 0
                or flushed_rows > 0
                or downstream_update
            ):
                run_downstream_layers(device_type, rebuild=args.initialize or args.force_rebuild)

        _log(f"\nIngestion complete - {total_parquet} parquet rows written, {total_flushed} flushed to bronze, downstream layers refreshed as needed.")

    if args.poll > 0:
        _log(f"Polling every {args.poll} minute(s). Ctrl+C to stop.")
        while True:
            run_once()
            time.sleep(args.poll * 60)
    else:
        run_once()


if __name__ == "__main__":
    main()
