# Model Directory

This package intentionally ships without a trained or promoted model. It is
normal to start the persistent collectors before this folder contains model
artifacts.

Generated runtime state:

- `runs/<run_id>/`: one training attempt with model weights, scaler stats,
  metrics, and manifest when training completes.
- `current_run.txt`: pointer to the latest completed candidate run.
- `production_run.txt`: pointer to the promoted run used by the live app.
- `retrain_status.json`: summary of the latest scheduled retrain attempt,
  including skipped evidence gates and split policy.
- `retrain.lock`: transient lock held while `zone5.retrain_once` is active.

The deployed systemd path trains through `zone5-trainer.timer`, not inside the
live collector. The timer runs `run_zone5_trainer.sh`, which snapshots the live
training CSV, trains, and then runs promotion. Early scheduled attempts may skip
or fail while data is still accumulating. Typical blockers are too few
blind-test positive windows, too few positive buckets/events, not enough
coverage-eligible strict-CV dates, or single-class train/validation windows.

Current runs must use `model_contract_version:
zone5_missingness_decoupled_v1`. The feature columns are raw sensors plus
deterministic time features only; old mmWave-recency runs are intentionally not
supported by the current app.

For the first production model, bootstrap fallback can be used only after strict
rolling validation has no viable lookbacks. Once `production_run.txt` exists,
future promoted candidates must come from strict validation. The default
blind-test evidence gates require at least 5 positive model windows, 5 positive
10-second buckets, and 1 contiguous positive event.

Promotion updates `production_run.txt` only when the candidate passes the CV
target contract, bootstrap/strict progression policy, blind-test evidence gates,
quality gates, smoke test, and same-window non-regression check. Until a real
10-second CV-target model is promoted, the web app reports that the production
pointer is missing.
