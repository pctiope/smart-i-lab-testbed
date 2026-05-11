param(
    [string]$OutputCsv = "data\sen55_data.csv",
    [string]$OutputParquet = "data\sen55_data.parquet"
)

$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot

python -m air1_all_zones.sen55_mqtt_collector --output-csv $OutputCsv --output-parquet $OutputParquet


