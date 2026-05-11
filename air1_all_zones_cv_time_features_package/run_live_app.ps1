param(
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot

if (-not $env:AIR1_ALL_ZONES_DATA_SOURCE) {
    $env:AIR1_ALL_ZONES_DATA_SOURCE = "live"
}
if (-not $env:AIR1_ALL_ZONES_PRODUCTION_POINTER) {
    $env:AIR1_ALL_ZONES_PRODUCTION_POINTER = "model\production_run.txt"
}
if (-not $env:AIR1_ALL_ZONES_MJPEG_TARGET_FPS) {
    $env:AIR1_ALL_ZONES_MJPEG_TARGET_FPS = "15"
}

python -m uvicorn web_app.main:app --host $HostAddress --port $Port


