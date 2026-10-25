@echo off
rem Sable launcher (Windows) - runs the plaintext dev tree via uv/python.
setlocal EnableDelayedExpansion
set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

if not defined SABLE_PORT set "SABLE_PORT=61770"

where uv >nul 2>&1
if %errorlevel% equ 0 (
    uv run python server.py %*
    exit /b %errorlevel%
)

where python >nul 2>&1
if %errorlevel% equ 0 (
    python server.py %*
    exit /b %errorlevel%
)

echo [Sable] ERROR: neither uv nor python found on PATH >&2
exit /b 1