# Root Test Suite

This folder contains the active root-level tests for the DuckDB Bronze/Silver/Gold pipeline and the migrated Zone 5 runtime.

## Test Files

- `test_pipeline.py` — broad BSG storage and loader regression suite
- `test_live.py` — live API and Bronze freshness monitor
- `test_training_migrated.py` — SQL-only Zone 5 migration checks
- `test_train.py` — migrated smoke-frame comparison against the legacy Zone 5 contract

## Typical Commands

Run from the repository root.

### Focused migrated checks

```powershell
python -m unittest TEST.test_training_migrated -v
python -m unittest TEST.test_train -v
```

### Broader regression suite

```powershell
python -m unittest TEST.test_pipeline -v
```

### Live monitor

```powershell
python TEST/test_live.py --all --check-only
python TEST/test_live.py --all
```
