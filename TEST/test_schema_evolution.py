"""Schema evolution checks for BSG incremental silver/gold writers."""

from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

import duckdb
import pandas as pd


REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
os.chdir(REPO_ROOT)
os.environ["DUCKDB_READ_ONLY"] = "1"

_storage_mod = importlib.import_module("CSV Training Data Code")
_bronze2silver = importlib.import_module("bronze2silver_preprocess")
_silver2gold = importlib.import_module("silver2gold_preprocess")


class SchemaEvolutionTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_storage_db = _storage_mod._db
        self._orig_bronze2silver_db = _bronze2silver._db
        self._orig_silver2gold_db = _silver2gold._db

        self.db = duckdb.connect(":memory:")
        self.db.execute("CREATE SCHEMA IF NOT EXISTS bronze")
        self.db.execute("CREATE SCHEMA IF NOT EXISTS silver")
        self.db.execute("CREATE SCHEMA IF NOT EXISTS gold")

        _storage_mod._db = self.db
        _bronze2silver._db = self.db
        _silver2gold._db = self.db

    def tearDown(self):
        _storage_mod._db = self._orig_storage_db
        _bronze2silver._db = self._orig_bronze2silver_db
        _silver2gold._db = self._orig_silver2gold_db
        try:
            self.db.close()
        except Exception:
            pass
        self._tmpdir.cleanup()

    def _create_table(self, table_name: str, df: pd.DataFrame) -> None:
        view_name = f"seed_{uuid4().hex}"
        self.db.register(view_name, df)
        try:
            self.db.execute(
                f"CREATE TABLE {_storage_mod._q(table_name)} AS "
                f"SELECT * FROM {_storage_mod._q(view_name)}"
            )
        finally:
            self.db.unregister(view_name)

    def _columns(self, table_name: str) -> set[str]:
        schema_name, table_basename = table_name.split(".", 1)
        rows = self.db.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = ? AND table_name = ?",
            [schema_name, table_basename],
        ).fetchall()
        return {column_name for (column_name,) in rows}


class TestSchemaEvolution(SchemaEvolutionTestCase):
    def test_bronze_to_silver_adds_optional_zigbee_column_without_duplicates(self):
        timestamp_existing = pd.Timestamp("2026-05-29 10:00:00")
        timestamp_new = pd.Timestamp("2026-05-29 10:01:00")
        silver_existing = pd.DataFrame(
            [
                {
                    "timestamp": timestamp_existing,
                    "device_id": "lamp_1",
                    "state": "ON",
                }
            ]
        )
        bronze_source = pd.DataFrame(
            [
                {
                    "timestamp": timestamp_existing,
                    "device_id": "lamp_1",
                    "state": "ON",
                    "brightness": 10,
                },
                {
                    "timestamp": timestamp_new,
                    "device_id": "lamp_1",
                    "state": "ON",
                    "brightness": 20,
                },
            ]
        )

        silver_table = _storage_mod._silver_table("zigbee2mqtt")
        self._create_table(silver_table, silver_existing)
        self._create_table(_storage_mod._bronze_table("zigbee2mqtt"), bronze_source)

        _bronze2silver.run_bronze_to_silver("zigbee2mqtt")

        self.assertIn("brightness", self._columns(silver_table))
        rows = self.db.execute(
            f"SELECT timestamp, device_id, state, brightness "
            f"FROM {_storage_mod._q(silver_table)} ORDER BY timestamp"
        ).df()
        self.assertEqual(len(rows), 2)
        duplicate_count = self.db.execute(
            f"SELECT COUNT(*) FROM {_storage_mod._q(silver_table)} "
            "WHERE timestamp = ? AND device_id = ?",
            [timestamp_existing, "lamp_1"],
        ).fetchone()[0]
        self.assertEqual(duplicate_count, 1)
        self.assertTrue(pd.isna(rows.loc[0, "brightness"]))
        self.assertEqual(int(rows.loc[1, "brightness"]), 20)

    def test_silver_to_gold_adds_optional_zigbee_column(self):
        timestamp_existing = pd.Timestamp("2026-05-29 10:00:00")
        timestamp_new = pd.Timestamp("2026-05-29 10:01:00")
        gold_existing = pd.DataFrame(
            [
                {
                    "timestamp": timestamp_existing,
                    "device_id": "lamp_1",
                    "state": "ON",
                }
            ]
        )
        silver_source = pd.DataFrame(
            [
                {
                    "timestamp": timestamp_existing,
                    "device_id": "lamp_1",
                    "state": "ON",
                    "brightness": 10,
                },
                {
                    "timestamp": timestamp_new,
                    "device_id": "lamp_1",
                    "state": "ON",
                    "brightness": 20,
                },
            ]
        )

        gold_table = _storage_mod._gold_table("zigbee2mqtt")
        self._create_table(gold_table, gold_existing)
        self._create_table(_storage_mod._silver_table("zigbee2mqtt"), silver_source)

        _silver2gold.run_silver_to_gold("zigbee2mqtt")

        self.assertIn("brightness", self._columns(gold_table))
        rows = self.db.execute(
            f"SELECT timestamp, device_id, state, brightness "
            f"FROM {_storage_mod._q(gold_table)} ORDER BY timestamp"
        ).df()
        self.assertEqual(len(rows), 2)
        self.assertTrue(pd.isna(rows.loc[0, "brightness"]))
        self.assertEqual(int(rows.loc[1, "brightness"]), 20)


if __name__ == "__main__":
    unittest.main()
