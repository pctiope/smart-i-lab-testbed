# Model Directory

This package intentionally ships without a trained or promoted model.

It is normal to start the persistent collectors before this folder contains a
model. Hourly retraining may fail at first while data is still accumulating.
After a retrain succeeds, the live collector runs promotion automatically and
updates the production pointer only if the candidate passes.

Training creates:

- `model/runs/<run_id>/`
- `model/current_run.txt`

Promotion creates:

- `model/production_run.txt`

Until a real 10-second CV-target model is promoted, the web app reports that
the production pointer is missing.

The promoted model must use the all-zones CV contract:

- artifact: `best_cnn_all_zones.pt`
- target column: `occupied`
- grouping column: `zone_id`
- valid output zones: 1-15
- excluded/unlabeled camera table: Table 16
- inputs: AIR-1, shared SEN55, missing indicators, and time features
- no `zone_id`, mmWave, or smart-plug feature input

After promotion, the web app should expose per-zone probabilities through
`zone_probabilities` and aggregate readouts through `probability` and
`aggregate_probability`. It should not emit Table 16 as a model probability.


