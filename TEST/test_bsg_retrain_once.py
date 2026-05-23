from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "zone5_cv_time_features_package") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "zone5_cv_time_features_package"))

import bsg_retrain_once as retrain  # noqa: E402


class TestBsgRetrainContract(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_main_writes_skipped_summary_when_lock_is_held(self) -> None:
        lock_file = self.root / "model" / "retrain.lock"
        summary_json = self.root / "model" / "retrain_status.json"
        with retrain._exclusive_lock(lock_file, wait=False) as locked:
            self.assertTrue(locked)
            argv = [
                "bsg_retrain_once.py",
                "--lock-file",
                str(lock_file),
                "--summary-json",
                str(summary_json),
                "--output-dir",
                str(self.root / "model"),
                "--no-promote",
            ]
            with mock.patch.object(sys, "argv", argv):
                self.assertEqual(retrain.main(), 0)

        payload = json.loads(summary_json.read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "skipped_locked")
        self.assertEqual(payload["lock_file"], str(lock_file))

    def test_main_writes_legacy_summary_shape_for_successful_no_promote_run(self) -> None:
        lock_file = self.root / "model" / "retrain.lock"
        summary_json = self.root / "model" / "retrain_status.json"
        output_dir = self.root / "model"
        snapshot_path = output_dir / "training_input_snapshots" / "snapshot.parquet"
        snapshot = {
            "snapshot_path": snapshot_path,
            "snapshot_rows": 10,
            "source_table": "silver.zone5_training_input",
            "read_only_live": True,
        }
        argv = [
            "bsg_retrain_once.py",
            "--lock-file",
            str(lock_file),
            "--summary-json",
            str(summary_json),
            "--output-dir",
            str(output_dir),
            "--production-pointer",
            str(output_dir / "production_run.txt"),
            "--read-only-live",
            "--no-promote",
            "--bootstrap-fallback",
            "always",
            "--cv-folds",
            "1",
            "--min-positive-windows",
            "0",
            "--min-positive-buckets",
            "0",
            "--min-positive-events",
            "0",
        ]
        with mock.patch.object(sys, "argv", argv), \
            mock.patch.object(retrain, "_build_training_snapshot", return_value=snapshot), \
            mock.patch.object(retrain.training, "train_zone_5_from_csv", return_value={"run_id": "run_001"}):
            self.assertEqual(retrain.main(), 0)

        payload = json.loads(summary_json.read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["promotion"]["status"], "disabled")
        self.assertEqual(payload["train_result"]["run_id"], "run_001")
        self.assertEqual(payload["snapshot"]["source_table"], "silver.zone5_training_input")
        self.assertTrue(payload["snapshot"]["read_only_live"])

    def test_promote_maps_pointer_change_to_promoted_status(self) -> None:
        output_dir = self.root / "model"
        pointer = output_dir / "production_run.txt"
        pointer.parent.mkdir(parents=True, exist_ok=True)
        pointer.write_text("model/runs/old\n", encoding="utf-8")

        def fake_run(*_args, **_kwargs):
            pointer.write_text("model/runs/new\n", encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="promotion ok\n", stderr="")

        args = argparse.Namespace(
            output_dir=output_dir,
            production_pointer=pointer,
            min_positive_windows=5,
            min_positive_buckets=5,
            min_positive_events=1,
            promote_skip_smoke=False,
            promote_skip_non_regression_smoke=False,
        )
        with mock.patch.object(retrain.subprocess, "run", side_effect=fake_run):
            promotion = retrain._promote(args)

        self.assertEqual(promotion["status"], "promoted")
        self.assertEqual(promotion["previous"], "model/runs/old")
        self.assertEqual(promotion["current"], "model/runs/new")
        self.assertIn("stdout_tail", promotion)

    def test_build_training_snapshot_passes_read_only_live_to_bsg_builder(self) -> None:
        output_dir = self.root / "model"
        calls = {}

        def fake_build_training_snapshot(**kwargs):
            calls.update(kwargs)
            return {"snapshot_path": output_dir / "snapshot.parquet"}

        args = argparse.Namespace(
            output_dir=output_dir,
            rebuild_training_input=False,
            occupied_threshold=1.0,
            read_only_live=True,
        )
        with mock.patch.object(retrain.train_zone5_migrated, "build_training_snapshot", side_effect=fake_build_training_snapshot):
            snapshot = retrain._build_training_snapshot(args)

        self.assertEqual(snapshot["snapshot_path"], output_dir / "snapshot.parquet")
        self.assertEqual(calls["output_dir"], output_dir)
        self.assertFalse(calls["rebuild_training_input"])
        self.assertTrue(calls["read_only_live"])


class TestBsgSystemdUnits(unittest.TestCase):
    def test_live_collector_runs_root_bsg_ingestion(self) -> None:
        unit = (REPO_ROOT / "zone5_cv_time_features_package/systemd/user/zone5-live-collector.service").read_text(
            encoding="utf-8"
        )
        self.assertIn("WorkingDirectory=%h/smart-i-lab-testbed", unit)
        self.assertIn("run_bsg_live_collector.sh", unit)
        self.assertIn("SMART_ILAB_DUCKDB_PATH=%h/smart-i-lab-testbed/data/smart_ilab.duckdb", unit)
        self.assertNotIn("run_live_collector.sh", unit)

    def test_trainer_runs_root_bsg_retrain_wrapper(self) -> None:
        unit = (REPO_ROOT / "zone5_cv_time_features_package/systemd/user/zone5-trainer.service").read_text(
            encoding="utf-8"
        )
        self.assertIn("WorkingDirectory=%h/smart-i-lab-testbed", unit)
        self.assertIn("run_bsg_trainer.sh", unit)
        self.assertIn("BSG_MANAGE_SYSTEMD_INGESTION=1", unit)
        self.assertNotIn("run_zone5_trainer.sh", unit)

    def test_live_app_keeps_existing_port_and_pointer_contract(self) -> None:
        unit = (REPO_ROOT / "zone5_cv_time_features_package/systemd/user/zone5-live-app.service").read_text(
            encoding="utf-8"
        )
        self.assertIn("Environment=PORT=8000", unit)
        self.assertIn("Environment=ZONE5_PRODUCTION_POINTER=model/production_run.txt", unit)
        self.assertIn("ExecStart=/usr/bin/env bash -lc 'exec bash run_live_app.sh", unit)


if __name__ == "__main__":
    unittest.main(verbosity=2)
