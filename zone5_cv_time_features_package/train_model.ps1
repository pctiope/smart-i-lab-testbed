param(
    [int]$NTrials = 50,
    [int]$MaxEpochs = 20,
    [int]$OptunaJobs = 0,
    [string]$OutputDir = "model",
    [ValidateSet("1", "2", "3")]
    [int]$CvFolds = 3,
    [switch]$AllowDegenerateValidation,
    [switch]$BootstrapFallback,
    [switch]$SkipPromote
)

$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot

Write-Error @"
This package-local training wrapper is archived in the desktop repo copy.

Do not use zone5.training or CSV inputs from zone5_cv_time_features_package/.
Use the repository-root DuckDB SQL-only pipeline described in README/README_training_migrated.md.
"@
exit 1

$trainArgs = @(
    "-m", "zone5.training",
    "--csv", "data\zone5_training_cv.csv",
    "--output-dir", $OutputDir,
    "--n-trials", $NTrials,
    "--max-epochs", $MaxEpochs,
    "--cv-folds", $CvFolds
)

if ($OptunaJobs -gt 0) {
    $trainArgs += @("--optuna-jobs", $OptunaJobs)
}
if ($AllowDegenerateValidation) {
    $trainArgs += "--allow-degenerate-validation"
}
if ($BootstrapFallback) {
    $trainArgs += "--bootstrap-fallback"
}

python @trainArgs

if (-not $SkipPromote -and $OutputDir -eq "model") {
    python -m zone5.promote_model --candidate-run $OutputDir
}
