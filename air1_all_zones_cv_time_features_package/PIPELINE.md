# AIR-1 All-Zones Pipeline

This document describes the persistent AIR-1 all-zones occupancy pipeline:
two-camera CV labels, live feature collection, scheduled training, validation,
promotion, and inference. `DEPLOYMENT.md` and `DOCKER.md` remain the command
runbooks.

## Scope

The pipeline trains and serves one shared model for AIR-1 zones 1-15. Runtime
state is generated under:

- `data/`
- `model/`
- `logs/`

The persistent systemd services are:

- `air1-all-zones-person-counter-cam1.service`
- `air1-all-zones-person-counter-cam2.service`
- `air1-all-zones-mqtt-aggregator.service`
- `air1-all-zones-sen55-collector.service`
- `air1-all-zones-live-collector.service`
- `air1-all-zones-live-app.service`

The training scheduler is:

- `air1-all-zones-trainer.timer`
- `air1-all-zones-trainer.service`

## End-To-End Flow

```text
cam1 RTSP
  -> rtsp_zone_tracker.py
  -> MQTT topic care_ssl/all_zones/person_count_by_zone
  -> data/person_counts_by_zone_cam1.csv

cam2 RTSP
  -> rtsp_zone_tracker.py
  -> MQTT topic care_ssl/all_zones/person_count_by_zone
  -> data/person_counts_by_zone_cam2.csv

per-zone MQTT payloads
  -> air1_all_zones.occupancy_mqtt_aggregator
  -> data/cv_occupancy_all_air1_10sec.csv
  -> data/cv_occupancy_all_air1_10sec.parquet

SEN55 MQTT topic sen55_01/data
  -> air1_all_zones.sen55_mqtt_collector
  -> data/sen55_data.csv
  -> data/sen55_data.parquet

AIR-1 API + data/sen55_data.csv + data/cv_occupancy_all_air1_10sec.csv
  -> air1_all_zones.collect_training_data --live-append
  -> data/air1_all_zones_training_cv.csv
  -> data/air1_all_zones_training_cv.parquet
  -> data/air1_all_zones_training_cv.metadata.json

air1-all-zones-trainer.timer
  -> run_air1_all_zones_trainer.sh
  -> air1_all_zones.retrain_once
  -> stable snapshot in data/training_snapshots/
  -> model/runs/<run_id>/
  -> model/current_run.txt
  -> air1_all_zones.promote_model
  -> model/production_run.txt

model/production_run.txt
  + live AIR-1/API data
  + package-local SEN55 and CV label tables
  -> web_app
  -> shared all-zones probability output on http://127.0.0.1:8000/
```

## Trigger Summary

| Trigger | Owner | Result | Notes |
|---|---|---|---|
| Cam1 person-count loop | `air1-all-zones-person-counter-cam1.service` | Publishes per-zone counts and writes `data/person_counts_by_zone_cam1.csv`. | Cam1 covers its configured zone mask only. |
| Cam2 person-count loop | `air1-all-zones-person-counter-cam2.service` | Publishes per-zone counts and writes `data/person_counts_by_zone_cam2.csv`. | Cam2 covers its configured zone mask only. |
| Completed 10-second CV bucket | `air1-all-zones-mqtt-aggregator.service` | Appends `data/cv_occupancy_all_air1_10sec.csv`. | Labels are keyed by `timestamp + zone_id`. |
| Completed 10-second SEN55 bucket | `air1-all-zones-sen55-collector.service` | Appends `data/sen55_data.csv`. | Numeric values are averaged per bucket. |
| Live append interval | `air1-all-zones-live-collector.service` | Appends or updates `data/air1_all_zones_training_cv.csv`. | Default wrapper interval is 10 seconds with a 120-second backfill. |
| Joined-table snapshot interval | `air1-all-zones-live-collector.service` | Rebuilds Parquet and metadata. | `PARQUET_REBUILD_EVERY_HOURS=1` by default. |
| Training timer | `air1-all-zones-trainer.timer` | Runs `air1-all-zones-trainer.service`. | `OnBootSec=5min`, then `OnUnitActiveSec=1h`; `Persistent=true`. |
| Retrain lock available | `air1_all_zones.retrain_once` | Creates a stable snapshot and may train. | If `model/retrain.lock` is held, retrain skips rather than overlaps. |
| Successful retrain | `air1_all_zones.retrain_once` | Writes `model/current_run.txt` and runs promotion unless disabled. | `current_run.txt` is a candidate pointer. |
| Successful promotion | `air1_all_zones.promote_model` | Atomically updates `model/production_run.txt`. | The live app uses only the production pointer for inference. |

## Feature And Label Contract

Rows are long-form: one row per `timestamp + zone_id`. Valid model zones are
`1-15`; Table 16 is intentionally excluded until the feature contract changes.

The model input excludes `zone_id`, mmWave, and power features. `zone_id` is
kept for grouping, windows, audits, splits, and display. Features come from
AIR-1, shared SEN55, missing indicators, and time features.

The target is `occupied`:

```text
occupied = 1 when the per-zone median occupancy_count >= 1
occupied = 0 when the per-zone median occupancy_count < 1
occupied = null when no CV label exists for that timestamp + zone_id
```

Missing camera coverage stays null. Do not convert missing labels to zero.

## Training Schedule

The deployed systemd path keeps collection and training separate:

- `air1-all-zones-live-collector.service` appends live rows and rebuilds the
  joined CSV/Parquet snapshots.
- `air1-all-zones-trainer.timer` starts `air1-all-zones-trainer.service`.
- `air1-all-zones-trainer.service` runs `run_air1_all_zones_trainer.sh`.

The trainer defaults are:

```text
RETRAIN_N_TRIALS=50
RETRAIN_MAX_EPOCHS=20
RETRAIN_OPTUNA_JOBS=1
RETRAIN_BOOTSTRAP_FALLBACK=auto
RETRAIN_CV_FOLDS=auto
PROMOTE_AFTER_RETRAIN=1
```

The trainer snapshots the current CSV or Parquet under
`data/training_snapshots/`, writes `model/retrain_status.json`, trains into
`model/runs/<run_id>/`, updates `model/current_run.txt`, and then attempts
promotion.

Useful checks:

```bash
date '+%Y-%m-%d %H:%M:%S %Z %z'
systemctl --user status air1-all-zones-live-collector.service
systemctl --user status air1-all-zones-trainer.timer
systemctl --user status air1-all-zones-trainer.service
stat data/air1_all_zones_training_cv.csv data/air1_all_zones_training_cv.parquet data/air1_all_zones_training_cv.metadata.json
tail -n 120 logs/live_collector.log
tail -n 120 logs/air1_all_zones_trainer.log
ls -lah model/runs model/current_run.txt model/production_run.txt model/retrain_status.json
```

## Validation And Promotion

Strict validation is the normal path. Training keeps the latest calendar day as
a hidden blind-test split, then uses one-day rolling validation folds from the
pre-test dates. Auto CV fold policy matures production progressively:

| Production State | Next Strict Request |
|---|---|
| No production pointer | 1 fold |
| Bootstrap production | 1 fold |
| Strict 1-fold production | 2 folds |
| Strict 2-fold production | 3 folds |
| Strict 3-fold production | 3 folds |

`RETRAIN_BOOTSTRAP_FALLBACK=auto` allows a first-model chronological fallback
only when `model/production_run.txt` is missing or empty. Once a production
pointer exists, replacements must pass strict rolling validation.

Promotion checks the validation mode, CV fold maturity, blind-test PR-AUC,
Brier score, log loss, positive windows, positive 10-second buckets, positive
events, smoke-test loading, and same-window non-regression when production
already exists. Promotion failure does not stop collection.

## Deployment Checks

Install all user units from the package root:

```bash
install -m 0644 systemd/user/air1-all-zones-*.service systemd/user/air1-all-zones-*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
```

Expected installed trainer units:

```bash
systemctl --user status air1-all-zones-trainer.timer
systemctl --user status air1-all-zones-trainer.service
```

Enable the trainer timer only after:

- no `air1_all_zones.retrain_once` process is already running;
- the live collector is active;
- `data/air1_all_zones_training_cv.csv` has labeled rows for zones 1-15;
- the selected strict fold level has viable lookbacks.

If another full trainer is active, wait for it to finish before starting AIR1
training so CPU and I/O contention do not hide the real result.

## Common States

### Production Pointer Missing

This is normal before the first successful promotion. The app can stay up, but
inference is not production-ready until `model/production_run.txt` exists.

Check:

```bash
ls -lah model
tail -n 120 logs/air1_all_zones_trainer.log
cat model/retrain_status.json
```

If `logs/air1_all_zones_trainer.log` is missing, the trainer service has not
started yet. Check the timer and service:

```bash
systemctl --user status air1-all-zones-trainer.timer
systemctl --user status air1-all-zones-trainer.service
journalctl --user -u air1-all-zones-trainer.service -n 120 --no-pager
```

### Training Skips Because The Lock Exists

`model/retrain.lock` is not a blocker by itself. It matters only when a live
trainer process holds the lock. Check for an active retrain process before
removing or changing runtime artifacts.

```bash
ps -eo pid,ppid,stat,etimes,cmd | rg 'air1_all_zones\.retrain_once|run_air1_all_zones_trainer'
```

### Training Fails With No Labeled Rows

The joined table has no usable `occupied` labels. Keep both person counters and
the MQTT aggregator running until `data/cv_occupancy_all_air1_10sec.csv`
contains current labels for the expected zones.

### Strict Training Has No Viable Lookbacks

Strict training needs enough calendar-day structure and both occupied and
unoccupied examples in the relevant train, validation, and blind-test windows.
Under-covered dates and rejected lookbacks are written into the split policy or
`model/retrain_status.json`.

### Bootstrap Trains But Does Not Promote

Bootstrap is only a first-production recovery path. It still must pass the
blind-test evidence gates and smoke test before promotion writes
`model/production_run.txt`.

## Minimal Manual Split Check

Run this from the package root to see the current label and lookback state:

```bash
python - <<'PY'
from pathlib import Path
import pandas as pd
from air1_all_zones import training

frame, path = training.load_all_zones_csv(Path("data/air1_all_zones_training_cv.csv"), allow_bad_lines=True)
labels = frame[training.TARGET_COLUMN].dropna().astype(int)
print("source", path)
print("rows", len(frame), "labeled", len(labels))
print("start", frame[training.TIMESTAMP_COLUMN].min())
print("end", frame[training.TIMESTAMP_COLUMN].max())
print("zones", sorted(frame[training.ZONE_ID_COLUMN].dropna().astype(int).unique().tolist()))
print("label_counts", labels.value_counts().sort_index().to_dict())
print(frame.assign(_date=pd.to_datetime(frame[training.TIMESTAMP_COLUMN]).dt.date)
      .groupby("_date")[training.TARGET_COLUMN].agg(["count", "sum"]))

for folds in [1, 2, 3]:
    plan = training.select_cv_lookback_plan(frame, cv_folds=folds, bootstrap_fallback=False)
    print("strict", folds,
          "used", plan["split_policy"].get("cv_folds_used"),
          "candidates", plan["lookback_candidates"],
          "rejected", plan["rejected_lookbacks"])
PY
```
