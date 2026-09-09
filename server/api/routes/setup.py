
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

    # Check if any API provider has keys configured
    has_api_provider = False
    try:
        from server.api.routes.misc import _get_available_backends
        backends = _get_available_backends()
        # 'local' is always available; check for real providers
        has_api_provider = len(backends - {"local"}) > 0
    except Exception:
        pass

    setup_done = has_account or has_api_provider
    return {
        "needs_password": not has_token,
        "needs_setup": not setup_done,
        "setup_complete": has_token and setup_done,
        "has_browser_account": has_account,
        "has_api_provider": has_api_provider,
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
    # Set default persona to Agent for first-time users
    _set_default_persona()
    return {"status": "ok"}


def _set_default_persona() -> None:
    """Set the active persona to Agent.md for first-run setup."""
    import json as _json
    instruction_dir = BASE_DIR / "instruction"
    config_path = instruction_dir / ".persona_config.json"
    try:
        cfg: dict[str, Any] = {}
        if config_path.exists():
            cfg = _json.loads(config_path.read_text(encoding="utf-8"))
        # Only override if no persona is explicitly set or it's still the default
        if not cfg.get("active") or cfg.get("active") == "Maria":
            cfg["active"] = "Agent"
            cfg.setdefault("disabled", [])
            cfg["output_format_enabled"] = False  # Agent mode doesn't use output format
            config_path.write_text(_json.dumps(cfg, indent=2), encoding="utf-8")
            logger.info("First-run: set default persona to Agent")
    except Exception as exc:
        logger.warning("Failed to set default persona: %s", exc)


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


# ── Setup API key + auto-register models ───────────────────────────────────

@router.post("/api/setup/api-key")
async def setup_add_api_key(payload: dict[str, str]) -> dict[str, Any]:
    """Save an API key during first-run setup and auto-register provider models.

    Body: {"provider": "gemini", "api_key": "AIza..."}
    Saves the key via the existing provider endpoint, then fetches available
    models from that provider's API and registers them as custom models.
    """
    import httpx as _httpx
    from engine.config import add_custom_model, get_all_models

    provider = (payload.get("provider") or "").strip().lower()
    api_key = (payload.get("api_key") or "").strip()
    if not provider or not api_key:
        raise HTTPException(status_code=400, detail="Missing provider or api_key")

    # Step 1: Save the API key using existing logic
    saved = False
    try:
        if provider == "gemini":
            from connectors.gemini.client import get_client as get_gemini_client
            client = get_gemini_client()
            if api_key not in client._keys:
                client.add_key(api_key)
            saved = True
        elif provider == "groq":
            from connectors.groq.client import get_client as get_groq_client
            client = get_groq_client()
            if api_key not in client._keys:
                client.add_key(api_key)
            saved = True
        elif provider == "mistral":
            from connectors.mistral.client import get_client as get_mistral_client
            client = get_mistral_client()
            if api_key not in client._keys:
                client.add_key(api_key)
            saved = True
        elif provider == "openai":
            from connectors.openai.client import get_client as get_openai_client
            client = get_openai_client()
            if api_key not in client._keys:
                client.add_key(api_key)
            saved = True
        elif provider == "deepseek":
            from connectors.deepseek.client import save_token_for_account
            save_token_for_account(api_key)
            saved = True
        else:
            raise HTTPException(status_code=400, detail=f"Unknown provider: {provider}")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to save key: {exc}")

    if not saved:
        raise HTTPException(status_code=500, detail="Could not save API key")

    # Step 2: Fetch available models from the provider
    fetched_models: list[dict[str, Any]] = []
    try:
        if provider == "gemini":
            async with _httpx.AsyncClient(timeout=15.0) as http:
                r = await http.get(
                    "https://generativelanguage.googleapis.com/v1beta/models",
                    params={"key": api_key, "pageSize": 100},
                )
                r.raise_for_status()
                data = r.json()
                for m in data.get("models", []):
                    name = m.get("name", "").replace("models/", "")
                    display = m.get("displayName", name)
                    methods = m.get("supportedGenerationMethods", [])
                    if "generateContent" in methods:
                        fetched_models.append({"id": name, "label": display})

        elif provider == "groq":
            async with _httpx.AsyncClient(timeout=15.0) as http:
                r = await http.get(
                    "https://api.groq.com/openai/v1/models",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                r.raise_for_status()
                data = r.json()
                for m in data.get("data", []):
                    mid = m.get("id", "")
                    if mid:
                        label = mid.replace("-", " ").replace("_", " ").title()
                        fetched_models.append({"id": mid, "label": label})

        elif provider == "mistral":
            async with _httpx.AsyncClient(timeout=15.0) as http:
                r = await http.get(
                    "https://api.mistral.ai/v1/models",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                r.raise_for_status()
                data = r.json()
                for m in data.get("data", []):
                    mid = m.get("id", "")
                    if mid:
                        label = mid.replace("-", " ").replace("_", " ").title()
                        fetched_models.append({"id": mid, "label": label})

        elif provider == "openai":
            async with _httpx.AsyncClient(timeout=15.0) as http:
                r = await http.get(
                    "https://api.openai.com/v1/models",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                r.raise_for_status()
                data = r.json()
                skip_kw = ("embedding", "tts", "whisper", "dall-e", "babbage", "davinci")
                for m in data.get("data", []):
                    mid = m.get("id", "")
                    if mid and not any(s in mid for s in skip_kw):
                        label = mid.replace("-", " ").replace("_", " ").title()
                        fetched_models.append({"id": mid, "label": label})
                fetched_models.sort(key=lambda x: x["id"])

        elif provider == "deepseek":
            # DeepSeek uses fixed model types, not a dynamic list
            fetched_models = [
                {"id": "default", "label": "DeepSeek Instant"},
                {"id": "expert", "label": "DeepSeek Expert"},
                {"id": "vision", "label": "DeepSeek Vision"},
            ]

    except Exception as exc:
        logger.warning("Setup: failed to fetch models for %s: %s", provider, exc)

    # Step 3: Register fetched models as custom models (skip duplicates of static ones)
    existing_ids = {m["id"] for m in get_all_models()}
    registered: list[str] = []
    for fm in fetched_models:
        mid = fm["id"]
        if mid in existing_ids:
            continue  # already exists as static or custom
        thinking_modes = [
            {"id": "fast", "label": "Fast", "thinking_enabled": False, "auto_thinking": False, "thinking_mode": "Fast"},
        ]
        # Add thinking mode for known reasoning models
        if any(kw in mid.lower() for kw in ("think", "reason", "o1", "o3", "r1")):
            thinking_modes.append(
                {"id": "high", "label": "Thinking", "thinking_enabled": True, "auto_thinking": False, "thinking_mode": "High"}
            )
        model_def: dict[str, Any] = {
            "id": mid,
            "label": fm["label"],
            "api_backend": provider,
            "api_model_type": mid,
            "max_session_chars": 500_000,
            "capabilities": {"image": False, "video": False, "document": False, "audio": False},
            "thinking_modes": thinking_modes,
            "_custom": True,
        }
        add_custom_model(model_def)
        registered.append(mid)
        existing_ids.add(mid)

    logger.info("Setup: added %s key, registered %d models: %s", provider, len(registered), registered)
    return {
        "status": "ok",
        "provider": provider,
        "models_registered": len(registered),
        "model_ids": registered,
    }

