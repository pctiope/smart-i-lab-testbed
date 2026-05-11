from __future__ import annotations

import argparse
import calendar
import csv
import json
import os
import sys
import time
import urllib.parse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from air1_all_zones import csv_size_guard
from air1_all_zones.feature_contract import (
    ALL_ZONE_IDS,
    DEVICE_TO_ZONE_ID,
    SAMPLE_INTERVAL,
    SAMPLE_INTERVAL_SECONDS,
    SENSOR_ORDER,
    floor_datetime_to_sample,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

API_URL = os.environ.get("AIR1_API_URL", "http://10.158.66.30:80")
API_KEY = os.environ.get("AIR1_API_KEY", "")

SCRIPT_DIR = Path(__file__).resolve().parent
PACKAGE_ROOT = SCRIPT_DIR.parent
DEFAULT_OUTPUT_DIR = PACKAGE_ROOT / "data" / "training_csv"
LOCAL_OFFSET = timedelta(hours=8)
LOCAL_TZ = timezone(LOCAL_OFFSET)
API_TIMEOUT_SECONDS = 30
API_RETRIES = 1
API_RETRY_BASE_DELAY_SECONDS = 1.0
DEFAULT_CHUNK_DAYS = 5.0
DEFAULT_MIN_CHUNK_HOURS = 3.0
MAX_PARALLEL_API_REQUESTS = 16
DEFAULT_PROGRESS_EVERY = 25
TRANSIENT_HTTP_STATUS_CODES = {408, 429}


def format_api_timestamp(dt: datetime) -> str:
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def parse_cli_datetime_utc(value: str) -> datetime:
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            f"Invalid datetime '{value}'. Use ISO format such as 2026-03-28T03:00:00Z."
        ) from error
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"Expected a number, got '{value}'.") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError(f"Expected a value greater than 0, got {value}.")
    return parsed


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"Expected an integer, got '{value}'.") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError(f"Expected an integer greater than 0, got {value}.")
    return parsed


def nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"Expected an integer, got '{value}'.") from error
    if parsed < 0:
        raise argparse.ArgumentTypeError(f"Expected an integer greater than or equal to 0, got {value}.")
    return parsed


def add_one_calendar_month(dt: datetime) -> datetime:
    month = dt.month + 1
    year = dt.year
    if month > 12:
        month = 1
        year += 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def split_time_range_into_chunks(time_start: datetime, time_end: datetime, chunk_days: float = DEFAULT_CHUNK_DAYS):
    if time_end <= time_start:
        return []
    if chunk_days <= 0:
        raise ValueError("chunk_days must be greater than 0")
    chunk_delta = timedelta(days=chunk_days)
    chunks = []
    current_start = time_start
    while current_start < time_end:
        current_end = min(current_start + chunk_delta, time_end)
        chunks.append((current_start, current_end))
        current_start = current_end
    return chunks


def parse_timestamp_to_local(timestamp_value: Any) -> datetime | None:
    if timestamp_value is None:
        return None
    if isinstance(timestamp_value, datetime):
        dt = timestamp_value
    else:
        timestamp_text = str(timestamp_value).strip()
        if not timestamp_text:
            return None
        timestamp_text = timestamp_text.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(timestamp_text)
        except ValueError:
            try:
                dt = datetime.strptime(timestamp_text, "%Y-%m-%dT%H:%M:%S.%f")
            except ValueError:
                dt = datetime.strptime(timestamp_text, "%Y-%m-%dT%H:%M:%S")
    if dt.tzinfo is None:
        return dt + LOCAL_OFFSET
    return dt.astimezone(LOCAL_TZ).replace(tzinfo=None)


def sample_floor(dt: datetime) -> datetime:
    return floor_datetime_to_sample(dt)


def minute_floor(dt: datetime) -> datetime:
    return sample_floor(dt)


def build_local_minute_index(time_start: datetime, time_end: datetime) -> list[datetime]:
    start_local = sample_floor(parse_timestamp_to_local(time_start))
    end_local = parse_timestamp_to_local(time_end)
    if start_local is None or end_local is None:
        return []
    minutes = []
    current = start_local
    while current < end_local:
        minutes.append(current)
        current += SAMPLE_INTERVAL
    return minutes


def normalize_records(payload: Any) -> list[Any]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "readings", "history", "results", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        return [payload]
    return []


def get_nested_value(record: Any, key: str) -> Any:
    if not isinstance(record, dict):
        return None
    if key in record:
        return record[key]
    current = record
    for part in key.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def first_present_value(record: Any, keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = get_nested_value(record, key)
        if value is not None:
            return value
    return None


def get_reading_timestamp(record: Any) -> Any:
    return first_present_value(record, ("timestamp", "time", "created_at", "updated_at"))


def to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return float(int(value))
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def average(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def requested_local_bounds(time_start: datetime | None, time_end: datetime | None):
    if time_start is None or time_end is None:
        return None, None
    return parse_timestamp_to_local(time_start), parse_timestamp_to_local(time_end)


def within_local_bounds(dt_local: datetime, start_local: datetime | None, end_local: datetime | None) -> bool:
    if start_local is None or end_local is None:
        return True
    return start_local <= dt_local < end_local


def aggregate_air1_by_minute(readings_by_device: dict[str, Any], time_start=None, time_end=None):
    start_local, end_local = requested_local_bounds(time_start, time_end)
    value_buckets = defaultdict(lambda: {
        "temps": defaultdict(list),
        "rhs": defaultdict(list),
        "co2s": defaultdict(list),
        "pm25s": defaultdict(list),
    })

    metric_keys = (
        ("temps", ("temperature", "temp")),
        ("rhs", ("humidity", "rh")),
        ("co2s", ("co2",)),
        ("pm25s", ("pm_2_5", "pm25", "pm2_5")),
    )

    for device_id, payload in readings_by_device.items():
        zone_id = DEVICE_TO_ZONE_ID.get(device_id)
        if zone_id is None:
            continue
        for reading in normalize_records(payload):
            if not isinstance(reading, dict):
                continue
            dt_local = parse_timestamp_to_local(get_reading_timestamp(reading))
            if dt_local is None or not within_local_bounds(dt_local, start_local, end_local):
                continue
            minute_key = minute_floor(dt_local)
            for bucket_name, candidate_keys in metric_keys:
                numeric_value = to_float(first_present_value(reading, candidate_keys))
                if numeric_value is not None:
                    value_buckets[minute_key][bucket_name][zone_id].append(numeric_value)

    averaged = defaultdict(lambda: {"temps": {}, "rhs": {}, "co2s": {}, "pm25s": {}})
    for minute_key, metric_buckets in value_buckets.items():
        for metric_name, position_values in metric_buckets.items():
            for zone_id, values in position_values.items():
                averaged[minute_key][metric_name][zone_id] = average(values)
    return averaged


def payload_item_count(payload: Any) -> int:
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict):
        return len(normalize_records(payload))
    return 0


def value_or_blank(value: Any) -> Any:
    return "" if value is None else value


def validate_csv_width(filename: str | Path, expected_column_count: int) -> None:
    bad_rows = []
    with open(filename, "r", newline="", encoding="utf-8") as csvfile:
        reader = csv.reader(csvfile)
        try:
            header = next(reader)
        except StopIteration:
            return
        if len(header) != expected_column_count:
            bad_rows.append((1, len(header)))
        for row in reader:
            if len(row) != expected_column_count:
                bad_rows.append((reader.line_num, len(row)))
    if bad_rows:
        preview = ", ".join(f"line {line}: {width} fields" for line, width in bad_rows[:5])
        raise ValueError(f"CSV width check failed for {filename}; expected {expected_column_count} columns ({preview}).")


def format_elapsed(seconds: float) -> str:
    return f"{seconds:.2f}s"


def _split_failed_chunk(start: datetime, end: datetime) -> tuple[tuple[datetime, datetime], tuple[datetime, datetime]]:
    midpoint = start + (end - start) / 2
    return (start, midpoint), (midpoint, end)


class Air1Device:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        api_timeout: float = API_TIMEOUT_SECONDS,
        api_retries: int = API_RETRIES,
        max_workers: int = MAX_PARALLEL_API_REQUESTS,
        chunk_days: float = DEFAULT_CHUNK_DAYS,
        min_chunk_hours: float = DEFAULT_MIN_CHUNK_HOURS,
        progress_every: int = DEFAULT_PROGRESS_EVERY,
        verbose_progress: bool = False,
        timing_summary: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.api_timeout = float(api_timeout)
        self.api_retries = int(api_retries)
        self.max_workers = int(max_workers)
        self.chunk_days = float(chunk_days)
        self.min_chunk_hours = float(min_chunk_hours)
        self.progress_every = int(progress_every)
        self.verbose_progress = bool(verbose_progress)
        self.timing_summary = bool(timing_summary)
        self.last_fetch_metrics: dict[str, Any] | None = None

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
            headers["x-api-key"] = self.api_key
        return headers

    def _get_json(self, path: str, context: str) -> Any:
        payload, _attempts, failure, _retryable = self._get_json_with_retry_details(path, context)
        if failure is not None:
            raise RuntimeError(f"{context} failed: {failure}")
        return payload

    def _get_json_with_retry_details(self, path: str, context: str):
        url = f"{self.base_url}{path}"
        attempts = 0
        last_failure = None
        retryable_failure = False
        for attempt in range(self.api_retries + 1):
            attempts = attempt + 1
            try:
                response = requests.get(url, headers=self._headers(), timeout=self.api_timeout)
                if response.status_code in TRANSIENT_HTTP_STATUS_CODES:
                    last_failure = f"HTTP {response.status_code}"
                    retryable_failure = True
                else:
                    response.raise_for_status()
                    return response.json(), attempts, None, False
            except requests.RequestException as exc:
                last_failure = f"{type(exc).__name__}: {exc}"
                retryable_failure = True
            if attempt < self.api_retries:
                time.sleep(API_RETRY_BASE_DELAY_SECONDS * (attempt + 1))
        return None, attempts, last_failure or "unknown error", retryable_failure

    def _get_historical_data_with_retries(
        self,
        endpoint: str,
        device_id: str,
        time_start: datetime,
        time_end: datetime,
        error_context: str,
    ):
        start_encoded = urllib.parse.quote(format_api_timestamp(time_start))
        end_encoded = urllib.parse.quote(format_api_timestamp(time_end))
        path = f"/{endpoint}/{device_id}?time_start={start_encoded}&time_end={end_encoded}"
        return self._get_json_with_retry_details(path, error_context)

    def get_all_devices(self):
        return self._get_json("/air-1", "AIR-1 device list request") or []

    def get_device_data(self, device_id: str):
        return self._get_json(f"/air-1/{device_id}", f"AIR-1 device {device_id} request")

    def get_historical_data(self, device_id: str, time_start: datetime, time_end: datetime):
        start_encoded = urllib.parse.quote(format_api_timestamp(time_start))
        end_encoded = urllib.parse.quote(format_api_timestamp(time_end))
        return self._get_json(
            f"/air-1/{device_id}?time_start={start_encoded}&time_end={end_encoded}",
            f"Historical AIR-1 request for device {device_id}",
        )

    def _fetch_one_history_job(self, job: dict[str, Any]) -> dict[str, Any]:
        fetch_start = job["fetch_start"]
        fetch_end = job["fetch_end"]
        context = (
            f"{job['source_name']} {job['device_id']} "
            f"{format_api_timestamp(fetch_start)}..{format_api_timestamp(fetch_end)}"
        )
        payload, attempts, failure, retryable_failure = self._get_historical_data_with_retries(
            job["endpoint"],
            job["device_id"],
            fetch_start,
            fetch_end,
            context,
        )
        return {
            **job,
            "payload": payload,
            "attempts": attempts,
            "failure": failure,
            "retryable_failure": retryable_failure,
            "request_elapsed_seconds": 0.0,
            "record_count": payload_item_count(payload),
        }

    def _fetch_adaptive_historical_sources(self, source_specs: list[dict[str, Any]], time_start, time_end):
        fetch_started_at = time.perf_counter()
        histories_by_source = {spec["source_key"]: {} for spec in source_specs}
        failures: list[dict[str, Any]] = []
        initial_jobs = []
        for source_spec in source_specs:
            chunks = split_time_range_into_chunks(time_start, time_end, self.chunk_days)
            for device_id in source_spec.get("device_ids", []):
                for chunk_idx, (chunk_start, chunk_end) in enumerate(chunks, start=1):
                    initial_jobs.append(
                        {
                            "source_key": source_spec["source_key"],
                            "source_name": source_spec["source_name"],
                            "endpoint": source_spec["endpoint"],
                            "device_id": device_id,
                            "device_description": source_spec["describe_device"](device_id),
                            "fetch_start": chunk_start,
                            "fetch_end": chunk_end,
                            "initial_chunk_number": chunk_idx,
                            "initial_chunk_count": len(chunks),
                        }
                    )

        completed = 0
        record_count = 0
        with ThreadPoolExecutor(max_workers=max(1, min(self.max_workers, len(initial_jobs) or 1))) as executor:
            futures = [executor.submit(self._fetch_one_history_job, job) for job in initial_jobs]
            for future in as_completed(futures):
                result = future.result()
                completed += 1
                if result["failure"] is not None:
                    failures.append({key: value for key, value in result.items() if key != "payload"})
                    continue
                source_key = result["source_key"]
                device_id = result["device_id"]
                histories_by_source.setdefault(source_key, {}).setdefault(device_id, [])
                histories_by_source[source_key][device_id].extend(normalize_records(result["payload"]))
                record_count += int(result["record_count"])
                if self.verbose_progress or (
                    self.progress_every > 0 and completed % self.progress_every == 0
                ):
                    print(f"Completed {completed}/{len(initial_jobs)} AIR-1 history request(s)")

        self.last_fetch_metrics = {
            "elapsed_seconds": time.perf_counter() - fetch_started_at,
            "initial_requests": len(initial_jobs),
            "submitted_requests": len(initial_jobs),
            "completed_requests": completed,
            "terminal_chunks": completed,
            "successful_chunks": completed - len(failures),
            "failed_chunks": len(failures),
            "split_count": 0,
            "retry_count": 0,
            "record_count": record_count,
            "source_names": {spec["source_key"]: spec["source_name"] for spec in source_specs},
        }
        return histories_by_source, failures

    def _print_failed_historical_request_summary(self, failures: list[dict[str, Any]]) -> None:
        if not failures:
            return
        print("\nFAILED AIR-1 HISTORY REQUESTS:")
        print("-" * 50)
        for failure in failures[:20]:
            print(
                f"  {failure.get('source_name')} {failure.get('device_description')}: "
                f"{failure.get('failure')}"
            )


def all_zones_source_devices(devices: list[str] | tuple[str, ...] | set[str]) -> dict[str, list[str]]:
    available_devices = set(devices)
    return {
        "air1": [device_id for device_id in SENSOR_ORDER if device_id in available_devices],
    }


def build_all_zones_source_specs(source_devices: dict[str, list[str]]) -> list[dict[str, Any]]:
    return [
        {
            "source_key": "air1",
            "source_name": "AIR-1",
            "endpoint": "air-1",
            "device_ids": source_devices["air1"],
            "describe_device": lambda device_id: (
                f"device {device_id} (zone_id {DEVICE_TO_ZONE_ID[device_id]})"
            ),
        }
    ]


def check_expected_air1_sensors(air1: Air1Device, devices: list[str], max_workers: int):
    available_devices = set(devices)
    sensors_to_check = [expected for expected in SENSOR_ORDER if expected in available_devices]
    futures_by_sensor = {}
    if sensors_to_check:
        worker_count = min(max_workers, len(sensors_to_check))
        print(f"Checking {len(sensors_to_check)} expected AIR-1 sensors with {worker_count} parallel workers")
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures_by_sensor = {
                expected_sensor: executor.submit(air1.get_device_data, expected_sensor)
                for expected_sensor in sensors_to_check
            }

    working_devices = []
    missing_devices = []
    for expected_sensor in SENSOR_ORDER:
        if expected_sensor not in available_devices:
            print(f"Sensor {expected_sensor} not found on network")
            missing_devices.append(expected_sensor)
            continue
        try:
            data = futures_by_sensor[expected_sensor].result()
        except Exception as error:
            print(f"Sensor {expected_sensor} found but check failed: {error}")
            missing_devices.append(expected_sensor)
            continue
        if data and "timestamp" in data:
            working_devices.append(expected_sensor)
            print(f"Sensor {expected_sensor} is active and has data")
        else:
            print(f"Sensor {expected_sensor} found but has no data yet")
            missing_devices.append(expected_sensor)
    return working_devices, missing_devices


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export AIR-1 history for all 15 zones to a long-form CSV.")
    parser.add_argument(
        "--time-start",
        type=parse_cli_datetime_utc,
        default=parse_cli_datetime_utc("2026-03-28T03:00:00Z"),
    )
    parser.add_argument("--time-end", type=parse_cli_datetime_utc, default=None)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--chunk-days", type=positive_float, default=DEFAULT_CHUNK_DAYS)
    parser.add_argument("--min-chunk-hours", type=positive_float, default=DEFAULT_MIN_CHUNK_HOURS)
    parser.add_argument("--api-timeout", type=positive_float, default=API_TIMEOUT_SECONDS)
    parser.add_argument("--api-retries", type=nonnegative_int, default=API_RETRIES)
    parser.add_argument("--max-workers", type=positive_int, default=MAX_PARALLEL_API_REQUESTS)
    parser.add_argument("--progress-every", type=positive_int, default=DEFAULT_PROGRESS_EVERY)
    parser.add_argument("--verbose-progress", action="store_true")
    timing_group = parser.add_mutually_exclusive_group()
    timing_group.add_argument("--timing-summary", dest="timing_summary", action="store_true", default=True)
    timing_group.add_argument("--no-timing-summary", dest="timing_summary", action="store_false")
    return parser.parse_args()


def main() -> None:
    from air1_all_zones import feature_builder

    args = parse_args()
    if args.time_end is None:
        args.time_end = add_one_calendar_month(args.time_start)
    if args.time_end <= args.time_start:
        raise SystemExit("--time-end must be after --time-start")

    client = Air1Device(
        API_URL,
        API_KEY,
        api_timeout=args.api_timeout,
        api_retries=args.api_retries,
        max_workers=args.max_workers,
        chunk_days=args.chunk_days,
        min_chunk_hours=args.min_chunk_hours,
        progress_every=args.progress_every,
        verbose_progress=args.verbose_progress,
        timing_summary=args.timing_summary,
    )
    devices = client.get_all_devices()
    working_devices, _missing = check_expected_air1_sensors(client, devices, args.max_workers)
    source_devices = all_zones_source_devices(working_devices)
    minute_index = build_local_minute_index(args.time_start, args.time_end)
    histories, failures = client._fetch_adaptive_historical_sources(
        build_all_zones_source_specs(source_devices),
        args.time_start,
        args.time_end,
    )
    air_by_minute = aggregate_air1_by_minute(histories.get("air1", {}), args.time_start, args.time_end)
    frame = feature_builder.build_all_zones_base_feature_frame(minute_index, air_by_minute)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    start_str = args.time_start.strftime("%Y%m%d_%H%M%S")
    end_str = args.time_end.strftime("%Y%m%d_%H%M%S")
    output_csv = output_dir / f"air1_all_zones_{start_str}_to_{end_str}.csv"
    csv_size_guard.write_dataframe_rolling_atomic(frame, output_csv)
    for csv_part in csv_size_guard.existing_csv_parts(output_csv) or [output_csv]:
        validate_csv_width(csv_part, expected_column_count=len(frame.columns))
    client._print_failed_historical_request_summary(failures)
    print(json.dumps({"csv_path": str(output_csv.resolve()), "rows": int(len(frame))}, indent=2))


if __name__ == "__main__":
    main()
