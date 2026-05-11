param(
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot

$env:ZONE5_SKIP_DOTENV = "1"
$env:ZONE5_DATA_SOURCE = "replay"
if (-not $env:ZONE5_REPLAY_TABLE) {
    $env:ZONE5_REPLAY_TABLE = "data\zone5_training_cv.csv"
}
$env:ZONE5_PRODUCTION_POINTER = "model\production_run.txt"
$env:ZONE5_MAX_AGE_MINUTES = "none"
$env:ZONE5_TICK_INTERVAL_SEC = "1"

python -m uvicorn web_app.main:app --host $HostAddress --port $Port
