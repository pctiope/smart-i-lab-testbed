# AIR-1 All-Zones Package Gap Audit

Audit refreshed: 2026-05-08.

## Bottom Line

The `air1_all_zones_cv_time_features_package/` refactor is code-complete enough to start operational collection, but it is not production-ready yet. The package layout exists, the source compiles, the package unit-test module passes under `unittest`, and `data/` plus `model/` intentionally contain source-only placeholder README files.

What is still lacking is operational evidence: a real all-zones training dataset from the new mask-based per-zone labels, a promoted production model, first baseline metrics, and a live end-to-end run against the lab AIR-1 API, MQTT broker, SEN55 stream, both RTSP cameras, and web app.

## What Is Already Done

- Package path exists: `air1_all_zones_cv_time_features_package/`.
- The package uses long-form rows: one `timestamp` plus one `zone_id` per row.
- The feature contract is AIR-1 + SEN55 + missingness/time features. It excludes mmWave and smart-plug features.
- Person counting now uses `rtsp_zone_tracker.py` with one process per camera and publishes mask-based per-zone counts to `care_ssl/all_zones/person_count_by_zone`.
- The CV target path is per-zone. Labels join on `timestamp + zone_id`, with missing/unmapped zones kept null.
- `Table N` maps to `zone_id=N` for Tables 1-15. `Table 16` is excluded/unlabeled.
- Runtime defaults and docs now use `AIR1_ALL_ZONES_RTSP_URL_CAM1` for camera host `10.158.71.241` and `AIR1_ALL_ZONES_RTSP_URL_CAM2` for camera host `10.158.71.240`.
- `web_app/.env`, `.env.example`, Docker Compose, and service docs point person-count and occupancy aggregation to `care_ssl/all_zones/person_count_by_zone`.
- The web app now runs two RTSP grabbers. It exposes `/api/video/cam1.mjpg` and `/api/video/cam2.mjpg`, keeps `/api/video.mjpg` as a cam1 compatibility alias, and reports both camera statuses in `/api/health` as `rtsp_by_camera`.
- Current/history events expose `zone_probabilities`, max probability, mean probability, `occupied_zones`, aggregate CV fields, and `ground_truth_by_zone` for model zones 1-15.
- The dashboard is an all-zones operations view: top summary, cam1/cam2 feeds, grouped zone grid by camera coverage, selected-zone detail, and history for max probability, mean probability, and occupied-zone count.
- Default data outputs are defined:
  - `data/cv_occupancy_all_air1_10sec.csv`
  - `data/cv_occupancy_all_air1_10sec.parquet`
  - `data/sen55_data.csv`
  - `data/sen55_data.parquet`
  - `data/air1_all_zones_training_cv.csv`
  - `data/air1_all_zones_training_cv.parquet`
  - `data/air1_all_zones_training_cv.metadata.json`
- Default model pointer is defined as `model/production_run.txt`.
- Current local verification:
  - `python -m py_compile rtsp_zone_tracker.py` passed.
  - `python -m compileall air1_all_zones web_app tests smoke_test.py rtsp_zone_tracker.py` passed.
  - `python -m unittest discover -s tests -p "test*.py" -v` passed 27 tests.
  - PowerShell launcher parse checks passed.
  - `bash -n` checks passed for the shell launchers.
  - `node --check web_app\static\app.js` passed.
  - Source/config entrypoint files were checked for UTF-8 BOMs, and the regression test now guards the Linux entrypoints plus Docker files.

## Logic And Consistency Check

- Per-zone labels are the only current production CV target path: `rtsp_zone_tracker.py` emits `counts_by_zone`, the aggregator writes one row per `timestamp + zone_id`, and training joins on that same key.
- Missing camera/zone coverage stays nullable through the label table and training join; it is not converted into false zero occupancy.
- The model remains one shared all-zones model. `zone_id` is retained as grouping/audit metadata and is excluded from model features.
- Table 16 is consistently treated as visible-but-unlabeled and is filtered out of model labels.
- Table 16 is also filtered out of web-app model output and `ground_truth_by_zone`; it should appear only as excluded/unlabeled metadata.
- Cam2 failures are now operationally visible because health and the dashboard show cam2 independently from cam1.
- Remaining `full-frame` wording is limited to the legacy single-mask script under `cv_counter/`, not the production mask-zone tracker path.
- Regression tests guard against old one-zone topic or mask leakage back into this all-zones package.
- Runtime readiness still depends on live validation with real RTSP, MQTT, SEN55, AIR-1 API, and a promoted model; the code and docs are coherent, but live services have not been exercised in this shell.

## What Is Still Missing

- No real collected all-zones training dataset exists yet. `data/` only has `README.md`.
- No real collected per-zone label dataset exists yet. The code path is present, but it still needs a live run from both cameras to produce usable labels.
- No promoted production model exists yet. `model/` only has `README.md`; there is no `model/production_run.txt`.
- No first real baseline metrics exist from training/promotion. Until a real training run completes, there is no trustworthy PR-AUC, ROC-AUC, Brier score, log loss, or positive-window count.
- No data-sufficiency confirmation exists for the default chronological split and positive-window requirements. The first dataset must prove there are enough timestamps, enough per-zone windows, and enough positive occupied windows to support validation and smoke testing.
- No live end-to-end validation has been run yet against:
  - AIR-1 API
  - MQTT broker
  - SEN55 MQTT stream
  - cam1 RTSP stream
  - cam2 RTSP stream
  - CV person-count topic
  - all-zones live collector
  - web app inference loop
- No dependency/container smoke test has been completed with installed requirements or Docker Compose.
- `web_app/.env` is package-local and contains lab endpoint/credential variables. Treat this bundle as internal-only until the file is removed, redacted, or replaced by a sanitized `.env.example`.

## Recommended Next Steps

From the CARE-SSL workspace root, enter the package first:

```powershell
Set-Location .\air1_all_zones_cv_time_features_package
```

Start both RTSP zone trackers:

```powershell
.\run_person_counter.ps1 -Camera cam1
.\run_person_counter.ps1 -Camera cam2
```

Start the SEN55 collector:

```powershell
.\run_sen55_collector.ps1
```

Aggregate person-count MQTT messages into 10-second CV labels:

```powershell
python -m air1_all_zones.occupancy_mqtt_aggregator `
  --output-csv data\cv_occupancy_all_air1_10sec.csv `
  --output-parquet data\cv_occupancy_all_air1_10sec.parquet
```

Start the AIR-1 + SEN55 + CV-label training-data collector:

```powershell
.\run_live_collector.ps1
```

Before treating the result as a per-zone occupancy model, validate the label table: it must include `zone_id`, contain only zones 1-15, preserve nulls for missing labels, and show zone-specific differences across timestamps.

After enough data has accumulated, train and attempt first promotion:

```powershell
.\train_model.ps1
```

Run the model smoke test after a candidate or production run exists:

```powershell
python .\smoke_test.py --candidate-run model --production-pointer model\production_run.txt
```

Launch replay mode after `data/air1_all_zones_training_cv.parquet` and `model/production_run.txt` exist:

```powershell
.\run_replay_app.ps1
```

Launch live app mode only after the production pointer and live dependencies are valid:

```powershell
.\run_live_app.ps1
```

When the app is running, validate the API/dashboard contract:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/health |
  Select-Object -ExpandProperty rtsp_by_camera
```

Then open:

```text
http://127.0.0.1:8000/api/video/cam1.mjpg
http://127.0.0.1:8000/api/video/cam2.mjpg
```

Both camera feeds should be independent, and the dashboard should show
per-zone CV labels rather than only one aggregate person count.

## Verification To Run After Data Exists

- Confirm data files are no longer placeholders:
  - `data/cv_occupancy_all_air1_10sec.csv`
  - `data/cv_occupancy_all_air1_10sec.parquet`
  - `data/sen55_data.csv`
  - `data/sen55_data.parquet`
  - `data/air1_all_zones_training_cv.csv`
  - `data/air1_all_zones_training_cv.parquet`
  - `data/air1_all_zones_training_cv.metadata.json`
- Inspect `data/air1_all_zones_training_cv.metadata.json` for row count, timestamp span, SEN55 availability, CV-label availability, failed AIR-1 gaps, and output hashes.
- Confirm the training split has enough chronological coverage and enough positive occupied windows for the default smoke-test guardrail.
- Confirm `model/current_run.txt`, `model/runs/<run_id>/`, and `model/production_run.txt` are created only after a valid training/promotion path.
- Confirm the promoted run contains `best_cnn_all_zones.pt`, scaler stats, best parameters, metrics, and a manifest with the AIR-1 all-zones CV target contract.
- Run local verification with installed requirements:

```powershell
python -m compileall air1_all_zones web_app tests smoke_test.py rtsp_zone_tracker.py
python -m unittest discover -s tests -p "test*.py" -v
node --check web_app\static\app.js
python .\smoke_test.py --candidate-run model --production-pointer model\production_run.txt
```

- Run Docker Compose checks after Docker and dependencies are available:

```powershell
docker compose --profile ops run --rm compile-check
docker compose --profile ops run --rm unit-tests
docker compose --profile ops run --rm smoke-test
```

- Run a full containerized live stack only after `.env` has been intentionally kept internal or sanitized:

```powershell
docker compose up --build person-counter person-counter-cam2 mqtt-aggregator sen55-collector live-collector live-app
```

## Notes Before Sharing

- Do not share `web_app/.env` outside the lab/internal handoff path. It contains lab endpoint and credential variables.
- If the package is shared externally, remove `web_app/.env`, provide a redacted `.env.example`, and document which values the operator must supply.
- Do not claim model readiness until `model/production_run.txt` exists and points to a promoted run with smoke-test evidence.
- Do not claim live readiness until the person counter, occupancy aggregator, SEN55 collector, AIR-1 collector, production model, and web app have been run together against the real lab services.
