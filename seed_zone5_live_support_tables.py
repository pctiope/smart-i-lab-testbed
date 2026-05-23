"""Seed missing Zone 5 support tables from current live BSG sources.

This utility exists to make the migrated Zone 5 runtime smoke frame fully
populated even when dedicated SEN55 and CV-label collectors are not yet wired
into the root BSG pipeline.

Current seeding strategy
------------------------
- silver.zone5_sen55     <- live silver.ag_one rows (single AG-One device)
- silver.zone5_cv_labels <- live silver.msr_2 occupancy surrogate for the
                            confirmed Zone 5 mmWave device

The CV-label seed is a smoke-only surrogate. It is suitable for runtime checks
and pipeline validation, not for model-quality training labels.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
from datetime import datetime, timezone

import pandas as pd

os.environ.pop("DUCKDB_READ_ONLY", None)

from dataloader import DataLoader
import zone5_training_migrated as migrated


LOGGER = logging.getLogger("seed_zone5_live_support_tables")


def _loader() -> DataLoader:
    return DataLoader()


def configure_logging(log_path: str | None = None) -> None:
    LOGGER.handlers.clear()
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    LOGGER.addHandler(console_handler)

    if log_path:
        path = Path(log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        LOGGER.addHandler(file_handler)


def _log(message: str) -> None:
    LOGGER.info(message)


def _normalize_start_timestamp(value: str | None) -> pd.Timestamp | None:
    if not value:
        return None
    parsed = pd.Timestamp(value)
    if parsed.tzinfo is not None:
        parsed = parsed.tz_convert(None)
    return parsed.floor("s")


def _normalize_end_timestamp(value: str | None) -> pd.Timestamp | None:
    if not value:
        return None
    parsed = pd.Timestamp(value)
    if parsed.tzinfo is not None:
        parsed = parsed.tz_convert(None)
    if len(value) <= 10:
        parsed = parsed + pd.Timedelta(days=1)
    return parsed.floor("s")


def _filter_timestamp_range(
    frame: pd.DataFrame,
    *,
    start_timestamp: pd.Timestamp | None,
    end_timestamp: pd.Timestamp | None,
    source_label: str,
) -> pd.DataFrame:
    filtered = frame.copy()
    filtered["timestamp"] = pd.to_datetime(filtered["timestamp"], errors="coerce").dt.floor("10s")
    filtered = filtered.dropna(subset=["timestamp"])
    if start_timestamp is not None:
        filtered = filtered.loc[filtered["timestamp"] >= start_timestamp]
    if end_timestamp is not None:
        filtered = filtered.loc[filtered["timestamp"] < end_timestamp]
    _log(
        f"[{source_label}] rows after timestamp filter: {len(filtered)} "
        f"(start={start_timestamp.isoformat() if start_timestamp is not None else 'min'}, "
        f"end={end_timestamp.isoformat() if end_timestamp is not None else 'latest'})"
    )
    return filtered.reset_index(drop=True)


def _select_seed_device(frame: pd.DataFrame, device_id: str | None) -> str:
    if device_id:
        return device_id
    if "device_id" not in frame.columns or frame.empty:
        raise ValueError("No device_id column is available to choose a seed source")
    counts = frame["device_id"].astype(str).value_counts(dropna=True)
    if counts.empty:
        raise ValueError("No candidate device IDs found in source frame")
    return str(counts.index[0])


def seed_zone5_sen55_from_ag_one(
    *,
    rebuild: bool = False,
    source_device_id: str | None = None,
    start_timestamp: pd.Timestamp | None = None,
    end_timestamp: pd.Timestamp | None = None,
) -> dict[str, object]:
    ag_one = _loader().load_all(device_type="ag-one", layer="silver")
    if ag_one.empty:
        raise ValueError("silver.ag_one is empty; cannot seed silver.zone5_sen55")

    selected_device_id = _select_seed_device(ag_one, source_device_id)
    _log(f"[zone5_sen55] selected AG-One device_id={selected_device_id}")
    seeded = ag_one.loc[ag_one["device_id"].astype(str) == selected_device_id].copy()
    if seeded.empty:
        raise ValueError(f"AG-One device_id '{selected_device_id}' was not found in silver.ag_one")

    seeded = _filter_timestamp_range(
        seeded,
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
        source_label="zone5_sen55",
    )
    if seeded.empty:
        raise ValueError("No AG-One rows remain after timestamp filtering for silver.zone5_sen55")
    seeded = seeded.rename(
        columns={
            "pm_1_0": "pm1_0",
            "pm_2_5": "pm2_5",
            "pm_10_0": "pm10_0",
        }
    )
    if "pm4_0" not in seeded.columns:
        seeded["pm4_0"] = pd.to_numeric(seeded.get("pm2_5"), errors="coerce")

    keep = ["timestamp", "pm1_0", "pm2_5", "pm4_0", "pm10_0", "temperature", "humidity", "voc", "nox"]
    for column in keep:
        if column not in seeded.columns:
            seeded[column] = pd.NA
    seeded = seeded[keep].sort_values("timestamp").drop_duplicates("timestamp", keep="last").reset_index(drop=True)
    _log(f"[zone5_sen55] writing {len(seeded)} rows to {migrated.SILVER_SEN55}")
    migrated.upsert_table_dataframe(seeded, migrated.SILVER_SEN55, key_columns=["timestamp"], rebuild=rebuild)

    return {
        "source_table": "silver.ag_one",
        "source_device_id": selected_device_id,
        "target_table": migrated.SILVER_SEN55,
        "rows": int(len(seeded)),
        "start": seeded["timestamp"].min().isoformat() if not seeded.empty else None,
        "end": seeded["timestamp"].max().isoformat() if not seeded.empty else None,
    }


def seed_zone5_cv_labels_from_mmwave(
    *,
    rebuild: bool = False,
    source_device_id: str | None = None,
    start_timestamp: pd.Timestamp | None = None,
    end_timestamp: pd.Timestamp | None = None,
) -> dict[str, object]:
    msr = _loader().load_all(device_type="msr-2", layer="silver")
    if msr.empty:
        raise ValueError("silver.msr_2 is empty; cannot seed silver.zone5_cv_labels")

    selected_device_id = source_device_id or migrated.ZONE5_MMWAVE_DEVICE_ID
    _log(f"[zone5_cv_labels] selected MSR-2 device_id={selected_device_id}")
    seeded = msr.loc[msr["device_id"].astype(str) == selected_device_id].copy()
    if seeded.empty:
        raise ValueError(f"MSR-2 device_id '{selected_device_id}' was not found in silver.msr_2")

    seeded = _filter_timestamp_range(
        seeded,
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
        source_label="zone5_cv_labels",
    )
    if seeded.empty:
        raise ValueError("No MSR-2 rows remain after timestamp filtering for silver.zone5_cv_labels")
    occupancy_columns = [
        column
        for column in (
            "zone_1_occupancy",
            "zone_2_occupancy",
            "zone_3_occupancy",
            "detection_target",
            "moving_target",
            "still_target",
            "radar_zone_1_occupancy",
            "radar_zone_2_occupancy",
            "radar_zone_3_occupancy",
            "radar_target",
        )
        if column in seeded.columns
    ]
    if not occupancy_columns:
        raise ValueError("No occupancy columns found in silver.msr_2 for CV-label surrogate seeding")

    occupancy = seeded[occupancy_columns].replace(
        {True: 1, False: 0, "true": 1, "false": 0, "True": 1, "False": 0}
    ).infer_objects(copy=False)
    occupancy = occupancy.apply(pd.to_numeric, errors="coerce").max(axis=1)
    seeded["occupancy_count"] = occupancy.fillna(0.0)
    seeded["zone_occupied"] = (seeded["occupancy_count"] >= 1.0).astype(float)
    seeded["cv_is_occupied"] = seeded["zone_occupied"]
    seeded = seeded[["timestamp", "occupancy_count", "cv_is_occupied", "zone_occupied"]]
    seeded = seeded.sort_values("timestamp").drop_duplicates("timestamp", keep="last").reset_index(drop=True)
    _log(f"[zone5_cv_labels] writing {len(seeded)} rows to {migrated.SILVER_CV_LABELS}")
    migrated.upsert_table_dataframe(seeded, migrated.SILVER_CV_LABELS, key_columns=["timestamp"], rebuild=rebuild)

    return {
        "source_table": "silver.msr_2",
        "source_device_id": selected_device_id,
        "target_table": migrated.SILVER_CV_LABELS,
        "rows": int(len(seeded)),
        "surrogate": True,
        "start": seeded["timestamp"].min().isoformat() if not seeded.empty else None,
        "end": seeded["timestamp"].max().isoformat() if not seeded.empty else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed missing Zone 5 support tables from live BSG sources")
    parser.add_argument("--rebuild", action="store_true", help="Rebuild the target tables before writing seed rows")
    parser.add_argument("--ag-one-device-id", default=None, help="Override the AG-One device_id used for the Zone 5 SEN55 seed")
    parser.add_argument("--mmwave-device-id", default=None, help="Override the MSR-2 device_id used for the Zone 5 CV-label surrogate")
    parser.add_argument("--rebuild-training-input", action="store_true", help="Rebuild silver.zone5_training_input after seeding the support tables")
    parser.add_argument("--start-date", default=None, help="Inclusive lower timestamp bound for seeding, for example 2026-05-01")
    parser.add_argument("--end-date", default=None, help="Exclusive upper timestamp bound for seeding; date-only values advance to the next day")
    parser.add_argument("--log-path", default=None, help="Optional file path for seed-process logs")
    args = parser.parse_args()

    configure_logging(args.log_path)
    start_timestamp = _normalize_start_timestamp(args.start_date)
    end_timestamp = _normalize_end_timestamp(args.end_date)
    _log(
        "Starting Zone 5 support-table seeding "
        f"(rebuild={args.rebuild}, rebuild_training_input={args.rebuild_training_input}, "
        f"start={start_timestamp.isoformat() if start_timestamp is not None else 'min'}, "
        f"end={end_timestamp.isoformat() if end_timestamp is not None else 'latest'})"
    )

    sen55_report = seed_zone5_sen55_from_ag_one(
        rebuild=args.rebuild,
        source_device_id=args.ag_one_device_id,
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
    )
    cv_report = seed_zone5_cv_labels_from_mmwave(
        rebuild=args.rebuild,
        source_device_id=args.mmwave_device_id,
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
    )
    result: dict[str, object] = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "zone5_sen55": sen55_report,
        "zone5_cv_labels": cv_report,
    }

    if args.rebuild_training_input:
        _log("Rebuilding silver.zone5_training_input from filtered support tables")
        training_input = migrated.build_zone5_training_input_from_silver(rebuild=True)
        result["training_input_rows"] = int(len(training_input))

    _log("Zone 5 support-table seeding completed successfully")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())