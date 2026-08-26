# Sable Start Script for Windows
# Requires: Python 3.11+, uv (https://docs.astral.sh/uv/)

$ErrorActionPreference = "Stop"
$SABLE_PORT = if ($env:SABLE_PORT) { $env:SABLE_PORT } else { "61770" }
$SABLE_URL = "http://127.0.0.1:$SABLE_PORT"
$SCRIPT_DIR = Split-Path -Parent $MyInvocation.MyCommand.Path

Set-Location $SCRIPT_DIR

#  First-run bootstrap 

# Playwright chromium (idempotent)
try {
    & uv run playwright install chromium 2>$null
} catch {}

# Template files
if ((Test-Path "instruction\Maria.md.example") -and -not (Test-Path "instruction\Maria.md")) {
    Copy-Item "instruction\Maria.md.example" "instruction\Maria.md"
    Write-Host " Created instruction\Maria.md from template"
}
if ((Test-Path "Brain\Memory.json.example") -and -not (Test-Path "Brain\Memory.json")) {
    Copy-Item "Brain\Memory.json.example" "Brain\Memory.json"
    Write-Host " Created Brain\Memory.json from template"
}

# Browser profile directory
if (-not (Test-Path "system")) { New-Item -ItemType Directory -Path "system" | Out-Null }
if (-not (Test-Path "system\browser-data-acc1")) {
    New-Item -ItemType Directory -Path "system\browser-data-acc1" | Out-Null
}
# Note: the legacy "system\browser-data" symlink/junction is no longer needed.
# Account switching now uses the system\.active_account file (set_active_account / get_active_account).
# If an old junction exists it will keep working; new installs skip it entirely.

# SearXNG search backend (Docker, idempotent)
try {
    $dockerAvailable = Get-Command docker -ErrorAction SilentlyContinue
    if ($dockerAvailable) {
        $containerRunning = $false
        try {
            $status = docker inspect -f '{{.State.Running}}' searxng 2>$null
            if ($status -eq 'true') { $containerRunning = $true }
        } catch {}

        if (-not $containerRunning) {
            docker rm -f searxng 2>$null | Out-Null
            Write-Host " Starting SearXNG search backend..."
            $imageExists = $false
            try {
                docker image inspect searxng/searxng:latest 2>$null | Out-Null
                if ($LASTEXITCODE -eq 0) { $imageExists = $true }
            } catch {}
            if (-not $imageExists) {
                Write-Host "   Pulling searxng/searxng:latest (first time only)..."
                docker pull searxng/searxng:latest
            }
            docker run -d --name searxng `
                -p 8080:8080 `
                -e SEARXNG_BASE_URL=http://localhost:8080/ `
                --restart unless-stopped `
                searxng/searxng:latest | Out-Null
            # Enable JSON API format (write config snippet into container)
            Start-Sleep -Seconds 3
            $configSnippet = @"

search:
  formats:
    - html
    - json
"@
            $tmpFile = Join-Path $env:TEMP "searxng_formats.yml"
            Set-Content -Path $tmpFile -Value $configSnippet -NoNewline -Encoding UTF8
            docker cp $tmpFile "/tmp/searxng_formats.yml" 2>$null | Out-Null
            docker exec searxng sh -c "grep -q 'formats:' /etc/searxng/settings.yml || cat /tmp/searxng_formats.yml >> /etc/searxng/settings.yml" 2>$null
            Remove-Item $tmpFile -ErrorAction SilentlyContinue
            docker restart searxng | Out-Null
            Start-Sleep -Seconds 2
            Write-Host " SearXNG ready on http://localhost:8080"
        } else {
            Write-Host " SearXNG already running"
        }
    } else {
        Write-Host " Docker not found - SearXNG search backend skipped"
    }
} catch {
    Write-Host " SearXNG setup failed: $_"
}

#  BurntToast notifications (auto-install, idempotent) 

try {
    $btInstalled = Get-Module -ListAvailable -Name BurntToast -ErrorAction SilentlyContinue
    if (-not $btInstalled) {
        Write-Host " Installing BurntToast for native notifications..."
        Install-Module BurntToast -Scope CurrentUser -Force -ErrorAction Stop
        Write-Host " BurntToast installed"
    }
} catch {
    Write-Host "  BurntToast install skipped: $_"
}

#  Auto-start via Task Scheduler (first-run, idempotent) 

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
        Write-Host "  Installed auto-start task: $TASK_NAME (runs on login)"
    }
} catch {
    Write-Host "  Could not install auto-start task: $_"
}

#  Sync dependencies 

Write-Host " Synchronizing dependencies..."
& uv sync --extra windows
Write-Host ""

#  Info box 

function Show-InfoBox {
    param($Url, $Port)
    $line = "-" * 54
    Write-Host ""
    Write-Host "+$line+"
    Write-Host "| Sable is running!                                    |"
    Write-Host "|                                                      |"
    Write-Host ("| URL:     {0,-41} |" -f $Url)
    Write-Host ("| Port:    {0,-41} |" -f $Port)
    Write-Host "|                                                      |"
    Write-Host "| Stop:    Ctrl+C                                      |"
    Write-Host "+$line+"
    Write-Host ""
}

#  Auto-open browser 

function Open-Browser {
    param($Url)
    # Poll until the server is accepting connections (up to 30 s), then open browser.
    $deadline = (Get-Date).AddSeconds(30)
    while ((Get-Date) -lt $deadline) {
        try {
            $null = Invoke-WebRequest -Uri "$Url/api/health" -UseBasicParsing -TimeoutSec 1 -ErrorAction Stop
            break
        } catch {
            Start-Sleep -Milliseconds 500
        }
    }
    try {
        Start-Process $Url
    } catch {}
}

#  Start server directly 

Show-InfoBox $SABLE_URL $SABLE_PORT
Start-Job -ScriptBlock { param($u) Open-Browser $u } -ArgumentList $SABLE_URL | Out-Null

$env:TERM = "xterm-256color"
& uv run python server.py
