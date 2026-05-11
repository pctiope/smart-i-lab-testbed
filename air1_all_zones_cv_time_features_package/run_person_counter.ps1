param(
    [ValidateSet("cam1", "cam2")][string]$Camera = $(if ($env:PERSON_COUNT_CAMERA_ID) { $env:PERSON_COUNT_CAMERA_ID } else { "cam1" }),
    [string]$Source = "",
    [string]$Script = "rtsp_zone_tracker.py",
    [string]$Zones = $(if ($env:PERSON_COUNT_ZONE_MAP) { $env:PERSON_COUNT_ZONE_MAP } else { "" }),
    [string]$Mask = $(if ($env:PERSON_COUNT_MASK) { $env:PERSON_COUNT_MASK } else { "" }),
    [string]$Model = "cv_counter\models\headtracker-m.pt",
    [string]$Tracker = "cv_counter\trackers\botsort.yaml",
    [string]$Device = "cpu",
    [string]$CountsCsv = "",
    [string]$Python = $(if ($env:PYTHON_BIN) { $env:PYTHON_BIN } else { "python" }),
    [string]$MqttBroker = $(if ($env:PERSON_COUNT_MQTT_BROKER) { $env:PERSON_COUNT_MQTT_BROKER } else { "10.158.71.19" }),
    [int]$MqttPort = $(if ($env:PERSON_COUNT_MQTT_PORT) { [int]$env:PERSON_COUNT_MQTT_PORT } else { 1883 }),
    [string]$MqttTopic = $(if ($env:PERSON_COUNT_MQTT_TOPIC) { $env:PERSON_COUNT_MQTT_TOPIC } else { "care_ssl/all_zones/person_count_by_zone" }),
    [string]$MqttUsername = $(if ($env:PERSON_COUNT_MQTT_USERNAME) { $env:PERSON_COUNT_MQTT_USERNAME } else { "guest" }),
    [string]$MqttPassword = $(if ($env:PERSON_COUNT_MQTT_PASSWORD) { $env:PERSON_COUNT_MQTT_PASSWORD } else { "smartilab123" }),
    [int]$MqttEvery = 1,
    [int]$CountsEvery = 1
)

$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot

$PackagePythonPath = Join-Path $PSScriptRoot ".python-packages"
if (Test-Path -LiteralPath $PackagePythonPath) {
    $env:PYTHONPATH = "$PackagePythonPath;$PSScriptRoot" + $(if ($env:PYTHONPATH) { ";$env:PYTHONPATH" } else { "" })
}
if (-not $env:YOLO_AUTOINSTALL) {
    $env:YOLO_AUTOINSTALL = "False"
}

function Resolve-RequiredFile {
    param(
        [Parameter(Mandatory=$true)][string]$Label,
        [Parameter(Mandatory=$true)][string]$PathValue
    )
    $resolved = Resolve-Path -LiteralPath $PathValue -ErrorAction SilentlyContinue
    if (-not $resolved) {
        throw "$Label not found: $PathValue. The AIR-1 all zones package must include tracker assets before starting the person counter."
    }
    return $resolved.Path
}

if ([string]::IsNullOrWhiteSpace($Zones)) {
    $Zones = if ($Camera -eq "cam1") { "cam1-zones.json" } else { "cam2-zones.json" }
}
if ([string]::IsNullOrWhiteSpace($Mask)) {
    $Mask = if ($Camera -eq "cam1") { "masks\cam1-mask-zones.png" } else { "masks\cam2-mask-zones.png" }
}
if ([string]::IsNullOrWhiteSpace($CountsCsv)) {
    $CountsCsv = "data\person_counts_by_zone_$Camera.csv"
}
if ([string]::IsNullOrWhiteSpace($Source)) {
    if ($Camera -eq "cam1") {
        $Source = $(if ($env:AIR1_ALL_ZONES_RTSP_URL_CAM1) { $env:AIR1_ALL_ZONES_RTSP_URL_CAM1 } else { "rtsp://admin:++smartilab2023@10.158.71.241:554/Streaming/channels/101" })
    } else {
        $Source = $(if ($env:AIR1_ALL_ZONES_RTSP_URL_CAM2) { $env:AIR1_ALL_ZONES_RTSP_URL_CAM2 } else { "rtsp://admin:++smartilab2023@10.158.71.240:554/Streaming/channels/101" })
    }
}
if ([string]::IsNullOrWhiteSpace($Source)) {
    throw "Pass -Source or set AIR1_ALL_ZONES_RTSP_URL_$($Camera.ToUpperInvariant()) for $Camera."
}

$ScriptPath = Resolve-RequiredFile -Label "Zone tracker script" -PathValue $Script
$ZonesPath = Resolve-RequiredFile -Label "Zone map" -PathValue $Zones
$MaskPath = Resolve-RequiredFile -Label "Zone mask" -PathValue $Mask
$ModelPath = Resolve-RequiredFile -Label "YOLO model file" -PathValue $Model
$TrackerPath = Resolve-RequiredFile -Label "Tracker config" -PathValue $Tracker

& $Python -c "import lap" *> $null
if ($LASTEXITCODE -ne 0) {
    throw "Python dependency 'lap' is missing. Run: python -m pip install --upgrade -r requirements.txt"
}

$CountsDir = Split-Path -Parent $CountsCsv
if (-not [string]::IsNullOrWhiteSpace($CountsDir)) {
    New-Item -ItemType Directory -Force -Path $CountsDir | Out-Null
}

$ArgsList = @(
    $ScriptPath,
    "--source", $Source,
    "--model", $ModelPath,
    "--tracker", $TrackerPath,
    "--device", $Device,
    "--zones", $ZonesPath,
    "--mask", $MaskPath,
    "--camera-id", $Camera,
    "--counts-csv", $CountsCsv,
    "--counts-every", $CountsEvery,
    "--mqtt-broker", $MqttBroker,
    "--mqtt-port", $MqttPort,
    "--mqtt-username", $MqttUsername,
    "--mqtt-password", $MqttPassword,
    "--mqtt-topic", $MqttTopic,
    "--mqtt-every", $MqttEvery
)

& $Python @ArgsList
