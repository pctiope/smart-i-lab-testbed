# AIR-1 All-Zones Deployment Runbook

This runbook keeps the live AIR-1 all-zones services running persistently. The
runtime pipeline is:

```text
cam1 RTSP -> rtsp_zone_tracker.py -> care_ssl/all_zones/person_count_by_zone
cam2 RTSP -> rtsp_zone_tracker.py -> care_ssl/all_zones/person_count_by_zone
per-zone MQTT -> air1_all_zones.occupancy_mqtt_aggregator -> cv_occupancy_all_air1_10sec.*
SEN55 MQTT -> air1_all_zones.sen55_mqtt_collector -> sen55_data.*
AIR-1 API + SEN55 + per-zone CV labels -> collect_training_data -> air1_all_zones_training_cv.*
optional retraining + promotion -> model/production_run.txt
FastAPI app -> cam1/cam2 feeds + shared model probabilities + per-zone CV labels for zones 1-15
```

Runtime state is generated under `data/`, `model/`, and `logs/`. Do not expect
a fresh package to include real training data or a promoted model pointer.

## Ubuntu Server Start Here

From the server:

```bash
cd ~/air1_all_zones_cv_time_features_package
python3 -m pip install --upgrade pip
python3 -m pip install --target .python-packages --upgrade -r requirements.txt
mkdir -p data model logs ~/.config/systemd/user
[ -f web_app/.env ] || cp web_app/.env.example web_app/.env
nano web_app/.env
```

Set real values for:

```env
AIR1_API_URL=...
AIR1_API_KEY=...
AIR1_ALL_ZONES_RTSP_URL_CAM1=rtsp://admin:<password>@10.158.71.241:554/Streaming/channels/101
AIR1_ALL_ZONES_RTSP_URL_CAM2=rtsp://admin:<password>@10.158.71.240:554/Streaming/channels/101
PERSON_COUNT_MQTT_BROKER=...
PERSON_COUNT_MQTT_TOPIC=care_ssl/all_zones/person_count_by_zone
OCCUPANCY_MQTT_TOPIC=care_ssl/all_zones/person_count_by_zone
SEN55_MQTT_TOPIC=sen55_01/data
AIR1_ALL_ZONES_MJPEG_TARGET_FPS=15
RETRAIN_AFTER_PARQUET=0
PROMOTE_AFTER_RETRAIN=0
```

The live collector collects CSV rows and rebuilds
`data/air1_all_zones_training_cv.parquet` by default. Set
`RETRAIN_AFTER_PARQUET=1` only for explicit inline retraining. The default
systemd training path is the separate `air1-all-zones-trainer.service` and
optional `air1-all-zones-trainer.timer`; enabling the timer is an operator
action, not a deployment side effect.

Enable lingering so user services continue after SSH disconnects:

```bash
loginctl enable-linger "$USER"
```

If that needs privileges:

```bash
sudo loginctl enable-linger "$USER"
```

## User Systemd Services

Repo-local templates live under `systemd/user/`. Copy them into
`~/.config/systemd/user/`, or use the inline examples below. Replace the
`WorkingDirectory` path if your package lives elsewhere.

`air1-all-zones-person-counter-cam1.service`:

```ini
[Unit]
Description=AIR-1 all-zones cam1 person counter
After=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/air1_all_zones_cv_time_features_package
EnvironmentFile=%h/air1_all_zones_cv_time_features_package/web_app/.env
Environment=PYTHONPATH=%h/air1_all_zones_cv_time_features_package/.python-packages:%h/air1_all_zones_cv_time_features_package
Environment=PYTHON_BIN=python3
Environment=PERSON_COUNT_CAMERA_ID=cam1
ExecStart=/usr/bin/bash run_person_counter.sh
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

`air1-all-zones-person-counter-cam2.service`:

```ini
[Unit]
Description=AIR-1 all-zones cam2 person counter
After=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/air1_all_zones_cv_time_features_package
EnvironmentFile=%h/air1_all_zones_cv_time_features_package/web_app/.env
Environment=PYTHONPATH=%h/air1_all_zones_cv_time_features_package/.python-packages:%h/air1_all_zones_cv_time_features_package
Environment=PYTHON_BIN=python3
Environment=PERSON_COUNT_CAMERA_ID=cam2
ExecStart=/usr/bin/bash run_person_counter.sh
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

`air1-all-zones-mqtt-aggregator.service`:

```ini
[Unit]
Description=AIR-1 all-zones per-zone CV label aggregator
After=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/air1_all_zones_cv_time_features_package
EnvironmentFile=%h/air1_all_zones_cv_time_features_package/web_app/.env
Environment=PYTHONPATH=%h/air1_all_zones_cv_time_features_package/.python-packages:%h/air1_all_zones_cv_time_features_package
ExecStart=/usr/bin/python3 -m air1_all_zones.occupancy_mqtt_aggregator
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

`air1-all-zones-sen55-collector.service`:

```ini
[Unit]
Description=SEN55 table collector
After=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/air1_all_zones_cv_time_features_package
EnvironmentFile=%h/air1_all_zones_cv_time_features_package/web_app/.env
Environment=PYTHONPATH=%h/air1_all_zones_cv_time_features_package/.python-packages:%h/air1_all_zones_cv_time_features_package
ExecStart=/usr/bin/bash run_sen55_collector.sh
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

`air1-all-zones-live-collector.service`:

```ini
[Unit]
Description=AIR-1 all-zones live training collector
After=network-online.target air1-all-zones-mqtt-aggregator.service air1-all-zones-sen55-collector.service

[Service]
Type=simple
WorkingDirectory=%h/air1_all_zones_cv_time_features_package
EnvironmentFile=%h/air1_all_zones_cv_time_features_package/web_app/.env
Environment=PYTHONPATH=%h/air1_all_zones_cv_time_features_package/.python-packages:%h/air1_all_zones_cv_time_features_package
ExecStart=/usr/bin/bash run_live_collector.sh
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
```

`air1-all-zones-live-app.service`:

```ini
[Unit]
Description=AIR-1 all-zones live occupancy app
After=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/air1_all_zones_cv_time_features_package
EnvironmentFile=%h/air1_all_zones_cv_time_features_package/web_app/.env
Environment=PYTHONPATH=%h/air1_all_zones_cv_time_features_package/.python-packages:%h/air1_all_zones_cv_time_features_package
Environment=HOST=0.0.0.0
Environment=PORT=8000
ExecStart=/usr/bin/bash run_live_app.sh
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

Start them:

```bash
systemctl --user daemon-reload
systemctl --user enable --now air1-all-zones-person-counter-cam1.service
systemctl --user enable --now air1-all-zones-person-counter-cam2.service
systemctl --user enable --now air1-all-zones-mqtt-aggregator.service
systemctl --user enable --now air1-all-zones-sen55-collector.service
systemctl --user enable --now air1-all-zones-live-collector.service
systemctl --user enable --now air1-all-zones-live-app.service
```

Run a one-off non-promoting trainer smoke pass:

```bash
systemctl --user start air1-all-zones-trainer.service
journalctl --user -u air1-all-zones-trainer.service -n 100 --no-pager
```

For a faster dry run, invoke the launcher directly:

```bash
RETRAIN_N_TRIALS=1 RETRAIN_MAX_EPOCHS=1 PROMOTE_AFTER_RETRAIN=0 bash run_air1_all_zones_trainer.sh
```

Enable the scheduled trainer only after the live collector is producing stable
CSV/Parquet data:

```bash
systemctl --user enable --now air1-all-zones-trainer.timer
```

## Checks

```bash
systemctl --user status air1-all-zones-live-app.service
journalctl --user -u air1-all-zones-live-collector.service -f
curl -fsS http://127.0.0.1:8000/api/health
ls -lh data/cv_occupancy_all_air1_10sec.csv data/air1_all_zones_training_cv.parquet
```

The app may be healthy while reporting `production pointer missing`. That is
expected until enough real labeled data exists, training succeeds, and promotion
creates `model/production_run.txt`.

`/api/health` should include:

- `rtsp_by_camera.cam1` for host `10.158.71.241`;
- `rtsp_by_camera.cam2` for host `10.158.71.240`;
- legacy `rtsp` as the cam1 status;
- `config.mjpeg_target_fps`, defaulting to `15`;
- inference status with the current model pointer and last event.

Current/history events should include `zone_probabilities`,
`aggregate_probability`, `occupied_zones`, aggregate CV fields, and
`ground_truth_by_zone` for zones 1-15 only.

## Windows Task Scheduler

Use the `.ps1` wrappers as scheduled actions from this package root:

```powershell
.\run_person_counter.ps1 -Camera cam1
.\run_person_counter.ps1 -Camera cam2
python -m air1_all_zones.occupancy_mqtt_aggregator
.\run_sen55_collector.ps1
.\run_live_collector.ps1
.\run_live_app.ps1
```

Set the task working directory to the package root and load `web_app\.env`
values into the task environment or system environment.

## Cadence

- CV label CSV rows are flushed after completed 10-second buckets.
- CV label Parquet rebuilds when missing, hourly, and on shutdown.
- SEN55 CSV rows are flushed by completed 10-second buckets.
- SEN55 Parquet rebuilds when missing, hourly, and on shutdown.
- Training CSV appends every 10 seconds in live mode.
- Training Parquet/metadata rebuild hourly or when missing.
- Retraining is opt-in with `RETRAIN_AFTER_PARQUET=1`.
- Promotion is opt-in with `RETRAIN_AFTER_PARQUET=1` plus `PROMOTE_AFTER_RETRAIN=1`.

## Important Data Rules

- The training join key is `timestamp + zone_id`.
- Zones are `1-15`. Table 16 is visible in camera data but excluded/unlabeled.
- Missing per-zone labels remain null. They are not converted to false zeros.
- Masks are the operational per-zone label source; JSON polygons are editing
  aids/fallbacks.

## Dashboard Readiness Check

Before calling the web app operational, open `http://127.0.0.1:8000/` and
confirm:

- cam1 and cam2 feeds render independently;
- cam2 status is visible enough that a stopped cam2 stream cannot be missed;
- the zone grid is grouped by camera coverage;
- selecting a zone shows camera source, probability, CV count, occupied/clear
  label, label age, and label timestamp;
- Table 16 is not shown as a model zone.
