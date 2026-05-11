from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from zone5 import air1_exporter as csv_training


ZONE_NUM = 5


class Zone5Air1Client(csv_training.Air1Device):
    """AIR-1 API client used by Zone 5 live collection and web inference."""


def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): json_safe(val) for key, val in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(item) for item in value]
    if isinstance(value, (datetime, pd.Timestamp)):
        if pd.isna(value):
            return None
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "item"):
        try:
            return json_safe(value.item())
        except (TypeError, ValueError):
            pass
    return value


def failure_to_metadata(failure: dict[str, Any]) -> dict[str, Any]:
    metadata_keys = [
        "source_key",
        "source_name",
        "endpoint",
        "device_id",
        "device_description",
        "range_start",
        "chunk_start",
        "chunk_end",
        "fetch_start",
        "fetch_end",
        "attempts",
        "request_elapsed_seconds",
        "failure",
        "retryable_failure",
        "split_depth",
        "initial_chunk_number",
        "initial_chunk_count",
    ]
    return {key: json_safe(failure[key]) for key in metadata_keys if key in failure}


def zone_5_air_devices(devices: list[str] | tuple[str, ...] | set[str]) -> list[str]:
    zone_5_device = csv_training.SENSOR_ORDER[ZONE_NUM - 1]
    return [zone_5_device] if zone_5_device in set(devices) else []


def devices_for_zone(mapping: dict[str, int], zone: int) -> list[str]:
    return sorted(device_id for device_id, mapped_zone in mapping.items() if mapped_zone == zone)


def zone_5_source_devices(devices: list[str] | tuple[str, ...] | set[str]) -> dict[str, list[str]]:
    return {
        "air1": zone_5_air_devices(devices),
        "smart_plug": devices_for_zone(csv_training.SMART_PLUG_DEVICE_TO_ZONE, ZONE_NUM),
        "mmwave": devices_for_zone(csv_training.MMWAVE_DEVICE_TO_ZONE, ZONE_NUM),
    }


def build_zone_5_source_specs(source_devices: dict[str, list[str]]) -> list[dict[str, Any]]:
    return [
        {
            "source_key": "air1",
            "source_name": "AIR-1",
            "endpoint": "air-1",
            "device_ids": source_devices["air1"],
            "describe_device": lambda device_id: (
                f"device {device_id} (Position {csv_training.DEVICE_TO_POSITION[device_id]})"
            ),
        },
        {
            "source_key": "smart_plug",
            "source_name": "smart plug",
            "endpoint": "smart-plug-v2",
            "device_ids": source_devices["smart_plug"],
            "describe_device": lambda device_id: (
                f"device {device_id} (Zone {csv_training.SMART_PLUG_DEVICE_TO_ZONE[device_id]})"
            ),
        },
        {
            "source_key": "mmwave",
            "source_name": "mmWave",
            "endpoint": "msr-2",
            "device_ids": source_devices["mmwave"],
            "describe_device": lambda device_id: (
                f"device {device_id} (Zone {csv_training.MMWAVE_DEVICE_TO_ZONE[device_id]})"
            ),
            "first_chunk_lookback": timedelta(hours=1),
        },
    ]
