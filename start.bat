@echo off
setlocal enabledelayedexpansion
:: Sable Start Script for Windows - double-click to launch.
:: Runs setup checks visibly, then starts the server in background.
:: Use the desktop shortcut to also get a live log viewer + browser.

:: Force clean state - ignore any leaked env vars from parent shell
set "SABLE_BACKGROUND=0"

:: Resolve script directory safely
set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR%"=="" (
    echo [Sable] ERROR: Could not determine script directory.
    pause
    exit /b 1
)

:: Change to script directory
cd /d "%SCRIPT_DIR%"
if %errorlevel% neq 0 (
    echo [Sable] ERROR: Could not change to directory %SCRIPT_DIR%
    pause
    exit /b 1
)

:: Sanity Check: Ensure we are in the project root
if not exist "server.py" (
    echo [Sable] CRITICAL ERROR: server.py not found in %SCRIPT_DIR%
    echo [Sable] This script must be run from the Sable project root.
    pause
    exit /b 1
)

if "%~1"=="--background" (
    set SABLE_BACKGROUND=1
) else (
    set SABLE_BACKGROUND=0
)

if not defined SABLE_PORT set SABLE_PORT=61770

where powershell >nul 2>&1
if %errorlevel% neq 0 (
    echo [Sable] ERROR: PowerShell not found. Cannot start.
    pause
    exit /b 1
)

:: Background mode: relaunch via PowerShell Start-Process (no VBS needed)
if "%SABLE_BACKGROUND%"=="1" (
    powershell -NoProfile -Command "Start-Process powershell -ArgumentList '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File \"%SCRIPT_DIR%start.ps1\"' -WindowStyle Hidden"
    exit /b 0
)

:: Foreground: show setup logs, server starts in background and script exits.
powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%start.ps1"
exit /b %errorlevel%
