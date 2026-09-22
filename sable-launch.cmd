@echo off
rem ─────────────────────────────────────────────────────────────────────────────
rem  Sable launcher (Windows) — boots the sealed vault via sable-starter.
rem  Starter resolution: %SABLE_STARTER% -> sable-tools\starter\sable-starter.exe
rem                      -> sable-starter.exe -> starter\sable-starter.exe
rem                      -> server.py (plaintext dev tree)
rem ─────────────────────────────────────────────────────────────────────────────
setlocal EnableDelayedExpansion
set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

if not defined SABLE_PORT  set "SABLE_PORT=61770"
if not defined SABLE_VAULT set "SABLE_VAULT=%SCRIPT_DIR%vault.sable"

set "STARTER="
if defined SABLE_STARTER (
    if exist "%SABLE_STARTER%" set "STARTER=%SABLE_STARTER%"
)
if not defined STARTER (
    for %%P in (
        "%SCRIPT_DIR%sable-tools\starter\sable-starter.exe"
        "%SCRIPT_DIR%sable-starter.exe"
        "%SCRIPT_DIR%starter\sable-starter.exe"
    ) do (
        if not defined STARTER if exist "%%~fP" set "STARTER=%%~fP"
    )
)

if defined STARTER (
    echo [Sable] starter: %STARTER%
    echo [Sable] vault:   %SABLE_VAULT%
    "%STARTER%" --vault "%SABLE_VAULT%" --port %SABLE_PORT% --run
    exit /b %errorlevel%
)

if exist "%SCRIPT_DIR%server.py" (
    echo [Sable] no starter, no vault - plaintext dev tree
    where uv >nul 2>&1 && ( uv run python server.py & exit /b %errorlevel% )
    where python >nul 2>&1 && ( python server.py & exit /b %errorlevel% )
)

echo [Sable] nothing to launch: no starter, no vault.sable, no server.py >&2
exit /b 1
