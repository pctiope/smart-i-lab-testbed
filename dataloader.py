"""
dataloader.py — Silver layer DataLoader for ML training and analysis
====================================================================

Wraps the query_silver(), query_gold(), and training_data_loader() functions in
CSV Training Data Code.py with a clean, ergonomic class interface.

Example
-------
    from dataloader import DataLoader

    dl = DataLoader("air-1")

    # Latest 1000 rows
    df = dl.load_latest_n(1000)

    # All data between two dates
    from datetime import datetime
    df = dl.load_time_range(datetime(2024, 6, 1), datetime(2024, 6, 30))

    # Only temperature & RH columns for the last 24 h
    from datetime import datetime, timedelta
    since = datetime.now() - timedelta(hours=24)
    df = dl.load_since(since, columns=["timestamp", "temp_s1", "rh_s1"])

    # Specific sensors (narrow-format device types)
    dl_plug = DataLoader("smart-plug-v2")
    df = dl_plug.load_by_region(
        time_start=since,
        sensors=["device_abc", "device_def"],
    )

    # Raw SQL — use {table} as the silver-table placeholder
    df = dl.load_sql("SELECT timestamp, temp_s1 FROM {table} WHERE temp_s1 > 28")
"""

import os
from datetime import datetime
from importlib import import_module as _im

import pandas as pd

# ── Read-only mode is opt-in via DUCKDB_READ_ONLY=1 in the caller's
#    environment before this module is imported. ─────────────────────────────

# ── Storage layer ─────────────────────────────────────────────────────────────
_storage = _im("CSV Training Data Code")

DEVICE_TYPES         = _storage.DEVICE_TYPES
query_silver         = _storage.query_silver
query_gold           = _storage.query_gold
query_bronze         = _storage.query_bronze
query_table          = _storage.query_table
training_data_loader = _storage.training_data_loader
_db                  = _storage._db
_q                   = _storage._q
_silver_table        = _storage._silver_table
_gold_table          = _storage._gold_table
_bronze_table        = _storage._bronze_table
_table_exists        = _storage._table_exists
training_table_name  = _storage.training_table_name


class DataLoader:
    """
    High-level interface to the bronze, silver, and gold DuckDB layers.

    Parameters
    ----------
    default_device_type : str or None
        Device type used when device_type is omitted from method calls.
        One of: "air-1", "msr-2", "smart-plug-v2", "ag-one",
                "zigbee2mqtt", "sensibo"
    """

    def __init__(self, default_device_type: str | None = None):
        if default_device_type is not None and default_device_type not in DEVICE_TYPES:
            raise ValueError(
                f"Unknown device_type '{default_device_type}'. "
                f"Valid options: {DEVICE_TYPES}"
            )
        self.default_device_type = default_device_type

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _device(self, device_type: str | None) -> str:
        dt = device_type or self.default_device_type
        if dt is None:
            raise ValueError("device_type must be provided either at construction or call time.")
        if dt not in DEVICE_TYPES:
            raise ValueError(f"Unknown device_type '{dt}'. Valid: {DEVICE_TYPES}")
        return dt

    # ── Query methods ─────────────────────────────────────────────────────────

    def load_latest_n(
        self,
        n: int,
        device_type: str | None = None,
        columns: list[str] | None = None,
        layer: str = "silver",
    ) -> pd.DataFrame:
        """Return the latest `n` rows, ordered by timestamp."""
        dt = self._device(device_type)
        if layer == "bronze":
            return query_bronze(device_type=dt, latest_n=n, columns=columns)
        if layer == "gold":
            return query_gold(device_type=dt, latest_n=n, columns=columns)
        return query_silver(device_type=dt, latest_n=n, columns=columns)

    def load_since(
        self,
        time_start: datetime,
        device_type: str | None = None,
        columns: list[str] | None = None,
        layer: str = "silver",
    ) -> pd.DataFrame:
        """Return all rows with timestamp >= time_start, ordered by timestamp."""
        dt = self._device(device_type)
        if layer == "bronze":
            return query_bronze(device_type=dt, time_start=time_start, columns=columns)
        if layer == "gold":
            return query_gold(device_type=dt, time_start=time_start, columns=columns)
        return query_silver(device_type=dt, time_start=time_start, columns=columns)

    def load_time_range(
        self,
        time_start: datetime,
        time_end: datetime,
        device_type: str | None = None,
        columns: list[str] | None = None,
        layer: str = "silver",
    ) -> pd.DataFrame:
        """Return rows within [time_start, time_end], ordered by timestamp."""
        dt = self._device(device_type)
        if layer == "bronze":
            return query_bronze(device_type=dt, time_start=time_start, time_end=time_end, columns=columns)
        if layer == "gold":
            return query_gold(device_type=dt, time_start=time_start, time_end=time_end, columns=columns)
        return query_silver(device_type=dt, time_start=time_start, time_end=time_end, columns=columns)

    def load_by_region(
        self,
        time_start: datetime | None = None,
        time_end: datetime | None = None,
        sensors: list[str] | None = None,
        device_type: str | None = None,
        columns: list[str] | None = None,
        layer: str = "silver",
    ) -> pd.DataFrame:
        """Return rows filtered by optional time window and/or sensor list."""
        dt = self._device(device_type)
        if layer == "bronze":
            return query_bronze(device_type=dt, time_start=time_start, time_end=time_end,
                                sensors=sensors, columns=columns)
        if layer == "gold":
            return query_gold(device_type=dt, time_start=time_start, time_end=time_end,
                              sensors=sensors, columns=columns)
        return query_silver(device_type=dt, time_start=time_start, time_end=time_end,
                            sensors=sensors, columns=columns)

    def load_all(
        self,
        device_type: str | None = None,
        columns: list[str] | None = None,
        layer: str = "silver",
    ) -> pd.DataFrame:
        """Return the full table ordered by timestamp."""
        dt = self._device(device_type)
        if layer == "bronze":
            return query_bronze(device_type=dt, columns=columns, limit=10_000_000)
        if layer == "gold":
            return query_gold(device_type=dt, columns=columns)
        return query_silver(device_type=dt, columns=columns)

    def load_sql(self, sql: str, device_type: str | None = None, layer: str = "silver") -> pd.DataFrame:
        """
        Execute a raw SQL query against the selected table.

        Use `{table}` as the placeholder for the resolved table name.

        Example
        -------
        df = dl.load_sql("SELECT timestamp, temp_s1 FROM {table} WHERE temp_s1 > 28")
        """
        dt         = self._device(device_type)
        if layer == "bronze":
            table_name = _bronze_table(dt)
        elif layer == "gold":
            table_name = _gold_table(dt)
        else:
            table_name = _silver_table(dt)
        if not _table_exists(table_name):
            print(f"[DataLoader] Table {table_name} does not exist.")
            return pd.DataFrame()
        resolved_sql = sql.format(table=_q(table_name))
        try:
            return _db.execute(resolved_sql).df()
        except Exception as exc:
            print(f"[DataLoader] SQL error: {exc}")
            return pd.DataFrame()

    def load_table(
        self,
        table_name: str,
        *,
        columns: list[str] | None = None,
        time_start: datetime | None = None,
        time_end: datetime | None = None,
        latest_n: int | None = None,
        timestamp_column: str | None = "timestamp",
        filters: dict | None = None,
        order_by: str | None = None,
    ) -> pd.DataFrame:
        """Read an arbitrary DuckDB table, including migrated training tables."""
        return query_table(
            table_name,
            columns=columns,
            time_start=time_start,
            time_end=time_end,
            latest_n=latest_n,
            timestamp_column=timestamp_column,
            filters=filters,
            order_by=order_by,
        )

    def load_training_table(
        self,
        pipeline_name: str,
        dataset_name: str,
        *,
        layer: str = "silver",
        columns: list[str] | None = None,
        time_start: datetime | None = None,
        time_end: datetime | None = None,
        latest_n: int | None = None,
        timestamp_column: str | None = "timestamp",
        filters: dict | None = None,
        order_by: str | None = None,
    ) -> pd.DataFrame:
        """Read a migrated training table by its logical pipeline and dataset names."""
        return self.load_table(
            training_table_name(pipeline_name, dataset_name, layer=layer),
            columns=columns,
            time_start=time_start,
            time_end=time_end,
            latest_n=latest_n,
            timestamp_column=timestamp_column,
            filters=filters,
            order_by=order_by,
        )

    def load_training_config(self, config: dict) -> pd.DataFrame:
        """
        Config-driven loader (thin wrapper around training_data_loader).

        Merges the DataLoader's default_device_type if not in config.

        Config keys
        -----------
        device_type : str       — optional if default_device_type is set
        sql         : str       — optional raw SQL (use {table} as placeholder)
        time_start  : datetime
        time_end    : datetime
        sensors     : list[str]
        columns     : list[str]
        latest_n    : int
        """
        full_config = dict(config)
        if "device_type" not in full_config:
            full_config["device_type"] = self._device(None)
        return training_data_loader(full_config)

    # ── Introspection ─────────────────────────────────────────────────────────

    def available_tables(self, layer: str = "silver") -> list[str]:
        """Return table names that exist in DuckDB for the given layer."""
        fn = _bronze_table if layer == "bronze" else (_gold_table if layer == "gold" else _silver_table)
        return [fn(dt) for dt in DEVICE_TYPES if _table_exists(fn(dt))]

    def row_count(self, device_type: str | None = None, layer: str = "silver") -> int:
        """Return total rows in the table for this device type and layer."""
        dt         = self._device(device_type)
        table_name = _bronze_table(dt) if layer == "bronze" else (_gold_table(dt) if layer == "gold" else _silver_table(dt))
        if not _table_exists(table_name):
            return 0
        return _storage._db.execute(f"SELECT COUNT(*) FROM {_q(table_name)}").fetchone()[0]

    def column_names(self, device_type: str | None = None, layer: str = "silver") -> list[str]:
        """Return column names for the table."""
        dt         = self._device(device_type)
        table_name = _bronze_table(dt) if layer == "bronze" else (_gold_table(dt) if layer == "gold" else _silver_table(dt))
        if not _table_exists(table_name):
            return []
        return [col[0] for col in _storage._db.execute(
            f"DESCRIBE {_q(table_name)}"
        ).fetchall()]

    def __repr__(self) -> str:
        tables = self.available_tables(layer="bronze")
        return (
            f"DataLoader(default_device_type={self.default_device_type!r}, "
            f"bronze_tables={tables})"
        )


# =============================================================================
# CLI  — run alongside the live ingest pipeline (read-only, non-blocking)
# =============================================================================
# Usage (PowerShell — Terminal 2 while api_ingestion.py polls in Terminal 1):
#
#   $env:DUCKDB_READ_ONLY = "1"
#   .venv\Scripts\python.exe dataloader.py --device-type air-1 --latest 20
#   .venv\Scripts\python.exe dataloader.py --device-type air-1 --since "2026-05-11 10:00"
#   .venv\Scripts\python.exe dataloader.py --device-type air-1 --from "10:00" --to "13:00"
#   .venv\Scripts\python.exe dataloader.py --device-type smart-plug-v2 --latest 50 --columns timestamp,device_id,power
#   .venv\Scripts\python.exe dataloader.py --summary
# =============================================================================

if __name__ == "__main__":
    import argparse
    import sys
    from datetime import datetime, timedelta

    parser = argparse.ArgumentParser(
        description="Query the Smart i-Lab DuckDB bronze/silver/gold layer.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples (run from d:\\Phys 231 with DUCKDB_READ_ONLY=1 set):
  --summary                              show row counts for all tables
  --device-type air-1 --latest 20        last 20 rows from bronze_air_1
  --device-type air-1 --since "2026-05-11 10:00"
  --device-type air-1 --from "10:00" --to "13:00"
    --device-type smart-plug-v2 --latest 50 --columns timestamp,device_id,power
    --device-type air-1 --layer silver --latest 10
    --device-type air-1 --layer gold --latest 10
""",
    )
    parser.add_argument("--device-type", choices=DEVICE_TYPES, help="Device type to query")
    parser.add_argument("--layer", choices=["bronze", "silver", "gold"], default="silver",
                        help="DB layer to query (default: silver — preprocessed, falls back to Parquet in read-only mode)")
    parser.add_argument("--latest", type=int, metavar="N", help="Return the N most recent rows")
    parser.add_argument("--since", metavar="DATETIME",
                        help='Return rows since this timestamp, e.g. "2026-05-11 10:00"')
    parser.add_argument("--from", dest="from_dt", metavar="DATETIME",
                        help="Time-range start (use with --to)")
    parser.add_argument("--to", dest="to_dt", metavar="DATETIME",
                        help="Time-range end (use with --from)")
    parser.add_argument("--columns", metavar="col1,col2,...",
                        help="Comma-separated column list to project")
    parser.add_argument("--sensors", metavar="id1,id2,...",
                        help="Comma-separated device_id list (narrow-format types)")
    parser.add_argument("--summary", action="store_true",
                        help="Print row counts for all bronze, silver, and gold tables and exit")
    args = parser.parse_args()

    # ── Summary mode ─────────────────────────────────────────────────────────
    if args.summary:
        import glob as _glob_mod
        print(f"\n{'Table':<35} {'Rows':>10}  Source")
        print("-" * 58)
        for dt in DEVICE_TYPES:
            if _storage._read_only:
                # Read-only: silver falls back to Parquet (same data as bronze)
                silver_tbl = _silver_table(dt)
                pattern    = _storage._parquet_glob(dt)
                files      = _glob_mod.glob(pattern, recursive=True)
                if files:
                    n = len(_storage._query_parquet_direct(dt))
                    print(f"  {silver_tbl:<33} {n:>10,}  silver (Parquet fallback)")
                else:
                    print(f"  {silver_tbl:<33} {'—':>10}  silver (no Parquet files)")
            else:
                # Write-mode: show actual bronze, silver, and gold table counts
                bronze_tbl = _bronze_table(dt)
                silver_tbl = _silver_table(dt)
                gold_tbl = _gold_table(dt)
                if _table_exists(bronze_tbl):
                    n = _storage._db.execute(f"SELECT COUNT(*) FROM {_q(bronze_tbl)}").fetchone()[0]
                    print(f"  {bronze_tbl:<33} {n:>10,}  bronze")
                else:
                    print(f"  {bronze_tbl:<33} {'—':>10}  bronze (not created)")
                if _table_exists(silver_tbl):
                    n = _storage._db.execute(f"SELECT COUNT(*) FROM {_q(silver_tbl)}").fetchone()[0]
                    print(f"  {silver_tbl:<33} {n:>10,}  silver")
                else:
                    print(f"  {silver_tbl:<33} {'—':>10}  silver (not created)")
                if _table_exists(gold_tbl):
                    n = _storage._db.execute(f"SELECT COUNT(*) FROM {_q(gold_tbl)}").fetchone()[0]
                    print(f"  {gold_tbl:<33} {n:>10,}  gold")
                else:
                    print(f"  {gold_tbl:<33} {'—':>10}  gold (not created)")
        print()
        sys.exit(0)

    if not args.device_type:
        parser.error("--device-type is required (or use --summary)")

    # ── Parse optional filters ────────────────────────────────────────────────
    cols    = [c.strip() for c in args.columns.split(",")] if args.columns else None
    sensors = [s.strip() for s in args.sensors.split(",")] if args.sensors else None

    def _parse_dt(s: str) -> datetime:
        today = datetime.now().date()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                    "%H:%M:%S", "%H:%M"):
            try:
                dt_val = datetime.strptime(s, fmt)
                # If only time was given, attach today's date
                if fmt in ("%H:%M:%S", "%H:%M"):
                    dt_val = dt_val.replace(year=today.year, month=today.month, day=today.day)
                return dt_val
            except ValueError:
                continue
        parser.error(f"Cannot parse datetime: '{s}' — use 'YYYY-MM-DD HH:MM' or 'HH:MM'")

    loader = DataLoader(args.device_type)

    # ── Query dispatch ────────────────────────────────────────────────────────
    if args.latest:
        df = loader.load_latest_n(args.latest, columns=cols, layer=args.layer)
    elif args.since:
        df = loader.load_since(_parse_dt(args.since), columns=cols, layer=args.layer)
    elif args.from_dt and args.to_dt:
        df = loader.load_time_range(_parse_dt(args.from_dt), _parse_dt(args.to_dt),
                                    columns=cols, layer=args.layer)
    elif args.sensors:
        df = loader.load_by_region(sensors=sensors, columns=cols, layer=args.layer)
    else:
        df = loader.load_all(columns=cols, layer=args.layer)

    # Apply sensor filter if combined with time args
    if sensors and (args.since or args.from_dt):
        df = loader.load_by_region(
            time_start=_parse_dt(args.since) if args.since else (_parse_dt(args.from_dt) if args.from_dt else None),
            time_end=_parse_dt(args.to_dt) if args.to_dt else None,
            sensors=sensors, columns=cols, layer=args.layer,
        )

    # ── Output ────────────────────────────────────────────────────────────────
    tbl_name = _bronze_table(args.device_type) if args.layer == "bronze" else (_gold_table(args.device_type) if args.layer == "gold" else _silver_table(args.device_type))
    print(f"\n[{tbl_name}]  {len(df):,} rows returned\n")
    if df.empty:
        print("  (no data — check device-type and time range)")
    else:
        with pd.option_context("display.max_rows", 50, "display.max_columns", 20,
                               "display.width", 140, "display.float_format", "{:.3f}".format):
            print(df.to_string(index=False))
    print()
