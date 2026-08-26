@echo off
:: Sable Start Script for Windows — double-click to launch
:: Uses %~dp0 (script directory) so it works for any user/install path
cd /d "%~dp0"

if not defined SABLE_PORT set SABLE_PORT=61770

:: Check uv is available
where uv >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] uv not found in PATH.
    echo Install uv: https://docs.astral.sh/uv/
    pause
    exit /b 1
)

:: First-run: copy template files if missing
if exist "instruction\Maria.md.example" if not exist "instruction\Maria.md" (
    copy "instruction\Maria.md.example" "instruction\Maria.md" >nul
    echo Created instruction\Maria.md from template
)
if exist "Brain\Memory.json.example" if not exist "Brain\Memory.json" (
    copy "Brain\Memory.json.example" "Brain\Memory.json" >nul
    echo Created Brain\Memory.json from template
)

:: Ensure system directories exist
if not exist "system" mkdir "system"
if not exist "system\browser-data-acc1" mkdir "system\browser-data-acc1"

:: Sync dependencies
echo Synchronizing dependencies...
call uv sync --extra windows
echo.

:: Info
echo +------------------------------------------------------+
echo ^| Sable is running!                                    ^|
echo ^|                                                      ^|
echo ^| URL:     http://127.0.0.1:%SABLE_PORT%                ^|
echo ^| Port:    %SABLE_PORT%                                ^|
echo ^|                                                      ^|
echo ^| Stop:    Ctrl+C                                      ^|
echo +------------------------------------------------------+
echo.

:: Auto-open browser after server starts (non-blocking)
start "" cmd /c "timeout /t 8 /nobreak >nul && start http://127.0.0.1:%SABLE_PORT%"

:: Start server
set TERM=xterm-256color
call uv run python server.py
