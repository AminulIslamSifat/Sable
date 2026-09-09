#!/usr/bin/env bash
# Sable smart launcher — checks if server is running, starts if needed, opens browser
SABLE_PORT="${SABLE_PORT:-61770}"
SABLE_URL="http://127.0.0.1:${SABLE_PORT}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

is_server_running() {
    curl -sf --max-time 2 "${SABLE_URL}/api/health" >/dev/null 2>&1 || \
    curl -sf --max-time 2 "${SABLE_URL}/" >/dev/null 2>&1
}

open_browser() {
    if command -v xdg-open >/dev/null 2>&1; then
        xdg-open "$SABLE_URL" 2>/dev/null &
    elif command -v python3 >/dev/null 2>&1; then
        python3 -c "import webbrowser; webbrowser.open('${SABLE_URL}')" 2>/dev/null &
    fi
}

if is_server_running; then
    open_browser
else
    # Start server in background via the main start script, wait, then open
    cd "$SCRIPT_DIR" || exit 1
    nohup bash "$SCRIPT_DIR/start" --background >/dev/null 2>&1 &

    # Wait up to 30s for server to come up
    elapsed=0
    while [ $elapsed -lt 30 ]; do
        sleep 1
        ((elapsed++))
        if is_server_running; then
            open_browser
            exit 0
        fi
    done

    # Timeout — try opening anyway
    open_browser
fi
