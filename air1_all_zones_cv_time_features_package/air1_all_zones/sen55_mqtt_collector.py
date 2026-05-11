from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from air1_all_zones import csv_size_guard
from air1_all_zones.feature_contract import SAMPLE_INTERVAL_PANDAS_FREQ


PACKAGE_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PACKAGE_ROOT / "data"

DEFAULT_BROKER = os.getenv("SEN55_MQTT_BROKER", "10.158.71.19")
DEFAULT_PORT = int(os.getenv("SEN55_MQTT_PORT", "1883"))
DEFAULT_TOPIC = os.getenv("SEN55_MQTT_TOPIC", "sen55_01/data")
DEFAULT_USERNAME = os.getenv("SEN55_MQTT_USERNAME", "guest")
DEFAULT_PASSWORD = os.getenv("SEN55_MQTT_PASSWORD", "smartilab123")
DEFAULT_CLIENT_ID = os.getenv("SEN55_MQTT_CLIENT_ID", "sen55_table_subscriber")
DEFAULT_OUTPUT_CSV = DATA_DIR / "sen55_data.csv"
DEFAULT_OUTPUT_PARQUET = DATA_DIR / "sen55_data.parquet"

CSV_HEADERS = [
    "timestamp",
    "sensor_id",
    "location",
    "room",
    "pm1_0",
    "pm2_5",
    "pm4_0",
    "pm10_0",
    "temperature",
    "humidity",
    "voc",
    "nox",
]
METADATA_FIELDS = ["sensor_id", "location", "room"]
NUMERIC_FIELDS = ["pm1_0", "pm2_5", "pm4_0", "pm10_0", "temperature", "humidity", "voc", "nox"]


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


def init_csv(path: Path) -> None:
    csv_size_guard.write_header_if_missing(path, CSV_HEADERS)


def value_or_blank(value: Any) -> Any:
    return "" if value is None else value


def row_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {col: value_or_blank(payload.get(col)) for col in CSV_HEADERS}


def dedupe_key(row: dict[str, Any]) -> tuple[Any, ...]:
    timestamp = str(row.get("timestamp", "")).strip()
    sensor_id = str(row.get("sensor_id", "")).strip()
    if timestamp:
        return (timestamp, sensor_id)
    return tuple(row.get(col, "") for col in CSV_HEADERS)


def read_existing_keys(path: Path) -> set[tuple[Any, ...]]:
    if not csv_size_guard.has_csv_data(path):
        return set()
    try:
        return {dedupe_key(row) for row in csv_size_guard.iter_dict_rows(path)}
    except OSError:
        return set()


def append_sen55_row(path: Path, payload: dict[str, Any]) -> bool:
    buffer = _APPEND_BUFFERS.get(Path(path))
    if buffer is None:
        buffer = Sen55BucketBuffer(Path(path))
        _APPEND_BUFFERS[Path(path)] = buffer
    result = buffer.add_payload(payload)
    return bool(result["accepted"])


def _coerce_bucket_timestamp(value: Any) -> pd.Timestamp:
    timestamp = pd.to_datetime(value, errors="coerce")
    if pd.isna(timestamp):
        raise ValueError(f"SEN55 payload timestamp is missing or invalid: {value!r}")
    timestamp = pd.Timestamp(timestamp)
    if timestamp.tzinfo is not None:
        timestamp = timestamp.tz_convert(None)
    return timestamp.floor(SAMPLE_INTERVAL_PANDAS_FREQ)


def _format_bucket_timestamp(timestamp: pd.Timestamp) -> str:
    return pd.Timestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(numeric):
        return None
    return numeric


def _latest_committed_bucket_from_csv(path: Path) -> pd.Timestamp | None:
    if not csv_size_guard.has_csv_data(path):
        return None
    try:
        frame = csv_size_guard.read_csv_parts(path, usecols=["timestamp"])
    except (OSError, ValueError, pd.errors.EmptyDataError):
        return None
    if frame.empty or "timestamp" not in frame.columns:
        return None
    timestamps = pd.to_datetime(frame["timestamp"], errors="coerce").dropna()
    if timestamps.empty:
        return None
    return pd.Timestamp(timestamps.max()).floor(SAMPLE_INTERVAL_PANDAS_FREQ)


@dataclass
class Sen55Bucket:
    timestamp: pd.Timestamp
    numeric_values: dict[str, list[float]] = field(
        default_factory=lambda: {field_name: [] for field_name in NUMERIC_FIELDS}
    )
    metadata: dict[str, str] = field(default_factory=lambda: {field_name: "" for field_name in METADATA_FIELDS})
    payload_count: int = 0

    def add_payload(self, payload: dict[str, Any]) -> None:
        row = row_from_payload(payload)
        self.payload_count += 1
        for field_name in METADATA_FIELDS:
            text = str(row.get(field_name, "")).strip()
            if text:
                self.metadata[field_name] = text
        for field_name in NUMERIC_FIELDS:
            numeric = _coerce_float(row.get(field_name))
            if numeric is not None:
                self.numeric_values[field_name].append(numeric)

    def to_row(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "timestamp": _format_bucket_timestamp(self.timestamp),
            **self.metadata,
        }
        for field_name in NUMERIC_FIELDS:
            values = self.numeric_values[field_name]
            row[field_name] = (sum(values) / len(values)) if values else ""
        return {field_name: row.get(field_name, "") for field_name in CSV_HEADERS}


class Sen55BucketBuffer:
    def __init__(self, csv_path: Path) -> None:
        self.csv_path = Path(csv_path)
        init_csv(self.csv_path)
        self._buckets: dict[pd.Timestamp, Sen55Bucket] = {}
        self.last_committed_bucket = _latest_committed_bucket_from_csv(self.csv_path)
        self.skipped_late_payloads = 0
        self.flushed_buckets = 0

    def add_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        bucket_ts = _coerce_bucket_timestamp(payload.get("timestamp"))
        flushed_rows = self.flush_completed(reference_time=bucket_ts)
        if self.last_committed_bucket is not None and bucket_ts <= self.last_committed_bucket:
            self.skipped_late_payloads += 1
            return {
                "accepted": False,
                "bucket_timestamp": _format_bucket_timestamp(bucket_ts),
                "flushed_rows": flushed_rows,
                "reason": "bucket already flushed",
                "skipped_late_payloads": self.skipped_late_payloads,
            }
        bucket = self._buckets.setdefault(bucket_ts, Sen55Bucket(bucket_ts))
        bucket.add_payload(payload)
        return {
            "accepted": True,
            "bucket_timestamp": _format_bucket_timestamp(bucket_ts),
            "flushed_rows": flushed_rows,
            "buffered_buckets": len(self._buckets),
            "skipped_late_payloads": self.skipped_late_payloads,
        }

    def flush_completed(self, reference_time: Any | None = None, *, force: bool = False) -> list[dict[str, Any]]:
        if not self._buckets:
            return []
        if force:
            keys = sorted(self._buckets)
        else:
            reference_bucket = _coerce_bucket_timestamp(reference_time if reference_time is not None else pd.Timestamp.now())
            keys = sorted(bucket_ts for bucket_ts in self._buckets if bucket_ts < reference_bucket)
        if not keys:
            return []
        return self._flush_keys(keys)

    def _flush_keys(self, keys: list[pd.Timestamp]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for bucket_ts in keys:
            bucket = self._buckets.pop(bucket_ts, None)
            if bucket is None:
                continue
            if self.last_committed_bucket is not None and bucket_ts <= self.last_committed_bucket:
                self.skipped_late_payloads += bucket.payload_count
                continue
            rows.append(bucket.to_row())
            self.last_committed_bucket = bucket_ts
        if rows:
            csv_size_guard.append_rows_rolling(self.csv_path, CSV_HEADERS, rows)
            self.flushed_buckets += len(rows)
        return rows


_APPEND_BUFFERS: dict[Path, Sen55BucketBuffer] = {}


def _latest_non_empty(values: pd.Series) -> str:
    non_empty = values.dropna().astype(str).map(str.strip)
    non_empty = non_empty[non_empty != ""]
    if non_empty.empty:
        return ""
    return str(non_empty.iloc[-1])


def normalize_sen55_frame(frame: pd.DataFrame) -> pd.DataFrame:
    normalized = frame.copy()
    for col in CSV_HEADERS:
        if col not in normalized.columns:
            normalized[col] = ""
    normalized = normalized[CSV_HEADERS].copy()
    normalized["sample_timestamp"] = pd.to_datetime(normalized["timestamp"], errors="coerce")
    normalized = normalized.dropna(subset=["sample_timestamp"])
    normalized["sensor_id"] = normalized["sensor_id"].fillna("").astype(str)
    normalized = normalized.sort_values("sample_timestamp")
    normalized = normalized.drop_duplicates(subset=["sample_timestamp", "sensor_id"], keep="last")
    normalized["timestamp"] = normalized["sample_timestamp"].dt.floor(SAMPLE_INTERVAL_PANDAS_FREQ)
    for col in ["pm1_0", "pm2_5", "pm4_0", "pm10_0", "temperature", "humidity", "voc", "nox"]:
        normalized[col] = pd.to_numeric(normalized[col], errors="coerce")
    for col in ["sensor_id", "location", "room"]:
        normalized[col] = normalized[col].fillna("").astype(str)
    if normalized.empty:
        return pd.DataFrame(columns=CSV_HEADERS)
    aggregated = (
        normalized.groupby("timestamp", as_index=False)
        .agg(
            sensor_id=("sensor_id", _latest_non_empty),
            location=("location", _latest_non_empty),
            room=("room", _latest_non_empty),
            pm1_0=("pm1_0", "mean"),
            pm2_5=("pm2_5", "mean"),
            pm4_0=("pm4_0", "mean"),
            pm10_0=("pm10_0", "mean"),
            temperature=("temperature", "mean"),
            humidity=("humidity", "mean"),
            voc=("voc", "mean"),
            nox=("nox", "mean"),
        )
        .sort_values("timestamp")
        .drop_duplicates("timestamp", keep="last")
        .reset_index(drop=True)
    )
    aggregated["timestamp"] = pd.to_datetime(aggregated["timestamp"], errors="coerce").dt.strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    return aggregated[CSV_HEADERS]


def rewrite_parquet_from_csv(csv_path: Path, parquet_path: Path | None) -> int:
    if parquet_path is None:
        return 0
    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    if not csv_size_guard.has_csv_data(csv_path):
        frame = pd.DataFrame(columns=CSV_HEADERS)
    else:
        frame = normalize_sen55_frame(csv_size_guard.read_csv_parts(csv_path))
    fd, tmp_name = tempfile.mkstemp(
        prefix=parquet_path.stem + ".",
        suffix=".tmp.parquet",
        dir=str(parquet_path.parent),
    )
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        frame.to_parquet(tmp_path, index=False)
        os.replace(tmp_path, parquet_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
    return int(len(frame))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Subscribe to SEN55 MQTT messages and write package-local CSV and Parquet tables."
    )
    parser.add_argument("--mqtt-broker", default=DEFAULT_BROKER, help="MQTT broker host.")
    parser.add_argument("--mqtt-port", type=int, default=DEFAULT_PORT, help="MQTT broker port.")
    parser.add_argument("--mqtt-topic", default=DEFAULT_TOPIC, help="SEN55 MQTT topic.")
    parser.add_argument("--mqtt-username", default=DEFAULT_USERNAME, help="Optional MQTT username.")
    parser.add_argument("--mqtt-password", default=DEFAULT_PASSWORD, help="Optional MQTT password.")
    parser.add_argument("--mqtt-client-id", default=DEFAULT_CLIENT_ID, help="MQTT client ID.")
    parser.add_argument("--output-csv", type=Path, default=DEFAULT_OUTPUT_CSV, help="Output SEN55 CSV path.")
    parser.add_argument(
        "--output-parquet",
        default=str(DEFAULT_OUTPUT_PARQUET),
        help="Output normalized SEN55 Parquet path. Pass an empty string to disable.",
    )
    parser.add_argument(
        "--parquet-rebuild-every-hours",
        type=float,
        default=1.0,
        help="Rebuild the Parquet snapshot from the rolling CSV this often. Default: 1.",
    )
    parser.add_argument(
        "--parquet-check-seconds",
        type=float,
        default=1.0,
        help="How often the main loop checks whether the SEN55 Parquet snapshot is due. Default: 1.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_csv = Path(args.output_csv)
    output_parquet = Path(args.output_parquet) if str(args.output_parquet).strip() else None
    bucket_buffer = Sen55BucketBuffer(output_csv)
    last_rebuild_mono = 0.0

    def rebuild_if_due(reason: str = "interval") -> int | None:
        nonlocal last_rebuild_mono
        if output_parquet is None:
            return None
        if not csv_size_guard.has_csv_rows(output_csv):
            return None
        due = last_rebuild_mono <= 0.0 or (
            time.monotonic() - last_rebuild_mono >= float(args.parquet_rebuild_every_hours) * 3600.0
        )
        missing = not output_parquet.is_file()
        if reason == "shutdown" or due or missing:
            rows = rewrite_parquet_from_csv(output_csv, output_parquet)
            last_rebuild_mono = time.monotonic()
            print(f"Rebuilt SEN55 Parquet {output_parquet}: parquet_rows={rows} reason={reason}")
            return rows
        return None

    def flush_buffer_and_maybe_rebuild(reason: str) -> None:
        flushed_rows = bucket_buffer.flush_completed(force=(reason == "shutdown"))
        if flushed_rows:
            print(
                f"Flushed SEN55 buckets to {output_csv}: rows={len(flushed_rows)} "
                f"latest={flushed_rows[-1]['timestamp']} reason={reason}"
            )
        rebuild_if_due(reason=reason)

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
        try:
            payload = json.loads(msg.payload.decode(errors="replace"))
            if not isinstance(payload, dict):
                raise ValueError("SEN55 payload is not a JSON object")
            result = bucket_buffer.add_payload(payload)
            parquet_rows = rebuild_if_due(reason="interval_or_missing")
            suffix = f"; parquet_rows={parquet_rows}" if parquet_rows is not None else ""
            if result["accepted"]:
                flushed = result.get("flushed_rows") or []
                print(
                    f"Buffered SEN55 payload for bucket {result['bucket_timestamp']}: "
                    f"open_buckets={result.get('buffered_buckets', 0)} flushed={len(flushed)}{suffix}"
                )
            else:
                print(
                    f"Skipped late SEN55 payload for bucket {result['bucket_timestamp']}: "
                    f"{result['reason']} skipped_late_payloads={result['skipped_late_payloads']}{suffix}"
                )
        except Exception as exc:
            print(f"Skipping SEN55 message on {msg.topic}: {type(exc).__name__}: {exc}")

    def on_disconnect(mqtt, userdata, rc):
        if rc != 0:
            print(f"MQTT disconnected unexpectedly with rc={rc}")

    client.on_connect = on_connect
    client.on_message = on_message
    client.on_disconnect = on_disconnect
    client.reconnect_delay_set(min_delay=1, max_delay=60)

    print(f"Writing SEN55 CSV to {output_csv.resolve()}")
    if output_parquet is not None:
        print(f"Writing SEN55 Parquet to {output_parquet.resolve()}")
        rebuild_if_due(reason="missing")
    client.connect(args.mqtt_broker, args.mqtt_port, keepalive=60)
    client.loop_start()
    try:
        while True:
            time.sleep(float(args.parquet_check_seconds))
            flush_buffer_and_maybe_rebuild(reason="interval")
    except KeyboardInterrupt:
        print("Stopping SEN55 MQTT collector...")
    finally:
        flush_buffer_and_maybe_rebuild(reason="shutdown")
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()


