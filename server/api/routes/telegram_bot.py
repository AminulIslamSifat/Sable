"""Telegram Bot settings endpoints — configure bot token and preferences.

Standalone module. Does NOT modify any existing Sable code.
Bot token stored in system/.telegram_bot_config.json
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from engine.config import PERSISTENT_ROOT

router = APIRouter(prefix="/api/telegram-bot", tags=["telegram-bot"])

_CONFIG_PATH = PERSISTENT_ROOT / "system" / ".telegram_bot_config.json"


def _load() -> dict[str, Any]:
    if not _CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(cfg: dict[str, Any]) -> None:
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


class TelegramBotConfig(BaseModel):
    bot_token: str | None = None
    allowed_users: list[int] | None = None
    enabled: bool | None = None


@router.get("/config")
async def get_bot_config():
    """Get current Telegram bot configuration (token masked)."""
    cfg = _load()
    safe = dict(cfg)
    if safe.get("bot_token"):
        tok = str(safe["bot_token"])
        safe["bot_token_masked"] = tok[:6] + "..." + tok[-4:] if len(tok) > 10 else "***"
        safe.pop("bot_token", None)
    safe["has_token"] = bool(cfg.get("bot_token"))
    return safe


@router.post("/config")
async def save_bot_config(req: TelegramBotConfig):
    """Save Telegram bot configuration.

    Merges with existing config so partial updates work.
    Setting bot_token to empty string clears it.
    """
    existing = _load()

    if req.bot_token is not None:
        existing["bot_token"] = req.bot_token
    if req.allowed_users is not None:
        existing["allowed_users"] = req.allowed_users
    if req.enabled is not None:
        existing["enabled"] = req.enabled

    _save(existing)
    return {"ok": True, "has_token": bool(existing.get("bot_token"))}


@router.get("/status")
async def bot_status():
    """Check if bot is configured and running."""
    cfg = _load()
    has_token = bool(cfg.get("bot_token"))
    enabled = cfg.get("enabled", True)

    # Check if bot process is actually running
    running = False
    try:
        from telegram_bot.bot import _bot_app  # noqa: F401
        running = _bot_app is not None
    except Exception:
        pass

    # Auto-detect server URL
    try:
        from engine.config import PORT, HOST
        host = "localhost" if HOST in ("0.0.0.0", "::") else HOST
        server_url = f"http://{host}:{PORT}"
    except ImportError:
        import os
        port = os.getenv("SABLE_PORT", os.getenv("PORT", "61770"))
        server_url = f"http://localhost:{port}"

    return {
        "configured": has_token,
        "enabled": enabled,
        "running": running,
        "server_url": server_url,
        "allowed_users": cfg.get("allowed_users", []),
    }


@router.post("/start")
async def start_bot():
    """Manually start the Telegram bot (useful when auto-start failed)."""
    import asyncio as _aio

    try:
        from telegram_bot.bot import _bot_app, start_bot_in_background
    except Exception as e:
        return {"ok": False, "error": f"Import failed: {e}"}

    if _bot_app is not None:
        return {"ok": True, "message": "Bot already running"}

    # Start in background task
    _aio.create_task(start_bot_in_background())
    return {"ok": True, "message": "Bot starting..."}


@router.post("/stop")
async def stop_bot():
    """Stop the Telegram bot."""
    try:
        from telegram_bot.bot import _bot_app
    except Exception:
        return {"ok": False, "error": "Bot module not loaded"}

    if _bot_app is None:
        return {"ok": True, "message": "Bot not running"}

    try:
        await _bot_app.updater.stop()
        await _bot_app.stop()
        await _bot_app.shutdown()
        return {"ok": True, "message": "Bot stopped"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
