# AIR-1 All-Zones Docker Runbook

Run the AIR-1 all-zones package through Docker Compose:

- `person-counter`: cam1 RTSP -> mask-based per-zone counts -> MQTT
- `person-counter-cam2`: cam2 RTSP -> mask-based per-zone counts -> MQTT
- `mqtt-aggregator`: per-zone MQTT -> `data/cv_occupancy_all_air1_10sec.*`
- `sen55-collector`: SEN55 MQTT -> `data/sen55_data.*`
- `live-collector`: AIR-1 API + local label tables -> training CSV/Parquet; retrain/promotion are opt-in
- `live-app`: FastAPI two-camera all-zones dashboard on `http://localhost:8000/`
- `replay-app`: replay dashboard on `http://localhost:8001/`
- ops jobs: historical collection/export, CV training rebuild, training,
  promotion, smoke tests, compile checks, unit tests, and shell access

Runtime state stays on the host through bind mounts:

```text
./data  -> /app/data
./model -> /app/model
./logs  -> /app/logs
```

Masks, zone JSON files, model files, and tracker configs are mounted read-only
so zone edits can be picked up without rebuilding the image.

## First-Time Setup

From this package root:

```bash
mkdir -p data model logs
[ -f web_app/.env ] || cp web_app/.env.example web_app/.env
nano web_app/.env
```

On Linux, also create the root `.env` used by Compose for host-user ownership:

```bash
printf "AIR1_ALL_ZONES_UID=$(id -u)\nAIR1_ALL_ZONES_GID=$(id -g)\n" > .env
```

At minimum, review these values in `web_app/.env`:

```env
AIR1_API_URL=...
AIR1_API_KEY=...
AIR1_ALL_ZONES_RTSP_URL_CAM1=rtsp://admin:<password>@10.158.71.241:554/Streaming/channels/101
AIR1_ALL_ZONES_RTSP_URL_CAM2=rtsp://admin:<password>@10.158.71.240:554/Streaming/channels/101
PERSON_COUNT_MQTT_TOPIC=care_ssl/all_zones/person_count_by_zone
OCCUPANCY_MQTT_TOPIC=care_ssl/all_zones/person_count_by_zone
SEN55_MQTT_TOPIC=sen55_01/data
AIR1_ALL_ZONES_MJPEG_TARGET_FPS=15
RETRAIN_AFTER_PARQUET=0
PROMOTE_AFTER_RETRAIN=0
```

Both camera trackers publish to the same topic. `cam1` uses
`cam1-zones.json` plus `masks/cam1-mask-zones.png`; `cam2` uses
`cam2-zones.json` plus `masks/cam2-mask-zones.png`.

## Build And Start

```bash
docker compose config --quiet
docker compose build
docker compose up -d
```

Equivalent explicit service command:

```bash
docker compose up -d person-counter person-counter-cam2 mqtt-aggregator sen55-collector live-collector live-app
```

Open:

```text
http://localhost:8000/
http://localhost:8000/api/health
http://localhost:8000/api/video/cam1.mjpg
http://localhost:8000/api/video/cam2.mjpg
```

The app can start before a promoted model exists. It reports that
`model/production_run.txt` is missing until training and promotion create it.
`/api/health` reports both cameras under `rtsp_by_camera` and keeps legacy
`rtsp` as cam1. Current/history events expose `zone_probabilities`,
`aggregate_probability`, `occupied_zones`, aggregate CV fields, and
`ground_truth_by_zone` for zones 1-15 only. Health also reports
`config.mjpeg_target_fps`; the default is `15`.

The dashboard should show cam1 and cam2 feeds independently. If cam2 is stopped
or misconfigured, the cam2 status should be visible without relying on the cam1
feed.

The live collector does not retrain or promote by default. Keep it in
collect/rebuild mode for normal runtime. Use the `trainer` ops service for the
separate retrain path:

```bash
docker compose --profile ops run --rm -e RETRAIN_N_TRIALS=1 -e RETRAIN_MAX_EPOCHS=1 -e PROMOTE_AFTER_RETRAIN=0 trainer
```

The trainer writes immutable snapshots under `data/training_snapshots/`, holds
`model/retrain.lock`, records `model/retrain_status.json`, and uses
`RETRAIN_CV_FOLDS=auto` plus `RETRAIN_BOOTSTRAP_FALLBACK=auto`. Promotion only
updates `model/production_run.txt` when progressive CV, metric, and positive
evidence gates pass. Inline retraining is still available with
`RETRAIN_AFTER_PARQUET=1`, but it is not the default deployment path.

## Rebuild After Code Changes

```bash
docker compose build
docker compose up -d --force-recreate
```

For one service:

```bash
docker compose up -d --force-recreate live-app
docker compose up -d --force-recreate live-collector
docker compose up -d --force-recreate person-counter
docker compose up -d --force-recreate person-counter-cam2
```

## Status And Logs

```bash
docker compose ps
docker compose logs -f
docker compose logs -f person-counter
docker compose logs -f person-counter-cam2
docker compose logs -f mqtt-aggregator
docker compose logs -f live-collector
docker compose logs -f live-app
```

Check generated files:

```bash
ls -lh data/cv_occupancy_all_air1_10sec.csv data/cv_occupancy_all_air1_10sec.parquet
ls -lh data/air1_all_zones_training_cv.csv data/air1_all_zones_training_cv.parquet
cat model/production_run.txt
```

Check the web-app API contract:

```bash
curl -fsS http://127.0.0.1:8000/api/health
```

The health payload should include `rtsp_by_camera.cam1` and
`rtsp_by_camera.cam2`.

## Ops Jobs

```bash
docker compose --profile ops run --rm compile-check
docker compose --profile ops run --rm unit-tests
docker compose --profile ops run --rm trainer
docker compose --profile ops run --rm promoter
docker compose --profile ops run --rm smoke-test
```

Replay dashboard:

```bash
docker compose --profile replay up -d replay-app
```

## Stop

```bash
docker compose down
```

This stops containers only. Runtime state remains in `data/`, `model/`, and
`logs/`.
