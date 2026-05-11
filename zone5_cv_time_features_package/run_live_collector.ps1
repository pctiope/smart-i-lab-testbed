param(
    [int]$DurationMin = 1440,
    [double]$AppendEverySec = 10,
    [double]$BackfillSec = 120,
    [double]$SnapshotRefreshEveryHours = 1,
    [switch]$RetrainAfterSnapshot,
    [ValidateSet("auto", "always", "never")]
    [string]$RetrainBootstrapFallback = "auto",
    [ValidateSet("auto", "1", "2", "3")]
    [string]$RetrainCvFolds = "auto",
    [switch]$NoPromoteAfterRetrain
)

$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot

if (-not $env:AIR1_API_URL) {
    $env:AIR1_API_URL = "http://10.158.66.30:80"
}
if (-not $env:AIR1_API_KEY) {
    $env:AIR1_API_KEY = "9c5c3569-cfe7-42ae-bf00-e86ae08519ef"
}

$collectorArgs = @(
    "-m", "zone5.collect_training_data",
    "--live-append",
    "--duration-min", $DurationMin,
    "--append-every-sec", $AppendEverySec,
    "--backfill-sec", $BackfillSec,
    "--snapshot-refresh-every-hours", $SnapshotRefreshEveryHours,
    "--retrain-bootstrap-fallback", $RetrainBootstrapFallback,
    "--retrain-cv-folds", $RetrainCvFolds
)

if ($RetrainAfterSnapshot) {
    $collectorArgs += "--retrain-after-snapshot"
} else {
    $collectorArgs += "--no-retrain-after-snapshot"
}

if ($NoPromoteAfterRetrain) {
    $collectorArgs += "--no-promote-after-retrain"
}

python @collectorArgs
