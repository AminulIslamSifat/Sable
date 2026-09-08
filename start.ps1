# ─────────────────────────────────────────────────────────────────────────────
#  Sable — Zero-Intervention Start Script (Windows)
#  Handles: Python, uv, git, Docker, VCRedist, Playwright, SearXNG,
#           BurntToast, Task Scheduler auto-start, error recovery, and launch.
#  The user should never have to do anything after .\start.ps1
# ─────────────────────────────────────────────────────────────────────────────

$ErrorActionPreference = "Continue"
$ProgressPreference    = "SilentlyContinue"   # Speed up Invoke-WebRequest

# ── Globals ──────────────────────────────────────────────────────────────────
$SABLE_PORT   = if ($env:SABLE_PORT) { $env:SABLE_PORT } else { "61770" }
$SABLE_URL    = "http://127.0.0.1:$SABLE_PORT"
$SCRIPT_DIR   = Split-Path -Parent $MyInvocation.MyCommand.Path
$TASK_NAME    = "Sable Server"
$MAX_RETRIES  = 3
$RETRY_DELAY  = 2

Set-Location $SCRIPT_DIR

# ── Logging ──────────────────────────────────────────────────────────────────
function Write-Log  { param($msg) Write-Host "[Sable] $msg" }
function Write-Ok   { param($msg) Write-Host "[Sable] ✓ $msg" -ForegroundColor Green }
function Write-Warn { param($msg) Write-Host "[Sable] ⚠ $msg" -ForegroundColor Yellow }
function Write-Err  { param($msg) Write-Host "[Sable] ✗ $msg" -ForegroundColor Red }
function Write-Info { param($msg) Write-Host "[Sable] → $msg" -ForegroundColor Cyan }

# ── Retry Helper ─────────────────────────────────────────────────────────────
function Invoke-WithRetry {
    param([scriptblock]$Action, [string]$Name = "Operation")
    for ($i = 1; $i -le $MAX_RETRIES; $i++) {
        try {
            & $Action
            return $true
        } catch {
            Write-Warn "$Name attempt $i/$MAX_RETRIES failed: $_"
            if ($i -lt $MAX_RETRIES) { Start-Sleep -Seconds $RETRY_DELAY }
        }
    }
    Write-Err "$Name failed after $MAX_RETRIES attempts"
    return $false
}

# ── Command Existence Check ─────────────────────────────────────────────────
function Test-Command {
    param([string]$cmd)
    return [bool](Get-Command $cmd -ErrorAction SilentlyContinue)
}

# ── Ensure Execution Policy ─────────────────────────────────────────────────
function Ensure-ExecutionPolicy {
    Write-Info "Checking PowerShell execution policy..."
    $policy = Get-ExecutionPolicy -Scope CurrentUser
    if ($policy -eq "Restricted" -or $policy -eq "AllSigned") {
        try {
            Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned -Force
            Write-Ok "Execution policy set to RemoteSigned (CurrentUser)"
        } catch {
            Write-Warn "Could not set execution policy — script may fail on fresh installs"
        }
    } else {
        Write-Ok "Execution policy OK ($policy)"
    }
}

# ── Ensure Python 3.12+ ─────────────────────────────────────────────────────
function Ensure-Python {
    Write-Info "Checking Python version..."

    # Try common python commands
    $pyCmd = $null
    foreach ($cmd in @("python3.12", "python3", "python", "py")) {
        if (Test-Command $cmd) {
            $pyCmd = $cmd
            break
        }
    }

    if (-not $pyCmd) {
        Write-Warn "Python not found — attempting install via winget..."
        if (Test-Command "winget") {
            winget install Python.Python.3.12 --accept-source-agreements --accept-package-agreements 2>$null
            # Refresh PATH
            $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
            foreach ($cmd in @("python3.12", "python3", "python", "py")) {
                if (Test-Command $cmd) { $pyCmd = $cmd; break }
            }
        }
    }

    if (-not $pyCmd) {
        Write-Err "Python 3.12+ is required but could not be installed automatically."
        Write-Err "Install from: https://www.python.org/downloads/"
        Write-Err "Or run: winget install Python.Python.3.12"
        exit 1
    }

    # Verify version
    try {
        $verStr = & $pyCmd -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
        $parts = $verStr.Split('.')
        $major = [int]$parts[0]
        $minor = [int]$parts[1]

        if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 12)) {
            Write-Err "Python 3.12+ required (found $verStr). Please upgrade."
            exit 1
        }
        Write-Ok "Python $verStr detected ($pyCmd)"
    } catch {
        Write-Warn "Could not verify Python version — proceeding anyway"
    }
}

# ── Ensure uv ────────────────────────────────────────────────────────────────
function Ensure-Uv {
    if (Test-Command "uv") {
        $ver = (uv --version 2>$null) -replace "^uv ", ""
        Write-Ok "uv found (v$ver)"
        return
    }

    Write-Info "Installing uv..."
    try {
        powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex" 2>$null
    } catch {}

    # Refresh PATH
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
    $localBin = Join-Path $env:USERPROFILE ".local\bin"
    $cargoBin = Join-Path $env:USERPROFILE ".cargo\bin"
    if (Test-Path $localBin) { $env:Path += ";$localBin" }
    if (Test-Path $cargoBin) { $env:Path += ";$cargoBin" }

    if (-not (Test-Command "uv")) {
        Write-Err "Failed to install uv. Install manually: https://docs.astral.sh/uv/"
        exit 1
    }
    Write-Ok "uv installed successfully"
}

# ── Ensure Git ───────────────────────────────────────────────────────────────
function Ensure-Git {
    if (Test-Command "git") {
        Write-Ok "git available"
        return
    }
    Write-Info "Installing git..."
    if (Test-Command "winget") {
        winget install Git.Git --accept-source-agreements --accept-package-agreements 2>$null
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
    }
    if (-not (Test-Command "git")) {
        Write-Err "git is required. Install from: https://git-scm.com/download/win"
        exit 1
    }
    Write-Ok "git installed"
}

# ── Ensure VC++ Redistributable (for greenlet / Playwright) ─────────────────
function Ensure-VCRedist {
    Write-Info "Checking Visual C++ Redistributable..."
    # Check if the DLL exists in system directory
    $vcDll = Join-Path $env:SystemRoot "System32\vcruntime140.dll"
    if (Test-Path $vcDll) {
        Write-Ok "VC++ Redistributable present"
        return
    }
    Write-Info "Installing VC++ Redistributable..."
    if (Test-Command "winget") {
        winget install Microsoft.VCRedist.2015+.x64 --accept-source-agreements --accept-package-agreements 2>$null
    } else {
        Write-Warn "Install VC++ Redistributable manually: https://aka.ms/vs/17/release/vc_redist.x64.exe"
    }
}

# ── Ensure Docker (Desktop) ─────────────────────────────────────────────────
function Ensure-Docker {
    Write-Info "Checking Docker..."

    if (Test-Command "docker") {
        # CLI exists — check if daemon is responsive
        try {
            $null = docker info 2>&1
            if ($LASTEXITCODE -eq 0) {
                Write-Ok "Docker daemon running"
                return $true
            }
        } catch {}

        # Daemon not running — try to start Docker Desktop
        Write-Info "Docker CLI found but daemon not responding — starting Docker Desktop..."
        $ddPath = Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"
        if (-not (Test-Path $ddPath)) {
            $ddPath = Join-Path ${env:ProgramFiles(x86)} "Docker\Docker\Docker Desktop.exe"
        }
        if (Test-Path $ddPath) {
            Start-Process $ddPath 2>$null
            Write-Info "Waiting for Docker Desktop to start (up to 60s)..."
            for ($i = 0; $i -lt 30; $i++) {
                Start-Sleep -Seconds 2
                try {
                    $null = docker info 2>&1
                    if ($LASTEXITCODE -eq 0) {
                        Write-Ok "Docker Desktop started"
                        return $true
                    }
                } catch {}
            }
        }
        Write-Warn "Docker daemon not responding — SearXNG will be skipped"
        Write-Warn "Make sure Docker Desktop is running (whale icon in system tray)"
        return $false
    }

    # Not installed
    Write-Info "Docker not found — attempting install via winget..."
    if (Test-Command "winget") {
        winget install Docker.DockerDesktop --accept-source-agreements --accept-package-agreements 2>$null
        Write-Warn "Docker Desktop installed — you may need to REBOOT and start Docker Desktop once."
        Write-Warn "After reboot, run this script again. SearXNG skipped for now."
    } else {
        Write-Warn "Install Docker Desktop: https://www.docker.com/products/docker-desktop/"
    }
    return $false
}

# ── Setup SearXNG Container ─────────────────────────────────────────────────
function Setup-SearXNG {
    Write-Info "Checking SearXNG search backend..."

    $dockerReady = Ensure-Docker
    if (-not $dockerReady) {
        Write-Warn "Docker unavailable — SearXNG search backend skipped"
        return
    }

    # Check if already running
    try {
        $status = docker inspect -f '{{.State.Running}}' searxng 2>$null
        if ($status -eq 'true') {
            Write-Ok "SearXNG already running"
            return
        }
    } catch {}

    # Clean up stale container
    docker rm -f searxng 2>$null | Out-Null

    # Pull image if needed
    $imageExists = $false
    try {
        docker image inspect searxng/searxng:latest 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { $imageExists = $true }
    } catch {}

    if (-not $imageExists) {
        Write-Info "Pulling searxng/searxng:latest (first time only)..."
        $pullOk = Invoke-WithRetry -Action { docker pull searxng/searxng:latest } -Name "Docker pull"
        if (-not $pullOk) {
            Write-Warn "Failed to pull SearXNG image — skipped"
            return
        }
    }

    # Start container
    Write-Info "Starting SearXNG container..."
    try {
        docker run -d --name searxng `
            -p 8080:8080 `
            -e SEARXNG_BASE_URL=http://localhost:8080/ `
            --restart unless-stopped `
            searxng/searxng:latest 2>$null | Out-Null
    } catch {
        Write-Warn "Failed to start SearXNG container: $_"
        return
    }

    # Wait for initialization
    Start-Sleep -Seconds 3

    # Enable JSON API format (idempotent)
    $configSnippet = @"

search:
  formats:
    - html
    - json
"@
    $tmpFile = Join-Path $env:TEMP "searxng_formats.yml"
    Set-Content -Path $tmpFile -Value $configSnippet -NoNewline -Encoding UTF8
    docker cp $tmpFile "searxng:/tmp/searxng_formats.yml" 2>$null | Out-Null
    docker exec searxng sh -c "grep -q 'formats:' /etc/searxng/settings.yml || cat /tmp/searxng_formats.yml >> /etc/searxng/settings.yml" 2>$null
    Remove-Item $tmpFile -ErrorAction SilentlyContinue

    docker restart searxng 2>$null | Out-Null
    Start-Sleep -Seconds 2
    Write-Ok "SearXNG ready on http://localhost:8080"
}

# ── Bootstrap Templates & Directories ────────────────────────────────────────
function Bootstrap-Files {
    Write-Info "Bootstrapping project files..."

    # Template files
    if ((Test-Path "instruction\Maria.md.example") -and -not (Test-Path "instruction\Maria.md")) {
        Copy-Item "instruction\Maria.md.example" "instruction\Maria.md"
        Write-Ok "Created instruction\Maria.md from template"
    }
    if ((Test-Path "Brain\Memory.json.example") -and -not (Test-Path "Brain\Memory.json")) {
        Copy-Item "Brain\Memory.json.example" "Brain\Memory.json"
        Write-Ok "Created Brain\Memory.json from template"
    }

    # System directories
    if (-not (Test-Path "system")) { New-Item -ItemType Directory -Path "system" | Out-Null }
    if (-not (Test-Path "system\browser-data-acc1")) {
        New-Item -ItemType Directory -Path "system\browser-data-acc1" | Out-Null
    }

    Write-Ok "Bootstrap complete"
}

# ── Sync Dependencies ───────────────────────────────────────────────────────
function Sync-Dependencies {
    Write-Info "Synchronizing Python dependencies (uv sync)..."
    $syncOk = Invoke-WithRetry -Action {
        cmd /c "uv sync --extra windows 2>&1"
        if ($LASTEXITCODE -ne 0) { throw "uv sync exited with code $LASTEXITCODE" }
    } -Name "uv sync"

    if (-not $syncOk) {
        Write-Warn "uv sync failed — attempting venv recreation..."
        Remove-Item -Recurse -Force ".venv" -ErrorAction SilentlyContinue
        $retryOk = Invoke-WithRetry -Action {
            cmd /c "uv sync --extra windows 2>&1"
            if ($LASTEXITCODE -ne 0) { throw "uv sync exited with code $LASTEXITCODE" }
        } -Name "uv sync (retry)"
        if (-not $retryOk) {
            Write-Err "Dependency sync failed. Check network or pyproject.toml."
            exit 1
        }
    }
    Write-Ok "Dependencies synced"
}

# ── Setup Playwright Chromium ────────────────────────────────────────────────
function Setup-Playwright {
    Write-Info "Ensuring Playwright Chromium..."
    try {
        cmd /c "uv run playwright install chromium 2>&1"
    } catch {
        Write-Warn "Playwright Chromium install had issues — browser automation may not work"
    }
    Write-Ok "Playwright check done"
}

# ── Install BurntToast Notifications ────────────────────────────────────────
function Setup-BurntToast {
    Write-Info "Checking BurntToast notification module..."
    try {
        $btInstalled = Get-Module -ListAvailable -Name BurntToast -ErrorAction SilentlyContinue
        if (-not $btInstalled) {
            Write-Info "Installing BurntToast for native notifications..."
            Install-Module BurntToast -Scope CurrentUser -Force -ErrorAction Stop
            Write-Ok "BurntToast installed"
        } else {
            Write-Ok "BurntToast available"
        }
    } catch {
        Write-Warn "BurntToast install skipped: $_"
    }
}

# ── Cleanup Stale Process ───────────────────────────────────────────────────
function Cleanup-StaleProcess {
    $pidFile = Join-Path $SCRIPT_DIR ".sable_server.pid"
    if (Test-Path $pidFile) {
        $oldPid = Get-Content $pidFile -ErrorAction SilentlyContinue
        if ($oldPid) {
            $proc = Get-Process -Id $oldPid -ErrorAction SilentlyContinue
            if ($proc) {
                Write-Info "Stopping previous instance (PID $oldPid)..."
                Stop-Process -Id $oldPid -Force -ErrorAction SilentlyContinue
                Start-Sleep -Seconds 1
            }
        }
        Remove-Item $pidFile -ErrorAction SilentlyContinue
    }

    # Also kill any orphaned sable server processes by command line
    try {
        $orphans = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -match "server\.py" -and $_.CommandLine -match "Sable" }
        foreach ($p in $orphans) {
            Write-Info "Killing orphaned server process (PID $($p.ProcessId))..."
            Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
        }
    } catch {}
}

# ── Task Scheduler Auto-Start ───────────────────────────────────────────────
function Setup-AutoStart {
    Write-Info "Checking auto-start configuration..."
    $startBat = Join-Path $SCRIPT_DIR "start.bat"

    # Clean up legacy registry Run key
    try {
        $oldReg = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
        if (Get-ItemProperty -Path $oldReg -Name "Sable Server" -ErrorAction SilentlyContinue) {
            Remove-ItemProperty -Path $oldReg -Name "Sable Server" -ErrorAction SilentlyContinue
            Write-Ok "Removed legacy registry auto-start entry"
        }
    } catch {}

    try {
        $existingTask = Get-ScheduledTask -TaskName $TASK_NAME -ErrorAction SilentlyContinue

        $action = New-ScheduledTaskAction `
            -Execute "cmd.exe" `
            -Argument "/c `"$startBat`" --background" `
            -WorkingDirectory $SCRIPT_DIR

        if (-not $existingTask) {
            $trigger = New-ScheduledTaskTrigger -AtLogOn
            $settings = New-ScheduledTaskSettingsSet `
                -AllowStartIfOnBatteries `
                -DontStopIfGoingOnBatteries `
                -ExecutionTimeLimit ([TimeSpan]::Zero) `
                -RestartCount 3 `
                -RestartInterval (New-TimeSpan -Minutes 1)
            $principal = New-ScheduledTaskPrincipal `
                -UserId $env:USERNAME `
                -LogonType Interactive `
                -RunLevel Limited

            Register-ScheduledTask `
                -TaskName $TASK_NAME `
                -Action $action `
                -Trigger $trigger `
                -Settings $settings `
                -Principal $principal `
                -Description "Auto-start Sable agentic chat server on login (hidden)" `
                | Out-Null
            Write-Ok "Installed auto-start task: $TASK_NAME (runs hidden on login)"
        } else {
            # Update working directory + args in case install moved
            Set-ScheduledTask -TaskName $TASK_NAME -Action $action | Out-Null
            Write-Ok "Auto-start task already configured"
        }
    } catch {
        Write-Warn "Could not configure auto-start: $_"
    }
}

# ── Health Check ─────────────────────────────────────────────────────────────
function Wait-ForServer {
    Write-Info "Waiting for server to accept connections..."
    $deadline = (Get-Date).AddSeconds(30)
    while ((Get-Date) -lt $deadline) {
        try {
            $null = Invoke-WebRequest -Uri "$SABLE_URL/api/health" -UseBasicParsing -TimeoutSec 1 -ErrorAction Stop
            Write-Ok "Server responding on $SABLE_URL"
            return $true
        } catch {
            try {
                $null = Invoke-WebRequest -Uri "$SABLE_URL/" -UseBasicParsing -TimeoutSec 1 -ErrorAction Stop
                Write-Ok "Server responding on $SABLE_URL"
                return $true
            } catch {}
        }
        Start-Sleep -Milliseconds 500
    }
    Write-Warn "Server didn't respond within 30s (may still be starting)"
    return $false
}

# ── Open Browser ─────────────────────────────────────────────────────────────
function Open-Browser {
    param([string]$Url)
    Start-Job -ScriptBlock {
        param($u)
        Start-Sleep -Seconds 3
        try { Start-Process $u } catch {}
    } -ArgumentList $Url | Out-Null
}

# ── Info Box ─────────────────────────────────────────────────────────────────
function Show-InfoBox {
    param($Url, $Port)
    $line = "─" * 58
    Write-Host ""
    Write-Host "╭$line╮"
    Write-Host "│ 🦊 Sable is running!                                     │"
    Write-Host "│                                                          │"
    Write-Host ("│ 🌐 URL:    {0,-47} │" -f $Url)
    Write-Host ("│ 📡 Port:   {0,-47} │" -f $Port)
    Write-Host "│                                                          │"
    Write-Host "│ 📋 Manage: Get-ScheduledTask -TaskName 'Sable Server'   │"
    Write-Host "│ 🛑 Stop:   Ctrl+C                                        │"
    Write-Host "╰$line╯"
    Write-Host ""
}

# ── Main ─────────────────────────────────────────────────────────────────────
function Main {
    Write-Host ""
    Write-Log "═══════════════════════════════════════════"
    Write-Log "  Sable — Agentic Chat Platform (Windows)"
    Write-Log "═══════════════════════════════════════════"
    Write-Host ""

    Ensure-ExecutionPolicy
    Ensure-Python
    Ensure-Uv
    Ensure-Git
    Ensure-VCRedist
    Cleanup-StaleProcess
    Bootstrap-Files
    Setup-Playwright
    Setup-SearXNG
    Sync-Dependencies
    Setup-BurntToast
    Setup-AutoStart

    Show-InfoBox $SABLE_URL $SABLE_PORT
    Open-Browser $SABLE_URL

    Write-Info "Starting server..."
    $env:TERM = "xterm-256color"
    cmd /c "uv run python server.py 2>&1"
}

Main
