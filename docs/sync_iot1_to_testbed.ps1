<#
.SYNOPSIS
  Sync the hardened IoT1 stack from CARE-SSL into smart-i-lab-testbed.

.DESCRIPTION
  Copies IoT1 source files (SSL-IoT1-REST, Smart-iLab_DigitalTwin,
  Smart-iLAB-Python-Files, migrations, compose files, etc.) from
  CARE-SSL/IoT1/ to smart-i-lab-testbed/. Preserves the testbed-only CV
  consumer packages (air1_all_zones_cv_time_features_package,
  zone5_cv_time_features_package).

  Any overwritten file is first backed up to
  `<Target>/_backup_<yyyyMMdd_HHmmss>/<relative-path>`.

  Defaults to dry-run via -WhatIf. To actually copy, re-run without -WhatIf.

.PARAMETER Source
  Hardened IoT1 root. Default: C:\Users\pjtio\OneDrive\Desktop\CARE-SSL\IoT1

.PARAMETER Target
  Testbed root (the parent of SSL-IoT1-REST etc., not of IoT1 itself).
  Default: C:\Users\pjtio\smart-i-lab-testbed

.PARAMETER NoBackup
  Skip the backup step. Faster, but destructive if anything in the target
  has local changes you haven't committed.

.EXAMPLE
  .\sync_iot1_to_testbed.ps1 -WhatIf
  # Lists files that would be copied; writes nothing.

.EXAMPLE
  .\sync_iot1_to_testbed.ps1
  # Performs the sync after a confirmation prompt; backs up overwritten files.

.EXAMPLE
  .\sync_iot1_to_testbed.ps1 -Confirm:$false
  # Performs the sync without prompting.
#>

[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
param(
    [string] $Source = "C:\Users\pjtio\OneDrive\Desktop\CARE-SSL\IoT1",
    [string] $Target = "C:\Users\pjtio\smart-i-lab-testbed",
    [switch] $NoBackup
)

$ErrorActionPreference = "Stop"

# Paths inside the target that must NEVER be touched (testbed-only CV apps).
$PreservePaths = @(
    'air1_all_zones_cv_time_features_package',
    'zone5_cv_time_features_package',
    '_backup_*'
)

# File-name patterns to skip when enumerating Source (build artifacts).
$SkipPatterns = @(
    'node_modules',
    'dist',
    '__pycache__',
    '.venv',
    'venv',
    '.git'
)

function Write-Section($text) {
    Write-Host ""
    Write-Host "=== $text ===" -ForegroundColor Cyan
}

function Test-PreservedPath($relativePath) {
    foreach ($pattern in $PreservePaths) {
        if ($relativePath -like "$pattern*" -or $relativePath -like "*/$pattern/*" -or $relativePath -like "*\$pattern\*") {
            return $true
        }
    }
    return $false
}

if (-not (Test-Path -LiteralPath $Source)) {
    throw "Source not found: $Source"
}
if (-not (Test-Path -LiteralPath $Target)) {
    throw "Target not found: $Target"
}

$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$backupRoot = Join-Path $Target "_backup_$timestamp"

Write-Section "Sync plan"
Write-Host "  Source: $Source"
Write-Host "  Target: $Target"
Write-Host "  Backup: $backupRoot (only if files are overwritten and -NoBackup not set)"
Write-Host "  Preserve in target: $($PreservePaths -join ', ')"

# Enumerate every file under Source, skipping build-artifact subtrees.
Write-Section "Scanning source tree"
$sourceFiles = Get-ChildItem -LiteralPath $Source -Recurse -File -Force | Where-Object {
    $rel = $_.FullName.Substring($Source.Length).TrimStart('\','/')
    foreach ($pattern in $SkipPatterns) {
        # Match the pattern only at segment boundaries so `.git` doesn't also
        # exclude `.gitignore` or `.github`.
        if ($rel -eq $pattern -or
            $rel -like "$pattern\*" -or $rel -like "$pattern/*" -or
            $rel -like "*\$pattern\*" -or $rel -like "*/$pattern/*" -or
            $rel -like "*\$pattern" -or $rel -like "*/$pattern") {
            return $false
        }
    }
    return $true
}
Write-Host "  Found $($sourceFiles.Count) candidate file(s) in Source."

$counts = @{ added = 0; updated = 0; unchanged = 0; skipped = 0; backed_up = 0 }
$details = @{ added = @(); updated = @(); skipped = @() }

Write-Section "Comparing"
foreach ($file in $sourceFiles) {
    $relative = $file.FullName.Substring($Source.Length).TrimStart('\','/')
    $destination = Join-Path $Target $relative

    if (Test-PreservedPath $relative) {
        $counts.skipped++
        $details.skipped += $relative
        continue
    }

    if (Test-Path -LiteralPath $destination) {
        $sourceHash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash
        $destHash   = (Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash
        if ($sourceHash -eq $destHash) {
            $counts.unchanged++
            continue
        }
        # Differs -- count + log immediately so -WhatIf gives a useful summary.
        $counts.updated++
        $details.updated += $relative
        if ($PSCmdlet.ShouldProcess($destination, "Backup + overwrite (was: $($destHash.Substring(0,8))..., new: $($sourceHash.Substring(0,8))...)")) {
            if (-not $NoBackup) {
                $backupPath = Join-Path $backupRoot $relative
                $backupDir  = Split-Path -Parent $backupPath
                if (-not (Test-Path -LiteralPath $backupDir)) {
                    New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
                }
                Copy-Item -LiteralPath $destination -Destination $backupPath -Force
                $counts.backed_up++
            }
            $destDir = Split-Path -Parent $destination
            if (-not (Test-Path -LiteralPath $destDir)) {
                New-Item -ItemType Directory -Path $destDir -Force | Out-Null
            }
            Copy-Item -LiteralPath $file.FullName -Destination $destination -Force
        }
    } else {
        $counts.added++
        $details.added += $relative
        if ($PSCmdlet.ShouldProcess($destination, "Add (new file)")) {
            $destDir = Split-Path -Parent $destination
            if (-not (Test-Path -LiteralPath $destDir)) {
                New-Item -ItemType Directory -Path $destDir -Force | Out-Null
            }
            Copy-Item -LiteralPath $file.FullName -Destination $destination -Force
        }
    }
}

Write-Section "Summary"
Write-Host ("  Added     : {0}" -f $counts.added)        -ForegroundColor Green
Write-Host ("  Updated   : {0}" -f $counts.updated)      -ForegroundColor Yellow
Write-Host ("  Unchanged : {0}" -f $counts.unchanged)
Write-Host ("  Skipped   : {0} (preserve list)" -f $counts.skipped) -ForegroundColor DarkGray
Write-Host ("  Backed up : {0}" -f $counts.backed_up)
if ($WhatIfPreference) {
    Write-Host ""
    Write-Host "  [Dry-run mode -- no files written. Re-run without -WhatIf to apply.]" -ForegroundColor Magenta
}

if ($details.added.Count -gt 0 -and $details.added.Count -le 40) {
    Write-Host ""
    Write-Host "  New files:" -ForegroundColor Green
    $details.added | Sort-Object | ForEach-Object { Write-Host "    + $_" }
}
if ($details.updated.Count -gt 0 -and $details.updated.Count -le 40) {
    Write-Host ""
    Write-Host "  Updated files:" -ForegroundColor Yellow
    $details.updated | Sort-Object | ForEach-Object { Write-Host "    ~ $_" }
}

if (-not $WhatIfPreference -and $counts.backed_up -gt 0) {
    Write-Host ""
    Write-Host "  Backup of overwritten files is at:" -ForegroundColor DarkGray
    Write-Host "    $backupRoot"
}

Write-Host ""
Write-Host "Done." -ForegroundColor Cyan
