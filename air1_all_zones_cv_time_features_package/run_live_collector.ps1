param(
    [int]$DurationMin = 1440,
    [double]$AppendEverySec = 10,
    [double]$BackfillSec = 120,
    [double]$ParquetRebuildEveryHours = 1,
    [switch]$RetrainAfterParquet,
    [switch]$PromoteAfterRetrain,
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
    "-m", "air1_all_zones.collect_training_data",
    "--live-append",
    "--duration-min", $DurationMin,
    "--append-every-sec", $AppendEverySec,
    "--backfill-sec", $BackfillSec,
    "--parquet-rebuild-every-hours", $ParquetRebuildEveryHours
)

if ($NoPromoteAfterRetrain) {
    $PromoteAfterRetrain = $false
}

$retrainEnv = if ($env:RETRAIN_AFTER_PARQUET) { $env:RETRAIN_AFTER_PARQUET.Trim().ToLowerInvariant() } else { "" }
if ($RetrainAfterParquet -or @("1", "true", "yes", "on") -contains $retrainEnv) {
    $collectorArgs += "--retrain-after-parquet"
} else {
    $collectorArgs += "--no-retrain-after-parquet"
}

$promoteEnv = if ($env:PROMOTE_AFTER_RETRAIN) { $env:PROMOTE_AFTER_RETRAIN.Trim().ToLowerInvariant() } else { "" }
if (($RetrainAfterParquet -or @("1", "true", "yes", "on") -contains $retrainEnv) -and ($PromoteAfterRetrain -or @("1", "true", "yes", "on") -contains $promoteEnv)) {
    $collectorArgs += "--promote-after-retrain"
} else {
    $collectorArgs += "--no-promote-after-retrain"
}

python @collectorArgs


