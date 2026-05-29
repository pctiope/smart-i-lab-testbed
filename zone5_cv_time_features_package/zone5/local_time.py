from __future__ import annotations

from typing import Any

import pandas as pd

ZONE5_LOCAL_TZ_NAME = "Asia/Manila"
ZONE5_LOCAL_OFFSET_HOURS = 8
ZONE5_LOCAL_OFFSET = pd.Timedelta(hours=ZONE5_LOCAL_OFFSET_HOURS)


def to_zone5_local_naive_timestamp(value: Any) -> pd.Timestamp:
    """Return a Zone 5 local naive timestamp for API/display/model timelines."""
    if value is None:
        return pd.NaT
    try:
        timestamp = pd.Timestamp(value)
    except (TypeError, ValueError):
        return pd.NaT
    if pd.isna(timestamp):
        return pd.NaT
    if timestamp.tzinfo is not None:
        return timestamp.tz_convert(ZONE5_LOCAL_TZ_NAME).tz_localize(None)
    return timestamp.tz_localize(None) if getattr(timestamp, "tz", None) is not None else timestamp


def zone5_local_now(freq: str | None = None) -> pd.Timestamp:
    timestamp = pd.Timestamp.now(tz=ZONE5_LOCAL_TZ_NAME).tz_localize(None)
    return timestamp.floor(freq) if freq else timestamp
