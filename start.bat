@echo off
:: Sable Start Script for Windows - double-click to launch.
:: Normal launch = visible log console (foreground).
:: Pass --background to run fully silent (no console window stays open).

cd /d "%~dp0"

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
    > "%VBS%" echo Set sh = CreateObject("WScript.Shell")
    >>"%VBS%" echo sh.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""%~dp0start.ps1""", 0, False
    wscript //nologo "%VBS%"
    del /q "%VBS%" 2>nul
    exit /b 0
)

:: Foreground mode (default): show the console with live logs.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"
exit /b %errorlevel%
