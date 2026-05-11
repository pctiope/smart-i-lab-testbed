# CI/CD Tutorial: Docker Compose Deployment

This tutorial turns this folder into a simple CI/CD project that tests every
change and deploys the live AIR-1 all-zones stack to an Ubuntu server with
Docker Compose.

Use this path when the server should run the services from `docker-compose.yml`:

- `person-counter` for cam1
- `person-counter-cam2` for cam2
- `mqtt-aggregator`
- `sen55-collector`
- `live-collector`
- `live-app`

Runtime state stays on the server in `data/`, `model/`, `logs/`, and
`web_app/.env`. CI should test code and build the image; it should not train or
promote production models on every commit.

## 1. Prepare The Folder For Git

From this package root:

```bash
git init
git branch -M main
git add .
git commit -m "Initial AIR-1 all-zones package"
git remote add origin git@github.com:YOUR_ORG/YOUR_REPO.git
git push -u origin main
```

Do not commit live credentials, camera URLs, API keys, MQTT passwords, generated
data, generated model runs, logs, virtual environments, or package-local
dependency directories.

## 2. Prepare The Ubuntu Server

Install Docker and the Compose plugin, then clone the repo:

```bash
cd ~
git clone git@github.com:YOUR_ORG/YOUR_REPO.git air1_all_zones_cv_time_features_package
cd ~/air1_all_zones_cv_time_features_package
mkdir -p data model logs
printf "AIR1_ALL_ZONES_UID=$(id -u)\nAIR1_ALL_ZONES_GID=$(id -g)\n" > .env
[ -f web_app/.env ] || cp web_app/.env.example web_app/.env
nano web_app/.env
```

Set server-only values in `web_app/.env`:

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

The live collector only collects CSV rows and rebuilds the training Parquet by
default. Scheduled training belongs to `run_air1_all_zones_trainer.sh` or the
Compose `trainer` ops service, which writes `data/training_snapshots/`,
`model/retrain.lock`, and `model/retrain_status.json`.

Validate and start manually once before automating deploys:

```bash
docker compose config --quiet
docker compose build
docker compose up -d person-counter person-counter-cam2 mqtt-aggregator sen55-collector live-collector live-app
curl -fsS http://127.0.0.1:8000/api/health
```

The app may report that `model/production_run.txt` is missing until enough real
data exists, training succeeds, and promotion creates the production pointer.
That is acceptable for deployment smoke testing. The health payload should
still include `rtsp_by_camera.cam1` and `rtsp_by_camera.cam2`, and the dashboard
should show separate cam1/cam2 feeds plus grouped zone output. Health also
reports `config.mjpeg_target_fps`, defaulting to `15`.

## 3. Add GitHub Actions CI

Create `.github/workflows/ci.yml`:

```yaml
name: ci

on:
  pull_request:
  push:
    branches: [main]

jobs:
  test:
    runs-on: ubuntu-latest
    timeout-minutes: 60

    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install Python dependencies
        run: |
          python -m pip install --upgrade pip
          python -m pip install -r requirements.txt

      - name: Compile Python modules
        run: python -m compileall air1_all_zones web_app tests smoke_test.py rtsp_zone_tracker.py

      - name: Run unit tests
        run: python -m unittest discover -s tests -p 'test*.py' -v

      - name: Create CI-only env files
        run: |
          touch .env
          touch web_app/.env

      - name: Validate Compose config
        run: docker compose config --quiet

      - name: Build Docker image
        run: docker compose build
```

This CI checks code and proves the Docker image can be built. It does not start
live services because the hosted runner cannot reach the lab camera, MQTT
broker, AIR-1 API, or server runtime files.

## 4. Add Deploy Workflow

Add repository secrets:

```text
SSH_HOST
SSH_USER
SSH_KEY
SSH_PORT
```

Create `.github/workflows/deploy-compose.yml`:

```yaml
name: deploy-compose

on:
  workflow_run:
    workflows: ["ci"]
    types: [completed]
    branches: [main]
  workflow_dispatch:

jobs:
  deploy:
    if: github.event_name == 'workflow_dispatch' || github.event.workflow_run.conclusion == 'success'
    runs-on: ubuntu-latest
    timeout-minutes: 30

    steps:
      - name: Install SSH key
        run: |
          mkdir -p ~/.ssh
          printf '%s\n' "${{ secrets.SSH_KEY }}" > ~/.ssh/deploy_key
          chmod 600 ~/.ssh/deploy_key
          ssh-keyscan -p "${{ secrets.SSH_PORT || 22 }}" "${{ secrets.SSH_HOST }}" >> ~/.ssh/known_hosts

      - name: Deploy Compose stack
        run: |
          ssh -i ~/.ssh/deploy_key -p "${{ secrets.SSH_PORT || 22 }}" "${{ secrets.SSH_USER }}@${{ secrets.SSH_HOST }}" <<'EOF'
          set -euo pipefail
          cd ~/air1_all_zones_cv_time_features_package
          git pull --ff-only
          docker compose config --quiet
          docker compose build
          docker compose up -d --force-recreate person-counter person-counter-cam2 mqtt-aggregator sen55-collector live-collector live-app
          curl -fsS http://127.0.0.1:8000/api/health >/dev/null
          EOF
```

Keep `web_app/.env`, `.env`, `data/`, `model/`, and `logs/` on the server. Do
not replace them from CI.

Deployment success only proves the container stack restarted and the health
endpoint responded. It does not prove model readiness until
`model/production_run.txt` exists, and it does not prove label readiness until
`ground_truth_by_zone` contains current per-zone labels from both camera
streams.
