# Training Migration Notes

This document tracks the root-folder migration away from the Legacy Zone 5 file-based pipeline and into the shared Bronze/Silver/Gold DuckDB layout.

## Current Scope

The migrated root pipeline now has a BSG-only runtime path for Zone 5 training input and training output.

The former legacy file artifacts are no longer part of the migrated runtime contract.

Runtime tables:

- silver.zone5_cv_labels
- silver.zone5_sen55
- silver.zone5_training_input
- silver.zone5_training_output
- gold.zone5_training_output

The root helper module is [../zone5_training_migrated.py](../zone5_training_migrated.py). The runtime builder `build_zone5_training_input_from_silver()` reads only DuckDB BSG tables and writes the migrated training input back into Silver.

## Data Flow

1. Raw device histories stay in Bronze.
2. Device preprocessing belongs in [../bronze2silver_preprocess.py](../bronze2silver_preprocess.py).
3. Training-ready inputs and training outputs are stored in Silver.
4. If a post-processing step exists, keep it in [../silver2gold_preprocess.py](../silver2gold_preprocess.py).
5. If no post-processing exists, copy only the output dump from Silver to Gold.

The training-specific hooks now present in the root are:

- [../bronze2silver_preprocess.py](../bronze2silver_preprocess.py) `run_zone5_training_preprocess()`
- [../silver2gold_preprocess.py](../silver2gold_preprocess.py) `run_zone5_training_postprocess()`

Current status:

- Pre-training data preparation is migrated to the Bronze -> Silver side through `run_zone5_training_preprocess()`.
- Post-training output handling is migrated to the Silver -> Gold side through `run_zone5_training_postprocess()`.
- No extra post-processing is currently defined for Zone 5 output, so only the output dump is copied to Gold.

For the current Zone 5 migration helpers, the table layout is:

- silver.zone5_cv_labels
- silver.zone5_sen55
- silver.zone5_training_input
- silver.zone5_training_output
- gold.zone5_training_output

## Runtime DuckDB Path

The migrated runtime no longer requires CSV files for Zone 5 smoke/input preparation.

- Build training input from BSG tables with [../zone5_training_migrated.py](../zone5_training_migrated.py) `build_zone5_training_input_from_silver()`.
- Build the smoke-test window from Silver with [../zone5_training_migrated.py](../zone5_training_migrated.py) `build_zone5_smoke_frame_from_silver()`.
- Write model outputs to Silver with [../zone5_training_migrated.py](../zone5_training_migrated.py) `write_training_output_to_silver()`.
- Copy only the output dump to Gold with [../zone5_training_migrated.py](../zone5_training_migrated.py) `copy_training_output_to_gold()`.
- Run full training from Silver with [../train_zone5_migrated.py](../train_zone5_migrated.py) `train_zone5_from_silver()`.

## Full Training Commands

The migrated root trainer supports both test and live runs. The training source remains DuckDB Silver; the run output directory is the isolation boundary.

Test run:

```powershell
python train_zone5_migrated.py --mode test --output-dir test_runs\zone5_full_train_test --rebuild --n-trials 50 --max-epochs 20 --cv-folds 3 --report-path test_runs\zone5_full_train_test\train_report.json
```

Live run:

```powershell
python train_zone5_migrated.py --mode live --output-dir model --rebuild --n-trials 50 --max-epochs 20 --cv-folds 3 --report-path model\train_report.json
```

The trainer rebuilds `silver.zone5_training_input`, snapshots the labeled Silver table to a Parquet artifact under the selected output directory, and runs the full model-training flow from that snapshot. It does not depend on CSV inputs.

## SQL-Only Validation Handling

Validation now checks the migrated DuckDB path directly.

- Use [../CSV Training Data Code.py](../CSV%20Training%20Data%20Code.py) `upsert_table_dataframe()` to seed test tables.
- Use [../dataloader.py](../dataloader.py) `load_table()` or `load_training_table()` to read migrated outputs back through SQL.

## Test Coverage

The migration validation suite is [../TEST/test_training_migrated.py](../TEST/test_training_migrated.py).

The richer smoke-flow comparison harness is [../TEST/test_train.py](../TEST/test_train.py).

It currently checks:

- SQL-only training-input build from Silver tables.
- Upsert parity on the Silver training-input table.
- Output-only gold copy: only silver.zone5_training_output is copied to Gold.
- Quality-report coverage for schema, dtype, and null diagnostics.
- Legacy smoke-flow parity: legacy smoke input columns, dtypes, and null counts match the migrated BSG smoke frame.
- Migrated smoke diagnostics: prints schema, dtype, null, NaN, and non-finite summaries for inspection.

## Validation Commands

Run the focused migration checks from the project root:

```powershell
python -m py_compile "CSV Training Data Code.py" dataloader.py zone5_training_migrated.py train_zone5_migrated.py TEST\test_training_migrated.py TEST\test_train.py
python -m unittest TEST.test_training_migrated -v
python -m unittest TEST.test_train -v
```

Run the broader storage regression suite if needed:

```powershell
python -m unittest TEST.test_pipeline -v
```

## Testing Log Template

Use this short checklist while iterating on the migration:

- [ ] `python -m py_compile ...` passes for all touched files.
- [ ] `python -m unittest TEST.test_training_migrated -v` passes.
- [ ] `python -m unittest TEST.test_train -v` passes.
- [ ] `python -m unittest TEST.test_pipeline -v` still passes.
- [ ] Silver tables contain the expected migrated Zone 5 rows.
- [ ] Gold contains only the promoted training output dump, not the intermediate tables.

## Next Root Migration Steps

1. Persist model metadata and prediction history in Silver with explicit table contracts.
2. Add any required post-processing into [../silver2gold_preprocess.py](../silver2gold_preprocess.py) before widening Gold.