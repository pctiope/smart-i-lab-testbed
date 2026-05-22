# Zone 5 CV Time-Features Package

This package trains and serves the Zone 5 occupancy model from scratch using
CV person-count labels plus AIR-1, smart plug, mmWave, SEN55, and engineered
time features. It intentionally does not include generated training data,
archived runs, or a production model. The existing wrapper script names remain
the user-facing commands. The CV counter runtime assets are package-local under
`cv_counter/`, so deployment does not depend on files outside this folder.
The first-class persistent deployment paths are Ubuntu user-systemd and Docker
Compose. PowerShell wrappers remain for legacy/manual runs only.

## Install

Ubuntu without a venv, from this package root. Dependencies are installed into
the package-local `.python-packages/` directory so they do not conflict with
other user Python tools such as `emuvim`. The person counter defaults to
Ultralytics ByteTrack tracking and can still be switched to BoT-SORT; the
shared tracker dependency `lap` is included in `requirements.txt` so it is
installed before the service starts. The person-counter launcher also sets
`YOLO_AUTOINSTALL=False`, so missing
dependencies fail with a clear package-local install command instead of trying
to write into `/usr/local`:

```bash
python3 -m pip install --upgrade pip
python3 -m pip install --target .python-packages --upgrade -r requirements.txt
```

When running direct Python commands without the `.sh` wrappers or systemd, set
`PYTHONPATH` first:

```bash
export PYTHONPATH="$PWD/.python-packages:$PWD"
```

Legacy/manual PowerShell setup, from this package root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## Docker

This package includes a Dockerfile and Compose stack for the live person
counter, MQTT aggregators, live collector, web app, replay dashboard, historical
collection, secondary AIR-1 CSV export, one-shot training, promotion, smoke
tests, compile checks, and unit tests. Use [DOCKER.md](DOCKER.md) for the
container workflow.

Quick start from this package root:

```bash
[ -f web_app/.env ] || cp web_app/.env.example web_app/.env
docker compose build
docker compose up -d
```

Open `http://localhost:8000/`. Runtime state is bind-mounted from the host
under `data/`, `model/`, and `logs/`.

## Deployment Runbook

Use [DEPLOYMENT.md](DEPLOYMENT.md) for the persistent Ubuntu systemd commands
and legacy/manual Windows Task Scheduler examples. The deployment runbook also
states when each CSV is written, which processes should stay running, and how
to check logs and outputs.

Use [PIPELINE.md](PIPELINE.md) for the end-to-end pipeline map covering runtime
services, generated files, training validation, bootstrap fallback, promotion,
and inference.

On Ubuntu, make the first real run persistent immediately: start at
`Ubuntu Server Start Here` in [DEPLOYMENT.md](DEPLOYMENT.md). That path installs
the bundled user services from `systemd/user/` and starts them with
`systemctl --user enable --now`, instead of manually running the `.sh` files in
terminals. The Ubuntu service path uses `python3` directly; it does not require
`.venv` and uses package-local `.python-packages/`.

For an Ubuntu server, the intended order is:

```bash
cd ~/zone5_cv_time_features_package
python3 -m pip install --target .python-packages --upgrade -r requirements.txt
mkdir -p data model logs ~/.config/systemd/user
[ -f web_app/.env ] || cp web_app/.env.example web_app/.env
nano web_app/.env
```

Then continue straight through `Ubuntu Server Start Here` in
[DEPLOYMENT.md](DEPLOYMENT.md). You do not need to run the shell files manually
first.

If you update this folder later, refresh the package-local dependencies before
restarting services:

```bash
cd ~/zone5_cv_time_features_package
python3 -m pip install --target .python-packages --upgrade -r requirements.txt
install -m 0644 systemd/user/zone5-*.service systemd/user/zone5-*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user restart zone5-person-counter.service
systemctl --user restart zone5-mqtt-aggregator.service
systemctl --user restart zone5-sen55-collector.service
systemctl --user restart zone5-live-collector.service
systemctl --user restart zone5-trainer.timer
systemctl --user restart zone5-live-app.service
```

## Manual Workflow Reference

Use this section only when running commands by hand. For the Ubuntu server,
start with `Ubuntu Server Start Here` in [DEPLOYMENT.md](DEPLOYMENT.md) instead.

0. Start the CV person-count publisher. It reads the Zone 5 RTSP camera with
`cv_counter/masks/cam1-desk5-mask.png`, `cv_counter/models/headtracker-m.pt`,
and `cv_counter/trackers/bytetrack.yaml`, draws the mask into
`data/yolo_latest.jpg`, and publishes counts to `care_ssl/zone5/person_count`.
The packaged mask is `cv_counter/masks/cam1-desk5-mask.png`.

```powershell
.\run_person_counter.ps1
```

```bash
bash run_person_counter.sh
```

This writes `data/person_counts.csv` and publishes MQTT messages for the CV
aggregator.

1. Collect CV labels from the person-count MQTT topic.

```powershell
python -m zone5.occupancy_mqtt_aggregator
```

```bash
python -m zone5.occupancy_mqtt_aggregator
```

This writes `data/cv_occupancy_zone5_10sec.csv` as buckets complete.

2. Collect SEN55 readings while the SEN55 device is publishing.

```powershell
.\run_sen55_collector.ps1
```

```bash
bash run_sen55_collector.sh
```

This buffers MQTT payloads into completed 10-second buckets, writes
`data/sen55_data.csv` inside this package. Numeric fields are averaged per
bucket, metadata uses the latest non-empty value, and payloads for
already-flushed buckets are skipped. The live collector and web app read the
live CSV by default.

3. Collect the wide Zone 5 sensor and label table.

```powershell
.\run_live_collector.ps1
```

```bash
bash run_live_collector.sh
```

Equivalent explicit command:

```powershell
python -m zone5.collect_training_data --live-append --duration-min 1440 --append-every-sec 10 --snapshot-refresh-every-hours 1
```

```bash
python -m zone5.collect_training_data --live-append --duration-min 1440 --append-every-sec 10 --snapshot-refresh-every-hours 1
```

For a fixed historical UTC window:

```powershell
python -m zone5.collect_training_data --time-start 2026-05-06T00:00:00Z --time-end 2026-05-07T00:00:00Z --require-cv-labels
```

```bash
python -m zone5.collect_training_data --time-start 2026-05-06T00:00:00Z --time-end 2026-05-07T00:00:00Z --require-cv-labels
```

The collector creates `data/` if needed and writes
`zone5_training_cv.csv` and `zone5_training_cv.metadata.json`.

In `--live-append` mode, CSV rows are appended every 10 seconds. Each cycle
refetches the latest 120 seconds by default so delayed SEN55 samples and CV
occupancy labels can backfill existing rows. Metadata is refreshed hourly.
Direct collector retraining is disabled by default and only runs when
`--retrain-after-snapshot` is passed. The deployed
systemd path keeps retraining separate: `zone5-trainer.timer` runs
`run_zone5_trainer.sh`, which takes `model/retrain.lock`, snapshots the live
CSV into `data/training_snapshots/`, trains from that immutable Parquet
snapshot, and then runs promotion without blocking live appends.

The default live retrain policy enables bootstrap fallback only while
`model/production_run.txt` is missing or empty, and progresses strict rolling
validation from 1 to 2 to 3 one-day folds as production matures. After a
successful retrain, promotion updates `model/production_run.txt` only when the
candidate passes. Use `--retrain-after-snapshot` only when you intentionally
want the collector process itself to train after metadata refreshes. Use
`--no-promote-after-retrain`/`PROMOTE_AFTER_RETRAIN=0` when you want retraining
without automatic production promotion.

CSV files are capped at 1 GB each. When a CSV reaches the cap, the package keeps
one physical CSV by dropping the oldest rows and retaining the newest rows that
fit under the cap. Override the byte limit only when needed with
`ZONE5_MAX_CSV_BYTES`.

4. Train and promote a CV-target model.

```powershell
.\train_model.ps1
```

```bash
bash train_model.sh
```

The wrappers call `python -m zone5.training` and train from
`data/zone5_training_cv.csv`. Direct Parquet training is still accepted for
manual immutable snapshots. Training creates `model/runs/<run_id>/` and
`model/current_run.txt`; promotion writes `model/production_run.txt`. Fresh
packages do not include either pointer until those real steps run.

By default, training keeps the latest 1 calendar day as the hidden/blind test.
Strict training uses rolling 1-day validation folds from the earlier days, using
fewer than 3 folds when the pre-test history is short. With exactly 2 calendar
days of labeled data, the first day is split chronologically for emergency
train/validation and the second day remains hidden. With fewer than 2 calendar
days, training fails because at least 2 calendar days are required for a true
1-day hidden test.

`--bootstrap-fallback` is a first-model escape hatch. Training first tries the
strict split above. If strict validation has no viable lookbacks, bootstrap
fallback keeps the same hidden test day and uses one chronological pre-test fold:
the first 80% for training and the last 20% for validation. Bootstrap still
rejects single-class training windows, and promotion accepts a bootstrap
candidate only when no production pointer exists yet. After
`model/production_run.txt` is created, replacement models must come from strict
validation. Use `--cv-folds 1`, `--cv-folds 2`, or `--cv-folds 3` to request
the strict rolling fold count explicitly. Live retraining defaults to
`--retrain-cv-folds auto`, which trains 1 fold before first strict production,
then requires 2 folds for the next replacement, then 3 folds after that. A
strict 1-fold candidate may replace the first strict 1-fold production model
only when both are `rolling_calendar` runs and the candidate's stored blind-test
PR-AUC is greater than production's stored blind-test PR-AUC plus
`--min-pr-auc-delta`; the next auto retrain still requests 2 folds.
`--allow-degenerate-validation` is separate and remains a smoke/dev option for
allowing single-class validation windows; it still does not allow single-class
training windows, and promotion rejects degenerate-validation candidates.

Quick training smoke:

```powershell
.\train_model.ps1 -NTrials 1 -MaxEpochs 1 -OutputDir .test_runtime\train_smoke -CvFolds 1 -BootstrapFallback -AllowDegenerateValidation -SkipPromote
```

```bash
ZONE5_SKIP_PROMOTE=1 bash train_model.sh --n-trials 1 --max-epochs 1 --optuna-jobs 1 --output-dir .test_runtime/train_smoke --cv-folds 1 --bootstrap-fallback --allow-degenerate-validation
```

5. Run the app after `model/production_run.txt` points to a promoted run. Before
promotion, the app stays up but reports that the production pointer is missing.

```powershell
.\run_live_app.ps1
```

```bash
bash run_live_app.sh
```

Replay mode uses the generated CV training CSV by default:

```powershell
.\run_replay_app.ps1
```

```bash
bash run_replay_app.sh
```

Open `http://127.0.0.1:8000/`. On Ubuntu, set `HOST=0.0.0.0 PORT=8000` before
the app wrapper when it should listen outside localhost.

## Persistent Deployment

You can start persistent collection immediately after install. Keep these data
processes running from day 0:

```text
run_person_counter.ps1/.sh       -> MQTT care_ssl/zone5/person_count
zone5.occupancy_mqtt_aggregator  -> data/cv_occupancy_zone5_10sec.csv
zone5.sen55_mqtt_collector       -> data/sen55_data.csv
zone5.collect_training_data      -> data/zone5_training_cv.csv + metadata
```

On Ubuntu systemd, also keep `zone5-trainer.timer` enabled. The live collector
does not train inline in that path; the timer runs `zone5-trainer.service`
hourly after boot. Early retraining attempts can skip or fail and that is
expected: the trainer needs enough labeled blind-test evidence, at least two
coverage-eligible pre-test dates for strict rolling validation, and usable
occupied and unoccupied examples in each relevant train/validation window.
While no production pointer exists, the default bootstrap policy can use the
first-model bootstrap split if strict validation has no viable lookbacks. The
services keep collecting data after retraining or promotion failures.

The first successful hourly retrain writes `model/current_run.txt`, then
automatic promotion runs. If the candidate passes the existing promotion gates,
`model/production_run.txt` is updated and the live app reloads it on its pointer
poll interval. If the candidate is too weak or does not beat the current
production model, promotion is skipped or failed in the log and data collection
continues.

Production CI/CD for split training and web-app servers is documented in
`CICD_MODEL_DELIVERY.md`. Model weights stay out of git; CI/CD packages a
promoted `model/runs/<run_id>/` artifact, validates it on the web app server,
then flips that server's `model/production_run.txt` pointer.

Useful checks for training and automatic promotion:

```powershell
Get-ChildItem model\runs, model\current_run.txt
Get-Content logs\live_collector.log -Tail 80
Get-Content logs\zone5_trainer.log -Tail 80
```

```bash
ls -lah model/runs model/current_run.txt
tail -n 80 logs/live_collector.log
tail -n 80 logs/zone5_trainer.log
```

Keep the app running if the dashboard should stay available. If the app is
started before the first successful automatic promotion, it stays up but
reports that the production pointer is missing:

```powershell
.\run_live_app.ps1
```

```bash
bash run_live_app.sh
```

The exact persistent deployment commands for Ubuntu systemd and the legacy
manual Windows path are in [DEPLOYMENT.md](DEPLOYMENT.md). Docker Compose
deployment is covered in [DOCKER.md](DOCKER.md).

## Data Contract

The model target is `zone_occupied`, derived only from CV person-count labels.
CV/person-count columns are audit and display fields, not model inputs.

Model raw inputs are `temp_s5`, `rh_s5`, `co2_s5`, `pm25_s5`, `power_s5`,
`mmwave_s5`, the SEN55 particulate/environment fields, one `*_missing` channel
per raw feature, and engineered time features. The shared feature builder is
used by both the live collector and web app. SEN55 is read from
`data/sen55_data.csv` by default. If the SEN55 table is missing, SEN55 raw
fields remain null and the model uses training medians plus missing flags.
For `mmwave_s5`, each mapped MSR-2 device is treated as occupied when any
internal radar region `zone_1_occupancy`, `zone_2_occupancy`, or
`zone_3_occupancy` is occupied.

Rows with missing sensor values or missing CV labels are kept in saved tables.
Training skips windows whose final `zone_occupied` label is null.

## Commands

### Wrapper Defaults And Overrides

PowerShell wrappers take named parameters. Bash wrappers use environment
variables, except `train_model.sh`, which passes CLI flags through to
`python -m zone5.training`. The PowerShell rows are retained for legacy/manual
Windows runs only; Ubuntu systemd uses the bash/Python rows and the bundled
unit files under `systemd/user/`.

| Wrapper | Defaults | Common overrides |
| --- | --- | --- |
| `run_person_counter.ps1` | `-Source` uses `ZONE5_RTSP_URL` or `rtsp://admin:++smartilab2023@10.158.71.241:554/Streaming/channels/101`; `-Mask cv_counter\masks\cam1-desk5-mask.png`; `-ShowMask true`; `-Tracking true`; `-Tracker cv_counter\trackers\bytetrack.yaml`; `-Imgsz 256`; `-Device cpu`; `-CountsCsv data\person_counts.csv`; `-MqttBroker 10.158.71.19`; `-MqttPort 1883`; `-MqttTopic care_ssl/zone5/person_count`; `-MqttUsername guest`; `-MqttPassword smartilab123`; `-MqttEvery 1`; `-CountsEvery 1`; package-local `-Script`, `-Model`. | `.\run_person_counter.ps1 -Device 0 -CountsEvery 5 -MqttEvery 5` |
| `run_person_counter.sh` | Same values via `ZONE5_RTSP_URL`/`SOURCE`, `DEVICE`, `COUNTS_CSV`, `PERSON_COUNT_MQTT_*` or `MQTT_*`, `MQTT_EVERY`, `COUNTS_EVERY`, `SCRIPT`, `MASK`, `MODEL`; `TRACKER=cv_counter/trackers/bytetrack.yaml`; `PERSON_COUNT_SHOW_MASK=1` draws the mask into `data/yolo_latest.jpg`; `PERSON_COUNT_TRACKING=1` uses ByteTrack by default; `PERSON_COUNT_IMGSZ=256` by default. | `DEVICE=0 COUNTS_EVERY=5 MQTT_EVERY=5 bash run_person_counter.sh` |
| `run_sen55_collector.ps1` | `-OutputCsv data\sen55_data.csv`. | `.\run_sen55_collector.ps1 -OutputCsv "data\sen55_data.csv"` |
| `run_sen55_collector.sh` | `OUTPUT_CSV=data/sen55_data.csv`. | `OUTPUT_CSV=data/sen55_data.csv bash run_sen55_collector.sh` |
| `run_live_collector.ps1` | `-DurationMin 1440`; `-AppendEverySec 10`; `-BackfillSec 120`; `-SnapshotRefreshEveryHours 1`; inline retraining is off unless `-RetrainAfterSnapshot` is set; AIR-1 defaults are set if missing. | `.\run_live_collector.ps1 -DurationMin 120 -BackfillSec 300 -SnapshotRefreshEveryHours 0.5 -NoPromoteAfterRetrain` |
| `run_live_collector.sh` | `DURATION_MIN=1440`; `APPEND_EVERY_SEC=10`; `BACKFILL_SEC=120`; `SNAPSHOT_REFRESH_EVERY_HOURS=1`; `RETRAIN_AFTER_SNAPSHOT=0`; `PROMOTE_AFTER_RETRAIN=0`; AIR-1 defaults are exported if missing. | `DURATION_MIN=120 BACKFILL_SEC=300 SNAPSHOT_REFRESH_EVERY_HOURS=0.5 bash run_live_collector.sh` |
| `run_zone5_trainer.sh` | Snapshots `data/zone5_training_cv.csv` by default into `data/training_snapshots/`; uses `model/retrain.lock`; `RETRAIN_N_TRIALS=50`; `RETRAIN_MAX_EPOCHS=20`; `RETRAIN_OPTUNA_JOBS=1`; `PROMOTE_AFTER_RETRAIN=1`; `RETRAIN_CV_FOLDS=auto`. | `RETRAIN_N_TRIALS=1 RETRAIN_MAX_EPOCHS=1 PROMOTE_AFTER_RETRAIN=0 bash run_zone5_trainer.sh` |
| `train_model.ps1` | `-NTrials 50`; `-MaxEpochs 20`; `-OptunaJobs 0` means auto; `-OutputDir model`; promotes unless `-SkipPromote`. | `.\train_model.ps1 -NTrials 20 -MaxEpochs 10 -SkipPromote` |
| `train_model.sh` | Calls `python -m zone5.training --csv data/zone5_training_cv.csv`; auto-promotes when output dir is `model` unless `ZONE5_SKIP_PROMOTE=1`. | `ZONE5_SKIP_PROMOTE=1 bash train_model.sh --n-trials 20 --max-epochs 10` |
| `run_live_app.ps1` | `-HostAddress 127.0.0.1`; `-Port 8000`; `ZONE5_DATA_SOURCE=live`; `ZONE5_PRODUCTION_POINTER=model\production_run.txt`. | `.\run_live_app.ps1 -HostAddress 0.0.0.0 -Port 8000` |
| `run_live_app.sh` | `HOST=127.0.0.1`; `PORT=8000`; `ZONE5_DATA_SOURCE=live`; `ZONE5_PRODUCTION_POINTER=model/production_run.txt`; `ZONE5_MJPEG_TARGET_FPS=15`. | `HOST=0.0.0.0 PORT=8000 bash run_live_app.sh` |
| `run_replay_app.ps1` / `.sh` | Same host/port defaults, but forces replay mode, `ZONE5_REPLAY_TABLE=data/zone5_training_cv.csv`, `ZONE5_MAX_AGE_MINUTES=none`, and `ZONE5_TICK_INTERVAL_SEC=1`. | `HOST=0.0.0.0 PORT=8001 bash run_replay_app.sh` |

### Direct Python Flags

Use direct Python commands when you need options that wrappers do not expose.

`python -m zone5.collect_training_data` accepts:
`--duration-min` (default `240`, wrapper uses `1440`), `--time-start`,
`--time-end`, `--sen55-table`, `--cv-labels`, `--output-csv`,
`--metadata`, `--occupied-threshold`,
`--require-cv-labels`, `--live-append`, `--append-every-sec`,
`--backfill-sec`, `--snapshot-refresh-every-hours`, `--retrain-after-snapshot`,
`--no-retrain-after-snapshot`, `--retrain-output-dir`,
`--retrain-n-trials`, `--retrain-optuna-jobs`, `--retrain-max-epochs`,
`--retrain-seed`, `--retrain-allow-bad-lines`,
`--retrain-allow-degenerate-validation`, `--retrain-bootstrap-fallback`
(`auto`, `always`, `never`; default `auto`), `--retrain-cv-folds`
(`auto`, `1`, `2`, `3`; default `auto`), `--promote-after-retrain`,
`--no-promote-after-retrain`, `--production-pointer`,
`--promote-skip-smoke`, `--promote-skip-non-regression-smoke`,
`--min-positive-windows`, `--min-positive-buckets`,
`--min-positive-events`, `--min-strict-date-coverage`, `--chunk-days`,
`--min-chunk-hours`, `--api-timeout`, `--api-retries`, `--max-workers`,
`--progress-every`, `--verbose-progress`, `--timing-summary`, and
`--no-timing-summary`.

`python -m zone5.occupancy_mqtt_aggregator` accepts:
`--mqtt-broker`, `--mqtt-port`, `--mqtt-topic`, `--mqtt-username`,
`--mqtt-password`, `--mqtt-client-id`, `--output-csv`, `--aggregate`
(`max`, `last`, `mean`, `median`; default `median`),
`--occupied-threshold` (default `1`), `--late-grace-seconds` (default `5`),
`--flush-check-seconds` (default `1`), and `--use-receive-time`.

`python -m zone5.sen55_mqtt_collector` accepts:
`--mqtt-broker`, `--mqtt-port`, `--mqtt-topic`, `--mqtt-username`,
`--mqtt-password`, `--mqtt-client-id`, `--output-csv`, and
`--flush-check-seconds` (default `1`).

`python -m zone5.training` accepts:
`--csv`, `--parquet`, `--output-dir` (default `model`), `--n-trials`
(default `50`), `--optuna-jobs`, `--max-epochs` (default `20`), `--seed`
(default `42`), `--cv-folds` (`1`, `2`, `3`; default `3`),
`--allow-bad-lines`, `--allow-degenerate-validation`, `--bootstrap-fallback`,
and `--min-strict-date-coverage`.

`python -m zone5.promote_model` accepts:
`--candidate-run` (default `model`), `--production-pointer` (default
`model/production_run.txt`), `--skip-smoke`, `--skip-non-regression-smoke`,
`--min-test-pr-auc` (default `0`), `--min-mean-cv-pr-auc` (default `0`),
`--min-pr-auc-delta` (default `0`), `--min-positive-windows` (default `5`),
`--min-positive-buckets` (default `5`), `--min-positive-events` (default `1`),
`--max-test-brier` (default `1.0`), and `--max-test-log-loss` (default
`10.0`).

`python smoke_test.py` accepts:
`--candidate-run`, `--production-pointer`, `--min-test-roc-auc`,
`--min-test-pr-auc`, `--min-positive-windows`, `--min-positive-buckets`,
`--min-positive-events`, `--max-test-brier`, `--max-test-log-loss`,
`--max-regression`, `--skip-non-regression`, `--fixture-csv`,
`--fixture-parquet`, and
`--json`.

```powershell
python -m zone5.collect_training_data --help
python -m zone5.sen55_mqtt_collector --help
python -m zone5.occupancy_mqtt_aggregator --help
python -m zone5.training --help
python -m zone5.promote_model --help
python -m uvicorn web_app.main:app --help
python smoke_test.py --help
```

```bash
python -m zone5.collect_training_data --help
python -m zone5.sen55_mqtt_collector --help
python -m zone5.occupancy_mqtt_aggregator --help
python -m zone5.training --help
python -m zone5.promote_model --help
python -m uvicorn web_app.main:app --help
python smoke_test.py --help
```

## Verification

```powershell
python -m compileall zone5 web_app smoke_test.py
python -m unittest discover -s tests -p "test*.py" -v
```

```bash
python -m compileall zone5 web_app smoke_test.py
python -m unittest discover -s tests -p "test*.py" -v
```
