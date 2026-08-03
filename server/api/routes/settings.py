from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from engine.scraper import (
    get_settings as get_scraper_settings,
    list_engines as list_scraper_engines,
    scraper as scraper_service,
    update_settings as update_scraper_settings,
)
from connectors.deepseek.client import get_client as get_deepseek_client

from server.config import (
    BASE_DIR, _SYSTEM_DIR, _ACTIVE_PROFILE_LINK, _BROWSER_PROFILES,
)
from server.utils import _dir_size_mb, _read_profile_email, logger
from ..dependencies import service

router = APIRouter()

# --- Browser profile stripping (shared by switch + manual strip endpoint) ---
_STRIP_KEEP = [
    "Local State", "Last Version",
    "Default/Cookies", "Default/Cookies-journal",
    "Default/Local Storage", "Default/Session Storage",
    "Default/IndexedDB", "Default/Preferences",
    "Default/Secure Preferences", "Default/Login Data",
    "Default/Login Data For Account", "Default/Web Data",
    "Default/Account Web Data", "Default/Network Action Predictor",
    "Default/Network Persistent State", "Default/TransportSecurity",
    "Default/Trust Tokens",
]


def _strip_one_profile(profile: Path) -> tuple[str, float, float]:
    """Strip a single browser profile to bare session data. Returns (name, before_mb, after_mb)."""
    import tempfile
    before = _dir_size_mb(profile)
    tmp = Path(tempfile.mkdtemp())
    for item in _STRIP_KEEP:
        src = profile / item
        if src.exists():
            dest = tmp / item
            dest.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(src, dest, symlinks=True)
            else:
                shutil.copy2(src, dest)
    shutil.rmtree(profile)
    profile.mkdir(parents=True)
    for item in tmp.iterdir():
        dest = profile / item.name
        if item.is_dir():
            shutil.copytree(item, dest, symlinks=True, dirs_exist_ok=True)
        else:
            shutil.copy2(item, dest)
    shutil.rmtree(tmp)
    after = _dir_size_mb(profile)
    return (profile.name, before, after)

@router.get("/api/settings/scraper")
async def get_scraper_settings_route() -> dict[str, Any]:
    return get_scraper_settings()

@router.get("/api/settings/scraper/engines")
async def get_scraper_engines_route() -> dict[str, Any]:
    return {"engines": list_scraper_engines()}

@router.post("/api/settings/scraper")
async def update_scraper_settings_route(payload: dict[str, Any]) -> dict[str, Any]:
    old_settings = get_scraper_settings()
    settings = update_scraper_settings(payload)
    engine_changed = old_settings.get("engine_type") != settings.get("engine_type")
    toggled_off = old_settings.get("enabled") and not settings.get("enabled")
    if engine_changed or toggled_off:
        await scraper_service.stop()
    if settings.get("enabled"):
        prelaunch_result = await scraper_service.prelaunch()
        settings["prelaunch"] = prelaunch_result
    return settings

@router.get("/api/settings/browser")
async def get_browser_settings() -> dict[str, bool]:
    return {"headless": service.browser_headless}

@router.post("/api/settings/browser")
async def update_browser_settings(payload: dict[str, bool]) -> dict[str, Any]:
    headless = payload.get("headless")
    if headless is None:
        raise HTTPException(status_code=400, detail="Missing 'headless' field")
    await service.restart_browser(headless=headless)
    return {"status": "ok", "headless": service.browser_headless}

@router.post("/api/settings/deepseek/refresh-token")
async def refresh_deepseek_token() -> dict[str, Any]:
    try:
        token = await get_deepseek_client().refresh_token()
        return {"status": "ok", "token_preview": token[:20] + "...", "active": True}
    except Exception as exc:
        logger.error("DeepSeek token refresh failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Refresh failed: {exc}")


@router.post("/api/settings/gemini/api-key")
async def add_gemini_api_key(request: Request) -> dict[str, Any]:
    """Add a Gemini API key to the pool."""
    from connectors.gemini.client import get_client as get_gemini_client
    body = await request.json()
    key = body.get("api_key", "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="Missing 'api_key' field")
    client = get_gemini_client()
    if key in client._keys:
        raise HTTPException(status_code=409, detail="Key already exists")
    client.add_key(key)
    return {"status": "ok", "keys": client.list_keys(), "available": client.is_available}


@router.get("/api/settings/gemini/keys")
async def list_gemini_keys() -> dict[str, Any]:
    """List all configured Gemini API keys (masked)."""
    from connectors.gemini.client import get_client as get_gemini_client
    client = get_gemini_client()
    return {"keys": client.list_keys(), "available": client.is_available}


@router.delete("/api/settings/gemini/api-key/{index}")
async def remove_gemini_api_key(index: int) -> dict[str, Any]:
    """Remove a Gemini API key by index."""
    from connectors.gemini.client import get_client as get_gemini_client
    client = get_gemini_client()
    if not client.remove_key(index):
        raise HTTPException(status_code=404, detail="Key not found at that index")
    return {"status": "ok", "keys": client.list_keys(), "available": client.is_available}


# ---------------------------------------------------------------------------
# Groq API key management
# ---------------------------------------------------------------------------

@router.post("/api/settings/groq/api-key")
async def add_groq_api_key(request: Request) -> dict[str, Any]:
    """Add a Groq API key to the pool."""
    from connectors.groq.client import get_client as get_groq_client
    body = await request.json()
    key = body.get("api_key", "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="Missing 'api_key' field")
    client = get_groq_client()
    if key in client._keys:
        raise HTTPException(status_code=409, detail="Key already exists")
    client.add_key(key)
    return {"status": "ok", "keys": client.list_keys(), "available": client.is_available}


@router.get("/api/settings/groq/keys")
async def list_groq_keys() -> dict[str, Any]:
    """List all configured Groq API keys (masked)."""
    from connectors.groq.client import get_client as get_groq_client
    client = get_groq_client()
    return {"keys": client.list_keys(), "available": client.is_available}


@router.delete("/api/settings/groq/api-key/{index}")
async def remove_groq_api_key(index: int) -> dict[str, Any]:
    """Remove a Groq API key by index."""
    from connectors.groq.client import get_client as get_groq_client
    client = get_groq_client()
    if not client.remove_key(index):
        raise HTTPException(status_code=404, detail="Key not found at that index")
    return {"status": "ok", "keys": client.list_keys(), "available": client.is_available}


# ---------------------------------------------------------------------------
# Mistral API key management
# ---------------------------------------------------------------------------

@router.post("/api/settings/mistral/api-key")
async def add_mistral_api_key(request: Request) -> dict[str, Any]:
    """Add a Mistral API key to the pool."""
    from connectors.mistral.client import get_client as get_mistral_client
    body = await request.json()
    key = body.get("api_key", "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="Missing 'api_key' field")
    client = get_mistral_client()
    if key in client._keys:
        raise HTTPException(status_code=409, detail="Key already exists")
    client.add_key(key)
    return {"status": "ok", "keys": client.list_keys(), "available": client.is_available}


@router.get("/api/settings/mistral/keys")
async def list_mistral_keys() -> dict[str, Any]:
    """List all configured Mistral API keys (masked)."""
    from connectors.mistral.client import get_client as get_mistral_client
    client = get_mistral_client()
    return {"keys": client.list_keys(), "available": client.is_available}


@router.delete("/api/settings/mistral/api-key/{index}")
async def remove_mistral_api_key(index: int) -> dict[str, Any]:
    """Remove a Mistral API key by index."""
    from connectors.mistral.client import get_client as get_mistral_client
    client = get_mistral_client()
    if not client.remove_key(index):
        raise HTTPException(status_code=404, detail="Key not found at that index")
    return {"status": "ok", "keys": client.list_keys(), "available": client.is_available}


# ---------------------------------------------------------------------------
# Provider model listing (fetch available models from a provider's API)
# ---------------------------------------------------------------------------

@router.get("/api/settings/providers/{provider}/models")
async def list_provider_models(provider: str) -> dict[str, Any]:
    """Fetch available models from a provider's API using configured keys.

    Supported providers: gemini, groq.
    Returns empty list if no API key is configured.
    """
    import httpx as _httpx

    provider = provider.lower().strip()

    if provider == "gemini":
        from connectors.gemini.client import get_client as get_gemini_client
        client = get_gemini_client()
        if not client.is_available:
            return {"models": [], "available": False}
        key = client._current_key
        try:
            async with _httpx.AsyncClient(timeout=15.0) as http:
                r = await http.get(
                    "https://generativelanguage.googleapis.com/v1beta/models",
                    params={"key": key, "pageSize": 100},
                )
                r.raise_for_status()
                data = r.json()
                models = []
                for m in data.get("models", []):
                    name = m.get("name", "").replace("models/", "")
                    display = m.get("displayName", name)
                    # Only include generateContent-capable models
                    methods = m.get("supportedGenerationMethods", [])
                    if "generateContent" in methods:
                        models.append({"id": name, "label": display})
                return {"models": models, "available": True}
        except Exception as exc:
            logger.warning("Gemini model fetch failed: %s", exc)
            return {"models": [], "available": True, "error": str(exc)}

    elif provider == "groq":
        from connectors.groq.client import get_client as get_groq_client
        client = get_groq_client()
        if not client.is_available:
            return {"models": [], "available": False}
        key = client._current_key
        try:
            async with _httpx.AsyncClient(timeout=15.0) as http:
                r = await http.get(
                    "https://api.groq.com/openai/v1/models",
                    headers={"Authorization": f"Bearer {key}"},
                )
                r.raise_for_status()
                data = r.json()
                models = []
                for m in data.get("data", []):
                    mid = m.get("id", "")
                    if mid:
                        # Generate a nice display name from the model id
                        label = mid.replace("-", " ").replace("_", " ").title()
                        models.append({"id": mid, "label": label})
                return {"models": models, "available": True}
        except Exception as exc:
            logger.warning("Groq model fetch failed: %s", exc)
            return {"models": [], "available": True, "error": str(exc)}

    elif provider == "mistral":
        from connectors.mistral.client import get_client as get_mistral_client
        client = get_mistral_client()
        if not client.is_available:
            return {"models": [], "available": False}
        key = client._current_key
        try:
            async with _httpx.AsyncClient(timeout=15.0) as http:
                r = await http.get(
                    "https://api.mistral.ai/v1/models",
                    headers={"Authorization": f"Bearer {key}"},
                )
                r.raise_for_status()
                data = r.json()
                models = []
                for m in data.get("data", []):
                    mid = m.get("id", "")
                    if mid:
                        label = mid.replace("-", " ").replace("_", " ").title()
                        models.append({"id": mid, "label": label})
                return {"models": models, "available": True}
        except Exception as exc:
            logger.warning("Mistral model fetch failed: %s", exc)
            return {"models": [], "available": True, "error": str(exc)}

    raise HTTPException(status_code=400, detail=f"Unknown provider: {provider}")


# ---------------------------------------------------------------------------
# Custom model management
# ---------------------------------------------------------------------------

@router.post("/api/settings/models")
async def add_model(request: Request) -> dict[str, Any]:
    """Add or update a custom model definition."""
    from engine.config import add_custom_model
    body = await request.json()
    mid = body.get("id", "").strip()
    label = body.get("label", "").strip()
    backend = body.get("api_backend", "").strip()
    if not mid or not label or not backend:
        raise HTTPException(status_code=400, detail="Missing id, label, or api_backend")
    model_def = {
        "id": mid,
        "label": label,
        "api_backend": backend,
        "api_model_type": body.get("api_model_type", mid),
        "capabilities": body.get("capabilities", {"image": False, "video": False, "document": False, "audio": False}),
        "thinking_modes": body.get("thinking_modes", [
            {"id": "fast", "label": "Fast", "thinking_enabled": False, "auto_thinking": False, "thinking_mode": "Fast"},
        ]),
        "_custom": True,
    }
    add_custom_model(model_def)
    return {"status": "ok", "model": model_def}


@router.delete("/api/settings/models/{model_id:path}")
async def delete_model(model_id: str) -> dict[str, Any]:
    """Remove any model (custom or static). Static models get hidden via an exclusion list."""
    from engine.config import remove_custom_model, _load_hidden_models, _save_hidden_models, MODELS
    # Try custom first
    if remove_custom_model(model_id):
        return {"status": "ok", "type": "custom"}
    # Check if it's a static model
    static_ids = {m["id"] for m in MODELS}
    if model_id in static_ids:
        hidden = _load_hidden_models()
        if model_id not in hidden:
            hidden.append(model_id)
            _save_hidden_models(hidden)
        return {"status": "ok", "type": "static_hidden"}
    raise HTTPException(status_code=404, detail="Model not found")

@router.get("/api/settings/accounts")
async def list_accounts() -> dict[str, Any]:
    def _scan() -> list[dict[str, Any]]:
        accounts: list[dict[str, Any]] = []
        for entry in _SYSTEM_DIR.iterdir():
            m = re.match(r"browser-data-acc(\d+)$", entry.name)
            if entry.is_dir() and m:
                accounts.append({
                    "name": entry.name,
                    "num": int(m.group(1)),
                    "email": _read_profile_email(entry),
                    "size_mb": _dir_size_mb(entry),
                })
        accounts.sort(key=lambda a: a["num"])
        return accounts
    accounts = await asyncio.to_thread(_scan)
    active: str | None = None
    if _ACTIVE_PROFILE_LINK.is_symlink():
        target = _ACTIVE_PROFILE_LINK.resolve().name
        active = target
    elif _ACTIVE_PROFILE_LINK.is_dir():
        active = "browser-data (not yet migrated)"
    return {"accounts": accounts, "active": active}

@router.post("/api/settings/accounts/switch")
async def switch_account(payload: dict[str, str]) -> dict[str, Any]:
    target_name = payload.get("profile", "")
    if not target_name.startswith("browser-data-acc"):
        raise HTTPException(status_code=400, detail="Profile must match 'browser-data-acc*'")
    target_path = _SYSTEM_DIR / target_name
    if not target_path.is_dir():
        raise HTTPException(status_code=404, detail=f"Profile directory '{target_name}' not found")
    # Resolve old profile before switching (for post-switch strip)
    old_profile: Path | None = None
    if _ACTIVE_PROFILE_LINK.is_symlink():
        resolved = _ACTIVE_PROFILE_LINK.resolve()
        if resolved != target_path and resolved.is_dir():
            old_profile = resolved
    def _do_switch() -> None:
        if _ACTIVE_PROFILE_LINK.is_dir() and not _ACTIVE_PROFILE_LINK.is_symlink():
            migration_name = "browser-data-acc1"
            migration_path = _SYSTEM_DIR / migration_name
            if migration_path.exists():
                shutil.rmtree(_ACTIVE_PROFILE_LINK)
            else:
                _ACTIVE_PROFILE_LINK.rename(migration_path)
        elif _ACTIVE_PROFILE_LINK.is_symlink():
            _ACTIVE_PROFILE_LINK.unlink()
        _ACTIVE_PROFILE_LINK.symlink_to(target_path)
    await service.close()
    try:
        await asyncio.to_thread(_do_switch)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Switch failed: {exc}")
    await service.warmup()
    try:
        ds_token = await service.refresh_deepseek_token()
        get_deepseek_client().set_token(ds_token)
    except Exception:
        pass
    # Sync context for the new account's browser profile
    try:
        await service.sync_context()
    except Exception as exc:
        logger.warning("sync_context after switch failed: %s", exc)
    # Strip the old profile to reclaim disk space (fire-and-forget)
    stripped_info: str | None = None
    if old_profile and old_profile.is_dir():
        try:
            name, before, after = await asyncio.to_thread(_strip_one_profile, old_profile)
            stripped_info = f"{name}: {before:.1f}MB → {after:.1f}MB"
            logger.info("Auto-stripped old profile %s", stripped_info)
        except Exception as exc:
            logger.warning("Failed to strip old profile: %s", exc)
    result: dict[str, Any] = {"status": "ok", "active": target_name, "email": _read_profile_email(target_path)}
    if stripped_info:
        result["stripped"] = stripped_info
    return result


@router.post("/api/settings/accounts/create")
async def create_account() -> dict[str, Any]:
    """Find next available acc integer and launch browser_opener headed."""
    def _next_acc() -> int:
        existing: set[int] = set()
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

    def _launch() -> None:
        subprocess.Popen(
            ["uv", "run", "python", "engine/account_login.py", profile_name],
            cwd=str(BASE_DIR),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    try:
        await asyncio.to_thread(_launch)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to launch browser_opener: {exc}")
    return {"status": "ok", "profile": profile_name}


@router.delete("/api/settings/accounts/delete")
async def delete_account(payload: dict[str, str]) -> dict[str, Any]:
    """Delete a browser-data-accN profile directory (and its .bak if present)."""
    target_name = payload.get("profile", "")
    if not re.match(r"^browser-data-acc\d+$", target_name):
        raise HTTPException(status_code=400, detail="Invalid profile name")
    target_path = _SYSTEM_DIR / target_name
    if not target_path.is_dir():
        raise HTTPException(status_code=404, detail=f"'{target_name}' not found")
    # Block deleting the active profile
    if _ACTIVE_PROFILE_LINK.is_symlink() and _ACTIVE_PROFILE_LINK.resolve() == target_path.resolve():
        raise HTTPException(status_code=400, detail="Cannot delete the active profile. Switch first.")

    def _remove() -> None:
        shutil.rmtree(target_path)
        bak = _SYSTEM_DIR / f"{target_name}.bak"
        if bak.is_dir():
            shutil.rmtree(bak)

    await asyncio.to_thread(_remove)
    return {"status": "ok", "deleted": target_name}


@router.post("/api/settings/accounts/open")
async def open_account_browser(payload: dict[str, str]) -> dict[str, Any]:
    """Launch a headful browser with the specified profile (like browser_opener.py)."""
    target_name = payload.get("profile", "")
    if not re.match(r"^browser-data-acc\d+$", target_name):
        raise HTTPException(status_code=400, detail="Invalid profile name")
    target_path = _SYSTEM_DIR / target_name
    if not target_path.is_dir():
        raise HTTPException(status_code=404, detail=f"'{target_name}' not found")

    url = payload.get("url", "https://chat.qwen.ai")

    async def _run_browser() -> None:
        from playwright.async_api import async_playwright
        try:
            async with async_playwright() as p:
                context = await p.chromium.launch_persistent_context(
                    user_data_dir=str(target_path),
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
                await page.goto(url)
                # Wait until user closes the browser window
                await context.wait_for_event("close")
        except Exception as e:
            logger.warning(f"Browser for {target_name} exited: {e}")

    asyncio.create_task(_run_browser())
    return {"status": "opened", "profile": target_name, "url": url}


_SERVICE_NAME = "sable.service"


@router.post("/api/settings/service/stop")
async def stop_service() -> dict[str, str]:
    subprocess.Popen(["systemctl", "--user", "stop", _SERVICE_NAME])
    return {"status": "stopping"}


@router.post("/api/settings/service/restart")
async def restart_service() -> dict[str, str]:
    subprocess.Popen(["systemctl", "--user", "restart", _SERVICE_NAME])
    return {"status": "restarting"}





@router.get("/api/settings/accounts/backups")
async def get_account_backups() -> dict[str, Any]:
    """List each browser-data-accN dir with its .bak status."""
    def _collect() -> list[dict[str, Any]]:
        accounts: list[dict[str, Any]] = []
        for d in _SYSTEM_DIR.iterdir():
            m = re.match(r"browser-data-acc(\d+)$", d.name)
            if not m or not d.is_dir():
                continue
            bak = _SYSTEM_DIR / f"{d.name}.bak"
            accounts.append({
                "name": d.name,
                "num": int(m.group(1)),
                "size_mb": _dir_size_mb(d),
                "has_backup": bak.is_dir(),
                "backup_size_mb": _dir_size_mb(bak) if bak.is_dir() else 0,
                "email": _read_profile_email(d),
            })
        accounts.sort(key=lambda a: a["num"])
        return accounts
    result = await asyncio.to_thread(_collect)
    return {"accounts": result}


@router.post("/api/settings/accounts/backup")
async def backup_account(payload: dict[str, str]) -> dict[str, Any]:
    """Create a .bak snapshot of a specific account profile."""
    name = payload.get("profile", "")
    if not re.match(r"browser-data-acc\d+$", name):
        raise HTTPException(status_code=400, detail="Profile must match 'browser-data-accN'")
    data_path = _SYSTEM_DIR / name
    if not data_path.is_dir():
        raise HTTPException(status_code=404, detail=f"Profile '{name}' not found")
    bak_path = _SYSTEM_DIR / f"{name}.bak"

    def _do() -> None:
        if bak_path.is_dir():
            shutil.rmtree(bak_path)
        shutil.copytree(data_path, bak_path, symlinks=True)

    await asyncio.to_thread(_do)
    return {"status": "ok", "profile": name, "size_mb": _dir_size_mb(bak_path)}


@router.post("/api/settings/accounts/restore")
async def restore_account(payload: dict[str, str]) -> dict[str, Any]:
    """Restore an account profile from its .bak snapshot."""
    name = payload.get("profile", "")
    if not re.match(r"browser-data-acc\d+$", name):
        raise HTTPException(status_code=400, detail="Profile must match 'browser-data-accN'")
    data_path = _SYSTEM_DIR / name
    bak_path = _SYSTEM_DIR / f"{name}.bak"
    if not bak_path.is_dir():
        raise HTTPException(status_code=404, detail=f"No backup found for '{name}'")

    def _do() -> None:
        if data_path.is_dir():
            shutil.rmtree(data_path)
        shutil.copytree(bak_path, data_path, symlinks=True)

    await asyncio.to_thread(_do)
    return {"status": "ok", "profile": name, "restored_from": str(bak_path)}



@router.get("/api/settings/browser/profiles")
async def get_browser_profiles() -> dict[str, Any]:
    def _collect() -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, (data_path, bak_path) in _BROWSER_PROFILES.items():
            result[key] = {
                "label": {"api": "API (ChatService)", "scraper": "Scraper", "automation": "Automation (Browser Control)"}[key],
                "data_dir": str(data_path.relative_to(BASE_DIR)),
                "exists": data_path.is_dir(),
                "size_mb": _dir_size_mb(data_path),
                "has_backup": bak_path.is_dir(),
                "backup_size_mb": _dir_size_mb(bak_path),
            }
        return result
    result = await asyncio.to_thread(_collect)
    return {"profiles": result}

@router.post("/api/settings/browser/restore")
async def restore_browser_profile(payload: dict[str, str]) -> dict[str, Any]:
    profile = payload.get("profile", "")
    if profile not in _BROWSER_PROFILES:
        raise HTTPException(status_code=400, detail=f"Unknown profile '{profile}'. Use 'api', 'scraper', or 'automation'.")
    data_path, bak_path = _BROWSER_PROFILES[profile]
    if not bak_path.is_dir():
        raise HTTPException(status_code=404, detail=f"No backup found at {bak_path}")
    def _do_restore() -> None:
        if data_path.is_dir():
            shutil.rmtree(data_path)
        shutil.copytree(bak_path, data_path, symlinks=True)
    await asyncio.to_thread(_do_restore)
    return {
        "status": "ok",
        "profile": profile,
        "restored_from": str(bak_path),
        "restored_to": str(data_path),
    }

@router.post("/api/settings/browser/strip-profiles")
async def strip_browser_profiles() -> dict[str, Any]:
    """Strip all browser profiles to bare session data (pure Python, no subprocess)."""

    def _strip_all() -> list[tuple[str, float, float]]:
        results = []
        for entry in sorted(_SYSTEM_DIR.iterdir()):
            if entry.is_dir() and (
                entry.name.startswith("browser-data-acc")
                or entry.name in ("browser-scraper-data", "automation-browser-data")
            ):
                results.append(_strip_one_profile(entry))
        return results

    results = await asyncio.to_thread(_strip_all)
    lines = [f"  {name}: {b:.1f}MB → {a:.1f}MB" for name, b, a in results]
    total = _dir_size_mb(_SYSTEM_DIR)
    output = f"Stripped {len(results)} profiles.\n" + "\n".join(lines) + f"\nTotal system/: {total}MB"
    return {"status": "ok", "output": output, "profiles_stripped": len(results)}


@router.post("/api/settings/browser/create-backup")
async def create_browser_backup(payload: dict[str, str]) -> dict[str, Any]:
    profile = payload.get("profile", "")
    if profile not in _BROWSER_PROFILES:
        raise HTTPException(status_code=400, detail=f"Unknown profile '{profile}'. Use 'api', 'scraper', or 'automation'.")
    data_path, bak_path = _BROWSER_PROFILES[profile]
    if not data_path.is_dir():
        raise HTTPException(status_code=404, detail=f"No data found at {data_path}")
    def _do_backup() -> None:
        if bak_path.is_dir():
            shutil.rmtree(bak_path)
        shutil.copytree(data_path, bak_path, symlinks=True)
    await asyncio.to_thread(_do_backup)
    return {
        "status": "ok",
        "profile": profile,
        "backed_up": str(data_path),
        "backup_to": str(bak_path),
        "size_mb": _dir_size_mb(bak_path),
    }