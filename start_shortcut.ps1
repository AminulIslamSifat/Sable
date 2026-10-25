# ─────────────────────────────────────────────────────────────────────────────
#  Sable launcher (Windows PowerShell) — runs the plaintext dev tree.
#  Thin wrapper over sable-launch.cmd so the shortcut has a visible window
#  while the starter's own output stays live.
# ─────────────────────────────────────────────────────────────────────────────
$ErrorActionPreference = "Continue"
$SCRIPT_DIR = $PSScriptRoot
if (-not $SCRIPT_DIR) { $SCRIPT_DIR = Split-Path -Parent $MyInvocation.MyCommand.Path }
if (-not $SCRIPT_DIR) { $SCRIPT_DIR = (Get-Location).Path }

$launcher = Join-Path $SCRIPT_DIR "sable-launch.cmd"
if (-not (Test-Path $launcher)) {
    Write-Host "[Sable] ERROR: sable-launch.cmd not found in $SCRIPT_DIR" -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}

Write-Host "==========================================="
Write-Host "  Sable - starting via starter"
Write-Host "==========================================="
Write-Host ""

& cmd.exe /d /c "call `"$launcher`""

Write-Host ""
Write-Host "[Sable] server exited. Press Enter to close."
Read-Host
