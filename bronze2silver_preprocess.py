"""
bronze2silver_preprocess.py — Bronze → Silver preprocessing pipeline
=====================================================================

For each device type:
1.  Pull the full bronze DuckDB table via SQL.
2.  Apply the Python preprocessing pipeline (placeholder — fill in per device).
3.  Write the cleaned DataFrame to the silver DuckDB table.

Run this after api_ingestion.py to keep the silver layer current.

Usage
-----
    python bronze2silver_preprocess.py                # all device types
    python bronze2silver_preprocess.py --device-type air-1
    python bronze2silver_preprocess.py --rebuild      # drop & recreate silver tables
"""

import argparse
from importlib import import_module as _im

import pandas as pd

# ── Storage layer ─────────────────────────────────────────────────────────────
_storage = _im("CSV Training Data Code")

DEVICE_TYPES  = _storage.DEVICE_TYPES
_db           = _storage._db
_q            = _storage._q
_bronze_table = _storage._bronze_table
_silver_table = _storage._silver_table
_table_exists = _storage._table_exists
_register_df  = _storage._register_df


# =============================================================================
# Per-device preprocessing steps (PLACEHOLDERS — implement as needed)
# =============================================================================

def preprocess_air1(df: pd.DataFrame) -> pd.DataFrame:
    """
    Preprocessing pipeline for bronze_air_1 → silver_air_1.

    Placeholder — currently copies bronze as-is.
    Add real steps here (outlier clipping, forward-fill, resampling, etc.)
    """
    df = df.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.sort_values("timestamp").reset_index(drop=True)
    return df


def preprocess_msr2(df: pd.DataFrame) -> pd.DataFrame:
    """Preprocessing pipeline for bronze_msr_2 → silver_msr_2. PLACEHOLDER."""
    df = df.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.sort_values("timestamp").reset_index(drop=True)
    # TODO: add MSR-2-specific cleaning steps
    return df


def preprocess_smart_plug_v2(df: pd.DataFrame) -> pd.DataFrame:
    """Preprocessing pipeline for bronze_smart_plug_v2 → silver_smart_plug_v2. PLACEHOLDER."""
    df = df.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.sort_values(["timestamp", "device_id"]).reset_index(drop=True)
    # TODO: add smart-plug-specific cleaning (e.g., negative power → 0)
    return df


def preprocess_ag_one(df: pd.DataFrame) -> pd.DataFrame:
    """Preprocessing pipeline for bronze_ag_one → silver_ag_one. PLACEHOLDER."""
    df = df.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.sort_values("timestamp").reset_index(drop=True)
    return df


def preprocess_zigbee2mqtt(df: pd.DataFrame) -> pd.DataFrame:
    """Preprocessing pipeline for bronze_zigbee2mqtt → silver_zigbee2mqtt. PLACEHOLDER."""
    df = df.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.sort_values(["timestamp", "device_id"]).reset_index(drop=True)
    return df


def preprocess_sensibo(df: pd.DataFrame) -> pd.DataFrame:
    """Preprocessing pipeline for bronze_sensibo → silver_sensibo. PLACEHOLDER."""
    df = df.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.sort_values(["timestamp", "device_id"]).reset_index(drop=True)
    return df


# Map device_type → preprocessing function
PREPROCESS_FN = {
    "air-1":         preprocess_air1,
    "msr-2":         preprocess_msr2,
    "smart-plug-v2": preprocess_smart_plug_v2,
    "ag-one":        preprocess_ag_one,
    "zigbee2mqtt":   preprocess_zigbee2mqtt,
    "sensibo":       preprocess_sensibo,
}


def run_zone5_training_preprocess(rebuild: bool = False) -> pd.DataFrame:
    """
    Build the migrated Zone 5 training input in Silver from BSG tables only.

    This is the root-side replacement for the old CSV-based training-input join.
    """
    migrated = _im("zone5_training_migrated")
    print("\n[zone5] Building migrated training input in silver ...")
    frame = migrated.build_zone5_training_input_from_silver(rebuild=rebuild)
    print(f"  [zone5] {migrated.SILVER_TRAINING_INPUT}: {len(frame):,} rows")
    return frame


# =============================================================================
# Bronze → Silver write
# =============================================================================

def _push_to_silver(df: pd.DataFrame, device_type: str, rebuild: bool):
    """Write a preprocessed DataFrame into the silver DuckDB table."""
    from uuid import uuid4

    silver_name   = _silver_table(device_type)
    quoted_silver = _q(silver_name)

    if rebuild and _table_exists(silver_name):
        _db.execute(f"DROP TABLE {quoted_silver}")
        print(f"  [silver] Dropped {silver_name}")

    temp = f"silver_in_{uuid4().hex}"
    _register_df(temp, df)
    try:
        if not _table_exists(silver_name):
            _db.execute(f"CREATE TABLE {quoted_silver} AS SELECT * FROM {_q(temp)}")
            print(f"  [silver] Created {silver_name}: {len(df):,} rows")
        else:
            # Upsert: insert only rows not already present by timestamp
            has_device_id = "device_id" in df.columns
            if has_device_id:
                sql = (
                    f"INSERT INTO {quoted_silver} BY NAME "
                    f"SELECT * FROM {_q(temp)} i "
                    f"WHERE NOT EXISTS ("
                    f"  SELECT 1 FROM {quoted_silver} e "
                    f"  WHERE e.timestamp = i.timestamp AND e.device_id = i.device_id)"
                )
            else:
                sql = (
                    f"INSERT INTO {quoted_silver} BY NAME "
                    f"SELECT * FROM {_q(temp)} i "
                    f"WHERE NOT EXISTS ("
                    f"  SELECT 1 FROM {quoted_silver} e WHERE e.timestamp = i.timestamp)"
                )
            before = _db.execute(f"SELECT COUNT(*) FROM {quoted_silver}").fetchone()[0]
            _db.execute(sql)
            after  = _db.execute(f"SELECT COUNT(*) FROM {quoted_silver}").fetchone()[0]
            print(f"  [silver] {silver_name}: +{after - before:,} rows (total {after:,})")
    finally:
        _db.unregister(temp)


# =============================================================================
# Main pipeline
# =============================================================================

def run_bronze_to_silver(device_type: str, rebuild: bool = False):
    """
    Pull the full bronze table for device_type, preprocess it, push to silver.
    """
    bronze_name = _bronze_table(device_type)
    if not _table_exists(bronze_name):
        print(f"[{device_type}] Bronze table {bronze_name} does not exist — run api_ingestion.py first")
        return

    # ── Step 1: Read bronze via SQL ───────────────────────────────────────────
    print(f"\n[{device_type}] Reading {bronze_name} …")
    df_bronze = _db.execute(
        f"SELECT * FROM {_q(bronze_name)} ORDER BY timestamp"
    ).df()
    print(f"  [bronze] {len(df_bronze):,} rows loaded")

    if df_bronze.empty:
        print(f"  [bronze] Empty — skipping")
        return

    # ── Step 2: Apply preprocessing ───────────────────────────────────────────
    preprocess_fn = PREPROCESS_FN.get(device_type)
    if preprocess_fn is None:
        print(f"  [preprocess] No preprocessing function registered for {device_type} — writing as-is")
        df_silver = df_bronze
    else:
        print(f"  [preprocess] Applying {preprocess_fn.__name__} …")
        df_silver = preprocess_fn(df_bronze)
        print(f"  [preprocess] {len(df_bronze):,} -> {len(df_silver):,} rows after preprocessing")

    # ── Step 3: Push to silver ────────────────────────────────────────────────
    _push_to_silver(df_silver, device_type, rebuild=rebuild)


def main():
    parser = argparse.ArgumentParser(description="Bronze -> Silver preprocessing pipeline")
    parser.add_argument("--device-type", default=None, choices=DEVICE_TYPES,
                        help="Process a single device type (default: all)")
    parser.add_argument("--rebuild", action="store_true",
                        help="Drop and recreate silver tables from scratch")
    args = parser.parse_args()

    device_types = [args.device_type] if args.device_type else DEVICE_TYPES

    print("=" * 60)
    print("Bronze -> Silver Preprocessing Pipeline")
    print("=" * 60)

    for device_type in device_types:
        run_bronze_to_silver(device_type, rebuild=args.rebuild)

    print("\n" + "=" * 60)
    print("Silver layer summary:")
    for device_type in device_types:
        silver_name = _silver_table(device_type)
        if _table_exists(silver_name):
            count = _db.execute(f'SELECT COUNT(*) FROM "{silver_name}"').fetchone()[0]
            print(f"  {silver_name:30s}  {count:>10,} rows")
        else:
            print(f"  {silver_name:30s}  (not built)")


if __name__ == "__main__":
    main()
