param(
    [string]$Source = $(if ($env:ZONE5_RTSP_URL) { $env:ZONE5_RTSP_URL } else { "rtsp://admin:++smartilab2023@10.158.71.241:554/Streaming/channels/101" }),
    [string]$Script = "cv_counter\rtsp_person_mask_tracker_new.py",
    [string]$Mask = "cv_counter\masks\cam1-desk5-mask.png",
    [string]$Model = "cv_counter\models\headtracker-m.pt",
    [string]$Tracker = "cv_counter\trackers\bytetrack.yaml",
    [string]$Device = "cpu",
    [int]$Imgsz = $(if ($env:PERSON_COUNT_IMGSZ) { [int]$env:PERSON_COUNT_IMGSZ } else { 256 }),
    [string]$CountsCsv = "data\person_counts.csv",
    [string]$Python = $(if ($env:PYTHON_BIN) { $env:PYTHON_BIN } else { "python" }),
    [string]$MqttBroker = $(if ($env:PERSON_COUNT_MQTT_BROKER) { $env:PERSON_COUNT_MQTT_BROKER } else { "10.158.71.19" }),
    [int]$MqttPort = $(if ($env:PERSON_COUNT_MQTT_PORT) { [int]$env:PERSON_COUNT_MQTT_PORT } else { 1883 }),
    [string]$MqttTopic = $(if ($env:PERSON_COUNT_MQTT_TOPIC) { $env:PERSON_COUNT_MQTT_TOPIC } else { "care_ssl/zone5/person_count" }),
    [string]$MqttUsername = $(if ($env:PERSON_COUNT_MQTT_USERNAME) { $env:PERSON_COUNT_MQTT_USERNAME } else { "guest" }),
    [string]$MqttPassword = $(if ($env:PERSON_COUNT_MQTT_PASSWORD) { $env:PERSON_COUNT_MQTT_PASSWORD } else { "smartilab123" }),
    [int]$MqttEvery = 1,
    [int]$CountsEvery = 1,
    [string]$ShowMask = $(if ($env:PERSON_COUNT_SHOW_MASK) { $env:PERSON_COUNT_SHOW_MASK } else { "true" }),
    [string]$Tracking = $(if ($env:PERSON_COUNT_TRACKING) { $env:PERSON_COUNT_TRACKING } else { "true" })
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
        throw "$Label not found: $PathValue. The Zone 5 package must include cv_counter assets before starting the person counter."
    }
    return $resolved.Path
}

function Test-Enabled {
    param(
        [Parameter(Mandatory=$true)][string]$Name,
        [Parameter(Mandatory=$true)][string]$Value
    )
    switch ($Value.Trim().ToLowerInvariant()) {
        { $_ -in @("1", "true", "yes", "on") } { return $true }
        { $_ -in @("0", "false", "no", "off", "disabled") } { return $false }
        default { throw "$Name must be true or false, got: $Value" }
    }
}

if ([string]::IsNullOrWhiteSpace($Source)) {
    throw "Pass -Source or set ZONE5_RTSP_URL to the camera RTSP URL."
}

$ScriptPath = Resolve-RequiredFile -Label "Person-counter script" -PathValue $Script
$MaskPath = Resolve-RequiredFile -Label "Mask file" -PathValue $Mask
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
    "--mask", $MaskPath,
    "--model", $ModelPath,
    "--tracker", $TrackerPath,
    "--device", $Device,
    "--imgsz", $Imgsz,
    "--counts-csv", $CountsCsv,
    "--counts-every", $CountsEvery
)

if (-not (Test-Enabled -Name "PERSON_COUNT_TRACKING/Tracking" -Value $Tracking)) {
    $ArgsList += "--no-tracker"
}

if (Test-Enabled -Name "PERSON_COUNT_SHOW_MASK/ShowMask" -Value $ShowMask) {
    $ArgsList += "--show-mask"
}

$ArgsList += @(
    "--mqtt-broker", $MqttBroker,
    "--mqtt-port", $MqttPort,
    "--mqtt-username", $MqttUsername,
    "--mqtt-password", $MqttPassword,
    "--mqtt-topic", $MqttTopic,
    "--mqtt-every", $MqttEvery
)

& $Python @ArgsList
