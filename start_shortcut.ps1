# Sable Desktop Shortcut Launcher
# Launched by Sable.lnk.
# Flow:
#   1. Run start.bat normally and show its setup output here.
#   2. When start.bat exits, open the browser.
#   3. Then show live server logs from sable.log.

$ErrorActionPreference = "Continue"
$SCRIPT_DIR = $PSScriptRoot
if (-not $SCRIPT_DIR) { $SCRIPT_DIR = Split-Path -Parent $MyInvocation.MyCommand.Path }
if (-not $SCRIPT_DIR) { $SCRIPT_DIR = (Get-Location).Path }

$startBat = Join-Path $SCRIPT_DIR "start.bat"
$logPath  = Join-Path $SCRIPT_DIR "sable.log"
$port     = if ($env:SABLE_PORT) { $env:SABLE_PORT } else { "61770" }
$url      = "http://127.0.0.1:$port"

Write-Host "==========================================="
Write-Host "  Sable - Starting..."
Write-Host "==========================================="
Write-Host ""

# Fresh marker so it is obvious the log tail is live for this launch.
"" | Out-File -FilePath $logPath -Encoding utf8 -Append
("========== Sable shortcut launch: {0} ==========" -f (Get-Date)) | Out-File -FilePath $logPath -Encoding utf8 -Append

# Run start.bat normally in this same window.
# It shows setup logs and exits after launching server.py in background.
& cmd.exe /d /c "call `"$startBat`""

Write-Host ""
Write-Host "Opening browser..."
try { Start-Process $url } catch {}

Write-Host ""
Write-Host "Showing live server log: $logPath"
Write-Host "Press Ctrl+C to stop viewing logs."
Write-Host "-------------------------------------------"
Write-Host ""

while (-not (Test-Path $logPath)) {
    Start-Sleep -Seconds 1
}

Get-Content -Wait -Tail 80 $logPath
