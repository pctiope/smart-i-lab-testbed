# =============================================================================
# parquet_restructure.py
# Phase 1 + Phase 5 utility
#
# Scans a source directory for .csv and .parquet files, converts / restructures
# them into a Hive-partitioned Parquet store, and optionally uploads to MinIO.
#
# Usage:
#   python parquet_restructure.py \
#       --source "D:\CoE 199\data_199" \
#       --dest   "data" \
#       --device-type air-1 \
#       [--upload]
#
# To query the resulting store use data_loader() from CSV Training Data Code.py
# =============================================================================

import argparse
import os
from datetime import datetime
from importlib import import_module as _import_module
from pathlib import Path

import pandas as pd

_storage = _import_module("CSV Training Data Code")
build_database_from_parquet = _storage.build_database_from_parquet

# ── MinIO config (mirrors CSV Training Data Code.py) ─────────────────────────
MINIO_ENDPOINT   = "http://localhost:9000"
MINIO_ACCESS_KEY = "minioadmin"
MINIO_SECRET_KEY = "minioadmin"
MINIO_BUCKET     = "smart-ilab-data"

SUPPORTED_DEVICE_TYPES = [
    "air-1", "msr-2", "smart-plug-v2", "ag-one", "zigbee2mqtt", "sensibo"
]

# ── Path helpers ──────────────────────────────────────────────────────────────

def _partition_dir(dest: Path, device_type: str, dt: datetime) -> Path:
    return (
        dest / device_type
        / f"year={dt.year}"
        / f"month={dt.month:02d}"
        / f"day={dt.day:02d}"
    )


def _infer_date(df: pd.DataFrame) -> datetime:
    """Try to extract the date from the first timestamp row; fall back to today."""
    if "timestamp" in df.columns and len(df) > 0:
        try:
            return pd.to_datetime(df["timestamp"].iloc[0]).to_pydatetime()
        except Exception:
            pass
    return datetime.now()


# ── I/O ───────────────────────────────────────────────────────────────────────

def save_parquet(df: pd.DataFrame, out_path: Path):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out_path, index=False, engine="pyarrow")
    print(f"  ✅ Saved  : {out_path}")


def upload_to_minio(local_path: Path, remote_key: str):
    try:
        from minio import Minio  # pip install minio
        client = Minio(
            MINIO_ENDPOINT.replace("http://", "").replace("https://", ""),
            access_key=MINIO_ACCESS_KEY,
            secret_key=MINIO_SECRET_KEY,
            secure=MINIO_ENDPOINT.startswith("https"),
        )
        if not client.bucket_exists(MINIO_BUCKET):
            client.make_bucket(MINIO_BUCKET)
        client.fput_object(MINIO_BUCKET, remote_key, str(local_path))
        print(f"  ☁️  Uploaded: s3://{MINIO_BUCKET}/{remote_key}")
    except ImportError:
        print("  ⚠️  minio not installed — skipping upload.  pip install minio")
    except Exception as e:
        print(f"  ❌ Upload error: {e}")


# ── Conversion logic ──────────────────────────────────────────────────────────

def convert_csv(src: Path, dest: Path, device_type: str, upload: bool):
    print(f"\nCSV → Parquet : {src.name}")
    try:
        df = pd.read_csv(src)
    except Exception as e:
        print(f"  ❌ Failed to read CSV: {e}")
        return

    dt       = _infer_date(df)
    out_path = _partition_dir(dest, device_type, dt) / src.with_suffix(".parquet").name
    save_parquet(df, out_path)

    if upload:
        remote_key = str(out_path.relative_to(dest)).replace("\\", "/")
        upload_to_minio(out_path, remote_key)


def restructure_parquet(src: Path, dest: Path, device_type: str, upload: bool):
    print(f"\nParquet → restructured : {src.name}")
    try:
        df = pd.read_parquet(src, engine="pyarrow")
    except Exception as e:
        print(f"  ❌ Failed to read Parquet: {e}")
        return

    dt       = _infer_date(df)
    out_path = _partition_dir(dest, device_type, dt) / src.name
    save_parquet(df, out_path)

    if upload:
        remote_key = str(out_path.relative_to(dest)).replace("\\", "/")
        upload_to_minio(out_path, remote_key)


# ── All-devices scan (Phase 5 helper) ────────────────────────────────────────

def restructure_all(source_root: Path, dest: Path, upload: bool):
    """
    Walk source_root looking for sub-folders named after known device types.
    If a sub-folder is found, restructure every CSV/Parquet inside it.
    Also processes any CSV/Parquet files directly in source_root as 'air-1'
    (the most common export format from CSV Training Data Code.py).
    """
    print("\n" + "="*60)
    print("RESTRUCTURING ALL DEVICE DATA")
    print("="*60)

    for device_type in SUPPORTED_DEVICE_TYPES:
        device_dir = source_root / device_type
        if device_dir.exists():
            _process_dir(device_dir, dest, device_type, upload)

    # Files directly in source root — treat as air-1 (training CSV exports)
    direct_csvs     = list(source_root.glob("*.csv"))
    direct_parquets = list(source_root.glob("*.parquet"))
    if direct_csvs or direct_parquets:
        print(f"\nFiles at root — treating as 'air-1'")
        for f in direct_csvs:
            convert_csv(f, dest, "air-1", upload)
        for f in direct_parquets:
            restructure_parquet(f, dest, "air-1", upload)


def _process_dir(src_dir: Path, dest: Path, device_type: str, upload: bool):
    csvs     = list(src_dir.rglob("*.csv"))
    parquets = list(src_dir.rglob("*.parquet"))
    print(f"\n{device_type}: {len(csvs)} CSV(s), {len(parquets)} Parquet(s)")
    for f in csvs:
        convert_csv(f, dest, device_type, upload)
    for f in parquets:
        restructure_parquet(f, dest, device_type, upload)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Convert/restructure CSV and Parquet files into a Hive-partitioned Parquet store"
    )
    parser.add_argument(
        "--source", required=True,
        help="Source directory containing CSV / Parquet files"
    )
    parser.add_argument(
        "--dest", required=True,
        help="Destination root directory for the Parquet store (local)"
    )
    parser.add_argument(
        "--device-type", default=None,
        choices=SUPPORTED_DEVICE_TYPES + ["all"],
        help="Device type to tag files with.  Use 'all' to auto-detect by sub-folder name."
    )
    parser.add_argument(
        "--upload", action="store_true",
        help="Upload converted Parquet files to MinIO after writing locally"
    )
    parser.add_argument(
        "--build-db", action="store_true",
        help="Materialize or refresh DuckDB tables from the destination Parquet store after conversion"
    )
    parser.add_argument(
        "--rebuild-db", action="store_true",
        help="Drop and recreate DuckDB tables from the destination Parquet store"
    )
    args = parser.parse_args()

    src  = Path(args.source)
    dest = Path(args.dest)

    if not src.exists():
        print(f"❌ Source directory not found: {src}")
        return

    if args.device_type == "all" or args.device_type is None:
        restructure_all(src, dest, args.upload)
        db_device_types = SUPPORTED_DEVICE_TYPES
    else:
        csv_files     = list(src.rglob("*.csv"))
        parquet_files = list(src.rglob("*.parquet"))

        print(f"\nSource      : {src}")
        print(f"Device type : {args.device_type}")
        print(f"Destination : {dest}")
        print(f"Upload      : {args.upload}")
        print(f"Found       : {len(csv_files)} CSV(s), {len(parquet_files)} Parquet(s)")
        print("="*60)

        for f in csv_files:
            convert_csv(f, dest, args.device_type, args.upload)
        for f in parquet_files:
            restructure_parquet(f, dest, args.device_type, args.upload)
        db_device_types = [args.device_type]

    if args.build_db:
        print("\n" + "=" * 60)
        print("BUILDING DUCKDB TABLES FROM PARQUET STORE")
        print("=" * 60)
        counts = build_database_from_parquet(db_device_types, rebuild=args.rebuild_db)
        for device_type, row_count in counts.items():
            print(f"{device_type:15s}: {row_count} row(s)")

    print("\n" + "="*60)
    print("✅ Restructure complete")


if __name__ == "__main__":
    main()
