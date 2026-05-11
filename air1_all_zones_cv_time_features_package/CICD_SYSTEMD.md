# CI/CD Tutorial: Ubuntu Systemd Deployment

This tutorial turns this folder into a simple CI/CD project that tests every
change and deploys the live AIR-1 all-zones services to an Ubuntu server using
user-level systemd services.

Use this path when the server should run the existing shell and Python commands
directly instead of Docker Compose.

The systemd services are:

- `air1-all-zones-person-counter-cam1.service`
- `air1-all-zones-person-counter-cam2.service`
- `air1-all-zones-mqtt-aggregator.service`
- `air1-all-zones-sen55-collector.service`
- `air1-all-zones-live-collector.service`
- `air1-all-zones-live-app.service`

The live app is expected to serve both cameras. Its health payload must include
`rtsp_by_camera.cam1` and `rtsp_by_camera.cam2`; legacy `rtsp` is retained as a
cam1 compatibility field. It also reports `config.mjpeg_target_fps`, defaulting
to `15`.

Runtime state stays on the server in `data/`, `model/`, `logs/`,
`.python-packages/`, and `web_app/.env`. CI should test code; it should not
train or promote production models on every commit.

## 1. Prepare The Folder For Git

From this package root:

```bash
git init
git branch -M main
```

Do not commit live credentials, camera URLs, API keys, MQTT passwords, generated
data, generated model runs, logs, virtual environments, or package-local
dependency directories. The existing `.gitignore` ignores the usual runtime
paths.

Commit the source:

```bash
git add .
git commit -m "Initial AIR-1 all-zones package"
```

Push to your Git host:

```bash
git remote add origin git@github.com:YOUR_ORG/YOUR_REPO.git
git push -u origin main
```

## 2. Prepare The Ubuntu Server

```bash
cd ~
git clone git@github.com:YOUR_ORG/YOUR_REPO.git air1_all_zones_cv_time_features_package
cd ~/air1_all_zones_cv_time_features_package
python3 -m pip install --upgrade pip
python3 -m pip install --target .python-packages --upgrade -r requirements.txt
mkdir -p data model logs ~/.config/systemd/user
[ -f web_app/.env ] || cp web_app/.env.example web_app/.env
nano web_app/.env
```

Set server-only values in `web_app/.env`, including both camera URLs and the
per-zone MQTT topic:

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
default. Scheduled training belongs to the separate
`air1-all-zones-trainer.service` and optional
`air1-all-zones-trainer.timer`; do not enable the timer automatically from CI.

Create the service files from [DEPLOYMENT.md](DEPLOYMENT.md), then start them:

```bash
systemctl --user daemon-reload
systemctl --user enable --now air1-all-zones-person-counter-cam1.service
systemctl --user enable --now air1-all-zones-person-counter-cam2.service
systemctl --user enable --now air1-all-zones-mqtt-aggregator.service
systemctl --user enable --now air1-all-zones-sen55-collector.service
systemctl --user enable --now air1-all-zones-live-collector.service
systemctl --user enable --now air1-all-zones-live-app.service
curl -fsS http://127.0.0.1:8000/api/health
```

After the first deploy, open the dashboard and confirm it shows separate cam1
and cam2 feeds plus a zone grid grouped by camera coverage.

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
```

This CI checks code only. It does not start live services because the hosted
runner cannot reach the lab camera, MQTT broker, AIR-1 API, or server runtime
files.

## 4. Add Deploy Workflow

Add repository secrets:

```text
SSH_HOST
SSH_USER
SSH_KEY
SSH_PORT
```

Create `.github/workflows/deploy-systemd.yml`:

```yaml
name: deploy-systemd

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

      - name: Deploy and restart user services
        run: |
          ssh -i ~/.ssh/deploy_key -p "${{ secrets.SSH_PORT || 22 }}" "${{ secrets.SSH_USER }}@${{ secrets.SSH_HOST }}" <<'EOF'
          set -euo pipefail
          cd ~/air1_all_zones_cv_time_features_package
          git pull --ff-only
          python3 -m pip install --target .python-packages --upgrade -r requirements.txt
          systemctl --user daemon-reload
          systemctl --user restart air1-all-zones-person-counter-cam1.service
          systemctl --user restart air1-all-zones-person-counter-cam2.service
          systemctl --user restart air1-all-zones-mqtt-aggregator.service
          systemctl --user restart air1-all-zones-sen55-collector.service
          systemctl --user restart air1-all-zones-live-collector.service
          systemctl --user restart air1-all-zones-live-app.service
          curl -fsS http://127.0.0.1:8000/api/health >/dev/null
          EOF
```

Keep `web_app/.env`, `data/`, `model/`, and `logs/` on the server. Do not
replace them from CI.

Deployment success only proves the service restarted and the health endpoint
responded. It does not prove model readiness until `model/production_run.txt`
exists, and it does not prove label readiness until `ground_truth_by_zone`
contains current per-zone labels from both camera streams.
