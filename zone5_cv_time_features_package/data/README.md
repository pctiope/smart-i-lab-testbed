# Data Directory

Runtime collectors write package-local CSV tables and metadata here. These
files do not need to exist before persistent services start; the collectors
create them as live data arrives.

Expected generated files:

- `person_counts.csv`: raw CV person-count audit stream from the RTSP counter.
- `cv_occupancy_zone5_10sec.csv`: 10-second CV occupancy labels from the MQTT
  aggregator.
- `sen55_data.csv`: 10-second SEN55 buckets.
- `zone5_training_cv.csv`: joined AIR-1, smart plug, mmWave, SEN55, and CV
  label table used by the trainer.
- `zone5_training_cv.metadata.json`: metadata for the joined table.
- `training_snapshots/`: immutable Parquet snapshots created by
  `run_zone5_trainer.sh` before each retrain.

The joined table stores raw sensor columns and audit missingness columns.
Training derives only deterministic time features for the model; it no longer
derives or persists mmWave-recency model inputs.

The deployed systemd trainer snapshots `zone5_training_cv.csv` by default so it
can train from the freshest joined data without blocking live appends. Older
unreferenced files in `training_snapshots/` can be removed after confirming
they are not referenced by `model/retrain_status.json` or a model manifest.

Top-level `data/*.parquet` files are not part of the active runtime contract.
If one appears, treat it as a stale manual artifact unless a specific model
manifest or snapshot metadata file references it.
