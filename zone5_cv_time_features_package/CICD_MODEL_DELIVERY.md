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
and installs it into the local production web app package.

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

Keep the runner service scoped to trusted workflows. Do not run production
deployment jobs from arbitrary pull request code.

## Repository Secrets And Variables

Same-server staging deploy, production retrain, and local model delivery do not
need SSH secrets. Optional secrets:

- `MODEL_REGISTRY_TOKEN` for private artifact downloads

Optional repository variables:

- `ZONE5_TRAINING_PKG_ROOT`
- `ZONE5_WEBAPP_PKG_ROOT`
- `ZONE5_TRAINING_TIMEOUT_MINUTES`
- `ZONE5_PRODUCTION_HEALTH_URL`

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
