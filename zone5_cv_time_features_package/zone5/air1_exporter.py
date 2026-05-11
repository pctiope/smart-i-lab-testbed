import requests
import json
import argparse
import calendar
from datetime import datetime, timedelta, timezone
import urllib.parse
import csv
import os
import sys
import time
import threading
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

from zone5 import csv_size_guard
from zone5.feature_contract import SAMPLE_INTERVAL, SAMPLE_INTERVAL_SECONDS, ZONE_NUM, floor_datetime_to_sample

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# API configuration. Set AIR1_API_KEY in the environment for live/API exports.
API_URL = os.environ.get("AIR1_API_URL", "http://10.158.66.30:80")
API_KEY = os.environ.get("AIR1_API_KEY", "")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PACKAGE_ROOT = os.path.dirname(SCRIPT_DIR)
DEFAULT_OUTPUT_DIR = os.path.join(PACKAGE_ROOT, "data", "training_csv")
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
MMWAVE_MAX_STALE_MINUTES = int(os.environ.get("MMWAVE_MAX_STALE_MINUTES", "5"))
MMWAVE_MAX_STALE_GAP = timedelta(minutes=MMWAVE_MAX_STALE_MINUTES)
ZONES = (ZONE_NUM,)

# AIR-1 sensor order
SENSOR_ORDER = [
    "88e4c8", "88e590", "89e8d8", "889720", "87f510",
    "2da640", "89ea14", "889b88", "889938", "88e85c",
    "89e548", "88970c", "2deb24", "89e5f0", "cc8f24"
]

# Map AIR-1 device id to sensor position.
DEVICE_TO_POSITION = {device_id: idx + 1 for idx, device_id in enumerate(SENSOR_ORDER)}

# Reference-derived smart plug entity mapping. Lab area 16 is intentionally omitted.
SMART_PLUG_ENTITY_TO_ZONE = {
    "smart_plug_v2_9d86e0_power": 1,
    "smart_plug_v2_9d86aa_power": 1,
    "smart_plug_v2_9d9572_power": 2,
    "smart_plug_v2_9d93d2_power": 2,
    "smart_plug_v2_9d923d_power": 3,
    "smart_plug_v2_9d8665_power": 3,
    "smart_plug_v2_9d929b_power": 4,
    "smart_plug_v2_9d9293_power": 4,
    "smart_plug_v2_9d88e7_power": 5,
    "smart_plug_v2_9d929e_power": 6,
    "smart_plug_v2_9d9421_power": 7,
    "athom_smart_plug_v2_9d3535_power": 8,
    "smart_plug_v2_9d92a3_power": 9,
    "smart_plug_v2_9d8718_power": 10,
    "smart_plug_v2_9d9265_power": 11,
    "smart_plug_v2_9d90b9_power": 12,
    "smart_plug_v2_9d97ec_power": 13,
    "smart_plug_v2_9d8671_power": 13,
    "smart_plug_v2_9d927c_power": 14,
    "smart_plug_v2_9d356f_power": 14,
    "smart_plug_v2_9d88c5_power": 15,
    "smart_plug_v2_9d887f_power": 15,
}

# Reference-derived MSR-2 device-to-lab-zone mapping. Lab area 16 is intentionally
# omitted. Each MSR-2 device is treated as occupied when any internal radar
# region 1, 2, or 3 is occupied.
MMWAVE_ENTITY_TO_ZONE = {
    "apollo_msr_2_2b7624_radar_zone_3_occupancy": 1,
    "apollo_msr_2_87a5f4_radar_zone_3_occupancy": 2,
    "apollo_msr_2_c07ce8_radar_zone_3_occupancy": 3,
    "apollo_msr_2_cc0b5c_radar_zone_3_occupancy": 4,
    "apollo_msr_2_89f464_radar_zone_3_occupancy": 5,
    "apollo_msr_2_87a5dc_radar_zone_3_occupancy": 6,
    "apollo_msr_2_1ee998_radar_zone_3_occupancy": 7,
    "apollo_msr_2_87a5ec_radar_zone_3_occupancy": 8,
    "apollo_msr_2_1ef110_radar_zone_3_occupancy": 9,
    "apollo_msr_2_87a298_radar_zone_3_occupancy": 10,
    "apollo_msr_2_89304c_radar_zone_3_occupancy": 11,
    "apollo_msr_2_88edc8_radar_zone_3_occupancy": 12,
    "apollo_msr_2_cd7014_radar_zone_3_occupancy": 13,
    "apollo_msr_2_c660fc_radar_zone_3_occupancy": 14,
    "apollo_msr_2_c8f5b4_radar_target": 15,
}


def smart_plug_entity_to_device_id(entity_id):
    for prefix in ("smart_plug_v2_", "athom_smart_plug_v2_"):
        if entity_id.startswith(prefix) and entity_id.endswith("_power"):
            return entity_id[len(prefix):-len("_power")]
    raise ValueError(f"Unsupported smart plug entity id: {entity_id}")


def mmwave_entity_to_device_id(entity_id):
    prefix = "apollo_msr_2_"
    if not entity_id.startswith(prefix):
        raise ValueError(f"Unsupported mmWave entity id: {entity_id}")

    device_and_field = entity_id[len(prefix):]
    for suffix in (
        "_radar_zone_1_occupancy",
        "_radar_zone_2_occupancy",
        "_radar_zone_3_occupancy",
        "_radar_target",
    ):
        if device_and_field.endswith(suffix):
            return device_and_field[:-len(suffix)]
    raise ValueError(f"Unsupported mmWave entity id: {entity_id}")


def mmwave_entity_to_field(entity_id):
    for radar_zone in (1, 2, 3):
        suffix = f"_radar_zone_{radar_zone}_occupancy"
        if entity_id.endswith(suffix):
            return f"radar_zone_{radar_zone}_occupancy"
    if entity_id.endswith("_radar_target"):
        return "radar_target"
    raise ValueError(f"Unsupported mmWave entity id: {entity_id}")


SMART_PLUG_DEVICE_TO_ZONE = {
    smart_plug_entity_to_device_id(entity_id): zone
    for entity_id, zone in SMART_PLUG_ENTITY_TO_ZONE.items()
}

MMWAVE_DEVICE_TO_ZONE = {
    mmwave_entity_to_device_id(entity_id): zone
    for entity_id, zone in MMWAVE_ENTITY_TO_ZONE.items()
}

MMWAVE_DEVICE_TO_FIELD = {
    mmwave_entity_to_device_id(entity_id): mmwave_entity_to_field(entity_id)
    for entity_id in MMWAVE_ENTITY_TO_ZONE
}
MMWAVE_RADAR_OCCUPANCY_FIELDS = tuple(
    f"radar_zone_{radar_zone}_occupancy"
    for radar_zone in (1, 2, 3)
)
MMWAVE_RADAR_OCCUPANCY_FIELD_CANDIDATES = tuple(
    candidate
    for field_name in MMWAVE_RADAR_OCCUPANCY_FIELDS
    for candidate in (
        field_name,
        f"radar.{field_name}",
        field_name[len("radar_"):],
        f"radar.{field_name[len('radar_'):]}"
    )
)
MMWAVE_RADAR_TARGET_FIELD_CANDIDATES = (
    "radar_target",
    "radar.radar_target",
    "radar.target",
    "target",
    "detection_target",
    "radar.detection_target",
    "radar_still_target",
    "radar.radar_still_target",
    "radar.still_target",
    "still_target",
)
MMWAVE_PRIMARY_OCCUPANCY_FIELD_CANDIDATES = (
    *MMWAVE_RADAR_OCCUPANCY_FIELD_CANDIDATES,
    *MMWAVE_RADAR_TARGET_FIELD_CANDIDATES,
)


def format_api_timestamp(dt):
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def parse_cli_datetime_utc(value):
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


def positive_float(value):
    try:
        parsed = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"Expected a number, got '{value}'.") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError(f"Expected a value greater than 0, got {value}.")
    return parsed


def positive_int(value):
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"Expected an integer, got '{value}'.") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError(f"Expected an integer greater than 0, got {value}.")
    return parsed


def nonnegative_int(value):
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"Expected an integer, got '{value}'.") from error
    if parsed < 0:
        raise argparse.ArgumentTypeError(f"Expected an integer greater than or equal to 0, got {value}.")
    return parsed


def add_one_calendar_month(dt):
    month = dt.month + 1
    year = dt.year
    if month > 12:
        month = 1
        year += 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def split_time_range_into_chunks(time_start, time_end, chunk_days=DEFAULT_CHUNK_DAYS):
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


def parse_timestamp_to_local(timestamp_value):
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


def sample_floor(dt):
    return floor_datetime_to_sample(dt)


def minute_floor(dt):
    return sample_floor(dt)


def build_local_minute_index(time_start, time_end):
    start_local = sample_floor(parse_timestamp_to_local(time_start))
    end_local = parse_timestamp_to_local(time_end)

    minutes = []
    current = start_local
    while current < end_local:
        minutes.append(current)
        current += SAMPLE_INTERVAL
    return minutes


def normalize_records(payload):
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


def get_nested_value(record, key):
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


def first_present_value(record, keys):
    for key in keys:
        value = get_nested_value(record, key)
        if value is not None:
            return value
    return None


def get_reading_timestamp(record):
    return first_present_value(record, ("timestamp", "time", "created_at", "updated_at"))


def to_float(value):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return float(int(value))
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def to_binary_state(value):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("true", "on", "occupied", "detected", "yes"):
            return 1
        if normalized in ("false", "off", "clear", "unoccupied", "none", "no"):
            return 0

    numeric_value = to_float(value)
    if numeric_value is None:
        return None
    return 1 if numeric_value > 0 else 0


def average(values):
    return sum(values) / len(values) if values else None


def requested_local_bounds(time_start, time_end):
    if time_start is None or time_end is None:
        return None, None
    return parse_timestamp_to_local(time_start), parse_timestamp_to_local(time_end)


def within_local_bounds(dt_local, start_local, end_local):
    if start_local is None or end_local is None:
        return True
    return start_local <= dt_local < end_local


def within_requested_range(dt_local, time_start, time_end):
    start_local, end_local = requested_local_bounds(time_start, time_end)
    return within_local_bounds(dt_local, start_local, end_local)


def aggregate_air1_by_minute(readings_by_device, time_start=None, time_end=None):
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
        position = DEVICE_TO_POSITION.get(device_id)
        if position is None:
            continue

        for reading in normalize_records(payload):
            if not isinstance(reading, dict):
                continue

            dt_local = parse_timestamp_to_local(get_reading_timestamp(reading))
            if dt_local is None:
                continue
            if not within_local_bounds(dt_local, start_local, end_local):
                continue

            minute_key = minute_floor(dt_local)
            for bucket_name, candidate_keys in metric_keys:
                numeric_value = to_float(first_present_value(reading, candidate_keys))
                if numeric_value is not None:
                    value_buckets[minute_key][bucket_name][position].append(numeric_value)

    return average_nested_minute_buckets(value_buckets)


def average_nested_minute_buckets(value_buckets):
    averaged = defaultdict(lambda: {
        "temps": {},
        "rhs": {},
        "co2s": {},
        "pm25s": {},
    })

    for minute_key, metric_buckets in value_buckets.items():
        for metric_name, position_values in metric_buckets.items():
            for position, values in position_values.items():
                averaged[minute_key][metric_name][position] = average(values)
    return averaged


def extract_smart_plug_power(reading):
    return to_float(first_present_value(
        reading,
        (
            "power",
            "W.value",
            "w.value",
            "watts",
            "watt",
            "active_power",
            "current_power",
            "power_w",
            "value",
        ),
    ))


def aggregate_smart_plug_by_minute(readings_by_device, time_start=None, time_end=None):
    start_local, end_local = requested_local_bounds(time_start, time_end)
    per_device_minute_values = defaultdict(lambda: defaultdict(list))

    for device_id, payload in readings_by_device.items():
        if device_id not in SMART_PLUG_DEVICE_TO_ZONE:
            continue

        for reading in normalize_records(payload):
            if not isinstance(reading, dict):
                continue

            dt_local = parse_timestamp_to_local(get_reading_timestamp(reading))
            if dt_local is None:
                continue
            if not within_local_bounds(dt_local, start_local, end_local):
                continue

            power_value = extract_smart_plug_power(reading)
            if power_value is None:
                continue

            per_device_minute_values[minute_floor(dt_local)][device_id].append(power_value)

    power_by_minute = defaultdict(dict)
    for minute_key, device_values in per_device_minute_values.items():
        zone_totals = defaultdict(float)
        for device_id, values in device_values.items():
            zone = SMART_PLUG_DEVICE_TO_ZONE[device_id]
            zone_totals[zone] += average(values)

        for zone, total_power in zone_totals.items():
            power_by_minute[minute_key][zone] = total_power

    return power_by_minute


def extract_mmwave_state(reading, device_id):
    radar_states = [
        to_binary_state(get_nested_value(reading, field_candidate))
        for field_candidate in MMWAVE_PRIMARY_OCCUPANCY_FIELD_CANDIDATES
    ]
    radar_states = [state for state in radar_states if state is not None]
    if radar_states:
        return 1 if any(radar_states) else 0

    field_name = MMWAVE_DEVICE_TO_FIELD.get(device_id)
    field_candidates = []
    if field_name:
        field_candidates.extend((field_name, f"radar.{field_name}"))
        if field_name.startswith("radar_zone_"):
            short_field_name = field_name[len("radar_"):]
            field_candidates.extend((short_field_name, f"radar.{short_field_name}"))

    field_candidates.extend((
        "state.value",
        "state",
        "occupancy",
        "target",
        "value",
    ))

    return to_binary_state(first_present_value(reading, field_candidates))


def aggregate_mmwave_by_minute(readings_by_device, time_start, time_end, max_stale_gap=MMWAVE_MAX_STALE_GAP):
    minute_index = build_local_minute_index(time_start, time_end)
    events_by_zone = defaultdict(list)

    for device_id, payload in readings_by_device.items():
        zone = MMWAVE_DEVICE_TO_ZONE.get(device_id)
        if zone is None:
            continue

        for reading in normalize_records(payload):
            if not isinstance(reading, dict):
                continue

            dt_local = parse_timestamp_to_local(get_reading_timestamp(reading))
            if dt_local is None:
                continue

            state_value = extract_mmwave_state(reading, device_id)
            if state_value is None:
                continue

            events_by_zone[zone].append((dt_local, state_value))

    mmwave_by_minute = defaultdict(dict)
    if not minute_index:
        return mmwave_by_minute

    start_local = minute_index[0]
    for zone, events in events_by_zone.items():
        events.sort(key=lambda item: item[0])
        event_index = 0
        current_state = None
        current_state_timestamp = None

        while event_index < len(events) and events[event_index][0] < start_local:
            current_state_timestamp = events[event_index][0]
            current_state = events[event_index][1]
            event_index += 1

        for minute_key in minute_index:
            minute_end = minute_key + SAMPLE_INTERVAL
            minute_max = current_state
            newest_event_timestamp = current_state_timestamp

            while event_index < len(events) and events[event_index][0] < minute_end:
                current_state_timestamp = events[event_index][0]
                newest_event_timestamp = current_state_timestamp
                current_state = events[event_index][1]
                minute_max = current_state if minute_max is None else max(minute_max, current_state)
                event_index += 1

            known_in_minute = minute_max is not None
            if known_in_minute and max_stale_gap is not None:
                if newest_event_timestamp is None or minute_end - newest_event_timestamp > max_stale_gap:
                    known_in_minute = False

            if known_in_minute:
                mmwave_by_minute[minute_key][zone] = 1 if minute_max else 0

    return mmwave_by_minute


def payload_item_count(payload):
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict):
        return len(normalize_records(payload))
    return 0


def value_or_blank(value):
    return "" if value is None else value


def validate_csv_width(filename, expected_column_count):
    bad_rows = []
    with open(filename, "r", newline="", encoding="utf-8") as csvfile:
        reader = csv.reader(csvfile)
        for row_number, row in enumerate(reader, start=1):
            if len(row) != expected_column_count:
                bad_rows.append((row_number, len(row)))
    if bad_rows:
        preview = ", ".join(
            f"line {row_number}: {column_count} columns"
            for row_number, column_count in bad_rows[:5]
        )
        raise ValueError(
            f"CSV validation failed for {filename}; expected {expected_column_count} columns. "
            f"Bad rows: {preview}"
        )


def format_utc_range(time_start, time_end):
    return f"{format_api_timestamp(time_start)} to {format_api_timestamp(time_end)}"


def format_elapsed(seconds):
    return f"{seconds:.2f}s"


class ApiResponseError(Exception):

    def __init__(self, message, status_code=None, transient=False):
        super().__init__(message)
        self.status_code = status_code
        self.transient = transient


class Air1Device:

    def __init__(
        self,
        api_url,
        api_key,
        api_timeout=API_TIMEOUT_SECONDS,
        api_retries=API_RETRIES,
        max_workers=MAX_PARALLEL_API_REQUESTS,
        chunk_days=DEFAULT_CHUNK_DAYS,
        min_chunk_hours=DEFAULT_MIN_CHUNK_HOURS,
        progress_every=DEFAULT_PROGRESS_EVERY,
        verbose_progress=False,
        timing_summary=True,
        retry_base_delay=API_RETRY_BASE_DELAY_SECONDS,
    ):
        if not api_key:
            raise ValueError("AIR1_API_KEY is required. Set it in the environment before running this script.")
        self.api_url = api_url.rstrip("/")
        self.headers = {
            "Accept": "*/*",
            "X-API-KEY": api_key
        }
        if api_timeout <= 0:
            raise ValueError("api_timeout must be greater than 0")
        if api_retries < 0:
            raise ValueError("api_retries must be greater than or equal to 0")
        if max_workers <= 0:
            raise ValueError("max_workers must be greater than 0")
        if chunk_days <= 0:
            raise ValueError("chunk_days must be greater than 0")
        if min_chunk_hours <= 0:
            raise ValueError("min_chunk_hours must be greater than 0")
        if progress_every <= 0:
            raise ValueError("progress_every must be greater than 0")
        self.api_timeout = api_timeout
        self.api_retries = api_retries
        self.max_workers = max_workers
        self.chunk_days = chunk_days
        self.min_chunk_hours = min_chunk_hours
        self.progress_every = progress_every
        self.verbose_progress = verbose_progress
        self.timing_summary = timing_summary
        self.retry_base_delay = retry_base_delay
        self._thread_local = threading.local()
        self.last_fetch_metrics = {}

    def _get_session(self):
        session = getattr(self._thread_local, "session", None)
        if session is None:
            session = requests.Session()
            adapter = requests.adapters.HTTPAdapter(
                pool_connections=max(1, self.max_workers),
                pool_maxsize=max(1, self.max_workers),
            )
            session.mount("http://", adapter)
            session.mount("https://", adapter)
            session.headers.update(self.headers)
            self._thread_local.session = session
        return session

    def _request_json_once(self, path, error_context):
        response = self._get_session().get(
            f"{self.api_url}{path}",
            timeout=self.api_timeout,
        )
        if response.status_code == 200:
            if response.text and response.text.strip():
                try:
                    return response.json()
                except json.JSONDecodeError:
                    print(f"{error_context} returned invalid JSON")
                    return None
            print(f"{error_context} returned an empty response")
            return None

        transient = (
            response.status_code in TRANSIENT_HTTP_STATUS_CODES
            or 500 <= response.status_code <= 599
        )
        raise ApiResponseError(
            f"{error_context} failed with status code {response.status_code}",
            status_code=response.status_code,
            transient=transient,
        )

    def _get_json(self, path, error_context):
        try:
            return self._request_json_once(path, error_context)
        except ApiResponseError as error:
            print(error)
            return None
        except requests.exceptions.RequestException as error:
            print(f"{error_context} connection error: {error}")
            return None

    def _get_json_with_retry_details(self, path, error_context):
        max_attempts = self.api_retries + 1
        transient_errors = (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
        )

        for attempt in range(1, max_attempts + 1):
            try:
                return self._request_json_once(path, error_context), attempt, None, False
            except transient_errors as error:
                if attempt >= max_attempts:
                    return None, attempt, str(error), True

                delay_seconds = self.retry_base_delay * (2 ** (attempt - 1))
                print(
                    f"  {error_context} timeout/connection error on attempt "
                    f"{attempt}/{max_attempts}: {error}. Retrying in {delay_seconds:.1f}s"
                )
                time.sleep(delay_seconds)
            except ApiResponseError as error:
                if error.transient and attempt < max_attempts:
                    delay_seconds = self.retry_base_delay * (2 ** (attempt - 1))
                    print(
                        f"  {error_context} transient HTTP error on attempt "
                        f"{attempt}/{max_attempts}: {error}. Retrying in {delay_seconds:.1f}s"
                    )
                    time.sleep(delay_seconds)
                    continue
                return None, attempt, str(error), error.transient
            except requests.exceptions.RequestException as error:
                return None, attempt, str(error), False

        return None, max_attempts, "request failed", True

    def _get_json_with_retries(self, path, error_context):
        payload, attempts, failure, _retryable_failure = self._get_json_with_retry_details(path, error_context)
        return payload, attempts, failure

    def _get_historical_data(self, endpoint, device_id, time_start, time_end):
        start_encoded = urllib.parse.quote(format_api_timestamp(time_start))
        end_encoded = urllib.parse.quote(format_api_timestamp(time_end))
        path = f"/{endpoint}/{device_id}?time_start={start_encoded}&time_end={end_encoded}"
        return self._get_json(path, f"Historical {endpoint} request for device {device_id}")

    def _get_historical_data_with_retries(self, endpoint, device_id, time_start, time_end, error_context):
        start_encoded = urllib.parse.quote(format_api_timestamp(time_start))
        end_encoded = urllib.parse.quote(format_api_timestamp(time_end))
        path = f"/{endpoint}/{device_id}?time_start={start_encoded}&time_end={end_encoded}"
        return self._get_json_with_retry_details(path, error_context)

    def get_all_devices(self):
        return self._get_json("/air-1", "AIR-1 device list request") or []

    def get_device_data(self, device_id):
        return self._get_json(f"/air-1/{device_id}", f"AIR-1 device {device_id} request")

    def get_historical_data(self, device_id, time_start, time_end):
        return self._get_historical_data("air-1", device_id, time_start, time_end)

    def get_all_smart_plugs(self):
        return self._get_json("/smart-plug-v2", "Smart plug device list request") or []

    def get_smart_plug_data(self, device_id):
        return self._get_json(f"/smart-plug-v2/{device_id}", f"Smart plug device {device_id} request")

    def get_smart_plug_historical_data(self, device_id, time_start, time_end):
        return self._get_historical_data("smart-plug-v2", device_id, time_start, time_end)

    def get_all_msr2_devices(self):
        return self._get_json("/msr-2", "MSR-2 device list request") or []

    def get_msr2_data(self, device_id):
        return self._get_json(f"/msr-2/{device_id}", f"MSR-2 device {device_id} request")

    def get_msr2_historical_data(self, device_id, time_start, time_end):
        return self._get_historical_data("msr-2", device_id, time_start, time_end)

    def _fetch_historical_device_chunk(self, job):
        fetch_start = self._historical_job_fetch_start(job)
        fetch_end = job["chunk_end"]
        error_context = (
            f"Historical {job['source_name']} request for {job['device_description']}, "
            f"{format_utc_range(fetch_start, fetch_end)}"
        )
        request_started_at = time.perf_counter()
        payload, attempts, failure, retryable_failure = self._get_historical_data_with_retries(
            job["endpoint"],
            job["device_id"],
            fetch_start,
            fetch_end,
            error_context,
        )
        request_elapsed_seconds = time.perf_counter() - request_started_at
        result = dict(job)
        result.update({
            "fetch_start": fetch_start,
            "fetch_end": fetch_end,
            "attempts": attempts,
            "request_elapsed_seconds": request_elapsed_seconds,
            "failure": failure,
            "retryable_failure": retryable_failure,
            "records": normalize_records(payload),
        })
        return result

    def _historical_job_fetch_start(self, job):
        first_chunk_lookback = job.get("first_chunk_lookback")
        if first_chunk_lookback is not None and job["chunk_start"] == job["range_start"]:
            return job["chunk_start"] - first_chunk_lookback
        return job["chunk_start"]

    def _make_historical_jobs_for_source(self, source_spec, time_start, time_end):
        device_ids = list(source_spec["device_ids"])
        if not device_ids:
            return []

        fetch_jobs = []
        chunks = split_time_range_into_chunks(time_start, time_end, self.chunk_days)
        for chunk_index, (chunk_start, chunk_end) in enumerate(chunks, start=1):
            for device_id in device_ids:
                fetch_jobs.append({
                    "source_key": source_spec["source_key"],
                    "source_name": source_spec["source_name"],
                    "endpoint": source_spec["endpoint"],
                    "device_id": device_id,
                    "device_description": source_spec["describe_device"](device_id),
                    "range_start": time_start,
                    "chunk_start": chunk_start,
                    "chunk_end": chunk_end,
                    "initial_chunk_number": chunk_index,
                    "initial_chunk_count": len(chunks),
                    "split_depth": 0,
                    "first_chunk_lookback": source_spec.get("first_chunk_lookback"),
                })
        return fetch_jobs

    def _split_historical_job(self, job):
        chunk_duration = job["chunk_end"] - job["chunk_start"]
        minimum_duration = timedelta(hours=self.min_chunk_hours)
        if chunk_duration < minimum_duration * 2:
            return []

        midpoint = job["chunk_start"] + chunk_duration / 2
        left = dict(job)
        right = dict(job)
        left["chunk_end"] = midpoint
        right["chunk_start"] = midpoint
        left["split_depth"] = job["split_depth"] + 1
        right["split_depth"] = job["split_depth"] + 1
        return [left, right]

    def _build_round_robin_historical_queue(self, source_specs, time_start, time_end):
        pending_jobs = deque()
        source_job_counts = {}
        source_job_queues = []

        for source_spec in source_specs:
            source_jobs = deque(self._make_historical_jobs_for_source(source_spec, time_start, time_end))
            source_job_counts[source_spec["source_key"]] = len(source_jobs)
            if source_jobs:
                source_job_queues.append(source_jobs)

        while source_job_queues:
            next_source_job_queues = []
            for source_jobs in source_job_queues:
                pending_jobs.append(source_jobs.popleft())
                if source_jobs:
                    next_source_job_queues.append(source_jobs)
            source_job_queues = next_source_job_queues

        return pending_jobs, source_job_counts

    def _fetch_adaptive_historical_sources(self, source_specs, time_start, time_end):
        fetch_started_at = time.perf_counter()
        histories_by_source = {
            source_spec["source_key"]: defaultdict(list)
            for source_spec in source_specs
        }
        source_names = {
            source_spec["source_key"]: source_spec["source_name"]
            for source_spec in source_specs
        }
        failures = []
        pending_jobs, source_job_counts = self._build_round_robin_historical_queue(
            source_specs,
            time_start,
            time_end,
        )

        initial_job_count = len(pending_jobs)
        if not initial_job_count:
            self.last_fetch_metrics = {
                "elapsed_seconds": 0.0,
                "initial_requests": 0,
                "submitted_requests": 0,
                "completed_requests": 0,
                "terminal_chunks": 0,
                "successful_chunks": 0,
                "failed_chunks": 0,
                "split_count": 0,
                "retry_count": 0,
                "record_count": 0,
                "source_names": source_names,
                "source_metrics": {},
            }
            return {
                source_key: dict(histories_by_device)
                for source_key, histories_by_device in histories_by_source.items()
            }, failures

        metrics = {
            "elapsed_seconds": 0.0,
            "initial_requests": initial_job_count,
            "submitted_requests": 0,
            "completed_requests": 0,
            "terminal_chunks": 0,
            "successful_chunks": 0,
            "failed_chunks": 0,
            "split_count": 0,
            "retry_count": 0,
            "record_count": 0,
            "source_names": source_names,
            "source_metrics": {
                source_key: {
                    "initial_requests": source_job_counts.get(source_key, 0),
                    "completed_requests": 0,
                    "terminal_chunks": 0,
                    "successful_chunks": 0,
                    "failed_chunks": 0,
                    "split_count": 0,
                    "retry_count": 0,
                    "record_count": 0,
                    "request_seconds": 0.0,
                }
                for source_key in source_names
            },
        }

        worker_count = min(self.max_workers, initial_job_count)
        print("\n" + "=" * 80)
        print("FETCHING HISTORICAL DATA WITH ADAPTIVE CHUNKS")
        print("=" * 80)
        print(
            f"Starting {initial_job_count} request(s) across "
            f"{len(source_specs)} source(s) with {worker_count} total worker(s)"
        )
        for source_spec in source_specs:
            print(f"  {source_spec['source_name']}: {source_job_counts[source_spec['source_key']]} initial request(s)")

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            active_futures = {}
            submitted_requests = 0
            completed_requests = 0
            terminal_jobs = 0

            def submit_available_jobs():
                nonlocal submitted_requests
                while pending_jobs and len(active_futures) < worker_count:
                    job = pending_jobs.popleft()
                    future = executor.submit(self._fetch_historical_device_chunk, job)
                    active_futures[future] = job
                    submitted_requests += 1
                    metrics["submitted_requests"] = submitted_requests

            submit_available_jobs()

            while active_futures:
                done_futures, _not_done = wait(active_futures, return_when=FIRST_COMPLETED)
                for future in done_futures:
                    job = active_futures.pop(future)
                    completed_requests += 1
                    try:
                        result = future.result()
                    except Exception as error:
                        result = dict(job)
                        result.update({
                            "fetch_start": self._historical_job_fetch_start(job),
                            "fetch_end": job["chunk_end"],
                            "attempts": 0,
                            "request_elapsed_seconds": 0.0,
                            "failure": str(error),
                            "retryable_failure": True,
                            "records": [],
                        })

                    self._update_request_metrics(metrics, result)
                    child_jobs = []
                    if result["failure"] and result.get("retryable_failure"):
                        child_jobs = self._split_historical_job(result)

                    if child_jobs:
                        metrics["split_count"] += 1
                        metrics["source_metrics"][result["source_key"]]["split_count"] += 1
                        pending_jobs.extendleft(reversed(child_jobs))
                        print(
                            f"  Split {result['source_name']} {result['device_description']} "
                            f"{format_utc_range(result['fetch_start'], result['fetch_end'])} "
                            f"after {result['attempts']} attempt(s): {result['failure']}"
                        )
                    else:
                        terminal_jobs += 1
                        metrics["terminal_chunks"] = terminal_jobs
                        self._record_historical_result(result, histories_by_source, failures, metrics)
                        self._print_adaptive_request_progress(
                            result,
                            completed_requests,
                            terminal_jobs,
                            submitted_requests,
                            len(pending_jobs),
                            len(active_futures),
                        )

                submit_available_jobs()

        metrics["elapsed_seconds"] = time.perf_counter() - fetch_started_at
        self.last_fetch_metrics = metrics
        return {
            source_key: dict(histories_by_device)
            for source_key, histories_by_device in histories_by_source.items()
        }, failures

    def _update_request_metrics(self, metrics, result):
        source_key = result["source_key"]
        source_metrics = metrics["source_metrics"][source_key]
        attempts = result.get("attempts", 0)
        retry_count = max(0, attempts - 1)
        record_count = len(result.get("records", []))
        request_elapsed_seconds = result.get("request_elapsed_seconds", 0.0)

        metrics["completed_requests"] += 1
        metrics["retry_count"] += retry_count
        metrics["record_count"] += record_count

        source_metrics["completed_requests"] += 1
        source_metrics["retry_count"] += retry_count
        source_metrics["record_count"] += record_count
        source_metrics["request_seconds"] += request_elapsed_seconds

    def _record_historical_result(self, result, histories_by_source, failures, metrics=None):
        record_count = len(result["records"])
        if metrics is not None:
            source_metrics = metrics["source_metrics"][result["source_key"]]
            source_metrics["terminal_chunks"] += 1
            if result["failure"]:
                metrics["failed_chunks"] += 1
                source_metrics["failed_chunks"] += 1
            else:
                metrics["successful_chunks"] += 1
                source_metrics["successful_chunks"] += 1
        if result["failure"]:
            failures.append(result)
            return
        if record_count:
            histories_by_source[result["source_key"]][result["device_id"]].extend(result["records"])

    def _print_adaptive_request_progress(
        self,
        result,
        completed_requests,
        terminal_jobs,
        submitted_requests,
        pending_count,
        active_count,
    ):
        record_count = len(result["records"])
        progress_prefix = (
            f"  [{completed_requests} completed / {submitted_requests} submitted] "
            f"{result['source_name']} {format_utc_range(result['fetch_start'], result['fetch_end'])} | "
            f"{result['device_description']} | attempts {result['attempts']} | records {record_count}"
        )

        if result["failure"]:
            print(f"{progress_prefix} | FAILED: {result['failure']}")
            return

        if self.verbose_progress:
            print(progress_prefix)
            return

        if completed_requests % self.progress_every == 0:
            print(
                f"  Progress: {completed_requests} request(s) completed, "
                f"{terminal_jobs} terminal chunk(s), {pending_count} queued, {active_count} active"
            )

    def _fetch_historical_chunks(
        self,
        device_ids,
        endpoint,
        source_name,
        time_start,
        time_end,
        describe_device,
        first_chunk_lookback=None,
    ):
        source_key = source_name.lower().replace(" ", "_")
        source_specs = [{
            "source_key": source_key,
            "source_name": source_name,
            "endpoint": endpoint,
            "device_ids": list(device_ids),
            "describe_device": describe_device,
            "first_chunk_lookback": first_chunk_lookback,
        }]
        histories_by_source, failures = self._fetch_adaptive_historical_sources(
            source_specs,
            time_start,
            time_end,
        )
        return histories_by_source.get(source_key, {}), failures

    def _print_failed_historical_request_summary(self, failures):
        if not failures:
            print("\nNo historical device/chunk requests failed.")
            return

        print("\nWARNING: Historical device/chunk request failures detected.")
        print(
            f"{len(failures)} request(s) failed after the configured retries; "
            "the final CSV leaves those device/chunk values blank."
        )

        max_preview = 50
        for failure in failures[:max_preview]:
            print(
                f"  {failure['source_name']} "
                f"{format_utc_range(failure['fetch_start'], failure['fetch_end'])} | "
                f"{failure['device_description']} | attempts {failure['attempts']} | "
                f"{failure['failure']}"
            )

        remaining = len(failures) - max_preview
        if remaining > 0:
            print(f"  ... {remaining} more failed request(s) omitted from this summary.")

    def _print_timing_summary(
        self,
        total_elapsed_seconds,
        aggregation_elapsed_seconds,
        csv_write_elapsed_seconds,
        failed_gap_count,
    ):
        fetch_metrics = self.last_fetch_metrics or {}
        source_metrics = fetch_metrics.get("source_metrics", {})
        source_names = fetch_metrics.get("source_names", {})

        print("\nTIMING SUMMARY:")
        print("-" * 50)
        print(f"  Total elapsed:        {format_elapsed(total_elapsed_seconds)}")
        print(f"  Historical fetch:     {format_elapsed(fetch_metrics.get('elapsed_seconds', 0.0))}")
        print(f"  Aggregation:          {format_elapsed(aggregation_elapsed_seconds)}")
        print(f"  CSV write+validate:   {format_elapsed(csv_write_elapsed_seconds)}")
        print(
            "  Requests:             "
            f"initial={fetch_metrics.get('initial_requests', 0)}, "
            f"submitted={fetch_metrics.get('submitted_requests', 0)}, "
            f"completed={fetch_metrics.get('completed_requests', 0)}"
        )
        print(
            "  Adaptive outcomes:    "
            f"terminal={fetch_metrics.get('terminal_chunks', 0)}, "
            f"successful={fetch_metrics.get('successful_chunks', 0)}, "
            f"failed_gaps={failed_gap_count}, "
            f"splits={fetch_metrics.get('split_count', 0)}, "
            f"retries={fetch_metrics.get('retry_count', 0)}"
        )
        print(f"  Records fetched:      {fetch_metrics.get('record_count', 0)}")

        if source_metrics:
            print("  Per-source request worker time:")
            for source_key, metrics in source_metrics.items():
                source_name = source_names.get(source_key, source_key)
                print(
                    f"    {source_name}: "
                    f"{format_elapsed(metrics.get('request_seconds', 0.0))}, "
                    f"completed={metrics.get('completed_requests', 0)}, "
                    f"terminal={metrics.get('terminal_chunks', 0)}, "
                    f"splits={metrics.get('split_count', 0)}, "
                    f"failed={metrics.get('failed_chunks', 0)}, "
                    f"records={metrics.get('record_count', 0)}"
                )

    def convert_timestamp_to_datetime(self, timestamp_str):
        try:
            return parse_timestamp_to_local(timestamp_str)
        except Exception as error:
            print(f"Error converting timestamp {timestamp_str}: {error}")
            return None

    def print_device_summary(self, device_id):
        data = self.get_device_data(device_id)
        if data:
            print("\n" + "____________________________________________")
            print(f"AIR-1 Device: {device_id}")
            print("____________________________________________")

            original_timestamp = data.get("timestamp")
            if original_timestamp:
                dt_local = self.convert_timestamp_to_datetime(original_timestamp)
                if dt_local:
                    print(f"Local Time (+8 hrs): {dt_local.strftime('%Y-%m-%d %H:%M:%S')}")

            print(f"Temperature: {data.get('temperature', 'N/A')} C")
            print(f"Humidity: {data.get('humidity', 'N/A')}%")
            print(f"CO2: {data.get('co2', 'N/A')} ppm")
            print(f"PM2.5: {data.get('pm_2_5', 'N/A')} ug/m3")
            print("____________________________________________")
            return True

        print(f"No data available for device {device_id}")
        return False

    def export_all_historical_to_single_csv(self, devices, time_start, time_end, output_dir=DEFAULT_OUTPUT_DIR):
        """
        Export AIR-1, smart plug, and MSR-2 mmWave history to one wide CSV.
        Each row is one local +8 10-second bucket in the requested UTC range.
        """
        export_started_at = time.perf_counter()
        minute_index = build_local_minute_index(time_start, time_end)
        if not minute_index:
            print("No local minute rows fall inside the requested time range")
            return None

        all_fetch_failures = []

        print("\n" + "=" * 80)
        print(f"COLLECTING HISTORICAL AIR-1 DATA FOR ZONE {ZONE_NUM}")
        print("=" * 80)
        print(f"AIR-1 device: {SENSOR_ORDER[ZONE_NUM - 1]}")
        print("=" * 80)

        available_air_devices = set(devices)
        zone_air_device = SENSOR_ORDER[ZONE_NUM - 1]
        air_devices = [zone_air_device] if zone_air_device in available_air_devices else []
        if not air_devices:
            print(f"\nWarning: Zone {ZONE_NUM} AIR-1 device {zone_air_device} is not available.")

        print("\n" + "=" * 80)
        print("PREPARING SMART PLUG POWER DATA REQUESTS")
        print("=" * 80)
        smart_plug_list = self.get_all_smart_plugs()
        print(f"Smart plug list endpoint returned {payload_item_count(smart_plug_list)} item(s)")

        smart_devices = sorted(
            device_id
            for device_id, zone in SMART_PLUG_DEVICE_TO_ZONE.items()
            if zone == ZONE_NUM
        )

        print("\n" + "=" * 80)
        print("PREPARING MSR-2 MMWAVE DATA REQUESTS")
        print("=" * 80)
        msr2_list = self.get_all_msr2_devices()
        print(f"MSR-2 list endpoint returned {payload_item_count(msr2_list)} item(s)")

        mmwave_devices = sorted(
            device_id
            for device_id, zone in MMWAVE_DEVICE_TO_ZONE.items()
            if zone == ZONE_NUM
        )

        histories_by_source, all_fetch_failures = self._fetch_adaptive_historical_sources(
            [
                {
                    "source_key": "air1",
                    "source_name": "AIR-1",
                    "endpoint": "air-1",
                    "device_ids": air_devices,
                    "describe_device": lambda device_id: (
                        f"device {device_id} (Position {DEVICE_TO_POSITION[device_id]})"
                    ),
                },
                {
                    "source_key": "smart_plug",
                    "source_name": "smart plug",
                    "endpoint": "smart-plug-v2",
                    "device_ids": smart_devices,
                    "describe_device": lambda device_id: (
                        f"device {device_id} (Zone {SMART_PLUG_DEVICE_TO_ZONE[device_id]})"
                    ),
                },
                {
                    "source_key": "mmwave",
                    "source_name": "mmWave",
                    "endpoint": "msr-2",
                    "device_ids": mmwave_devices,
                    "describe_device": lambda device_id: (
                        f"device {device_id} (Zone {MMWAVE_DEVICE_TO_ZONE[device_id]})"
                    ),
                    "first_chunk_lookback": timedelta(hours=1),
                },
            ],
            time_start,
            time_end,
        )

        air_history_by_device = histories_by_source.get("air1", {})
        smart_history_by_device = histories_by_source.get("smart_plug", {})
        mmwave_history_by_device = histories_by_source.get("mmwave", {})

        aggregation_started_at = time.perf_counter()
        air_by_minute = aggregate_air1_by_minute(air_history_by_device, time_start, time_end)
        power_by_minute = aggregate_smart_plug_by_minute(smart_history_by_device, time_start, time_end)
        mmwave_by_minute = aggregate_mmwave_by_minute(mmwave_history_by_device, time_start, time_end)
        aggregation_elapsed_seconds = time.perf_counter() - aggregation_started_at

        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
            print(f"\nCreated directory: {output_dir}")

        start_str = time_start.strftime("%Y%m%d_%H%M%S")
        end_str = time_end.strftime("%Y%m%d_%H%M%S")
        filename = os.path.join(output_dir, f"training_data_zone_{ZONE_NUM}_{start_str}_to_{end_str}.csv")

        headers = ["timestamp"]
        for prefix in ("temp", "rh", "co2", "pm25", "power", "mmwave"):
            for zone in ZONES:
                headers.append(f"{prefix}_s{zone}")

        try:
            csv_write_started_at = time.perf_counter()
            def rows():
                for minute_key in minute_index:
                    air_row = air_by_minute.get(minute_key, {})
                    row_data = {
                        "timestamp": minute_key.strftime("%Y-%m-%d %H:%M:%S")
                    }

                    for zone in ZONES:
                        row_data[f"temp_s{zone}"] = value_or_blank(air_row.get("temps", {}).get(zone))
                    for zone in ZONES:
                        row_data[f"rh_s{zone}"] = value_or_blank(air_row.get("rhs", {}).get(zone))
                    for zone in ZONES:
                        row_data[f"co2_s{zone}"] = value_or_blank(air_row.get("co2s", {}).get(zone))
                    for zone in ZONES:
                        row_data[f"pm25_s{zone}"] = value_or_blank(air_row.get("pm25s", {}).get(zone))
                    for zone in ZONES:
                        row_data[f"power_s{zone}"] = value_or_blank(power_by_minute.get(minute_key, {}).get(zone))
                    for zone in ZONES:
                        row_data[f"mmwave_s{zone}"] = value_or_blank(mmwave_by_minute.get(minute_key, {}).get(zone))

                    yield row_data

            csv_parts = csv_size_guard.write_rows_rolling_atomic(
                filename,
                headers,
                rows(),
                extrasaction="raise",
            )
            for csv_part in csv_parts:
                validate_csv_width(csv_part, expected_column_count=len(headers))
            csv_write_elapsed_seconds = time.perf_counter() - csv_write_started_at

            row_count = len(minute_index)
            print("\n" + "=" * 80)
            print("EXPORT COMPLETE!")
            print(f"Total minute rows exported: {row_count}")
            print(f"File saved as: {filename}")
            print(f"Full path: {os.path.abspath(filename)}")

            self._print_availability_stats(
                minute_index,
                air_by_minute,
                power_by_minute,
                mmwave_by_minute,
                row_count,
            )
            self._print_failed_historical_request_summary(all_fetch_failures)
            if self.timing_summary:
                self._print_timing_summary(
                    total_elapsed_seconds=time.perf_counter() - export_started_at,
                    aggregation_elapsed_seconds=aggregation_elapsed_seconds,
                    csv_write_elapsed_seconds=csv_write_elapsed_seconds,
                    failed_gap_count=len(all_fetch_failures),
                )
            return filename

        except Exception as error:
            print(f"Error writing CSV file: {error}")
            import traceback
            traceback.print_exc()
            return None

    def _print_availability_stats(self, minute_index, air_by_minute, power_by_minute, mmwave_by_minute, row_count):
        print("\nDATA AVAILABILITY BY ZONE/SENSOR POSITION:")
        print("-" * 50)

        for zone in ZONES:
            device_id = SENSOR_ORDER[zone - 1]
            temp_count = sum(1 for minute in minute_index if zone in air_by_minute.get(minute, {}).get("temps", {}))
            rh_count = sum(1 for minute in minute_index if zone in air_by_minute.get(minute, {}).get("rhs", {}))
            co2_count = sum(1 for minute in minute_index if zone in air_by_minute.get(minute, {}).get("co2s", {}))
            pm25_count = sum(1 for minute in minute_index if zone in air_by_minute.get(minute, {}).get("pm25s", {}))
            power_count = sum(1 for minute in minute_index if zone in power_by_minute.get(minute, {}))
            mmwave_count = sum(1 for minute in minute_index if zone in mmwave_by_minute.get(minute, {}))

            print(f"  Zone/Sensor {zone} ({device_id}):")
            print(f"    Temperature: {temp_count}/{row_count} ({self._pct(temp_count, row_count):.1f}%)")
            print(f"    Humidity:    {rh_count}/{row_count} ({self._pct(rh_count, row_count):.1f}%)")
            print(f"    CO2:         {co2_count}/{row_count} ({self._pct(co2_count, row_count):.1f}%)")
            print(f"    PM2.5:       {pm25_count}/{row_count} ({self._pct(pm25_count, row_count):.1f}%)")
            print(f"    Power:       {power_count}/{row_count} ({self._pct(power_count, row_count):.1f}%)")
            print(f"    mmWave:      {mmwave_count}/{row_count} ({self._pct(mmwave_count, row_count):.1f}%)")

    @staticmethod
    def _pct(count, total):
        return (count / total) * 100 if total > 0 else 0


def check_expected_air1_sensors(air1, devices, max_workers):
    available_devices = set(devices)
    sensors_to_check = [
        expected_sensor
        for expected_sensor in SENSOR_ORDER
        if expected_sensor in available_devices
    ]
    futures_by_sensor = {}

    if sensors_to_check:
        worker_count = min(max_workers, len(sensors_to_check))
        print(f"Checking {len(sensors_to_check)} expected sensors with {worker_count} parallel workers")
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


def parse_args():
    parser = argparse.ArgumentParser(description="Export AIR-1, smart plug, and mmWave history to a training CSV.")
    parser.add_argument(
        "--time-start",
        type=parse_cli_datetime_utc,
        default=parse_cli_datetime_utc("2026-03-28T03:00:00Z"),
        help="UTC start time, ISO format. Default: 2026-03-28T03:00:00Z",
    )
    parser.add_argument(
        "--time-end",
        type=parse_cli_datetime_utc,
        default=None,
        help="UTC end time, ISO format. Default: one calendar month after --time-start.",
    )
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help="Directory for the exported CSV.")
    parser.add_argument(
        "--chunk-days",
        type=positive_float,
        default=DEFAULT_CHUNK_DAYS,
        help="Initial UTC chunk size for adaptive historical requests, in days. Default: 5.",
    )
    parser.add_argument(
        "--min-chunk-hours",
        type=positive_float,
        default=DEFAULT_MIN_CHUNK_HOURS,
        help="Smallest fallback chunk size for adaptive retries, in hours. Default: 3.",
    )
    parser.add_argument(
        "--api-timeout",
        type=positive_float,
        default=API_TIMEOUT_SECONDS,
        help="Per-request API timeout in seconds. Default: 30.",
    )
    parser.add_argument(
        "--api-retries",
        type=nonnegative_int,
        default=API_RETRIES,
        help="Retries after timeout/connection failures. Default: 1.",
    )
    parser.add_argument(
        "--max-workers",
        type=positive_int,
        default=MAX_PARALLEL_API_REQUESTS,
        help="Maximum total parallel historical API workers. Default: 16.",
    )
    parser.add_argument(
        "--progress-every",
        type=positive_int,
        default=DEFAULT_PROGRESS_EVERY,
        help="Print one historical progress summary per N completed requests. Default: 25.",
    )
    parser.add_argument(
        "--verbose-progress",
        action="store_true",
        help="Print every historical request result instead of periodic progress summaries.",
    )
    timing_group = parser.add_mutually_exclusive_group()
    timing_group.add_argument(
        "--timing-summary",
        dest="timing_summary",
        action="store_true",
        default=True,
        help="Print timing and request metrics after a successful export. Default: enabled.",
    )
    timing_group.add_argument(
        "--no-timing-summary",
        dest="timing_summary",
        action="store_false",
        help="Disable the final timing and request metrics summary.",
    )

    return parser.parse_args()


def main():
    args = parse_args()
    if args.time_end is None:
        args.time_end = add_one_calendar_month(args.time_start)
    if args.time_end <= args.time_start:
        print("--time-end must be after --time-start")
        return

    air1 = Air1Device(
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

    devices = air1.get_all_devices()
    print(f"\nFound {len(devices)} total air-1 devices on network: {devices}")

    if not devices:
        print("No AIR-1 devices found")
        return

    print("\n" + "=" * 80)
    print("CHECKING EXPECTED SENSORS")
    print("=" * 80)

    working_devices, missing_devices = check_expected_air1_sensors(
        air1,
        devices,
        args.max_workers,
    )

    zone_device = SENSOR_ORDER[ZONE_NUM - 1]
    print(f"\nSummary: {len(working_devices)}/15 expected AIR-1 sensors are active and have data")

    if zone_device in working_devices:
        print("\n" + "=" * 80)
        print(f"Export Zone {ZONE_NUM} historical data for ML training")
        print("=" * 80)

        try:
            time_start = args.time_start
            time_end = args.time_end

            print(f"\nTime range for export (UTC): {time_start} to {time_end}")
            print("This will be converted to local time (+8 hours) in the CSV file")
            print(
                f"Historical fetch settings: chunk_days={args.chunk_days}, "
                f"min_chunk_hours={args.min_chunk_hours}, "
                f"api_timeout={args.api_timeout}s, api_retries={args.api_retries}, "
                f"max_workers={args.max_workers}, progress_every={args.progress_every}, "
                f"verbose_progress={args.verbose_progress}, timing_summary={args.timing_summary}"
            )
            print("=" * 80)

            csv_file = air1.export_all_historical_to_single_csv(
                working_devices,
                time_start,
                time_end,
                output_dir=args.output_dir,
            )

            if csv_file:
                print(f"\nSuccessfully exported training data to: {csv_file}")
            else:
                print("\nFailed to export data")

        except Exception as error:
            print(f"Error in historical data export: {error}")
            import traceback
            traceback.print_exc()
    else:
        print(f"\nZone {ZONE_NUM} AIR-1 sensor {zone_device} is not active. Please check if it is online and has data.")


if __name__ == "__main__":
    main()
