# Per-Zone Occupancy Ground Truth

## Current Contract

This package now uses mask-based per-zone CV labels for the shared AIR-1 all-zones model.

The label key is:

```text
timestamp + zone_id -> occupied
```

The shared model remains global. `zone_id` is retained for joins, grouping, audit, windows, and display, but it is not a model input feature.

## Operational Label Source

Run one tracker process per camera:

```powershell
.\run_person_counter.ps1 -Camera cam1
.\run_person_counter.ps1 -Camera cam2
```

or on Linux:

```bash
CAMERA=cam1 ./run_person_counter.sh
CAMERA=cam2 ./run_person_counter.sh
```

Defaults:

- `cam1`: `cam1-zones.json` + `masks/cam1-mask-zones.png`
- `cam2`: `cam2-zones.json` + `masks/cam2-mask-zones.png`
- MQTT topic: `care_ssl/all_zones/person_count_by_zone`

Camera coverage:

- `cam1`, host `10.158.71.241`: Tables 4 and 9-15 are model zones; Table 16 is excluded/unlabeled.
- `cam2`, host `10.158.71.240`: Tables 1-3 and 5-8 are model zones.

The PNG masks are the operational source of zone membership. The polygons in the JSON files are optional/editing aids and fallback geometry if a mask image is not supplied.

## Table Mapping

- `Table N` maps to AIR-1 `zone_id=N` for Tables 1-15.
- `Table 16` is excluded/unlabeled for now because the current model contract has only 15 AIR-1 zones.
- Invalid names such as `Desk 4` or unsupported table numbers are rejected.

## MQTT Payload

Each tracker publishes per-frame counts like:

```json
{
  "timestamp": "2026-05-08 12:00:00",
  "camera_id": "cam1",
  "frame_index": 12345,
  "counted_persons": 3,
  "counts_by_zone": {
    "4": 1,
    "9": 0,
    "10": 2
  },
  "unlabeled_zones": [16],
  "assignment_rule": "largest_overlap",
  "label_scope": "per_zone",
  "zone_map": "cam1-zones.json",
  "mask": "masks/cam1-mask-zones.png"
}
```

Each accepted person is assigned to exactly one model zone: the valid zone with the largest mask overlap.

## Label Table

The MQTT aggregator writes:

```text
data/cv_occupancy_all_air1_10sec.csv
data/cv_occupancy_all_air1_10sec.parquet
```

Required columns:

```text
timestamp, zone_id, occupancy_count, occupied, sample_count,
min_count, max_count, mean_count, median_count, last_count,
first_message_time, last_message_time, source_topic, camera_ids,
label_scope, label_source
```

Rules:

- One row is written per `timestamp + zone_id` label bucket.
- Counts from both camera streams merge into the same 10-second bucket.
- Missing/unmapped labels stay null. They are not converted to false zero.
- Table 16 is ignored by the label and training tables.

## Web-App Label Surface

The web app uses the same label table as the training path:

```text
data/cv_occupancy_all_air1_10sec.csv
```

Each inference event keeps aggregate compatibility fields:

```text
ground_truth_count
ground_truth_occupied
ground_truth_timestamp
ground_truth_age_minutes
```

It also exposes per-zone labels as:

```text
ground_truth_by_zone["1"] ... ground_truth_by_zone["15"]
```

Missing labels remain `null`; they are not rendered or exported as zero. Table
16 must not appear in `ground_truth_by_zone`.

The dashboard groups zones by camera coverage and shows the selected zone's
camera, model probability, CV count, occupied/clear label, label age, and label
timestamp. This is the operator check that per-zone labels are really arriving,
not just one aggregate count.

## Training Join

The CV training builder joins features and labels on:

```text
features.timestamp + features.zone_id
labels.timestamp + labels.zone_id
```

Rows with null `occupied` remain in storage. The training dataset code skips null targets when building supervised windows and metrics.

## Validation Before Training

Run these checks before treating a dataset as per-zone training data:

```powershell
@'
import pandas as pd
labels = pd.read_parquet("data/cv_occupancy_all_air1_10sec.parquet")
train = pd.read_parquet("data/air1_all_zones_training_cv.parquet")
print(labels.head())
print("label zones:", sorted(labels["zone_id"].dropna().astype(int).unique()))
print("training zones:", sorted(train["zone_id"].dropna().astype(int).unique()))
print(train.groupby("zone_id")["occupied"].agg(["count", "sum", "mean"]))
'@ | python -
```

Minimum checks:

- `zone_id` exists in CV labels and training data.
- Unique zones are only 1-15.
- Table 16 does not appear.
- labels are not identical across every zone at each timestamp.
- null labels remain null.
- cam1 and cam2 both contribute after both trackers have been running.
- `data/air1_all_zones_training_cv.metadata.json` reports `label_scope=per_zone` and join key `timestamp + zone_id`.
