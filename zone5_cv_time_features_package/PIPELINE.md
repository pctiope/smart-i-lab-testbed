# Zone 5 Pipeline

## Legacy Notice

This document describes the archived package-local CSV pipeline.

For the active desktop repo copy, use the repository-root DuckDB SQL-only migration path documented in:

- `README/README_db.md`
- `README/RUNTIME_GUIDE.md`
- `README/README_training_migrated.md`

The package-local collector and trainer wrappers in `zone5_cv_time_features_package/` are disabled in this desktop copy so the active path does not depend on CSV files.

This document describes the end-to-end Zone 5 occupancy pipeline in this
package: live collection, feature building, training, validation, promotion,
and inference. It is the conceptual map; `DEPLOYMENT.md` and `DOCKER.md`
remain the command runbooks.

## Scope

The pipeline is Zone 5 only. Generated runtime state stays under:

- `data/`
- `model/`
- `logs/`

The active persistent services are:

- `zone5-person-counter.service`
- `zone5-mqtt-aggregator.service`
- `zone5-sen55-collector.service`
- `zone5-live-collector.service`
- `zone5-live-app.service`

The active training scheduler is:

- `zone5-trainer.timer`
- `zone5-trainer.service`

The matching Docker Compose services are:

- `person-counter`
- `mqtt-aggregator`
- `sen55-collector`
- `live-collector`
- `live-app`

Optional Compose services are profile-gated: `replay-app` under `replay`, and
ops jobs such as `trainer`, `promoter`, `historical-collector`,
`csv-exporter`, `smoke-test`, `compile-check`, and `unit-tests` under `ops`.

## End-To-End Flow

```text
RTSP camera
  -> cv_counter/rtsp_person_mask_tracker_new.py
  -> MQTT topic care_ssl/zone5/person_count
  -> zone5.occupancy_mqtt_aggregator
  -> data/cv_occupancy_zone5_10sec.csv

SEN55 MQTT topic sen55_01/data
  -> zone5.sen55_mqtt_collector
  -> data/sen55_data.csv

AIR-1 API + smart plug API + mmWave API
  + data/cv_occupancy_zone5_10sec.csv
  + data/sen55_data.csv
  -> zone5.collect_training_data --live-append
  -> data/zone5_training_cv.csv
  -> data/zone5_training_cv.metadata.json

zone5-trainer.timer
  -> run_zone5_trainer.sh
  -> zone5.retrain_once
  -> stable snapshot in data/training_snapshots/
  -> zone5.training
  -> model/runs/<run_id>/
  -> model/current_run.txt

model/current_run.txt
  -> zone5.promote_model
  -> model/production_run.txt

model/production_run.txt
  + live AIR-1/API data
  + data/sen55_data.csv
  -> web_app
  -> occupancy probability on http://127.0.0.1:8000/
```

## Live Inference Health

Service reachability and inference output are different signals.
`/api/health` with `ok=true`, the dashboard page, and video availability prove
that the web app is reachable. `/api/current.probability` proves the live
inference path is producing a probability. Use `/api/current` for the live
prediction status and inspect `error` when `probability` is missing.

The diagnosed degraded-data failure mode is:

```text
LIVE DATA DEGRADED: core sensor coverage below gate
```

That means the core input coverage gate failed. AIR-1 and power fields require
at least `0.80` coverage, and `mmwave_s5` requires at least `0.95` coverage.
`sen55-missing` is not the blocker by itself because SEN55 is optional. The
core AIR-1, smart plug, and mmWave fields gate live prediction.

Operator flow:

1. Check `/api/current`.
2. Inspect the `error` field.
3. Verify upstream Smart I-Lab API history for the Zone 5 AIR-1, smart plug,
   and mmWave devices.
4. If upstream history is healthy but the running app cache remains stale,
   restart only the live app service.

## Trigger Summary

| Trigger | Owner | Result | Notes |
|---|---|---|---|
| Person-count loop | `zone5-person-counter.service` | Publishes counts to `care_ssl/zone5/person_count` and writes `data/person_counts.csv`. | Controlled by `COUNTS_EVERY` and `MQTT_EVERY`. |
| Completed 10-second CV bucket | `zone5-mqtt-aggregator.service` | Appends `data/cv_occupancy_zone5_10sec.csv`. | A bucket is flushed after the late-grace window. |
| Completed 10-second SEN55 bucket | `zone5-sen55-collector.service` | Appends `data/sen55_data.csv`. | Numeric values are averaged per bucket. |
| Live append interval | `zone5-live-collector.service` | Appends or updates `data/zone5_training_cv.csv`. | Default wrapper interval is 10 seconds with a 120-second backfill. |
| Joined-table snapshot metadata interval | `zone5-live-collector.service` | Updates `data/zone5_training_cv.metadata.json`. | Controlled by `SNAPSHOT_REFRESH_EVERY_HOURS`; no top-level Parquet cache is written. |
| Training timer | `zone5-trainer.timer` | Runs `zone5-trainer.service`. | `OnBootSec=5min`, then `OnUnitActiveSec=1h`; `Persistent=true`. |
| Retrain lock available | `zone5.retrain_once` | Creates a stable training snapshot and may train. | If `model/retrain.lock` is held, the retrain skips instead of overlapping. |
| Successful retrain | `zone5.retrain_once` | Writes `model/current_run.txt` and runs promotion unless disabled. | Promotion can still fail without stopping collectors. |
| Successful promotion | `zone5.promote_model` | Atomically updates `model/production_run.txt`. | The live app reloads the promoted model on pointer changes. |

## Runtime Services

### Person Counter

The person counter reads the Zone 5 RTSP stream, applies the packaged desk mask,
runs the packaged YOLO/ByteTrack counter by default, writes the masked annotated
frame to `data/yolo_latest.jpg`, and publishes person counts to MQTT.

Main command path:

```text
run_person_counter.sh
run_person_counter.ps1
cv_counter/rtsp_person_mask_tracker_new.py
```

Key outputs:

- MQTT topic: `care_ssl/zone5/person_count`
- Local CSV: `data/person_counts.csv`
- Latest annotated JPEG for the web app: `data/yolo_latest.jpg`

Default launcher settings use `TRACKER=cv_counter/trackers/bytetrack.yaml`,
`PERSON_COUNT_TRACKING=1`, `PERSON_COUNT_IMGSZ=256`, and
`PERSON_COUNT_SHOW_MASK=1`. BoT-SORT can still be selected by overriding
`TRACKER=cv_counter/trackers/botsort.yaml`.

The MQTT person-count stream is not the training target directly. It first
becomes a 10-second CV occupancy label table.

### CV MQTT Aggregator

`python -m zone5.occupancy_mqtt_aggregator` subscribes to the person-count
topic and writes one 10-second bucket at a time.

Defaults:

- MQTT topic: `care_ssl/zone5/person_count`
- Aggregation: `median`
- Occupied threshold: `1`
- Late grace: `5` seconds

Outputs:

- `data/cv_occupancy_zone5_10sec.csv`

The target rule is:

```text
zone_occupied = 1 when median occupancy_count >= occupied_threshold
zone_occupied = 0 when median occupancy_count < occupied_threshold
zone_occupied = null when no CV label exists for the bucket
```

The training target is `zone_occupied`. It comes from CV labels, not from
`mmwave_s5`, so `mmwave_s5` is an input feature and not target leakage.

### SEN55 Collector

`python -m zone5.sen55_mqtt_collector` subscribes to SEN55 MQTT messages and
writes package-local 10-second bucket tables.

Defaults:

- Broker: `10.158.71.19`
- Topic: `sen55_01/data`
- Username: `guest`

Outputs:

- `data/sen55_data.csv`

Numeric SEN55 fields are averaged per 10-second bucket. Metadata fields keep
the latest non-empty value. Late payloads for already-flushed buckets are
skipped.

### Live Collector

`python -m zone5.collect_training_data --live-append` joins sensor features and
CV labels into the training table. The shell wrapper is `run_live_collector.sh`.

The current wrapper defaults are:

```text
DURATION_MIN=1440
APPEND_EVERY_SEC=10
BACKFILL_SEC=120
SNAPSHOT_REFRESH_EVERY_HOURS=1
RETRAIN_AFTER_SNAPSHOT=0
PROMOTE_AFTER_RETRAIN=0
RETRAIN_BOOTSTRAP_FALLBACK=auto
RETRAIN_CV_FOLDS=auto
```

Systemd may override those values in the unit file. Check the active command
with:

```bash
systemctl --user --no-pager --full status zone5-live-collector.service
```

The live collector does four jobs in the deployed systemd path:

1. Fetch current Zone 5 AIR-1, smart plug, and mmWave histories from the API.
2. Read local SEN55 and CV-label tables.
3. Append deduplicated 10-second rows to `data/zone5_training_cv.csv`.
4. Refresh `data/zone5_training_cv.metadata.json` on schedule.

Each live append cycle refetches the latest `BACKFILL_SEC` seconds so delayed
SEN55 samples and CV labels can update recent rows.

## Feature Contract

The training table is sampled at 10 seconds. Core columns are:

- `timestamp`
- raw feature columns
- missing indicators
- engineered time features
- `zone_occupied`

Raw feature families:

- AIR-1: temperature, relative humidity, CO2, PM2.5
- Smart plug: power
- mmWave: `mmwave_s5`
- SEN55: PM, temperature, humidity, VOC, NOx
- Time features: hour and day-of-week sin/cos

`mmwave_s5` is the Zone 5 mmWave occupancy feature. Zone 5 maps to MSR-2 device
`89f464`. The parser accepts the Zone 5 mmWave occupancy fields and
target-like fields such as `radar_target` and still-target variants.

Missing raw values are retained. Training preprocessing adds `*_missing`
indicator columns and imputes raw missing values from training medians, or
`0.0` when no finite median exists.

Generated CSV files are capped by the configured CSV size guard. When a CSV
reaches the cap, the package keeps one physical CSV by retaining newest rows
that fit. Metadata refreshes and training snapshots use that current rolling
CSV window.

## Training Schedule

The live collector refreshes training metadata every
`SNAPSHOT_REFRESH_EVERY_HOURS`. With the default, metadata is refreshed hourly.
No top-level joined Parquet cache is written.

For direct `zone5.collect_training_data` usage, retraining can still run inline
after each live snapshot refresh when `--retrain-after-snapshot` is passed. The deployed
systemd path splits this work: `zone5-live-collector.service` uses
`RETRAIN_AFTER_SNAPSHOT=0`, and `zone5-trainer.timer` launches
`run_zone5_trainer.sh` separately.

The trainer takes `model/retrain.lock`, waits for the selected source to settle,
builds a fresh atomic Parquet snapshot in `data/training_snapshots/`, trains
from that snapshot, and promotes independently while live appends continue. The
default source is the live CSV (`--snapshot-source csv`) because it is the
freshest joined table. The older `--source-parquet` path is compatibility for
old immutable snapshots and should not be used as a live top-level cache path.

The next collector metadata refresh is based on collector process start time or
the last completed refresh. The next training time is controlled by
`zone5-trainer.timer`; if one training run is still active, the next run skips
because the lock is held.

Useful checks:

```bash
date '+%Y-%m-%d %H:%M:%S %Z %z'
systemctl --user show zone5-live-collector.service --property=ActiveEnterTimestamp,ExecMainStartTimestamp,ExecMainPID
stat data/zone5_training_cv.csv data/zone5_training_cv.metadata.json
tail -n 120 logs/live_collector.log
tail -n 120 logs/zone5_trainer.log
```

## Strict Validation

Strict validation is the normal production path.

Training keeps the latest calendar day as a hidden blind-test split. Earlier
calendar days are pre-test data. Strict rolling validation folds are selected
from the most recent pre-test validation dates.

Default strict policy:

```text
hidden blind test: latest calendar day
strict validation fold size: 1 calendar day
maximum strict folds: 3
lookbacks: 15, 60, and 180 minutes
```

For `--cv-folds 1`, only the most recent pre-test validation day is used. For
`--cv-folds 2`, the two most recent pre-test validation days are used when
enough days exist. For `--cv-folds 3`, the three most recent pre-test
validation days are used when enough days exist.

Strict training requires usable labels in all relevant windows:

- final pre-test training windows must include both classes
- every fold train window must include both classes
- every fold validation window must include both classes unless
  `--allow-degenerate-validation` is passed
- the blind test must be large enough for the selected lookback

Strict CV also filters under-covered dates before choosing rolling validation
folds. By default, a date must contain at least 75 percent of a full 10-second
day to be eligible as strict-CV train/validation date. Excluded dates and the
reason are recorded in `split_policy`.

This means a calendar date can exist in the CSV and still be excluded from
strict CV. For example, a partial first day is useful for bootstrap training
history, but it is not treated as a strict rolling validation day unless it has
enough 10-second coverage.

`--allow-degenerate-validation` is for smoke/dev only. Promotion rejects
degenerate-validation candidates even when training was allowed to create them.

## Bootstrap Fallback

Bootstrap fallback is only for the first production model.

When `--bootstrap-fallback` is enabled, training still tries strict validation
first. If strict validation has no viable lookback candidates, bootstrap
fallback keeps the same hidden blind-test day and creates one chronological
validation fold inside the pre-test rows:

```text
bootstrap train: first 80 percent of pre-test rows
bootstrap validation: last 20 percent of pre-test rows
hidden blind test: unchanged latest calendar day
```

Bootstrap still rejects single-class training windows. It does not bypass the
model contract or promotion gates.

Live retraining uses `--retrain-bootstrap-fallback auto` by default:

- no `model/production_run.txt`: bootstrap fallback is allowed after strict
  failure
- empty `model/production_run.txt`: bootstrap fallback is allowed after strict
  failure
- production pointer exists: bootstrap fallback is disabled

Promotion accepts a bootstrap candidate only when no production pointer exists.
After the first production model exists, replacements must come from strict
rolling validation.

## Progressive Strict Fold Maturity

Live retraining uses `--retrain-cv-folds auto` by default. Auto mode reads
`model/production_run.txt` and requests the next strict fold level:

| Production State | Next Strict Request |
|---|---|
| No production pointer | 1 fold |
| Bootstrap production | 1 fold |
| Strict 1-fold production | 2 folds |
| Strict 2-fold production | 3 folds |
| Strict 3-fold production | 3 folds |

This makes production mature progressively:

```text
first production may be bootstrap
bootstrap -> strict 1-fold
strict 1-fold -> strict 2-fold
strict 2-fold -> strict 3-fold
strict 3-fold -> strict 3-fold
```

A stricter candidate can skip ahead if it passes all gates. For example, a
strict 3-fold candidate can replace bootstrap or strict 1-fold production.
A strict 1-fold candidate may also replace the first strict 1-fold production
model when both runs use `rolling_calendar` validation and the candidate's
stored blind-test PR-AUC is greater than production's stored blind-test PR-AUC
plus `--min-pr-auc-delta`. That exception uses stored PR-AUC for this 1-fold
replacement comparison; the next auto retrain still requests 2 folds and the
remaining promotion gates, including smoke non-regression, still run.

## Training Artifacts

Training writes a run directory:

```text
model/runs/<run_id>/
  manifest.json
  models/best_cnn_zone_5.pt
  tables/best_params_zone_5.json
  tables/scaler_stats_zone_5.json
  tables/metrics_zone_5.json
```

Training also updates:

```text
model/current_run.txt
```

Artifacts record:

- source data path and format
- feature contract and target column
- lookback rows and minutes
- split sizes
- split policy
- validation mode
- requested and actual CV fold counts
- bootstrap fallback metadata when used
- per-fold CV metrics
- blind-test metrics

`model/current_run.txt` is a candidate pointer. The web app does not use it for
production inference.

## Promotion

Promotion runs automatically after each successful scheduled retrain unless
`run_zone5_trainer.sh` is launched with `PROMOTE_AFTER_RETRAIN=0`. Inline
collector retraining uses `--no-promote-after-retrain` for the same purpose.
Manual promotion is:

```bash
python -m zone5.promote_model \
  --candidate-run model \
  --production-pointer model/production_run.txt
```

Promotion checks:

- candidate artifacts exist and load
- target column is the CV target `zone_occupied`
- model uses the 10-second contract
- candidate is not a smoke/dev degenerate-validation run
- bootstrap candidates only promote as the first production model
- strict candidates meet the next required progressive fold count
- a better stored-PR-AUC strict 1-fold candidate may replace the first strict
  1-fold production model, but only before production matures to 2 folds
- blind-test PR-AUC meets `--min-test-pr-auc`
- mean CV PR-AUC meets `--min-mean-cv-pr-auc`
- blind-test positive windows meet `--min-positive-windows`
- blind-test positive 10-second buckets meet `--min-positive-buckets`
- blind-test contiguous positive events meet `--min-positive-events`
- blind-test Brier score and log loss are below configured maxima
- smoke test passes unless `--skip-smoke` is passed
- existing production is not regressed on the candidate blind-test window

Default promotion gate highlights:

```text
min test PR-AUC: 0
min mean CV PR-AUC: 0
min positive blind-test windows: 5
min positive blind-test buckets: 5
min positive blind-test events: 1
max test Brier: 1.0
max test log loss: 10.0
```

Positive windows are overlapping model windows and are useful as a model-input
gate. Positive buckets count labeled 10-second blind-test rows. Positive events
count contiguous occupied runs, split by non-positive/missing buckets or by
timestamp gaps larger than one sample interval.

Promotion and smoke testing use the same non-regression basis: if production
exists, production is scored on the candidate blind-test window before PR-AUC
comparison. Stored production blind-test metrics from an older window are used
only for the narrow strict 1-fold improvement exception above, not as the
general replacement or smoke non-regression comparison.

When promotion succeeds, it writes:

```text
model/production_run.txt
```

Promotion failure does not stop collection. The live collector logs the failure
and continues appending data.

## Pipeline Decisions

### Why Training Is Separate From Live Appends

Training can take minutes and may use CPU heavily. The deployed systemd path
keeps `zone5-live-collector.service` focused on appending data and refreshing
joined metadata. `zone5-trainer.timer` starts training in a separate
oneshot service, so live appends continue while training is busy.

### Why The Trainer Snapshots The CSV To Parquet

The live CSV is updated every append cycle. The trainer therefore defaults to
`--snapshot-source csv`, waits briefly for the CSV files to settle, writes an
atomic Parquet snapshot under `data/training_snapshots/`, and trains from that
immutable snapshot. The snapshot is kept because a training run needs a stable
table even while live appends continue.

### Why Bootstrap Is Limited To First Production

Bootstrap fallback exists to get an initial production model when strict rolling
CV is not mature yet. It does not replace the long-term standard. Once
`model/production_run.txt` exists, promotion rejects bootstrap candidates so
future replacements must prove strict rolling validation.

### Why Strict Folds Progress From 1 To 3

Immediately requiring three one-day strict folds can block production for days
even after a one-fold strict candidate is useful. Auto progression lets the
system first promote a 1-fold strict model, then requires 2 folds for the next
replacement, then 3 folds for the long-term standard. A stricter candidate can
still skip ahead if it passes the gates.

### Why Blind-Test Positive Gates Exist

PR-AUC, Brier score, and log loss are not meaningful when the blind test barely
contains any occupied evidence. Promotion therefore checks three evidence
counts before accepting a candidate:

- positive windows: model windows whose final target is occupied
- positive buckets: occupied 10-second blind-test rows
- positive events: contiguous occupied runs

The defaults require at least 5 positive windows, 5 positive buckets, and 1
positive event. These are minimum evidence checks, not quality targets.

### Why Same-Window Non-Regression Is Required

If production already exists, the candidate and production model must be scored
on the same candidate blind-test window before comparing PR-AUC. Comparing a new
candidate against production metrics from an older blind-test day would mix
different distributions and can accept a weaker replacement for the current
data. The only stored-metric comparison is the strict 1-fold improvement
exception, and it applies only to that first strict 1-fold replacement;
same-window smoke non-regression still runs afterward.

## Inference

The live web app uses:

```text
ZONE5_DATA_SOURCE=live
ZONE5_PRODUCTION_POINTER=model/production_run.txt
```

At startup and on pointer changes, the inference loop resolves
`model/production_run.txt`, loads the promoted run artifacts, checks the
feature contract, and serves occupancy probability.

The live data source uses:

- current AIR-1/API feature history
- current smart plug/API feature history
- current mmWave/API feature history
- package-local SEN55 table
- engineered time features

Live feature timestamps, CV ground-truth timestamps, and API event timestamps
are normalized to the Zone 5 local timeline, `Asia/Manila` (`UTC+08:00`), and
returned as naive ISO strings for the dashboard. This keeps Docker staging from
drifting behind when the container OS timezone is UTC.

Before the first successful promotion, the app stays running but reports that
the production pointer is missing. After promotion writes
`model/production_run.txt`, the app reloads the promoted model on its pointer
poll interval.

The model output is threshold-free probability by default. The app can also
compute an occupied boolean if `ZONE5_OCCUPIED_THRESHOLD` is configured.

## Deployment Modes

### Systemd

Systemd is the persistent Ubuntu deployment path. The important user services
are:

```bash
systemctl --user status zone5-person-counter.service
systemctl --user status zone5-mqtt-aggregator.service
systemctl --user status zone5-sen55-collector.service
systemctl --user status zone5-live-collector.service
systemctl --user status zone5-trainer.timer
systemctl --user status zone5-trainer.service
systemctl --user status zone5-live-app.service
```

Restart after code or wrapper changes:

```bash
systemctl --user daemon-reload
systemctl --user restart zone5-person-counter.service
systemctl --user restart zone5-mqtt-aggregator.service
systemctl --user restart zone5-sen55-collector.service
systemctl --user restart zone5-live-collector.service
systemctl --user restart zone5-trainer.timer
systemctl --user restart zone5-live-app.service
systemctl --user --no-pager --failed
```

### Docker Compose

Compose starts the same logical pipeline:

```bash
docker compose up -d --force-recreate person-counter mqtt-aggregator sen55-collector live-collector live-app
```

Ops-only containers are available under profiles for training, promotion,
historical collection, secondary AIR-1 CSV export, compile checks, smoke tests,
and unit tests.

## Files To Inspect

Runtime state:

```bash
ls -lh data/person_counts.csv
ls -lh data/cv_occupancy_zone5_10sec.csv
ls -lh data/sen55_data.csv
ls -lh data/zone5_training_cv.csv data/zone5_training_cv.metadata.json
ls -lh data/training_snapshots
ls -lah model/runs model/current_run.txt model/production_run.txt
```

Logs:

```bash
tail -n 120 logs/live_collector.log
tail -n 120 logs/live_app.log
journalctl --user -u zone5-live-collector.service -n 120 --no-pager
journalctl --user -u zone5-live-app.service -n 120 --no-pager
```

Model artifacts:

```bash
cat model/current_run.txt
cat model/production_run.txt
python -m json.tool model/runs/<run_id>/manifest.json
python -m json.tool model/runs/<run_id>/tables/metrics_zone_5.json
```

## Common States

### Production Pointer Missing

This is normal before the first successful promotion. The app stays up and
reports the missing pointer. The collectors should continue building data.

Check:

```bash
ls -lah model
tail -n 120 logs/live_collector.log
```

### Training Fails With No Labeled Rows

The joined table has no usable `zone_occupied` labels. Keep the person counter
and MQTT aggregator running so CV labels are created.

### Training Fails With Too Few Calendar Days

Strict training needs enough calendar-day structure for a true hidden day and
validation folds. With fewer than two coverage-eligible calendar days before
the hidden day, strict training fails. Under-covered partial dates are recorded
in `split_policy.strict_date_excluded_dates`.

### Training Fails With Single-Class Windows

This means at least one training or validation window has only occupied or only
unoccupied labels after the selected lookback is applied. More labeled examples
from both classes are needed in the relevant calendar windows.

### Bootstrap Trains But Does Not Promote

Bootstrap can train as the first-model fallback, but promotion still requires
blind-test gates. The default positive-window gate is five positive blind-test
windows.

### Strict 1-Fold Cannot Promote Yet

Strict 1-fold requires the fold training side to include both classes. If the
only pre-validation training day has zero positives, strict 1-fold remains
blocked until a later calendar day makes both classes available on the training
side.

### Promotion Skips Because Candidate Is Not Better

When production already exists, promotion compares candidate blind-test PR-AUC
against production on the same candidate blind-test window plus the configured
delta. The strict 1-fold improvement exception first compares stored blind-test
PR-AUC against the first strict 1-fold production model. If the candidate is not
better at the applicable gate, promotion leaves `model/production_run.txt`
unchanged.

## Minimal Manual Checks

Check label availability in the current training CSV:

```bash
python - <<'PY'
from pathlib import Path
import pandas as pd
from zone5 import training

frame, path = training.load_zone_5_csv(Path("data/zone5_training_cv.csv"), allow_bad_lines=True)
labels = frame[training.TARGET_COLUMN].dropna().astype(int)
print("source", path)
print("rows", len(frame), "labeled", len(labels))
print("start", frame[training.TIMESTAMP_COLUMN].min())
print("end", frame[training.TIMESTAMP_COLUMN].max())
print("label_counts", labels.value_counts().sort_index().to_dict())
print(frame.groupby(pd.to_datetime(frame[training.TIMESTAMP_COLUMN]).dt.date)[training.TARGET_COLUMN]
      .agg(["count", "sum"]))
PY
```

Check strict and bootstrap split viability:

```bash
python - <<'PY'
from pathlib import Path
from zone5 import training

frame, _ = training.load_zone_5_csv(Path("data/zone5_training_cv.csv"), allow_bad_lines=True)
for folds in [1, 2, 3]:
    plan = training.select_cv_lookback_plan(frame, cv_folds=folds, bootstrap_fallback=False)
    print("strict", folds, "used", plan["split_policy"]["cv_folds_used"],
          "candidates", plan["lookback_candidates"],
          "rejected", plan["rejected_lookbacks"])

plan = training.select_cv_lookback_plan(frame, cv_folds=1, bootstrap_fallback=True)
print("fallback mode", plan["split_policy"]["validation_mode"])
print("fallback candidates", plan["lookback_candidates"])
print("fallback rejected", plan["rejected_lookbacks"])
PY
```

Run a short training smoke without touching production:

```bash
python -m zone5.training \
  --csv data/zone5_training_cv.csv \
  --output-dir .test_runtime/train_smoke \
  --cv-folds 1 \
  --bootstrap-fallback \
  --allow-degenerate-validation \
  --n-trials 1 \
  --max-epochs 1 \
  --optuna-jobs 1
```

## Source Modules

Pipeline entrypoints:

- `cv_counter/rtsp_person_mask_tracker_new.py`
- `zone5.occupancy_mqtt_aggregator`
- `zone5.sen55_mqtt_collector`
- `zone5.collect_training_data`
- `zone5.training`
- `zone5.promote_model`
- `web_app.main`

Core implementation files:

- `zone5/air1_exporter.py`
- `zone5/feature_builder.py`
- `zone5/feature_contract.py`
- `zone5/dataset.py`
- `zone5/training.py`
- `zone5/promote_model.py`
- `web_app/inference_loop.py`
- `smoke_test.py`
