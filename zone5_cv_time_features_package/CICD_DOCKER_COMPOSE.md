# CI/CD Tutorial: Docker Compose Deployment

This tutorial turns this folder into a simple CI/CD project that tests every
change and deploys the live Zone 5 stack to an Ubuntu server with Docker
Compose.

Use this path when the server should run the services from `docker-compose.yml`:

- `person-counter`
- `mqtt-aggregator`
- `sen55-collector`
- `live-collector`
- `live-app`

Compose does not include an hourly trainer timer. The live services collect and
serve data; training and promotion are ops commands unless you explicitly enable
inline retraining with `RETRAIN_AFTER_SNAPSHOT=1`.

Runtime state stays on the server in `data/`, `model/`, `logs/`, and
`web_app/.env`. CI should test code and build the image; it should not train or
promote production models on every commit. Runtime services write CSV tables;
only trainer ops write immutable snapshots under `data/training_snapshots/`.

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

Install Docker and the Compose plugin on the server, then clone the repo:

```bash
cd ~
git clone git@github.com:YOUR_ORG/YOUR_REPO.git zone5_cv_time_features_package
cd ~/zone5_cv_time_features_package
```

Create runtime directories:

```bash
mkdir -p data model logs
```

Create the root `.env` file used by Compose for host user ownership:

```bash
printf "ZONE5_UID=$(id -u)\nZONE5_GID=$(id -g)\n" > .env
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

Validate and start manually once before automating deploys:

```bash
docker compose config --quiet
docker compose build
docker compose up -d person-counter mqtt-aggregator sen55-collector live-collector live-app
curl -fsS http://127.0.0.1:8000/api/health
```

The app may report that `model/production_run.txt` is missing until enough real
data exists and the server-side trainer/promoter ops commands create the
production pointer.

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

      - name: Create CI-only Compose env file
        run: touch web_app/.env

      - name: Validate Compose config
        run: docker compose config --quiet

      - name: Build Docker image
        run: docker compose build
```

This CI checks the code and proves the Docker image can be built. It does not
start live services because the hosted runner cannot reach the lab camera, MQTT
broker, AIR-1 API, or your server runtime files.

## 4. Add GitHub Actions Deploy

Add these repository secrets in GitHub:

```text
SSH_HOST       example: zone5-server.example.com
SSH_USER       example: ubuntu
SSH_KEY        private SSH key that can log in as SSH_USER
SSH_PORT       optional, defaults to 22
```

Create:

```text
.github/workflows/deploy-compose.yml
```

Use:

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
          mkdir -p data model logs
          docker compose config --quiet
          docker compose build
          docker compose up -d --force-recreate person-counter mqtt-aggregator sen55-collector live-collector live-app
          docker compose ps
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

docker_build:
  stage: test
  image: docker:27
  services:
    - docker:27-dind
  variables:
    DOCKER_HOST: tcp://docker:2375
    DOCKER_TLS_CERTDIR: ""
  before_script:
    - apk add --no-cache docker-cli-compose
    - touch web_app/.env
  script:
    - docker compose config --quiet
    - docker compose build

deploy_compose:
  stage: deploy
  image: alpine:3.20
  needs: [test, docker_build]
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
      mkdir -p data model logs
      docker compose config --quiet
      docker compose build
      docker compose up -d --force-recreate person-counter mqtt-aggregator sen55-collector live-collector live-app
      docker compose ps
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
docker compose --profile ops run --rm trainer
docker compose --profile ops run --rm promoter
docker compose --profile ops run --rm smoke-test
```

The live collector can also retrain and promote after hourly metadata refreshes
only when inline retraining is explicitly enabled, for example
`RETRAIN_AFTER_SNAPSHOT=1` and `PROMOTE_AFTER_RETRAIN=1` in `web_app/.env`.
Keep the default off for normal CI/CD so collection is not blocked by training.

## 7. Verify And Roll Back

Check status after deploy:

```bash
cd ~/zone5_cv_time_features_package
docker compose ps
docker compose logs --tail 100 live-app
docker compose logs --tail 100 live-collector
curl -fsS http://127.0.0.1:8000/api/health
```

Roll back to a previous commit:

```bash
cd ~/zone5_cv_time_features_package
git log --oneline -n 10
git checkout PREVIOUS_COMMIT_SHA
docker compose build
docker compose up -d --force-recreate person-counter mqtt-aggregator sen55-collector live-collector live-app
```

Return to normal tracking after rollback testing:

```bash
git checkout main
git pull --ff-only origin main
docker compose build
docker compose up -d --force-recreate person-counter mqtt-aggregator sen55-collector live-collector live-app
```
