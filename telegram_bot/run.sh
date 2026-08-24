#!/usr/bin/env bash
# Run Sable Telegram Bot (standalone, no Sable core modifications)
set -euo pipefail
cd "$(dirname "$0")/.."
echo "🤖 Starting Sable Telegram Bot..."
uv run python3 -m telegram_bot.bot
