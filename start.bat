@echo off
rem ─────────────────────────────────────────────────────────────────────────────
rem  Sable setup / launcher entrypoint (Windows).
rem  Prefers the sealed starter; falls back to the plaintext dev tree.
rem ─────────────────────────────────────────────────────────────────────────────
setlocal EnableDelayedExpansion
set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

if not exist "%SCRIPT_DIR%sable-launch.cmd" (
    echo [Sable] ERROR: sable-launch.cmd not found in %SCRIPT_DIR% >&2
    pause
    exit /b 1
)

call "%SCRIPT_DIR%sable-launch.cmd" %*
exit /b %errorlevel%
