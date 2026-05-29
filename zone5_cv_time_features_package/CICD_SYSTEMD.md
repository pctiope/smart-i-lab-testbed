# CI/CD Tutorial: Ubuntu systemd Deployment

This tutorial turns this folder into a simple CI/CD project that tests every
change and deploys the live Zone 5 services to an Ubuntu server running
user-level systemd services.

Use this path when the server should run the existing shell and Python commands
directly instead of Docker Compose.

In the combined `smart-i-lab-testbed` repo, production source deploy is handled
by `.github/workflows/zone5-systemd-source-deploy.yml`. Docker Compose remains
the staging deploy path on the separate Compose branch. Model delivery and
training stay in their own production workflows, so a source deploy never starts
or restarts `zone5-trainer.service`.

The systemd services are:

- `zone5-person-counter.service`
- `zone5-mqtt-aggregator.service`
- `zone5-sen55-collector.service`
- `zone5-live-collector.service`
- `zone5-live-app.service`
- `zone5-vite-frontend.service`
- `zone5-trainer.timer`
- `zone5-trainer.service`

The source deploy workflow restarts only the six live services. It keeps
`zone5-trainer.timer` disabled. If `zone5-trainer.service` is already active or
activating when the workflow reaches the production runner, the workflow reports
a deferred deploy and exits without changing the production checkout or live
services. The deploy helper still refuses to mutate production if training
starts after that preflight.

Runtime state stays on the server in `data/`, `model/`, `logs/`,
`.python-packages/`, and `web_app/.env`. CI should test code; it should not
train or promote production models on every commit. Runtime services write CSV
tables; only `zone5-trainer.service` writes immutable snapshots under
`data/training_snapshots/`.

Current production-compatible model artifacts must use
`zone5_missingness_decoupled_v1`: raw sensor columns plus deterministic time
features only. Old mmWave-recency artifacts are not loadable after this
contract migration; use `zone5.promote_model --force-promote` only for the
intentional one-time promotion after the new candidate passes its own gates.

## 1. Prepare The Folder For Git

From this package root:

```bash
git init
git branch -M main
```

Review files before the first push. Do not commit live credentials, camera URLs,
API keys, MQTT passwords, generated data, generated model runs, logs, virtual
environments, or package-local dependency directories.

The existing `.gitignore` already ignores the important local runtime paths:

```text
web_app/.env
*.env
data/*
model/*
logs/
.python-packages/
.venv/
.test_runtime/
```

Before pushing to a hosted Git service, also review docs and examples for real
deployment secrets. Replace real values in examples with placeholders if the
repo will be shared.

Commit the source:

```bash
git add .
git commit -m "Initial Zone 5 package"
```

Create an empty repository in GitHub or GitLab, then push:

```bash
git remote add origin git@github.com:YOUR_ORG/YOUR_REPO.git
git push -u origin main
```

For GitLab, use the GitLab SSH URL instead:

```bash
git remote add origin git@gitlab.com:YOUR_GROUP/YOUR_REPO.git
git push -u origin main
```

If the Git host rejects the packaged model file because of size limits, move
large binary assets to Git LFS before the first public push.

## 2. Prepare The Ubuntu Server

Clone the repo on the server:

```bash
cd ~
git clone git@github.com:YOUR_ORG/YOUR_REPO.git zone5_cv_time_features_package
cd ~/zone5_cv_time_features_package
```

Install package-local dependencies:

```bash
python3 -m pip install --upgrade pip
python3 -m pip install --target .python-packages --upgrade -r requirements.txt
mkdir -p data model logs ~/.config/systemd/user
```

Create the service runtime environment file:

```bash
[ -f web_app/.env ] || cp web_app/.env.example web_app/.env
nano web_app/.env
```

Set real values in `web_app/.env` on the server only. At minimum, review:

```env
AIR1_API_URL=...
AIR1_API_KEY=...
ZONE5_RTSP_URL=...
PERSON_COUNT_MQTT_BROKER=...
PERSON_COUNT_MQTT_USERNAME=...
PERSON_COUNT_MQTT_PASSWORD=...
PERSON_COUNT_IMGSZ=256
PERSON_COUNT_TRACKING=1
PERSON_COUNT_SHOW_MASK=1
TRACKER=cv_counter/trackers/bytetrack.yaml
OCCUPANCY_MQTT_BROKER=...
OCCUPANCY_MQTT_USERNAME=...
OCCUPANCY_MQTT_PASSWORD=...
SEN55_MQTT_BROKER=...
SEN55_MQTT_USERNAME=...
SEN55_MQTT_PASSWORD=...
```

Enable lingering so user services keep running after SSH disconnects and after
reboot:

```bash
loginctl enable-linger "$USER"
```

If that requires elevated permissions:

```bash
sudo loginctl enable-linger "$USER"
```

Install the bundled user service and timer files, then start them:

```bash
install -m 0644 systemd/user/zone5-*.service systemd/user/zone5-*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user stop sen55-table.service 2>/dev/null || true
systemctl --user enable --now zone5-person-counter.service
systemctl --user enable --now zone5-mqtt-aggregator.service
systemctl --user enable --now zone5-sen55-collector.service
systemctl --user enable --now zone5-live-collector.service
systemctl --user enable --now zone5-live-app.service
systemctl --user enable --now zone5-vite-frontend.service
systemctl --user disable --now zone5-trainer.timer
```

Check the app once before automating deploys:

```bash
curl -fsS http://127.0.0.1:8000/api/health
```

`/api/health` with `ok=true`, the dashboard page, and video availability prove
service reachability. They do not prove live inference output. Check
`/api/current` as well; `/api/current.probability` proves the live app is
producing an inference probability.

```bash
curl -fsS http://127.0.0.1:8000/api/current
```

The app may report that `model/production_run.txt` is missing until enough real
data exists, training succeeds, and promotion creates the production pointer.
If `/api/current` reports `LIVE DATA DEGRADED: core sensor coverage below gate`,
the core coverage gate failed. AIR-1 and power fields require at least `0.80`
coverage, and `mmwave_s5` requires at least `0.95` coverage. `sen55-missing` is
not the blocker by itself because SEN55 is optional. The core AIR-1, smart
plug, and mmWave fields gate live prediction. Inspect `/api/current.error`,
verify upstream Smart I-Lab API history for the Zone 5 devices, and restart
only the live app service if upstream data is healthy but the running app cache
remains stale.

## 3. Add GitHub Actions CI

Create this file in the repo:

```text
.github/workflows/ci.yml
```

Use:

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
        run: python -m compileall zone5 web_app smoke_test.py

      - name: Run unit tests
        run: python -m unittest discover -s tests -p 'test*.py' -v
```

This CI checks code only. It does not start live services because the hosted
runner cannot reach the lab camera, MQTT broker, AIR-1 API, or your server
runtime files.

## 4. Add GitHub Actions Deploy

The current repository already has a production source deploy workflow:

```text
.github/workflows/zone5-systemd-source-deploy.yml
```

It runs on manual dispatch and on relevant `main` pushes under
`zone5_cv_time_features_package/`, excluding docs-only, `model/`, `data/`,
`logs/`, and `model_registry/` metadata-only changes. Validation runs on a
GitHub-hosted Ubuntu runner:

```bash
python -m compileall zone5 web_app ci smoke_test.py
PYTHONPATH=. python tests/test_cv_ground_truth.py
npm ci && npm run build
systemd-analyze verify systemd/user/*.service systemd/user/*.timer
docker compose config --quiet
docker compose build
```

Deployment runs on the self-hosted runner labeled
`self-hosted`, `linux`, `x64`, `zone5`, and `smart-ilab`. The helper script is:

```text
ci/deploy_zone5_systemd_source.sh
```

By default it deploys to:

```text
$HOME/smart-i-lab-testbed
```

The helper refuses to run on a dirty production checkout, fetches `origin/main`,
fast-forwards to the workflow SHA, updates `.python-packages/` only when
`requirements.txt` changed or the target directory is missing, runs `npm ci`
only when frontend dependencies are missing or changed, rebuilds the Vite app
only when frontend files changed or `dist/` is missing, installs changed user
units, disables `zone5-trainer.timer`, restarts only live services, and checks
production backend/frontend health.

The source deploy, retrain, and model-delivery workflows all use the shared
`zone5-production` concurrency group so production mutations do not overlap.
The source deploy and retrain workflows also preflight `zone5-trainer.service`;
an already-running retrain becomes a successful deferred/no-op workflow run
instead of a red failure that implies broken code.
After the first manual source deploy, verify the runner, staging stack, and
production endpoints from the production host:

```bash
bash zone5_cv_time_features_package/ci/check_zone5_cicd_health.sh
```

The example below is a generic SSH deploy pattern for a standalone copy of this
package. The combined repo should use the self-hosted-runner workflow above.

Add these repository secrets in GitHub:

```text
SSH_HOST       example: zone5-server.example.com
SSH_USER       example: ubuntu
SSH_KEY        private SSH key that can log in as SSH_USER
SSH_PORT       optional, defaults to 22
```

Create:

```text
.github/workflows/deploy-systemd.yml
```

Use:

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
      - name: Configure SSH
        run: |
          mkdir -p ~/.ssh
          printf '%s\n' "${{ secrets.SSH_KEY }}" > ~/.ssh/zone5_deploy_key
          chmod 600 ~/.ssh/zone5_deploy_key
          ssh-keyscan -p "${{ secrets.SSH_PORT || '22' }}" "${{ secrets.SSH_HOST }}" >> ~/.ssh/known_hosts

      - name: Deploy on server
        run: |
          ssh -i ~/.ssh/zone5_deploy_key -p "${{ secrets.SSH_PORT || '22' }}" "${{ secrets.SSH_USER }}@${{ secrets.SSH_HOST }}" <<'EOF'
          set -euo pipefail
          cd ~/zone5_cv_time_features_package
          git fetch origin main
          git checkout main
          git pull --ff-only origin main
          python3 -m pip install --target .python-packages --upgrade -r requirements.txt
          mkdir -p data model logs ~/.config/systemd/user
          install -m 0644 systemd/user/zone5-*.service systemd/user/zone5-*.timer ~/.config/systemd/user/
          systemctl --user daemon-reload
          systemctl --user stop sen55-table.service 2>/dev/null || true
          systemctl --user restart zone5-person-counter.service
          systemctl --user restart zone5-mqtt-aggregator.service
          systemctl --user restart zone5-sen55-collector.service
          systemctl --user restart zone5-live-collector.service
          systemctl --user restart zone5-live-app.service
          systemctl --user restart zone5-vite-frontend.service
          systemctl --user disable --now zone5-trainer.timer
          systemctl --user --no-pager --failed
          curl -fsS http://127.0.0.1:8000/api/health || true
          EOF
```

The final health check is allowed to be non-fatal because a fresh deployment can
start before a production model has been promoted.

## 5. GitLab CI Version

If the repo lives in GitLab instead, create:

```text
.gitlab-ci.yml
```

Use:

```yaml
stages:
  - test
  - deploy

variables:
  PIP_DISABLE_PIP_VERSION_CHECK: "1"

test:
  stage: test
  image: python:3.11-slim
  before_script:
    - python -m pip install --upgrade pip
    - python -m pip install -r requirements.txt
  script:
    - python -m compileall zone5 web_app smoke_test.py
    - python -m unittest discover -s tests -p 'test*.py' -v

deploy_systemd:
  stage: deploy
  image: alpine:3.20
  needs: [test]
  rules:
    - if: '$CI_COMMIT_BRANCH == "main"'
  before_script:
    - apk add --no-cache openssh-client
    - mkdir -p ~/.ssh
    - printf '%s\n' "$SSH_PRIVATE_KEY" > ~/.ssh/zone5_deploy_key
    - chmod 600 ~/.ssh/zone5_deploy_key
    - ssh-keyscan -p "${SSH_PORT:-22}" "$SSH_HOST" >> ~/.ssh/known_hosts
  script:
    - |
      ssh -i ~/.ssh/zone5_deploy_key -p "${SSH_PORT:-22}" "$SSH_USER@$SSH_HOST" <<'EOF'
      set -euo pipefail
      cd ~/zone5_cv_time_features_package
      git fetch origin main
      git checkout main
      git pull --ff-only origin main
      python3 -m pip install --target .python-packages --upgrade -r requirements.txt
      mkdir -p data model logs ~/.config/systemd/user
      install -m 0644 systemd/user/zone5-*.service systemd/user/zone5-*.timer ~/.config/systemd/user/
      systemctl --user daemon-reload
      systemctl --user stop sen55-table.service 2>/dev/null || true
      systemctl --user restart zone5-person-counter.service
      systemctl --user restart zone5-mqtt-aggregator.service
      systemctl --user restart zone5-sen55-collector.service
      systemctl --user restart zone5-live-collector.service
      systemctl --user restart zone5-live-app.service
      systemctl --user restart zone5-vite-frontend.service
      systemctl --user disable --now zone5-trainer.timer
      systemctl --user --no-pager --failed
      curl -fsS http://127.0.0.1:8000/api/health || true
      EOF
```

Add these GitLab CI/CD variables:

```text
SSH_HOST
SSH_USER
SSH_PRIVATE_KEY
SSH_PORT
```

Mark `SSH_PRIVATE_KEY` as masked and protected if the repo uses protected
branches.

## 6. What CI Should Not Do

Do not run normal training or automatic promotion in CI. Training needs real
time-series data, can be slow, and writes production artifacts under `model/`.

Keep these as server-side operations:

```bash
cd ~/zone5_cv_time_features_package
PYTHONPATH="$PWD/.python-packages:$PWD" bash run_zone5_trainer.sh
PYTHONPATH="$PWD/.python-packages:$PWD" python3 -m zone5.promote_model
PYTHONPATH="$PWD/.python-packages:$PWD" python3 smoke_test.py
```

Production retraining is owned by `.github/workflows/zone5-production-retrain.yml`
after the manual proof run succeeds and `ZONE5_ENABLE_SCHEDULED_RETRAIN=true`
is set. Keep `zone5-trainer.timer` disabled so GitHub and systemd do not both
launch retraining, and keep inline collector retraining disabled with
`RETRAIN_AFTER_SNAPSHOT=0` so training cannot block live appends. If the
production trainer is already active when the workflow starts, the workflow
leaves that run alone and exits successfully with an "already running" notice;
rerun it after the active trainer finishes if a packaged model delivery is
needed.

## 7. Verify And Roll Back

Check status after deploy:

```bash
systemctl --user status zone5-person-counter.service
systemctl --user status zone5-mqtt-aggregator.service
systemctl --user status zone5-sen55-collector.service
systemctl --user status zone5-live-collector.service
systemctl --user status zone5-trainer.timer
systemctl --user status zone5-trainer.service
systemctl --user status zone5-live-app.service
systemctl --user status zone5-vite-frontend.service
tail -n 100 ~/zone5_cv_time_features_package/logs/live_app.log
tail -n 100 ~/zone5_cv_time_features_package/logs/live_collector.log
tail -n 100 ~/zone5_cv_time_features_package/logs/zone5_trainer.log
curl -fsS http://127.0.0.1:8000/api/health
```

Roll back to a previous commit:

```bash
cd ~/zone5_cv_time_features_package
git log --oneline -n 10
git checkout PREVIOUS_COMMIT_SHA
python3 -m pip install --target .python-packages --upgrade -r requirements.txt
install -m 0644 systemd/user/zone5-*.service systemd/user/zone5-*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user restart zone5-person-counter.service
systemctl --user restart zone5-mqtt-aggregator.service
systemctl --user restart zone5-sen55-collector.service
systemctl --user restart zone5-live-collector.service
systemctl --user restart zone5-live-app.service
systemctl --user restart zone5-vite-frontend.service
systemctl --user disable --now zone5-trainer.timer
```

Return to normal tracking after rollback testing:

```bash
git checkout main
git pull --ff-only origin main
python3 -m pip install --target .python-packages --upgrade -r requirements.txt
install -m 0644 systemd/user/zone5-*.service systemd/user/zone5-*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user restart zone5-person-counter.service
systemctl --user restart zone5-mqtt-aggregator.service
systemctl --user restart zone5-sen55-collector.service
systemctl --user restart zone5-live-collector.service
systemctl --user restart zone5-live-app.service
systemctl --user restart zone5-vite-frontend.service
systemctl --user disable --now zone5-trainer.timer
```
