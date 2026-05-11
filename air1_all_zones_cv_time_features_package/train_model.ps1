param(
    [int]$NTrials = 50,
    [int]$MaxEpochs = 20,
    [int]$OptunaJobs = 0,
    [string]$OutputDir = "model",
    [switch]$AllowDegenerateValidation,
    [switch]$SkipPromote
)

$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot

$trainArgs = @(
    "-m", "air1_all_zones.training",
    "--parquet", "data\air1_all_zones_training_cv.parquet",
    "--output-dir", $OutputDir,
    "--n-trials", $NTrials,
    "--max-epochs", $MaxEpochs
)

if ($OptunaJobs -gt 0) {
    $trainArgs += @("--optuna-jobs", $OptunaJobs)
}
if ($AllowDegenerateValidation) {
    $trainArgs += "--allow-degenerate-validation"
}

python @trainArgs

if (-not $SkipPromote -and $OutputDir -eq "model") {
    python -m air1_all_zones.promote_model --candidate-run $OutputDir
}


