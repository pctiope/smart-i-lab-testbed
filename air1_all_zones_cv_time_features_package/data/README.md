# Data Directory

Generated CV labels, SEN55 tables, AIR-1 exports, and long-form training files
are written here during normal operation. The package intentionally ships
without historical data.

Expected runtime files:

- `cv_occupancy_all_air1_10sec.csv`
- `cv_occupancy_all_air1_10sec.parquet`
- `sen55_data.csv`
- `sen55_data.parquet`
- `air1_all_zones_training_cv.csv`
- `air1_all_zones_training_cv.parquet`
- `air1_all_zones_training_cv.metadata.json`
- `person_counts_by_zone_cam1.csv`
- `person_counts_by_zone_cam2.csv`

The CV label table is long-form and keyed by `timestamp + zone_id`. Valid model
zones are 1-15 only. Table 16 is excluded/unlabeled and must not appear in the
training label table or web-app `ground_truth_by_zone` payload.

Missing per-zone labels should stay empty/null. Do not backfill missing camera
coverage as zero occupancy.
