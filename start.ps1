# Sable Start Script for Windows
# Requires: Python 3.11+, uv (https://docs.astral.sh/uv/)

$ErrorActionPreference = "Stop"
$SABLE_PORT = if ($env:SABLE_PORT) { $env:SABLE_PORT } else { "61770" }
$SABLE_URL = "http://127.0.0.1:$SABLE_PORT"
$SCRIPT_DIR = Split-Path -Parent $MyInvocation.MyCommand.Path

Set-Location $SCRIPT_DIR

# ── First-run bootstrap ────────────────────────────────────────────────

# Playwright chromium (idempotent)
try {
    & uv run playwright install chromium 2>$null
} catch {}

# Template files
if ((Test-Path "instruction\Maria.md.example") -and -not (Test-Path "instruction\Maria.md")) {
    Copy-Item "instruction\Maria.md.example" "instruction\Maria.md"
    Write-Host "📝 Created instruction\Maria.md from template"
}
if ((Test-Path "Brain\Memory.json.example") -and -not (Test-Path "Brain\Memory.json")) {
    Copy-Item "Brain\Memory.json.example" "Brain\Memory.json"
    Write-Host "🧠 Created Brain\Memory.json from template"
}

# Browser profile directory (no symlinks needed on Windows)
if (-not (Test-Path "system")) { New-Item -ItemType Directory -Path "system" | Out-Null }
if (-not (Test-Path "system\browser-data-acc1")) {
    New-Item -ItemType Directory -Path "system\browser-data-acc1" | Out-Null
}
if (-not (Test-Path "system\browser-data")) {
    # On Windows, use a junction or just point directly
    # For simplicity, create a directory junction
    try {
        cmd /c mklink /J "system\browser-data" "system\browser-data-acc1" 2>$null
    } catch {
        # Fallback: just copy the reference
        New-Item -ItemType Directory -Path "system\browser-data" | Out-Null
    }
}

# ── BurntToast notifications (auto-install, idempotent) ───────────────

try {
    $btInstalled = Get-Module -ListAvailable -Name BurntToast -ErrorAction SilentlyContinue
    if (-not $btInstalled) {
        Write-Host "🔔 Installing BurntToast for native notifications..."
        Install-Module BurntToast -Scope CurrentUser -Force -ErrorAction Stop
        Write-Host "✅ BurntToast installed"
    }
} catch {
    Write-Host "⚠️  BurntToast install skipped: $_"
}

# ── Auto-start via Task Scheduler (first-run, idempotent) ─────────────

$TASK_NAME = "Sable Server"
$START_SCRIPT = Join-Path $SCRIPT_DIR "start.ps1"

try {
    $existingTask = Get-ScheduledTask -TaskName $TASK_NAME -ErrorAction SilentlyContinue
    if (-not $existingTask) {
        $action = New-ScheduledTaskAction `
            -Execute "powershell.exe" `
            -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$START_SCRIPT`""
        $trigger = New-ScheduledTaskTrigger -AtLogOn
        $settings = New-ScheduledTaskSettingsSet `
            -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries `
            -ExecutionTimeLimit ([TimeSpan]::Zero) `
            -RestartCount 3 `
            -RestartInterval (New-TimeSpan -Minutes 1)
        Register-ScheduledTask `
            -TaskName $TASK_NAME `
            -Action $action `
            -Trigger $trigger `
            -Settings $settings `
            -Description "Auto-start Sable agentic chat server on login" `
            | Out-Null
        Write-Host "⚙️  Installed auto-start task: $TASK_NAME (runs on login)"
    }
} catch {
    Write-Host "⚠️  Could not install auto-start task: $_"
}

# ── Sync dependencies ─────────────────────────────────────────────────

Write-Host "🔄 Synchronizing dependencies..."
& uv sync --extra windows
Write-Host ""

# ── Info box ───────────────────────────────────────────────────────────

function Show-InfoBox {
    param($Url, $Port)
    $line = "─" * 54
    Write-Host ""
    Write-Host "╭$line╮"
    Write-Host "│ 🚀 Sable is running!                                 │"
    Write-Host "│                                                      │"
    Write-Host ("│ 🌐 URL:    {0,-41} │" -f $Url)
    Write-Host ("│ 📡 Port:   {0,-41} │" -f $Port)
    Write-Host "│                                                      │"
    Write-Host "│ 📋 Stop:   Ctrl+C                                    │"
    Write-Host "╰$line╯"
    Write-Host ""
}

# ── Auto-open browser ─────────────────────────────────────────────────

function Open-Browser {
    Start-Sleep -Seconds 5
    try {
        Start-Process $SABLE_URL
    } catch {}
}

# ── Start server directly ─────────────────────────────────────────────

Show-InfoBox $SABLE_URL $SABLE_PORT
Open-Browser

$env:TERM = "xterm-256color"
& uv run python server.py
