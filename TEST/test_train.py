"""
test_train.py — compare legacy Zone 5 smoke-flow tables against the migrated BSG path

This file is intentionally more verbose than the focused unit tests. It prints
schema, dtype, null/NaN diagnostics, and row-level comparison summaries so the
legacy smoke-flow contract can be inspected while migrating away from CSV.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parent.parent
TEST_ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
LEGACY_ZONE5_ROOT = next(
    path
    for path in (
        ROOT / "Legacy" / "zone5_cv_time_features_package",
        ROOT / "zone5_cv_time_features_package",
    )
    if path.exists()
)
if str(LEGACY_ZONE5_ROOT) not in sys.path:
    sys.path.insert(0, str(LEGACY_ZONE5_ROOT))

os.chdir(ROOT)
os.environ["DUCKDB_READ_ONLY"] = "1"

_storage_mod = importlib.import_module("CSV Training Data Code")
import dataloader as _dl_mod  # noqa: E402
import zone5_training_migrated as _migrated  # noqa: E402
from zone5 import build_cv_training_data as _legacy_builder  # noqa: E402


def _report_frame(frame: pd.DataFrame) -> dict[str, object]:
    return {
        "rows": int(len(frame)),
        "columns": list(frame.columns),
        "dtypes": {column: str(dtype) for column, dtype in frame.dtypes.items()},
        "null_counts": {column: int(frame[column].isna().sum()) for column in frame.columns},
        "nan_counts": {
            column: int(pd.to_numeric(frame[column], errors="coerce").isna().sum())
            for column in frame.columns
            if pd.api.types.is_numeric_dtype(frame[column])
        },
    }


def _normalize_for_compare(frame: pd.DataFrame) -> pd.DataFrame:
    normalized = frame.copy()
    if "timestamp" in normalized.columns:
        normalized["timestamp"] = pd.to_datetime(normalized["timestamp"], errors="coerce")
    normalized = normalized.reindex(sorted(normalized.columns), axis=1)
    if "timestamp" in normalized.columns:
        normalized = normalized.sort_values("timestamp")
    return normalized.reset_index(drop=True)


class PatchedTrainComparisonCase(unittest.TestCase):
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

    def _seed_silver_sources(self) -> tuple[pd.DataFrame, pd.DataFrame]:
        timestamps = pd.date_range("2026-05-06 10:00:00", periods=4, freq="10s")

        air1 = pd.DataFrame(
            {
                "timestamp": timestamps,
                "temp_s5": [24.5, 24.6, 24.7, 24.8],
                "rh_s5": [51.0, 51.1, 51.2, 51.3],
                "co2_s5": [500, 505, 510, 515],
                "pm25_s5": [5.0, 5.1, np.nan, 5.3],
            }
        )
        power = pd.DataFrame(
            {
                "timestamp": timestamps,
                "device_id": [_migrated.ZONE5_SMART_PLUG_DEVICE_ID] * len(timestamps),
                "power": [30.0, 31.0, 32.0, 33.0],
            }
        )
        mmwave = pd.DataFrame(
            {
                "timestamp": timestamps,
                "device_id": [_migrated.ZONE5_MMWAVE_DEVICE_ID] * len(timestamps),
                "radar_zone_3_occupancy": [0, 1, 1, 0],
                "radar_target": [0, 1, 1, 0],
            }
        )
        sen55 = pd.DataFrame(
            {
                "timestamp": timestamps,
                "pm1_0": [1.0, 1.1, 1.2, 1.3],
                "pm2_5": [2.0, 2.1, 2.2, 2.3],
                "pm4_0": [3.0, 3.1, 3.2, 3.3],
                "pm10_0": [4.0, 4.1, 4.2, 4.3],
                "temperature": [25.0, 25.1, 25.2, 25.3],
                "humidity": [50.0, 50.1, 50.2, 50.3],
                "voc": [10.0, np.nan, 12.0, 13.0],
                "nox": [20.0, 21.0, 22.0, 23.0],
            }
        )
        labels = pd.DataFrame(
            {
                "timestamp": timestamps,
                "occupancy_count": [0, 1, 2, 0],
                "cv_is_occupied": [0, 1, 1, 0],
            }
        )

        _storage_mod.upsert_table_dataframe(air1, _storage_mod._silver_table("air-1"), key_columns=["timestamp"], rebuild=True)
        _storage_mod.upsert_table_dataframe(power, _storage_mod._silver_table("smart-plug-v2"), key_columns=["timestamp", "device_id"], rebuild=True)
        _storage_mod.upsert_table_dataframe(mmwave, _storage_mod._silver_table("msr-2"), key_columns=["timestamp", "device_id"], rebuild=True)
        _storage_mod.upsert_table_dataframe(sen55, _migrated.SILVER_SEN55, key_columns=["timestamp"], rebuild=True)
        _storage_mod.upsert_table_dataframe(labels, _migrated.SILVER_CV_LABELS, key_columns=["timestamp"], rebuild=True)

        raw_features = air1.merge(power[["timestamp", "power"]].rename(columns={"power": "power_s5"}), on="timestamp")
        raw_features = raw_features.merge(
            mmwave[["timestamp", "radar_zone_3_occupancy"]].rename(columns={"radar_zone_3_occupancy": "mmwave_s5"}),
            on="timestamp",
        )
        raw_features = raw_features.merge(
            sen55.rename(
                columns={
                    "pm1_0": "sen55_pm1_0",
                    "pm2_5": "sen55_pm2_5",
                    "pm4_0": "sen55_pm4_0",
                    "pm10_0": "sen55_pm10_0",
                    "temperature": "sen55_temperature",
                    "humidity": "sen55_humidity",
                    "voc": "sen55_voc",
                    "nox": "sen55_nox",
                }
            ),
            on="timestamp",
        )
        return raw_features, labels


class TestTrainFlowComparison(PatchedTrainComparisonCase):
    def test_legacy_vs_migrated_zone5_smoke_flow(self):
        raw_features, raw_labels = self._seed_silver_sources()

        legacy_joined, _legacy_features, _legacy_labels = _legacy_builder.combine_feature_label_frames(
            raw_features,
            raw_labels,
            occupied_threshold=1.0,
        )
        _migrated.build_zone5_training_input_from_silver(rebuild=True)

        smoke_columns = ["timestamp", *_migrated.RAW_FEATURE_COLUMNS]
        legacy_smoke = legacy_joined[smoke_columns].tail(3).reset_index(drop=True)
        migrated_smoke = _migrated.build_zone5_smoke_frame_from_silver(lookback=2, safety_rows=1)

        legacy_prepped = _normalize_for_compare(legacy_smoke)
        migrated_prepped = _normalize_for_compare(migrated_smoke)

        print("\nLegacy smoke-frame report:")
        print(json.dumps(_report_frame(legacy_prepped), indent=2, sort_keys=True, default=str))
        print("\nMigrated smoke-frame report:")
        print(json.dumps(_report_frame(migrated_prepped), indent=2, sort_keys=True, default=str))

        self.assertListEqual(list(legacy_prepped.columns), list(migrated_prepped.columns))
        self.assertDictEqual(
            {column: str(dtype) for column, dtype in legacy_prepped.dtypes.items()},
            {column: str(dtype) for column, dtype in migrated_prepped.dtypes.items()},
        )
        self.assertDictEqual(
            {column: int(legacy_prepped[column].isna().sum()) for column in legacy_prepped.columns},
            {column: int(migrated_prepped[column].isna().sum()) for column in migrated_prepped.columns},
        )
        pd.testing.assert_frame_equal(legacy_prepped, migrated_prepped, check_dtype=False)

    def test_migrated_smoke_frame_has_expected_window_contract(self):
        self._seed_silver_sources()
        _migrated.build_zone5_training_input_from_silver(rebuild=True)

        smoke = _migrated.build_zone5_smoke_frame_from_silver(lookback=2, safety_rows=1)
        report = _migrated.training_input_quality_report(smoke)
        print("\nMigrated smoke-frame report:")
        print(json.dumps(report, indent=2, sort_keys=True, default=str))

        self.assertEqual(len(smoke), 3)
        self.assertIn("timestamp", smoke.columns)
        for column in _migrated.RAW_FEATURE_COLUMNS:
            self.assertIn(column, smoke.columns)


if __name__ == "__main__":
    unittest.main(verbosity=2)
