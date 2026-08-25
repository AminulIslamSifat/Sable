
from __future__ import annotations

import asyncio
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


# Module-level handle so we can keep the browser alive across requests
_setup_browser_context = None


@router.post("/api/setup/browser-login")
async def browser_login() -> dict[str, Any]:
    """Launch headed browser for Qwen login during first-run setup.

    Unlike the old fire-and-forget approach, this now waits for the browser
    to actually open before returning. Errors are returned to the frontend
    instead of being silently swallowed.
    """
    global _setup_browser_context
    profile_path = _SYSTEM_DIR / "browser-data-acc1"
    profile_path.mkdir(parents=True, exist_ok=True)

    try:
        from playwright.async_api import async_playwright
    except ImportError as e:
        raise HTTPException(
            status_code=500,
            detail=(
                f"Playwright import failed: {e}\n\n"
                f"This is a known Windows issue — greenlet's DLL needs the Visual C++ runtime.\n\n"
                f"Fix (pick one):\n"
                f"  A) Install VC++ Redistributable: https://aka.ms/vs/17/release/vc_redist.x64.exe\n"
                f"  B) Pin older greenlet: uv pip install \"greenlet==1.1.3\"\n\n"
                f"Then restart Sable."
            ),
        )

    # WSL2 → launch Windows-side Chrome via CDP
    try:
        from engine.wsl_browser import launch_windows_chrome
        wsl_session = launch_windows_chrome(
            str(profile_path), port=9301, headless=False,
            extra_args=[
                "--disk-cache-size=2097152",
                "--disable-gpu-shader-cache",
                "--disable-component-update",
            ],
        )
    except Exception:
        wsl_session = None

    if wsl_session is not None:
        logger.info("WSL2: connected to Windows Chrome at %s", wsl_session.cdp_url)
        try:
            pw = await async_playwright().start()
            browser = await pw.chromium.connect_over_cdp(wsl_session.cdp_url)
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto("https://chat.qwen.ai")
            _setup_browser_context = context  # keep alive
            return {"status": "opened", "profile": "browser-data-acc1"}
        except Exception as e:
            logger.error("WSL2 browser login failed: %s", e)
            raise HTTPException(status_code=500, detail=f"WSL2 browser launch failed: {e}")

    # Native launch (Linux or Windows)
    launch_args = [
        "--disable-blink-features=AutomationControlled",
        "--disk-cache-size=2097152",
        "--disable-gpu-shader-cache",
        "--disable-component-update",
    ]
    # --no-sandbox only on Linux; on Windows it's not needed and can cause issues
    if not _IS_WINDOWS:
        launch_args.insert(0, "--no-sandbox")

    try:
        pw = await async_playwright().start()
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_path),
            headless=False,
            timeout=30000,
            args=launch_args,
        )
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto("https://chat.qwen.ai", timeout=30000)
        _setup_browser_context = context  # keep alive
        logger.info("Setup browser opened successfully (profile=%s)", profile_path)
        return {"status": "opened", "profile": "browser-data-acc1"}
    except Exception as e:
        logger.error("Setup browser login FAILED: %s", e, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=(
                f"Failed to open browser: {e}\n\n"
                f"Troubleshooting:\n"
                f"1. Run 'uv run playwright install chromium' to ensure browsers are installed\n"
                f"2. Check that no other process is using the profile: {profile_path}\n"
                f"3. On Windows, ensure Playwright is installed in the same Python environment"
            ),
        )
#
