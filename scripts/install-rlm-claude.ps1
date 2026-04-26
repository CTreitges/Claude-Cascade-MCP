# Cascade-Bot: RLM-Claude installer for Windows users.
#
# RLM-Claude is Linux-native; on Windows the recommended path is WSL2.
# This script either:
#   1. Confirms WSL is available and walks you through the Linux install
#      from inside WSL, OR
#   2. Prints the manual steps if WSL is missing.
#
# Run from a PowerShell prompt:
#   pwsh -File scripts/install-rlm-claude.ps1
#
# Notes:
# - Use pwsh (PowerShell 7+), not the legacy Windows PowerShell 5 — pwsh
#   handles UTF-8 better and matches the user's documented preference.
# - Nothing here writes to the registry or to system PATH.

[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

function Write-Step([string]$msg) { Write-Host "==> $msg" -ForegroundColor Yellow }
function Write-OK([string]$msg)   { Write-Host "✓  $msg" -ForegroundColor Green  }
function Write-Bad([string]$msg)  { Write-Host "✗  $msg" -ForegroundColor Red    }

Write-Step "Cascade-Bot: RLM-Claude install (Windows host)"

# Check WSL
$wslAvail = $false
try {
    $null = & wsl.exe --status 2>$null
    if ($LASTEXITCODE -eq 0) { $wslAvail = $true }
} catch { }

if (-not $wslAvail) {
    Write-Bad "WSL is not available."
    Write-Host ""
    Write-Host "RLM-Claude needs Linux. Install WSL2 first:"
    Write-Host "  Open PowerShell as Admin and run:" -ForegroundColor Cyan
    Write-Host "    wsl --install -d Ubuntu" -ForegroundColor Green
    Write-Host "  Reboot if asked, then re-run this script."
    Write-Host ""
    Write-Host "Once WSL is up, re-run:" -ForegroundColor Cyan
    Write-Host "    pwsh -File scripts/install-rlm-claude.ps1"
    exit 2
}

Write-OK "WSL is installed."
Write-Step "Detecting default WSL distro"
$distro = (wsl.exe -l -q 2>$null | Select-Object -First 1).Trim()
if (-not $distro) {
    Write-Bad "No WSL distro found — install one with 'wsl --install -d Ubuntu'."
    exit 2
}
Write-OK "Default distro: $distro"

# Resolve repo root (this script lives in scripts/, so parent of parent)
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot  = Split-Path -Parent $scriptDir

# Translate Windows path → /mnt/c/... for WSL.
$wslPath = (& wsl.exe wslpath -a (Resolve-Path $repoRoot).Path).Trim()
Write-OK "Repo path inside WSL: $wslPath"

Write-Step "Running scripts/install-rlm-claude.sh inside WSL"
Write-Host ""
& wsl.exe -d $distro -- bash -lc "cd '$wslPath' && bash scripts/install-rlm-claude.sh"
$installerExit = $LASTEXITCODE

Write-Host ""
if ($installerExit -eq 0) {
    Write-OK "RLM-Claude installer completed inside WSL."
    Write-Host ""
    Write-Host "Reminder: the bot must run INSIDE WSL too (it's a Linux service)."
    Write-Host "  wsl -d $distro -- bash -lc 'cd $wslPath && python3 -m bot'"
} else {
    Write-Bad "Installer inside WSL exited $installerExit. Open a WSL shell and re-run manually."
}
