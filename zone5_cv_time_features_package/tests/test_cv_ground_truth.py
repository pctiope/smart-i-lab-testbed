from __future__ import annotations

import argparse
import atexit
import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import time
import unittest
from unittest import mock
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import torch

from zone5 import air1_exporter
from zone5 import air1_sources
from zone5 import build_cv_training_data as cv_training_builder
from zone5 import model as training
from zone5 import collect_training_data
from zone5 import csv_size_guard
from zone5 import feature_builder
from zone5 import occupancy_mqtt_aggregator
from zone5 import promote_model
from zone5 import retrain_once
from zone5 import sen55_mqtt_collector
import smoke_test
from zone5.build_cv_training_data import build_cv_training_data
from zone5.occupancy_mqtt_aggregator import (
    CSV_HEADERS,
    DEFAULT_TOPIC as OCCUPANCY_DEFAULT_TOPIC,
    OccupancyAggregator,
    aggregate_counts,
)
from web_app.cv_ground_truth import CvGroundTruthTailer
from web_app.data_source import LiveAir1DataSource, ReplayTableDataSource, build_data_source_from_env
from web_app.inference_loop import InferenceConfig, InferenceLoop
from web_app import main as web_main


TEST_TEMP_ROOTS: list[Path] = []
TEST_RUNTIME_ROOT = Path(__file__).resolve().parents[1] / ".test_runtime"


def _cleanup_test_dirs() -> None:
    for root in TEST_TEMP_ROOTS:
        shutil.rmtree(root, ignore_errors=True)
    shutil.rmtree(TEST_RUNTIME_ROOT, ignore_errors=True)


atexit.register(_cleanup_test_dirs)


def fresh_test_dir(name: str) -> Path:
    TEST_RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    root = TEST_RUNTIME_ROOT / name
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    TEST_TEMP_ROOTS.append(root)
    return root


def minimal_cnn_params(lookback: int = 3) -> dict[str, object]:
    return {
        "lookback": int(lookback),
        "batch_size": 64,
        "num_conv_blocks": 2,
        "base_channels": 32,
        "kernel_size": 3,
        "late_kernel_size": 3,
        "activation": "ReLU",
        "normalization": "none",
        "pooling_pattern": "after_block_2",
        "global_pool": "avg",
        "dense_units": 32,
        "dropout": 0.0,
    }


def write_minimal_zone5_artifact(root: Path, lookback: int = 3) -> Path:
    run_dir = root / "artifact"
    (run_dir / "models").mkdir(parents=True)
    (run_dir / "tables").mkdir(parents=True)
    params = minimal_cnn_params(lookback)
    torch.manual_seed(7)
    model = training.TunableZoneOccupancyCNN(params=params, input_channels=len(training.FEATURE_COLUMNS))
    scaler = {
        "model_contract_version": training.MODEL_CONTRACT_VERSION,
        "feature_columns": training.FEATURE_COLUMNS,
        "raw_feature_columns": training.RAW_FEATURE_COLUMNS,
        "missing_indicator_columns": training.MISSING_INDICATOR_COLUMNS,
        "target_column": training.TARGET_COLUMN,
        "sample_interval_seconds": training.SAMPLE_INTERVAL_SECONDS,
        "lookback": lookback,
        "lookback_rows": lookback,
        "lookback_minutes": lookback * training.SAMPLE_INTERVAL_SECONDS / 60.0,
        "feature_fill_values": {col: 0.0 for col in training.RAW_FEATURE_COLUMNS},
        "means": {col: 0.0 for col in training.FEATURE_COLUMNS},
        "stds": {col: 1.0 for col in training.FEATURE_COLUMNS},
    }
    best_params = {
        "model_contract_version": training.MODEL_CONTRACT_VERSION,
        "params": params,
        "sample_interval_seconds": training.SAMPLE_INTERVAL_SECONDS,
        "lookback": lookback,
        "lookback_rows": lookback,
    }
    checkpoint = {
        "model_contract_version": training.MODEL_CONTRACT_VERSION,
        "model_state_dict": model.state_dict(),
        "params": params,
        "target_column": training.TARGET_COLUMN,
        "sample_interval_seconds": training.SAMPLE_INTERVAL_SECONDS,
        "lookback": lookback,
        "lookback_rows": lookback,
        "feature_fill_values": {col: 0.0 for col in training.RAW_FEATURE_COLUMNS},
    }
    (run_dir / "tables" / "scaler_stats_zone_5.json").write_text(json.dumps(scaler), encoding="utf-8")
    (run_dir / "tables" / "best_params_zone_5.json").write_text(json.dumps(best_params), encoding="utf-8")
    torch.save(checkpoint, run_dir / "models" / "best_cnn_zone_5.pt")
    return run_dir


class OccupancyMqttAggregatorTests(unittest.TestCase):
    def test_median_output_schema_and_restart_skip(self) -> None:
        self.assertEqual(aggregate_counts([0, 0, 1, 1, 1, 2], "median"), 1.0)
        root = fresh_test_dir("aggregator")
        csv_path = root / "cv_occupancy_zone5_10sec.csv"
        args = argparse.Namespace(
            output_csv=csv_path,
            aggregate="median",
            occupied_threshold=1.0,
            late_grace_seconds=0.0,
            use_receive_time=False,
        )
        aggregator = OccupancyAggregator(args)
        for count in [0, 0, 1, 1, 1, 2]:
            aggregator.add_payload(
                "care_ssl/zone5/person_count",
                json.dumps({"timestamp": "2026-05-06 10:42:10", "counted_persons": count}),
            )
        aggregator.flush_due(force=True)

        csv_frame = pd.read_csv(csv_path)
        self.assertEqual(list(csv_frame.columns), CSV_HEADERS)
        self.assertEqual(len(csv_frame), 1)
        self.assertEqual(float(csv_frame.loc[0, "occupancy_count"]), 1.0)
        self.assertEqual(int(csv_frame.loc[0, "cv_is_occupied"]), 1)
        self.assertEqual(int(csv_frame.loc[0, "sample_count"]), 6)
        self.assertEqual(float(csv_frame.loc[0, "median_count"]), 1.0)

        restarted = OccupancyAggregator(args)
        restarted.add_payload(
            "care_ssl/zone5/person_count",
            json.dumps({"timestamp": "2026-05-06 10:42:19", "counted_persons": 9}),
        )
        restarted.flush_due(force=True)
        self.assertEqual(len(pd.read_csv(csv_path)), 1)
        self.assertEqual(pd.read_csv(csv_path).loc[0, "timestamp"], "2026-05-06 10:42:10")


class CvTrainingDataBuilderTests(unittest.TestCase):
    def test_join_builder_preserves_null_features_and_labels(self) -> None:
        root = fresh_test_dir("builder")
        features_path = root / "features.parquet"
        labels_path = root / "labels.parquet"
        output_csv_path = root / "zone5_training_cv.csv"
        output_path = root / "zone5_training_cv.parquet"
        metadata_path = root / "zone5_training_cv.metadata.json"

        pd.DataFrame(
            {
                "timestamp": pd.to_datetime(
                    ["2026-05-06 10:00:00", "2026-05-06 10:01:00", "2026-05-06 10:02:00"]
                ),
                "temp_s5": [None, 24.2, 24.3],
                "rh_s5": [50.0, 50.1, 50.2],
                "co2_s5": [700, 705, 710],
                "pm25_s5": [12.0, 12.1, 12.2],
                "power_s5": [3.0, 3.1, 3.2],
                "mmwave_s5": [0, 1, 1],
                "sen55_pm1_0": [1.0, 1.1, 1.2],
                "sen55_pm2_5": [2.0, 2.1, None],
                "sen55_pm4_0": [3.0, 3.1, 3.2],
                "sen55_pm10_0": [4.0, 4.1, 4.2],
                "sen55_temperature": [25.0, 25.1, 25.2],
                "sen55_humidity": [55.0, 55.1, 55.2],
                "sen55_voc": [100.0, None, 120.0],
                "sen55_nox": [8.0, 8.1, 8.2],
            }
        ).to_parquet(features_path, index=False)
        pd.DataFrame(
            {
                "timestamp": pd.to_datetime(["2026-05-06 10:01:05"]),
                "occupancy_count": [0],
                "cv_is_occupied": [0],
                "sample_count": [4],
                "min_count": [0],
                "max_count": [0],
                "mean_count": [0.0],
                "median_count": [0.0],
                "last_count": [0],
                "first_message_time": ["2026-05-06 10:01:05"],
                "last_message_time": ["2026-05-06 10:01:55"],
                "source_topic": ["care_ssl/zone5/person_count"],
            }
        ).to_parquet(labels_path, index=False)

        metadata = build_cv_training_data(
            features_path=features_path,
            labels_path=labels_path,
            output_csv_path=output_csv_path,
            output_path=output_path,
            metadata_path=metadata_path,
        )
        joined = pd.read_csv(output_csv_path)
        self.assertEqual(len(joined), 3)
        self.assertFalse(output_path.exists())
        self.assertTrue(pd.isna(joined.loc[0, "zone_occupied"]))
        self.assertEqual(float(joined.loc[1, "zone_occupied"]), 0.0)
        self.assertTrue(pd.isna(joined.loc[2, "zone_occupied"]))
        self.assertIn("occupancy_count", joined.columns)
        self.assertIn("mmwave_s5", joined.columns)
        self.assertIn("sen55_voc", joined.columns)
        self.assertEqual(int(joined.loc[0, "temp_s5_missing"]), 1)
        self.assertEqual(int(joined.loc[1, "sen55_voc_missing"]), 1)
        self.assertEqual(metadata["target_column"], "zone_occupied")
        self.assertIn("occupancy_count", metadata["audit_columns_not_model_inputs"])
        self.assertEqual(json.loads(metadata_path.read_text())["join"]["joined_rows"], 3)
        self.assertEqual(json.loads(metadata_path.read_text())["join"]["unlabeled_rows"], 2)

    def test_join_builder_parquet_output_is_optional(self) -> None:
        root = fresh_test_dir("builder_csv_only")
        features_path = root / "features.parquet"
        labels_path = root / "cv_occupancy_zone5_10sec.csv"
        output_csv_path = root / "zone5_training_cv.csv"
        metadata_path = root / "zone5_training_cv.metadata.json"

        feature_row = {"timestamp": pd.Timestamp("2026-05-06 10:00:00")}
        for col in training.RAW_FEATURE_COLUMNS:
            feature_row[col] = 1.0
        pd.DataFrame([feature_row]).to_parquet(features_path, index=False)
        pd.DataFrame({"timestamp": ["2026-05-06 10:00:00"], "occupancy_count": [1]}).to_csv(
            labels_path,
            index=False,
        )

        metadata = build_cv_training_data(
            features_path=features_path,
            labels_path=labels_path,
            output_csv_path=output_csv_path,
            output_path=None,
            metadata_path=metadata_path,
        )

        self.assertTrue(output_csv_path.is_file())
        self.assertFalse((root / "zone5_training_cv.parquet").exists())
        self.assertNotIn("parquet_enabled", metadata["outputs"])
        self.assertNotIn("parquet_path", metadata["outputs"])

    def test_join_builder_drops_label_only_buckets(self) -> None:
        root = fresh_test_dir("builder_label_only")
        features_path = root / "features.parquet"
        labels_path = root / "labels.parquet"
        output_csv_path = root / "zone5_training_cv.csv"
        output_path = root / "zone5_training_cv.parquet"
        metadata_path = root / "zone5_training_cv.metadata.json"

        feature_row = {"timestamp": pd.Timestamp("2026-05-06 10:00:00")}
        for col in training.RAW_FEATURE_COLUMNS:
            feature_row[col] = 1.0
        pd.DataFrame([feature_row]).to_parquet(features_path, index=False)
        pd.DataFrame(
            {
                "timestamp": pd.to_datetime(["2026-05-06 10:00:00", "2026-05-06 10:00:10"]),
                "occupancy_count": [1, 1],
            }
        ).to_parquet(labels_path, index=False)

        metadata = build_cv_training_data(
            features_path=features_path,
            labels_path=labels_path,
            output_csv_path=output_csv_path,
            output_path=output_path,
            metadata_path=metadata_path,
        )

        joined = pd.read_csv(output_csv_path)
        self.assertFalse(output_path.exists())
        self.assertEqual(joined["timestamp"].astype(str).tolist(), ["2026-05-06 10:00:00"])
        self.assertEqual(float(joined.loc[0, "zone_occupied"]), 1.0)
        self.assertEqual(metadata["join"]["how"], "left")
        self.assertEqual(metadata["join"]["label_rows"], 2)
        self.assertEqual(metadata["join"]["joined_rows"], 1)

    def test_builder_integration_builds_cv_training_csv(self) -> None:
        root = fresh_test_dir("builder_integration")
        features_path = root / "features.parquet"
        labels_path = root / "cv_occupancy_zone5_10sec.parquet"
        output_csv_path = root / "zone5_training_cv.csv"
        output_path = root / "zone5_training_cv.parquet"
        metadata_path = root / "zone5_training_cv.metadata.json"

        pd.DataFrame(
            {
                "timestamp": pd.to_datetime(["2026-05-06 11:00:00", "2026-05-06 11:01:00"]),
                "temp_s5": [24.1, 24.2],
                "rh_s5": [50.0, 50.1],
                "co2_s5": [700, 705],
                "pm25_s5": [12.0, 12.1],
                "power_s5": [3.0, 3.1],
                "mmwave_s5": [0, 1],
                "sen55_pm1_0": [1.0, 1.1],
                "sen55_pm2_5": [2.0, 2.1],
                "sen55_pm4_0": [3.0, 3.1],
                "sen55_pm10_0": [4.0, 4.1],
                "sen55_temperature": [25.0, 25.1],
                "sen55_humidity": [55.0, 55.1],
                "sen55_voc": [100.0, 101.0],
                "sen55_nox": [8.0, 8.1],
            }
        ).to_parquet(features_path, index=False)
        pd.DataFrame(
            {
                "timestamp": pd.to_datetime(["2026-05-06 11:00:00", "2026-05-06 11:01:00"]),
                "occupancy_count": [1, 0],
                "cv_is_occupied": [1, 0],
            }
        ).to_parquet(labels_path, index=False)

        metadata = build_cv_training_data(
            features_path=features_path,
            labels_path=labels_path,
            output_csv_path=output_csv_path,
            output_path=output_path,
            metadata_path=metadata_path,
            allow_missing_labels=False,
        )

        self.assertIsNotNone(metadata)
        self.assertTrue(output_csv_path.is_file())
        self.assertFalse(output_path.exists())
        self.assertTrue(metadata_path.is_file())
        joined = pd.read_csv(output_csv_path)
        self.assertEqual(joined["zone_occupied"].tolist(), [1.0, 0.0])

    def test_join_builder_uses_ten_second_timestamps(self) -> None:
        root = fresh_test_dir("builder_10s")
        features_path = root / "features.parquet"
        labels_path = root / "labels.parquet"
        output_csv_path = root / "zone5_training_cv.csv"
        output_path = root / "zone5_training_cv.parquet"
        metadata_path = root / "zone5_training_cv.metadata.json"

        rows = []
        for ts, value in [("2026-05-06 10:00:09", 1.0), ("2026-05-06 10:00:12", 2.0)]:
            row = {"timestamp": ts}
            for col in training.RAW_FEATURE_COLUMNS:
                row[col] = value
            rows.append(row)
        pd.DataFrame(rows).to_parquet(features_path, index=False)
        pd.DataFrame(
            {
                "timestamp": ["2026-05-06 10:00:18"],
                "occupancy_count": [1],
                "cv_is_occupied": [1],
            }
        ).to_parquet(labels_path, index=False)

        metadata = build_cv_training_data(
            features_path=features_path,
            labels_path=labels_path,
            output_csv_path=output_csv_path,
            output_path=output_path,
            metadata_path=metadata_path,
        )

        joined = pd.read_csv(output_csv_path)
        self.assertFalse(output_path.exists())
        self.assertEqual(
            pd.to_datetime(joined["timestamp"]).dt.strftime("%H:%M:%S").tolist(),
            ["10:00:00", "10:00:10"],
        )
        self.assertEqual(float(joined.loc[1, "zone_occupied"]), 1.0)
        self.assertEqual(metadata["sample_interval_seconds"], 10)


class TrainingPreprocessingTests(unittest.TestCase):
    def _live_feature_frame(self, rows: int = 3, value: float = 1.0) -> pd.DataFrame:
        records = []
        for idx in range(rows):
            row = {
                "timestamp": pd.Timestamp("2026-05-06 10:00:00") + pd.Timedelta(seconds=10 * idx),
            }
            for col in training.RAW_FEATURE_COLUMNS:
                row[col] = float(value)
            records.append(row)
        return pd.DataFrame(records)

    def _split_frame(self, days: int, rows_per_day: int = 4) -> pd.DataFrame:
        rows = []
        for day_idx in range(days):
            day = pd.Timestamp("2026-05-01") + pd.Timedelta(days=day_idx)
            for row_idx in range(rows_per_day):
                row = {
                    "timestamp": day + pd.Timedelta(minutes=row_idx),
                    "zone_occupied": float((day_idx + row_idx) % 2),
                }
                for col in training.RAW_FEATURE_COLUMNS:
                    row[col] = float(row_idx + 1)
                rows.append(row)
        return pd.DataFrame(rows)

    def _three_day_bootstrap_frame(self, bootstrap_train_single_class: bool = False) -> pd.DataFrame:
        rows = []
        rows_per_day = 240
        for day_idx in range(3):
            day = pd.Timestamp("2026-05-01") + pd.Timedelta(days=day_idx)
            for row_idx in range(rows_per_day):
                if day_idx == 0:
                    label = 0.0
                elif day_idx == 1 and bootstrap_train_single_class and row_idx < 144:
                    label = 0.0
                elif day_idx == 2:
                    label = 1.0 if row_idx % 2 == 0 else 0.0
                else:
                    label = float(row_idx % 2)
                row = {
                    "timestamp": day + pd.Timedelta(seconds=10 * row_idx),
                    "zone_occupied": label,
                }
                for col in training.RAW_FEATURE_COLUMNS:
                    row[col] = float((row_idx % 10) + 1)
                rows.append(row)
        return pd.DataFrame(rows)

    def _five_day_progressive_frame(self) -> pd.DataFrame:
        rows = []
        rows_per_day = 240
        for day_idx in range(5):
            day = pd.Timestamp("2026-05-06") + pd.Timedelta(days=day_idx)
            for row_idx in range(rows_per_day):
                label = 0.0 if day_idx == 0 else float(row_idx % 2)
                row = {
                    "timestamp": day + pd.Timedelta(seconds=10 * row_idx),
                    "zone_occupied": label,
                }
                for col in training.RAW_FEATURE_COLUMNS:
                    row[col] = float((row_idx % 10) + 1)
                rows.append(row)
        return pd.DataFrame(rows)

    def test_blind_split_holds_out_latest_calendar_day_only(self) -> None:
        frame = self._split_frame(days=4, rows_per_day=3)

        splits = training.blind_test_split(frame)

        pre_test_dates = pd.to_datetime(splits["pre_test"]["timestamp"]).dt.date.unique().tolist()
        test_dates = pd.to_datetime(splits["test"]["timestamp"]).dt.date.unique().tolist()
        self.assertEqual([str(value) for value in pre_test_dates], ["2026-05-01", "2026-05-02", "2026-05-03"])
        self.assertEqual([str(value) for value in test_dates], ["2026-05-04"])

    def test_adaptive_rolling_validation_uses_fewer_folds_for_short_history(self) -> None:
        frame = self._split_frame(days=4, rows_per_day=3)

        _blind_splits, cv_folds, split_policy = training.blind_test_and_validation_splits(
            frame,
            min_strict_date_coverage=0.0,
        )

        self.assertEqual(split_policy["validation_mode"], "rolling_calendar")
        self.assertEqual(split_policy["cv_folds_requested"], 3)
        self.assertEqual(split_policy["cv_folds_used"], 2)
        self.assertEqual(len(cv_folds), 2)
        self.assertEqual(cv_folds[0]["validation_start_date"], "2026-05-02")
        self.assertEqual(cv_folds[1]["validation_start_date"], "2026-05-03")

    def test_one_requested_cv_fold_uses_most_recent_pre_test_validation_day(self) -> None:
        frame = self._five_day_progressive_frame()

        one_fold = training.select_cv_lookback_plan(
            frame,
            cv_folds=1,
            bootstrap_fallback=False,
            min_strict_date_coverage=0.0,
        )
        three_folds = training.select_cv_lookback_plan(
            frame,
            cv_folds=3,
            bootstrap_fallback=False,
            min_strict_date_coverage=0.0,
        )

        self.assertEqual(one_fold["split_policy"]["cv_folds_requested"], 1)
        self.assertEqual(one_fold["split_policy"]["cv_folds_used"], 1)
        self.assertEqual(len(one_fold["cv_folds"]), 1)
        self.assertEqual(one_fold["cv_folds"][0]["validation_start_date"], "2026-05-09")
        self.assertTrue(one_fold["lookback_candidates"])

        self.assertEqual(three_folds["cv_folds"][0]["validation_start_date"], "2026-05-07")
        reasons = [reason for fold_reasons in three_folds["rejected_lookbacks"].values() for reason in fold_reasons]
        self.assertIn("fold_1_train_windows_single_class", reasons)

    def test_strict_cv_excludes_undercovered_partial_dates(self) -> None:
        rows = []
        for day_idx, rows_per_day in enumerate([100, 6480, 6480, 6480, 6480]):
            day = pd.Timestamp("2026-05-01") + pd.Timedelta(days=day_idx)
            for row_idx in range(rows_per_day):
                row = {
                    "timestamp": day + pd.Timedelta(seconds=10 * row_idx),
                    training.TARGET_COLUMN: float(row_idx % 2),
                }
                for col in training.RAW_FEATURE_COLUMNS:
                    row[col] = float((row_idx % 10) + 1)
                rows.append(row)
        frame = pd.DataFrame(rows)

        plan = training.select_cv_lookback_plan(frame, cv_folds=3, bootstrap_fallback=False)

        self.assertEqual(plan["split_policy"]["strict_date_min_coverage"], 0.75)
        self.assertEqual(
            [item["date"] for item in plan["split_policy"]["strict_date_excluded_dates"]],
            ["2026-05-01"],
        )
        self.assertEqual(
            [fold["validation_start_date"] for fold in plan["cv_folds"]],
            ["2026-05-03", "2026-05-04"],
        )
        self.assertNotIn(
            pd.Timestamp("2026-05-01").date(),
            pd.to_datetime(plan["cv_folds"][0]["train"]["timestamp"]).dt.date.unique().tolist(),
        )

    def test_blind_test_evidence_counts_buckets_events_and_windows(self) -> None:
        frame = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(
                    [
                        "2026-05-06 10:00:00",
                        "2026-05-06 10:00:10",
                        "2026-05-06 10:00:20",
                        "2026-05-06 10:01:00",
                        "2026-05-06 10:01:10",
                    ]
                ),
                training.TARGET_COLUMN: [1, 1, 0, 1, 1],
            }
        )

        evidence = training.blind_test_evidence_by_lookback(frame, [2])

        self.assertEqual(evidence["positive_buckets"], 4)
        self.assertEqual(evidence["negative_buckets"], 1)
        self.assertEqual(evidence["positive_events"], 2)
        self.assertEqual(evidence["positive_windows_by_lookback"], {2: 3})

    def test_final_training_epochs_use_ceiled_cv_median(self) -> None:
        self.assertEqual(
            training._final_training_epochs_from_cv(
                [{"best_epoch": 2}, {"best_epoch": 3}, {"best_epoch": 7}],
                max_epochs=20,
            ),
            3,
        )
        self.assertEqual(
            training._final_training_epochs_from_cv(
                [{"best_epoch": 2}, {"best_epoch": 5}],
                max_epochs=20,
            ),
            4,
        )
        self.assertEqual(
            training._final_training_epochs_from_cv(
                [{"best_epoch": 50}],
                max_epochs=10,
            ),
            10,
        )

    def test_two_calendar_days_use_emergency_validation_before_hidden_day(self) -> None:
        frame = self._split_frame(days=2, rows_per_day=10)

        blind_splits, cv_folds, split_policy = training.blind_test_and_validation_splits(frame)

        self.assertEqual(split_policy["validation_mode"], "emergency_chronological")
        self.assertEqual(split_policy["cv_folds_requested"], 3)
        self.assertEqual(split_policy["cv_folds_used"], 1)
        self.assertEqual(len(cv_folds), 1)
        self.assertEqual(
            [str(value) for value in pd.to_datetime(blind_splits["test"]["timestamp"]).dt.date.unique().tolist()],
            ["2026-05-02"],
        )
        fold = cv_folds[0]
        self.assertEqual(
            [str(value) for value in pd.to_datetime(fold["train"]["timestamp"]).dt.date.unique().tolist()],
            ["2026-05-01"],
        )
        self.assertEqual(
            [str(value) for value in pd.to_datetime(fold["val"]["timestamp"]).dt.date.unique().tolist()],
            ["2026-05-01"],
        )
        self.assertLess(fold["train"]["timestamp"].max(), fold["val"]["timestamp"].min())
        self.assertEqual(split_policy["blind_test_bounds"]["start_date"], "2026-05-02")
        self.assertEqual(split_policy["cv_fold_bounds"][0]["validation_mode"], "emergency_chronological")

    def test_fewer_than_two_calendar_days_fails_clear_hidden_test_error(self) -> None:
        frame = self._split_frame(days=1, rows_per_day=10)

        with self.assertRaisesRegex(ValueError, "At least 2 calendar days are required for a true 1-day hidden test"):
            training.blind_test_and_validation_splits(frame)

    def test_strict_validation_still_rejects_single_class_first_fold_without_bootstrap(self) -> None:
        frame = self._three_day_bootstrap_frame()

        plan = training.select_cv_lookback_plan(
            frame,
            bootstrap_fallback=False,
            min_strict_date_coverage=0.0,
        )

        self.assertEqual(plan["split_policy"]["validation_mode"], "rolling_calendar")
        self.assertFalse(plan["split_policy"]["bootstrap_fallback_used"])
        self.assertEqual(plan["lookback_candidates"], [])
        reasons = [reason for fold_reasons in plan["rejected_lookbacks"].values() for reason in fold_reasons]
        self.assertIn("fold_1_train_windows_single_class", reasons)

    def test_bootstrap_fallback_selects_chronological_fold_after_strict_rejection(self) -> None:
        frame = self._three_day_bootstrap_frame()

        plan = training.select_cv_lookback_plan(
            frame,
            bootstrap_fallback=True,
            min_strict_date_coverage=0.0,
        )

        self.assertEqual(plan["split_policy"]["validation_mode"], "bootstrap_chronological_fallback")
        self.assertTrue(plan["split_policy"]["bootstrap_fallback_used"])
        self.assertEqual(plan["split_policy"]["bootstrap_fallback_reason"], "strict_validation_no_viable_lookback_candidates")
        self.assertTrue(plan["lookback_candidates"])
        self.assertIn("strict_rejected_lookbacks", plan["split_policy"])
        self.assertEqual(plan["cv_folds"][0]["validation_mode"], "bootstrap_chronological_fallback")
        self.assertLess(plan["cv_folds"][0]["train"]["timestamp"].max(), plan["cv_folds"][0]["val"]["timestamp"].min())

    def test_bootstrap_fallback_rejects_single_class_training_segment(self) -> None:
        frame = self._three_day_bootstrap_frame(bootstrap_train_single_class=True)

        plan = training.select_cv_lookback_plan(
            frame,
            bootstrap_fallback=True,
            min_strict_date_coverage=0.0,
        )

        self.assertEqual(plan["split_policy"]["validation_mode"], "bootstrap_chronological_fallback")
        self.assertEqual(plan["lookback_candidates"], [])
        reasons = [reason for fold_reasons in plan["rejected_lookbacks"].values() for reason in fold_reasons]
        self.assertIn("fold_1_train_windows_single_class", reasons)

    def test_median_fill_indicators_and_unlabeled_window_skip(self) -> None:
        rows = []
        timestamps = pd.to_datetime(
            [
                "2026-05-06 10:00:00",
                "2026-05-06 10:01:00",
                "2026-05-06 10:02:00",
                "2026-05-06 10:03:00",
            ]
        )
        targets = [0, None, 1, None]
        for idx, timestamp in enumerate(timestamps):
            row = {"timestamp": timestamp, "zone_occupied": targets[idx]}
            for col in training.RAW_FEATURE_COLUMNS:
                row[col] = float(idx + 1)
            rows.append(row)
        rows[1]["sen55_voc"] = None

        cleaned = training._clean_zone_5_training_frame(pd.DataFrame(rows), "unit")
        self.assertEqual(len(cleaned), 4)
        self.assertEqual(int(cleaned.loc[1, "sen55_voc_missing"]), 1)

        splits = {"train": cleaned.iloc[:3].copy(), "val": cleaned.iloc[3:].copy()}
        scaled, _stats, fill_values = training.prepare_and_standardize_splits(splits)
        self.assertIn("sen55_voc", fill_values)
        self.assertFalse(scaled["train"][training.FEATURE_COLUMNS].isna().any().any())
        self.assertIn("sen55_voc_missing", training.MISSING_INDICATOR_COLUMNS)
        self.assertNotIn("sen55_voc_missing", training.FEATURE_COLUMNS)

        X, y, ts = training.make_windows(scaled["train"], lookback=1)
        self.assertEqual(y.tolist(), [0.0, 1.0])
        self.assertEqual(len(ts), 2)
        self.assertFalse(pd.isna(X).any())

    def test_feature_contract_keeps_raw_sensors_but_excludes_missingness_channels(self) -> None:
        for col in [*training.RAW_FEATURE_COLUMNS, *training.TIME_FEATURE_COLUMNS]:
            self.assertIn(col, training.FEATURE_COLUMNS)
        self.assertFalse(any(col.endswith("_missing") for col in training.FEATURE_COLUMNS))
        self.assertEqual(training.INPUT_CHANNEL_COUNT, len(training.FEATURE_COLUMNS))

    def test_core_missingness_blocks_training_windows_but_sen55_missing_does_not(self) -> None:
        rows = []
        for idx in range(3):
            row = {
                "timestamp": pd.Timestamp("2026-05-06 10:00:00") + pd.Timedelta(seconds=10 * idx),
                training.TARGET_COLUMN: float(idx % 2),
            }
            for col in training.RAW_FEATURE_COLUMNS:
                row[col] = float(idx + 1)
            rows.append(row)
        frame = training._clean_zone_5_training_frame(pd.DataFrame(rows), "unit")

        sen55_missing = frame.copy()
        sen55_missing["sen55_voc"] = pd.NA
        sen55_prepared = training.apply_training_preprocessing(
            sen55_missing,
            {col: 0.0 for col in training.RAW_FEATURE_COLUMNS},
        )
        self.assertEqual(training.make_windows(sen55_prepared, lookback=1)[1].tolist(), [0.0, 1.0, 0.0])

        core_missing = frame.copy()
        core_missing["mmwave_s5"] = pd.NA
        core_prepared = training.apply_training_preprocessing(
            core_missing,
            {col: 0.0 for col in training.RAW_FEATURE_COLUMNS},
        )
        self.assertEqual(training.make_windows(core_prepared, lookback=1)[1].tolist(), [])

    def test_missing_sen55_warns_but_does_not_block_live_prediction(self) -> None:
        root = fresh_test_dir("sen55_live_optional")
        run_dir = write_minimal_zone5_artifact(root, lookback=3)
        frame = self._live_feature_frame(rows=3)
        for col in training.SEN55_FEATURE_COLUMNS:
            frame[col] = pd.NA

        probability, diagnostics = training.predict_zone_5_probability(
            frame,
            artifact_dir=run_dir,
            reference_time=frame[training.TIMESTAMP_COLUMN].iloc[-1],
            max_age_minutes=None,
            return_diagnostics=True,
        )

        self.assertTrue(np.isfinite(probability))
        self.assertIn("SEN55 unavailable; using neutral fill", diagnostics["warnings"])

    def test_missing_core_sensor_blocks_live_prediction(self) -> None:
        root = fresh_test_dir("core_live_gate")
        run_dir = write_minimal_zone5_artifact(root, lookback=3)
        frame = self._live_feature_frame(rows=3)
        frame["mmwave_s5"] = pd.NA

        with self.assertRaisesRegex(ValueError, "LIVE DATA DEGRADED: .*mmwave_s5"):
            training.predict_zone_5_probability(
                frame,
                artifact_dir=run_dir,
                reference_time=frame[training.TIMESTAMP_COLUMN].iloc[-1],
                max_age_minutes=None,
            )

    def test_legacy_missingness_channel_artifact_is_rejected(self) -> None:
        legacy_feature_columns = [
            *training.RAW_FEATURE_COLUMNS,
            *training.MISSING_INDICATOR_COLUMNS,
            *training.TIME_FEATURE_COLUMNS,
        ]

        with self.assertRaisesRegex(ValueError, "legacy missingness feature channels"):
            training.require_10_second_model_contract(
                {
                    "model_contract_version": training.MODEL_CONTRACT_VERSION,
                    "sample_interval_seconds": training.SAMPLE_INTERVAL_SECONDS,
                    "feature_columns": legacy_feature_columns,
                },
                {
                    "model_contract_version": training.MODEL_CONTRACT_VERSION,
                    "sample_interval_seconds": training.SAMPLE_INTERVAL_SECONDS,
                },
                {
                    "model_contract_version": training.MODEL_CONTRACT_VERSION,
                    "sample_interval_seconds": training.SAMPLE_INTERVAL_SECONDS,
                },
            )

    def test_sen55_missingness_only_does_not_change_prediction_with_neutral_values(self) -> None:
        root = fresh_test_dir("sen55_missingness_neutral")
        run_dir = write_minimal_zone5_artifact(root, lookback=3)
        available = self._live_feature_frame(rows=3, value=1.0)
        for col in training.SEN55_FEATURE_COLUMNS:
            available[col] = 0.0
            available[f"{col}_missing"] = 0
        missing = available.copy()
        for col in training.SEN55_FEATURE_COLUMNS:
            missing[f"{col}_missing"] = 1

        prob_available = training.predict_zone_5_probability(
            available,
            artifact_dir=run_dir,
            reference_time=available[training.TIMESTAMP_COLUMN].iloc[-1],
            max_age_minutes=None,
        )
        prob_missing = training.predict_zone_5_probability(
            missing,
            artifact_dir=run_dir,
            reference_time=missing[training.TIMESTAMP_COLUMN].iloc[-1],
            max_age_minutes=None,
        )

        self.assertAlmostEqual(float(prob_available), float(prob_missing), places=7)

    def test_lookback_minutes_are_converted_to_ten_second_rows(self) -> None:
        self.assertEqual(training.LOOKBACK_ROWS_BY_MINUTES, {15: 90, 60: 360, 180: 1080})
        self.assertEqual(training.LOOKBACK_CHOICES, [90, 360, 1080])


class LiveAppendAndWebFeatureTests(unittest.TestCase):
    def _row(self, timestamp: str, value: float, target: float | None = None) -> dict[str, float | str | None]:
        row: dict[str, float | str | None] = {"timestamp": timestamp, "zone_occupied": target}
        for col in training.RAW_FEATURE_COLUMNS:
            row[col] = value
        return row

    def test_live_append_deduplicates_existing_timestamps(self) -> None:
        root = fresh_test_dir("live_append")
        output_csv = root / "zone5_training_cv.csv"
        existing = pd.DataFrame(
            [
                self._row("2026-05-06 10:00:00", 1.0, 0.0),
                self._row("2026-05-06 10:01:00", 2.0, 0.0),
            ]
        )
        existing.to_csv(output_csv, index=False)
        new_rows = pd.DataFrame(
            [
                self._row("2026-05-06 10:01:00", 20.0, 1.0),
                self._row("2026-05-06 10:02:00", 3.0, 1.0),
            ]
        )

        combined, summary = collect_training_data._append_rows_to_csv(output_csv, new_rows)
        reloaded = pd.read_csv(output_csv)

        self.assertEqual(summary["inserted_rows"], 1)
        self.assertEqual(summary["updated_rows"], 1)
        self.assertEqual(len(combined), 3)
        self.assertEqual(len(reloaded), 3)
        self.assertEqual(reloaded["timestamp"].tolist(), [
            "2026-05-06 10:00:00",
            "2026-05-06 10:01:00",
            "2026-05-06 10:02:00",
        ])
        self.assertEqual(float(reloaded.loc[1, "temp_s5"]), 20.0)
        self.assertEqual(float(reloaded.loc[1, "zone_occupied"]), 1.0)

    def test_live_append_preserves_features_when_label_update_is_sparse(self) -> None:
        root = fresh_test_dir("live_append_sparse_label")
        output_csv = root / "zone5_training_cv.csv"
        existing = pd.DataFrame([self._row("2026-05-06 10:00:00", 24.0, None)])
        existing.to_csv(output_csv, index=False)
        new_rows = pd.DataFrame(
            {
                "timestamp": ["2026-05-06 10:00:00"],
                "zone_occupied": [1.0],
                "occupancy_count": [2.0],
                "sample_count": [5.0],
            }
        )

        combined, summary = collect_training_data._append_rows_to_csv(output_csv, new_rows)
        reloaded = pd.read_csv(output_csv)

        self.assertEqual(summary["inserted_rows"], 0)
        self.assertEqual(summary["updated_rows"], 1)
        self.assertEqual(len(combined), 1)
        self.assertEqual(float(reloaded.loc[0, "temp_s5"]), 24.0)
        self.assertEqual(float(reloaded.loc[0, "zone_occupied"]), 1.0)
        self.assertEqual(float(reloaded.loc[0, "occupancy_count"]), 2.0)

    def test_live_append_preserves_features_when_sen55_update_is_sparse(self) -> None:
        root = fresh_test_dir("live_append_sparse_sen55")
        output_csv = root / "zone5_training_cv.csv"
        existing_row = self._row("2026-05-06 10:00:00", 24.0, None)
        for col in [
            "sen55_pm1_0",
            "sen55_pm2_5",
            "sen55_pm4_0",
            "sen55_pm10_0",
            "sen55_temperature",
            "sen55_humidity",
            "sen55_voc",
            "sen55_nox",
        ]:
            existing_row[col] = None
        pd.DataFrame([existing_row]).to_csv(output_csv, index=False)
        new_rows = pd.DataFrame(
            {
                "timestamp": ["2026-05-06 10:00:00"],
                "sen55_pm1_0": [1.5],
                "sen55_pm2_5": [2.5],
                "sen55_temperature": [25.5],
            }
        )

        combined, summary = collect_training_data._append_rows_to_csv(output_csv, new_rows)
        reloaded = pd.read_csv(output_csv)

        self.assertEqual(summary["inserted_rows"], 0)
        self.assertEqual(summary["updated_rows"], 1)
        self.assertEqual(len(combined), 1)
        self.assertEqual(float(reloaded.loc[0, "temp_s5"]), 24.0)
        self.assertEqual(float(reloaded.loc[0, "sen55_pm1_0"]), 1.5)
        self.assertEqual(float(reloaded.loc[0, "sen55_pm2_5"]), 2.5)
        self.assertEqual(float(reloaded.loc[0, "sen55_temperature"]), 25.5)

    def test_live_append_filters_cv_labels_to_requested_window(self) -> None:
        raw_labels = pd.DataFrame(
            {
                "timestamp": [
                    "2026-05-06 10:00:00",
                    "2026-05-06 10:00:10",
                    "2026-05-06 10:00:20",
                ],
                "occupancy_count": [0, 1, 0],
            }
        )

        filtered = collect_training_data._filter_labels_to_time_window(
            raw_labels,
            time_start=datetime(2026, 5, 6, 2, 0, 10),
            time_end=datetime(2026, 5, 6, 2, 0, 20),
        )

        self.assertIsNotNone(filtered)
        self.assertEqual(filtered["timestamp"].astype(str).tolist(), ["2026-05-06 10:00:10"])
        self.assertEqual(filtered["occupancy_count"].tolist(), [1])

    def test_live_append_bounds_refetch_backfill_window(self) -> None:
        args = argparse.Namespace(
            duration_min=60.0,
            time_start=None,
            time_end=None,
            backfill_sec=120.0,
        )

        bounds = collect_training_data._live_window_bounds(
            args,
            latest_local_timestamp=pd.Timestamp("2026-05-06 10:05:00"),
            now_utc=datetime(2026, 5, 6, 2, 5, 47),
        )

        self.assertEqual(bounds, (datetime(2026, 5, 6, 2, 3, 0), datetime(2026, 5, 6, 2, 5, 40)))

    def test_live_append_bounds_can_disable_backfill(self) -> None:
        args = argparse.Namespace(
            duration_min=60.0,
            time_start=None,
            time_end=None,
            backfill_sec=0.0,
        )

        bounds = collect_training_data._live_window_bounds(
            args,
            latest_local_timestamp=pd.Timestamp("2026-05-06 10:05:00"),
            now_utc=datetime(2026, 5, 6, 2, 5, 47),
        )

        self.assertEqual(bounds, (datetime(2026, 5, 6, 2, 5, 10), datetime(2026, 5, 6, 2, 5, 40)))

    def test_csv_size_guard_keeps_single_rolling_csv(self) -> None:
        root = fresh_test_dir("csv_size_guard")
        output_csv = root / "zone5_training_cv.csv"
        frame = pd.DataFrame(
            [
                self._row("2026-05-06 10:00:00", 1000.123, 0.0),
                self._row("2026-05-06 10:01:00", 2000.456, 1.0),
                self._row("2026-05-06 10:02:00", 3000.789, 1.0),
            ]
        )

        parts = csv_size_guard.write_dataframe_rolling_atomic(frame, output_csv, max_bytes=420)
        reloaded = csv_size_guard.read_csv_parts(output_csv)

        self.assertEqual(parts, [output_csv])
        self.assertLessEqual(output_csv.stat().st_size, 420)
        self.assertFalse(list(root.glob("zone5_training_cv_part*.csv")))
        self.assertLess(len(reloaded), len(frame))
        self.assertEqual(reloaded["timestamp"].tolist(), frame["timestamp"].tail(len(reloaded)).tolist())

    def test_web_live_feature_builder_merges_sen55_or_marks_missing(self) -> None:
        root = fresh_test_dir("web_sen55")
        sen55_csv = root / "sen55_data.csv"
        pd.DataFrame(
            {
                "timestamp": ["2026-05-06 10:00:11", "2026-05-06 10:00:19"],
                "pm1_0": [1.0, 3.0],
                "pm2_5": [2.0, 4.0],
                "pm4_0": [5.0, 7.0],
                "pm10_0": [8.0, 10.0],
                "temperature": [25.0, 27.0],
                "humidity": [55.0, 57.0],
                "voc": [100.0, 120.0],
                "nox": [8.0, 10.0],
            }
        ).to_csv(sen55_csv, index=False)
        frame = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(["2026-05-06 10:00:10", "2026-05-06 10:00:20"]),
                "temp_s5": [24.0, 24.1],
                "rh_s5": [50.0, 50.1],
                "co2_s5": [700.0, 705.0],
                "pm25_s5": [12.0, 12.1],
                "power_s5": [3.0, 3.1],
                "mmwave_s5": [0.0, 1.0],
            }
        )
        data_source = object.__new__(LiveAir1DataSource)
        data_source.sen55_csv = sen55_csv
        merged = LiveAir1DataSource._merge_sen55_features(data_source, frame)
        shared = feature_builder.merge_sen55_features(frame, sen55_csv)

        self.assertEqual(float(merged.loc[0, "sen55_pm1_0"]), 2.0)
        self.assertTrue(pd.isna(merged.loc[1, "sen55_pm1_0"]))
        pd.testing.assert_frame_equal(
            merged[training.RAW_FEATURE_COLUMNS],
            shared[training.RAW_FEATURE_COLUMNS],
            check_dtype=False,
        )

        data_source.sen55_csv = root / "missing_sen55.csv"
        missing = LiveAir1DataSource._merge_sen55_features(data_source, frame)
        shared_missing = feature_builder.merge_sen55_features(frame, data_source.sen55_csv)
        for col in training.RAW_FEATURE_COLUMNS:
            self.assertIn(col, missing.columns)
            self.assertIn(col, shared_missing.columns)
        self.assertTrue(missing["sen55_pm1_0"].isna().all())
        pd.testing.assert_frame_equal(
            missing[training.RAW_FEATURE_COLUMNS],
            shared_missing[training.RAW_FEATURE_COLUMNS],
            check_dtype=False,
        )

    def test_sen55_duplicate_samples_do_not_bias_ten_second_average(self) -> None:
        root = fresh_test_dir("sen55_csv")
        csv_path = root / "sen55_data.csv"
        pd.DataFrame(
            {
                "timestamp": [
                    "2026-05-06 10:00:15",
                    "2026-05-06 10:00:15",
                    "2026-05-06 10:00:19",
                ],
                "sensor_id": ["sen55_01", "sen55_01", "sen55_01"],
                "location": ["Lab area", "Lab area", "Lab area"],
                "room": ["Lab", "Lab", "Lab"],
                "pm1_0": [1.0, 1.0, 3.0],
                "pm2_5": [2.0, 2.0, 4.0],
                "pm4_0": [5.0, 5.0, 7.0],
                "pm10_0": [8.0, 8.0, 10.0],
                "temperature": [25.0, 25.0, 27.0],
                "humidity": [55.0, 55.0, 57.0],
                "voc": [100.0, 100.0, 120.0],
                "nox": [8.0, 8.0, 10.0],
            }
        ).to_csv(csv_path, index=False)

        normalized = sen55_mqtt_collector.normalize_sen55_frame(csv_size_guard.read_csv_parts(csv_path))
        by_minute = feature_builder.aggregate_sen55_by_minute(normalized)
        self.assertEqual(len(by_minute), 1)
        self.assertEqual(float(by_minute.loc[0, "sen55_pm1_0"]), 2.0)

        frame = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(["2026-05-06 10:00:10"]),
                "temp_s5": [24.0],
                "rh_s5": [50.0],
                "co2_s5": [700.0],
                "pm25_s5": [12.0],
                "power_s5": [3.0],
                "mmwave_s5": [0.0],
            }
        )
        merged = feature_builder.merge_sen55_features(frame, csv_path)
        self.assertEqual(float(merged.loc[0, "sen55_pm1_0"]), 2.0)

    def test_sen55_bucket_buffer_writes_mean_values_and_latest_metadata(self) -> None:
        root = fresh_test_dir("sen55_bucket_buffer")
        csv_path = root / "sen55_data.csv"
        buffer = sen55_mqtt_collector.Sen55BucketBuffer(csv_path)

        buffer.add_payload(
            {
                "timestamp": "2026-05-06 10:00:11",
                "sensor_id": "sen55_01",
                "location": "Lab area old",
                "room": "Lab",
                "pm1_0": 1.0,
                "temperature": 25.0,
                "voc": 100.0,
            }
        )
        buffer.add_payload(
            {
                "timestamp": "2026-05-06 10:00:19",
                "sensor_id": "sen55_01",
                "location": "Lab area",
                "room": "Lab B",
                "pm1_0": 3.0,
                "temperature": 27.0,
                "voc": 120.0,
            }
        )

        flushed = buffer.flush_completed(force=True)
        self.assertEqual(len(flushed), 1)
        written = pd.read_csv(csv_path)
        self.assertEqual(len(written), 1)
        self.assertEqual(written.loc[0, "timestamp"], "2026-05-06 10:00:10")
        self.assertEqual(written.loc[0, "location"], "Lab area")
        self.assertEqual(written.loc[0, "room"], "Lab B")
        self.assertEqual(float(written.loc[0, "pm1_0"]), 2.0)
        self.assertEqual(float(written.loc[0, "temperature"]), 26.0)
        self.assertEqual(float(written.loc[0, "voc"]), 110.0)

    def test_sen55_late_payload_after_flush_is_skipped_without_duplicate_or_full_rewrite(self) -> None:
        root = fresh_test_dir("sen55_late_payload")
        csv_path = root / "sen55_data.csv"
        buffer = sen55_mqtt_collector.Sen55BucketBuffer(csv_path)
        buffer.add_payload({"timestamp": "2026-05-06 10:00:11", "sensor_id": "sen55_01", "pm1_0": 1.0})
        buffer.flush_completed(force=True)

        with mock.patch.object(
            sen55_mqtt_collector.csv_size_guard,
            "write_dataframe_rolling_atomic",
            side_effect=AssertionError("late payload should not force a full CSV rewrite"),
        ):
            result = buffer.add_payload(
                {"timestamp": "2026-05-06 10:00:12", "sensor_id": "sen55_01", "pm1_0": 9.0}
            )

        self.assertFalse(result["accepted"])
        self.assertEqual(buffer.skipped_late_payloads, 1)
        written = pd.read_csv(csv_path)
        self.assertEqual(len(written), 1)
        self.assertEqual(float(written.loc[0, "pm1_0"]), 1.0)

    def test_training_metadata_refresh_preserves_ten_second_rows(self) -> None:
        root = fresh_test_dir("training_metadata_refresh")
        output_csv = root / "zone5_training_cv.csv"
        metadata_path = root / "zone5_training_cv.metadata.json"
        frame = pd.DataFrame(
            [
                self._row("2026-05-06 10:00:11", 1.0, 0.0),
                self._row("2026-05-06 10:00:22", 2.0, 1.0),
            ]
        )
        frame.to_csv(output_csv, index=False)

        metadata = collect_training_data._write_metadata_from_csv(
            output_csv=output_csv,
            metadata_path=metadata_path,
            sen55_csv=root / "sen55_data.csv",
            cv_labels=root / "cv_occupancy_zone5_10sec.csv",
            occupied_threshold=1.0,
            requested_time_range={
                "start": datetime(2026, 5, 6, 2, 0, 0),
                "end": datetime(2026, 5, 6, 2, 1, 0),
            },
            fetch_metadata=None,
            elapsed_seconds=None,
            collector_mode="unit",
        )

        self.assertEqual(metadata["sample_interval_seconds"], 10)
        self.assertEqual(metadata["row_count"], 2)
        written_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        self.assertEqual(written_metadata["requested_time_range_utc"]["start"], "2026-05-06 02:00:00")
        self.assertEqual(written_metadata["requested_time_range_utc"]["end"], "2026-05-06 02:01:00")

    def test_training_metadata_can_update_without_runtime_parquet(self) -> None:
        root = fresh_test_dir("training_metadata_without_parquet")
        output_csv = root / "zone5_training_cv.csv"
        metadata_path = root / "zone5_training_cv.metadata.json"
        pd.DataFrame([self._row("2026-05-06 10:00:11", 1.0, 0.0)]).to_csv(output_csv, index=False)

        metadata = collect_training_data._write_metadata_from_csv(
            output_csv=output_csv,
            metadata_path=metadata_path,
            sen55_csv=root / "sen55_data.csv",
            cv_labels=root / "cv_occupancy_zone5_10sec.csv",
            occupied_threshold=1.0,
            requested_time_range=None,
            fetch_metadata=None,
            elapsed_seconds=None,
            collector_mode="unit",
        )

        self.assertTrue(metadata_path.is_file())
        self.assertFalse((root / "zone5_training_cv.parquet").exists())
        self.assertNotIn("parquet_enabled", metadata["outputs"])
        self.assertNotIn("parquet_path", metadata["outputs"])

    def test_live_collector_refresh_clock_uses_existing_metadata_mtime(self) -> None:
        root = fresh_test_dir("collector_refresh_clock")
        metadata_path = root / "zone5_training_cv.metadata.json"
        metadata_path.write_text("{}", encoding="utf-8")
        old_time = time.time() - 7200
        collect_training_data.os.utime(metadata_path, (old_time, old_time))

        last_refresh_mono = collect_training_data._initial_snapshot_refresh_mono(metadata_path)

        self.assertGreaterEqual(time.monotonic() - last_refresh_mono, 3600)

    def test_runtime_collectors_do_not_accept_top_level_parquet_outputs(self) -> None:
        with mock.patch.object(sys, "argv", ["prog"]):
            cv_args = occupancy_mqtt_aggregator.parse_args()
        with mock.patch.object(sys, "argv", ["prog"]):
            sen55_args = sen55_mqtt_collector.parse_args()
        with mock.patch.object(sys, "argv", ["prog"]):
            collector_args = collect_training_data.parse_args()
        with mock.patch.object(sys, "argv", ["prog"]):
            builder_args = cv_training_builder.parse_args()

        self.assertFalse(hasattr(cv_args, "output_parquet"))
        self.assertFalse(hasattr(sen55_args, "output_parquet"))
        self.assertFalse(hasattr(collector_args, "output_parquet"))
        self.assertFalse(hasattr(builder_args, "output"))
        self.assertEqual(builder_args.labels, cv_training_builder.DEFAULT_LABELS_CSV)
        for module in [occupancy_mqtt_aggregator, sen55_mqtt_collector, collect_training_data]:
            with self.subTest(module=module.__name__), mock.patch.object(
                sys,
                "argv",
                ["prog", "--output-parquet", str(TEST_RUNTIME_ROOT / "cache.parquet")],
            ):
                with self.assertRaises(SystemExit):
                    module.parse_args()

    def test_live_collector_snapshot_flags_include_legacy_aliases(self) -> None:
        with mock.patch.object(
            sys,
            "argv",
            ["prog", "--snapshot-refresh-every-hours", "0.5", "--retrain-after-snapshot"],
        ):
            args = collect_training_data.parse_args()
        self.assertEqual(float(args.snapshot_refresh_every_hours), 0.5)
        self.assertTrue(args.retrain_after_snapshot)

        with mock.patch.object(
            sys,
            "argv",
            ["prog", "--parquet-rebuild-every-hours", "2", "--retrain-after-parquet"],
        ):
            legacy_args = collect_training_data.parse_args()
        self.assertEqual(float(legacy_args.snapshot_refresh_every_hours), 2.0)
        self.assertTrue(legacy_args.retrain_after_snapshot)

        sh = (Path(__file__).resolve().parents[1] / "run_live_collector.sh").read_text(encoding="utf-8")
        self.assertIn('COLLECTOR_EXTRA_ARGS+=(--retrain-after-snapshot)', sh)
        self.assertIn('SNAPSHOT_REFRESH_EVERY_HOURS="${SNAPSHOT_REFRESH_EVERY_HOURS:-${PARQUET_REBUILD_EVERY_HOURS:-1}}"', sh)

    def test_replay_table_reads_csv_without_training_parquet(self) -> None:
        root = fresh_test_dir("replay_table_csv")
        timestamps = pd.date_range("2026-05-06 10:00:00", periods=10, freq="10s")
        training_rows = pd.DataFrame({"timestamp": timestamps, training.TARGET_COLUMN: [0, 1] * 5})
        for col in training.RAW_FEATURE_COLUMNS:
            training_rows[col] = 1.0
        csv_path = root / "zone5_training_cv.csv"
        training_rows.to_csv(csv_path, index=False)

        replay = ReplayTableDataSource(csv_path, slack_rows=0)
        window, latest_ts = replay.get_latest_window(5)

        self.assertEqual(len(window), 5)
        self.assertEqual(pd.Timestamp(latest_ts), timestamps[4])
        self.assertEqual(replay.label, "replay[zone5_training_cv.csv]")

    def test_replay_env_requires_explicit_table(self) -> None:
        root = fresh_test_dir("replay_table_env")
        timestamps = pd.date_range("2026-05-06 10:00:00", periods=6, freq="10s")
        training_rows = pd.DataFrame({"timestamp": timestamps, training.TARGET_COLUMN: [0, 1, 0, 1, 0, 1]})
        for col in training.RAW_FEATURE_COLUMNS:
            training_rows[col] = 1.0
        csv_path = root / "zone5_training_cv.csv"
        training_rows.to_csv(csv_path, index=False)

        source = build_data_source_from_env({"ZONE5_DATA_SOURCE": "replay", "ZONE5_REPLAY_TABLE": str(csv_path)})
        window, latest_ts = source.get_latest_window(3)

        self.assertEqual(len(window), 3)
        self.assertEqual(pd.Timestamp(latest_ts), timestamps[2])
        with self.assertRaisesRegex(RuntimeError, "ZONE5_REPLAY_TABLE"):
            build_data_source_from_env({"ZONE5_DATA_SOURCE": "replay", "ZONE5_REPLAY_PARQUET": str(csv_path)})

    def test_live_retrain_auto_promotion_records_passed_gate(self) -> None:
        root = fresh_test_dir("live_retrain_auto_promote")
        production_pointer = root / "production_run.txt"
        args = argparse.Namespace(
            retrain_output_dir=root / "model",
            production_pointer=production_pointer,
            promote_skip_smoke=False,
            promote_skip_non_regression_smoke=False,
        )

        def fake_run(cmd, cwd, text, capture_output, check):
            production_pointer.write_text("model/runs/good\n", encoding="utf-8")
            return collect_training_data.subprocess.CompletedProcess(
                cmd,
                0,
                stdout="Promoted CV-target run to production",
                stderr="",
            )

        with mock.patch.object(collect_training_data.subprocess, "run", side_effect=fake_run) as run_mock:
            summary = collect_training_data._promote_after_retrain(args)

        self.assertEqual(summary["status"], "promoted")
        self.assertEqual(summary["returncode"], 0)
        command = run_mock.call_args.args[0]
        self.assertIn("zone5.promote_model", command)
        self.assertIn("--candidate-run", command)
        self.assertIn(str(root / "model"), command)
        self.assertIn("--production-pointer", command)
        self.assertIn(str(production_pointer), command)
        self.assertEqual(summary["current"], "model/runs/good")

    def test_live_retrain_bootstrap_auto_only_when_no_production_pointer(self) -> None:
        root = fresh_test_dir("live_retrain_bootstrap_auto")
        production_pointer = root / "production_run.txt"
        args = argparse.Namespace(
            retrain_bootstrap_fallback="auto",
            production_pointer=production_pointer,
        )

        self.assertTrue(collect_training_data._retrain_bootstrap_fallback_enabled(args))
        production_pointer.write_text("\n", encoding="utf-8")
        self.assertTrue(collect_training_data._retrain_bootstrap_fallback_enabled(args))
        production_pointer.write_text("model/runs/current\n", encoding="utf-8")
        self.assertFalse(collect_training_data._retrain_bootstrap_fallback_enabled(args))

        args.retrain_bootstrap_fallback = "always"
        self.assertTrue(collect_training_data._retrain_bootstrap_fallback_enabled(args))
        args.retrain_bootstrap_fallback = "never"
        self.assertFalse(collect_training_data._retrain_bootstrap_fallback_enabled(args))

    def test_live_retrain_cv_folds_auto_progresses_from_production_metadata(self) -> None:
        root = fresh_test_dir("live_retrain_cv_folds_auto")
        production_pointer = root / "production_run.txt"
        args = argparse.Namespace(
            retrain_cv_folds="auto",
            production_pointer=production_pointer,
        )

        def write_policy_run(run_id: str, validation_mode: str, cv_folds_used: int) -> Path:
            run_dir = root / "runs" / run_id
            tables_dir = run_dir / "tables"
            tables_dir.mkdir(parents=True, exist_ok=True)
            split_policy = {
                "validation_mode": validation_mode,
                "cv_folds": cv_folds_used,
                "cv_folds_requested": cv_folds_used,
                "cv_folds_used": cv_folds_used,
            }
            (run_dir / training.RUN_MANIFEST_FILENAME).write_text(
                json.dumps({"run_id": run_id, "validation_mode": validation_mode, "split_policy": split_policy}),
                encoding="utf-8",
            )
            (tables_dir / "metrics_zone_5.json").write_text(
                json.dumps({"validation_mode": validation_mode, "split_policy": split_policy}),
                encoding="utf-8",
            )
            return run_dir

        self.assertEqual(collect_training_data._retrain_cv_folds_policy(args)["cv_folds"], 1)

        for run_id, validation_mode, current_folds, expected_folds in [
            ("bootstrap", training.BOOTSTRAP_VALIDATION_MODE, 1, 1),
            ("strict_1", training.STRICT_VALIDATION_MODE, 1, 2),
            ("strict_2", training.STRICT_VALIDATION_MODE, 2, 3),
            ("strict_3", training.STRICT_VALIDATION_MODE, 3, 3),
        ]:
            production_run = write_policy_run(run_id, validation_mode, current_folds)
            production_pointer.write_text(str(production_run) + "\n", encoding="utf-8")
            with self.subTest(run_id=run_id):
                policy = collect_training_data._retrain_cv_folds_policy(args)
                self.assertEqual(policy["mode"], "auto")
                self.assertEqual(policy["cv_folds"], expected_folds)

        args.retrain_cv_folds = "2"
        explicit_policy = collect_training_data._retrain_cv_folds_policy(args)
        self.assertEqual(explicit_policy["reason"], "explicit_retrain_cv_folds")
        self.assertEqual(explicit_policy["cv_folds"], 2)

    def test_retrain_once_skips_when_blind_test_cannot_meet_positive_window_gate(self) -> None:
        root = fresh_test_dir("retrain_preflight_positive_gate")
        source = root / "zone5_training_cv.parquet"
        rows = []
        start = pd.Timestamp("2026-05-06 00:00:00")
        for day_idx in range(3):
            for row_idx in range(100):
                target = float(row_idx % 2) if day_idx < 2 else 0.0
                row = {
                    "timestamp": start + pd.Timedelta(days=day_idx, seconds=10 * row_idx),
                    training.TARGET_COLUMN: target,
                }
                for col in training.RAW_FEATURE_COLUMNS:
                    row[col] = float(row_idx)
                rows.append(row)
        pd.DataFrame(rows).to_parquet(source, index=False)

        summary_path = root / "model" / "retrain_status.json"
        argv = [
            "zone5.retrain_once",
            "--source-parquet",
            str(source),
            "--snapshot-source",
            "parquet",
            "--snapshot-dir",
            str(root / "snapshots"),
            "--lock-file",
            str(root / "model" / "retrain.lock"),
            "--summary-json",
            str(summary_path),
            "--output-dir",
            str(root / "model"),
            "--n-trials",
            "1",
            "--max-epochs",
            "1",
            "--bootstrap-fallback",
            "never",
            "--cv-folds",
            "1",
            "--min-positive-windows",
            "5",
            "--min-strict-date-coverage",
            "0",
        ]

        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(retrain_once.main(), 0)

        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        self.assertEqual(summary["status"], "skipped_not_promotable_yet")
        self.assertEqual(summary["positive_windows_by_lookback"], {"90": 0})
        self.assertEqual(summary["positive_buckets"], 0)
        self.assertEqual(summary["positive_events"], 0)
        self.assertFalse((root / "model" / training.CURRENT_RUN_POINTER).exists())

    def test_retrain_once_csv_snapshot_uses_latest_csv_not_stale_parquet(self) -> None:
        root = fresh_test_dir("retrain_csv_snapshot")
        source_csv = root / "zone5_training_cv.csv"
        source_parquet = root / "zone5_training_cv.parquet"
        rows = []
        for idx in range(120):
            row = {
                "timestamp": pd.Timestamp("2026-05-06 10:00:00") + pd.Timedelta(seconds=10 * idx),
                training.TARGET_COLUMN: float(idx % 2),
            }
            for col in training.RAW_FEATURE_COLUMNS:
                row[col] = float(idx)
            rows.append(row)
        pd.DataFrame(rows[:90]).to_parquet(source_parquet, index=False)
        pd.DataFrame(rows).to_csv(source_csv, index=False)
        args = argparse.Namespace(
            source_csv=source_csv,
            source_parquet=source_parquet,
            source_metadata=root / "missing.metadata.json",
            snapshot_source="csv",
            snapshot_dir=root / "snapshots",
            snapshot_wait_timeout_sec=5.0,
            snapshot_settle_sec=0.0,
            allow_bad_lines=False,
        )

        snapshot = retrain_once._snapshot_training_source(args)

        snapped = pd.read_parquet(snapshot["snapshot_parquet"])
        self.assertEqual(snapshot["source_format"], "csv")
        self.assertEqual(pd.to_datetime(snapped["timestamp"]).max(), pd.Timestamp("2026-05-06 10:19:50"))


class PackageAuditTests(unittest.TestCase):
    def test_file_backed_mjpeg_grabber_streams_latest_jpeg_and_marks_stale_files_disconnected(self) -> None:
        from web_app.rtsp_stream import FileBackedMjpegGrabber

        root = fresh_test_dir("file_backed_mjpeg")
        jpeg_path = root / "latest.jpg"

        missing = FileBackedMjpegGrabber(jpeg_path, max_age_sec=5.0)
        missing_chunk = next(missing.mjpeg_iterator(target_fps=100.0))
        missing.stop()
        self.assertFalse(missing.connected)
        self.assertIsNone(missing.latest_frame_age)
        self.assertIn(b"Content-Type: image/jpeg", missing_chunk)
        self.assertIn(b"\xff\xd8", missing_chunk)

        jpeg_bytes = b"\xff\xd8valid-yolo-frame\xff\xd9"
        jpeg_path.write_bytes(jpeg_bytes)
        fresh = FileBackedMjpegGrabber(jpeg_path, max_age_sec=5.0)
        fresh_chunk = next(fresh.mjpeg_iterator(target_fps=100.0))
        fresh.stop()
        self.assertTrue(fresh.connected)
        self.assertEqual(fresh.latest_jpeg(), jpeg_bytes)
        self.assertIn(jpeg_bytes, fresh_chunk)

        stale_mtime = time.time() - 30.0
        os.utime(jpeg_path, (stale_mtime, stale_mtime))
        stale = FileBackedMjpegGrabber(jpeg_path, max_age_sec=5.0)
        stale_chunk = next(stale.mjpeg_iterator(target_fps=100.0))
        stale.stop()
        self.assertFalse(stale.connected)
        self.assertGreater(stale.latest_frame_age or 0.0, 5.0)
        self.assertNotIn(jpeg_bytes, stale_chunk)

    def test_web_app_prefers_annotated_frame_grabber_and_can_fallback_to_rtsp(self) -> None:
        from web_app.rtsp_stream import FileBackedMjpegGrabber, RtspFrameGrabber

        root = fresh_test_dir("annotated_frame_config")
        env = {
            "ZONE5_ANNOTATED_FRAME_PATH": str(root / "latest.jpg"),
            "ZONE5_ANNOTATED_FRAME_MAX_AGE_SEC": "1.25",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            grabber = web_main._build_video_grabber()
        self.assertIsInstance(grabber, FileBackedMjpegGrabber)
        self.assertIn("latest.jpg", grabber.public_url)

        fallback_env = {
            "ZONE5_ANNOTATED_FRAME_PATH": "disabled",
            "ZONE5_RTSP_URL": "rtsp://user:pass@example.test/stream",
        }
        with mock.patch.dict(os.environ, fallback_env, clear=False):
            fallback = web_main._build_video_grabber()
        self.assertIsInstance(fallback, RtspFrameGrabber)
        self.assertEqual(fallback.public_url, "rtsp://***@example.test/stream")

    def test_web_app_mjpeg_target_fps_is_configurable(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(web_main._build_mjpeg_target_fps(), 15.0)
        with mock.patch.dict(os.environ, {"ZONE5_MJPEG_TARGET_FPS": "22.5"}, clear=True):
            self.assertEqual(web_main._build_mjpeg_target_fps(), 22.5)
        with mock.patch.dict(os.environ, {"ZONE5_MJPEG_TARGET_FPS": "0"}, clear=True):
            self.assertEqual(web_main._build_mjpeg_target_fps(), 1.0)

    def test_web_app_serves_package_mask_by_default_and_can_disable_it(self) -> None:
        package_root = Path(__file__).resolve().parents[1]
        default_mask = package_root / "cv_counter" / "masks" / "cam1-desk5-mask.png"

        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(web_main._build_mask_path(), default_mask)
            self.assertEqual(
                web_main._mask_status(default_mask),
                {"available": True, "path": str(default_mask), "url": "/api/mask.png"},
            )

        with mock.patch.dict(os.environ, {"ZONE5_MASK_PATH": "disabled"}, clear=True):
            self.assertIsNone(web_main._build_mask_path())
            self.assertEqual(
                web_main._mask_status(None),
                {"available": False, "path": None, "url": None},
            )

        with mock.patch.dict(os.environ, {"ZONE5_MASK_PATH": "cv_counter/masks/cam1-desk5-mask.png"}, clear=True):
            self.assertEqual(web_main._build_mask_path(), default_mask)

    def test_zone_5_sensor_mapping_and_source_selection(self) -> None:
        self.assertEqual(training.ZONE_NUM, 5)
        self.assertEqual(air1_exporter.SENSOR_ORDER[4], "87f510")
        self.assertEqual(air1_exporter.SMART_PLUG_DEVICE_TO_ZONE["9d88e7"], 5)
        self.assertEqual(air1_exporter.MMWAVE_DEVICE_TO_ZONE["89f464"], 5)

        devices = ["88e4c8", "87f510", "89e8d8"]
        source_devices = air1_sources.zone_5_source_devices(devices)

        self.assertEqual(source_devices["air1"], ["87f510"])
        self.assertEqual(source_devices["smart_plug"], ["9d88e7"])
        self.assertEqual(source_devices["mmwave"], ["89f464"])
        self.assertNotIn("2b7624", source_devices["mmwave"])

    def test_mmwave_parser_or_combines_internal_radar_regions(self) -> None:
        self.assertEqual(
            air1_exporter.mmwave_entity_to_device_id("apollo_msr_2_89f464_radar_zone_1_occupancy"),
            "89f464",
        )
        self.assertEqual(
            air1_exporter.mmwave_entity_to_field("apollo_msr_2_89f464_radar_zone_2_occupancy"),
            "radar_zone_2_occupancy",
        )
        occupied = air1_exporter.extract_mmwave_state(
            {
                "zone_1_occupancy": "false",
                "zone_2_occupancy": "true",
                "zone_3_occupancy": "false",
                "state": "false",
            },
            "89f464",
        )
        unoccupied = air1_exporter.extract_mmwave_state(
            {
                "zone_1_occupancy": "false",
                "zone_2_occupancy": "false",
                "zone_3_occupancy": "false",
                "state": "true",
            },
            "89f464",
        )
        radar_target_occupied = air1_exporter.extract_mmwave_state(
            {
                "zone_1_occupancy": "false",
                "zone_2_occupancy": "false",
                "zone_3_occupancy": "false",
                "radar_target": "true",
            },
            "89f464",
        )
        still_target_occupied = air1_exporter.extract_mmwave_state(
            {
                "zone_1_occupancy": "false",
                "zone_2_occupancy": "false",
                "zone_3_occupancy": "false",
                "still_target": "true",
            },
            "89f464",
        )

        self.assertEqual(occupied, 1)
        self.assertEqual(unoccupied, 0)
        self.assertEqual(radar_target_occupied, 1)
        self.assertEqual(still_target_occupied, 1)

    def test_zone_5_artifact_loader_rejects_old_zone_file_names(self) -> None:
        root = fresh_test_dir("old_artifact_rejected")
        (root / "models").mkdir(parents=True)
        old_model_name = "best_cnn_zone_" + "1.pt"
        torch.save({"params": {}}, root / "models" / old_model_name)

        with self.assertRaises(FileNotFoundError):
            training._load_artifacts(root)

    def test_cv_ground_truth_tailer_uses_table_wording_and_reads_csv_and_parquet(self) -> None:
        root = fresh_test_dir("cv_ground_truth_tailer")
        rows = pd.DataFrame(
            {
                "timestamp": ["2026-05-06 10:00:11", "2026-05-06 10:00:21"],
                "occupancy_count": [1, 0],
                "cv_is_occupied": [1, 0],
            }
        )
        csv_path = root / "cv_occupancy_zone5_10sec.csv"
        parquet_path = root / "cv_occupancy_zone5_10sec.parquet"
        rows.to_csv(csv_path, index=False)
        rows.to_parquet(parquet_path, index=False)

        csv_tailer = CvGroundTruthTailer(csv_path)
        parquet_tailer = CvGroundTruthTailer(parquet_path)
        self.assertEqual(csv_tailer.latest("2026-05-06 10:00:30").count, 0.0)
        self.assertEqual(parquet_tailer.latest("2026-05-06 10:00:30").count, 0.0)
        self.assertEqual(csv_tailer.parquet_path, csv_path)
        self.assertEqual(csv_tailer.table_path, csv_path)

        missing = CvGroundTruthTailer(root / "missing.csv")
        status = missing.status()
        self.assertIn("CV ground-truth table not found", status["last_error"] or "")
        self.assertNotIn("parquet", status["last_error"] or "")

    def test_smoke_test_reports_missing_trained_model_clearly(self) -> None:
        root = fresh_test_dir("smoke_missing_model")
        candidate = root / "model"
        candidate.mkdir()
        output = io.StringIO()
        argv = ["smoke_test.py", "--candidate-run", str(candidate), "--skip-non-regression"]
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(output):
            rc = smoke_test.main()
        self.assertEqual(rc, 1)
        message = output.getvalue()
        self.assertIn("No trained model found", message)
        self.assertIn("Train first", message)
        self.assertIn("production_run.txt", message)

    def test_smoke_test_uses_csv_source_metadata_without_reading_parquet(self) -> None:
        root = fresh_test_dir("smoke_csv_source")
        run_dir = root / "model" / "runs" / "csv_run"
        (run_dir / "tables").mkdir(parents=True)
        csv_path = root / "zone5_training_cv.csv"
        rows = []
        for idx in range(12):
            row = {
                "timestamp": pd.Timestamp("2026-05-06 10:00:00") + pd.Timedelta(seconds=10 * idx),
                training.TARGET_COLUMN: float(idx % 2),
            }
            for col in training.RAW_FEATURE_COLUMNS:
                row[col] = float(idx)
            rows.append(row)
        pd.DataFrame(rows).to_csv(csv_path, index=False)
        (run_dir / "tables" / "scaler_stats_zone_5.json").write_text(
            json.dumps(
                {
                    "feature_columns": training.FEATURE_COLUMNS,
                    "source_data_path": str(csv_path),
                    "source_csv_path": str(csv_path),
                }
            ),
            encoding="utf-8",
        )

        with mock.patch.object(smoke_test.pd, "read_parquet", side_effect=AssertionError("unexpected parquet read")):
            frame = smoke_test._build_smoke_frame(run_dir, lookback=3, safety_rows=2)

        self.assertEqual(len(frame), 5)
        self.assertEqual(pd.Timestamp(frame[training.TIMESTAMP_COLUMN].iloc[-1]), pd.Timestamp("2026-05-06 10:01:50"))

    def test_smoke_test_uses_same_window_non_regression_metric(self) -> None:
        root = fresh_test_dir("smoke_same_window_regression")
        candidate = root / "candidate"
        production = root / "production"
        candidate.mkdir()
        production.mkdir()
        production_pointer = root / "production_run.txt"
        production_pointer.write_text(str(production) + "\n", encoding="utf-8")
        candidate_metrics = {
            "metrics_by_split": {
                "test": {
                    "pr_auc": 0.80,
                    "roc_auc": 0.80,
                    "brier_score": 0.20,
                    "bce_log_loss": 0.40,
                    "positive_rate": 0.50,
                    "positive_windows": 10,
                    "positive_buckets": 10,
                    "positive_events": 1,
                    "n_windows": 20,
                }
            }
        }
        scaler = {
            "model_contract_version": training.MODEL_CONTRACT_VERSION,
            "feature_columns": training.FEATURE_COLUMNS,
            "raw_feature_columns": training.RAW_FEATURE_COLUMNS,
            "missing_indicator_columns": training.MISSING_INDICATOR_COLUMNS,
            "target_column": training.TARGET_COLUMN,
            "sample_interval_seconds": training.SAMPLE_INTERVAL_SECONDS,
            "lookback": 90,
        }
        params = {
            "params": {"lookback": 90},
            "sample_interval_seconds": training.SAMPLE_INTERVAL_SECONDS,
            "model_contract_version": training.MODEL_CONTRACT_VERSION,
        }
        checkpoint = {
            "target_column": training.TARGET_COLUMN,
            "sample_interval_seconds": training.SAMPLE_INTERVAL_SECONDS,
            "model_contract_version": training.MODEL_CONTRACT_VERSION,
        }
        fixture = pd.DataFrame({"timestamp": [pd.Timestamp("2026-05-06 10:00:00")]})
        argv = [
            "smoke_test.py",
            "--candidate-run",
            str(candidate),
            "--production-pointer",
            str(production_pointer),
        ]

        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(smoke_test, "_resolve_run_dir", side_effect=lambda path: Path(path)),
            mock.patch.object(smoke_test, "_validate_manifest", return_value={}),
            mock.patch.object(smoke_test, "_load_metrics", return_value=candidate_metrics) as load_metrics,
            mock.patch.object(smoke_test.training, "_load_artifacts", return_value=(scaler, params, checkpoint)),
            mock.patch.object(smoke_test, "_build_smoke_frame", return_value=fixture),
            mock.patch.object(smoke_test.training, "predict_zone_5_probability", return_value=0.5),
            mock.patch.object(
                promote_model,
                "_load_run_payload",
                side_effect=lambda path: {"path": str(path), "is_candidate": Path(path) == candidate},
            ),
            mock.patch.object(
                promote_model,
                "_production_metrics_on_candidate_blind_test",
                return_value={"pr_auc": 0.70},
            ) as same_window,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(smoke_test.main(), 0)

        load_metrics.assert_called_once_with(candidate)
        same_window.assert_called_once()

    def test_docker_compose_has_no_top_level_parquet_cache_services(self) -> None:
        package_root = Path(__file__).resolve().parents[1]
        compose = (package_root / "docker-compose.yml").read_text(encoding="utf-8")

        self.assertNotIn("parquet-exporter", compose)
        self.assertNotIn("cv-training-builder", compose)
        self.assertNotIn("--output-parquet", compose)
        self.assertIn("SNAPSHOT_REFRESH_EVERY_HOURS", compose)
        self.assertIn("RETRAIN_AFTER_SNAPSHOT", compose)

    def test_systemd_units_are_zone5_csv_first(self) -> None:
        package_root = Path(__file__).resolve().parents[1]
        unit_dir = package_root / "systemd" / "user"
        unit_names = {path.name for path in unit_dir.glob("*")}
        expected = {
            "zone5-live-app.service",
            "zone5-live-collector.service",
            "zone5-mqtt-aggregator.service",
            "zone5-person-counter.service",
            "zone5-sen55-collector.service",
            "zone5-trainer.service",
            "zone5-trainer.timer",
        }
        self.assertTrue(expected.issubset(unit_names))

        combined = "\n".join(path.read_text(encoding="utf-8") for path in unit_dir.glob("*"))
        self.assertNotIn("Zone 1", combined)
        self.assertNotIn("zone1", combined.lower())
        self.assertNotIn("--output-parquet", combined)
        self.assertNotIn("OUTPUT_PARQUET", combined)
        self.assertIn("SNAPSHOT_REFRESH_EVERY_HOURS=1", combined)
        self.assertIn("RETRAIN_AFTER_SNAPSHOT=0", combined)

    def test_person_counter_launchers_use_package_local_defaults(self) -> None:
        package_root = Path(__file__).resolve().parents[1]
        ps1 = (package_root / "run_person_counter.ps1").read_text(encoding="utf-8")
        sh = (package_root / "run_person_counter.sh").read_text(encoding="utf-8")

        for relative_path in [
            "cv_counter/rtsp_person_mask_tracker_new.py",
            "cv_counter/masks/cam1-desk5-mask.png",
            "cv_counter/models/headtracker-m.pt",
            "cv_counter/trackers/botsort.yaml",
            "cv_counter/trackers/bytetrack.yaml",
        ]:
            self.assertTrue((package_root / relative_path).is_file(), relative_path)

        self.assertIn(r"cv_counter\rtsp_person_mask_tracker_new.py", ps1)
        self.assertIn(r"cv_counter\masks\cam1-desk5-mask.png", ps1)
        self.assertIn(r"cv_counter\models\headtracker-m.pt", ps1)
        self.assertIn(r"cv_counter\trackers\bytetrack.yaml", ps1)
        self.assertIn("care_ssl/zone5/person_count", ps1)
        self.assertIn("Resolve-RequiredFile", ps1)
        self.assertIn("PERSON_COUNT_SHOW_MASK", ps1)
        self.assertIn("PERSON_COUNT_TRACKING", ps1)
        self.assertIn("--show-mask", ps1)
        self.assertIn("--no-tracker", ps1)
        self.assertIn("--imgsz", ps1)
        self.assertNotIn(r"..\rtsp_person_mask_tracker_new.py", ps1)
        self.assertNotIn(r"..\headtracker-m.pt", ps1)

        self.assertIn("cv_counter/rtsp_person_mask_tracker_new.py", sh)
        self.assertIn("cv_counter/masks/cam1-desk5-mask.png", sh)
        self.assertIn("cv_counter/models/headtracker-m.pt", sh)
        self.assertIn("cv_counter/trackers/bytetrack.yaml", sh)
        self.assertIn("care_ssl/zone5/person_count", sh)
        self.assertIn("require_file", sh)
        self.assertIn("--latest-jpeg", sh)
        self.assertIn("PERSON_COUNT_SHOW_MASK", sh)
        self.assertIn("PERSON_COUNT_TRACKING", sh)
        self.assertIn("--show-mask", sh)
        self.assertIn("--no-tracker", sh)
        self.assertIn("PERSON_COUNT_IMGSZ", sh)
        self.assertIn("ZONE5_ANNOTATED_FRAME_PATH", sh)
        self.assertIn("data/yolo_latest.jpg", sh)
        self.assertNotIn("../rtsp_person_mask_tracker_new.py", sh)
        self.assertNotIn("../headtracker-m.pt", sh)
        self.assertEqual(OCCUPANCY_DEFAULT_TOPIC, "care_ssl/zone5/person_count")

    def test_person_counter_mqtt_connection_failure_is_nonfatal(self) -> None:
        package_root = Path(__file__).resolve().parents[1]
        module_path = package_root / "cv_counter" / "rtsp_person_mask_tracker_new.py"
        spec = importlib.util.spec_from_file_location("rtsp_person_mask_tracker_new_test", module_path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        person_counter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(person_counter)

        class RefusingClient:
            def username_pw_set(self, *_args) -> None:
                pass

            def reconnect_delay_set(self, *, min_delay: int, max_delay: int) -> None:
                self.reconnect_delays = (min_delay, max_delay)

            def connect(self, *_args, **_kwargs) -> None:
                raise OSError("connection refused")

        args = argparse.Namespace(
            mqtt_broker="10.158.71.19",
            mqtt_port=1883,
            mqtt_client_id="care_ssl_person_counter",
            mqtt_username="guest",
            mqtt_password="smartilab123",
            mqtt_topic="care_ssl/zone5/person_count",
        )

        with mock.patch.object(person_counter, "create_mqtt_client", return_value=RefusingClient()):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                client = person_counter.connect_mqtt(args)

        self.assertIsNone(client)
        self.assertIn("continuing without MQTT publishing", output.getvalue())

    def test_person_counter_mask_visualization_keeps_background_live(self) -> None:
        package_root = Path(__file__).resolve().parents[1]
        module_path = package_root / "cv_counter" / "rtsp_person_mask_tracker_new.py"
        spec = importlib.util.spec_from_file_location("rtsp_person_mask_tracker_new_test", module_path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        person_counter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(person_counter)

        mask = np.zeros((3, 3), dtype=np.uint8)
        mask[1, 1] = 255
        first_frame = np.full((3, 3, 3), 40, dtype=np.uint8)
        second_frame = np.full((3, 3, 3), 200, dtype=np.uint8)

        first_overlay = person_counter.make_dim_overlay(first_frame, mask, alpha=0.5)
        second_overlay = person_counter.make_dim_overlay(second_frame, mask, alpha=0.5)

        self.assertEqual(int(first_overlay[0, 0, 0]), 20)
        self.assertEqual(int(second_overlay[0, 0, 0]), 100)
        self.assertTrue(np.array_equal(second_overlay[1, 1], second_frame[1, 1]))

    def test_static_dashboard_labels_are_zone5(self) -> None:
        package_root = Path(__file__).resolve().parents[1]
        html = (package_root / "web_app" / "static" / "index.html").read_text(encoding="utf-8")

        self.assertIn("ZONE 05 // OCCUPANCY MONITOR", html)
        self.assertIn("ZONE 05", html)
        self.assertIn("Z-005", html)
        self.assertIn("CAM Z-005", html)
        self.assertIn("YOLO MJPEG", html)
        self.assertIn("YOLO MJPEG + MASK", html)
        self.assertIn("EMA-5", html)
        self.assertIn("Live YOLO inference video", html)
        self.assertIn('id="mask-overlay"', html)
        self.assertIn("Zone 5 ROI mask overlay", html)
        self.assertNotIn("RTSP/H.264", html)
        old_zone = "ZONE 0" + "1"
        old_short_id = "Z-00" + "1"
        self.assertNotIn(old_zone, html)
        self.assertNotIn(old_short_id, html)
        self.assertNotIn("CAM " + old_short_id, html)

    def test_static_dashboard_smooths_displayed_probability_with_ema5(self) -> None:
        package_root = Path(__file__).resolve().parents[1]
        app_js = (package_root / "web_app" / "static" / "app.js").read_text(encoding="utf-8")

        self.assertIn("const SMOOTHING_WINDOW_TICKS = 5", app_js)
        self.assertIn("const SMOOTHING_ALPHA = 2 / (SMOOTHING_WINDOW_TICKS + 1)", app_js)
        self.assertIn("function smoothProbability(probability)", app_js)
        self.assertIn("function smoothProbabilitySeries(probabilities)", app_js)
        self.assertIn("let latestPlottedTimestampMs = null", app_js)
        self.assertIn("function eventTimestampMs(event)", app_js)
        self.assertIn("timestampMs <= latestPlottedTimestampMs", app_js)
        self.assertIn("hovertemplate: \"%{x}<br><b>p EMA-5 = %{y:.4f}</b><extra></extra>\"", app_js)
        self.assertIn("const ys = smoothProbabilitySeries(valid.map(ev => ev.probability))", app_js)
        self.assertIn("const displayedProbability = smoothProbability(event.probability)", app_js)
        self.assertIn("setOccupiedFlag(visualOccupied(displayedProbability), displayedProbability)", app_js)
        self.assertIn("p=${displayedProbability.toFixed(4)}", app_js)


class PromotionContractTests(unittest.TestCase):
    def _write_candidate(
        self,
        root: Path,
        run_id: str,
        target_column: str,
        validation_mode: str = "rolling_calendar",
        allow_degenerate_validation: bool = False,
        cv_folds_used: int = 3,
        pr_auc: float = 0.8,
    ) -> Path:
        if validation_mode == training.BOOTSTRAP_VALIDATION_MODE and cv_folds_used == 3:
            cv_folds_used = 1
        run_dir = root / "runs" / run_id
        models_dir = run_dir / "models"
        tables_dir = run_dir / "tables"
        models_dir.mkdir(parents=True, exist_ok=True)
        tables_dir.mkdir(parents=True, exist_ok=True)
        split_policy = {
            "validation_mode": validation_mode,
            "cv_folds": cv_folds_used,
            "cv_folds_requested": cv_folds_used,
            "cv_folds_used": cv_folds_used,
        }
        metrics = {
            "model_contract_version": training.MODEL_CONTRACT_VERSION,
            "metrics_by_split": {
                "test": {
                    "pr_auc": pr_auc,
                    "roc_auc": 0.8,
                    "brier_score": 0.2,
                    "bce_log_loss": 0.4,
                    "positive_rate": 0.5,
                    "positive_windows": 10,
                    "positive_buckets": 10,
                    "positive_events": 1,
                    "n_windows": 20,
                },
                "blind_test": {
                    "pr_auc": pr_auc,
                    "roc_auc": 0.8,
                    "brier_score": 0.2,
                    "bce_log_loss": 0.4,
                    "positive_rate": 0.5,
                    "positive_windows": 10,
                    "positive_buckets": 10,
                    "positive_events": 1,
                    "n_windows": 20,
                },
            },
            "cv_metrics": {"best_mean_pr_auc": 0.75},
            "split_policy": split_policy,
            "validation_mode": validation_mode,
            "allow_degenerate_validation": allow_degenerate_validation,
            "cv_folds_requested": cv_folds_used,
            "cv_folds_used": cv_folds_used,
        }
        scaler = {
            "model_contract_version": training.MODEL_CONTRACT_VERSION,
            "target_column": target_column,
            "feature_columns": training.FEATURE_COLUMNS,
            "raw_feature_columns": training.RAW_FEATURE_COLUMNS,
            "missing_indicator_columns": training.MISSING_INDICATOR_COLUMNS,
            "sample_interval_seconds": training.SAMPLE_INTERVAL_SECONDS,
            "lookback_minutes": 15,
            "lookback_rows": 90,
            "feature_fill_values": {col: 0.0 for col in training.RAW_FEATURE_COLUMNS},
            "means": {col: 0.0 for col in training.FEATURE_COLUMNS},
            "stds": {col: 1.0 for col in training.FEATURE_COLUMNS},
            "lookback": 90,
        }
        best_params = {
            "model_contract_version": training.MODEL_CONTRACT_VERSION,
            "params": {"lookback": 90},
            "best_mean_cv_pr_auc": 0.75,
            "sample_interval_seconds": training.SAMPLE_INTERVAL_SECONDS,
            "lookback_minutes": 15,
            "lookback_rows": 90,
            "allow_degenerate_validation": allow_degenerate_validation,
            "cv_folds_requested": cv_folds_used,
            "cv_folds_used": cv_folds_used,
        }
        manifest = {
            "run_id": run_id,
            "model_contract_version": training.MODEL_CONTRACT_VERSION,
            "target_column": target_column,
            "feature_columns": training.FEATURE_COLUMNS,
            "raw_feature_columns": training.RAW_FEATURE_COLUMNS,
            "missing_indicator_columns": training.MISSING_INDICATOR_COLUMNS,
            "sample_interval_seconds": training.SAMPLE_INTERVAL_SECONDS,
            "lookback_minutes": 15,
            "lookback_rows": 90,
            "split_policy": split_policy,
            "validation_mode": validation_mode,
            "allow_degenerate_validation": allow_degenerate_validation,
            "cv_folds_requested": cv_folds_used,
            "cv_folds_used": cv_folds_used,
            "files": [],
        }
        (tables_dir / "metrics_zone_5.json").write_text(json.dumps(metrics), encoding="utf-8")
        (tables_dir / "scaler_stats_zone_5.json").write_text(json.dumps(scaler), encoding="utf-8")
        (tables_dir / "best_params_zone_5.json").write_text(json.dumps(best_params), encoding="utf-8")
        (run_dir / training.RUN_MANIFEST_FILENAME).write_text(json.dumps(manifest), encoding="utf-8")
        torch.save(
            {
                "model_contract_version": training.MODEL_CONTRACT_VERSION,
                "target_column": target_column,
                "params": best_params["params"],
                "sample_interval_seconds": training.SAMPLE_INTERVAL_SECONDS,
                "lookback_minutes": 15,
                "lookback_rows": 90,
            },
            models_dir / "best_cnn_zone_5.pt",
        )
        (root / training.CURRENT_RUN_POINTER).write_text(run_id + "\n", encoding="utf-8")
        return run_dir

    def test_promotion_rejects_non_cv_target_and_accepts_valid_candidate(self) -> None:
        root = fresh_test_dir("promotion")
        bad_root = root / "bad_model"
        good_root = root / "good_model"
        bad_run = self._write_candidate(bad_root, "bad", "mmwave_s5")
        good_run = self._write_candidate(good_root, "good", training.TARGET_COLUMN)

        with self.assertRaises(ValueError):
            promote_model._load_run_payload(bad_run)

        production_pointer = root / "production_run.txt"
        argv = [
            "zone5.promote_model",
            "--candidate-run",
            str(good_root),
            "--production-pointer",
            str(production_pointer),
            "--skip-smoke",
            "--min-positive-windows",
            "1",
        ]
        with mock.patch.object(sys, "argv", argv):
            self.assertEqual(promote_model.main(), 0)
        self.assertTrue(production_pointer.is_file())
        pointer_text = production_pointer.read_text(encoding="utf-8").strip()
        pointer_path = Path(pointer_text)
        if not pointer_path.is_absolute():
            pointer_path = (Path(__file__).resolve().parents[1] / pointer_path).resolve()
        self.assertEqual(pointer_path, good_run.resolve())

    def test_bootstrap_candidate_can_only_promote_as_first_model(self) -> None:
        root = fresh_test_dir("promotion_bootstrap")
        first_root = root / "first_model"
        second_root = root / "second_model"
        first_run = self._write_candidate(
            first_root,
            "first",
            training.TARGET_COLUMN,
            validation_mode=training.BOOTSTRAP_VALIDATION_MODE,
        )
        second_run = self._write_candidate(
            second_root,
            "second",
            training.TARGET_COLUMN,
            validation_mode=training.BOOTSTRAP_VALIDATION_MODE,
        )
        production_pointer = root / "production_run.txt"

        argv = [
            "zone5.promote_model",
            "--candidate-run",
            str(first_root),
            "--production-pointer",
            str(production_pointer),
            "--skip-smoke",
            "--min-positive-windows",
            "1",
        ]
        with mock.patch.object(sys, "argv", argv):
            self.assertEqual(promote_model.main(), 0)
        pointer_text = production_pointer.read_text(encoding="utf-8").strip()
        pointer_path = Path(pointer_text)
        if not pointer_path.is_absolute():
            pointer_path = (Path(__file__).resolve().parents[1] / pointer_path).resolve()
        self.assertEqual(pointer_path, first_run.resolve())

        argv[2] = str(second_root)
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(promote_model.main(), 1)
        self.assertIn("bootstrap fallback candidates can only be promoted as the first production model", output.getvalue())
        pointer_text = production_pointer.read_text(encoding="utf-8").strip()
        pointer_path = Path(pointer_text)
        if not pointer_path.is_absolute():
            pointer_path = (Path(__file__).resolve().parents[1] / pointer_path).resolve()
        self.assertEqual(pointer_path, first_run.resolve())
        self.assertNotEqual(first_run.resolve(), second_run.resolve())

    def test_promotion_accepts_improved_same_level_strict_candidate_after_one_fold_production(self) -> None:
        root = fresh_test_dir("promotion_progressive_accept_same_level_one_fold")
        production_root = root / "production_model"
        candidate_root = root / "candidate_model"
        production_run = self._write_candidate(
            production_root,
            "production",
            training.TARGET_COLUMN,
            cv_folds_used=1,
            pr_auc=0.7,
        )
        candidate_run = self._write_candidate(
            candidate_root,
            "candidate",
            training.TARGET_COLUMN,
            cv_folds_used=1,
            pr_auc=0.9,
        )
        production_pointer = root / "production_run.txt"
        production_pointer.write_text(str(production_run) + "\n", encoding="utf-8")

        argv = [
            "zone5.promote_model",
            "--candidate-run",
            str(candidate_root),
            "--production-pointer",
            str(production_pointer),
            "--skip-smoke",
            "--min-positive-windows",
            "1",
        ]
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(
                promote_model,
                "_production_metrics_on_candidate_blind_test",
                return_value={"pr_auc": 0.70},
            ) as same_window,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(promote_model.main(), 0)
        same_window.assert_not_called()
        self.assertIn("same-level strict 1-fold replacement allowed", output.getvalue())
        pointer_text = production_pointer.read_text(encoding="utf-8").strip()
        pointer_path = Path(pointer_text)
        if not pointer_path.is_absolute():
            pointer_path = (Path(__file__).resolve().parents[1] / pointer_path).resolve()
        self.assertEqual(pointer_path, candidate_run.resolve())

    def test_promotion_rejects_same_level_one_fold_candidate_without_reported_pr_auc_improvement(self) -> None:
        root = fresh_test_dir("promotion_progressive_reject_same_level_one_fold")
        production_root = root / "production_model"
        candidate_root = root / "candidate_model"
        production_run = self._write_candidate(
            production_root,
            "production",
            training.TARGET_COLUMN,
            cv_folds_used=1,
            pr_auc=0.9,
        )
        self._write_candidate(
            candidate_root,
            "candidate",
            training.TARGET_COLUMN,
            cv_folds_used=1,
            pr_auc=0.9,
        )
        production_pointer = root / "production_run.txt"
        production_pointer.write_text(str(production_run) + "\n", encoding="utf-8")

        argv = [
            "zone5.promote_model",
            "--candidate-run",
            str(candidate_root),
            "--production-pointer",
            str(production_pointer),
            "--skip-smoke",
            "--min-positive-windows",
            "1",
        ]
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(promote_model, "_production_metrics_on_candidate_blind_test") as same_window,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(promote_model.main(), 1)
        same_window.assert_not_called()
        self.assertIn("same-level strict 1-fold replacement requires candidate reported blind-test PR-AUC", output.getvalue())
        self.assertEqual(production_pointer.read_text(encoding="utf-8").strip(), str(production_run))

    def test_promotion_accepts_stricter_candidate_after_one_fold_production(self) -> None:
        for candidate_folds in [2, 3]:
            root = fresh_test_dir(f"promotion_progressive_accept_{candidate_folds}")
            production_root = root / "production_model"
            candidate_root = root / "candidate_model"
            production_run = self._write_candidate(
                production_root,
                "production",
                training.TARGET_COLUMN,
                cv_folds_used=1,
                pr_auc=0.7,
            )
            candidate_run = self._write_candidate(
                candidate_root,
                "candidate",
                training.TARGET_COLUMN,
                cv_folds_used=candidate_folds,
                pr_auc=0.9,
            )
            production_pointer = root / "production_run.txt"
            production_pointer.write_text(str(production_run) + "\n", encoding="utf-8")

            argv = [
                "zone5.promote_model",
                "--candidate-run",
                str(candidate_root),
                "--production-pointer",
                str(production_pointer),
                "--skip-smoke",
                "--min-positive-windows",
                "1",
            ]
            with (
                self.subTest(candidate_folds=candidate_folds),
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(
                    promote_model,
                    "_production_metrics_on_candidate_blind_test",
                    return_value={"pr_auc": 0.70},
                ),
            ):
                self.assertEqual(promote_model.main(), 0)
            pointer_text = production_pointer.read_text(encoding="utf-8").strip()
            pointer_path = Path(pointer_text)
            if not pointer_path.is_absolute():
                pointer_path = (Path(__file__).resolve().parents[1] / pointer_path).resolve()
            self.assertEqual(pointer_path, candidate_run.resolve())

    def test_promotion_uses_same_window_production_metric_when_available(self) -> None:
        root = fresh_test_dir("promotion_same_window_compare")
        production_root = root / "production_model"
        candidate_root = root / "candidate_model"
        production_run = self._write_candidate(
            production_root,
            "production",
            training.TARGET_COLUMN,
            cv_folds_used=1,
            pr_auc=0.99,
        )
        candidate_run = self._write_candidate(
            candidate_root,
            "candidate",
            training.TARGET_COLUMN,
            cv_folds_used=2,
            pr_auc=0.80,
        )
        production_pointer = root / "production_run.txt"
        production_pointer.write_text(str(production_run) + "\n", encoding="utf-8")
        argv = [
            "zone5.promote_model",
            "--candidate-run",
            str(candidate_root),
            "--production-pointer",
            str(production_pointer),
            "--skip-smoke",
            "--min-positive-windows",
            "1",
        ]

        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(
                promote_model,
                "_production_metrics_on_candidate_blind_test",
                return_value={"pr_auc": 0.70},
            ) as same_window,
        ):
            self.assertEqual(promote_model.main(), 0)

        same_window.assert_called_once()
        pointer_text = production_pointer.read_text(encoding="utf-8").strip()
        pointer_path = Path(pointer_text)
        if not pointer_path.is_absolute():
            pointer_path = (Path(__file__).resolve().parents[1] / pointer_path).resolve()
        self.assertEqual(pointer_path, candidate_run.resolve())

    def test_promotion_rejects_degenerate_validation_candidate(self) -> None:
        root = fresh_test_dir("promotion_degenerate")
        candidate_root = root / "candidate_model"
        self._write_candidate(
            candidate_root,
            "candidate",
            training.TARGET_COLUMN,
            allow_degenerate_validation=True,
        )
        production_pointer = root / "production_run.txt"
        argv = [
            "zone5.promote_model",
            "--candidate-run",
            str(candidate_root),
            "--production-pointer",
            str(production_pointer),
            "--skip-smoke",
            "--min-positive-windows",
            "1",
        ]
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(promote_model.main(), 1)
        self.assertIn("degenerate validation candidates are smoke/dev-only", output.getvalue())
        self.assertFalse(production_pointer.exists())

    def test_web_model_loader_rejects_old_artifact_contract(self) -> None:
        root = fresh_test_dir("web_contract")
        run_dir = root / "runs" / "candidate"
        (run_dir / "models").mkdir(parents=True)
        (run_dir / "tables").mkdir(parents=True)
        scaler = {
            "target_column": training.TARGET_COLUMN,
            "feature_columns": training.FEATURE_COLUMNS,
            "raw_feature_columns": training.RAW_FEATURE_COLUMNS,
            "missing_indicator_columns": training.MISSING_INDICATOR_COLUMNS,
            "feature_fill_values": {col: 0.0 for col in training.RAW_FEATURE_COLUMNS},
            "means": {col: 0.0 for col in training.FEATURE_COLUMNS},
            "stds": {col: 1.0 for col in training.FEATURE_COLUMNS},
            "lookback": 15,
        }
        best_params = {"params": {"lookback": 15}, "best_mean_cv_pr_auc": 0.75}
        (run_dir / "tables" / "scaler_stats_zone_5.json").write_text(json.dumps(scaler), encoding="utf-8")
        (run_dir / "tables" / "best_params_zone_5.json").write_text(json.dumps(best_params), encoding="utf-8")
        torch.save({"target_column": training.TARGET_COLUMN, "params": best_params["params"]}, run_dir / "models" / "best_cnn_zone_5.pt")

        class DummySource:
            label = "dummy"

            def get_latest_window(self, lookback: int):
                raise AssertionError("not used")

        loop = InferenceLoop(
            InferenceConfig(
                production_pointer=root / "production_run.txt",
                tick_interval_sec=10.0,
                history_size=10,
                max_gap_minutes=5.0,
                max_age_minutes=15.0,
                threshold=None,
            ),
            DummySource(),
        )
        self.assertIsNone(loop._load_artifacts_blocking(run_dir))
        self.assertIn("not a 10-second", loop._last_error or "")

        scaler.update(
            {
                "model_contract_version": training.MODEL_CONTRACT_VERSION,
                "sample_interval_seconds": training.SAMPLE_INTERVAL_SECONDS,
                "lookback_minutes": 15,
                "lookback_rows": 90,
                "lookback": 90,
            }
        )
        best_params.update(
            {
                "model_contract_version": training.MODEL_CONTRACT_VERSION,
                "sample_interval_seconds": training.SAMPLE_INTERVAL_SECONDS,
                "lookback_minutes": 15,
                "lookback_rows": 90,
                "params": {"lookback": 90},
            }
        )
        (run_dir / "tables" / "scaler_stats_zone_5.json").write_text(json.dumps(scaler), encoding="utf-8")
        (run_dir / "tables" / "best_params_zone_5.json").write_text(json.dumps(best_params), encoding="utf-8")
        torch.save(
            {
                "model_contract_version": training.MODEL_CONTRACT_VERSION,
                "target_column": training.TARGET_COLUMN,
                "params": best_params["params"],
                "sample_interval_seconds": training.SAMPLE_INTERVAL_SECONDS,
                "lookback_minutes": 15,
                "lookback_rows": 90,
            },
            run_dir / "models" / "best_cnn_zone_5.pt",
        )
        self.assertIsNotNone(loop._load_artifacts_blocking(run_dir))


if __name__ == "__main__":
    unittest.main()
