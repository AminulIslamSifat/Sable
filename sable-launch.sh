#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  Sable launcher — boots the SEALED vault through sable-starter.
#
#  Starter resolution order:
#    1. $SABLE_STARTER                             (explicit override)
#    2. ./sable-tools/starter/sable-starter-bin    (bundled)
#    3. ./sable-starter                            (beside the vault)
#    4. sable-starter on PATH
#    5. ./sable-tools/sable-boot                   (python fallback)
#    6. ./server.py                                (plaintext dev tree)
#
#  Usage:  sable-launch.sh [--no-browser] [--port=N] [-- <starter args>]
#  Any unrecognised argument is forwarded to the starter verbatim.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

VAULT="${SABLE_VAULT:-$SCRIPT_DIR/vault.sable}"
PORT="${SABLE_PORT:-61770}"
URL="http://127.0.0.1:${PORT}"
NO_BROWSER=0

_log() { printf '[Sable] %s\n' "$*"; }
_die() { printf '[Sable] %s\n' "$*" >&2; exit 1; }

# ── split our own flags from starter passthrough ────────────────
PASSTHROUGH=()
while [ $# -gt 0 ]; do
    case "$1" in
        --no-browser) NO_BROWSER=1; shift ;;
        --port=*)     PORT="${1#*=}"; URL="http://127.0.0.1:${PORT}"; shift ;;
        *)            PASSTHROUGH+=("$@"); break ;;
    esac
done

open_browser() {
    [ "$NO_BROWSER" -eq 1 ] && return 0
    if command -v xdg-open >/dev/null 2>&1; then
        xdg-open "$URL" >/dev/null 2>&1 &
    elif command -v python3 >/dev/null 2>&1; then
        python3 -c "import webbrowser;webbrowser.open('$URL')" >/dev/null 2>&1 &
    fi
    return 0
}

find_starter() {
    if [ -n "${SABLE_STARTER:-}" ]; then
        [ -x "$SABLE_STARTER" ] || _die "SABLE_STARTER not executable: $SABLE_STARTER"
        printf '%s\n' "$SABLE_STARTER"; return 0
    fi
    for c in \
        "$SCRIPT_DIR/sable-tools/starter/sable-starter-bin" \
        "$SCRIPT_DIR/sable-tools/starter/sable-starter" \
        "$SCRIPT_DIR/sable-starter" \
        "$SCRIPT_DIR/starter/sable-starter-bin"
    do
        [ -x "$c" ] && { printf '%s\n' "$c"; return 0; }
    done
    if command -v sable-starter >/dev/null 2>&1; then
        command -v sable-starter; return 0
    fi
    return 1
}

# ── 1) starter ──────────────────────────────────────────────────
if STARTER="$(find_starter)"; then
    _log "starter: $STARTER"
    if [ "${#PASSTHROUGH[@]}" -gt 0 ]; then
        exec "$STARTER" "${PASSTHROUGH[@]}"
    fi
    _log "vault:   $VAULT"
    open_browser
    exec "$STARTER" --vault "$VAULT" --port "$PORT" --run
fi

# ── 2) python boot over a vault ─────────────────────────────────
if [ -f "$VAULT" ] && [ -x "$SCRIPT_DIR/sable-tools/sable-boot" ]; then
    _log "no starter binary — python boot"
    open_browser
    exec "$SCRIPT_DIR/sable-tools/sable-boot" "$VAULT" --run
fi

# ── 3) plaintext dev tree ───────────────────────────────────────
if [ -f "$SCRIPT_DIR/server.py" ]; then
    _log "no starter, no vault — plaintext dev tree"
    open_browser
    exec uv run python server.py
fi

_die "nothing to launch: no starter, no vault.sable, no server.py"
