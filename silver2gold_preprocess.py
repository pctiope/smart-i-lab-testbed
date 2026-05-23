"""
silver2gold_preprocess.py — Silver → Gold preprocessing pipeline
================================================================

For each device type:
1. Pull the full silver DuckDB table via SQL.
2. Apply the Python preprocessing pipeline (placeholder — currently copies silver).
3. Write the curated DataFrame to the gold DuckDB table.

For now, bronze = silver = gold in terms of data content. The separation exists
so downstream consumers can already target a gold layer without changing the
ingestion path later when real gold transformations are introduced.

Usage
-----
    python silver2gold_preprocess.py                # all device types
    python silver2gold_preprocess.py --device-type air-1
    python silver2gold_preprocess.py --rebuild      # drop & recreate gold tables
"""

import argparse
from importlib import import_module as _im

import pandas as pd

_storage = _im("CSV Training Data Code")

DEVICE_TYPES = _storage.DEVICE_TYPES
_db = _storage._db
_q = _storage._q
_silver_table = _storage._silver_table
_gold_table = _storage._gold_table
_table_exists = _storage._table_exists
_register_df = _storage._register_df


def preprocess_air1(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df.sort_values("timestamp").reset_index(drop=True)


def preprocess_msr2(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df.sort_values("timestamp").reset_index(drop=True)


def preprocess_smart_plug_v2(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df.sort_values(["timestamp", "device_id"]).reset_index(drop=True)


def preprocess_ag_one(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df.sort_values("timestamp").reset_index(drop=True)


def preprocess_zigbee2mqtt(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df.sort_values(["timestamp", "device_id"]).reset_index(drop=True)


def preprocess_sensibo(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df.sort_values(["timestamp", "device_id"]).reset_index(drop=True)


PREPROCESS_FN = {
    "air-1": preprocess_air1,
    "msr-2": preprocess_msr2,
    "smart-plug-v2": preprocess_smart_plug_v2,
    "ag-one": preprocess_ag_one,
    "zigbee2mqtt": preprocess_zigbee2mqtt,
    "sensibo": preprocess_sensibo,
}


def run_zone5_training_postprocess(rebuild: bool = False) -> pd.DataFrame:
    """
    Promote the migrated Zone 5 training output from Silver to Gold.

    No extra post-processing is currently defined, so this copies only the
    output dump table to Gold and intentionally leaves intermediate tables in
    Silver.
    """
    migrated = _im("zone5_training_migrated")
    print("\n[zone5] Copying migrated training output silver -> gold ...")
    frame = migrated.copy_training_output_to_gold(rebuild=rebuild)
    print(f"  [zone5] {migrated.GOLD_TRAINING_OUTPUT}: {len(frame):,} rows")
    return frame


def _push_to_gold(df: pd.DataFrame, device_type: str, rebuild: bool):
    """Write a curated DataFrame into the gold DuckDB table."""
    from uuid import uuid4

    gold_name = _gold_table(device_type)
    quoted_gold = _q(gold_name)

    if rebuild and _table_exists(gold_name):
        _db.execute(f"DROP TABLE {quoted_gold}")
        print(f"  [gold] Dropped {gold_name}")

    temp = f"gold_in_{uuid4().hex}"
    _register_df(temp, df)
    try:
        if not _table_exists(gold_name):
            _db.execute(f"CREATE TABLE {quoted_gold} AS SELECT * FROM {_q(temp)}")
            print(f"  [gold] Created {gold_name}: {len(df):,} rows")
        else:
            has_device_id = "device_id" in df.columns
            if has_device_id:
                sql = (
                    f"INSERT INTO {quoted_gold} BY NAME "
                    f"SELECT * FROM {_q(temp)} i "
                    f"WHERE NOT EXISTS ("
                    f"  SELECT 1 FROM {quoted_gold} e "
                    f"  WHERE e.timestamp = i.timestamp AND e.device_id = i.device_id)"
                )
            else:
                sql = (
                    f"INSERT INTO {quoted_gold} BY NAME "
                    f"SELECT * FROM {_q(temp)} i "
                    f"WHERE NOT EXISTS ("
                    f"  SELECT 1 FROM {quoted_gold} e WHERE e.timestamp = i.timestamp)"
                )
            before = _db.execute(f"SELECT COUNT(*) FROM {quoted_gold}").fetchone()[0]
            _db.execute(sql)
            after = _db.execute(f"SELECT COUNT(*) FROM {quoted_gold}").fetchone()[0]
            print(f"  [gold] {gold_name}: +{after - before:,} rows (total {after:,})")
    finally:
        _db.unregister(temp)


def run_silver_to_gold(device_type: str, rebuild: bool = False):
    """Pull the full silver table for a device type, preprocess, and push to gold."""
    silver_name = _silver_table(device_type)
    if not _table_exists(silver_name):
        print(f"[{device_type}] Silver table {silver_name} does not exist — run bronze2silver_preprocess.py first")
        return

    print(f"\n[{device_type}] Reading {silver_name} ...")
    df_silver = _db.execute(
        f"SELECT * FROM {_q(silver_name)} ORDER BY timestamp"
    ).df()
    print(f"  [silver] {len(df_silver):,} rows loaded")

    if df_silver.empty:
        print("  [silver] Empty — skipping")
        return

    preprocess_fn = PREPROCESS_FN.get(device_type)
    if preprocess_fn is None:
        print(f"  [preprocess] No gold preprocessing registered for {device_type} — writing as-is")
        df_gold = df_silver
    else:
        print(f"  [preprocess] Applying {preprocess_fn.__name__} ...")
        df_gold = preprocess_fn(df_silver)
        print(f"  [preprocess] {len(df_silver):,} -> {len(df_gold):,} rows after preprocessing")

    _push_to_gold(df_gold, device_type, rebuild=rebuild)


def main():
    parser = argparse.ArgumentParser(description="Silver -> Gold preprocessing pipeline")
    parser.add_argument("--device-type", default=None, choices=DEVICE_TYPES,
                        help="Process a single device type (default: all)")
    parser.add_argument("--rebuild", action="store_true",
                        help="Drop and recreate gold tables from scratch")
    args = parser.parse_args()

    device_types = [args.device_type] if args.device_type else DEVICE_TYPES

    print("=" * 60)
    print("Silver -> Gold Preprocessing Pipeline")
    print("=" * 60)

    for device_type in device_types:
        run_silver_to_gold(device_type, rebuild=args.rebuild)

    print("\n" + "=" * 60)
    print("Gold layer summary:")
    for device_type in device_types:
        gold_name = _gold_table(device_type)
        if _table_exists(gold_name):
            count = _db.execute(f"SELECT COUNT(*) FROM {_q(gold_name)}").fetchone()[0]
            print(f"  {gold_name:30s}  {count:>10,} rows")
        else:
            print(f"  {gold_name:30s}  (not built)")


if __name__ == "__main__":
    main()