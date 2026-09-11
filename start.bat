@echo off
:: Sable Start Script for Windows - double-click to launch
:: Delegates to start.ps1 for full zero-intervention bootstrap.
cd /d "%~dp0"

set SABLE_FOREGROUND=0
if "%~1"=="--foreground" set SABLE_FOREGROUND=1

if not defined SABLE_PORT set SABLE_PORT=61770

:: Check if PowerShell is available
where powershell >nul 2>&1
if %errorlevel% neq 0 (
    echo [Sable] ERROR: PowerShell not found. Cannot start.
    pause
    exit /b 1
)

:: Delegate to the real script (hidden by default, --foreground for debugging)
if "%SABLE_FOREGROUND%"=="1" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"
) else (
    powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0start.ps1"
)
