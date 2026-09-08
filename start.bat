@echo off
:: Sable Start Script for Windows - double-click to launch
:: Delegates to start.ps1 for full zero-intervention bootstrap.
cd /d "%~dp0"

set SABLE_BACKGROUND=0
if "%~1"=="--background" set SABLE_BACKGROUND=1

if not defined SABLE_PORT set SABLE_PORT=61770

:: Check if PowerShell is available
where powershell >nul 2>&1
if %errorlevel% neq 0 (
    echo [Sable] ERROR: PowerShell not found. Cannot start.
    pause
    exit /b 1
)

:: Delegate to the real script
if "%SABLE_BACKGROUND%"=="1" (
    powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0start.ps1"
) else (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"
)
