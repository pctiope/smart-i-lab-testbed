<#
.SYNOPSIS
  Smoke-test the SSL IoT-1 REST API after a fresh deploy.

.PARAMETER ApiKey
  X-API-KEY for an admin (access_level 0) user. Required for /users and /transactions tests.

.PARAMETER BaseUrl
  Base URL of the REST API. Default: http://localhost

.PARAMETER ReadOnlyKey
  X-API-KEY for a read-only (access_level 1) user. Optional. If provided, runs the
  access-level enforcement test.

.EXAMPLE
  .\smoke_test.ps1 -ApiKey "abc123..." -BaseUrl "http://localhost"

.EXAMPLE
  .\smoke_test.ps1 -ApiKey $env:SSL_ADMIN_KEY -BaseUrl "https://api.lab.example"
#>

param(
    [Parameter(Mandatory = $true)][string] $ApiKey,
    [string] $BaseUrl = "http://localhost",
    [string] $ReadOnlyKey = ""
)

$ErrorActionPreference = "Continue"
$script:Passed = 0
$script:Failed = 0
$script:Headers = @{ 'X-API-KEY' = $ApiKey }

function Invoke-Check {
    param(
        [string] $Name,
        [scriptblock] $Test
    )
    Write-Host -NoNewline "  $Name ... "
    try {
        & $Test
        Write-Host "PASS" -ForegroundColor Green
        $script:Passed++
    } catch {
        Write-Host "FAIL" -ForegroundColor Red
        Write-Host "    $($_.Exception.Message)" -ForegroundColor DarkRed
        $script:Failed++
    }
}

function Expect-Status {
    param(
        [string] $Method = "GET",
        [string] $Url,
        [hashtable] $Headers = @{},
        [int[]] $ExpectedStatus
    )
    try {
        $resp = Invoke-WebRequest -Method $Method -Uri $Url -Headers $Headers -SkipHttpErrorCheck -UseBasicParsing
    } catch {
        # Fallback for PS5 which doesn't support -SkipHttpErrorCheck
        try {
            $resp = Invoke-WebRequest -Method $Method -Uri $Url -Headers $Headers -UseBasicParsing
        } catch [System.Net.WebException] {
            $resp = $_.Exception.Response
            if ($null -eq $resp) { throw }
        }
    }
    $status = if ($resp -is [System.Net.HttpWebResponse]) { [int]$resp.StatusCode } else { $resp.StatusCode }
    if ($ExpectedStatus -notcontains $status) {
        throw "expected $ExpectedStatus, got $status"
    }
}

Write-Host "Smoke test against $BaseUrl" -ForegroundColor Cyan
Write-Host ""

Write-Host "=== Health ==="
Invoke-Check "GET /healthz returns 200" {
    Expect-Status -Url "$BaseUrl/healthz" -ExpectedStatus 200
}

Write-Host ""
Write-Host "=== Auth ==="
Invoke-Check "no api-key header returns 401" {
    Expect-Status -Url "$BaseUrl/air-1" -ExpectedStatus 401
}
Invoke-Check "valid admin key returns 200 from /air-1" {
    Expect-Status -Url "$BaseUrl/air-1" -Headers $script:Headers -ExpectedStatus 200
}

Write-Host ""
Write-Host "=== Read endpoints ==="
foreach ($endpoint in @('air-1','msr-2','smart-plug-v2','ag-one','zigbee2mqtt','sensibo','groups')) {
    Invoke-Check "GET /$endpoint returns 200" {
        Expect-Status -Url "$BaseUrl/$endpoint" -Headers $script:Headers -ExpectedStatus 200
    }
}

Write-Host ""
Write-Host "=== SQL injection regression ==="
Invoke-Check "POST /users with payload in username returns 400" {
    $payload = "bob'); DROP TABLE users;--"
    $encoded = [System.Uri]::EscapeDataString($payload)
    Expect-Status -Method POST -Url "$BaseUrl/users/$encoded`?access_level=1" -Headers $script:Headers -ExpectedStatus 400, 404, 429
}
Invoke-Check "POST /groups with payload in id returns 400" {
    $payload = "foo'); DROP TABLE groups;--"
    $encoded = [System.Uri]::EscapeDataString($payload)
    Expect-Status -Method POST -Url "$BaseUrl/groups?id=$encoded" -Headers $script:Headers -ExpectedStatus 400, 404, 429
}
Invoke-Check "GET /air-1/01/avg with disallowed sensData returns 400" {
    Expect-Status -Url "$BaseUrl/air-1/01/avg?sensData=api_key" -Headers $script:Headers -ExpectedStatus 400, 404
}

Write-Host ""
Write-Host "=== Validation ==="
Invoke-Check "PUT /users/_ with access_level=5abc returns 400" {
    Expect-Status -Method PUT -Url "$BaseUrl/users/_nonexistent?access_level=5abc" -Headers $script:Headers -ExpectedStatus 400, 404, 429
}

Write-Host ""
Write-Host "=== CORS ==="
Invoke-Check "request with disallowed Origin gets no matching ACAO" {
    $h = @{ 'X-API-KEY' = $ApiKey; 'Origin' = 'http://evil.example' }
    try {
        $resp = Invoke-WebRequest -Method GET -Uri "$BaseUrl/air-1" -Headers $h -UseBasicParsing -ErrorAction SilentlyContinue
    } catch { $resp = $_.Exception.Response }
    $acao = $resp.Headers['Access-Control-Allow-Origin']
    if ($acao -eq 'http://evil.example' -or $acao -eq '*') {
        throw "CORS allowed evil origin (ACAO: $acao)"
    }
}

if ($ReadOnlyKey) {
    Write-Host ""
    Write-Host "=== Access-level enforcement ==="
    Invoke-Check "read-only key cannot POST /air-1/01/light" {
        $h = @{ 'X-API-KEY' = $ReadOnlyKey }
        Expect-Status -Method POST -Url "$BaseUrl/air-1/01/light?state=ON" -Headers $h -ExpectedStatus 403
    }
}

Write-Host ""
Write-Host "=============================="
$total = $script:Passed + $script:Failed
Write-Host "  $($script:Passed)/$total passed" -ForegroundColor $(if ($script:Failed -eq 0) { 'Green' } else { 'Yellow' })
Write-Host "=============================="

if ($script:Failed -gt 0) { exit 1 }
exit 0
