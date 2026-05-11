# Zone 5 Docker Runbook

Run every Zone 5 package service through Docker Compose:

- `person-counter`: RTSP camera -> YOLO/ByteTrack CV person counts -> MQTT
- `mqtt-aggregator`: person-count MQTT -> `data/cv_occupancy_zone5_10sec.csv`
- `sen55-collector`: SEN55 MQTT -> `data/sen55_data.csv`
- `live-collector`: AIR-1/API + local label tables -> training CSV and metadata
- `live-app`: FastAPI dashboard on `http://localhost:8000/`
- `replay-app`: replay dashboard on `http://localhost:8001/`
- ops jobs: historical collection, secondary AIR-1 CSV export, training,
  promotion, smoke tests, compile checks, unit tests, and shell access

Runtime state stays on the host through bind mounts:

```text
./data  -> /app/data
./model -> /app/model
./logs  -> /app/logs
```

The image contains application code and Python dependencies. Rebuild the image
after changing `.py`, `.sh`, `requirements.txt`, the Dockerfile, or packaged CV
assets.

## From Package Root

PowerShell examples are for local Windows/manual Docker use. The production
server path is Ubuntu shell commands or CI/CD over SSH.

PowerShell:

```powershell
cd C:\Users\SmartI-Lab1\OneDrive\Desktop\CARE-SSL\zone5_cv_time_features_package
```

Bash:

```bash
cd ~/zone5_cv_time_features_package
```

## First-Time Setup

Create the runtime directories:

PowerShell:

```powershell
New-Item -ItemType Directory -Force -Path .\data, .\model, .\logs
```

Bash:

```bash
mkdir -p data model logs
```

On Linux, also create a root `.env` file next to `docker-compose.yml` so
containers write bind-mounted files as your host user instead of root:

```bash
printf "ZONE5_UID=$(id -u)\nZONE5_GID=$(id -g)\n" > .env
```

If Docker already created root-owned runtime files, fix ownership before using
the systemd deployment or non-root Compose containers:

```bash
sudo chown -R "$USER:$USER" data model logs .python-packages
chmod -R u+rwX data model logs .python-packages
```

Create `web_app/.env` if missing. Compose loads this file into every service.

PowerShell:

```powershell
if (!(Test-Path .\web_app\.env)) { Copy-Item .\web_app\.env.example .\web_app\.env }
notepad .\web_app\.env
```

Bash:

```bash
[ -f web_app/.env ] || cp web_app/.env.example web_app/.env
nano web_app/.env
```

Set or confirm these values in `web_app/.env`:

```env
AIR1_API_URL=http://10.158.66.30:80
AIR1_API_KEY=9c5c3569-cfe7-42ae-bf00-e86ae08519ef
ZONE5_RTSP_URL=rtsp://admin:++smartilab2023@10.158.71.241:554/Streaming/channels/101
PERSON_COUNT_MQTT_BROKER=10.158.71.19
PERSON_COUNT_MQTT_PORT=1883
PERSON_COUNT_MQTT_TOPIC=care_ssl/zone5/person_count
PERSON_COUNT_MQTT_USERNAME=guest
PERSON_COUNT_MQTT_PASSWORD=smartilab123
PERSON_COUNT_IMGSZ=256
PERSON_COUNT_TRACKING=1
PERSON_COUNT_SHOW_MASK=1
TRACKER=cv_counter/trackers/bytetrack.yaml
OCCUPANCY_MQTT_BROKER=10.158.71.19
OCCUPANCY_MQTT_PORT=1883
OCCUPANCY_MQTT_TOPIC=care_ssl/zone5/person_count
OCCUPANCY_MQTT_USERNAME=guest
OCCUPANCY_MQTT_PASSWORD=smartilab123
SEN55_MQTT_BROKER=10.158.71.19
SEN55_MQTT_PORT=1883
SEN55_MQTT_TOPIC=sen55_01/data
SEN55_MQTT_USERNAME=guest
SEN55_MQTT_PASSWORD=smartilab123
```

Optional wrapper settings can also go in `web_app/.env`:

```env
DEVICE=cpu
PERSON_COUNT_IMGSZ=256
PERSON_COUNT_TRACKING=1
PERSON_COUNT_SHOW_MASK=1
TRACKER=cv_counter/trackers/bytetrack.yaml
COUNTS_EVERY=1
MQTT_EVERY=1
DURATION_MIN=1440
APPEND_EVERY_SEC=10
SNAPSHOT_REFRESH_EVERY_HOURS=1
RETRAIN_AFTER_SNAPSHOT=0
PROMOTE_AFTER_RETRAIN=0
OUTPUT_CSV=data/sen55_data.csv
```

Runtime Compose services write CSV tables and metadata only. Immutable training
snapshots are still written under `data/training_snapshots/` by the trainer.

Validate the Compose file:

```bash
docker compose config --quiet
```

## Build

Build the image:

```bash
docker compose build
```

Build without cache when dependency layers look stale:

```bash
docker compose build --no-cache
```

## Start Live Stack

Start the full live pipeline:

```bash
docker compose up -d
```

Equivalent explicit service command:

```bash
docker compose up -d person-counter mqtt-aggregator sen55-collector live-collector live-app
```

Open:

```text
http://localhost:8000/
http://localhost:8000/api/health
```

The app can start before the first promoted model exists. It reports that
`model/production_run.txt` is missing until training and promotion create it.
The default Compose live collector only collects and refreshes metadata; run
the ops trainer/promoter manually, or explicitly enable inline retraining with
`RETRAIN_AFTER_SNAPSHOT=1`.

## Rebuild After Code Changes

For changed `.py` files, wrapper scripts, Docker files, requirements, or
packaged assets:

```bash
docker compose build
docker compose up -d --force-recreate
```

To recreate one service after a rebuild:

```bash
docker compose up -d --force-recreate live-app
docker compose up -d --force-recreate live-collector
docker compose up -d --force-recreate person-counter
```

## Status And Logs

List services:

```bash
docker compose ps
```

Follow all logs:

```bash
docker compose logs -f
```

Follow one service:

```bash
docker compose logs -f person-counter
docker compose logs -f mqtt-aggregator
docker compose logs -f sen55-collector
docker compose logs -f live-collector
docker compose logs -f live-app
```

Show recent logs:

```bash
docker compose logs --tail 100 live-app
docker compose logs --tail 100 live-collector
docker compose logs --tail 100 person-counter
```

Check the app health endpoint from the host:

PowerShell:

```powershell
Invoke-RestMethod http://localhost:8000/api/health
```

Bash:

```bash
curl -fsS http://localhost:8000/api/health
```

Check generated files on the host:

PowerShell:

```powershell
Get-ChildItem .\data
Get-ChildItem .\model
Get-ChildItem .\logs
```

Bash:

```bash
ls -lah data model logs
```

## Stop And Cleanup

Stop containers but keep them defined:

```bash
docker compose stop
```

Start stopped containers:

```bash
docker compose start
```

Stop and remove containers/network while keeping host `data/`, `model/`, and
`logs/`:

```bash
docker compose down
```

Remove stopped containers and the local image built by this Compose project:

```bash
docker compose down --rmi local
```

## Live Service Commands

Start only the person counter:

```bash
docker compose up -d person-counter
```

Start only the MQTT aggregators:

```bash
docker compose up -d mqtt-aggregator sen55-collector
```

Start only the live collector:

```bash
docker compose up -d live-collector
```

Start only the web app:

```bash
docker compose up -d live-app
```

Restart one service:

```bash
docker compose restart person-counter
docker compose restart mqtt-aggregator
docker compose restart sen55-collector
docker compose restart live-collector
docker compose restart live-app
```

Run a service in the foreground for debugging:

```bash
docker compose up person-counter
docker compose up live-app
```

## Replay Dashboard

Replay mode reads `data/zone5_training_cv.csv` by default through
`ZONE5_REPLAY_TABLE`. Point that variable at an immutable training snapshot
only when you intentionally want to replay an older snapshot.

Start replay mode on host port `8001`:

```bash
docker compose --profile replay up -d replay-app
```

Open:

```text
http://localhost:8001/
http://localhost:8001/api/health
```

Follow replay logs:

```bash
docker compose --profile replay logs -f replay-app
```

Stop replay:

```bash
docker compose --profile replay stop replay-app
```

Use a different replay host port:

PowerShell:

```powershell
$env:ZONE5_REPLAY_PORT = "8011"
docker compose --profile replay up -d replay-app
```

Bash:

```bash
ZONE5_REPLAY_PORT=8011 docker compose --profile replay up -d replay-app
```

## Ops Commands

Run Python compile checks inside the image:

```bash
docker compose --profile ops run --rm compile-check
```

Run unit tests inside the image:

```bash
docker compose --profile ops run --rm unit-tests
```

Run the smoke test:

```bash
docker compose --profile ops run --rm smoke-test
```

Open an interactive shell:

```bash
docker compose --profile ops run --rm shell
```

Print CLI help from inside the image:

```bash
docker compose --profile ops run --rm historical-collector python -m zone5.collect_training_data --help
docker compose --profile ops run --rm csv-exporter python -m zone5.air1_exporter --help
docker compose --profile ops run --rm trainer python -m zone5.training --help
docker compose --profile ops run --rm promoter python -m zone5.promote_model --help
docker compose --profile ops run --rm smoke-test python smoke_test.py --help
```

## Historical Data Commands

Collect the default historical window with `zone5.collect_training_data`:

```bash
docker compose --profile ops run --rm historical-collector
```

Collect a fixed UTC window and require CV labels:

```bash
docker compose --profile ops run --rm historical-collector \
  python -m zone5.collect_training_data \
  --time-start 2026-05-06T00:00:00Z \
  --time-end 2026-05-07T00:00:00Z \
  --require-cv-labels
```

Run live append once as an ops command instead of as the persistent service:

```bash
docker compose --profile ops run --rm historical-collector \
  python -m zone5.collect_training_data \
  --live-append \
  --duration-min 120 \
  --append-every-sec 10 \
  --snapshot-refresh-every-hours 1
```

Export the Zone 5 AIR-1 CSV:

```bash
docker compose --profile ops run --rm csv-exporter
```

Export the Zone 5 AIR-1 CSV for a fixed UTC window:

```bash
docker compose --profile ops run --rm csv-exporter \
  python -m zone5.air1_exporter \
  --time-start 2026-05-06T00:00:00Z \
  --time-end 2026-05-07T00:00:00Z
```

## Training And Promotion

The normal Compose live stack does not schedule hourly retraining. Use these
ops commands when you want to train or promote. The systemd deployment has the
separate `zone5-trainer.timer` path for scheduled server-side retraining.

Train from `data/zone5_training_cv.csv`:

```bash
docker compose --profile ops run --rm trainer
```

Run a quick training smoke without promotion:

```bash
docker compose --profile ops run --rm -e ZONE5_SKIP_PROMOTE=1 trainer \
  bash train_model.sh \
  --n-trials 1 \
  --max-epochs 1 \
  --optuna-jobs 1 \
  --cv-folds 1 \
  --output-dir .test_runtime/train_smoke \
  --bootstrap-fallback \
  --allow-degenerate-validation
```

Train with custom hyperparameter budget:

```bash
docker compose --profile ops run --rm trainer \
  bash train_model.sh \
  --n-trials 20 \
  --max-epochs 10 \
  --optuna-jobs 1 \
  --cv-folds 3
```

Train without promotion:

```bash
docker compose --profile ops run --rm -e ZONE5_SKIP_PROMOTE=1 trainer
```

Promote the current candidate run:

```bash
docker compose --profile ops run --rm promoter
```

Promote with explicit gates:

```bash
docker compose --profile ops run --rm promoter \
  python -m zone5.promote_model \
  --candidate-run model \
  --production-pointer model/production_run.txt \
  --min-test-pr-auc 0 \
  --min-mean-cv-pr-auc 0 \
  --min-positive-windows 5 \
  --min-positive-buckets 5 \
  --min-positive-events 1
```

## App Port Overrides

The live app host port is a Compose setting. Set it from the shell or a root
`.env` file next to `docker-compose.yml`.

PowerShell:

```powershell
$env:ZONE5_APP_PORT = "8080"
docker compose up -d live-app
```

Bash:

```bash
ZONE5_APP_PORT=8080 docker compose up -d live-app
```

## Runtime Overrides

Use `web_app/.env` for service runtime settings. Examples:

```env
DEVICE=cpu
PERSON_COUNT_IMGSZ=256
PERSON_COUNT_TRACKING=1
PERSON_COUNT_SHOW_MASK=1
TRACKER=cv_counter/trackers/bytetrack.yaml
COUNTS_EVERY=5
MQTT_EVERY=5
DURATION_MIN=120
APPEND_EVERY_SEC=10
SNAPSHOT_REFRESH_EVERY_HOURS=0.5
RETRAIN_AFTER_SNAPSHOT=0
PROMOTE_AFTER_RETRAIN=0
ZONE5_DATA_SOURCE=live
ZONE5_TICK_INTERVAL_SEC=10.0
ZONE5_HISTORY_SIZE=240
ZONE5_MAX_GAP_MINUTES=5.0
ZONE5_MAX_AGE_MINUTES=15.0
```

You can also pass one-off environment overrides:

```bash
docker compose run --rm -e DURATION_MIN=120 -e PROMOTE_AFTER_RETRAIN=0 live-collector
docker compose run --rm -e DEVICE=cpu -e COUNTS_EVERY=5 person-counter
docker compose run --rm -e TRACKER=cv_counter/trackers/botsort.yaml -e PERSON_COUNT_IMGSZ=320 person-counter
```

## GPU Person Counter

For NVIDIA GPU inference on Linux, install NVIDIA Container Toolkit and create
`docker-compose.override.yml`:

```yaml
services:
  person-counter:
    gpus: all
    environment:
      DEVICE: "0"
```

Then rebuild and restart the person counter:

```bash
docker compose build
docker compose up -d --force-recreate person-counter
```

## Troubleshooting

If Docker cannot read `web_app/.env`, create it:

```bash
[ -f web_app/.env ] || cp web_app/.env.example web_app/.env
```

If the app says the production pointer is missing, train and promote:

```bash
docker compose --profile ops run --rm trainer
docker compose --profile ops run --rm promoter
docker compose restart live-app
```

If `person-counter` exits because `lap` is missing, rebuild the image:

```bash
docker compose build --no-cache person-counter
docker compose up -d --force-recreate person-counter
```

If RTSP, MQTT, or AIR-1 cannot connect, verify `web_app/.env` and inspect logs:

```bash
docker compose logs --tail 100 person-counter
docker compose logs --tail 100 mqtt-aggregator
docker compose logs --tail 100 sen55-collector
docker compose logs --tail 100 live-collector
docker compose logs --tail 100 live-app
```

If you changed Python files and the running container still behaves like old
code:

```bash
docker compose build
docker compose up -d --force-recreate
```
