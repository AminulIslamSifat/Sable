@echo off
:: Sable Start Script for Windows - double-click to launch.
:: Normal launch = fully silent (no console window stays open).
:: Pass --foreground to keep a visible log console for debugging.

cd /d "%~dp0"

set SABLE_FOREGROUND=0
if "%~1"=="--foreground" set SABLE_FOREGROUND=1

if not defined SABLE_PORT set SABLE_PORT=61770

where powershell >nul 2>&1
if %errorlevel% neq 0 (
    echo [Sable] ERROR: PowerShell not found. Cannot start.
    pause
    exit /b 1
)

:: Foreground mode: show the console (for debugging).
if "%SABLE_FOREGROUND%"=="1" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"
    exit /b %errorlevel%
)

:: Silent mode: hand off to wscript + a temp VBS so NO console window
:: survives. wscript.exe is a GUI-subsystem binary -> it never allocates
:: a console, and it launches PowerShell with window flag 0 (hidden).
:: This bat exits immediately, so the double-click cmd window closes at once.
set "VBS=%TEMP%\sable_silent_%RANDOM%.vbs"
> "%VBS%" echo Set sh = CreateObject("WScript.Shell")
>>"%VBS%" echo sh.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""%~dp0start.ps1""", 0, False
wscript //nologo "%VBS%"
del /q "%VBS%" 2>nul
exit /b 0
