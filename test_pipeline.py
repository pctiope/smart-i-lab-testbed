"""
test_pipeline.py — BSG pipeline test suite
==========================================
Run from d:\\Phys 231\\ (the project root):

    python test_pipeline.py -v

Groups
------
  1. TestDBInit      (8 tests)  — fresh DB, stale DB detection, parquet bootstrap
  2. TestAppend      (11 tests) — incremental inserts, dedup, staging/flush, live feed
  3. TestDataLoader  (20 tests) — all DataLoader query modes, edge cases

No API connection required — all tests use synthetic DataFrames and an
isolated in-memory DuckDB.  The real smart_ilab.duckdb is never touched.
"""

import importlib
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import duckdb
import pandas as pd

# ── Must run from the project root so the space-named module is importable ────
os.chdir(Path(__file__).parent)

# ── Force in-memory DuckDB at import time so the test process never tries to
#    open smart_ilab.duckdb (which may be locked by a live ingest pipeline). ──
os.environ["DUCKDB_READ_ONLY"] = "1"

# ── Import storage + dataloader once; we'll swap _db in setUp/tearDown ────────
_storage_mod = importlib.import_module("CSV Training Data Code")
import dataloader as _dl_mod  # noqa: E402  (after chdir)
# Shorthand helpers so tests don't embed flat table-name strings
_bt = _storage_mod._bronze_table   # e.g. _bt("air-1")  → "bronze.air_1"
_st = _storage_mod._silver_table   # e.g. _st("air-1")  → "silver.air_1"
_gt = _storage_mod._gold_table     # e.g. _gt("air-1")  → "gold.air_1"

# =============================================================================
# Synthetic data factories (no API needed)
# =============================================================================

def _make_air1(n: int = 10, start: datetime | None = None) -> pd.DataFrame:
    """
    air-1 wide format:
      timestamp, temp_s1..s15, rh_s1..s15, co2_s1..s15, pm25_s1..s15  (61 cols)
    Each row is 1 minute apart.
    """
    start = start or datetime(2026, 1, 1, 0, 0, 0)
    records = []
    for i in range(n):
        row: dict = {"timestamp": start + timedelta(minutes=i)}
        for s in range(1, 16):
            row[f"temp_s{s}"]  = round(20.0 + s * 0.5 + i * 0.01, 2)
            row[f"rh_s{s}"]    = round(50.0 + s * 0.2,              2)
            row[f"co2_s{s}"]   = 400 + s * 5
            row[f"pm25_s{s}"]  = round(5.0 + s * 0.1,               2)
        records.append(row)
    return pd.DataFrame(records)


def _make_plug(
    n: int = 5,
    device_ids: list[str] | None = None,
    start: datetime | None = None,
) -> pd.DataFrame:
    """
    smart-plug-v2 narrow format:
      timestamp, device_id, device_type, power_w, voltage_v
    """
    start      = start      or datetime(2026, 1, 1, 0, 0, 0)
    device_ids = device_ids or ["plug_a", "plug_b"]
    records    = []
    for i in range(n):
        for dev in device_ids:
            records.append({
                "timestamp":   start + timedelta(minutes=i),
                "device_id":   dev,
                "device_type": "smart-plug-v2",
                "power_w":     round(100.0 + i * 2.5, 1),
                "voltage_v":   220.0,
            })
    return pd.DataFrame(records)


# =============================================================================
# Base class — isolated in-memory DuckDB + temp dirs for every test
# =============================================================================

class PatchedTestCase(unittest.TestCase):
    """
    Redirects the storage layer to an isolated in-memory DuckDB connection
    and a temporary directory.  Restores originals in tearDown so the real
    smart_ilab.duckdb is never touched.
    """

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        tmp = Path(self._tmpdir.name)

        # ── save originals ────────────────────────────────────────────────────
        self._orig_db         = _storage_mod._db
        self._orig_data_path  = _storage_mod.LOCAL_DATA_PATH
        self._orig_stage_path = _storage_mod.LOCAL_STAGE_PATH

        # ── patch with isolated resources ─────────────────────────────────────
        self.db = duckdb.connect(":memory:")
        # Schema-qualified tables require these schemas to exist first
        self.db.execute("CREATE SCHEMA IF NOT EXISTS bronze")
        self.db.execute("CREATE SCHEMA IF NOT EXISTS silver")
        self.db.execute("CREATE SCHEMA IF NOT EXISTS gold")
        _storage_mod._db          = self.db
        _storage_mod.LOCAL_DATA_PATH  = tmp / "data"
        _storage_mod.LOCAL_STAGE_PATH = tmp / "stage"

        # dataloader.py keeps its own _db reference — patch that too
        _dl_mod._db = self.db

    def tearDown(self):
        _storage_mod._db          = self._orig_db
        _storage_mod.LOCAL_DATA_PATH  = self._orig_data_path
        _storage_mod.LOCAL_STAGE_PATH = self._orig_stage_path
        _dl_mod._db = self._orig_db

        try:
            self.db.close()
        except Exception:
            pass
        self._tmpdir.cleanup()

    # ── test helpers ──────────────────────────────────────────────────────────

    def _row_count(self, table: str) -> int:
        return self.db.execute(
            f'SELECT COUNT(*) FROM {_storage_mod._q(table)}'
        ).fetchone()[0]

    def _create_silver(self, device_type: str, df: pd.DataFrame):
        """Directly create a silver DuckDB table (bypass preprocessing)."""
        silver = _storage_mod._silver_table(device_type)
        quoted = _storage_mod._q(silver)
        view   = f"sv_{uuid4().hex}"
        self.db.register(view, df)
        self.db.execute(f'CREATE TABLE {quoted} AS SELECT * FROM "{view}"')
        self.db.unregister(view)


# =============================================================================
# Group 1 — DB Initialization
# =============================================================================

class TestDBInit(PatchedTestCase):
    """
    Covers two scenarios:
      A) The DuckDB has no tables at all (first run / fresh environment)
      B) The DuckDB exists but is behind the API (stale data)
    """

    # ── A: fresh / no DB ─────────────────────────────────────────────────────

    def test_01_fresh_db_has_no_tables(self):
        """New in-memory DB reports no tables for any device type."""
        for dt in _storage_mod.DEVICE_TYPES:
            self.assertFalse(_storage_mod._table_exists(_storage_mod._bronze_table(dt)))
            self.assertFalse(_storage_mod._table_exists(_storage_mod._silver_table(dt)))
            self.assertFalse(_storage_mod._table_exists(_storage_mod._gold_table(dt)))

    def test_02_get_latest_ts_returns_none_when_table_missing(self):
        """get_latest_stored_timestamp → None when bronze table doesn't exist."""
        ts = _storage_mod.get_latest_stored_timestamp("air-1", layer="bronze")
        self.assertIsNone(ts)

    def test_03_insert_auto_creates_table(self):
        """insert_to_bronze creates the bronze table on first call."""
        df       = _make_air1(n=5)
        inserted = _storage_mod.insert_to_bronze(df, "air-1")
        self.assertTrue(_storage_mod._table_exists(_bt("air-1")))
        self.assertEqual(inserted, 5)
        self.assertEqual(_storage_mod._main_user_table_count(), 0)

    def test_04_timestamp_readable_after_first_insert(self):
        """After first insert, get_latest_stored_timestamp returns the max timestamp."""
        start = datetime(2026, 3, 1, 8, 0, 0)
        df    = _make_air1(n=10, start=start)
        _storage_mod.insert_to_bronze(df, "air-1")
        expected = start + timedelta(minutes=9)
        actual   = _storage_mod.get_latest_stored_timestamp("air-1", layer="bronze")
        self.assertIsNotNone(actual)
        self.assertEqual(actual.replace(microsecond=0), expected)

    def test_05_no_parquet_files_returns_zero_and_skips_table(self):
        """build_bronze_from_parquet returns 0 and does not create a table when no parquets exist."""
        result = _storage_mod.build_bronze_from_parquet(["air-1"], rebuild=False)
        self.assertEqual(result["air-1"], 0)
        self.assertFalse(_storage_mod._table_exists(_bt("air-1")))

    def test_06_parquet_bootstrap_creates_bronze_table(self):
        """save_dataframe → build_bronze_from_parquet creates a populated bronze table."""
        df = _make_air1(n=20)
        _storage_mod.save_dataframe(df, "air-1", datetime(2026, 4, 1))
        result = _storage_mod.build_bronze_from_parquet(["air-1"])
        self.assertTrue(_storage_mod._table_exists(_bt("air-1")))
        self.assertGreaterEqual(result["air-1"], 20)

    # ── B: DB exists but is stale ─────────────────────────────────────────────

    def test_07_stale_detection_db_ts_lags_api_ts(self):
        """
        Simulate: DB has old data; a fresh API reading arrives later.
        Verify the caller can detect db_ts < api_ts, indicating a sync is needed.
        """
        # Populate bronze with data ending at 12:04
        old_start  = datetime(2026, 4, 1, 12, 0, 0)
        simulated_api_ts = datetime(2026, 4, 1, 13, 0, 0)   # 1 hour ahead
        _storage_mod.insert_to_bronze(_make_air1(n=5, start=old_start), "air-1")

        db_ts = _storage_mod.get_latest_stored_timestamp("air-1", layer="bronze")
        self.assertIsNotNone(db_ts)
        self.assertLess(db_ts.replace(microsecond=0), simulated_api_ts,
                        "DB timestamp should lag the simulated API timestamp")

    def test_08_stale_db_updated_after_new_batch(self):
        """
        After detecting staleness, inserting the new batch advances the stored timestamp.
        """
        old_start = datetime(2026, 4, 1, 8, 0, 0)
        new_start = datetime(2026, 4, 1, 9, 0, 0)

        _storage_mod.insert_to_bronze(_make_air1(n=3, start=old_start), "air-1")
        ts_before = _storage_mod.get_latest_stored_timestamp("air-1", layer="bronze")

        _storage_mod.insert_to_bronze(_make_air1(n=3, start=new_start), "air-1")
        ts_after = _storage_mod.get_latest_stored_timestamp("air-1", layer="bronze")

        self.assertGreater(ts_after, ts_before)
        expected = new_start + timedelta(minutes=2)
        self.assertEqual(ts_after.replace(microsecond=0), expected)

    def test_09_reset_schema_recreates_bsg_and_clears_main(self):
        """reset_database_layout clears user tables and recreates bronze/silver/gold schemas."""
        self.db.execute("CREATE TABLE stray_table AS SELECT 1 AS id")
        _storage_mod.insert_to_bronze(_make_air1(n=3), "air-1")

        orig_read_only = _storage_mod._read_only
        _storage_mod._read_only = False
        try:
            _storage_mod.reset_database_layout(drop_main_tables=True)
        finally:
            _storage_mod._read_only = orig_read_only

        main_stray = self.db.execute(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = 'main' AND table_name = 'stray_table'"
        ).fetchone()[0]
        self.assertEqual(main_stray, 0)
        self.assertFalse(_storage_mod._table_exists(_bt("air-1")))
        self.assertTrue(_storage_mod._schema_exists("bronze"))
        self.assertTrue(_storage_mod._schema_exists("silver"))
        self.assertTrue(_storage_mod._schema_exists("gold"))


# =============================================================================
# Group 2 — Continuous Append (incremental inserts / live feed)
# =============================================================================

class TestAppend(PatchedTestCase):
    """
    Verifies that the bronze layer correctly handles:
      - deduplication on re-insert
      - overlapping batch windows
      - staging pipeline (stage → flush → bronze)
      - simulated live feed from the API
    """

    def test_01_re_insert_same_batch_no_duplicates(self):
        """Inserting the same 10 rows twice keeps the table at 10 rows."""
        df = _make_air1(n=10)
        _storage_mod.insert_to_bronze(df, "air-1")
        count_first = self._row_count(_bt("air-1"))

        _storage_mod.insert_to_bronze(df, "air-1")
        count_second = self._row_count(_bt("air-1"))

        self.assertEqual(count_first, 10)
        self.assertEqual(count_first, count_second)

    def test_02_insert_returns_only_new_row_count(self):
        """insert_to_bronze return value counts newly inserted rows, not duplicates."""
        df     = _make_air1(n=10)
        first  = _storage_mod.insert_to_bronze(df, "air-1")
        second = _storage_mod.insert_to_bronze(df, "air-1")   # all overlap
        self.assertEqual(first,  10)
        self.assertEqual(second,  0)

    def test_03_overlapping_batches_correct_dedup(self):
        """
        Batch A: rows t+0 … t+9
        Batch B: rows t+5 … t+14   (5 overlap)
        Expected total in DB: 15 unique rows.
        """
        start   = datetime(2026, 5, 1, 0, 0, 0)
        batch_a = _make_air1(n=10, start=start)
        batch_b = _make_air1(n=10, start=start + timedelta(minutes=5))

        _storage_mod.insert_to_bronze(batch_a, "air-1")
        _storage_mod.insert_to_bronze(batch_b, "air-1")

        self.assertEqual(self._row_count(_bt("air-1")), 15)

    def test_04_sequential_nonoverlapping_batches_sum_up(self):
        """Three non-overlapping batches of 10 → exactly 30 rows total."""
        start = datetime(2026, 5, 1, 0, 0, 0)
        for i in range(3):
            _storage_mod.insert_to_bronze(
                _make_air1(n=10, start=start + timedelta(minutes=i * 10)), "air-1"
            )
        self.assertEqual(self._row_count(_bt("air-1")), 30)

    def test_05_narrow_format_dedup_uses_device_id(self):
        """
        Narrow-format dedup keys are (timestamp, device_id).
        5 timestamps × 2 devices = 10 rows; re-inserting keeps count at 10.
        """
        df = _make_plug(n=5, device_ids=["plug_a", "plug_b"])
        _storage_mod.insert_to_bronze(df, "smart-plug-v2")
        count_first = self._row_count(_bt("smart-plug-v2"))

        _storage_mod.insert_to_bronze(df, "smart-plug-v2")
        count_second = self._row_count(_bt("smart-plug-v2"))

        self.assertEqual(count_first,  10)
        self.assertEqual(count_second, 10)

    def test_06_empty_df_insert_returns_zero(self):
        """Inserting an empty DataFrame does nothing and returns 0."""
        n = _storage_mod.insert_to_bronze(pd.DataFrame(), "air-1")
        self.assertEqual(n, 0)
        self.assertFalse(_storage_mod._table_exists(_bt("air-1")))

    def test_07_stage_force_flush_populates_bronze(self):
        """stage_dataframe followed by flush_staged_data(force=True) inserts all rows."""
        df = _make_air1(n=5)
        _storage_mod.stage_dataframe(df, "air-1")
        flushed = _storage_mod.flush_staged_data("air-1", force=True)

        self.assertEqual(flushed, 5)
        self.assertTrue(_storage_mod._table_exists(_bt("air-1")))
        self.assertEqual(self._row_count(_bt("air-1")), 5)

    def test_08_stage_below_threshold_does_not_flush(self):
        """Staging 3 rows with max_rows=500 does NOT flush into bronze."""
        df = _make_air1(n=3)
        _storage_mod.stage_and_maybe_flush(df, "air-1", max_rows=500, max_age_seconds=99999)
        self.assertFalse(_storage_mod._table_exists(_bt("air-1")))

    def test_09_stage_exceeds_threshold_triggers_flush(self):
        """
        3 batches of 4 rows with max_rows=10:
          after batch 3 (total staged=12 ≥ 10) a flush is triggered.
        """
        start    = datetime(2026, 6, 1, 0, 0, 0)
        max_rows = 10
        for i in range(3):
            batch = _make_air1(n=4, start=start + timedelta(hours=i))
            _storage_mod.stage_and_maybe_flush(batch, "air-1",
                                               max_rows=max_rows,
                                               max_age_seconds=99999)
        self.assertTrue(_storage_mod._table_exists(_bt("air-1")))
        self.assertGreater(self._row_count(_bt("air-1")), 0)

    def test_10_live_feed_simulation_unique_rows(self):
        """
        5 API polls each inserting exactly 1 new minute of data.
        Final bronze table must contain exactly 5 rows; latest timestamp correct.
        """
        start = datetime(2026, 5, 10, 12, 0, 0)
        for i in range(5):
            _storage_mod.insert_to_bronze(
                _make_air1(n=1, start=start + timedelta(minutes=i)), "air-1"
            )

        self.assertEqual(self._row_count(_bt("air-1")), 5)
        latest = _storage_mod.get_latest_stored_timestamp("air-1", layer="bronze")
        self.assertEqual(latest.replace(microsecond=0), start + timedelta(minutes=4))

    def test_11_sliding_window_live_feed_stays_consistent(self):
        """
        Sliding-window live feed (each poll returns last 3 rows).
          Poll 1: t+0, t+1, t+2
          Poll 2: t+1, t+2, t+3   (2 overlap)
          Poll 3: t+2, t+3, t+4   (2 overlap)
        Total unique rows: 5 (t+0 … t+4).
        """
        start = datetime(2026, 5, 10, 12, 0, 0)
        for i in range(3):
            window = _make_air1(n=3, start=start + timedelta(minutes=i))
            _storage_mod.insert_to_bronze(window, "air-1")
        self.assertEqual(self._row_count(_bt("air-1")), 5)

    def test_12_multi_device_types_independent(self):
        """Inserting into two device types does not cross-contaminate their tables."""
        _storage_mod.insert_to_bronze(_make_air1(n=5),                                  "air-1")
        _storage_mod.insert_to_bronze(_make_plug(n=3, device_ids=["p1", "p2"]), "smart-plug-v2")

        self.assertEqual(self._row_count(_bt("air-1")),          5)
        self.assertEqual(self._row_count(_bt("smart-plug-v2")),   6)  # 3t × 2 devs


# =============================================================================
# Group 3 — DataLoader (silver layer queries)
# =============================================================================

class TestDataLoader(PatchedTestCase):
    """
    Covers every DataLoader method and important edge cases.
    Silver tables are created directly (bypassing the preprocessing step)
    so tests remain independent of bronze2silver_preprocess.py.
    """

    START = datetime(2026, 6, 1, 0, 0, 0)
    N     = 20   # number of air-1 rows
    # air-1 col count: timestamp (1) + 4 metrics × 15 sensors (60) = 61
    AIR1_COLS = 61

    def setUp(self):
        super().setUp()
        self._air1_df = _make_air1(n=self.N, start=self.START)
        self._plug_df = _make_plug(n=10, device_ids=["p1", "p2", "p3"], start=self.START)
        self._create_silver("air-1",         self._air1_df)
        self._create_silver("smart-plug-v2", self._plug_df)

    def _dl(self, device_type: str = "air-1"):
        from dataloader import DataLoader
        return DataLoader(device_type)

    # ── load_all ──────────────────────────────────────────────────────────────

    def test_01_load_all_returns_full_table(self):
        df = self._dl().load_all()
        self.assertEqual(len(df), self.N)
        self.assertIn("timestamp", df.columns)

    def test_02_load_all_ordered_by_timestamp(self):
        df = self._dl().load_all()
        self.assertTrue(df["timestamp"].is_monotonic_increasing)

    # ── load_latest_n ─────────────────────────────────────────────────────────

    def test_03_load_latest_n_correct_count(self):
        df = self._dl().load_latest_n(5)
        self.assertEqual(len(df), 5)

    def test_04_load_latest_n_is_truly_latest(self):
        """The returned rows are the N most recent, still ordered ascending."""
        df = self._dl().load_latest_n(5)
        expected_first = self.START + timedelta(minutes=self.N - 5)
        actual_first   = pd.to_datetime(df["timestamp"].iloc[0]).replace(microsecond=0)
        self.assertEqual(actual_first, expected_first)

    def test_05_load_latest_n_larger_than_table_returns_all(self):
        df = self._dl().load_latest_n(9999)
        self.assertEqual(len(df), self.N)

    # ── load_since ────────────────────────────────────────────────────────────

    def test_06_load_since_correct_count(self):
        cutoff = self.START + timedelta(minutes=10)
        df = self._dl().load_since(cutoff)
        self.assertEqual(len(df), self.N - 10)

    def test_07_load_since_all_rows_at_or_after_cutoff(self):
        cutoff = self.START + timedelta(minutes=10)
        df = self._dl().load_since(cutoff)
        self.assertTrue((pd.to_datetime(df["timestamp"]) >= cutoff).all())

    # ── load_time_range ───────────────────────────────────────────────────────

    def test_08_load_time_range_closed_interval(self):
        """[t+5, t+14] inclusive = 10 rows."""
        t0 = self.START + timedelta(minutes=5)
        t1 = self.START + timedelta(minutes=14)
        df = self._dl().load_time_range(t0, t1)
        self.assertEqual(len(df), 10)

    def test_09_load_time_range_single_row(self):
        t = self.START + timedelta(minutes=7)
        df = self._dl().load_time_range(t, t)
        self.assertEqual(len(df), 1)

    # ── load_by_region ────────────────────────────────────────────────────────

    def test_10_load_by_region_time_window_only(self):
        t0 = self.START
        t1 = self.START + timedelta(minutes=4)
        df = self._dl().load_by_region(time_start=t0, time_end=t1)
        self.assertEqual(len(df), 5)

    def test_11_load_by_region_sensor_filter(self):
        """device_id filter on narrow-format table (smart-plug-v2)."""
        dl  = self._dl("smart-plug-v2")
        df  = dl.load_by_region(sensors=["p1", "p2"])
        expected = len(self._plug_df[self._plug_df["device_id"].isin(["p1", "p2"])])
        self.assertEqual(len(df), expected)

    def test_12_load_by_region_combined_time_and_sensor(self):
        """Time window + sensor filter together."""
        t1  = self.START + timedelta(minutes=4)
        dl  = self._dl("smart-plug-v2")
        df  = dl.load_by_region(time_start=self.START, time_end=t1, sensors=["p1"])
        # 5 timestamps × 1 device
        self.assertEqual(len(df), 5)

    # ── load_sql ──────────────────────────────────────────────────────────────

    def test_13_load_sql_aggregate(self):
        """load_sql: COUNT(*) via {table} placeholder."""
        df = self._dl().load_sql("SELECT COUNT(*) AS n FROM {table}")
        self.assertEqual(df["n"].iloc[0], self.N)

    def test_14_load_sql_filtered(self):
        cutoff = (self.START + timedelta(minutes=10)).isoformat()
        df     = self._dl().load_sql(
            f"SELECT * FROM {{table}} WHERE timestamp >= '{cutoff}' ORDER BY timestamp"
        )
        self.assertEqual(len(df), self.N - 10)

    # ── column projection ─────────────────────────────────────────────────────

    def test_15_column_projection_load_all(self):
        cols = ["timestamp", "temp_s1", "rh_s1"]
        df   = self._dl().load_all(columns=cols)
        self.assertEqual(list(df.columns), cols)

    def test_16_column_projection_load_latest_n(self):
        cols = ["timestamp", "co2_s1"]
        df   = self._dl().load_latest_n(3, columns=cols)
        self.assertEqual(list(df.columns), cols)

    # ── introspection helpers ─────────────────────────────────────────────────

    def test_17_row_count(self):
        self.assertEqual(self._dl().row_count(), self.N)

    def test_18_column_names(self):
        cols = self._dl().column_names()
        self.assertIn("timestamp", cols)
        self.assertIn("temp_s1",   cols)
        self.assertEqual(len(cols), self.AIR1_COLS)

    def test_19_available_tables(self):
        tables = self._dl().available_tables()
        self.assertIn(_st("air-1"),         tables)
        self.assertIn(_st("smart-plug-v2"), tables)
        self.assertNotIn(_st("msr-2"),      tables)   # not created in setUp

    # ── edge cases & error handling ───────────────────────────────────────────

    def test_20_missing_silver_load_all_returns_empty(self):
        df = self._dl("msr-2").load_all()
        self.assertTrue(df.empty)

    def test_21_missing_silver_load_latest_n_returns_empty(self):
        df = self._dl("sensibo").load_latest_n(5)
        self.assertTrue(df.empty)

    def test_22_missing_silver_load_sql_returns_empty(self):
        df = self._dl("zigbee2mqtt").load_sql("SELECT * FROM {table}")
        self.assertTrue(df.empty)

    def test_23_missing_silver_row_count_returns_zero(self):
        self.assertEqual(self._dl("ag-one").row_count(), 0)

    def test_24_missing_silver_column_names_returns_empty_list(self):
        self.assertEqual(self._dl("ag-one").column_names(), [])

    def test_25_invalid_device_type_raises_value_error(self):
        from dataloader import DataLoader
        with self.assertRaises(ValueError):
            DataLoader("not-a-device")

    def test_26_no_default_device_type_raises_on_call(self):
        from dataloader import DataLoader
        dl = DataLoader()   # no default
        with self.assertRaises(ValueError):
            dl.load_all()   # must fail without device_type arg

    def test_27_load_training_config_latest_n(self):
        dl = self._dl("air-1")
        df = dl.load_training_config({"latest_n": 3})
        self.assertEqual(len(df), 3)

    def test_28_load_training_config_time_range(self):
        dl  = self._dl("air-1")
        cfg = {
            "time_start": self.START + timedelta(minutes=2),
            "time_end":   self.START + timedelta(minutes=7),
        }
        df = dl.load_training_config(cfg)
        self.assertEqual(len(df), 6)

    def test_29_load_training_config_inherits_default_device(self):
        """load_training_config uses the DataLoader's default device type."""
        dl  = self._dl("air-1")   # default device set
        df  = dl.load_training_config({"latest_n": 1})   # no device_type in config
        self.assertEqual(len(df), 1)

    def test_30_load_all_narrow_format(self):
        """load_all on a narrow-format device returns all 30 rows (10t × 3 devs)."""
        dl = self._dl("smart-plug-v2")
        df = dl.load_all()
        self.assertEqual(len(df), 30)


# =============================================================================
# Runner
# =============================================================================

if __name__ == "__main__":
    # Print a summary header then run all tests
    print("=" * 65)
    print("Smart i-Lab BSG Pipeline — Test Suite")
    print(f"  DB isolation : in-memory DuckDB (real smart_ilab.duckdb untouched)")
    print(f"  API required : NO  (synthetic data only)")
    print("=" * 65)
    unittest.main(verbosity=2)
