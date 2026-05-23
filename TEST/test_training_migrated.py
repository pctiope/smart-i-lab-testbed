"""test_training_migrated.py — SQL-only checks for the migrated Zone 5 pipeline."""

from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

import duckdb
import pandas as pd


REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
os.chdir(REPO_ROOT)
os.environ["DUCKDB_READ_ONLY"] = "1"

_storage_mod = importlib.import_module("CSV Training Data Code")
import dataloader as _dl_mod  # noqa: E402
import train_zone5_migrated as _trainer  # noqa: E402
import zone5_training_migrated as _migrated  # noqa: E402


class PatchedTrainingMigrationTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name)

        self._orig_storage_db = _storage_mod._db
        self._orig_dataloader_db = _dl_mod._db

        self.db = duckdb.connect(":memory:")
        self.db.execute("CREATE SCHEMA IF NOT EXISTS bronze")
        self.db.execute("CREATE SCHEMA IF NOT EXISTS silver")
        self.db.execute("CREATE SCHEMA IF NOT EXISTS gold")

        _storage_mod._db = self.db
        _dl_mod._db = self.db

    def tearDown(self):
        _storage_mod._db = self._orig_storage_db
        _dl_mod._db = self._orig_dataloader_db
        try:
            self.db.close()
        except Exception:
            pass
        self._tmpdir.cleanup()

    def _seed_zone5_sources(self) -> None:
        timestamps = pd.date_range("2026-05-06 10:00:00", periods=3, freq="10s")

        air1 = pd.DataFrame(
            {
                "timestamp": timestamps,
                "temp_s5": [24.5, 24.6, 24.7],
                "rh_s5": [51.0, 51.1, 51.2],
                "co2_s5": [500, 505, 510],
                "pm25_s5": [5.0, 5.1, 5.2],
            }
        )
        smart_plug = pd.DataFrame(
            {
                "timestamp": timestamps,
                "device_id": [_migrated.ZONE5_SMART_PLUG_DEVICE_ID] * len(timestamps),
                "power": [30.0, 31.0, 32.0],
            }
        )
        msr2 = pd.DataFrame(
            {
                "timestamp": timestamps,
                "device_id": [_migrated.ZONE5_MMWAVE_DEVICE_ID] * len(timestamps),
                "radar_zone_3_occupancy": [0, 1, 0],
                "radar_target": [0, 1, 0],
            }
        )
        sen55 = pd.DataFrame(
            {
                "timestamp": timestamps,
                "pm1_0": [1.0, 1.1, 1.2],
                "pm2_5": [2.0, 2.1, 2.2],
                "pm4_0": [3.0, 3.1, 3.2],
                "pm10_0": [4.0, 4.1, 4.2],
                "temperature": [25.0, 25.1, 25.2],
                "humidity": [50.0, 50.1, 50.2],
                "voc": [10.0, 11.0, 12.0],
                "nox": [20.0, 21.0, 22.0],
            }
        )
        labels = pd.DataFrame(
            {
                "timestamp": timestamps,
                "occupancy_count": [0, 1, 0],
                "zone_occupied": [0.0, 1.0, 0.0],
            }
        )

        _storage_mod.upsert_table_dataframe(air1, _storage_mod._silver_table("air-1"), key_columns=["timestamp"], rebuild=True)
        _storage_mod.upsert_table_dataframe(smart_plug, _storage_mod._silver_table("smart-plug-v2"), key_columns=["timestamp", "device_id"], rebuild=True)
        _storage_mod.upsert_table_dataframe(msr2, _storage_mod._silver_table("msr-2"), key_columns=["timestamp", "device_id"], rebuild=True)
        _storage_mod.upsert_table_dataframe(sen55, _migrated.SILVER_SEN55, key_columns=["timestamp"], rebuild=True)
        _storage_mod.upsert_table_dataframe(labels, _migrated.SILVER_CV_LABELS, key_columns=["timestamp"], rebuild=True)


class TestTrainingMigrationParity(PatchedTrainingMigrationTestCase):
    def test_build_training_input_from_silver_uses_sql_tables_only(self):
        self._seed_zone5_sources()

        silver = _migrated.build_zone5_training_input_from_silver(rebuild=True)

        self.assertEqual(len(silver), 3)
        self.assertIn("timestamp", silver.columns)
        self.assertIn("temp_s5", silver.columns)
        self.assertIn("power_s5", silver.columns)
        self.assertIn("mmwave_s5", silver.columns)
        self.assertIn("zone_occupied", silver.columns)

    def test_training_input_upsert_replaces_matching_timestamp(self):
        first = pd.DataFrame(
            [{"timestamp": "2026-05-06 10:00:00", "air_temp": 24.5, "zone_occupied": 0}]
        )
        second = pd.DataFrame(
            [{"timestamp": "2026-05-06 10:00:00", "air_temp": 25.1, "zone_occupied": 1}]
        )

        _storage_mod.upsert_table_dataframe(first, _migrated.SILVER_TRAINING_INPUT, key_columns=["timestamp"], rebuild=True)
        _storage_mod.upsert_table_dataframe(second, _migrated.SILVER_TRAINING_INPUT, key_columns=["timestamp"])
        silver = _migrated.load_zone5_training_input()

        self.assertEqual(len(silver), 1)
        self.assertEqual(float(silver.loc[0, "air_temp"]), 25.1)
        self.assertEqual(int(silver.loc[0, "zone_occupied"]), 1)

    def test_only_training_output_is_copied_to_gold(self):
        self._seed_zone5_sources()

        output = pd.DataFrame(
            [
                {"timestamp": "2026-05-06 10:00:00", "model_run_id": "run_001", "probability": 0.73},
                {"timestamp": "2026-05-06 10:00:10", "model_run_id": "run_001", "probability": 0.42},
            ]
        )
        _migrated.write_training_output_to_silver(output, rebuild=True)
        gold = _migrated.copy_training_output_to_gold(rebuild=True)

        self.assertTrue(_storage_mod._table_exists(_migrated.SILVER_CV_LABELS))
        self.assertTrue(_storage_mod._table_exists(_migrated.SILVER_TRAINING_OUTPUT))
        self.assertTrue(_storage_mod._table_exists(_migrated.GOLD_TRAINING_OUTPUT))
        self.assertFalse(_storage_mod._table_exists(_storage_mod.training_table_name("zone5", "cv_labels", layer="gold")))
        pd.testing.assert_frame_equal(
            gold.reset_index(drop=True),
            output.assign(timestamp=lambda df: pd.to_datetime(df["timestamp"])),
            check_dtype=False,
        )

    def test_quality_report_tracks_nulls_and_dtypes(self):
        self._seed_zone5_sources()
        silver = _migrated.build_zone5_training_input_from_silver(rebuild=True)
        report = _migrated.training_input_quality_report(silver)

        self.assertEqual(report["rows"], 3)
        self.assertIn("timestamp", report["columns"])
        self.assertIn("temp_s5", report["dtypes"])
        self.assertIn("temp_s5", report["null_counts"])

    def test_build_training_snapshot_writes_labeled_parquet_under_output_dir(self):
        self._seed_zone5_sources()

        snapshot = _trainer.build_training_snapshot(
            output_dir=self.root / "artifacts",
            rebuild_training_input=True,
        )

        snapshot_path = Path(snapshot["snapshot_path"])
        self.assertTrue(snapshot_path.is_file())
        self.assertIn("training_input_snapshots", snapshot_path.parts)
        self.assertTrue(str(snapshot_path).startswith(str(self.root / "artifacts")))

        frame = pd.read_parquet(snapshot_path)
        self.assertEqual(len(frame), 3)
        self.assertIn("zone_occupied", frame.columns)
        self.assertEqual(snapshot["labeled_rows"], 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)