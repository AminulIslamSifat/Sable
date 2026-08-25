
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from server.config import _AUTH_TOKEN_FILE, BASE_DIR
from server.utils import logger

router = APIRouter()

_SYSTEM_DIR = BASE_DIR / "system"


class SetPasswordRequest(BaseModel):
    password: str


@router.get("/api/setup/status")
async def setup_status() -> dict[str, Any]:
    """Check if initial setup is needed (no auth token set yet)."""
    has_token = _AUTH_TOKEN_FILE.exists() and _AUTH_TOKEN_FILE.read_text(encoding="utf-8").strip() != ""
    has_browser_profile = (_SYSTEM_DIR / "browser-data-acc1").is_dir()
    return {
        "needs_password": not has_token,
        "needs_browser_login": not has_browser_profile,
        "setup_complete": has_token and has_browser_profile,
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


@router.post("/api/setup/browser-login")
async def browser_login() -> dict[str, Any]:
    """Launch browser_opener for Qwen login during first-run setup."""
    profile_path = _SYSTEM_DIR / "browser-data-acc1"

    async def _run_browser() -> None:
        from playwright.async_api import async_playwright
        try:
            # WSL2 → launch Windows-side Chrome via CDP
            from engine.wsl_browser import launch_windows_chrome
            wsl_session = launch_windows_chrome(
                str(profile_path), port=9301, headless=False,
                extra_args=[
                    "--disk-cache-size=2097152",
                    "--disable-gpu-shader-cache",
                    "--disable-component-update",
                ],
            )
            if wsl_session is not None:
                logger.info("WSL2: connected to Windows Chrome at %s", wsl_session.cdp_url)
                async with async_playwright() as p:
                    browser = await p.chromium.connect_over_cdp(wsl_session.cdp_url)
                    context = browser.contexts[0] if browser.contexts else await browser.new_context()
                    page = context.pages[0] if context.pages else await context.new_page()
                    await page.goto("https://chat.qwen.ai")
                    # Keep alive until user closes the window
                    try:
                        await page.wait_for_event("close", timeout=0)
                    except Exception:
                        pass
                return

            # Native Linux — original Playwright headed launch
            async with async_playwright() as p:
                context = await p.chromium.launch_persistent_context(
                    user_data_dir=str(profile_path),
                    headless=False,
                    timeout=0,
                    args=[
                        "--no-sandbox",
                        "--disable-blink-features=AutomationControlled",
                        "--disk-cache-size=2097152",
                        "--disable-gpu-shader-cache",
                        "--disable-component-update",
                    ],
                )
                page = context.pages[0] if context.pages else await context.new_page()
                await page.goto("https://chat.qwen.ai")
                await context.wait_for_event("close", timeout=0)
        except Exception as e:
            logger.warning("Setup browser login exited: %s", e)

    asyncio.create_task(_run_browser())
    return {"status": "opened", "profile": "browser-data-acc1"}
#
