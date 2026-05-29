# Zone 5 Production Retraining and Model Delivery

This runbook describes the CI/CD path for staging Compose deployment,
production retraining, and external model delivery. The current same-server
deployment uses a GitHub self-hosted runner on the Zone 5 server.

## Workflows

`Zone 5 Compose CI/CD` validates and deploys the staging Docker Compose stack
from `cicd/zone5-compose`. It does not replace the production user-systemd
services. CI runs on GitHub-hosted runners; deployment runs on the self-hosted
runner and writes to `~/smart-i-lab-testbed-compose`.

`Zone 5 Production Retrain` runs manually from `main`. It runs on the
self-hosted runner, disables the old `zone5-trainer.timer`, starts
`zone5-trainer.service`, waits for completion, packages a newly promoted run,
and installs it into the local production web app package. The workflow also
has a nightly 03:30 PHT cron, but scheduled retraining is gated until
`ZONE5_ENABLE_SCHEDULED_RETRAIN=true` is set as a repository variable.

`Zone 5 Model Delivery` is the external artifact handoff path. A training
server or model registry can upload a versioned model tarball, then trigger this
workflow with `workflow_dispatch` or `repository_dispatch`. The workflow runs on
the self-hosted runner and installs the downloaded artifact locally on the web
app server.

## Model Artifact Contract

The model artifact is a `.tar.gz` containing exactly one promoted run directory:

```text
model/runs/<run_id>/
  manifest.json
  models/best_cnn_zone_5.pt
  tables/best_params_zone_5.json
  tables/scaler_stats_zone_5.json
  tables/metrics_zone_5.json
```

The run metadata must use `model_contract_version:
zone5_missingness_decoupled_v1`. The contract contains raw sensor columns plus
deterministic time features only; mmWave recency columns from old
`zone5_mmwave_recency_v1` artifacts are not supported by the current live app.

The artifact must have a SHA-256 checksum. The web app server verifies the
checksum, runs the Zone 5 smoke test, installs the run under
`model/runs/<run_id>/`, then atomically writes:

```text
model/production_run.txt -> model/runs/<run_id>
```

The live app reloads the model from the pointer and the workflow polls
`/api/health` until `inference.model_run_id` matches `<run_id>`.

## Git Metadata

Git stores sanitized metadata only under `model_registry/`. Do not commit:

- model weights
- `model/production_run.txt`
- `model/current_run.txt`
- runtime `.env` files
- full `model/` contents

## Self-Hosted Runner

Install the repository self-hosted runner on the Zone 5 web app server as the
same user that owns the production checkout and user-systemd services. Use these
runner labels:

```text
self-hosted
linux
x64
zone5
smart-ilab
```

Current server convention:

```text
runner directory: ~/actions-runner-smart-ilab
service: actions.runner.pctiope-smart-i-lab-testbed.zone5-smart-ilab-ngrg-ric.service
docker drop-in: ~/.config/systemd/user/actions.runner.pctiope-smart-i-lab-testbed.zone5-smart-ilab-ngrg-ric.service.d/docker-exec.conf
```

The runner service should execute through the `docker` group so workflow jobs
can run `docker compose`:

```text
ExecStart=/usr/bin/sg docker -c /home/ngrg-user/actions-runner-smart-ilab/run.sh
```

Check the runner:

```bash
systemctl --user status actions.runner.pctiope-smart-i-lab-testbed.zone5-smart-ilab-ngrg-ric.service --no-pager
systemctl --user cat actions.runner.pctiope-smart-i-lab-testbed.zone5-smart-ilab-ngrg-ric.service
```

Restart the runner after changing the service or group membership:

```bash
systemctl --user daemon-reload
systemctl --user restart actions.runner.pctiope-smart-i-lab-testbed.zone5-smart-ilab-ngrg-ric.service
```

Keep the runner service scoped to trusted workflows. Do not run production
deployment jobs from arbitrary pull request code. GitHub runner registration
tokens are one-time credentials; do not commit them, paste them into issues, or
store them in repo docs.

## Health Checks

Run the bundled CI/CD health check from the production checkout:

```bash
bash zone5_cv_time_features_package/ci/check_zone5_cicd_health.sh
```

The script checks:

- self-hosted runner user service
- staging Compose services under `~/smart-i-lab-testbed-compose`
- staging backend and frontend proxy health on `8005` and `8016`
- production backend and frontend proxy health on `8000` and `8015`

These health endpoints prove service reachability, not necessarily live
inference output. `/api/health` with `ok=true`, the dashboard page, and video
availability show that the services are reachable. `/api/current.probability`
shows that the live app is producing an inference probability. When validating
model delivery or retraining, also check `/api/current` and inspect `error` if
`probability` is missing.

If `/api/current` reports `LIVE DATA DEGRADED: core sensor coverage below gate`,
the live app rejected inference because core sensor coverage is too low. AIR-1
and power fields require at least `0.80` coverage, and `mmwave_s5` requires at
least `0.95` coverage. `sen55-missing` is not the blocker by itself because
SEN55 is optional. The core AIR-1, smart plug, and mmWave fields gate live
prediction. Verify upstream Smart I-Lab API history for the Zone 5 devices
first; if the upstream history is healthy but the running app cache remains
stale, restart only the live app service.

The same checks can be run manually:

```bash
systemctl --user status actions.runner.pctiope-smart-i-lab-testbed.zone5-smart-ilab-ngrg-ric.service --no-pager
cd ~/smart-i-lab-testbed-compose/zone5_cv_time_features_package
docker compose ps
curl --noproxy '*' -fsS http://192.168.10.17:8005/api/health
curl --noproxy '*' -fsS http://192.168.10.17:8016/api/health
curl --noproxy '*' -fsS http://192.168.10.17:8000/api/health
curl --noproxy '*' -fsS http://192.168.10.17:8015/api/health
```

When staging must match the current production model, install the production run
under the staging model root and atomically set:

```text
~/smart-i-lab-testbed-compose/zone5_cv_time_features_package/model/production_run.txt
  -> model/runs/20260524T020721Z_2639c699
```

Then rebuild/recreate staging `live-app` from
`~/smart-i-lab-testbed-compose/compose.zone5-bsg.yaml` and verify both backend
health endpoints report:

```text
inference.model_run_id == 20260524T020721Z_2639c699
```

Also check `/api/current` on `8000` and `8005`; neither should report live
inference errors, and the staging `timestamp`, `reference_time`,
`ground_truth_timestamp`, and `sensor_context.window_end` fields should all be
Zone 5 local naive ISO strings.

## Automated Retraining Cadence

Default policy:

- prove `Zone 5 Production Retrain` once with a manual `workflow_dispatch`
- enable automated retraining only after that manual run succeeds
- run nightly at 03:30 PHT (`30 19 * * *` UTC)
- keep GitHub Actions as the only automated scheduler
- leave `zone5-trainer.timer` disabled while GitHub scheduled retraining is
  enabled

Enable the schedule after the manual proof by setting this repository variable:

```text
ZONE5_ENABLE_SCHEDULED_RETRAIN=true
```

Keep it unset or set to any value other than `true` to leave scheduled runs
disabled. Manual workflow dispatch continues to work either way.

Confirm the old systemd timer is disabled:

```bash
systemctl --user is-enabled zone5-trainer.timer
systemctl --user status zone5-trainer.timer --no-pager
```

If the timer is still enabled after GitHub scheduling is turned on, disable it:

```bash
systemctl --user disable --now zone5-trainer.timer
```

## Repository Secrets And Variables

Same-server staging deploy, production retrain, and local model delivery do not
need SSH secrets. Optional secrets:

- `MODEL_REGISTRY_TOKEN` for private artifact downloads

Optional repository variables:

- `ZONE5_TRAINING_PKG_ROOT`
- `ZONE5_WEBAPP_PKG_ROOT`
- `ZONE5_TRAINING_TIMEOUT_MINUTES`
- `ZONE5_PRODUCTION_HEALTH_URL`
- `ZONE5_ENABLE_SCHEDULED_RETRAIN`

If package-root variables are unset, the workflows use the production default:

```text
~/smart-i-lab-testbed/zone5_cv_time_features_package
```

For a future split-server setup, install the self-hosted runner on the web app
server and trigger `Zone 5 Model Delivery` with an artifact URL from the
training server or model registry.

## Triggering External Model Delivery

Manual dispatch:

```bash
gh workflow run zone5-model-delivery.yml \
  --ref main \
  -f run_id=20260523T000000Z_example \
  -f artifact_url=https://registry.example/zone5/zone5-model-20260523T000000Z_example.tar.gz \
  -f artifact_sha256=<64-hex-sha256> \
  -f artifact_uri=registry://zone5/20260523T000000Z_example
```

Repository dispatch from a training server:

```bash
curl -fsS -X POST \
  -H "Accept: application/vnd.github+json" \
  -H "Authorization: Bearer $GITHUB_TOKEN" \
  https://api.github.com/repos/<owner>/<repo>/dispatches \
  -d '{
    "event_type": "zone5_model_promoted",
    "client_payload": {
      "run_id": "20260523T000000Z_example",
      "artifact_url": "https://registry.example/zone5/zone5-model-20260523T000000Z_example.tar.gz",
      "artifact_sha256": "<64-hex-sha256>",
      "artifact_uri": "registry://zone5/20260523T000000Z_example"
    }
  }'
```

Use a non-expiring registry label for `artifact_uri`; do not commit presigned
download URLs to git.
