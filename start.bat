@echo off
setlocal enabledelayedexpansion
:: Sable Start Script for Windows - double-click to launch.
:: Normal launch = visible log console (foreground).
:: Pass --background to run fully silent (no console window stays open).

:: Force clean state — ignore any leaked env vars from parent shell
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

set SABLE_BACKGROUND=0
if "%~1"=="--background" set SABLE_BACKGROUND=1

if not defined SABLE_PORT set SABLE_PORT=61770

where powershell >nul 2>&1
if %errorlevel% neq 0 (
    echo [Sable] ERROR: PowerShell not found. Cannot start.
    pause
    exit /b 1
)

:: Background mode: hand off to wscript + a temp VBS so NO console window
:: survives. wscript.exe is a GUI-subsystem binary -> it never allocates
:: a console, and it launches PowerShell with window flag 0 (hidden).
:: This bat exits immediately, so the double-click cmd window closes at once.
if "%SABLE_BACKGROUND%"=="1" (
    set "VBS=%TEMP%\sable_silent_%RANDOM%.vbs"
    >"%VBS%" echo Set sh = CreateObject("WScript.Shell")
    if not exist "%VBS%" (
        echo [Sable] ERROR: Could not create temporary VBS file.
        pause
        exit /b 1
    )
    >>"%VBS%" echo sh.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""%SCRIPT_DIR%start.ps1""", 0, False
    wscript //nologo "%VBS%"
    if !errorlevel! neq 0 (
        echo [Sable] WARNING: wscript failed to launch background process.
    )
    del /q "%VBS%" 2>nul
    exit /b 0
)

:: Foreground mode (default): show the console with live logs.
echo [Sable] Starting in foreground mode...
powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%start.ps1"
exit /b %errorlevel%
