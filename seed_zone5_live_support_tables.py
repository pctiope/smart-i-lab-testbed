"""Seed missing Zone 5 support tables for the migrated Zone 5 BSG flow.

This utility bridges the existing package-local production CSV outputs into
DuckDB support tables while the root BSG pipeline owns the rest of ingestion.

Default strategy
----------------
- silver.zone5_sen55     <- zone5_cv_time_features_package/data/sen55_data.csv
- silver.zone5_cv_labels <- zone5_cv_time_features_package/data/cv_occupancy_zone5_10sec.csv

Smoke surrogate strategy
------------------------
Pass --smoke-surrogate to seed from live silver.ag_one and silver.msr_2 rows.
That path is suitable only for runtime smoke checks, not model-quality training
labels.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from datetime import datetime, timezone

import pandas as pd

os.environ.pop("DUCKDB_READ_ONLY", None)

from dataloader import DataLoader
import zone5_training_migrated as migrated


LOGGER = logging.getLogger("seed_zone5_live_support_tables")
ROOT = Path(__file__).resolve().parent
ZONE5_PACKAGE_ROOT = ROOT / "zone5_cv_time_features_package"
ZONE5_DATA_DIR = ZONE5_PACKAGE_ROOT / "data"
DEFAULT_CV_LABELS_CSV = ZONE5_DATA_DIR / "cv_occupancy_zone5_10sec.csv"
DEFAULT_SEN55_CSV = ZONE5_DATA_DIR / "sen55_data.csv"

if str(ZONE5_PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(ZONE5_PACKAGE_ROOT))

try:
    from zone5 import csv_size_guard
except Exception:  # pragma: no cover - direct pandas fallback for stripped deployments
    csv_size_guard = None


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


def _read_support_csv(path: str | Path) -> pd.DataFrame:
    csv_path = Path(path)
    if not csv_path.is_file():
        raise FileNotFoundError(f"Zone 5 support CSV not found: {csv_path}")
    if csv_size_guard is not None:
        return csv_size_guard.read_csv_parts(csv_path)
    return pd.read_csv(csv_path)


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


def seed_zone5_sen55_from_csv(
    *,
    csv_path: str | Path = DEFAULT_SEN55_CSV,
    rebuild: bool = False,
    start_timestamp: pd.Timestamp | None = None,
    end_timestamp: pd.Timestamp | None = None,
) -> dict[str, object]:
    raw = _read_support_csv(csv_path)
    if raw.empty:
        raise ValueError(f"{csv_path} is empty; cannot seed {migrated.SILVER_SEN55}")

    seeded = _filter_timestamp_range(
        raw,
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
        source_label="zone5_sen55",
    )
    if seeded.empty:
        raise ValueError("No SEN55 rows remain after timestamp filtering for silver.zone5_sen55")

    seeded = seeded.rename(
        columns={
            "pm_1_0": "pm1_0",
            "pm_2_5": "pm2_5",
            "pm_4_0": "pm4_0",
            "pm_10_0": "pm10_0",
        }
    )
    keep = ["timestamp", "pm1_0", "pm2_5", "pm4_0", "pm10_0", "temperature", "humidity", "voc", "nox"]
    for column in keep:
        if column not in seeded.columns:
            seeded[column] = pd.NA
    if "sensor_id" in seeded.columns:
        seeded = seeded.sort_values("timestamp").drop_duplicates(["timestamp", "sensor_id"], keep="last")
    else:
        seeded = seeded.sort_values("timestamp").drop_duplicates("timestamp", keep="last")
    for column in keep[1:]:
        seeded[column] = pd.to_numeric(seeded[column], errors="coerce")
    seeded = seeded.groupby("timestamp", as_index=False)[keep[1:]].mean(numeric_only=True)
    seeded = seeded[keep].sort_values("timestamp").reset_index(drop=True)

    _log(f"[zone5_sen55] writing {len(seeded)} CSV-backed rows to {migrated.SILVER_SEN55}")
    migrated.upsert_table_dataframe(seeded, migrated.SILVER_SEN55, key_columns=["timestamp"], rebuild=rebuild)

    return {
        "source_path": str(Path(csv_path)),
        "target_table": migrated.SILVER_SEN55,
        "rows": int(len(seeded)),
        "surrogate": False,
        "start": seeded["timestamp"].min().isoformat() if not seeded.empty else None,
        "end": seeded["timestamp"].max().isoformat() if not seeded.empty else None,
    }


def seed_zone5_cv_labels_from_csv(
    *,
    csv_path: str | Path = DEFAULT_CV_LABELS_CSV,
    rebuild: bool = False,
    occupied_threshold: float = 1.0,
    start_timestamp: pd.Timestamp | None = None,
    end_timestamp: pd.Timestamp | None = None,
) -> dict[str, object]:
    raw = _read_support_csv(csv_path)
    if raw.empty:
        raise ValueError(f"{csv_path} is empty; cannot seed {migrated.SILVER_CV_LABELS}")

    seeded = _filter_timestamp_range(
        raw,
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
        source_label="zone5_cv_labels",
    )
    if seeded.empty:
        raise ValueError("No CV-label rows remain after timestamp filtering for silver.zone5_cv_labels")

    if "occupancy_count" not in seeded.columns and "median_count" in seeded.columns:
        seeded["occupancy_count"] = seeded["median_count"]
    if "occupancy_count" not in seeded.columns:
        raise ValueError("CV labels are missing required column: occupancy_count")
    seeded["occupancy_count"] = pd.to_numeric(seeded["occupancy_count"], errors="coerce")

    target_source = None
    for candidate in (migrated.TARGET_COLUMN, "cv_is_occupied", "is_occupied"):
        if candidate in seeded.columns:
            target_source = candidate
            break
    if target_source is not None:
        seeded[migrated.TARGET_COLUMN] = pd.to_numeric(seeded[target_source], errors="coerce")
    else:
        seeded[migrated.TARGET_COLUMN] = (seeded["occupancy_count"] >= float(occupied_threshold)).astype(float)
    seeded["cv_is_occupied"] = pd.to_numeric(
        seeded.get("cv_is_occupied", seeded[migrated.TARGET_COLUMN]),
        errors="coerce",
    )

    for column in ["sample_count", "min_count", "max_count", "mean_count", "median_count", "last_count"]:
        if column not in seeded.columns:
            seeded[column] = pd.NA
        seeded[column] = pd.to_numeric(seeded[column], errors="coerce")
    for column in ["first_message_time", "last_message_time", "source_topic"]:
        if column not in seeded.columns:
            seeded[column] = None

    seeded = (
        seeded.sort_values("timestamp")
        .groupby("timestamp", as_index=False)
        .agg(
            occupancy_count=("occupancy_count", "median"),
            cv_is_occupied=("cv_is_occupied", "last"),
            zone_occupied=(migrated.TARGET_COLUMN, "last"),
            sample_count=("sample_count", "sum"),
            min_count=("min_count", "min"),
            max_count=("max_count", "max"),
            mean_count=("mean_count", "mean"),
            median_count=("median_count", "median"),
            last_count=("last_count", "last"),
            first_message_time=("first_message_time", "first"),
            last_message_time=("last_message_time", "last"),
            source_topic=("source_topic", "last"),
        )
        .sort_values("timestamp")
        .reset_index(drop=True)
    )

    _log(f"[zone5_cv_labels] writing {len(seeded)} CSV-backed rows to {migrated.SILVER_CV_LABELS}")
    migrated.upsert_table_dataframe(seeded, migrated.SILVER_CV_LABELS, key_columns=["timestamp"], rebuild=rebuild)

    return {
        "source_path": str(Path(csv_path)),
        "target_table": migrated.SILVER_CV_LABELS,
        "rows": int(len(seeded)),
        "surrogate": False,
        "start": seeded["timestamp"].min().isoformat() if not seeded.empty else None,
        "end": seeded["timestamp"].max().isoformat() if not seeded.empty else None,
    }


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
        "surrogate": True,
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
    parser = argparse.ArgumentParser(description="Seed missing Zone 5 support tables for the migrated BSG flow")
    parser.add_argument("--rebuild", action="store_true", help="Rebuild the target tables before writing seed rows")
    parser.add_argument("--cv-labels-csv", default=str(DEFAULT_CV_LABELS_CSV), help="CSV source for real Zone 5 CV labels")
    parser.add_argument("--sen55-csv", default=str(DEFAULT_SEN55_CSV), help="CSV source for real Zone 5 SEN55 readings")
    parser.add_argument("--occupied-threshold", type=float, default=1.0, help="Threshold used if CV labels need to be derived from occupancy_count")
    parser.add_argument("--smoke-surrogate", action="store_true", help="Use AG-One/mmWave surrogate support tables for smoke tests only")
    parser.add_argument("--ag-one-device-id", default=None, help="Override the AG-One device_id used with --smoke-surrogate")
    parser.add_argument("--mmwave-device-id", default=None, help="Override the MSR-2 device_id used with --smoke-surrogate")
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
        f"support_source={'smoke-surrogate' if args.smoke_surrogate else 'package-csv'}, "
        f"start={start_timestamp.isoformat() if start_timestamp is not None else 'min'}, "
        f"end={end_timestamp.isoformat() if end_timestamp is not None else 'latest'})"
    )

    if args.smoke_surrogate:
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
    else:
        sen55_report = seed_zone5_sen55_from_csv(
            csv_path=args.sen55_csv,
            rebuild=args.rebuild,
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
        )
        cv_report = seed_zone5_cv_labels_from_csv(
            csv_path=args.cv_labels_csv,
            rebuild=args.rebuild,
            occupied_threshold=args.occupied_threshold,
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
        )
    result: dict[str, object] = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "support_source": "smoke-surrogate" if args.smoke_surrogate else "package-csv",
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
