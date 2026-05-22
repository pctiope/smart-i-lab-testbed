# AIR-1 All-Zones CV Time-Features Package

This package trains and serves one shared CNN over all 15 AIR-1 zones. Training
rows are long-form: one row per `timestamp` and `zone_id`. The model feature
vector uses AIR-1, shared SEN55, missing indicators, and time features only;
`zone_id` is retained for grouping, audit, splits, windows, and display, but is
not a model input.

The CV labels are mask-based per-zone labels. Run one tracker per camera:
`cam1` uses `cam1-zones.json` plus `masks/cam1-mask-zones.png`, and `cam2` uses
`cam2-zones.json` plus `masks/cam2-mask-zones.png`. Both publish to
`care_ssl/all_zones/person_count_by_zone`.

The live web app is an all-zones operations view. It starts two independent
RTSP grabbers, exposes `/api/video/cam1.mjpg` and `/api/video/cam2.mjpg`, keeps
`/api/video.mjpg` as a cam1 compatibility alias, and shows the shared model
output alongside aggregate and per-zone CV labels.

## Install

PowerShell, from this package root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Ubuntu without a venv, from this package root. Dependencies are installed into
the package-local `.python-packages/` directory so they do not conflict with
other user Python tools. The person counter needs the Ultralytics BoT-SORT
dependency `lap`, and it is included in `requirements.txt`:

```bash
python3 -m pip install --upgrade pip
python3 -m pip install --target .python-packages --upgrade -r requirements.txt
```

When running direct Python commands without the `.sh` wrappers or systemd, set
`PYTHONPATH` first:

```bash
export PYTHONPATH="$PWD/.python-packages:$PWD"
```

## Docker And Deployment

Use [DOCKER.md](DOCKER.md) for the Docker Compose workflow. The stack includes
two person-counter services, the per-zone MQTT aggregator, SEN55 collector,
live collector, live app, replay app, and ops jobs for training, promotion,
smoke tests, compile checks, and unit tests.

Quick start from this package root:

```bash
[ -f web_app/.env ] || cp web_app/.env.example web_app/.env
docker compose build
docker compose up -d
```

Open `http://localhost:8000/`. Runtime state is bind-mounted from the host under
`data/`, `model/`, and `logs/`.

The dashboard should show:

- cam1 status and feed for host `10.158.71.241`, covering Tables 4 and 9-15;
- cam2 status and feed for host `10.158.71.240`, covering Tables 1-3 and 5-8;
- max and mean probability from the shared all-zones model;
- zones above threshold;
- aggregate CV count;
- per-zone CV labels with nulls preserved when a zone has no current label.

Use [DEPLOYMENT.md](DEPLOYMENT.md) for persistent Windows Task Scheduler and
Ubuntu systemd commands. [PIPELINE.md](PIPELINE.md) explains the all-zones
collection, training, promotion, and missing-production-model troubleshooting
flow. CI/CD examples are in
[CICD_SYSTEMD.md](CICD_SYSTEMD.md) and
[CICD_DOCKER_COMPOSE.md](CICD_DOCKER_COMPOSE.md).

## Manual Workflow Reference

Use this section only when running commands by hand. For a persistent Ubuntu
server, start with `Ubuntu Server Start Here` in [DEPLOYMENT.md](DEPLOYMENT.md)
instead.

1. Start both CV person-count publishers:

```powershell
.\run_person_counter.ps1 -Camera cam1
.\run_person_counter.ps1 -Camera cam2
```

```bash
CAMERA=cam1 bash run_person_counter.sh
CAMERA=cam2 bash run_person_counter.sh
```

These write `data/person_counts_by_zone_cam1.csv` and
`data/person_counts_by_zone_cam2.csv`, then publish per-zone MQTT payloads.
`Table N` maps to `zone_id=N` for Tables 1-15. Table 16 is intentionally
excluded/unlabeled until the model contract adds a zone 16.

2. Collect per-zone CV labels from MQTT:

```powershell
python -m air1_all_zones.occupancy_mqtt_aggregator
```

```bash
python -m air1_all_zones.occupancy_mqtt_aggregator
```

This writes `data/cv_occupancy_all_air1_10sec.csv` as buckets complete and
rebuilds `data/cv_occupancy_all_air1_10sec.parquet` when missing, then on the
hourly snapshot cadence.

3. Collect SEN55 readings while the SEN55 device is publishing:

```powershell
.\run_sen55_collector.ps1
```

```bash
bash run_sen55_collector.sh
```

This buffers MQTT payloads into completed 10-second buckets, writes
`data/sen55_data.csv`, and rebuilds `data/sen55_data.parquet`. Numeric fields
are averaged per bucket, metadata uses the latest non-empty value, and payloads
for already-flushed buckets are skipped.

4. Run the live all-zones collector:

```powershell
.\run_live_collector.ps1
```

```bash
bash run_live_collector.sh
```

Equivalent explicit command:

```powershell
python -m air1_all_zones.collect_training_data --live-append --duration-min 1440 --append-every-sec 10 --parquet-rebuild-every-hours 1
```

In `--live-append` mode, CSV rows are appended every 10 seconds. Each cycle
refetches the latest 120 seconds by default so delayed SEN55 samples and
per-zone CV labels can backfill existing `timestamp + zone_id` rows. The joined
Parquet snapshot is rebuilt hourly or when missing. Retraining is disabled by
default. The preferred production path is the separate trainer below; inline
collector retraining remains opt-in with `RETRAIN_AFTER_PARQUET=1` and uses the
same progressive CV fold policy when enabled.

5. Rebuild the joined Parquet from existing feature and label files:

```powershell
python -m air1_all_zones.build_cv_training_data
```

```bash
python -m air1_all_zones.build_cv_training_data
```

6. Train and promote a CV-target all-zones model:

```powershell
.\train_model.ps1
```

```bash
bash run_air1_all_zones_trainer.sh
```

The trainer snapshots the current CSV or Parquet under
`data/training_snapshots/`, holds `model/retrain.lock`, writes
`model/retrain_status.json`, trains into `model/runs/<run_id>/`, and promotes
only after the candidate passes the safety gates. `RETRAIN_CV_FOLDS=auto`
starts first production candidates at one strict fold, then progresses
replacement candidates to two and three folds as production matures.
`RETRAIN_BOOTSTRAP_FALLBACK=auto` can use a first-model chronological fallback
only while `model/production_run.txt` is missing or empty.

For a fast non-promoting trainer smoke pass:

```bash
RETRAIN_N_TRIALS=1 RETRAIN_MAX_EPOCHS=1 PROMOTE_AFTER_RETRAIN=0 bash run_air1_all_zones_trainer.sh
```

The older `train_model.sh` remains a manual direct trainer. Fresh packages do
not include `model/current_run.txt` or `model/production_run.txt` until real
training and promotion run.

7. Run the app:

```powershell
.\run_live_app.ps1
```

```bash
bash run_live_app.sh
```

Replay mode uses the generated CV training Parquet:

```powershell
.\run_replay_app.ps1
```

```bash
bash run_replay_app.sh
```

Important app endpoints:

```text
GET /api/health
GET /api/current
GET /api/history
GET /api/video/cam1.mjpg
GET /api/video/cam2.mjpg
GET /api/video.mjpg
```

`/api/health` returns `rtsp_by_camera` for cam1 and cam2 and still returns
legacy `rtsp` as cam1. Current/history events include `zone_probabilities`,
`aggregate_probability`, `occupied_zones`, `zone_count`, aggregate CV fields,
and `ground_truth_by_zone` for zones 1-15 only. MJPEG stream FPS defaults to
`AIR1_ALL_ZONES_MJPEG_TARGET_FPS=15` and is reported as
`config.mjpeg_target_fps` in `/api/health`.

## Data Contract

- Expected label rows are keyed by `timestamp + zone_id`.
- Valid model zones are `1-15`; Table 16 is excluded/unlabeled.
- Masks are the operational source of per-zone labels. JSON polygons are
  optional editing aids/fallbacks.
- Missing or unmapped zone labels stay null, not false zero.
- `occupied = 1` when the per-zone median `occupancy_count` is at least 1.
- Windows are built within each `zone_id` only.
- Train/validation/test splits are grouped chronologically by timestamp so one
  timestamp cannot cross splits.
- Web-app payloads also use zones `1-15` only; Table 16 must not appear in
  model probabilities or `ground_truth_by_zone`.

Default outputs:

```text
data/cv_occupancy_all_air1_10sec.csv
data/cv_occupancy_all_air1_10sec.parquet
data/air1_all_zones_training_cv.csv
data/air1_all_zones_training_cv.parquet
data/air1_all_zones_training_cv.metadata.json
model/production_run.txt
```

CSV files are capped at 1 GB each. When a CSV reaches the cap, the package keeps
one physical CSV by dropping the oldest rows and retaining the newest rows that
fit under the cap. Override the byte limit only when needed with
`AIR1_ALL_ZONES_MAX_CSV_BYTES`.
