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

from zone5 import csv_size_guard
from zone5.feature_contract import (
    SAMPLE_INTERVAL,
    SAMPLE_INTERVAL_PANDAS_FREQ,
    floor_datetime_to_sample,
)


PACKAGE_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PACKAGE_ROOT / "data"

DEFAULT_BROKER = os.getenv("OCCUPANCY_MQTT_BROKER", "10.158.71.19")
DEFAULT_PORT = int(os.getenv("OCCUPANCY_MQTT_PORT", "1883"))
DEFAULT_TOPIC = os.getenv("OCCUPANCY_MQTT_TOPIC", "care_ssl/zone5/person_count")
DEFAULT_USERNAME = os.getenv("OCCUPANCY_MQTT_USERNAME", "guest")
DEFAULT_PASSWORD = os.getenv("OCCUPANCY_MQTT_PASSWORD", "smartilab123")
DEFAULT_OUTPUT_CSV = DATA_DIR / "cv_occupancy_zone5_10sec.csv"

CSV_HEADERS = [
    "timestamp",
    "occupancy_count",
    "cv_is_occupied",
    "sample_count",
    "min_count",
    "max_count",
    "mean_count",
    "median_count",
    "last_count",
    "first_message_time",
    "last_message_time",
    "source_topic",
]


@dataclass
class MinuteBucket:
    timestamp: datetime
    source_topic: str
    counts: list[int] = field(default_factory=list)
    first_message_time: datetime | None = None
    last_message_time: datetime | None = None

    def add(self, count: int, message_time: datetime) -> None:
        self.counts.append(count)
        if self.first_message_time is None or message_time < self.first_message_time:
            self.first_message_time = message_time
        if self.last_message_time is None or message_time > self.last_message_time:
            self.last_message_time = message_time


class OccupancyAggregator:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.csv_path = Path(args.output_csv)
        self.lock = threading.Lock()
        self.buckets: dict[datetime, MinuteBucket] = {}
        self.flushed_timestamps = self._load_existing_timestamps()
        self._init_csv()

    def add_payload(self, topic: str, payload_text: str) -> None:
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError as exc:
            print(f"Skipping invalid JSON payload: {exc}")
            return

        count = parse_count(payload)
        if count is None:
            print("Skipping payload without counted_persons/occupancy_count/count")
            return

        message_time = parse_payload_time(payload) if not self.args.use_receive_time else datetime.now()
        if message_time is None:
            message_time = datetime.now()

        minute_start = floor_to_minute(message_time)
        timestamp_key = format_timestamp(minute_start)
        with self.lock:
            if timestamp_key in self.flushed_timestamps:
                print(f"Skipping late payload for already flushed bucket {timestamp_key}")
                return
            bucket = self.buckets.get(minute_start)
            if bucket is None:
                bucket = MinuteBucket(timestamp=minute_start, source_topic=topic)
                self.buckets[minute_start] = bucket
            bucket.add(count=count, message_time=message_time)

    def flush_due(self, force: bool = False) -> None:
        now = datetime.now()
        grace = timedelta(seconds=self.args.late_grace_seconds)
        rows: list[dict[str, Any]] = []

        with self.lock:
            for bucket_start in sorted(self.buckets):
                bucket = self.buckets[bucket_start]
                bucket_done_at = bucket_start + SAMPLE_INTERVAL + grace
                if not force and now < bucket_done_at:
                    continue
                rows.append(self._build_row(bucket))

            for row in rows:
                minute_start = parse_timestamp(str(row["timestamp"]))
                if minute_start in self.buckets:
                    del self.buckets[minute_start]
                self.flushed_timestamps.add(str(row["timestamp"]))

        if rows:
            self._append_rows(rows)
            for row in rows:
                print(
                    f"Wrote {row['timestamp']} count={row['occupancy_count']} "
                    f"cv_is_occupied={row['cv_is_occupied']} samples={row['sample_count']}"
                )

    def _build_row(self, bucket: MinuteBucket) -> dict[str, Any]:
        counts = bucket.counts
        occupancy_count = aggregate_counts(counts, self.args.aggregate)
        median_count = aggregate_counts(counts, "median")
        mean_count = statistics.mean(counts) if counts else 0.0
        cv_is_occupied = int(float(median_count) >= self.args.occupied_threshold)

        return {
            "timestamp": format_timestamp(bucket.timestamp),
            "occupancy_count": format_number(occupancy_count),
            "cv_is_occupied": cv_is_occupied,
            "sample_count": len(counts),
            "min_count": min(counts) if counts else 0,
            "max_count": max(counts) if counts else 0,
            "mean_count": format_number(mean_count),
            "median_count": format_number(median_count),
            "last_count": counts[-1] if counts else 0,
            "first_message_time": format_timestamp(bucket.first_message_time),
            "last_message_time": format_timestamp(bucket.last_message_time),
            "source_topic": bucket.source_topic,
        }

    def _init_csv(self) -> None:
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        if csv_size_guard.has_csv_data(self.csv_path):
            csv_size_guard.ensure_rolling_csv_limit(self.csv_path, CSV_HEADERS)
            return
        csv_size_guard.write_header_if_missing(self.csv_path, CSV_HEADERS)

    def _append_rows(self, rows: list[dict[str, Any]]) -> None:
        csv_size_guard.append_rows_rolling(self.csv_path, CSV_HEADERS, rows, extrasaction="ignore")

    def _load_existing_timestamps(self) -> set[str]:
        if csv_size_guard.has_csv_data(self.csv_path):
            values = set()
            for row in csv_size_guard.iter_dict_rows(self.csv_path):
                raw_value = row.get("timestamp") or row.get("minute_start")
                parsed = parse_timestamp(raw_value)
                if parsed is not None:
                    values.add(format_timestamp(floor_to_minute(parsed)))
            return values
        return set()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Subscribe to MQTT person counts and write Zone 5 CV ground truth at 10-second cadence."
    )
    parser.add_argument("--mqtt-broker", default=DEFAULT_BROKER, help="MQTT broker host.")
    parser.add_argument("--mqtt-port", type=int, default=DEFAULT_PORT, help="MQTT broker port.")
    parser.add_argument("--mqtt-topic", default=DEFAULT_TOPIC, help="MQTT topic to subscribe to.")
    parser.add_argument("--mqtt-username", default=DEFAULT_USERNAME, help="Optional MQTT username.")
    parser.add_argument("--mqtt-password", default=DEFAULT_PASSWORD, help="Optional MQTT password.")
    parser.add_argument("--mqtt-client-id", default="zone5_cv_occupancy_10sec", help="MQTT client ID.")
    parser.add_argument("--output-csv", type=Path, default=DEFAULT_OUTPUT_CSV, help="Output CSV path.")
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


def parse_count(payload: dict[str, Any]) -> int | None:
    for key in ("counted_persons", "occupancy_count", "count"):
        if key in payload and payload[key] is not None:
            try:
                return max(0, int(float(payload[key])))
            except (TypeError, ValueError):
                return None
    return None


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
    if "cv_is_occupied" not in normalized.columns and "is_occupied" in normalized.columns:
        normalized = normalized.rename(columns={"is_occupied": "cv_is_occupied"})
    for col in CSV_HEADERS:
        if col not in normalized.columns:
            normalized[col] = "" if col in {"first_message_time", "last_message_time", "source_topic"} else 0
    normalized = normalized[CSV_HEADERS].copy()
    normalized["timestamp"] = pd.to_datetime(normalized["timestamp"], errors="coerce").dt.floor(
        SAMPLE_INTERVAL_PANDAS_FREQ
    )
    normalized = normalized.dropna(subset=["timestamp"])
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
    normalized["cv_is_occupied"] = pd.to_numeric(normalized["cv_is_occupied"], errors="coerce").fillna(0).round().astype(int)
    normalized = normalized.sort_values("timestamp").drop_duplicates("timestamp", keep="last")
    return normalized.reset_index(drop=True)


def main() -> None:
    args = parse_args()

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
    print(f"Aggregation: {args.aggregate}; occupied threshold: {args.occupied_threshold}")

    client.connect(args.mqtt_broker, args.mqtt_port, keepalive=60)
    client.loop_start()

    try:
        while True:
            time.sleep(args.flush_check_seconds)
            aggregator.flush_due(force=False)
    except KeyboardInterrupt:
        print("Stopping CV occupancy aggregator...")
    finally:
        aggregator.flush_due(force=True)
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
