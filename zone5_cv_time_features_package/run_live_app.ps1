param(
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot

if (-not $env:ZONE5_DATA_SOURCE) {
    $env:ZONE5_DATA_SOURCE = "live"
}
if (-not $env:ZONE5_PRODUCTION_POINTER) {
    $env:ZONE5_PRODUCTION_POINTER = "model\production_run.txt"
}
if (-not $env:ZONE5_MJPEG_TARGET_FPS) {
    $env:ZONE5_MJPEG_TARGET_FPS = "15"
}

python -m uvicorn web_app.main:app --host $HostAddress --port $Port
