# Zone 5 production run 20260524T020721Z_2639c699

This report summarizes the currently promoted Zone 5 production run without
tracking model binaries, runtime logs, or generated artifacts under
`zone5_cv_time_features_package/model/`.

## Sources

The metrics below were transcribed from the promoted run artifacts:

- `zone5_cv_time_features_package/model/production_run.txt`
- `zone5_cv_time_features_package/model/runs/20260524T020721Z_2639c699/manifest.json`
- `zone5_cv_time_features_package/model/runs/20260524T020721Z_2639c699/tables/metrics_zone_5.json`

`production_run.txt` points at `model/runs/20260524T020721Z_2639c699`.
The manifest records `metrics_zone_5.json` with SHA-256
`78eb9416d76732c7d4d1e48a2c2d46a2081c28af1a749848fd1ed252ef190ca1`.

## Run metadata

| Field | Value |
|---|---|
| Run ID | `20260524T020721Z_2639c699` |
| Created at | `2026-05-24T09:13:37.562034+00:00` |
| Git revision | `d9412a30645146cdd1e9a22a94c168f0f8b41a5a` |
| Zone | 5 |
| Target | `zone_occupied` |
| Model contract | `zone5_mmwave_recency_v1` |
| Feature channels | 22 |
| Raw sensor channels | 14 |
| Missing-indicator channels | 14, retained as diagnostics/metadata only |
| Sample interval | 10 seconds |
| Lookback | 90 rows / 15 minutes |
| Validation mode | `rolling_calendar` |
| CV folds requested/used | 3 / 3 |
| Blind-test date | May 23, 2026 |
| Threshold policy | Threshold-free raw probability output |
| SEN55 policy | Optional; `sen55_dropout_probability = 0.2` |

## Feature groups

The 22 model input channels are:

- AIR-1 Zone 5: `temp_s5`, `rh_s5`, `co2_s5`, `pm25_s5`
- Smart plug: `power_s5`
- mmWave raw and recency: `mmwave_s5`,
  `mmwave_s5_recent_1m_fraction`, `mmwave_s5_recent_3m_fraction`,
  `mmwave_s5_recent_5m_fraction`,
  `mmwave_s5_minutes_since_last_occupied`
- SEN55: `sen55_pm1_0`, `sen55_pm2_5`, `sen55_pm4_0`,
  `sen55_pm10_0`, `sen55_temperature`, `sen55_humidity`,
  `sen55_voc`, `sen55_nox`
- Time: `hour_sin`, `hour_cos`, `dow_sin`, `dow_cos`

## Split policy and sizes

| Split | Date range | Rows | Windows |
|---|---:|---:|---:|
| Pre-test / final training window | 2026-05-18 08:00:00 to 2026-05-22 23:59:50 | 40,268 | 40,062 |
| Blind test | 2026-05-23 00:00:00 to 2026-05-23 23:59:50 | 8,366 | 8,277 |

The split policy records `test_timestamps_in_cv_folds = 0`.

| CV fold | Train date range | Train rows | Validation date range | Validation rows | Validation windows |
|---|---:|---:|---:|---:|---:|
| fold 1 | 2026-05-19 00:00:00 to 2026-05-19 23:59:50 | 8,640 | 2026-05-20 00:00:00 to 2026-05-20 23:59:50 | 8,602 | 8,483 |
| fold 2 | 2026-05-19 00:00:00 to 2026-05-20 23:59:50 | 17,242 | 2026-05-21 00:00:00 to 2026-05-21 23:59:50 | 8,640 | 8,551 |
| fold 3 | 2026-05-19 00:00:00 to 2026-05-21 23:59:50 | 25,882 | 2026-05-22 00:00:00 to 2026-05-22 23:59:50 | 8,639 | 8,550 |

## AUC summary

| Evaluation window | PR-AUC | ROC-AUC |
|---|---:|---:|
| Pre-test / training-window evaluation | 0.8625 | 0.9839 |
| Blind test, May 23, 2026 | 0.7334 | 0.9230 |
| CV fold 1 validation | 0.0799 | 0.0966 |
| CV fold 2 validation | 0.8459 | 0.9676 |
| CV fold 3 validation | 0.9364 | 0.9821 |

Mean CV PR-AUC was `0.6183` with standard deviation `0.3876`. The metrics
file stores the mean and standard deviation for PR-AUC; it does not store an
aggregate mean ROC-AUC.

## Calibration, loss, and class balance

| Evaluation window | Brier score | BCE log-loss | Windows | Positive windows | Negative windows | Positive rate | Mean occupancy probability |
|---|---:|---:|---:|---:|---:|---:|---:|
| Pre-test / training-window evaluation | 0.0419 | 0.1438 | 40,062 | 4,735 | 35,327 | 11.82% | 0.1630 |
| Blind test, May 23, 2026 | 0.0934 | 0.4475 | 8,277 | 1,806 | 6,471 | 21.82% | 0.2814 |
| CV fold 1 validation | 0.0800 | 1.2912 | 8,483 | 678 | 7,805 | 7.99% | 0.0029 |
| CV fold 2 validation | 0.0585 | 0.1998 | 8,551 | 1,643 | 6,908 | 19.21% | 0.1734 |
| CV fold 3 validation | 0.0683 | 0.2891 | 8,550 | 2,154 | 6,396 | 25.19% | 0.3293 |

Additional split-level count metrics:

| Evaluation window | Positive buckets | Negative buckets | Positive events |
|---|---:|---:|---:|
| Pre-test / training-window evaluation | 4,735 | 35,533 | 207 |
| Blind test, May 23, 2026 | 1,806 | 6,560 | 262 |

## Confusion matrix

No TP/FP/TN/FN confusion matrix was produced for this run. The current deployed
model is threshold-free (`threshold_free = true`) and the run artifacts do not
store an operating threshold. Reporting a confusion matrix would require
inventing a threshold, so it is intentionally omitted.
