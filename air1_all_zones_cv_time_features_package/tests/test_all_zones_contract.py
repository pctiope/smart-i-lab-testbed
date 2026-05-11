from __future__ import annotations

import unittest
import argparse
import atexit
import asyncio
import contextlib
import io
import json
import os
import shutil
import sys
import time
from unittest import mock
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import torch
from fastapi import HTTPException

import rtsp_zone_tracker
import smoke_test
from air1_all_zones import (
    air1_exporter,
    build_cv_training_data,
    collect_training_data,
    dataset,
    feature_builder,
    occupancy_mqtt_aggregator,
    promote_model,
    retrain_once,
    sen55_mqtt_collector,
    training,
)
from air1_all_zones import feature_contract as contract
from web_app.cv_ground_truth import CvGroundTruthTailer
from web_app.inference_loop import InferenceConfig, InferenceLoop, TickEvent
from web_app import main as web_main


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
TEST_TEMP_ROOTS: list[Path] = []
TEST_RUNTIME_ROOT = PACKAGE_ROOT / ".test_runtime"


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


class AllZonesFeatureContractTests(unittest.TestCase):
    def test_model_features_exclude_zone_id_mmwave_and_power(self) -> None:
        feature_columns = list(contract.FEATURE_COLUMNS)
        self.assertNotIn(contract.ZONE_ID_COLUMN, feature_columns)
        for column in feature_columns:
            lowered = column.lower()
            self.assertNotIn("mmwave", lowered)
            self.assertNotIn("power", lowered)
        self.assertEqual(contract.TARGET_COLUMN, "occupied")
        self.assertEqual(len(contract.ALL_ZONE_IDS), 15)

    def test_air1_base_frame_emits_all_15_zones_for_one_timestamp(self) -> None:
        timestamp = datetime(2026, 5, 8, 9, 0, 0)
        air_by_minute = {
            timestamp: {
                "temps": {1: 22.5},
                "rhs": {1: 55.0},
                "co2s": {1: 640.0},
                "pm25s": {1: 8.0},
            }
        }

        frame = feature_builder.build_all_zones_base_feature_frame([timestamp], air_by_minute)

        self.assertEqual(len(frame), 15)
        self.assertEqual(set(frame[contract.ZONE_ID_COLUMN].astype(int)), set(contract.ALL_ZONE_IDS))
        zone_1 = frame.loc[frame[contract.ZONE_ID_COLUMN].astype(int) == 1].iloc[0]
        zone_2 = frame.loc[frame[contract.ZONE_ID_COLUMN].astype(int) == 2].iloc[0]
        self.assertEqual(zone_1["temp"], 22.5)
        self.assertTrue(pd.isna(zone_2["temp"]))

    def test_exporter_source_specs_request_air1_only(self) -> None:
        devices = [contract.SENSOR_ORDER[0], contract.SENSOR_ORDER[4], "unrelated"]
        source_devices = air1_exporter.all_zones_source_devices(devices)
        specs = air1_exporter.build_all_zones_source_specs(source_devices)

        self.assertEqual(source_devices, {"air1": [contract.SENSOR_ORDER[0], contract.SENSOR_ORDER[4]]})
        self.assertEqual([spec["endpoint"] for spec in specs], ["air-1"])
        self.assertEqual([spec["source_key"] for spec in specs], ["air1"])


class AllZonesCvJoinTests(unittest.TestCase):
    def test_per_zone_labels_join_by_timestamp_and_zone_id(self) -> None:
        timestamp = datetime(2026, 5, 8, 9, 0, 0)
        features = feature_builder.build_all_zones_base_feature_frame([timestamp], {timestamp: {}})
        labels = pd.DataFrame(
            {
                "timestamp": [timestamp, timestamp, timestamp],
                contract.ZONE_ID_COLUMN: [1, 2, 16],
                "occupancy_count": [1, 0, 9],
            }
        )

        joined, cleaned_features, cleaned_labels = build_cv_training_data.combine_feature_label_frames(
            features,
            labels,
            occupied_threshold=1.0,
        )

        self.assertEqual(len(cleaned_features), 15)
        self.assertEqual(len(cleaned_labels), 2)
        self.assertEqual(len(joined), 15)
        self.assertEqual(set(joined[contract.ZONE_ID_COLUMN].astype(int)), set(contract.ALL_ZONE_IDS))
        zone_1 = joined.loc[joined[contract.ZONE_ID_COLUMN].astype(int) == 1].iloc[0]
        zone_2 = joined.loc[joined[contract.ZONE_ID_COLUMN].astype(int) == 2].iloc[0]
        zone_3 = joined.loc[joined[contract.ZONE_ID_COLUMN].astype(int) == 3].iloc[0]
        self.assertEqual(zone_1[contract.TARGET_COLUMN], 1)
        self.assertEqual(zone_2[contract.TARGET_COLUMN], 0)
        self.assertTrue(pd.isna(zone_3[contract.TARGET_COLUMN]))
        self.assertNotIn(16, set(cleaned_labels[contract.ZONE_ID_COLUMN].astype(int)))


class AllZonesOccupancyAggregatorTests(unittest.TestCase):
    def test_counts_by_zone_parsing_excludes_table_16_and_keeps_null_unlabeled(self) -> None:
        payload = {
            "counts_by_zone": {"1": 2, "2": 0, "16": 4},
            "unlabeled_zones": [3, 16],
        }

        parsed = occupancy_mqtt_aggregator.parse_counts_by_zone(payload)

        self.assertEqual(parsed[1], 2)
        self.assertEqual(parsed[2], 0)
        self.assertIsNone(parsed[3])
        self.assertNotIn(16, parsed)

    def test_zone_bucket_row_has_per_zone_scope_and_nullable_missing_count(self) -> None:
        args = type("Args", (), {"aggregate": "median", "occupied_threshold": 1.0})()
        aggregator = object.__new__(occupancy_mqtt_aggregator.OccupancyAggregator)
        aggregator.args = args
        bucket = occupancy_mqtt_aggregator.ZoneBucket(
            timestamp=datetime(2026, 5, 8, 9, 0, 0),
            zone_id=4,
            source_topic="care_ssl/all_zones/person_count_by_zone",
        )
        bucket.add(None, datetime(2026, 5, 8, 9, 0, 1), camera_id="cam1", label_source="mask")

        row = aggregator._build_row(bucket)

        self.assertEqual(row[contract.ZONE_ID_COLUMN], 4)
        self.assertEqual(row["occupancy_count"], "")
        self.assertEqual(row[contract.TARGET_COLUMN], "")
        self.assertEqual(row["label_scope"], "per_zone")

    def test_aggregator_writes_unique_timestamp_zone_rows_across_cameras(self) -> None:
        csv_path = PACKAGE_ROOT / "data" / "_unit_test_cv_labels.csv"
        part_path = PACKAGE_ROOT / "data" / "_unit_test_cv_labels_part0002.csv"
        try:
            csv_path.parent.mkdir(parents=True, exist_ok=True)
            csv_path.unlink(missing_ok=True)
            part_path.unlink(missing_ok=True)
            csv_path.write_text(",".join(occupancy_mqtt_aggregator.CSV_HEADERS) + "\n", encoding="utf-8")
            args = type(
                "Args",
                (),
                {
                    "output_csv": csv_path,
                    "output_parquet": "",
                    "use_receive_time": False,
                    "late_grace_seconds": 0.0,
                    "aggregate": "median",
                    "occupied_threshold": 1.0,
                    "parquet_rebuild_every_hours": 1.0,
                },
            )()
            aggregator = occupancy_mqtt_aggregator.OccupancyAggregator(args)
            payloads = [
                {
                    "timestamp": "2026-05-08 09:00:01",
                    "camera_id": "cam1",
                    "counts_by_zone": {"4": 1, "9": 0},
                    "label_scope": "per_zone",
                },
                {
                    "timestamp": "2026-05-08 09:00:02",
                    "camera_id": "cam2",
                    "counts_by_zone": {"1": 0, "2": 1},
                    "label_scope": "per_zone",
                },
                {
                    "timestamp": "2026-05-08 09:00:03",
                    "camera_id": "cam2",
                    "counts_by_zone": {"2": 1},
                    "label_scope": "per_zone",
                },
            ]
            for payload in payloads:
                aggregator.add_payload("care_ssl/all_zones/person_count_by_zone", json.dumps(payload))

            with contextlib.redirect_stdout(io.StringIO()):
                aggregator.flush_due(force=True)
            frame = pd.read_csv(csv_path)
        finally:
            csv_path.unlink(missing_ok=True)
            part_path.unlink(missing_ok=True)

        keys = set(zip(frame["timestamp"], frame[contract.ZONE_ID_COLUMN].astype(int)))
        self.assertEqual(keys, {
            ("2026-05-08 09:00:00", 1),
            ("2026-05-08 09:00:00", 2),
            ("2026-05-08 09:00:00", 4),
            ("2026-05-08 09:00:00", 9),
        })
        zone_2 = frame.loc[frame[contract.ZONE_ID_COLUMN].astype(int) == 2].iloc[0]
        self.assertEqual(zone_2["sample_count"], 2)
        self.assertEqual(zone_2["camera_ids"], "cam2")
        self.assertTrue((frame["label_scope"] == "per_zone").all())


class RtspZoneTrackerTests(unittest.TestCase):
    def test_table_name_mapping_rejects_invalid_labels_and_excludes_table_16(self) -> None:
        self.assertEqual(rtsp_zone_tracker.table_name_to_zone_id("Table 4"), 4)
        self.assertEqual(rtsp_zone_tracker.table_name_to_zone_id("Table 16"), 16)
        with self.assertRaises(ValueError):
            rtsp_zone_tracker.table_name_to_zone_id("Desk 4")
        with self.assertRaises(ValueError):
            rtsp_zone_tracker.table_name_to_zone_id("Table 17")

    def test_largest_overlap_assigns_one_zone(self) -> None:
        zone_1 = rtsp_zone_tracker.Zone("Table 1", 1, (0, 0, 255), None)
        zone_2 = rtsp_zone_tracker.Zone("Table 2", 2, (0, 255, 0), None)
        zone_1.binary_mask = np.zeros((10, 10), dtype=np.uint8)
        zone_2.binary_mask = np.zeros((10, 10), dtype=np.uint8)
        zone_1.binary_mask[:, :4] = 255
        zone_2.binary_mask[:, 4:] = 255

        assigned = rtsp_zone_tracker.assign_zone_for_box([zone_1, zone_2], 2, 0, 10, 10, overlap_thresh=0.1)

        self.assertIsNotNone(assigned)
        self.assertEqual(assigned.zone_id, 2)

    def test_camera_zone_maps_use_expected_masks_and_cover_air1_zones(self) -> None:
        cases = [
            ("cam1", "cam1-zones.json", "masks/cam1-mask-zones.png", {4, 9, 10, 11, 12, 13, 14, 15, 16}),
            ("cam2", "cam2-zones.json", "masks/cam2-mask-zones.png", {1, 2, 3, 5, 6, 7, 8}),
        ]
        model_zone_ids: set[int] = set()
        for camera_id, zones_file, mask_file, expected_zone_ids in cases:
            with self.subTest(camera=camera_id):
                zones, _ = rtsp_zone_tracker.load_zones(str(PACKAGE_ROOT / zones_file))
                self.assertEqual({zone.zone_id for zone in zones}, expected_zone_ids)
                mask = rtsp_zone_tracker.cv2.imread(str(PACKAGE_ROOT / mask_file), rtsp_zone_tracker.cv2.IMREAD_COLOR)
                self.assertIsNotNone(mask)
                self.assertEqual(mask.shape[:2], (1080, 1920))
                rtsp_zone_tracker.build_zone_masks(zones, mask, mask.shape[:2])
                for zone in zones:
                    self.assertGreater(int(np.count_nonzero(zone.binary_mask)), 0, f"{camera_id} {zone.name} has no mask pixels")
                    if zone.zone_id not in rtsp_zone_tracker.EXCLUDED_ZONE_IDS:
                        model_zone_ids.add(zone.zone_id)

        self.assertEqual(model_zone_ids, set(rtsp_zone_tracker.MODEL_ZONE_IDS))


class AllZonesDatasetTests(unittest.TestCase):
    @staticmethod
    def _frame(n_timestamps: int = 10) -> pd.DataFrame:
        rows = []
        base = pd.Timestamp("2026-05-08 09:00:00")
        for tick in range(n_timestamps):
            timestamp = base + pd.Timedelta(seconds=contract.SAMPLE_INTERVAL_SECONDS * tick)
            for zone_id in (1, 2):
                row = {
                    contract.TIMESTAMP_COLUMN: timestamp,
                    contract.ZONE_ID_COLUMN: zone_id,
                    contract.TARGET_COLUMN: tick % 2,
                }
                for feature in contract.FEATURE_COLUMNS:
                    row[feature] = float(zone_id * 1000 + tick)
                rows.append(row)
        return pd.DataFrame(rows)

    def test_windows_stay_within_single_zone(self) -> None:
        frame = self._frame(n_timestamps=8)
        lookback = 3

        X, y, timestamps, zone_ids = dataset.make_windows(frame, lookback=lookback)

        self.assertEqual(len(y), 12)
        self.assertEqual(len(timestamps), 12)
        self.assertEqual(list(zone_ids.astype(int).unique()), [1, 2])
        temp_idx = contract.FEATURE_COLUMNS.index("temp")
        zone_1_windows = X[zone_ids.astype(int).to_numpy() == 1, temp_idx, :]
        zone_2_windows = X[zone_ids.astype(int).to_numpy() == 2, temp_idx, :]
        self.assertTrue(np.all(zone_1_windows < 2000))
        self.assertTrue(np.all(zone_2_windows >= 2000))

    def test_chronological_split_groups_by_timestamp(self) -> None:
        frame = self._frame(n_timestamps=10)

        splits = dataset.chronological_split(frame)

        split_timestamps = {
            name: set(pd.to_datetime(split[contract.TIMESTAMP_COLUMN]))
            for name, split in splits.items()
        }
        self.assertTrue(split_timestamps["train"].isdisjoint(split_timestamps["val"]))
        self.assertTrue(split_timestamps["train"].isdisjoint(split_timestamps["test"]))
        self.assertTrue(split_timestamps["val"].isdisjoint(split_timestamps["test"]))
        for split in splits.values():
            self.assertEqual(split.groupby(contract.TIMESTAMP_COLUMN)[contract.ZONE_ID_COLUMN].nunique().min(), 2)

    def test_training_lookback_candidates_use_per_zone_rows(self) -> None:
        frame = self._frame(n_timestamps=10)
        old_choices = list(training.LOOKBACK_CHOICES)
        training.LOOKBACK_CHOICES[:] = [3, 15]
        try:
            candidates, _rejected = training.viable_cv_lookback_candidates(
                cv_folds=[{"train": frame, "val": frame}],
                final_splits={"pre_test": frame, "test": frame},
                allow_degenerate_validation=True,
            )
        finally:
            training.LOOKBACK_CHOICES[:] = old_choices

        self.assertIn(3, candidates)
        self.assertNotIn(15, candidates)

    @staticmethod
    def _daily_frame(patterns: list[str], ticks_per_day: int = 10) -> pd.DataFrame:
        rows = []
        base = pd.Timestamp("2026-05-01 09:00:00")
        for day_idx, pattern in enumerate(patterns):
            day = base + pd.Timedelta(days=day_idx)
            for tick in range(ticks_per_day):
                timestamp = day + pd.Timedelta(seconds=contract.SAMPLE_INTERVAL_SECONDS * tick)
                for zone_id in (1, 2):
                    if pattern == "zero":
                        target = 0
                    elif pattern == "one":
                        target = 1
                    else:
                        target = (tick + zone_id) % 2
                    row = {
                        contract.TIMESTAMP_COLUMN: timestamp,
                        contract.ZONE_ID_COLUMN: zone_id,
                        contract.TARGET_COLUMN: target,
                    }
                    for feature in contract.FEATURE_COLUMNS:
                        row[feature] = float(day_idx * 1000 + zone_id * 10 + tick)
                    rows.append(row)
        return pd.DataFrame(rows)

    def test_one_strict_fold_can_have_viable_lookbacks_when_two_folds_do_not(self) -> None:
        frame = self._daily_frame(["zero", "mixed", "mixed", "mixed"])
        old_choices = list(training.LOOKBACK_CHOICES)
        training.LOOKBACK_CHOICES[:] = [2]
        try:
            one_fold = training.select_cv_lookback_plan(
                frame,
                cv_folds=1,
                bootstrap_fallback=False,
                min_strict_date_coverage=0.0,
            )
            two_folds = training.select_cv_lookback_plan(
                frame,
                cv_folds=2,
                bootstrap_fallback=False,
                min_strict_date_coverage=0.0,
            )
        finally:
            training.LOOKBACK_CHOICES[:] = old_choices

        self.assertEqual(one_fold["split_policy"]["validation_mode"], training.STRICT_VALIDATION_MODE)
        self.assertEqual(one_fold["split_policy"]["cv_folds_requested"], 1)
        self.assertEqual(one_fold["lookback_candidates"], [2])
        self.assertEqual(two_folds["split_policy"]["cv_folds_requested"], 2)
        self.assertEqual(two_folds["lookback_candidates"], [])
        self.assertIn(2, two_folds["rejected_lookbacks"])

    def test_bootstrap_fallback_uses_chronological_pretest_fold(self) -> None:
        frame = self._daily_frame(["zero", "mixed", "mixed"], ticks_per_day=10)
        old_choices = list(training.LOOKBACK_CHOICES)
        training.LOOKBACK_CHOICES[:] = [2]
        try:
            plan = training.select_cv_lookback_plan(
                frame,
                cv_folds=1,
                bootstrap_fallback=True,
                min_strict_date_coverage=0.0,
            )
        finally:
            training.LOOKBACK_CHOICES[:] = old_choices

        self.assertEqual(plan["split_policy"]["validation_mode"], training.BOOTSTRAP_VALIDATION_MODE)
        self.assertTrue(plan["split_policy"]["bootstrap_fallback_used"])
        self.assertEqual(plan["lookback_candidates"], [2])


class AllZonesLiveAppendAndCollectorTests(unittest.TestCase):
    @staticmethod
    def _row(timestamp: str, zone_id: int, value: float, target: float | None = None) -> dict[str, float | str | None]:
        row: dict[str, float | str | None] = {
            "timestamp": timestamp,
            contract.ZONE_ID_COLUMN: zone_id,
            contract.TARGET_COLUMN: target,
        }
        for col in contract.RAW_FEATURE_COLUMNS:
            row[col] = value
        return row

    def test_live_append_deduplicates_timestamp_zone_keys(self) -> None:
        root = fresh_test_dir("live_append_all_zones")
        output_csv = root / "air1_all_zones_training_cv.csv"
        existing = pd.DataFrame(
            [
                self._row("2026-05-08 10:00:00", 1, 1.0, 0.0),
                self._row("2026-05-08 10:00:00", 2, 2.0, None),
            ]
        )
        existing.to_csv(output_csv, index=False)
        new_rows = pd.DataFrame(
            [
                self._row("2026-05-08 10:00:00", 1, 20.0, 1.0),
                self._row("2026-05-08 10:00:00", 3, 3.0, 0.0),
            ]
        )

        combined, summary = collect_training_data._append_rows_to_csv(output_csv, new_rows)
        reloaded = pd.read_csv(output_csv)

        self.assertEqual(summary["inserted_rows"], 1)
        self.assertEqual(summary["updated_rows"], 1)
        self.assertEqual(len(combined), 3)
        self.assertEqual(len(reloaded), 3)
        keys = list(zip(reloaded["timestamp"], reloaded[contract.ZONE_ID_COLUMN].astype(int)))
        self.assertEqual(
            keys,
            [
                ("2026-05-08 10:00:00", 1),
                ("2026-05-08 10:00:00", 2),
                ("2026-05-08 10:00:00", 3),
            ],
        )
        self.assertEqual(float(reloaded.loc[0, "temp"]), 20.0)
        self.assertEqual(float(reloaded.loc[0, contract.TARGET_COLUMN]), 1.0)

    def test_live_append_preserves_features_when_label_update_is_sparse(self) -> None:
        root = fresh_test_dir("live_append_sparse_all_zones")
        output_csv = root / "air1_all_zones_training_cv.csv"
        existing = pd.DataFrame([self._row("2026-05-08 10:00:00", 1, 24.0, None)])
        existing.to_csv(output_csv, index=False)
        sparse_label_update = pd.DataFrame(
            {
                "timestamp": ["2026-05-08 10:00:00"],
                contract.ZONE_ID_COLUMN: [1],
                contract.TARGET_COLUMN: [1.0],
                "occupancy_count": [1],
            }
        )

        combined, summary = collect_training_data._append_rows_to_csv(output_csv, sparse_label_update)
        reloaded = pd.read_csv(output_csv)

        self.assertEqual(summary["inserted_rows"], 0)
        self.assertEqual(summary["updated_rows"], 1)
        self.assertEqual(len(combined), 1)
        self.assertEqual(float(reloaded.loc[0, "temp"]), 24.0)
        self.assertEqual(float(reloaded.loc[0, contract.TARGET_COLUMN]), 1.0)
        self.assertEqual(float(reloaded.loc[0, "occupancy_count"]), 1.0)

    def test_training_parquet_rebuild_preserves_ten_second_rows_and_zone_ids(self) -> None:
        root = fresh_test_dir("training_parquet_rebuild_all_zones")
        output_csv = root / "air1_all_zones_training_cv.csv"
        output_parquet = root / "air1_all_zones_training_cv.parquet"
        metadata_path = root / "air1_all_zones_training_cv.metadata.json"
        frame = pd.DataFrame(
            [
                self._row("2026-05-08 10:00:11", 1, 1.0, 0.0),
                self._row("2026-05-08 10:00:22", 2, 2.0, 1.0),
            ]
        )
        frame.to_csv(output_csv, index=False)

        metadata = collect_training_data._write_parquet_and_metadata_from_csv(
            output_csv=output_csv,
            output_parquet=output_parquet,
            metadata_path=metadata_path,
            sen55_csv=root / "sen55_data.csv",
            cv_labels=root / "cv_occupancy_all_air1_10sec.csv",
            occupied_threshold=1.0,
            requested_time_range={
                "start": datetime(2026, 5, 8, 2, 0, 0),
                "end": datetime(2026, 5, 8, 2, 1, 0),
            },
            fetch_metadata=None,
            elapsed_seconds=None,
            collector_mode="unit",
        )

        rebuilt = pd.read_parquet(output_parquet)
        self.assertEqual(
            pd.to_datetime(rebuilt["timestamp"]).dt.strftime("%H:%M:%S").tolist(),
            ["10:00:10", "10:00:20"],
        )
        self.assertEqual(rebuilt[contract.ZONE_ID_COLUMN].astype(int).tolist(), [1, 2])
        self.assertEqual(metadata["sample_interval_seconds"], 10)
        self.assertEqual(metadata["join"]["key"], "timestamp + zone_id")
        written_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        self.assertEqual(written_metadata["requested_time_range_utc"]["start"], "2026-05-08 02:00:00")
        self.assertEqual(written_metadata["requested_time_range_utc"]["end"], "2026-05-08 02:01:00")

    def test_live_collector_retrain_and_promotion_are_opt_in(self) -> None:
        def parse_with(env: dict[str, str], extra_args: list[str] | None = None) -> argparse.Namespace:
            argv = ["collect_training_data.py"]
            if extra_args:
                argv.extend(extra_args)
            with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(sys, "argv", argv):
                return collect_training_data.parse_args()

        default_args = parse_with({})
        self.assertFalse(default_args.retrain_after_parquet)
        self.assertFalse(default_args.promote_after_retrain)

        retrain_args = parse_with({"RETRAIN_AFTER_PARQUET": "1"})
        self.assertTrue(retrain_args.retrain_after_parquet)
        self.assertFalse(retrain_args.promote_after_retrain)

        promote_only_args = parse_with({"PROMOTE_AFTER_RETRAIN": "1"})
        self.assertFalse(promote_only_args.retrain_after_parquet)
        self.assertTrue(promote_only_args.promote_after_retrain)

        both_args = parse_with({"RETRAIN_AFTER_PARQUET": "1", "PROMOTE_AFTER_RETRAIN": "1"})
        self.assertTrue(both_args.retrain_after_parquet)
        self.assertTrue(both_args.promote_after_retrain)

        cli_args = parse_with({}, ["--retrain-after-parquet", "--promote-after-retrain"])
        self.assertTrue(cli_args.retrain_after_parquet)
        self.assertTrue(cli_args.promote_after_retrain)

    def test_promote_after_retrain_without_retrain_does_not_train_or_promote(self) -> None:
        root = fresh_test_dir("promote_without_retrain_all_zones")
        args = argparse.Namespace(
            append_every_sec=1.0,
            backfill_sec=120.0,
            parquet_rebuild_every_hours=1.0,
            duration_min=1440.0,
            time_start=None,
            time_end=None,
            output_csv=root / "air1_all_zones_training_cv.csv",
            output_parquet=root / "air1_all_zones_training_cv.parquet",
            metadata=root / "air1_all_zones_training_cv.metadata.json",
            sen55_csv=root / "sen55_data.csv",
            cv_labels=root / "cv_occupancy_all_air1_10sec.csv",
            occupied_threshold=1.0,
            require_cv_labels=False,
            api_timeout=30.0,
            api_retries=1,
            max_workers=1,
            chunk_days=1.0,
            min_chunk_hours=1.0,
            progress_every=1,
            verbose_progress=False,
            timing_summary=False,
            retrain_after_parquet=False,
            promote_after_retrain=True,
        )
        metadata = {
            "collector_mode": "live_append",
            "row_count": 15,
            "live_append": {"enabled": True},
        }

        with (
            mock.patch.object(collect_training_data, "_latest_csv_timestamp", return_value=None),
            mock.patch.object(collect_training_data, "_live_window_bounds", return_value=None),
            mock.patch.object(collect_training_data.csv_size_guard, "has_csv_rows", return_value=True),
            mock.patch.object(
                collect_training_data,
                "_write_parquet_and_metadata_from_csv",
                return_value=metadata,
            ),
            mock.patch.object(collect_training_data.training, "train_all_zones_from_csv") as train_mock,
            mock.patch.object(collect_training_data, "_promote_after_retrain") as promote_mock,
            mock.patch.object(collect_training_data.time, "sleep", side_effect=KeyboardInterrupt),
        ):
            collect_training_data.live_append_training_data(args)

        train_mock.assert_not_called()
        promote_mock.assert_not_called()

    def test_live_retrain_auto_promotion_records_output_tail(self) -> None:
        root = fresh_test_dir("live_retrain_auto_promote_all_zones")
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
        self.assertEqual(summary["current"], "model/runs/good")
        self.assertIn("Promoted CV-target run", summary["stdout_tail"])
        command = run_mock.call_args.args[0]
        self.assertIn("air1_all_zones.promote_model", command)
        self.assertIn("--candidate-run", command)
        self.assertIn(str(root / "model"), command)


class AllZonesSen55CollectorTests(unittest.TestCase):
    def test_sen55_late_payload_after_flush_is_skipped_without_full_rewrite(self) -> None:
        root = fresh_test_dir("sen55_late_payload_all_zones")
        csv_path = root / "sen55_data.csv"
        buffer = sen55_mqtt_collector.Sen55BucketBuffer(csv_path)
        buffer.add_payload({"timestamp": "2026-05-08 10:00:11", "sensor_id": "sen55_01", "pm1_0": 1.0})
        buffer.flush_completed(force=True)

        with mock.patch.object(
            sen55_mqtt_collector.csv_size_guard,
            "write_dataframe_rolling_atomic",
            side_effect=AssertionError("late payload should not force a full CSV rewrite"),
        ):
            result = buffer.add_payload(
                {"timestamp": "2026-05-08 10:00:12", "sensor_id": "sen55_01", "pm1_0": 9.0}
            )

        self.assertFalse(result["accepted"])
        self.assertEqual(buffer.skipped_late_payloads, 1)
        written = pd.read_csv(csv_path)
        self.assertEqual(len(written), 1)
        self.assertEqual(float(written.loc[0, "pm1_0"]), 1.0)


class AllZonesWebAndPackageAuditTests(unittest.TestCase):
    def test_file_backed_mjpeg_grabber_streams_latest_jpeg_and_marks_stale_files_disconnected(self) -> None:
        from web_app import rtsp_stream
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
        iterator = fresh.mjpeg_iterator(target_fps=100.0)
        next(iterator)
        sleep_calls = 0

        def fake_sleep(_delay: float) -> None:
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls >= 2:
                raise RuntimeError("unchanged frame skipped")

        with mock.patch.object(rtsp_stream.time, "sleep", side_effect=fake_sleep):
            with self.assertRaisesRegex(RuntimeError, "unchanged frame skipped"):
                next(iterator)
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

    def test_web_app_prefers_annotated_frame_grabbers_and_can_fallback_to_rtsp(self) -> None:
        from web_app.rtsp_stream import FileBackedMjpegGrabber, RtspFrameGrabber

        root = fresh_test_dir("annotated_frame_config")
        env = {
            "AIR1_ALL_ZONES_ANNOTATED_FRAME_CAM1": str(root / "cam1.jpg"),
            "AIR1_ALL_ZONES_ANNOTATED_FRAME_CAM2": "disabled",
            "AIR1_ALL_ZONES_RTSP_URL_CAM2": "rtsp://user:pass@example.test/cam2",
            "AIR1_ALL_ZONES_ANNOTATED_FRAME_MAX_AGE_SEC": "1.25",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            grabbers = web_main._build_rtsp_grabbers()

        self.assertIsInstance(grabbers["cam1"], FileBackedMjpegGrabber)
        self.assertIn("cam1.jpg", grabbers["cam1"].public_url)
        self.assertIsInstance(grabbers["cam2"], RtspFrameGrabber)
        self.assertEqual(grabbers["cam2"].public_url, "rtsp://***@example.test/cam2")

    def test_web_app_mjpeg_target_fps_is_configurable(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(web_main._build_mjpeg_target_fps(), 15.0)
        with mock.patch.dict(os.environ, {"AIR1_ALL_ZONES_MJPEG_TARGET_FPS": "22.5"}, clear=True):
            self.assertEqual(web_main._build_mjpeg_target_fps(), 22.5)
        with mock.patch.dict(os.environ, {"AIR1_ALL_ZONES_MJPEG_TARGET_FPS": "0"}, clear=True):
            self.assertEqual(web_main._build_mjpeg_target_fps(), 1.0)

    def test_health_and_video_routes_are_camera_scoped(self) -> None:
        class DummyGrabber:
            def __init__(self, name: str) -> None:
                self.name = name
                self.public_url = f"rtsp://***@{name}"
                self.connected = name == "cam1"
                self.latest_frame_age = 1.5 if name == "cam1" else None
                self.calls = 0
                self.target_fps_values: list[float] = []

            def mjpeg_iterator(self, target_fps: float = 10.0):
                self.calls += 1
                self.target_fps_values.append(target_fps)
                marker = f"{self.name}-jpeg".encode("ascii")
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + marker + b"\r\n"

        async def first_chunk(response):
            async for chunk in response.body_iterator:
                return chunk
            return b""

        grabbers = {"cam1": DummyGrabber("cam1"), "cam2": DummyGrabber("cam2")}
        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    inference_loop=SimpleNamespace(status=lambda: {"model_run_id": "run", "data_source": "dummy"}),
                    rtsp_grabbers=grabbers,
                    rtsp_grabber=grabbers["cam1"],
                    ground_truth_source=None,
                    config=SimpleNamespace(
                        tick_interval_sec=10.0,
                        history_size=240,
                        max_gap_minutes=5.0,
                        max_age_minutes=15.0,
                        threshold=0.5,
                        production_pointer=Path("model/production_run.txt"),
                    ),
                    mjpeg_target_fps=22.5,
                )
            )
        )

        response = asyncio.run(web_main.health(request))
        payload = json.loads(response.body)
        self.assertIn("rtsp_by_camera", payload)
        self.assertEqual(set(payload["rtsp_by_camera"]), {"cam1", "cam2"})
        self.assertEqual(payload["rtsp"], payload["rtsp_by_camera"]["cam1"])
        self.assertEqual(payload["rtsp_by_camera"]["cam1"]["host"], "10.158.71.241")
        self.assertEqual(payload["rtsp_by_camera"]["cam2"]["host"], "10.158.71.240")
        self.assertEqual(payload["rtsp_by_camera"]["cam1"]["coverage_zones"], [4, 9, 10, 11, 12, 13, 14, 15])
        self.assertEqual(payload["rtsp_by_camera"]["cam2"]["coverage_zones"], [1, 2, 3, 5, 6, 7, 8])
        self.assertEqual(payload["config"]["mjpeg_target_fps"], 22.5)

        cam2_response = asyncio.run(web_main.video_by_camera(request, "cam2"))
        self.assertIn(b"cam2-jpeg", asyncio.run(first_chunk(cam2_response)))
        self.assertEqual(grabbers["cam2"].calls, 1)
        self.assertEqual(grabbers["cam2"].target_fps_values, [22.5])
        self.assertEqual(grabbers["cam1"].calls, 0)

        legacy_response = asyncio.run(web_main.video(request))
        self.assertIn(b"cam1-jpeg", asyncio.run(first_chunk(legacy_response)))
        self.assertEqual(grabbers["cam1"].calls, 1)
        self.assertEqual(grabbers["cam1"].target_fps_values, [22.5])
        with self.assertRaises(HTTPException) as raised:
            asyncio.run(web_main.video_by_camera(request, "cam3"))
        self.assertEqual(raised.exception.status_code, 404)

    def test_cv_ground_truth_tailer_reads_per_zone_csv_and_parquet(self) -> None:
        root = fresh_test_dir("cv_ground_truth_tailer_all_zones")
        rows = pd.DataFrame(
            {
                "timestamp": [
                    "2026-05-08 10:00:11",
                    "2026-05-08 10:00:12",
                    "2026-05-08 10:00:13",
                    "2026-05-08 10:00:21",
                ],
                contract.ZONE_ID_COLUMN: [1, 2, 16, 1],
                "occupancy_count": [1, 0, 9, 0],
                contract.TARGET_COLUMN: [1, 0, 1, 0],
            }
        )
        csv_path = root / "cv_occupancy_all_air1_10sec.csv"
        parquet_path = root / "cv_occupancy_all_air1_10sec.parquet"
        rows.to_csv(csv_path, index=False)
        rows.to_parquet(parquet_path, index=False)

        csv_tailer = CvGroundTruthTailer(csv_path)
        parquet_tailer = CvGroundTruthTailer(parquet_path)
        self.assertEqual(csv_tailer.latest("2026-05-08 10:00:19").count, 1.0)
        self.assertTrue(csv_tailer.latest("2026-05-08 10:00:19").occupied)
        self.assertEqual(parquet_tailer.latest("2026-05-08 10:00:30").count, 0.0)
        self.assertFalse(parquet_tailer.latest("2026-05-08 10:00:30").occupied)
        self.assertEqual(csv_tailer.parquet_path, csv_path)
        self.assertEqual(csv_tailer.table_path, csv_path)
        fields = csv_tailer.latest_event_fields("2026-05-08 10:00:19")
        self.assertEqual(len(fields["ground_truth_by_zone"]), 15)
        self.assertNotIn("16", fields["ground_truth_by_zone"])
        self.assertEqual(fields["ground_truth_by_zone"]["1"]["count"], 1)
        self.assertEqual(fields["ground_truth_by_zone"]["2"]["count"], 0)
        self.assertIsNone(fields["ground_truth_by_zone"]["3"]["count"])
        self.assertIsNone(fields["ground_truth_by_zone"]["3"]["occupied"])

        missing = CvGroundTruthTailer(root / "missing.csv")
        status = missing.status()
        self.assertIn("CV ground-truth table not found", status["last_error"] or "")

    def test_tick_event_payload_excludes_table_16_and_keeps_null_zone_labels(self) -> None:
        event = TickEvent(
            timestamp="2026-05-08T10:00:00",
            probability=0.9,
            occupied=True,
            model_run_id="run",
            reference_time="2026-05-08T10:00:00",
            source_label="dummy",
            zone_probabilities={1: 0.1, 16: 0.9},
            aggregate_probability=0.5,
            occupied_zones=[1, 16],
            zone_count=2,
            ground_truth_by_zone={
                "1": {"count": 1, "occupied": True, "timestamp": "2026-05-08T10:00:00", "age_minutes": 0},
                "16": {"count": 9, "occupied": True, "timestamp": "2026-05-08T10:00:00", "age_minutes": 0},
            },
        )

        payload = event.to_dict()

        self.assertEqual(payload["zone_probabilities"], {"1": 0.1})
        self.assertEqual(payload["occupied_zones"], [1])
        self.assertEqual(payload["zone_count"], 1)
        self.assertEqual(len(payload["ground_truth_by_zone"]), 15)
        self.assertNotIn("16", payload["ground_truth_by_zone"])
        self.assertEqual(payload["ground_truth_by_zone"]["1"]["count"], 1.0)
        self.assertIsNone(payload["ground_truth_by_zone"]["2"]["count"])
        self.assertIsNone(payload["ground_truth_by_zone"]["2"]["occupied"])

    def test_smoke_test_reports_missing_trained_model_clearly(self) -> None:
        root = fresh_test_dir("smoke_missing_model_all_zones")
        candidate = root / "model"
        candidate.mkdir()
        output = io.StringIO()
        argv = ["smoke_test.py", "--candidate-run", str(candidate), "--skip-non-regression"]
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(output):
            rc = smoke_test.main()
        self.assertEqual(rc, 1)
        message = output.getvalue()
        self.assertIn("No trained model found", message)
        self.assertIn("current_run.txt", message)
        self.assertIn("production_run.txt", message)

    def test_static_dashboard_and_env_are_all_zones_specific(self) -> None:
        html = (PACKAGE_ROOT / "web_app" / "static" / "index.html").read_text(encoding="utf-8")
        app_js = (PACKAGE_ROOT / "web_app" / "static" / "app.js").read_text(encoding="utf-8")
        main_py = (PACKAGE_ROOT / "web_app" / "main.py").read_text(encoding="utf-8")
        env_example = (PACKAGE_ROOT / "web_app" / ".env.example").read_text(encoding="utf-8")
        local_env_path = PACKAGE_ROOT / "web_app" / ".env"
        local_env = local_env_path.read_text(encoding="utf-8") if local_env_path.exists() else ""
        bare_minimum = (PACKAGE_ROOT / "BARE_MINIMUM_TO_START_SERVICES.md").read_text(encoding="utf-8")
        ops_docs = "\n".join(
            (PACKAGE_ROOT / name).read_text(encoding="utf-8")
            for name in ["DEPLOYMENT.md", "DOCKER.md", "CICD_SYSTEMD.md", "CICD_DOCKER_COMPOSE.md"]
        )

        self.assertIn("AIR-1 All-Zones Occupancy Monitor", html)
        self.assertIn("YOLO Inference Video", html)
        self.assertIn("Per-Zone Operations Grid", html)
        self.assertIn("Aggregate CV count", html)
        self.assertIn("Per-zone CV labels", html)
        self.assertIn("rtsp-feed-cam1", html)
        self.assertIn("rtsp-feed-cam2", html)
        self.assertIn("/api/video/cam1.mjpg", html)
        self.assertIn("/api/video/cam2.mjpg", html)
        self.assertIn("cam1 tables 4, 9-15", html)
        self.assertIn("cam2 tables 1-3, 5-8", html)
        self.assertIn("ground_truth_by_zone", app_js)
        self.assertIn("zones above threshold", app_js)
        self.assertIn("care_ssl/all_zones/person_count_by_zone", env_example)
        self.assertIn("AIR1_ALL_ZONES_RTSP_URL_CAM1", env_example)
        self.assertIn("AIR1_ALL_ZONES_RTSP_URL_CAM2", env_example)
        self.assertIn("AIR1_ALL_ZONES_ANNOTATED_FRAME_CAM1", env_example)
        self.assertIn("AIR1_ALL_ZONES_ANNOTATED_FRAME_CAM2", env_example)
        self.assertIn("AIR1_ALL_ZONES_MJPEG_TARGET_FPS=15", env_example)
        self.assertIn("RETRAIN_AFTER_PARQUET=0", env_example)
        self.assertIn("PROMOTE_AFTER_RETRAIN=0", env_example)
        self.assertIn("AIR1_ALL_ZONES_ANNOTATED_FRAME_CAM1", main_py)
        self.assertIn("AIR1_ALL_ZONES_ANNOTATED_FRAME_CAM2", main_py)
        self.assertIn("10.158.71.241", env_example)
        self.assertIn("10.158.71.240", env_example)
        self.assertIn("AIR1_ALL_ZONES_RTSP_URL_CAM1", main_py)
        self.assertIn("AIR1_ALL_ZONES_RTSP_URL_CAM1", bare_minimum)
        self.assertIn("AIR1_ALL_ZONES_RTSP_URL_CAM2", bare_minimum)
        self.assertIn("10.158.71.241", bare_minimum)
        self.assertIn("10.158.71.240", bare_minimum)
        self.assertIn("care_ssl/all_zones/person_count_by_zone", bare_minimum)
        self.assertIn("10.158.71.241", ops_docs)
        self.assertIn("10.158.71.240", ops_docs)
        self.assertIn("config.mjpeg_target_fps", ops_docs + bare_minimum)
        self.assertIn("air1-all-zones-sen55-collector.service", ops_docs)
        self.assertNotIn("AIR1_ALL_ZONES_RTSP_URL_CAM2=...", ops_docs)
        self.assertNotIn("sen55-table.service", ops_docs)
        if local_env:
            self.assertIn("AIR1_ALL_ZONES_RTSP_URL_CAM1", local_env)
            self.assertIn("AIR1_ALL_ZONES_RTSP_URL_CAM2", local_env)
            self.assertIn("10.158.71.240", local_env)
            self.assertIn("care_ssl/all_zones/person_count_by_zone", local_env)
            self.assertNotIn("care_ssl/all_zones/person_count\n", local_env)
        self.assertNotIn("ZONE 05", html)
        self.assertNotIn("CV person count", html)
        self.assertNotIn("care_ssl/zone5/person_count", env_example)
        self.assertNotIn(" · ", html + app_js)

    def test_linux_entrypoints_and_docker_files_are_bom_free(self) -> None:
        paths = [
            ".dockerignore",
            ".gitignore",
            "Dockerfile",
            "docker-compose.yml",
            "docker-entrypoint.sh",
            "run_live_app.sh",
            "run_live_collector.sh",
            "run_person_counter.sh",
            "run_replay_app.sh",
            "run_sen55_collector.sh",
            "run_air1_all_zones_trainer.sh",
            "train_model.sh",
        ]
        for relative_path in paths:
            with self.subTest(path=relative_path):
                data = (PACKAGE_ROOT / relative_path).read_bytes()
                self.assertFalse(data.startswith(b"\xef\xbb\xbf"), f"{relative_path} starts with a UTF-8 BOM")


class AllZonesPromotionContractTests(unittest.TestCase):
    def _write_candidate(
        self,
        root: Path,
        run_id: str,
        target_column: str,
        *,
        validation_mode: str | None = None,
        cv_folds_used: int = 1,
        allow_degenerate_validation: bool = False,
    ) -> Path:
        validation_mode = validation_mode or training.STRICT_VALIDATION_MODE
        run_dir = root / "runs" / run_id
        models_dir = run_dir / "models"
        tables_dir = run_dir / "tables"
        models_dir.mkdir(parents=True, exist_ok=True)
        tables_dir.mkdir(parents=True, exist_ok=True)
        metrics = {
            "metrics_by_split": {
                "test": {
                    "pr_auc": 0.8,
                    "roc_auc": 0.8,
                    "brier_score": 0.2,
                    "bce_log_loss": 0.4,
                    "positive_rate": 0.5,
                    "n_windows": 20,
                    "positive_windows": 10,
                    "positive_buckets": 10,
                    "positive_events": 2,
                },
                "blind_test": {
                    "pr_auc": 0.8,
                    "roc_auc": 0.8,
                    "brier_score": 0.2,
                    "bce_log_loss": 0.4,
                    "positive_rate": 0.5,
                    "n_windows": 20,
                    "positive_windows": 10,
                    "positive_buckets": 10,
                    "positive_events": 2,
                },
            },
            "cv_metrics": {"best_mean_pr_auc": 0.75},
            "validation_mode": validation_mode,
            "cv_folds_requested": 1,
            "cv_folds_used": cv_folds_used,
            "allow_degenerate_validation": allow_degenerate_validation,
        }
        scaler = {
            "target_column": target_column,
            "feature_columns": training.FEATURE_COLUMNS,
            "raw_feature_columns": training.RAW_FEATURE_COLUMNS,
            "missing_indicator_columns": training.MISSING_INDICATOR_COLUMNS,
            "feature_fill_values": {col: 0.0 for col in training.RAW_FEATURE_COLUMNS},
            "means": {col: 0.0 for col in training.FEATURE_COLUMNS},
            "stds": {col: 1.0 for col in training.FEATURE_COLUMNS},
            "sample_interval_seconds": training.SAMPLE_INTERVAL_SECONDS,
            "lookback_minutes": 15,
            "lookback_rows": 90,
            "lookback": 90,
            "validation_mode": validation_mode,
            "cv_folds_requested": 1,
            "cv_folds_used": cv_folds_used,
            "allow_degenerate_validation": allow_degenerate_validation,
        }
        best_params = {
            "params": {"lookback": 90},
            "best_mean_cv_pr_auc": 0.75,
            "sample_interval_seconds": training.SAMPLE_INTERVAL_SECONDS,
            "lookback_minutes": 15,
            "lookback_rows": 90,
            "validation_mode": validation_mode,
            "cv_folds_requested": 1,
            "cv_folds_used": cv_folds_used,
            "allow_degenerate_validation": allow_degenerate_validation,
        }
        manifest = {
            "run_id": run_id,
            "zones": "all_air1",
            "grouping_column": contract.ZONE_ID_COLUMN,
            "target_column": target_column,
            "feature_columns": training.FEATURE_COLUMNS,
            "raw_feature_columns": training.RAW_FEATURE_COLUMNS,
            "missing_indicator_columns": training.MISSING_INDICATOR_COLUMNS,
            "sample_interval_seconds": training.SAMPLE_INTERVAL_SECONDS,
            "lookback_minutes": 15,
            "lookback_rows": 90,
            "validation_mode": validation_mode,
            "cv_folds_requested": 1,
            "cv_folds_used": cv_folds_used,
            "allow_degenerate_validation": allow_degenerate_validation,
            "files": [],
        }
        (tables_dir / "metrics_all_zones.json").write_text(json.dumps(metrics), encoding="utf-8")
        (tables_dir / "scaler_stats_all_zones.json").write_text(json.dumps(scaler), encoding="utf-8")
        (tables_dir / "best_params_all_zones.json").write_text(json.dumps(best_params), encoding="utf-8")
        (run_dir / training.RUN_MANIFEST_FILENAME).write_text(json.dumps(manifest), encoding="utf-8")
        torch.save(
            {
                "target_column": target_column,
                "params": best_params["params"],
                "sample_interval_seconds": training.SAMPLE_INTERVAL_SECONDS,
                "lookback_minutes": 15,
                "lookback_rows": 90,
                "validation_mode": validation_mode,
                "cv_folds_used": cv_folds_used,
                "allow_degenerate_validation": allow_degenerate_validation,
            },
            models_dir / "best_cnn_all_zones.pt",
        )
        (root / training.CURRENT_RUN_POINTER).write_text(run_id + "\n", encoding="utf-8")
        return run_dir

    def test_promotion_rejects_non_cv_target_and_accepts_valid_candidate(self) -> None:
        root = fresh_test_dir("promotion_all_zones")
        bad_root = root / "bad_model"
        good_root = root / "good_model"
        bad_run = self._write_candidate(bad_root, "bad", "legacy_zone_occupied")
        good_run = self._write_candidate(good_root, "good", training.TARGET_COLUMN)

        with self.assertRaises(ValueError):
            promote_model._load_run_payload(bad_run)

        production_pointer = root / "production_run.txt"
        argv = [
            "air1_all_zones.promote_model",
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
            pointer_path = (PACKAGE_ROOT / pointer_path).resolve()
        self.assertEqual(pointer_path, good_run.resolve())

    def test_promotion_rejects_bootstrap_candidate_after_production_exists(self) -> None:
        root = fresh_test_dir("promotion_bootstrap_after_prod_all_zones")
        production_root = root / "production"
        candidate_root = root / "candidate"
        production_run = self._write_candidate(production_root, "prod", training.TARGET_COLUMN)
        self._write_candidate(
            candidate_root,
            "bootstrap",
            training.TARGET_COLUMN,
            validation_mode=training.BOOTSTRAP_VALIDATION_MODE,
        )
        production_pointer = root / "production_run.txt"
        production_pointer.write_text(str(production_run), encoding="utf-8")

        argv = [
            "air1_all_zones.promote_model",
            "--candidate-run",
            str(candidate_root),
            "--production-pointer",
            str(production_pointer),
            "--skip-smoke",
            "--min-positive-windows",
            "1",
            "--min-positive-buckets",
            "1",
            "--min-positive-events",
            "1",
        ]
        output = io.StringIO()
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(output):
            self.assertEqual(promote_model.main(), 1)
        self.assertIn("bootstrap fallback candidates can only be promoted as the first production", output.getvalue())

    def test_promotion_rejects_degenerate_validation_candidate(self) -> None:
        root = fresh_test_dir("promotion_degenerate_all_zones")
        candidate_root = root / "candidate"
        self._write_candidate(
            candidate_root,
            "degenerate",
            training.TARGET_COLUMN,
            allow_degenerate_validation=True,
        )

        argv = [
            "air1_all_zones.promote_model",
            "--candidate-run",
            str(candidate_root),
            "--production-pointer",
            str(root / "production_run.txt"),
            "--skip-smoke",
            "--min-positive-windows",
            "1",
            "--min-positive-buckets",
            "1",
            "--min-positive-events",
            "1",
        ]
        output = io.StringIO()
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(output):
            self.assertEqual(promote_model.main(), 1)
        self.assertIn("degenerate validation", output.getvalue())

    def test_progressive_cv_policy_advances_from_existing_production_metadata(self) -> None:
        root = fresh_test_dir("progressive_cv_policy_all_zones")
        pointer = root / "production_run.txt"

        self.assertEqual(retrain_once._cv_folds_policy("auto", pointer)["cv_folds"], 1)
        for current_folds, expected_next in [(1, 2), (2, 3), (3, 3)]:
            run = self._write_candidate(
                root / f"prod_{current_folds}",
                f"prod_{current_folds}",
                training.TARGET_COLUMN,
                cv_folds_used=current_folds,
            )
            pointer.write_text(str(run), encoding="utf-8")
            policy = retrain_once._cv_folds_policy("auto", pointer)
            self.assertEqual(policy["cv_folds"], expected_next)

    def test_retrain_once_writes_status_and_respects_lock(self) -> None:
        root = fresh_test_dir("retrain_once_all_zones")
        summary = root / "model" / "retrain_status.json"
        lock_file = root / "model" / "retrain.lock"
        snapshot = {
            "source": str(root / "data" / "air1_all_zones_training_cv.csv"),
            "source_format": "csv",
            "snapshot_parquet": str(root / "data" / "training_snapshots" / "snap.parquet"),
        }
        train_result = {"run_id": "run1", "run_dir": str(root / "model" / "runs" / "run1")}

        argv = [
            "air1_all_zones.retrain_once",
            "--lock-file",
            str(lock_file),
            "--summary-json",
            str(summary),
            "--output-dir",
            str(root / "model"),
            "--production-pointer",
            str(root / "model" / "production_run.txt"),
            "--no-promote",
        ]
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(retrain_once, "_snapshot_training_source", return_value=snapshot),
            mock.patch.object(retrain_once.training, "train_all_zones_from_csv", return_value=train_result) as train_mock,
        ):
            self.assertEqual(retrain_once.main(), 0)

        self.assertTrue(summary.is_file())
        payload = json.loads(summary.read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["promotion"]["status"], "disabled")
        train_mock.assert_called_once()

        with retrain_once._exclusive_lock(lock_file, wait=False) as locked:
            self.assertTrue(locked)
            with mock.patch.object(sys, "argv", argv):
                self.assertEqual(retrain_once.main(), 0)
        locked_payload = json.loads(summary.read_text(encoding="utf-8"))
        self.assertEqual(locked_payload["status"], "skipped_locked")

    def test_web_model_loader_rejects_old_artifact_contract(self) -> None:
        root = fresh_test_dir("web_contract_all_zones")
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
        (run_dir / "tables" / "scaler_stats_all_zones.json").write_text(json.dumps(scaler), encoding="utf-8")
        (run_dir / "tables" / "best_params_all_zones.json").write_text(json.dumps(best_params), encoding="utf-8")
        torch.save({"target_column": training.TARGET_COLUMN, "params": best_params["params"]}, run_dir / "models" / "best_cnn_all_zones.pt")

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
                "sample_interval_seconds": training.SAMPLE_INTERVAL_SECONDS,
                "lookback_minutes": 15,
                "lookback_rows": 90,
                "lookback": 90,
            }
        )
        best_params.update(
            {
                "sample_interval_seconds": training.SAMPLE_INTERVAL_SECONDS,
                "lookback_minutes": 15,
                "lookback_rows": 90,
                "params": {"lookback": 90},
            }
        )
        (run_dir / "tables" / "scaler_stats_all_zones.json").write_text(json.dumps(scaler), encoding="utf-8")
        (run_dir / "tables" / "best_params_all_zones.json").write_text(json.dumps(best_params), encoding="utf-8")
        torch.save(
            {
                "target_column": training.TARGET_COLUMN,
                "params": best_params["params"],
                "sample_interval_seconds": training.SAMPLE_INTERVAL_SECONDS,
                "lookback_minutes": 15,
                "lookback_rows": 90,
            },
            run_dir / "models" / "best_cnn_all_zones.pt",
        )
        self.assertIsNotNone(loop._load_artifacts_blocking(run_dir))


class AllZonesLauncherTests(unittest.TestCase):
    def test_person_counter_defaults_to_two_camera_zone_tracker(self) -> None:
        ps1 = (PACKAGE_ROOT / "run_person_counter.ps1").read_text(encoding="utf-8")
        sh = (PACKAGE_ROOT / "run_person_counter.sh").read_text(encoding="utf-8")
        tracker = (PACKAGE_ROOT / "rtsp_zone_tracker.py").read_text(encoding="utf-8")
        combined = "\n".join([ps1, sh, tracker])

        self.assertNotIn("cam1-desk5-mask.png", combined)
        self.assertIn("rtsp_zone_tracker.py", ps1)
        self.assertIn("rtsp_zone_tracker.py", sh)
        self.assertIn("cam1-zones.json", ps1)
        self.assertIn("cam2-zones.json", sh)
        self.assertIn("masks\\cam1-mask-zones.png", ps1)
        self.assertIn("masks/cam2-mask-zones.png", sh)
        self.assertIn("10.158.71.241", combined)
        self.assertIn("10.158.71.240", combined)
        self.assertIn("care_ssl/all_zones/person_count_by_zone", combined)
        self.assertIn("--camera-id", combined)
        self.assertIn("--latest-jpeg", sh)
        self.assertIn("AIR1_ALL_ZONES_ANNOTATED_FRAME_CAM1", sh)
        self.assertIn("AIR1_ALL_ZONES_ANNOTATED_FRAME_CAM2", sh)
        self.assertIn("data/yolo_latest_cam1.jpg", sh)
        self.assertIn("data/yolo_latest_cam2.jpg", sh)
        self.assertIn('"counts_by_zone"', tracker)

    def test_live_launchers_default_to_mjpeg_15_and_no_inline_retrain(self) -> None:
        app_sh = (PACKAGE_ROOT / "run_live_app.sh").read_text(encoding="utf-8")
        collector_sh = (PACKAGE_ROOT / "run_live_collector.sh").read_text(encoding="utf-8")

        self.assertIn('AIR1_ALL_ZONES_MJPEG_TARGET_FPS="${AIR1_ALL_ZONES_MJPEG_TARGET_FPS:-15}"', app_sh)
        self.assertIn('RETRAIN_AFTER_PARQUET="${RETRAIN_AFTER_PARQUET:-0}"', collector_sh)
        self.assertIn('PROMOTE_AFTER_RETRAIN="${PROMOTE_AFTER_RETRAIN:-0}"', collector_sh)
        self.assertIn('RETRAIN_BOOTSTRAP_FALLBACK="${RETRAIN_BOOTSTRAP_FALLBACK:-auto}"', collector_sh)
        self.assertIn('RETRAIN_CV_FOLDS="${RETRAIN_CV_FOLDS:-auto}"', collector_sh)
        self.assertIn("--retrain-after-parquet", collector_sh)
        self.assertIn("--no-retrain-after-parquet", collector_sh)
        self.assertIn("--promote-after-retrain", collector_sh)
        self.assertIn("--no-promote-after-retrain", collector_sh)
        trainer_sh = (PACKAGE_ROOT / "run_air1_all_zones_trainer.sh").read_text(encoding="utf-8")
        self.assertIn("air1_all_zones.retrain_once", trainer_sh)
        self.assertIn('RETRAIN_N_TRIALS="${RETRAIN_N_TRIALS:-50}"', trainer_sh)
        self.assertIn('RETRAIN_MAX_EPOCHS="${RETRAIN_MAX_EPOCHS:-20}"', trainer_sh)
        self.assertIn('PROMOTE_AFTER_RETRAIN="${PROMOTE_AFTER_RETRAIN:-1}"', trainer_sh)


if __name__ == "__main__":
    unittest.main()
