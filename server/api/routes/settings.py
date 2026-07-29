from __future__ import annotations

import asyncio
import shutil
from typing import Any

from fastapi import APIRouter, HTTPException
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

@router.get("/api/settings/accounts")
async def list_accounts() -> dict[str, Any]:
    def _scan() -> list[dict[str, Any]]:
        accounts: list[dict[str, Any]] = []
        for entry in sorted(_SYSTEM_DIR.iterdir()):
            if entry.is_dir() and entry.name.startswith("browser-data-acc"):
                accounts.append({
                    "name": entry.name,
                    "email": _read_profile_email(entry),
                    "size_mb": _dir_size_mb(entry),
                })
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
    return {"status": "ok", "active": target_name, "email": _read_profile_email(target_path)}

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