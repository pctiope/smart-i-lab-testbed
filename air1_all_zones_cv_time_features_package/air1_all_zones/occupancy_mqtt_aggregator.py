from __future__ import annotations

import argparse
import json
import os
import statistics
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from air1_all_zones import csv_size_guard
from air1_all_zones.feature_contract import (
    ALL_ZONE_IDS,
    SAMPLE_INTERVAL,
    SAMPLE_INTERVAL_PANDAS_FREQ,
    TARGET_COLUMN,
    ZONE_ID_COLUMN,
    floor_datetime_to_sample,
)


PACKAGE_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PACKAGE_ROOT / "data"

DEFAULT_BROKER = os.getenv("OCCUPANCY_MQTT_BROKER", "10.158.71.19")
DEFAULT_PORT = int(os.getenv("OCCUPANCY_MQTT_PORT", "1883"))
DEFAULT_TOPIC = os.getenv("OCCUPANCY_MQTT_TOPIC", "care_ssl/all_zones/person_count_by_zone")
DEFAULT_USERNAME = os.getenv("OCCUPANCY_MQTT_USERNAME", "guest")
DEFAULT_PASSWORD = os.getenv("OCCUPANCY_MQTT_PASSWORD", "smartilab123")
DEFAULT_OUTPUT_CSV = DATA_DIR / "cv_occupancy_all_air1_10sec.csv"
DEFAULT_OUTPUT_PARQUET = DATA_DIR / "cv_occupancy_all_air1_10sec.parquet"

CSV_HEADERS = [
    "timestamp",
    ZONE_ID_COLUMN,
    "occupancy_count",
    TARGET_COLUMN,
    "sample_count",
    "min_count",
    "max_count",
    "mean_count",
    "median_count",
    "last_count",
    "first_message_time",
    "last_message_time",
    "source_topic",
    "camera_ids",
    "label_scope",
    "label_source",
]


@dataclass
class ZoneBucket:
    timestamp: datetime
    zone_id: int
    source_topic: str
    counts: list[int] = field(default_factory=list)
    first_message_time: datetime | None = None
    last_message_time: datetime | None = None
    camera_ids: set[str] = field(default_factory=set)
    label_sources: set[str] = field(default_factory=set)

    def add(
        self,
        count: int | None,
        message_time: datetime,
        camera_id: str | None = None,
        label_source: str | None = None,
    ) -> None:
        if count is not None:
            self.counts.append(count)
        if self.first_message_time is None or message_time < self.first_message_time:
            self.first_message_time = message_time
        if self.last_message_time is None or message_time > self.last_message_time:
            self.last_message_time = message_time
        if camera_id:
            self.camera_ids.add(str(camera_id))
        if label_source:
            self.label_sources.add(str(label_source))


class OccupancyAggregator:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.csv_path = Path(args.output_csv)
        self.parquet_path = Path(args.output_parquet) if args.output_parquet else None
        self.lock = threading.Lock()
        self.buckets: dict[tuple[datetime, int], ZoneBucket] = {}
        self.flushed_keys = self._load_existing_keys()
        self.parquet_warning_printed = False
        self.last_parquet_rebuild_mono = self._initial_parquet_rebuild_mono()
        self._init_csv()

    def add_payload(self, topic: str, payload_text: str) -> None:
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError as exc:
            print(f"Skipping invalid JSON payload: {exc}")
            return

        zone_counts = parse_counts_by_zone(payload)
        if not zone_counts:
            print("Skipping payload without per-zone counts_by_zone labels")
            return

        message_time = parse_payload_time(payload) if not self.args.use_receive_time else datetime.now()
        if message_time is None:
            message_time = datetime.now()

        minute_start = floor_to_minute(message_time)
        timestamp_key = format_timestamp(minute_start)
        camera_id = payload.get("camera_id")
        label_source = payload.get("label_source") or build_label_source(payload)
        with self.lock:
            for zone_id, count in zone_counts.items():
                key = (minute_start, zone_id)
                if key in self.flushed_keys:
                    print(f"Skipping late payload for already flushed bucket {timestamp_key} zone_id={zone_id}")
                    continue
                bucket = self.buckets.get(key)
                if bucket is None:
                    bucket = ZoneBucket(timestamp=minute_start, zone_id=zone_id, source_topic=topic)
                    self.buckets[key] = bucket
                bucket.add(count=count, message_time=message_time, camera_id=camera_id, label_source=label_source)

    def flush_due(self, force: bool = False) -> None:
        now = datetime.now()
        grace = timedelta(seconds=self.args.late_grace_seconds)
        rows: list[dict[str, Any]] = []

        with self.lock:
            for key in sorted(self.buckets):
                bucket_start, zone_id = key
                bucket = self.buckets[key]
                bucket_done_at = bucket_start + SAMPLE_INTERVAL + grace
                if not force and now < bucket_done_at:
                    continue
                rows.append(self._build_row(bucket))

            for row in rows:
                minute_start = parse_timestamp(str(row["timestamp"]))
                zone_id = parse_zone_id(row.get(ZONE_ID_COLUMN))
                if minute_start is None or zone_id is None:
                    continue
                key = (minute_start, zone_id)
                if key in self.buckets:
                    del self.buckets[key]
                self.flushed_keys.add(key)

        if rows:
            self._append_rows(rows)
            if self.parquet_path is not None:
                self.rebuild_parquet_if_due(reason="interval_or_missing")
            for row in rows:
                print(
                    f"Wrote {row['timestamp']} zone_id={row[ZONE_ID_COLUMN]} count={row['occupancy_count']} "
                    f"{TARGET_COLUMN}={row[TARGET_COLUMN]} samples={row['sample_count']}"
                )

    def _build_row(self, bucket: ZoneBucket) -> dict[str, Any]:
        counts = bucket.counts
        if counts:
            occupancy_count = aggregate_counts(counts, self.args.aggregate)
            median_count = aggregate_counts(counts, "median")
            mean_count = statistics.mean(counts)
            occupied: int | str = int(float(median_count) >= self.args.occupied_threshold)
            min_count: int | str = min(counts)
            max_count: int | str = max(counts)
            last_count: int | str = counts[-1]
        else:
            occupancy_count = ""
            median_count = ""
            mean_count = ""
            occupied = ""
            min_count = ""
            max_count = ""
            last_count = ""

        return {
            "timestamp": format_timestamp(bucket.timestamp),
            ZONE_ID_COLUMN: bucket.zone_id,
            "occupancy_count": format_number(occupancy_count) if counts else "",
            TARGET_COLUMN: occupied,
            "sample_count": len(counts),
            "min_count": min_count,
            "max_count": max_count,
            "mean_count": format_number(mean_count) if counts else "",
            "median_count": format_number(median_count) if counts else "",
            "last_count": last_count,
            "first_message_time": format_timestamp(bucket.first_message_time),
            "last_message_time": format_timestamp(bucket.last_message_time),
            "source_topic": bucket.source_topic,
            "camera_ids": "|".join(sorted(bucket.camera_ids)),
            "label_scope": "per_zone",
            "label_source": "|".join(sorted(bucket.label_sources)) or "rtsp_zone_tracker",
        }

    def _init_csv(self) -> None:
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        if csv_size_guard.has_csv_data(self.csv_path):
            csv_size_guard.ensure_rolling_csv_limit(self.csv_path, CSV_HEADERS)
            return
        if self.parquet_path is not None and self.parquet_path.is_file():
            self._seed_csv_from_parquet()
            if csv_size_guard.has_csv_data(self.csv_path):
                return
        csv_size_guard.write_header_if_missing(self.csv_path, CSV_HEADERS)

    def _seed_csv_from_parquet(self) -> None:
        if self.parquet_path is not None:
            self._seed_csv_from_parquet_path(self.parquet_path)

    def _seed_csv_from_parquet_path(self, parquet_path: Path) -> None:
        try:
            import pandas as pd
        except ImportError:
            return
        try:
            frame = pd.read_parquet(parquet_path)
            frame = normalize_output_frame(frame)
            if frame.empty:
                return
            csv_size_guard.write_dataframe_rolling_atomic(frame, self.csv_path)
        except Exception as exc:
            if not self.parquet_warning_printed:
                print(f"Could not seed CSV from parquet {parquet_path}: {exc}")
                self.parquet_warning_printed = True

    def _append_rows(self, rows: list[dict[str, Any]]) -> None:
        csv_size_guard.append_rows_rolling(self.csv_path, CSV_HEADERS, rows, extrasaction="ignore")

    def _parquet_rebuild_interval_seconds(self) -> float:
        return float(getattr(self.args, "parquet_rebuild_every_hours", 1.0)) * 3600.0

    def _initial_parquet_rebuild_mono(self) -> float:
        now_mono = time.monotonic()
        if self.parquet_path is None:
            return now_mono
        try:
            snapshot_age_seconds = max(0.0, time.time() - self.parquet_path.stat().st_mtime)
        except OSError:
            return now_mono
        return now_mono - snapshot_age_seconds

    def rebuild_parquet_if_due(self, reason: str = "interval") -> bool:
        if self.parquet_path is None:
            return False
        if not csv_size_guard.has_csv_rows(self.csv_path):
            return False
        interval_seconds = self._parquet_rebuild_interval_seconds()
        due = self.last_parquet_rebuild_mono <= 0.0 or (
            time.monotonic() - self.last_parquet_rebuild_mono >= interval_seconds
        )
        missing = not self.parquet_path.is_file()
        if reason == "shutdown" or due or missing:
            self._rewrite_parquet()
            self.last_parquet_rebuild_mono = time.monotonic()
            return True
        return False

    def _rewrite_parquet(self) -> None:
        try:
            import pandas as pd
        except ImportError:
            if not self.parquet_warning_printed:
                print("Parquet output requires pandas and pyarrow. Install with: pip install pandas pyarrow")
                self.parquet_warning_printed = True
            return

        try:
            assert self.parquet_path is not None
            self.parquet_path.parent.mkdir(parents=True, exist_ok=True)
            frame = csv_size_guard.read_csv_parts(self.csv_path)
            frame = normalize_output_frame(frame)
            frame.to_parquet(self.parquet_path, index=False)
        except Exception as exc:
            if not self.parquet_warning_printed:
                print(f"Could not update parquet file {self.parquet_path}: {exc}")
                self.parquet_warning_printed = True

    def _load_existing_keys(self) -> set[tuple[datetime, int]]:
        if csv_size_guard.has_csv_data(self.csv_path):
            values: set[tuple[datetime, int]] = set()
            for row in csv_size_guard.iter_dict_rows(self.csv_path):
                raw_value = row.get("timestamp") or row.get("minute_start")
                parsed = parse_timestamp(raw_value)
                zone_id = parse_zone_id(row.get(ZONE_ID_COLUMN))
                if parsed is not None and zone_id is not None:
                    values.add((floor_to_minute(parsed), zone_id))
            return values
        if self.parquet_path is not None and self.parquet_path.is_file():
            try:
                import pandas as pd

                frame = pd.read_parquet(self.parquet_path, columns=["timestamp", ZONE_ID_COLUMN])
                timestamps = pd.to_datetime(frame["timestamp"], errors="coerce").dropna()
                zones = pd.to_numeric(frame[ZONE_ID_COLUMN], errors="coerce")
                values = set()
                for idx, timestamp in timestamps.items():
                    zone_id = parse_zone_id(zones.loc[idx])
                    if zone_id is not None:
                        values.add((floor_to_minute(timestamp.to_pydatetime()), zone_id))
                return values
            except Exception:
                return set()
        return set()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Subscribe to per-zone MQTT person counts and write AIR-1 CV ground truth at 10-second cadence."
    )
    parser.add_argument("--mqtt-broker", default=DEFAULT_BROKER, help="MQTT broker host.")
    parser.add_argument("--mqtt-port", type=int, default=DEFAULT_PORT, help="MQTT broker port.")
    parser.add_argument("--mqtt-topic", default=DEFAULT_TOPIC, help="MQTT topic to subscribe to.")
    parser.add_argument("--mqtt-username", default=DEFAULT_USERNAME, help="Optional MQTT username.")
    parser.add_argument("--mqtt-password", default=DEFAULT_PASSWORD, help="Optional MQTT password.")
    parser.add_argument("--mqtt-client-id", default="air1_all_zones_cv_occupancy_10sec", help="MQTT client ID.")
    parser.add_argument("--output-csv", type=Path, default=DEFAULT_OUTPUT_CSV, help="Output CSV path.")
    parser.add_argument(
        "--output-parquet",
        default=str(DEFAULT_OUTPUT_PARQUET),
        help="Output parquet path. Pass an empty string to disable parquet.",
    )
    parser.add_argument(
        "--parquet-rebuild-every-hours",
        type=float,
        default=1.0,
        help="Rebuild the Parquet snapshot from the rolling CSV this often. Default: 1.",
    )
    parser.add_argument(
        "--aggregate",
        choices=["max", "last", "mean", "median"],
        default="median",
        help="How to consolidate multiple count messages in a 10-second bucket. Default: median.",
    )
    parser.add_argument(
        "--occupied-threshold",
        type=float,
        default=1.0,
        help="Bucket is occupied when median_count is at least this value. Default: 1.",
    )
    parser.add_argument(
        "--late-grace-seconds",
        type=float,
        default=5.0,
        help="Wait this long after a 10-second bucket ends before flushing it. Default: 5.",
    )
    parser.add_argument(
        "--flush-check-seconds",
        type=float,
        default=1.0,
        help="How often to check for completed 10-second buckets. Default: 1.",
    )
    parser.add_argument(
        "--use-receive-time",
        action="store_true",
        help="Bucket messages by local receive time instead of payload timestamp.",
    )
    return parser.parse_args()


def create_mqtt_client(client_id: str):
    try:
        from paho.mqtt import client as mqtt_client
    except ImportError as exc:
        raise SystemExit("Install MQTT dependency with: pip install paho-mqtt") from exc

    try:
        return mqtt_client.Client(mqtt_client.CallbackAPIVersion.VERSION1, client_id)
    except AttributeError:
        return mqtt_client.Client(client_id)
    except TypeError:
        return mqtt_client.Client(client_id)


def parse_zone_id(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        zone_id = int(float(value))
    except (TypeError, ValueError):
        return None
    if zone_id not in ALL_ZONE_IDS:
        return None
    return zone_id


def parse_nullable_count(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return None


def parse_counts_by_zone(payload: dict[str, Any]) -> dict[int, int | None]:
    raw_counts = payload.get("counts_by_zone")
    parsed: dict[int, int | None] = {}
    if isinstance(raw_counts, dict):
        for raw_zone_id, raw_count in raw_counts.items():
            zone_id = parse_zone_id(raw_zone_id)
            if zone_id is not None:
                parsed[zone_id] = parse_nullable_count(raw_count)

    raw_unlabeled = payload.get("unlabeled_zones")
    if isinstance(raw_unlabeled, list):
        for raw_zone_id in raw_unlabeled:
            zone_id = parse_zone_id(raw_zone_id)
            if zone_id is not None and zone_id not in parsed:
                parsed[zone_id] = None
    return parsed


def build_label_source(payload: dict[str, Any]) -> str:
    parts = ["rtsp_zone_tracker"]
    if payload.get("camera_id"):
        parts.append(str(payload["camera_id"]))
    if payload.get("zone_map"):
        parts.append(str(payload["zone_map"]))
    if payload.get("mask"):
        parts.append(str(payload["mask"]))
    return ":".join(parts)


def parse_payload_time(payload: dict[str, Any]) -> datetime | None:
    for key in ("timestamp", "message_time", "time"):
        value = payload.get(key)
        if not value:
            continue
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value)
        if isinstance(value, str):
            parsed = parse_timestamp(value)
            if parsed is not None:
                return parsed
    return None


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "nat"}:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d_%H-%M-%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone().replace(tzinfo=None)
        return parsed
    except ValueError:
        return None


def floor_to_minute(value: datetime) -> datetime:
    return floor_datetime_to_sample(value)


def aggregate_counts(counts: list[int], mode: str) -> float:
    if not counts:
        return 0.0
    if mode == "last":
        return float(counts[-1])
    if mode == "mean":
        return float(statistics.mean(counts))
    if mode == "median":
        return float(statistics.median(counts))
    return float(max(counts))


def format_timestamp(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.strftime("%Y-%m-%d %H:%M:%S")


def format_number(value: float | int) -> int | float:
    if isinstance(value, int) or float(value).is_integer():
        return int(value)
    return round(float(value), 3)


def normalize_output_frame(frame: Any) -> Any:
    import pandas as pd

    normalized = frame.copy()
    if "timestamp" not in normalized.columns and "minute_start" in normalized.columns:
        normalized = normalized.rename(columns={"minute_start": "timestamp"})
    if TARGET_COLUMN not in normalized.columns:
        for legacy_col in ("cv_is_occupied", "is_occupied", "zone_occupied"):
            if legacy_col in normalized.columns:
                normalized = normalized.rename(columns={legacy_col: TARGET_COLUMN})
                break
    for col in CSV_HEADERS:
        if col not in normalized.columns:
            normalized[col] = "" if col in {
                "first_message_time",
                "last_message_time",
                "source_topic",
                "camera_ids",
                "label_scope",
                "label_source",
                TARGET_COLUMN,
            } else 0
    normalized = normalized[CSV_HEADERS].copy()
    normalized["timestamp"] = pd.to_datetime(normalized["timestamp"], errors="coerce").dt.floor(
        SAMPLE_INTERVAL_PANDAS_FREQ
    )
    normalized[ZONE_ID_COLUMN] = pd.to_numeric(normalized[ZONE_ID_COLUMN], errors="coerce").astype("Int64")
    normalized = normalized.dropna(subset=["timestamp", ZONE_ID_COLUMN])
    normalized = normalized.loc[normalized[ZONE_ID_COLUMN].astype(int).isin(ALL_ZONE_IDS)].copy()
    normalized["timestamp"] = normalized["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S")
    for col in [
        "occupancy_count",
        "sample_count",
        "min_count",
        "max_count",
        "mean_count",
        "median_count",
        "last_count",
    ]:
        normalized[col] = pd.to_numeric(normalized[col], errors="coerce")
    normalized[TARGET_COLUMN] = pd.to_numeric(normalized[TARGET_COLUMN], errors="coerce")
    normalized["label_scope"] = normalized["label_scope"].replace("", "per_zone").fillna("per_zone")
    normalized = normalized.sort_values(["timestamp", ZONE_ID_COLUMN]).drop_duplicates(["timestamp", ZONE_ID_COLUMN], keep="last")
    return normalized.reset_index(drop=True)


def main() -> None:
    args = parse_args()
    if str(args.output_parquet).strip() == "":
        args.output_parquet = None

    aggregator = OccupancyAggregator(args)
    client = create_mqtt_client(args.mqtt_client_id)
    if args.mqtt_username or args.mqtt_password:
        client.username_pw_set(args.mqtt_username, args.mqtt_password)

    def on_connect(mqtt, userdata, flags, rc):
        if rc == 0:
            print(f"Connected to MQTT broker {args.mqtt_broker}:{args.mqtt_port}")
            print(f"Subscribing to {args.mqtt_topic}")
            mqtt.subscribe(args.mqtt_topic)
        else:
            print(f"MQTT connection failed with rc={rc}")

    def on_message(mqtt, userdata, msg):
        payload_text = msg.payload.decode(errors="replace")
        aggregator.add_payload(msg.topic, payload_text)

    def on_disconnect(mqtt, userdata, rc):
        if rc != 0:
            print(f"MQTT disconnected unexpectedly with rc={rc}")

    client.on_connect = on_connect
    client.on_message = on_message
    client.on_disconnect = on_disconnect
    client.reconnect_delay_set(min_delay=1, max_delay=60)

    print(f"Writing CV ground-truth CSV to {Path(args.output_csv).resolve()}")
    if args.output_parquet:
        print(f"Writing CV ground-truth parquet to {Path(args.output_parquet).resolve()}")
    print(f"Aggregation: {args.aggregate}; occupied threshold: {args.occupied_threshold}")
    aggregator.rebuild_parquet_if_due(reason="missing")

    client.connect(args.mqtt_broker, args.mqtt_port, keepalive=60)
    client.loop_start()

    try:
        while True:
            time.sleep(args.flush_check_seconds)
            aggregator.flush_due(force=False)
            aggregator.rebuild_parquet_if_due(reason="interval")
    except KeyboardInterrupt:
        print("Stopping CV occupancy aggregator...")
    finally:
        aggregator.flush_due(force=True)
        aggregator.rebuild_parquet_if_due(reason="shutdown")
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()


