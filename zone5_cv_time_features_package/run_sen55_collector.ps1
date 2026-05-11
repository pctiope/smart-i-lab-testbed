param(
    [string]$OutputCsv = "data\sen55_data.csv"
)

$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot

python -m zone5.sen55_mqtt_collector --output-csv $OutputCsv
