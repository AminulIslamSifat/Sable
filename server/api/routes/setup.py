
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from server.config import _AUTH_TOKEN_FILE, BASE_DIR
from server.utils import logger

_IS_WINDOWS = sys.platform == "win32"

router = APIRouter()

_SYSTEM_DIR = BASE_DIR / "system"


class SetPasswordRequest(BaseModel):
    password: str


def _has_any_account() -> bool:
    """Return True if at least one browser-data-accN profile exists."""
    if not _SYSTEM_DIR.is_dir():
        return False
    for d in _SYSTEM_DIR.iterdir():
        if d.is_dir() and re.match(r"browser-data-acc\d+$", d.name):
            return True
    return False


@router.get("/api/setup/status")
async def setup_status() -> dict[str, Any]:
    """Check if initial setup is needed (no auth token set yet)."""
    has_token = _AUTH_TOKEN_FILE.exists() and _AUTH_TOKEN_FILE.read_text(encoding="utf-8").strip() != ""
    has_account = _has_any_account()
    return {
        "needs_password": not has_token,
        "needs_setup": not has_account,
        "setup_complete": has_token and has_account,
    }


@router.post("/api/setup/password")
async def set_password(payload: SetPasswordRequest) -> dict[str, str]:
    """Set the auth token during first-run setup."""
    password = payload.password.strip()
    if not password:
        raise HTTPException(status_code=400, detail="Password cannot be empty")
    _AUTH_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    _AUTH_TOKEN_FILE.write_text(password, encoding="utf-8")
    # Reload the in-memory token so login works immediately without restart
    try:
        import server.auth as auth_mod
        auth_mod.AUTH_TOKEN = password
    except Exception as exc:
        logger.warning("Failed to reload auth token in memory: %s", exc)
    return {"status": "ok"}


@router.get("/api/setup/available-browsers")
async def available_browsers() -> dict[str, Any]:
    """List available browsers for the setup flow (mirrors settings endpoint)."""
    from engine.platform_paths import list_available_browsers
    browsers = await asyncio.to_thread(list_available_browsers)
    return {"browsers": browsers}


@router.post("/api/setup/browser-login")
async def browser_login(payload: dict[str, str] | None = None) -> dict[str, Any]:
    """Launch headed browser for Qwen login during first-run setup.

    Mirrors /api/settings/accounts/create — uses the same browser resolution,
    extra args, and non-blocking task pattern so behaviour is identical.
    """
    def _next_acc() -> int:
        existing: set[int] = set()
        if _SYSTEM_DIR.is_dir():
            for d in _SYSTEM_DIR.iterdir():
                m = re.match(r"browser-data-acc(\d+)$", d.name)
                if m and d.is_dir():
                    existing.add(int(m.group(1)))
        n = 1
        while n in existing:
            n += 1
        return n

    acc_num = await asyncio.to_thread(_next_acc)
    profile_name = f"browser-data-acc{acc_num}"
    profile_path = _SYSTEM_DIR / profile_name
    profile_path.mkdir(parents=True, exist_ok=True)

    # Save user's browser choice
    chosen_browser = (payload or {}).get("browser_path", "")
    if chosen_browser:
        from server.api.routes.settings import _set_account_browser
        await asyncio.to_thread(_set_account_browser, profile_name, chosen_browser)

    # Resolve actual binary to use (same as settings flow)
    from engine.platform_paths import resolve_browser_for_profile, extra_browser_args
    resolved_browser = await asyncio.to_thread(resolve_browser_for_profile, profile_name)

    async def _run_browser() -> None:
        from playwright.async_api import async_playwright
        try:
            launch_kwargs: dict[str, Any] = {
                "user_data_dir": str(profile_path),
                "headless": False,
                "timeout": 0,
                "args": [
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disk-cache-size=2097152",
                    "--disable-gpu-shader-cache",
                    "--disable-component-update",
                ] + extra_browser_args(resolved_browser),
            }
            if resolved_browser:
                launch_kwargs["executable_path"] = resolved_browser
            async with async_playwright() as p:
                context = await p.chromium.launch_persistent_context(**launch_kwargs)
                page = context.pages[0] if context.pages else await context.new_page()
                await page.goto("https://chat.qwen.ai", timeout=120000)
                await context.wait_for_event("close", timeout=0)
        except Exception as e:
            logger.warning(f"Setup browser for {profile_name} exited: {e}")

    asyncio.create_task(_run_browser())
    return {"status": "opened", "profile": profile_name}
