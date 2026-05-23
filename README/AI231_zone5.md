# AI231 Zone 5

## Active Training Zone

The current migrated training path is for Zone 5 only.

The root migrated pipeline name is `zone5`, and the active training-input table is `silver.zone5_training_input`.

## Minimum Relevant Sensor Scope

The Zone 5 migrated trainer uses these inputs:

- AIR-1 Zone 5 slice:
  - logical columns: `temp_s5`, `rh_s5`, `co2_s5`, `pm25_s5`
  - mapped AIR-1 physical sensor position: `s5`
  - current AIR-1 device ID for position 5: `87f510`
- Smart plug Zone 5 device:
  - `device_id = 9d88e7`
  - used as `power_s5`
- mmWave Zone 5 device:
  - `device_id = 89f464`
  - used as `mmwave_s5`
- SEN55-style support source in the current migrated runtime:
  - seeded from `ag-one`
  - current surrogate device ID: `6f31cc`
  - used to populate `silver.zone5_sen55`
- Label support table in the current migrated runtime:
  - `silver.zone5_cv_labels`
  - currently seeded from the Zone 5 mmWave source as a smoke/runtime surrogate

## Important Scope Limitation

The current root ingestion CLI can restrict by device type, but not by a single sensor ID inside `air-1`.

That means the minimum practical backup/bootstrap scope is:

- `air-1`
- `smart-plug-v2`
- `msr-2`
- `ag-one`

This still avoids the unrelated device types:

- `sensibo`
- `zigbee2mqtt`

## Recommended Zone 5-Only Bootstrap Commands

Run from the desktop repo root.

### cmd.exe

```bat
cd /d "C:\Users\Ethan\Desktop\smart-i-lab-testbed"
call "D:\AI 231\.venv\Scripts\activate.bat"
python api_ingestion.py --device-type air-1 --history-start "2026-05-01" --initialize
python api_ingestion.py --device-type smart-plug-v2 --history-start "2026-05-01" --initialize
python api_ingestion.py --device-type msr-2 --history-start "2026-05-01" --initialize
python api_ingestion.py --device-type ag-one --history-start "2026-05-01" --initialize
python seed_zone5_live_support_tables.py --rebuild --rebuild-training-input
```
### PowerShell

```powershell
Set-Location "C:\Users\Ethan\Desktop\smart-i-lab-testbed"
& "D:\AI 231\.venv\Scripts\Activate.ps1"
python api_ingestion.py --device-type air-1 --history-start "2026-05-01" --initialize
python api_ingestion.py --device-type smart-plug-v2 --history-start "2026-05-01" --initialize
python api_ingestion.py --device-type msr-2 --history-start "2026-05-01" --initialize
python api_ingestion.py --device-type ag-one --history-start "2026-05-01" --initialize
python seed_zone5_live_support_tables.py --rebuild --rebuild-training-input
```

## Recommended Zone 5 Live Refresh Commands

If the desktop repo is already initialized and you only want the relevant live updates, run one terminal per device type:

```bat
python api_ingestion.py --device-type air-1 --poll 5
python api_ingestion.py --device-type smart-plug-v2 --poll 5
python api_ingestion.py --device-type msr-2 --poll 5
python api_ingestion.py --device-type ag-one --poll 5
```

## Zone 5 Full Training Commands

Test output only:

```bat
python train_zone5_migrated.py --mode test --output-dir test_runs\zone5_full_train_test --rebuild --n-trials 50 --max-epochs 20 --cv-folds 3 --report-path test_runs\zone5_full_train_test\train_report.json
```

Live output folder:

```bat
python train_zone5_migrated.py --mode live --output-dir model --rebuild --n-trials 50 --max-epochs 20 --cv-folds 3 --report-path model\train_report.json
```

The only intended difference between the two commands is the output directory.
