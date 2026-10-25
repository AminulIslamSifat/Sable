#!/usr/bin/env bash
# launch-test.sh — run the AppImage with a throwaway HOME, detached properly.
set -euo pipefail

APP=/home/sifat/hdd/projects/test/Sable-exp/build_app/release/Sable-2.0.3-x86_64.AppImage
HOME_DIR=${1:-/tmp/freshhome}
LOG=${2:-/tmp/fresh-run.log}

rm -rf "$HOME_DIR"
mkdir -p "$HOME_DIR"

# setsid detaches into its own session so the parent shell exiting can't kill it.
setsid env HOME="$HOME_DIR" "$APP" --no-sandbox > "$LOG" 2>&1 < /dev/null &

disown || true
echo "launched pid=$! home=$HOME_DIR log=$LOG"
