param(
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot

$env:AIR1_ALL_ZONES_SKIP_DOTENV = "1"
$env:AIR1_ALL_ZONES_DATA_SOURCE = "replay"
$env:AIR1_ALL_ZONES_REPLAY_PARQUET = "data\air1_all_zones_training_cv.parquet"
$env:AIR1_ALL_ZONES_PRODUCTION_POINTER = "model\production_run.txt"
$env:AIR1_ALL_ZONES_MAX_AGE_MINUTES = "none"
$env:AIR1_ALL_ZONES_TICK_INTERVAL_SEC = "1"

python -m uvicorn web_app.main:app --host $HostAddress --port $Port


