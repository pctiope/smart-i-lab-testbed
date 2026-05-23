# =============================================================================
# CSV Training Data Code.py
# Bronze-Silver-Gold storage and query layer
# =============================================================================
#
# Layer layout in smart_ilab.duckdb:
#   Bronze  : bronze.<device>  — raw API data, exact representation from ingest
#   Silver  : silver.<device>  — preprocessed / cleaned, ready for training
#   Gold    : gold.<device>    — curated / serving layer (placeholder copy of silver)
#
# USE_REMOTE=True routes Parquet reads/writes through MinIO instead of local disk.
# =============================================================================

import os
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import duckdb
import pandas as pd

# ── Storage config ────────────────────────────────────────────────────────────
USE_REMOTE      = False
LOCAL_DATA_PATH = Path("data")
DUCKDB_PATH     = Path("smart_ilab.duckdb")
LOCAL_STAGE_PATH = Path("stage")
LOCAL_TRAINING_TABLE_PATH = LOCAL_DATA_PATH / "_training_tables"

# MinIO / S3 credentials — only used when USE_REMOTE=True
S3_ENDPOINT   = "http://localhost:9000"
S3_ACCESS_KEY = "minioadmin"
S3_SECRET_KEY = "minioadmin"
S3_BUCKET     = "smart-ilab-data"
S3_REGION     = "us-east-1"

# ── Sensor / device metadata ──────────────────────────────────────────────────
SENSOR_ORDER = [
    "88e4c8", "88e590", "89e8d8", "889720", "87f510",
    "2da640", "89ea14", "889b88", "889938", "88e85c",
    "89e548", "88970c", "2deb24", "89e5f0", "cc8f24",
]

DEVICE_TO_POSITION = {dev: idx + 1 for idx, dev in enumerate(SENSOR_ORDER)}

DEVICE_TYPES = [
    "air-1", "msr-2", "smart-plug-v2", "ag-one", "zigbee2mqtt", "sensibo",
]

DEFAULT_STAGE_MAX_ROWS        = 500
DEFAULT_STAGE_MAX_AGE_SECONDS = 300

# ── DuckDB connection ─────────────────────────────────────────────────────────
# Set DUCKDB_READ_ONLY=1 before importing to avoid competing for the file lock.
# In that mode we use an in-memory connection and read from Parquet files
# directly — safe for concurrent access alongside the live ingest pipeline.
_read_only = os.environ.get("DUCKDB_READ_ONLY") == "1"
_db = duckdb.connect(":memory:" if _read_only else str(DUCKDB_PATH))


def _configure_duckdb_s3():
    _db.execute("INSTALL httpfs; LOAD httpfs;")
    endpoint = S3_ENDPOINT.replace("http://", "").replace("https://", "")
    use_ssl  = S3_ENDPOINT.startswith("https")
    _db.execute(f"""
        SET s3_endpoint='{endpoint}';
        SET s3_access_key_id='{S3_ACCESS_KEY}';
        SET s3_secret_access_key='{S3_SECRET_KEY}';
        SET s3_region='{S3_REGION}';
        SET s3_use_ssl={'true' if use_ssl else 'false'};
        SET s3_url_style='path';
    """)


if USE_REMOTE:
    _configure_duckdb_s3()


# =============================================================================
# Naming helpers
# =============================================================================

ROOT_SCHEMAS = ("bronze", "silver", "gold")

def _bronze_table(device_type: str) -> str:
    """bronze.air_1, bronze.msr_2, …"""
    return "bronze." + device_type.replace("-", "_")


def _silver_table(device_type: str) -> str:
    """silver.air_1, silver.msr_2, …"""
    return "silver." + device_type.replace("-", "_")


def _gold_table(device_type: str) -> str:
    """gold.air_1, gold.msr_2, …"""
    return "gold." + device_type.replace("-", "_")


def _sanitize_identifier_component(value: str) -> str:
    """Convert an arbitrary label into a DuckDB-safe identifier component."""
    sanitized = "".join(ch if ch.isalnum() else "_" for ch in str(value).strip().lower())
    sanitized = sanitized.strip("_")
    if not sanitized:
        raise ValueError("Identifier component cannot be empty")
    return sanitized


def training_table_name(pipeline_name: str, dataset_name: str, layer: str = "silver") -> str:
    """
    Resolve a migrated training table name inside the silver or gold schema.

    Examples
    --------
    training_table_name("zone5", "training_input")
      -> "silver.zone5_training_input"
    training_table_name("zone5", "model_output", layer="gold")
      -> "gold.zone5_model_output"
    """
    if layer not in {"silver", "gold"}:
        raise ValueError("training_table_name layer must be 'silver' or 'gold'")
    pipeline = _sanitize_identifier_component(pipeline_name)
    dataset = _sanitize_identifier_component(dataset_name)
    return f"{layer}.{pipeline}_{dataset}"


def _q(name: str) -> str:
    """Quote a DuckDB identifier, handling optional schema.table notation."""
    parts = name.split(".")
    return ".".join('"' + p.replace('"', '""') + '"' for p in parts)


def _local_glob(device_type: str) -> str:
    return str(LOCAL_DATA_PATH / device_type / "**" / "*.parquet")


def _s3_glob(device_type: str) -> str:
    return f"s3://{S3_BUCKET}/{device_type}/**/*.parquet"


def _parquet_glob(device_type: str) -> str:
    return _s3_glob(device_type) if USE_REMOTE else _local_glob(device_type)


def _partition_dir(device_type: str, dt: datetime) -> Path:
    return (
        LOCAL_DATA_PATH / device_type
        / f"year={dt.year}"
        / f"month={dt.month:02d}"
        / f"day={dt.day:02d}"
    )


def _stage_dir(device_type: str) -> Path:
    return LOCAL_STAGE_PATH / device_type


def _training_table_snapshot_path(table_name: str) -> Path:
    if "." in table_name:
        schema_name, table_basename = table_name.split(".", 1)
    else:
        schema_name, table_basename = "main", table_name
    return LOCAL_TRAINING_TABLE_PATH / schema_name / f"{table_basename}.parquet"


def _delete_training_table_snapshot(table_name: str) -> None:
    snapshot_path = _training_table_snapshot_path(table_name)
    if snapshot_path.exists():
        snapshot_path.unlink()


def _write_training_table_snapshot(df: pd.DataFrame, table_name: str) -> Path:
    snapshot_path = _training_table_snapshot_path(table_name)
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(snapshot_path, index=False, engine="pyarrow")
    return snapshot_path


# =============================================================================
# DuckDB table utilities
# =============================================================================

def _table_exists(table_name: str) -> bool:
    """Check if a table exists. table_name may be 'schema.table' or just 'table'."""
    if "." in table_name:
        schema, tbl = table_name.split(".", 1)
        row = _db.execute(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = ? AND table_name = ?",
            [schema, tbl],
        ).fetchone()
    else:
        row = _db.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?",
            [table_name],
        ).fetchone()
    return bool(row and row[0])


def _schema_exists(schema_name: str) -> bool:
    row = _db.execute(
        "SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name = ?",
        [schema_name],
    ).fetchone()
    return bool(row and row[0])


def _main_user_table_count() -> int:
    row = _db.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'main'"
    ).fetchone()
    return int(row[0]) if row else 0


def _user_schema_names() -> list[str]:
    rows = _db.execute(
        "SELECT DISTINCT schema_name FROM information_schema.schemata "
        "WHERE catalog_name NOT IN ('system', 'temp') "
        "AND schema_name NOT IN ('information_schema', 'pg_catalog', 'main') "
        "ORDER BY schema_name"
    ).fetchall()
    return [schema_name for (schema_name,) in rows]


def _register_df(view_name: str, df: pd.DataFrame):
    try:
        _db.unregister(view_name)
    except Exception:
        pass
    _db.register(view_name, df)


# =============================================================================
# Parquet store writers
# =============================================================================

def _save_parquet_local(df: pd.DataFrame, device_type: str, dt: datetime) -> Path:
    out_dir = _partition_dir(device_type, dt)
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = out_dir / f"{device_type}_{dt.strftime('%Y%m%d_%H%M%S_%f')}.parquet"
    df.to_parquet(fname, index=False, engine="pyarrow")
    print(f"  [bronze] Saved parquet : {fname}")
    return fname


def _upload_to_minio(local_path: Path, device_type: str, dt: datetime):
    try:
        from minio import Minio
        client = Minio(
            S3_ENDPOINT.replace("http://", "").replace("https://", ""),
            access_key=S3_ACCESS_KEY,
            secret_key=S3_SECRET_KEY,
            secure=S3_ENDPOINT.startswith("https"),
        )
        if not client.bucket_exists(S3_BUCKET):
            client.make_bucket(S3_BUCKET)
        remote_key = (
            f"{device_type}/year={dt.year}/month={dt.month:02d}"
            f"/day={dt.day:02d}/{local_path.name}"
        )
        client.fput_object(S3_BUCKET, remote_key, str(local_path))
        print(f"  [bronze] Uploaded MinIO: s3://{S3_BUCKET}/{remote_key}")
    except ImportError:
        print("  minio package not installed — pip install minio")
    except Exception as exc:
        print(f"  MinIO upload error: {exc}")


def save_dataframe(df: pd.DataFrame, device_type: str, dt: datetime) -> Path:
    """Write a DataFrame into the Hive-partitioned Parquet store (bronze raw)."""
    local_path = _save_parquet_local(df, device_type, dt)
    if USE_REMOTE:
        _upload_to_minio(local_path, device_type, dt)
    return local_path


# =============================================================================
# Bronze layer — build / insert from Parquet store
# =============================================================================

def build_bronze_from_parquet(device_types=None, rebuild: bool = False) -> dict:
    """
    Materialise Hive-partitioned Parquet files into DuckDB bronze tables.

    rebuild=True  : DROP and recreate the table from scratch.
    rebuild=False : UNION ALL with existing table, then deduplicate.
    """
    results = {}
    for device_type in (device_types or DEVICE_TYPES):
        glob_path    = _parquet_glob(device_type)
        table_name   = _bronze_table(device_type)
        quoted_table = _q(table_name)

        # Check whether any parquet files exist locally
        local_parquets = list((LOCAL_DATA_PATH / device_type).glob("**/*.parquet")) if not USE_REMOTE else [1]
        if not local_parquets:
            print(f"  [bronze] No parquet files for {device_type} — skipping")
            results[device_type] = 0
            continue

        if rebuild and _table_exists(table_name):
            _db.execute(f"DROP TABLE {quoted_table}")

        if not _table_exists(table_name):
            _db.execute(
                f"CREATE TABLE {quoted_table} AS "
                f"SELECT * FROM read_parquet('{glob_path}', hive_partitioning=true, union_by_name=true)"
            )
        else:
            _db.execute(
                f"CREATE OR REPLACE TABLE {quoted_table} AS "
                f"SELECT DISTINCT * FROM ("
                f"  SELECT * FROM {quoted_table} "
                f"  UNION ALL "
                f"  SELECT * FROM read_parquet('{glob_path}', hive_partitioning=true, union_by_name=true)"
                f")"
            )

        count = _db.execute(f"SELECT COUNT(*) FROM {quoted_table}").fetchone()[0]
        print(f"  [bronze] {table_name}: {count} rows")
        results[device_type] = count

    return results


# legacy alias so parquet_restructure.py import keeps working
build_database_from_parquet = build_bronze_from_parquet


# =============================================================================
# Bronze layer — incremental insert from a DataFrame
# =============================================================================

def insert_to_bronze(df: pd.DataFrame, device_type: str) -> int:
    """
    Insert new rows into the bronze DuckDB table.
    Skips rows whose timestamp (+ device_id if present) already exists.
    Returns the number of rows actually inserted.
    """
    if df.empty:
        return 0

    df = df.copy()
    has_device_id = "device_id" in df.columns
    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        dedup_cols = ["timestamp", "device_id"] if has_device_id else ["timestamp"]
        df = df.sort_values("timestamp").drop_duplicates(subset=dedup_cols, keep="last")

    table_name   = _bronze_table(device_type)
    quoted_table = _q(table_name)

    if not _table_exists(table_name):
        # Create the table from the actual data so DuckDB infers correct column
        # types (using an empty DataFrame loses type info for object/string cols).
        temp = f"seed_{uuid4().hex}"
        _register_df(temp, df)
        try:
            _db.execute(f"CREATE TABLE {quoted_table} AS SELECT * FROM {_q(temp)}")
            return len(df)   # all rows are new — skip the dedup INSERT
        finally:
            _db.unregister(temp)

    temp_view = f"inc_{uuid4().hex}"
    _register_df(temp_view, df)
    qv = _q(temp_view)

    try:
        if has_device_id:
            sql = (
                f"INSERT INTO {quoted_table} BY NAME "
                f"SELECT * FROM {qv} i "
                f"WHERE NOT EXISTS ("
                f"  SELECT 1 FROM {quoted_table} e "
                f"  WHERE e.timestamp = i.timestamp AND e.device_id = i.device_id)"
            )
        else:
            sql = (
                f"INSERT INTO {quoted_table} BY NAME "
                f"SELECT * FROM {qv} i "
                f"WHERE NOT EXISTS ("
                f"  SELECT 1 FROM {quoted_table} e WHERE e.timestamp = i.timestamp)"
            )
        before = _db.execute(f"SELECT COUNT(*) FROM {quoted_table}").fetchone()[0]
        _db.execute(sql)
        after  = _db.execute(f"SELECT COUNT(*) FROM {quoted_table}").fetchone()[0]
        return max(0, after - before)
    finally:
        _db.unregister(temp_view)


# legacy alias
insert_dataframe_to_db = insert_to_bronze


# =============================================================================
# Staging (live batch buffer before flush into bronze)
# =============================================================================

def stage_dataframe(df: pd.DataFrame, device_type: str) -> Path | None:
    if df.empty:
        return None
    stage_dir = _stage_dir(device_type)
    stage_dir.mkdir(parents=True, exist_ok=True)
    fname = stage_dir / f"stage_{datetime.utcnow().strftime('%Y%m%d_%H%M%S_%f')}_{uuid4().hex[:8]}.parquet"
    df.to_parquet(fname, index=False, engine="pyarrow")
    print(f"  [stage]  Staged  : {fname}")
    return fname


def flush_staged_data(
    device_type: str,
    force: bool = False,
    max_rows: int = DEFAULT_STAGE_MAX_ROWS,
    max_age_seconds: int = DEFAULT_STAGE_MAX_AGE_SECONDS,
) -> int:
    """Flush staged parquet batches into the bronze DuckDB table."""
    stage_dir  = _stage_dir(device_type)
    stage_files = sorted(stage_dir.glob("*.parquet")) if stage_dir.exists() else []
    if not stage_files:
        return 0

    total_rows, oldest_age = 0, 0.0
    now_ts = datetime.utcnow().timestamp()
    for fp in stage_files:
        total_rows += len(pd.read_parquet(fp, engine="pyarrow"))
        oldest_age  = max(oldest_age, now_ts - fp.stat().st_mtime)

    if not (force or total_rows >= max_rows or oldest_age >= max_age_seconds):
        return 0

    combined   = pd.concat([pd.read_parquet(fp, engine="pyarrow") for fp in stage_files], ignore_index=True)
    flushed    = insert_to_bronze(combined, device_type)
    for fp in stage_files:
        fp.unlink(missing_ok=True)
    print(f"  [bronze] Flushed : {device_type} ({flushed} row(s))")
    return flushed


def stage_and_maybe_flush(
    df: pd.DataFrame,
    device_type: str,
    max_rows: int = DEFAULT_STAGE_MAX_ROWS,
    max_age_seconds: int = DEFAULT_STAGE_MAX_AGE_SECONDS,
) -> int:
    stage_dataframe(df, device_type)
    return flush_staged_data(device_type, max_rows=max_rows, max_age_seconds=max_age_seconds)


# =============================================================================
# Timestamp helpers
# =============================================================================

def get_latest_stored_timestamp(device_type: str, layer: str = "bronze"):
    """
    Return the most recent timestamp in the bronze, silver, gold, or parquet layer.
    layer : 'bronze' | 'silver' | 'gold' | 'parquet'
    Returns a datetime or None.
    """
    if layer == "bronze":
        table_name = _bronze_table(device_type)
    elif layer == "silver":
        table_name = _silver_table(device_type)
    elif layer == "gold":
        table_name = _gold_table(device_type)
    elif layer == "parquet":
        try:
            result = _db.execute(
                f"SELECT MAX(timestamp) FROM read_parquet('{_parquet_glob(device_type)}', hive_partitioning=true)"
            ).fetchone()
            return pd.to_datetime(result[0]).to_pydatetime() if result and result[0] else None
        except Exception:
            return None
    else:
        raise ValueError("layer must be 'bronze', 'silver', 'gold', or 'parquet'")

    if not _table_exists(table_name):
        return None
    try:
        result = _db.execute(f"SELECT MAX(timestamp) FROM {_q(table_name)}").fetchone()
        return pd.to_datetime(result[0]).to_pydatetime() if result and result[0] else None
    except Exception:
        return None


# =============================================================================
# Silver layer — query helpers (used by DataLoader and training code)
# =============================================================================

def _run_select(sql: str, params=None) -> pd.DataFrame:
    try:
        return _db.execute(sql, params or []).df()
    except Exception as exc:
        print(f"Query error: {exc}")
        return pd.DataFrame()


def _build_select_sql(
    table_name: str,
    *,
    columns=None,
    time_start=None,
    time_end=None,
    latest_n: int | None = None,
    timestamp_column: str | None = "timestamp",
    filters: dict[str, object | list[object] | tuple[object, ...] | set[object]] | None = None,
    order_by: str | None = None,
) -> tuple[str, list[object]]:
    col_expr = ", ".join(columns) if columns else "*"
    from_sql = _q(table_name)
    conditions: list[str] = []
    params: list[object] = []

    if timestamp_column:
        quoted_ts = _q(timestamp_column)
        if time_start is not None:
            conditions.append(f"{quoted_ts} >= ?")
            params.append(pd.to_datetime(time_start))
        if time_end is not None:
            conditions.append(f"{quoted_ts} <= ?")
            params.append(pd.to_datetime(time_end))

    for column_name, raw_value in (filters or {}).items():
        quoted_column = _q(column_name)
        if isinstance(raw_value, (list, tuple, set)):
            values = list(raw_value)
            if not values:
                conditions.append("1 = 0")
                continue
            conditions.append(f"{quoted_column} IN ({', '.join('?' for _ in values)})")
            params.extend(values)
        else:
            conditions.append(f"{quoted_column} = ?")
            params.append(raw_value)

    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    order_expr = order_by or (timestamp_column if timestamp_column else None)

    if latest_n is not None:
        if order_expr is None:
            raise ValueError("latest_n requires timestamp_column or explicit order_by")
        inner = (
            f"SELECT {col_expr} FROM {from_sql}{where} "
            f"ORDER BY {_q(order_expr)} DESC LIMIT {int(latest_n)}"
        )
        if order_expr:
            sql = f"SELECT {col_expr} FROM ({inner}) ORDER BY {_q(order_expr)}"
        else:
            sql = inner
    else:
        sql = f"SELECT {col_expr} FROM {from_sql}{where}"
        if order_expr:
            sql += f" ORDER BY {_q(order_expr)}"

    return sql, params


def query_table(
    table_name: str,
    *,
    columns=None,
    time_start=None,
    time_end=None,
    latest_n: int | None = None,
    timestamp_column: str | None = "timestamp",
    filters: dict[str, object | list[object] | tuple[object, ...] | set[object]] | None = None,
    order_by: str | None = None,
) -> pd.DataFrame:
    """Generic query helper for migrated training tables in DuckDB."""
    if not _table_exists(table_name):
        if _read_only:
            snapshot_path = _training_table_snapshot_path(table_name)
            if snapshot_path.exists():
                return _query_training_table_snapshot(
                    snapshot_path,
                    columns=columns,
                    time_start=time_start,
                    time_end=time_end,
                    latest_n=latest_n,
                    timestamp_column=timestamp_column,
                    filters=filters,
                    order_by=order_by,
                )
        print(f"[query_table] Table {table_name} does not exist")
        return pd.DataFrame()
    sql, params = _build_select_sql(
        table_name,
        columns=columns,
        time_start=time_start,
        time_end=time_end,
        latest_n=latest_n,
        timestamp_column=timestamp_column,
        filters=filters,
        order_by=order_by,
    )
    return _run_select(sql, params)


def upsert_table_dataframe(
    df: pd.DataFrame,
    table_name: str,
    *,
    key_columns: list[str] | tuple[str, ...] | None = None,
    rebuild: bool = False,
) -> int:
    """
    Create or replace matching rows in an arbitrary DuckDB table.

    When key_columns are supplied, incoming rows replace existing rows with the
    same key before the batch is inserted. This matches the legacy CSV append
    behavior where a late-arriving row overwrites the previous version.
    """
    if df.empty:
        if rebuild:
            if _table_exists(table_name):
                _db.execute(f"DROP TABLE {_q(table_name)}")
            _delete_training_table_snapshot(table_name)
        return 0

    prepared = df.copy()
    if "timestamp" in prepared.columns:
        prepared["timestamp"] = pd.to_datetime(prepared["timestamp"])
    normalized_keys = list(key_columns or [])
    for key_column in normalized_keys:
        if key_column not in prepared.columns:
            raise ValueError(f"Key column '{key_column}' is missing from the DataFrame")
    if normalized_keys:
        prepared = prepared.drop_duplicates(subset=normalized_keys, keep="last")

    quoted_table = _q(table_name)
    if rebuild and _table_exists(table_name):
        _db.execute(f"DROP TABLE {quoted_table}")

    temp_view = f"upsert_{uuid4().hex}"
    _register_df(temp_view, prepared)
    quoted_temp = _q(temp_view)
    try:
        if not _table_exists(table_name):
            _db.execute(f"CREATE TABLE {quoted_table} AS SELECT * FROM {quoted_temp}")
            _write_training_table_snapshot(_db.execute(f"SELECT * FROM {quoted_table}").df(), table_name)
            return len(prepared)

        if normalized_keys:
            join_sql = " AND ".join(
                f"target.{_q(column)} = source.{_q(column)}" for column in normalized_keys
            )
            _db.execute(
                f"DELETE FROM {quoted_table} AS target USING {quoted_temp} AS source WHERE {join_sql}"
            )

        before = _db.execute(f"SELECT COUNT(*) FROM {quoted_table}").fetchone()[0]
        _db.execute(f"INSERT INTO {quoted_table} BY NAME SELECT * FROM {quoted_temp}")
        after = _db.execute(f"SELECT COUNT(*) FROM {quoted_table}").fetchone()[0]
        _write_training_table_snapshot(_db.execute(f"SELECT * FROM {quoted_table}").df(), table_name)
        return max(0, after - before)
    finally:
        _db.unregister(temp_view)


def _query_training_table_snapshot(
    snapshot_path: Path,
    *,
    columns=None,
    time_start=None,
    time_end=None,
    latest_n: int | None = None,
    timestamp_column: str | None = "timestamp",
    filters: dict[str, object | list[object] | tuple[object, ...] | set[object]] | None = None,
    order_by: str | None = None,
) -> pd.DataFrame:
    snapshot_glob = str(snapshot_path).replace("\\", "/")
    try:
        con = duckdb.connect(":memory:")
        con.execute(f"CREATE VIEW snapshot_view AS SELECT * FROM read_parquet('{snapshot_glob}')")
        sql, params = _build_select_sql(
            "snapshot_view",
            columns=columns,
            time_start=time_start,
            time_end=time_end,
            latest_n=latest_n,
            timestamp_column=timestamp_column,
            filters=filters,
            order_by=order_by,
        )
        result = con.execute(sql, params).df()
        con.close()
        return result
    except Exception as exc:
        print(f"[query_table] Snapshot query error for {snapshot_path}: {exc}")
        return pd.DataFrame()


def query_silver(
    device_type: str,
    time_start=None,
    time_end=None,
    sensors=None,
    columns=None,
    latest_n: int | None = None,
) -> pd.DataFrame:
    """
    Query the silver DuckDB table.

    In read-only mode (DUCKDB_READ_ONLY=1) the silver table is not accessible
    because it lives in the write-locked .duckdb file.  In that case we
    transparently fall back to querying the Parquet files directly — silver is
    a passthrough copy of bronze until real preprocessing is applied, so the
    data is identical.

    Modes
    -----
    All data              : query_silver("air-1")
    From timestamp        : query_silver("air-1", time_start=dt)
    Time range            : query_silver("air-1", time_start=dt1, time_end=dt2)
    Latest N rows         : query_silver("air-1", latest_n=100)
    """
    # ── Read-only mode: silver is in the locked file ─────────────────────────
    # Try _db first — unit tests inject an in-memory DB with silver tables, so
    # we can use them directly.  In the live CLI _db is a bare :memory: with no
    # tables, so we fall back to Parquet (same data, since preprocess is a copy).
    if _read_only:
        table_name = _silver_table(device_type)
        if not _table_exists(table_name):
            return _query_parquet_direct(
                device_type, time_start=time_start, time_end=time_end,
                sensors=sensors, columns=columns, latest_n=latest_n,
            )
        # table exists in in-memory _db (test patcher) — fall through to query it
    else:
        table_name = _silver_table(device_type)
        if not _table_exists(table_name):
            print(f"[silver] Table {table_name} does not exist — run bronze2silver_preprocess.py first")
            return pd.DataFrame()

    col_expr   = ", ".join(columns) if columns else "*"
    from_sql   = _q(table_name)
    conditions, params = [], []

    if time_start is not None:
        conditions.append("timestamp >= ?")
        params.append(pd.to_datetime(time_start))
    if time_end is not None:
        conditions.append("timestamp <= ?")
        params.append(pd.to_datetime(time_end))
    if sensors:
        conditions.append(f"device_id IN ({', '.join('?' for _ in sensors)})")
        params.extend(sensors)

    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""

    if latest_n is not None:
        inner = f"SELECT {col_expr} FROM {from_sql}{where} ORDER BY timestamp DESC LIMIT {int(latest_n)}"
        sql   = f"SELECT {col_expr} FROM ({inner}) ORDER BY timestamp"
    else:
        sql = f"SELECT {col_expr} FROM {from_sql}{where} ORDER BY timestamp"

    return _run_select(sql, params)


def query_gold(
    device_type: str,
    time_start=None,
    time_end=None,
    sensors=None,
    columns=None,
    latest_n: int | None = None,
) -> pd.DataFrame:
    """
    Query the gold DuckDB table.

    Gold is currently a placeholder copy of silver, so in read-only mode we
    fall back to querying Parquet directly when the gold table is unavailable.
    """
    if _read_only:
        table_name = _gold_table(device_type)
        if not _table_exists(table_name):
            return _query_parquet_direct(
                device_type, time_start=time_start, time_end=time_end,
                sensors=sensors, columns=columns, latest_n=latest_n,
            )
    else:
        table_name = _gold_table(device_type)
        if not _table_exists(table_name):
            print(f"[gold] Table {table_name} does not exist — run silver2gold_preprocess.py first")
            return pd.DataFrame()

    col_expr = ", ".join(columns) if columns else "*"
    from_sql = _q(table_name)
    conditions, params = [], []

    if time_start is not None:
        conditions.append("timestamp >= ?")
        params.append(pd.to_datetime(time_start))
    if time_end is not None:
        conditions.append("timestamp <= ?")
        params.append(pd.to_datetime(time_end))
    if sensors:
        conditions.append(f"device_id IN ({', '.join('?' for _ in sensors)})")
        params.extend(sensors)

    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""

    if latest_n is not None:
        inner = f"SELECT {col_expr} FROM {from_sql}{where} ORDER BY timestamp DESC LIMIT {int(latest_n)}"
        sql = f"SELECT {col_expr} FROM ({inner}) ORDER BY timestamp"
    else:
        sql = f"SELECT {col_expr} FROM {from_sql}{where} ORDER BY timestamp"

    return _run_select(sql, params)


# =============================================================================
# Training data loader — config-driven SQL on silver layer
# =============================================================================

def training_data_loader(config: dict) -> pd.DataFrame:
    """
    Load training data from the silver or gold layer via a config dict.

    Config keys
    -----------
    device_type : str       — required
    layer       : str       — optional 'silver' or 'gold' (default: 'silver')
    sql         : str       — optional raw SQL; use {table} as the selected table placeholder
    time_start  : datetime  — optional
    time_end    : datetime  — optional
    sensors     : list[str] — optional device_id filter
    columns     : list[str] — optional column projection
    latest_n    : int       — optional; cannot combine with time filters
    """
    device_type = config["device_type"]
    layer       = config.get("layer", "silver")
    if layer == "silver":
        table_name = _silver_table(device_type)
        query_fn = query_silver
    elif layer == "gold":
        table_name = _gold_table(device_type)
        query_fn = query_gold
    else:
        raise ValueError("training_data_loader layer must be 'silver' or 'gold'")

    if not _table_exists(table_name):
        print(f"[{layer}] Table {table_name} does not exist — build the {layer} layer first")
        return pd.DataFrame()

    if config.get("sql"):
        sql = config["sql"].format(table=_q(table_name))
        return _run_select(sql)

    return query_fn(
        device_type=device_type,
        time_start=config.get("time_start"),
        time_end=config.get("time_end"),
        sensors=config.get("sensors"),
        columns=config.get("columns"),
        latest_n=config.get("latest_n"),
    )


# =============================================================================
# Bronze query helper (for inspection / debugging)
# =============================================================================

# =============================================================================
# Bronze query helper (for inspection / debugging)
# =============================================================================

def _query_parquet_direct(
    device_type: str,
    time_start=None,
    time_end=None,
    sensors=None,
    columns=None,
    latest_n: int | None = None,
    limit: int | None = None,
) -> pd.DataFrame:
    """
    Query Parquet files directly using a fresh in-memory DuckDB connection.

    Used when DUCKDB_READ_ONLY=1 so we never touch the locked .duckdb file.
    Parquet files support concurrent reads with no OS-level lock.
    """
    import glob as _glob_mod
    pattern = _parquet_glob(device_type)
    files   = _glob_mod.glob(pattern, recursive=True)
    if not files:
        print(f"[parquet] No data files found for {device_type} — ingest first")
        return pd.DataFrame()

    # Forward slashes required by DuckDB on Windows
    glob_str  = pattern.replace("\\", "/")
    col_expr  = ", ".join(columns) if columns else "*"
    from_sql  = f"read_parquet('{glob_str}', hive_partitioning=true)"
    conditions, params = [], []

    if time_start is not None:
        conditions.append("timestamp >= ?")
        params.append(pd.to_datetime(time_start))
    if time_end is not None:
        conditions.append("timestamp <= ?")
        params.append(pd.to_datetime(time_end))
    if sensors:
        conditions.append(f"device_id IN ({', '.join('?' for _ in sensors)})")
        params.extend(sensors)

    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""

    if latest_n is not None:
        inner = f"SELECT {col_expr} FROM {from_sql}{where} ORDER BY timestamp DESC LIMIT {int(latest_n)}"
        sql   = f"SELECT {col_expr} FROM ({inner}) ORDER BY timestamp"
    elif conditions:
        sql = f"SELECT {col_expr} FROM {from_sql}{where} ORDER BY timestamp"
    elif limit is not None:
        sql = f"SELECT {col_expr} FROM {from_sql} ORDER BY timestamp LIMIT {limit}"
    else:
        sql = f"SELECT {col_expr} FROM {from_sql} ORDER BY timestamp"

    try:
        con    = duckdb.connect(":memory:")
        result = con.execute(sql, params or []).df()
        con.close()
        return result
    except Exception as exc:
        print(f"[parquet] Query error for {device_type}: {exc}")
        return pd.DataFrame()


def query_bronze(
    device_type: str,
    time_start=None,
    time_end=None,
    sensors=None,
    columns=None,
    latest_n: int | None = None,
    limit: int = 100,
) -> pd.DataFrame:
    """
    Query the bronze layer.

    In read-only mode (DUCKDB_READ_ONLY=1), reads directly from Parquet files
    so the ingest pipeline's write lock on smart_ilab.duckdb is not contested.
    Otherwise queries the bronze DuckDB table.

    If none of time_start/time_end/sensors/latest_n are set, returns `limit`
    rows ordered by timestamp (backward-compatible default).
    """
    # ── Read-only mode: bypass the locked .duckdb file ────────────────────────
    if _read_only:
        return _query_parquet_direct(
            device_type, time_start=time_start, time_end=time_end,
            sensors=sensors, columns=columns, latest_n=latest_n,
            limit=limit if (not latest_n and not time_start and not time_end and not sensors) else None,
        )

    # ── Normal mode: query bronze DuckDB table ────────────────────────────────
    table_name = _bronze_table(device_type)
    if not _table_exists(table_name):
        print(f"[bronze] Table {table_name} does not exist — run ingest first")
        return pd.DataFrame()

    col_expr   = ", ".join(columns) if columns else "*"
    from_sql   = _q(table_name)
    conditions, params = [], []

    if time_start is not None:
        conditions.append("timestamp >= ?")
        params.append(pd.to_datetime(time_start))
    if time_end is not None:
        conditions.append("timestamp <= ?")
        params.append(pd.to_datetime(time_end))
    if sensors:
        conditions.append(f"device_id IN ({', '.join('?' for _ in sensors)})")
        params.extend(sensors)

    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""

    if latest_n is not None:
        inner = f"SELECT {col_expr} FROM {from_sql}{where} ORDER BY timestamp DESC LIMIT {int(latest_n)}"
        sql   = f"SELECT {col_expr} FROM ({inner}) ORDER BY timestamp"
    elif conditions:
        sql = f"SELECT {col_expr} FROM {from_sql}{where} ORDER BY timestamp"
    else:
        sql = f"SELECT {col_expr} FROM {from_sql} ORDER BY timestamp LIMIT {limit}"

    return _run_select(sql, params)


# =============================================================================
# Module smoke-test
# =============================================================================

# =============================================================================
# Schema initialization and data migration (write-mode only)
# =============================================================================
# On first run after this restructuring, flat tables (bronze_air_1, …) are
# automatically migrated to schema-qualified tables (bronze.air_1, …).


def reset_database_layout(drop_main_tables: bool = True) -> None:
    """Drop and recreate bronze/silver/gold schemas; optionally clear user tables in main."""
    if _read_only:
        return

    for schema_name in reversed(ROOT_SCHEMAS):
        _db.execute(f"DROP SCHEMA IF EXISTS {_q(schema_name)} CASCADE")

    if drop_main_tables:
        main_tables = _db.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main' ORDER BY table_name"
        ).fetchall()
        for (table_name,) in main_tables:
            _db.execute(f"DROP TABLE IF EXISTS {_q('main.' + table_name)}")

    for schema_name in ROOT_SCHEMAS:
        _db.execute(f"CREATE SCHEMA IF NOT EXISTS {_q(schema_name)}")

def ensure_database_layout() -> None:
    """Create B/S/G schemas and migrate any legacy flat table names."""
    if _read_only:
        return

    for schema_name in ROOT_SCHEMAS:
        _db.execute(f"CREATE SCHEMA IF NOT EXISTS {_q(schema_name)}")

    for _dt in DEVICE_TYPES:
        _base  = _dt.replace("-", "_")
        _old_b = f"bronze_{_base}"
        _new_b = _bronze_table(_dt)
        if _table_exists(_old_b) and not _table_exists(_new_b):
            _db.execute(f"CREATE TABLE {_q(_new_b)} AS SELECT * FROM {_q(_old_b)}")
            _db.execute(f"DROP TABLE {_q(_old_b)}")
            print(f"  [migrate] {_old_b} → {_new_b}")
        _old_s = f"silver_{_base}"
        _new_s = _silver_table(_dt)
        if _table_exists(_old_s) and not _table_exists(_new_s):
            _db.execute(f"CREATE TABLE {_q(_new_s)} AS SELECT * FROM {_q(_old_s)}")
            _db.execute(f"DROP TABLE {_q(_old_s)}")
            print(f"  [migrate] {_old_s} → {_new_s}")
        _old_g = f"gold_{_base}"
        _new_g = _gold_table(_dt)
        if _table_exists(_old_g) and not _table_exists(_new_g):
            _db.execute(f"CREATE TABLE {_q(_new_g)} AS SELECT * FROM {_q(_old_g)}")
            _db.execute(f"DROP TABLE {_q(_old_g)}")
            print(f"  [migrate] {_old_g} → {_new_g}")


ensure_database_layout()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Inspect or reset the Smart i-Lab DuckDB layout.")
    parser.add_argument(
        "--reset-schema",
        action="store_true",
        help="Drop and recreate bronze/silver/gold schemas and clear user tables from main.",
    )
    args = parser.parse_args()

    if args.reset_schema:
        reset_database_layout(drop_main_tables=True)
        print("Reset bronze/silver/gold schemas and cleared user tables from main.")
        print("Note: DuckDB's built-in main schema still exists and cannot be removed.")

    print("DuckDB path:", DUCKDB_PATH)
    print("Pipeline schemas in DB:")
    for schema_name in _user_schema_names():
        print(f"  {schema_name}")

    print(f"Built-in main schema user tables: {_main_user_table_count()}")

    print("Tables in DB:")
    tables = _db.execute(
        "SELECT table_schema, table_name FROM information_schema.tables "
        "WHERE table_schema NOT IN ('information_schema', 'pg_catalog') "
        "ORDER BY table_schema, table_name"
    ).fetchall()
    for (schema, tbl) in tables:
        qualified = f"{schema}.{tbl}" if schema != "main" else tbl
        count = _db.execute(f"SELECT COUNT(*) FROM {_q(qualified)}").fetchone()[0]
        print(f"  {qualified:35s} {count:>8,} rows")
