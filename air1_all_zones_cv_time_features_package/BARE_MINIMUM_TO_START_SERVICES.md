# Bare Minimum To Start Services

## Bottom Line

The service wrappers, Docker Compose file, two-camera web app routes, and
per-zone label contracts are present. This checkout is still not operationally
ready as a full all-zones service stack until real data and a promoted model
exist.

The bare minimum still lacking is:

- install the missing person-counter Python packages;
- choose PowerShell scripts or Docker Compose as the launch path;
- confirm the lab RTSP, MQTT, SEN55, and AIR-1 API endpoints are reachable;
- start collection services long enough to create real `data/` files;
- start both camera trackers so mask-based per-zone CV labels are produced;
- train and promote one model before expecting the live app to produce predictions.

## Current On-Disk Status

Already present:

- `run_person_counter.ps1`
- `run_sen55_collector.ps1`
- `run_live_collector.ps1`
- `run_live_app.ps1`
- `docker-compose.yml`
- `cv_counter/models/headtracker-m.pt`
- `cv_counter/trackers/botsort.yaml`
- `web_app/.env`
- `web_app/.env.example`

Still placeholder-only:

- `data/`
- `model/`

Missing artifacts right now:

- `data/cv_occupancy_all_air1_10sec.csv`
- `data/cv_occupancy_all_air1_10sec.parquet`
- `data/sen55_data.csv`
- `data/sen55_data.parquet`
- `data/air1_all_zones_training_cv.csv`
- `data/air1_all_zones_training_cv.parquet`
- `model/production_run.txt`

Target signal path now present:

- `rtsp_zone_tracker.py` publishes mask-based per-zone labels from `cam1-zones.json` / `cam2-zones.json` and `masks/*-mask-zones.png`;
- labels are joined by `timestamp + zone_id`;
- Table 16 is excluded/unlabeled because the model contract covers only AIR-1 zones 1-15.
- the web app exposes `/api/video/cam1.mjpg` and `/api/video/cam2.mjpg`, keeps
  `/api/video.mjpg` as a cam1 compatibility alias, and reports both cameras in
  `/api/health` under `rtsp_by_camera` plus `config.mjpeg_target_fps`;
- inference events expose aggregate CV fields and `ground_truth_by_zone` for
  zones 1-15, with missing labels left null.

## Bare Minimum Missing Before Starting Collectors

You need both camera trackers and the MQTT aggregator running before claiming the model is learning per-zone occupancy. Missing camera coverage should remain null in the label table, not be converted to zero.

### 1. Install Runtime Dependencies

The current Python can run the unit tests, but a runtime import check showed these packages are missing:

- `ultralytics`
- `lap`
- `filterpy`

Install the package requirements first:

```powershell
Set-Location .\air1_all_zones_cv_time_features_package
python -m pip install --upgrade -r requirements.txt
```

Then confirm the person-counter dependencies import:

```powershell
python -c "import ultralytics, lap, filterpy; print('person counter deps ok')"
```

### 2. Confirm Service Credentials Are Set

For local PowerShell launch, the wrappers have lab defaults, but you should still confirm these variables match the current lab setup before starting anything:

- `AIR1_ALL_ZONES_RTSP_URL_CAM1` for camera host `10.158.71.241`
- `AIR1_ALL_ZONES_RTSP_URL_CAM2` for camera host `10.158.71.240`
- `PERSON_COUNT_MQTT_BROKER`
- `PERSON_COUNT_MQTT_PORT`
- `PERSON_COUNT_MQTT_TOPIC=care_ssl/all_zones/person_count_by_zone`
- `OCCUPANCY_MQTT_BROKER`
- `OCCUPANCY_MQTT_PORT`
- `OCCUPANCY_MQTT_TOPIC=care_ssl/all_zones/person_count_by_zone`
- `SEN55_MQTT_BROKER`
- `SEN55_MQTT_PORT`
- `SEN55_MQTT_TOPIC`
- `AIR1_API_URL`
- `AIR1_API_KEY`

Do not paste credentials into this Markdown. Keep them in the shell environment or `web_app/.env`.

### 3. Confirm Network Reachability

At minimum, confirm the MQTT broker and AIR-1 API host are reachable from the machine that will run the services:

```powershell
Test-NetConnection $env:PERSON_COUNT_MQTT_BROKER -Port $env:PERSON_COUNT_MQTT_PORT
Test-NetConnection $env:SEN55_MQTT_BROKER -Port $env:SEN55_MQTT_PORT
```

Also confirm both RTSP camera URLs open from this machine. The person counters cannot start without the camera streams.

### 4. Confirm SEN55 Is Actually Publishing

MQTT broker reachability is not enough. The SEN55 collector needs messages on the configured SEN55 topic.

Start this service and watch for buffered/flushed rows:

```powershell
.\run_sen55_collector.ps1
```

Minimum success signal:

- `data/sen55_data.csv` exists;
- the file has rows beyond the header;
- `data/sen55_data.parquet` is created after a Parquet rebuild.

### 5. Confirm Person Count MQTT Flow

Start both RTSP zone trackers in separate terminals:

```powershell
.\run_person_counter.ps1 -Camera cam1
.\run_person_counter.ps1 -Camera cam2
```

In another terminal, start the 10-second occupancy aggregator:

```powershell
python -m air1_all_zones.occupancy_mqtt_aggregator `
  --output-csv data\cv_occupancy_all_air1_10sec.csv `
  --output-parquet data\cv_occupancy_all_air1_10sec.parquet
```

Minimum success signal:

- `data/cv_occupancy_all_air1_10sec.csv` exists;
- the file has `timestamp` and `zone_id`;
- unique `zone_id` values are 1-15 only;
- it has rows beyond the header;
- `data/cv_occupancy_all_air1_10sec.parquet` is created after a Parquet rebuild.
- cam1 and cam2 both appear in the MQTT payloads or in the aggregator
  `camera_ids` column after both trackers have been running.

### 6. Confirm AIR-1 Live Collection

After SEN55 and CV labels are being written, start the all-zones collector:

```powershell
.\run_live_collector.ps1 -NoPromoteAfterRetrain
```

Minimum success signal:

- `data/air1_all_zones_training_cv.csv` exists;
- it has long-form rows for all 15 AIR-1 zones;
- `data/air1_all_zones_training_cv.parquet` exists after the first rebuild;
- `data/air1_all_zones_training_cv.metadata.json` exists.

## Bare Minimum Missing Before Live App Predictions

The live app can start before a model exists, but it should not be treated as working inference until promotion creates:

```text
model/production_run.txt
```

Before promoting a model, confirm the target table is genuinely per-zone: labels should not be identical across every zone at each timestamp, and Table 16 should not appear in the CV label or training Parquet.

After enough data exists, train and promote the first baseline:

```powershell
.\train_model.ps1
```

Then run the smoke test:

```powershell
python .\smoke_test.py --candidate-run model --production-pointer model\production_run.txt
```

Only after that should the live app be launched as an inference service:

```powershell
.\run_live_app.ps1
```

Minimum dashboard success signal:

- `GET /api/health` returns `rtsp_by_camera.cam1` and `rtsp_by_camera.cam2`;
- `/api/video/cam1.mjpg` and `/api/video/cam2.mjpg` stream independently;
- the dashboard shows cam2 status visibly, so a stopped cam2 stream is obvious;
- current/history payloads include 15-zone `zone_probabilities` and
  `ground_truth_by_zone` without Table 16.

## Docker Compose Path

Docker Compose is available in the package, but Docker is not currently available in this shell. If you want to start services with Compose, install/start Docker Desktop first and confirm:

```powershell
docker --version
docker compose version
```

Then run the compile and unit-test service checks:

```powershell
docker compose --profile ops run --rm compile-check
docker compose --profile ops run --rm unit-tests
```

Collection services can then be launched together:

```powershell
docker compose up --build person-counter person-counter-cam2 mqtt-aggregator sen55-collector live-collector
```

Add `live-app` only after `model/production_run.txt` exists:

```powershell
docker compose up --build live-app
```

## Not Required Just To Start Collection

These are still important, but they are not blockers for starting the collection services:

- baseline metrics;
- a promoted model;
- `model/production_run.txt`;
- replay app readiness;
- full production validation;
- Docker Compose, if you are using the PowerShell scripts instead.

## Do Not Share Yet

`web_app/.env` and `web_app/.env.example` contain lab deployment values in this checkout. Treat the bundle as internal-only until those files are redacted or replaced with a sanitized example.
