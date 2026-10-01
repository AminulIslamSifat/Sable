from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, AsyncGenerator

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import Response, StreamingResponse
from engine.scraper import (
    get_settings as get_scraper_settings,
    list_engines as list_scraper_engines,
    scraper as scraper_service,
    update_settings as update_scraper_settings,
)
from connectors.deepseek.client import get_client as get_deepseek_client
from connectors.zai.client import get_client as get_zai_client

from server.config import (
    BASE_DIR, _SYSTEM_DIR, _BROWSER_PROFILES,
)
from server.utils import _dir_size_mb, _read_profile_email, logger
from ..dependencies import service

router = APIRouter()

# Strong refs to fire-and-forget tasks so they aren't GC'd mid-flight
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _spawn_bg(coro) -> None:
    """Schedule a background task with GC-safe bookkeeping."""
    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


def _active_account_names() -> set[str]:
    """Every account that is active for AT LEAST ONE provider.

    With per-provider active browsers there is no single "active" profile, so
    destructive ops (delete/restore/strip) must protect any profile a provider
    is currently pinned to.
    """
    try:
        from engine.config import BROWSER_PROVIDERS, get_provider_active_account
        return {get_provider_active_account(p) for p in BROWSER_PROVIDERS}
    except Exception:
        return set()


# A managed profile dir is any `browser-data-*` folder that is not a backup.
# Registered profiles use the `browser-data-accN` form; unregistered ones are
# dedicated dirs (e.g. `browser-data-kimi`) surfaced in the account tab.
_PROFILE_RE = re.compile(r"^browser-data-[A-Za-z0-9._-]+$")


def _validate_profile_name(name: str) -> str:
    """Validate a browser-data profile directory name.

    Rejects path traversal, backups, and anything outside `browser-data-*`.
    Returns the name unchanged on success, raises HTTPException otherwise.
    """
    if not name or not _PROFILE_RE.match(name) or name.endswith(".bak"):
        raise HTTPException(status_code=400, detail="Invalid profile name")
    # Guard against any residual traversal (regex already forbids `/` and `..`
    # as a component, but be explicit).
    if "/" in name or chr(92) in name or name in (".", ".."):
        raise HTTPException(status_code=400, detail="Invalid profile name")
    return name


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
    # Safety: never strip a profile that is ACTIVE for ANY provider. Matters
    # when invoked as a background task after a switch — the user (or another
    # provider) may still be using it.
    try:
        from engine.config import BROWSER_PROVIDERS, get_provider_active_account, _SYSTEM as _SYS
        target = Path(profile).resolve()
        for _p in BROWSER_PROVIDERS:
            _active_dir = _SYS / get_provider_active_account(_p)
            if _active_dir.exists() and target == _active_dir.resolve():
                raise RuntimeError(
                    f"refusing to strip profile {profile.name} (active for {_p})"
                )
    except OSError:
        pass
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
    if profile.is_symlink():
        profile.unlink()
    else:
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

    # Headless/headed change: the running browser keeps its launch flags, so
    # the mode only takes effect on a fresh launch. Live scraper sessions that
    # are held by connector engines are restarted for real; otherwise the user
    # flips the toggle, sees nothing happen, and thinks it is broken.
    mode_changed = bool(old_settings.get("headless", False)) != bool(settings.get("headless", False))
    if mode_changed:
        restarted = await _restart_scraper_connectors()
        if restarted:
            settings["restarted_engines"] = restarted

    if settings.get("enabled"):
        prelaunch_result = await scraper_service.prelaunch()
        settings["prelaunch"] = prelaunch_result
    return settings


async def _restart_scraper_connectors() -> list[str]:
    """Tear down every live scraper connector engine so the next turn relaunches.

    A connector caches its engine per account; the engine owns the Chromium
    process with the old launch flags. Releasing the cached engines is what
    makes a headless/headed toggle actually take effect on a browser that is
    already open. Returns the connectors that were reset.
    """
    reset: list[str] = []
    try:
        from connectors.deepseek_scraper.client import _clients as _ds_clients, _default_client as _ds_default
    except Exception:
        _ds_clients, _ds_default = {}, None

    seen: set[int] = set()
    for client in list(_ds_clients.values()) + ([_ds_default] if _ds_default else []):
        if client is None or id(client) in seen:
            continue
        seen.add(id(client))
        try:
            await client.shutdown()
            reset.append(getattr(client, "backend_name", "scraper"))
        except Exception as exc:
            logger.warning("scraper connector restart failed: %s", exc)
    return reset

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


# --- Browser fleet slots: inspect and kill ---------------------------------
# Backs the 'no browser slot available' card. When both slots are held by live
# chat browsers a new chat cannot spawn one; the user is shown the holders and
# can kill one to free capacity immediately.

@router.get("/api/settings/browser/slots")
async def list_browser_slots() -> dict[str, Any]:
    """Live view of every browser slot this Sable process holds."""
    from engine import browser_governor
    try:
        snap = await asyncio.to_thread(browser_governor.own_slot_snapshot)
        stat = await asyncio.to_thread(browser_governor.stats)
    except Exception as exc:
        logger.warning("browser slot listing failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Slot listing failed: {exc}")
    return {
        "status": "ok",
        "max_browsers": stat.get("max_browsers"),
        "slots": snap,
    }


@router.post("/api/settings/browser/slots/kill")
async def kill_browser_slot(payload: dict[str, Any]) -> dict[str, Any]:
    """Kill one browser by pid and release its slot.

    Deliberately blunt, and behind an explicit user click: the pid comes from
    the no-slot card, so the user is choosing which of THEIR chat browsers to
    sacrifice. Kills the whole process group and drops the slot so the next
    chat message can claim the capacity right away.
    """
    from engine import browser_governor
    try:
        pid = int(payload.get("pid"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="A numeric 'pid' is required")
    try:
        ok = await asyncio.to_thread(browser_governor.kill_slot, pid)
    except Exception as exc:
        logger.warning("browser slot kill failed for pid %s: %s", pid, exc)
        raise HTTPException(status_code=500, detail=f"Kill failed: {exc}")
    if not ok:
        raise HTTPException(status_code=404, detail=f"No live browser with pid {pid}")
    return {"status": "ok", "pid": pid,
            "message": f"Killed browser {pid} — slot freed"}


# --- Network access mode: localhost-only vs shared LAN bind ----------------

_NETWORK_MODE_HOSTS = {"local": "127.0.0.1", "lan": "0.0.0.0"}


def _network_settings_path() -> Path:
    from engine.config import _SYSTEM
    return _SYSTEM / "network_settings.json"


def _current_bind_host() -> str:
    env = os.getenv("SABLE_HOST")
    if env:
        return env
    try:
        host = json.loads(_network_settings_path().read_text(encoding="utf-8")).get("host")
        if host in _NETWORK_MODE_HOSTS.values():
            return host
    except Exception:
        pass
    return "0.0.0.0"


@router.get("/api/settings/network")
async def get_network_settings() -> dict[str, Any]:
    host = _current_bind_host()
    return {"host": host, "mode": "lan" if host == "0.0.0.0" else "local"}


@router.post("/api/settings/network")
async def update_network_settings(payload: dict[str, Any]) -> dict[str, Any]:
    """Persist bind-host preference. Takes effect after server restart."""
    mode = str(payload.get("mode") or "").strip().lower()
    if mode not in _NETWORK_MODE_HOSTS:
        raise HTTPException(status_code=400, detail="mode must be 'local' or 'lan'")
    host = _NETWORK_MODE_HOSTS[mode]
    path = _network_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"host": host, "mode": mode}, indent=2), encoding="utf-8")
    return {"status": "ok", "host": host, "mode": mode, "restart_required": True}

@router.post("/api/settings/deepseek/refresh-token")
async def refresh_deepseek_token() -> dict[str, Any]:
    try:
        token = await get_deepseek_client().refresh_token()
        preview = token[:20] + "..." if len(token) > 20 else "***"
        return {
            "status": "ok",
            "message": f"DeepSeek token refreshed ({preview})",
            "token_preview": preview,
            "active": True,
        }
    except Exception as exc:
        logger.error("DeepSeek token refresh failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Refresh failed: {exc}")


@router.post("/api/settings/zai/refresh-token")
async def refresh_zai_token() -> dict[str, Any]:
    try:
        token = await get_zai_client().refresh_token()
        preview = token[:20] + "..." if len(token) > 20 else "***"
        return {
            "status": "ok",
            "message": f"z.ai token refreshed ({preview})",
            "token_preview": preview,
            "active": True,
        }
    except Exception as exc:
        logger.error("z.ai token refresh failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Refresh failed: {exc}")


@router.post("/api/settings/gemini_web/refresh-token")
async def refresh_gemini_web_token() -> dict[str, Any]:
    """Force a live Gemini cookie read from the provider's browser profile.

    Pinned account wins (get_provider_browser_dir); otherwise the global active
    account is used. On success the fresh jar is snapshotted to disk and the
    client's in-memory cache is invalidated so the next send picks it up.
    """
    from connectors.gemini_web import auth as _gauth
    try:
        browser = service._browser_for_provider("gemini_web")
        account = Path(browser.user_data_dir).name
        async with service._lock:
            await browser.start()
            ctx = browser.context
            if ctx is None:
                raise RuntimeError("browser context unavailable")
            raw = await ctx.cookies("https://gemini.google.com")
            jar = {c["name"]: c["value"] for c in raw}
        if not _gauth.has_google_auth(jar):
            raise RuntimeError(f"no Google auth cookies in {account}")
        _gauth.save_gemini_auth(browser.user_data_dir, jar)
        # Drop the client's cached jar so the next request re-reads
        try:
            from connectors.gemini_web.client import get_client as _gw
            _gw().invalidate_jar()
        except Exception:
            pass
        preview = ""
        for k in ("SAPISID", "__Secure-1PSID", "__Secure-3PSID"):
            if jar.get(k):
                v = jar[k]
                preview = f"{k}={v[:6]}...{v[-4:]}" if len(v) > 12 else f"{k}=***"
                break
        return {
            "status": "ok",
            "message": f"Gemini (web) token refreshed for {account} ({preview})",
            "account": account,
            "token_preview": preview,
            "active": True,
        }
    except Exception as exc:
        logger.error("Gemini (web) token refresh failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Refresh failed: {exc}")


@router.post("/api/settings/mimo/refresh-token")
async def refresh_mimo_token() -> dict[str, Any]:
    """Force a live MiMo cookie read AND validate it against MiMo.

    MiMo auth is 3 cookies (xiaomichatbot_serviceToken, userId,
    xiaomichatbot_ph). Pinned account wins; otherwise the global active
    account is used. Unlike the old version, this actually probes MiMo with
    the token before reporting success — a stale cookie now returns a real
    error instead of a fake "refreshed" toast.
    """
    try:
        jar = await service.get_mimo_cookies()
        browser = service._browser_for_provider("mimo")
        account = Path(browser.user_data_dir).name
        try:
            from connectors.mimo.client import get_client as _mimo
            client = _mimo()
            client.invalidate_jar()
        except Exception:
            client = None

        tok = (jar.get("xiaomichatbot_serviceToken", "") or "").strip('"')
        preview = f"{tok[:6]}...{tok[-4:]}" if len(tok) > 12 else "***"

        # Validate against MiMo — this is what the old route skipped.
        if client is not None:
            try:
                await client.validate_auth()
            except Exception as exc:
                logger.warning("MiMo token validation failed for %s: %s", account, exc)
                raise HTTPException(
                    status_code=502,
                    detail=f"MiMo rejected token for {account} [{preview}]: {exc}",
                )

        return {
            "status": "ok",
            "message": f"MiMo token refreshed & verified for {account} ({preview})",
            "account": account,
            "token_preview": preview,
            "validated": True,
            "active": True,
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("MiMo token refresh failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Refresh failed: {exc}")


@router.post("/api/settings/mistral_web/refresh-token")
async def refresh_mistral_web_token() -> dict[str, Any]:
    """Force a live Mistral (Web) cookie read AND validate it against Mistral.

    Auth is an Ory Kratos session (ory_session_* cookies). Pinned account wins;
    otherwise the global active account is used. Probes Mistral with the cookie
    before reporting success, so a stale session returns a real error.
    """
    try:
        jar = await service.get_mistral_web_cookies()
        browser = service._browser_for_provider("mistral_web")
        account = Path(browser.user_data_dir).name
        try:
            from connectors.mistral_web.client import get_client as _mw
            client = _mw()
            client.invalidate_jar()
        except Exception:
            client = None

        sess = next((k for k in jar if k.startswith("ory_session_")), "")
        preview = (sess[:14] + "...") if sess else "***"

        if client is not None:
            try:
                await client.validate_auth()
            except Exception as exc:
                logger.warning("Mistral (Web) token validation failed for %s: %s", account, exc)
                raise HTTPException(
                    status_code=502,
                    detail=f"Mistral rejected session for {account} [{preview}]: {exc}",
                )

        return {
            "status": "ok",
            "message": f"Mistral (Web) session refreshed & verified for {account}",
            "account": account,
            "token_preview": preview,
            "validated": True,
            "active": True,
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Mistral (Web) token refresh failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Refresh failed: {exc}")


@router.post("/api/settings/kimi/refresh-token")
async def refresh_kimi_token() -> dict[str, Any]:
    """Ensure a live Kimi ghost session that holds a login token.

    Kimi auth is a localStorage bearer the SPA refreshes silently, so this
    (re)acquires the ghost tab for the active/pinned account and verifies the
    token is present — respawning the session if it had died.
    """
    try:
        ok = await service.refresh_kimi_token()
        if not ok:
            raise HTTPException(
                status_code=502,
                detail="Kimi ghost session has no login token (not logged in?)",
            )
        return {"status": "ok", "message": "Kimi token refreshed", "active": True}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Kimi token refresh failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Refresh failed: {exc}")


@router.post("/api/settings/refresh-all-tokens")
async def refresh_all_tokens() -> dict[str, Any]:
    """Refresh every browser-backed provider against ITS OWN active account.

    Providers are grouped by the account they resolve to, so any two (or more)
    sharing the same active profile refresh together in one browser pass. Each
    provider is isolated — one failure never blocks the rest — and the
    per-provider result map is returned alongside a summary string.
    """
    try:
        results = await service.refresh_all_provider_tokens()
    except Exception as exc:
        logger.error("Refresh-all tokens failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Refresh all failed: {exc}")

    ok = [k for k, v in results.items() if v]
    failed = [k for k, v in results.items() if not v]
    msg = f"Refreshed {len(ok)}/{len(results)}: {', '.join(ok) or 'none'}"
    if failed:
        msg += f" · failed: {', '.join(failed)}"
    return {"status": "ok", "message": msg, "results": results}


@router.post("/api/settings/browser/refresh-waf")
async def refresh_waf_token() -> dict[str, Any]:
    """Re-launch browser briefly to extract fresh Qwen WAF/cookie headers,
    and opportunistically refresh DeepSeek + z.ai tokens for the active account."""
    try:
        svc = service
        headers = await svc._refresh_headers()
        has_cookie = bool(headers.get("Cookie"))
        has_bx = bool(headers.get("bx-ua"))

        # Grab EVERY provider's tokens in one pass. Each provider's browser is
        # launched once and left running (no close), so refreshing all of them
        # is one warm Chromium per account instead of an open→close→open cycle.
        ds_ok = zai_ok = gem_ok = mimo_ok = mistral_web_ok = False
        previews: dict[str, str] = {}

        def _pv(v: str, n: int = 20) -> str:
            v = (v or "").strip('"')
            return (v[:n] + "...") if len(v) > n else (v or "***")

        try:
            ds_token = await get_deepseek_client().refresh_token()
            ds_ok = bool(ds_token)
            previews["deepseek"] = _pv(ds_token)
        except Exception as exc:
            logger.warning("DeepSeek token refresh during WAF refresh failed: %s", exc)
            previews["deepseek"] = "failed"
        try:
            zai_token = await get_zai_client().refresh_token()
            zai_ok = bool(zai_token)
            previews["zai"] = _pv(zai_token)
        except Exception as exc:
            logger.warning("z.ai token refresh during WAF refresh failed: %s", exc)
            previews["zai"] = "failed"
        try:
            from connectors.gemini_web import auth as _gauth
            gem_browser = service._browser_for_provider("gemini_web")
            await gem_browser.start()
            ctx = gem_browser.context
            if ctx is not None:
                raw = await ctx.cookies("https://gemini.google.com")
                gjar = {c["name"]: c["value"] for c in raw}
                if _gauth.has_google_auth(gjar):
                    _gauth.save_gemini_auth(gem_browser.user_data_dir, gjar)
                    from connectors.gemini_web.client import get_client as _gw
                    _gw().invalidate_jar()
                    gem_ok = True
                    for k in ("SAPISID", "__Secure-1PSID", "__Secure-3PSID"):
                        if gjar.get(k):
                            previews["gemini_web"] = _pv(gjar[k])
                            break
        except Exception as exc:
            logger.warning("Gemini token refresh during WAF refresh failed: %s", exc)
            previews["gemini_web"] = "failed"
        try:
            mimo_jar = await service.get_mimo_cookies()
            if mimo_jar:
                from connectors.mimo.client import get_client as _mimo
                _mimo().invalidate_jar()
                mimo_ok = True
                previews["mimo"] = _pv(mimo_jar.get("xiaomichatbot_serviceToken", ""))
        except Exception as exc:
            logger.warning("MiMo token refresh during WAF refresh failed: %s", exc)
            previews["mimo"] = "failed"
        try:
            mw_jar = await service.get_mistral_web_cookies()
            if mw_jar:
                from connectors.mistral_web.client import get_client as _mw
                _mw().invalidate_jar()
                mistral_web_ok = True
                _sess = next((k for k in mw_jar if k.startswith("ory_session_")), "")
                previews["mistral_web"] = _pv(_sess, 14)
        except Exception as exc:
            logger.warning("Mistral (Web) token refresh during WAF refresh failed: %s", exc)
            previews["mistral_web"] = "failed"

        # Compact token summary for the toast popup.
        parts = []
        if has_cookie:
            parts.append("waf")
        if ds_ok:
            parts.append(f"ds={previews.get('deepseek','***')}")
        if zai_ok:
            parts.append(f"zai={previews.get('zai','***')}")
        if gem_ok:
            parts.append(f"gem={previews.get('gemini_web','***')}")
        if mimo_ok:
            parts.append(f"mimo={previews.get('mimo','***')}")
        if mistral_web_ok:
            parts.append(f"mweb={previews.get('mistral_web','***')}")
        summary = " ".join(parts) if parts else "none"

        return {
            "status": "ok",
            "message": f"WAF tokens refreshed: {summary}",
            "has_cookie": has_cookie,
            "has_bx_ua": has_bx,
            "deepseek": ds_ok,
            "zai": zai_ok,
            "gemini_web": gem_ok,
            "mimo": mimo_ok,
            "mistral_web": mistral_web_ok,
            "token_previews": previews,
        }
    except Exception as exc:
        logger.error("WAF token refresh failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Refresh failed: {exc}")


# ── Request Jitter (anti-burst throttle) ──────────────────────────────

@router.get("/api/settings/jitter")
async def get_jitter_settings() -> dict[str, Any]:
    """Return the per-provider request-jitter config."""
    from connectors.common.jitter import get_jitter_config, PROVIDERS
    cfg = get_jitter_config()
    cfg["available_providers"] = list(PROVIDERS)
    return cfg


@router.post("/api/settings/jitter")
async def set_jitter_settings(request: Request) -> dict[str, Any]:
    """Update the request-jitter config (enabled / min / max / per-provider)."""
    from connectors.common.jitter import (
        get_jitter_config, PROVIDERS, DEFAULT_MIN, DEFAULT_MAX,
    )
    body = await request.json()

    cfg = get_jitter_config()
    if "enabled" in body:
        cfg["enabled"] = bool(body["enabled"])
    if "min" in body:
        try:
            cfg["min"] = max(0.0, float(body["min"]))
        except (TypeError, ValueError):
            pass
    if "max" in body:
        try:
            cfg["max"] = max(0.0, float(body["max"]))
        except (TypeError, ValueError):
            pass
    if cfg["max"] < cfg["min"]:
        cfg["min"], cfg["max"] = cfg["max"], cfg["min"]

    providers = cfg.get("providers", {})
    if isinstance(body.get("providers"), dict):
        for prov in PROVIDERS:
            if prov in body["providers"]:
                providers[prov] = bool(body["providers"][prov])
    cfg["providers"] = providers

    _settings = _read_system_settings()
    _settings["request_jitter"] = {
        "enabled": cfg["enabled"],
        "min": cfg["min"],
        "max": cfg["max"],
        "providers": providers,
    }
    _write_system_settings(_settings)

    result = get_jitter_config()
    result["available_providers"] = list(PROVIDERS)
    return {"status": "ok", **result}



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
# OpenAI API key management
# ---------------------------------------------------------------------------

@router.post("/api/settings/openai/api-key")
async def add_openai_api_key(request: Request) -> dict[str, Any]:
    """Add an OpenAI API key to the pool."""
    from connectors.openai.client import get_client as get_openai_client
    body = await request.json()
    key = body.get("api_key", "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="Missing 'api_key' field")
    client = get_openai_client()
    if key in client._keys:
        raise HTTPException(status_code=409, detail="Key already exists")
    client.add_key(key)
    return {"status": "ok", "keys": client.list_keys(), "available": client.is_available}


@router.get("/api/settings/openai/keys")
async def list_openai_keys() -> dict[str, Any]:
    """List all configured OpenAI API keys (masked)."""
    from connectors.openai.client import get_client as get_openai_client
    client = get_openai_client()
    return {"keys": client.list_keys(), "available": client.is_available}


@router.delete("/api/settings/openai/api-key/{index}")
async def remove_openai_api_key(index: int) -> dict[str, Any]:
    """Remove an OpenAI API key by index."""
    from connectors.openai.client import get_client as get_openai_client
    client = get_openai_client()
    if not client.remove_key(index):
        raise HTTPException(status_code=404, detail="Key not found at that index")
    return {"status": "ok", "keys": client.list_keys(), "available": client.is_available}


# ---------------------------------------------------------------------------
# Puter API key management (free image generation)
# ---------------------------------------------------------------------------

@router.post("/api/settings/puter/api-key")
async def add_puter_api_key(request: Request) -> dict[str, Any]:
    """Add a Puter API token to the pool."""
    from connectors.puter.client import get_client as get_puter_client
    body = await request.json()
    key = body.get("api_key", "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="Missing 'api_key' field")
    client = get_puter_client()
    if key in client._keys:
        raise HTTPException(status_code=409, detail="Key already exists")
    client.add_key(key)
    return {"status": "ok", "keys": client.list_keys(), "available": client.is_available}


@router.get("/api/settings/puter/keys")
async def list_puter_keys() -> dict[str, Any]:
    """List all configured Puter API tokens (masked)."""
    from connectors.puter.client import get_client as get_puter_client
    client = get_puter_client()
    return {"keys": client.list_keys(), "available": client.is_available}


@router.delete("/api/settings/puter/api-key/{index}")
async def remove_puter_api_key(index: int) -> dict[str, Any]:
    """Remove a Puter API token by index."""
    from connectors.puter.client import get_client as get_puter_client
    client = get_puter_client()
    if not client.remove_key(index):
        raise HTTPException(status_code=404, detail="Key not found at that index")
    return {"status": "ok", "keys": client.list_keys(), "available": client.is_available}


@router.get("/api/settings/puter/usage")
async def get_puter_usage() -> dict[str, Any]:
    """Fetch current Puter monthly allowance + usage for the active key."""
    from connectors.puter.client import get_client as get_puter_client
    return get_puter_client().get_usage()


@router.get("/api/settings/puter/models")
async def list_puter_image_models() -> dict[str, Any]:
    """Return the catalog of Puter image-generation models."""
    from connectors.puter.client import PUTER_IMAGE_MODELS
    return {"models": [{"id": k, "label": v} for k, v in PUTER_IMAGE_MODELS.items()]}


# ---------------------------------------------------------------------------
# Cloudflare Workers AI credentials & info
# ---------------------------------------------------------------------------

@router.post("/api/settings/cloudflare/credentials")
async def save_cloudflare_creds(request: Request) -> dict[str, Any]:
    """Save Cloudflare AI API token. Account ID is auto-fetched from the API."""
    body = await request.json()
    api_token = (body.get("api_token") or "").strip()
    if not api_token:
        raise HTTPException(400, "API token is required")
    from connectors.cloudflare.client import save_credentials, CloudflareAIClient
    # Validate token by fetching account ID
    client = CloudflareAIClient(api_token=api_token)
    if not client._ensure_account_id():
        raise HTTPException(400, "Invalid token or could not fetch account ID")
    save_credentials(api_token, client._account_id)
    return {"ok": True, "message": "Cloudflare credentials saved", "account_id": client._account_id}


@router.get("/api/settings/cloudflare/status")
async def cloudflare_status() -> dict[str, Any]:
    """Check if Cloudflare AI is configured and estimate daily budget."""
    from connectors.cloudflare.client import get_client, CLOUDFLARE_IMAGE_MODELS
    client = get_client()
    if not client.is_available:
        return {"available": False, "models": []}
    budget = client.estimate_daily_budget()
    models = [
        {"id": k, "label": v["label"], "description": v["description"]}
        for k, v in CLOUDFLARE_IMAGE_MODELS.items()
    ]
    return {
        "available": True,
        "budget": budget,
        "models": models,
    }


@router.delete("/api/settings/cloudflare/credentials")
async def delete_cloudflare_creds() -> dict[str, Any]:
    """Remove saved Cloudflare credentials."""
    creds_file = Path(_SYSTEM_DIR) / ".cloudflare_ai_creds.json"
    if creds_file.exists():
        creds_file.unlink()
    # Reset singleton
    import connectors.cloudflare.client as cf_mod
    cf_mod._client = None
    return {"ok": True, "message": "Cloudflare credentials removed"}


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

    elif provider == "openai":
        from connectors.openai.client import get_client as get_openai_client
        client = get_openai_client()
        if not client.is_available:
            return {"models": [], "available": False}
        key = client._current_key
        try:
            async with _httpx.AsyncClient(timeout=15.0) as http:
                r = await http.get(
                    "https://api.openai.com/v1/models",
                    headers={"Authorization": f"Bearer {key}"},
                )
                r.raise_for_status()
                data = r.json()
                models = []
                for m in data.get("data", []):
                    mid = m.get("id", "")
                    # Focus on chat-capable models; skip embeddings/tts/whisper/etc.
                    if not mid or any(s in mid for s in ("embedding", "tts", "whisper", "dall-e", "babbage", "davinci")):
                        continue
                    label = mid.replace("-", " ").replace("_", " ").title()
                    models.append({"id": mid, "label": label})
                # Sort so gpt-4o / o-series / gpt-4.1 appear first
                models.sort(key=lambda m: m["id"])
                return {"models": models, "available": True}
        except Exception as exc:
            logger.warning("OpenAI model fetch failed: %s", exc)
            return {"models": [], "available": True, "error": str(exc)}

    raise HTTPException(status_code=400, detail=f"Unknown provider: {provider}")


@router.post("/api/settings/providers/custom/models")
async def _fetch_cloudflare_models(base_url: str, api_key: str) -> dict[str, Any]:
    """Fetch text-generation models from Cloudflare Workers AI via /ai/models/search."""
    import re as _re
    import httpx as _httpx
    # Extract account ID from base URL
    match = _re.search(r"/accounts/([a-f0-9]+)", base_url)
    if not match:
        return {"models": [], "available": False, "error": "Could not extract Cloudflare account ID from URL"}
    account_id = match.group(1)
    search_url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/models/search?per_page=300"
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        async with _httpx.AsyncClient(timeout=15.0) as http:
            r = await http.get(search_url, headers=headers)
            r.raise_for_status()
            data = r.json()
            raw = data.get("result", []) or []
            models = []
            for m in raw:
                task_name = (m.get("task") or {}).get("name", "")
                if task_name != "Text Generation":
                    continue
                mid = m.get("name", "")
                if not mid:
                    continue
                desc = m.get("description", "")[:60]
                props = {p.get("property_id"): p.get("value") for p in m.get("properties", [])}
                badges = []
                if "reasoning" in props:
                    badges.append("🧠")
                if "function_calling" in props:
                    badges.append("🔧")
                label = mid.split("/")[-1].replace("-", " ").title()
                if badges:
                    label += " " + "".join(badges)
                models.append({"id": mid, "label": label})
            models.sort(key=lambda m: m["id"])
            return {"models": models, "available": True}
    except _httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code in (401, 403):
            return {"models": [], "available": False, "error": "Auth rejected — check the API token"}
        return {"models": [], "available": False, "error": f"Cloudflare returned {code}"}
    except Exception as exc:
        logger.warning("Cloudflare model fetch failed: %s", exc)
        return {"models": [], "available": False, "error": f"Could not reach Cloudflare: {exc}"}


async def fetch_custom_endpoint_models(request: Request) -> dict[str, Any]:
    """Fetch the model list from an arbitrary OpenAI-compatible endpoint.

    Body: {"base_url": "https://.../v1", "api_key": "optional"}
    Hits {base_url}/models and normalizes the response.
    For Cloudflare endpoints, uses /ai/models/search instead.
    """
    import httpx as _httpx
    body = await request.json()
    base_url = (body.get("base_url") or "").strip().rstrip("/")
    api_key = (body.get("api_key") or "").strip()
    if not base_url:
        raise HTTPException(status_code=400, detail="Missing base_url")

    # Cloudflare Workers AI doesn't support /v1/models — use their search API
    if "api.cloudflare.com" in base_url and "/accounts/" in base_url:
        return await _fetch_cloudflare_models(base_url, api_key)

    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        async with _httpx.AsyncClient(timeout=15.0) as http:
            r = await http.get(f"{base_url}/models", headers=headers)
            r.raise_for_status()
            data = r.json()
            # OpenAI-compatible: {"data": [{"id": ...}, ...]} — tolerate a bare list too
            raw = data.get("data", data if isinstance(data, list) else [])
            models = []
            for m in raw:
                mid = m.get("id", "") if isinstance(m, dict) else str(m)
                if not mid:
                    continue
                # Skip obvious non-chat models
                if any(s in mid.lower() for s in ("embedding", "tts", "whisper", "dall-e")):
                    continue
                label = mid.replace("-", " ").replace("_", " ").replace("/", " / ").title()
                models.append({"id": mid, "label": label})
            models.sort(key=lambda m: m["id"])
            return {"models": models, "available": True}
    except _httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code in (401, 403):
            return {"models": [], "available": False, "error": "Auth rejected — check the API key"}
        return {"models": [], "available": False, "error": f"Endpoint returned {code}"}
    except Exception as exc:
        logger.warning("Custom endpoint model fetch failed (%s): %s", base_url, exc)
        return {"models": [], "available": False, "error": f"Could not reach endpoint: {exc}"}


# ---------------------------------------------------------------------------
# Custom endpoint providers (saved OpenAI-compatible endpoints)
# ---------------------------------------------------------------------------

_ENDPOINTS_PATH = _SYSTEM_DIR / ".custom_endpoints.json"


def _load_endpoints() -> list[dict[str, Any]]:
    if _ENDPOINTS_PATH.exists():
        try:
            data = json.loads(_ENDPOINTS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [e for e in data if isinstance(e, dict) and e.get("id")]
        except (json.JSONDecodeError, OSError):
            pass
    return []


def _save_endpoints(endpoints: list[dict[str, Any]]) -> None:
    _ENDPOINTS_PATH.write_text(json.dumps(endpoints, indent=2) + "\n", encoding="utf-8")


def _mask_endpoint_key(key: str) -> str:
    if not key:
        return ""
    return key[:8] + "..." + key[-4:] if len(key) > 12 else "***"


async def _fetch_openai_models(base_url: str, api_key: str = "") -> dict[str, Any]:
    """Fetch + normalize models from any OpenAI-compatible /models endpoint.

    For Cloudflare endpoints, delegates to _fetch_cloudflare_models.
    """
    base_url = base_url.strip().rstrip("/")
    api_key = api_key.strip()

    # Cloudflare Workers AI doesn't support /v1/models — use their search API
    if "api.cloudflare.com" in base_url and "/accounts/" in base_url:
        return await _fetch_cloudflare_models(base_url, api_key)

    import httpx as _httpx
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        async with _httpx.AsyncClient(timeout=15.0) as http:
            r = await http.get(f"{base_url}/models", headers=headers)
            r.raise_for_status()
            data = r.json()
            raw = data.get("data", data if isinstance(data, list) else [])
            models = []
            for m in raw:
                mid = m.get("id", "") if isinstance(m, dict) else str(m)
                if not mid:
                    continue
                if any(s in mid.lower() for s in ("embedding", "tts", "whisper", "dall-e")):
                    continue
                label = mid.replace("-", " ").replace("_", " ").replace("/", " / ").title()
                models.append({"id": mid, "label": label})
            models.sort(key=lambda m: m["id"])
            return {"models": models, "available": True}
    except _httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code in (401, 403):
            return {"models": [], "available": False, "error": "Auth rejected - check the API key"}
        return {"models": [], "available": False, "error": f"Endpoint returned {code}"}
    except Exception as exc:
        logger.warning("Endpoint model fetch failed (%s): %s", base_url, exc)
        return {"models": [], "available": False, "error": f"Could not reach endpoint: {exc}"}


@router.get("/api/settings/endpoints")
async def list_custom_endpoints() -> dict[str, Any]:
    """List saved custom endpoints (api_key masked)."""
    eps = _load_endpoints()
    return {"endpoints": [
        {
            "id": e.get("id", ""),
            "name": e.get("name", ""),
            "base_url": e.get("base_url", ""),
            "api_key_masked": _mask_endpoint_key(e.get("api_key", "")),
            "has_key": bool(e.get("api_key")),
        }
        for e in eps
    ]}


@router.post("/api/settings/endpoints")
async def add_custom_endpoint(request: Request) -> dict[str, Any]:
    """Save a custom OpenAI-compatible endpoint as a reusable provider."""
    import uuid as _uuid
    body = await request.json()
    name = (body.get("name") or "").strip()
    base_url = (body.get("base_url") or "").strip().rstrip("/")
    api_key = (body.get("api_key") or "").strip()
    if not base_url:
        raise HTTPException(status_code=400, detail="Missing base_url")
    if not name:
        from urllib.parse import urlparse
        name = urlparse(base_url).netloc or "Custom Endpoint"
    eps = _load_endpoints()
    ep = {"id": "ep-" + _uuid.uuid4().hex[:8], "name": name, "base_url": base_url, "api_key": api_key}
    eps.append(ep)
    _save_endpoints(eps)
    return {"status": "ok", "endpoint": {
        "id": ep["id"], "name": ep["name"], "base_url": ep["base_url"],
        "api_key_masked": _mask_endpoint_key(api_key), "has_key": bool(api_key),
    }}


@router.delete("/api/settings/endpoints/{endpoint_id}")
async def delete_custom_endpoint(endpoint_id: str) -> dict[str, Any]:
    """Remove a saved custom endpoint."""
    eps = _load_endpoints()
    new_eps = [e for e in eps if e.get("id") != endpoint_id]
    if len(new_eps) == len(eps):
        raise HTTPException(status_code=404, detail="Endpoint not found")
    _save_endpoints(new_eps)
    return {"status": "ok"}


@router.get("/api/settings/endpoints/{endpoint_id}/models")
async def list_custom_endpoint_models(endpoint_id: str) -> dict[str, Any]:
    """Fetch models from a saved endpoint using its stored URL + key."""
    eps = _load_endpoints()
    ep = next((e for e in eps if e.get("id") == endpoint_id), None)
    if not ep:
        raise HTTPException(status_code=404, detail="Endpoint not found")
    return await _fetch_openai_models(ep.get("base_url", ""), ep.get("api_key", ""))


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

    # Thinking support: explicit flag from UI, or derive from provided thinking_modes
    supports_thinking = body.get("supports_thinking", False)
    if "thinking_modes" in body:
        thinking_modes = body["thinking_modes"]
    elif supports_thinking:
        thinking_modes = [
            {"id": "fast", "label": "Fast", "thinking_enabled": False, "auto_thinking": False, "thinking_mode": "Fast"},
            {"id": "high", "label": "Thinking", "thinking_enabled": True, "auto_thinking": False, "thinking_mode": "High"},
        ]
    else:
        thinking_modes = [
            {"id": "fast", "label": "Fast", "thinking_enabled": False, "auto_thinking": False, "thinking_mode": "Fast"},
        ]

    model_def = {
        "id": mid,
        "label": label,
        "api_backend": backend,
        "api_model_type": body.get("api_model_type", mid),
        "max_session_chars": int(body.get("max_session_chars", 500_000)),
        "capabilities": body.get("capabilities", {"image": False, "video": False, "document": False, "audio": False}),
        "thinking_modes": thinking_modes,
        "_custom": True,
    }
    # Custom endpoint: either a saved endpoint (endpoint_id) or inline (local_endpoint)
    endpoint_id = (body.get("endpoint_id") or "").strip()
    if endpoint_id:
        eps = _load_endpoints()
        ep = next((e for e in eps if e.get("id") == endpoint_id), None)
        if not ep:
            raise HTTPException(status_code=400, detail="Saved endpoint not found")
        model_def["api_backend"] = "local"
        model_def["local_endpoint"] = ep.get("base_url", "")
        if ep.get("api_key"):
            model_def["local_api_key"] = ep["api_key"]
    else:
        local_endpoint = (body.get("local_endpoint") or "").strip()
        if local_endpoint:
            model_def["local_endpoint"] = local_endpoint
            local_api_key = (body.get("local_api_key") or "").strip()
            if local_api_key:
                model_def["local_api_key"] = local_api_key
    add_custom_model(model_def)
    return {"status": "ok", "model": model_def}


@router.patch("/api/settings/models/{model_id:path}")
async def update_model(model_id: str, request: Request) -> dict[str, Any]:
    """Update mutable fields (capabilities, thinking, label, max_session_chars) on any model.

    For custom models, updates in-place. For static models, creates a custom override
    so the changes persist without modifying the hardcoded MODELS list.
    """
    from engine.config import add_custom_model, get_all_models

    body = await request.json()
    all_models = get_all_models()
    existing = next((m for m in all_models if m["id"] == model_id), None)
    if not existing:
        raise HTTPException(status_code=404, detail="Model not found")

    # Start from a copy of the existing model definition
    updated = dict(existing)

    # Merge capabilities partially
    if "capabilities" in body and isinstance(body["capabilities"], dict):
        caps = dict(updated.get("capabilities") or {})
        for key in ("image", "video", "document", "audio"):
            if key in body["capabilities"]:
                caps[key] = bool(body["capabilities"][key])
        updated["capabilities"] = caps

    # Update label
    if "label" in body and isinstance(body["label"], str) and body["label"].strip():
        updated["label"] = body["label"].strip()

    # Update max_session_chars
    if "max_session_chars" in body:
        try:
            updated["max_session_chars"] = int(body["max_session_chars"])
        except (ValueError, TypeError):
            pass

    # Update thinking support
    if "supports_thinking" in body:
        supports = bool(body["supports_thinking"])
        if supports:
            updated["thinking_modes"] = [
                {"id": "fast", "label": "Fast", "thinking_enabled": False, "auto_thinking": False, "thinking_mode": "Fast"},
                {"id": "high", "label": "Thinking", "thinking_enabled": True, "auto_thinking": False, "thinking_mode": "High"},
            ]
        else:
            updated["thinking_modes"] = [
                {"id": "fast", "label": "Fast", "thinking_enabled": False, "auto_thinking": False, "thinking_mode": "Fast"},
            ]

    # Persist as custom model (works for both custom and static overrides)
    updated["_custom"] = True
    add_custom_model(updated)

    return {"status": "ok", "model": updated}


@router.delete("/api/settings/models/{model_id:path}")
async def delete_model(model_id: str) -> dict[str, Any]:
    """Remove any model (custom or static). Static models get hidden via an exclusion list."""
    from engine.config import remove_custom_model, _load_hidden_models, _save_hidden_models, MODELS
    from engine.cookbook.model_settings import delete_model_settings

    # Always clean up orphaned cookbook settings
    delete_model_settings(model_id)

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


# ---------------------------------------------------------------------------
# MCP (Model Context Protocol) server management
# ---------------------------------------------------------------------------

def _persist_mcp_enabled(name: str, enabled: bool) -> None:
    """Persist enabled flag so restarts restore the last live state."""
    import json as _json
    from engine.mcp.manager import MCP_CONFIG_PATH
    try:
        data = _json.loads(MCP_CONFIG_PATH.read_text())
        data.setdefault("servers", {}).setdefault(name, {})["enabled"] = enabled
        MCP_CONFIG_PATH.write_text(_json.dumps(data, indent=2) + "\n")
    except Exception:
        pass


@router.get("/api/settings/mcp")
async def list_mcp_servers() -> dict[str, Any]:
    """List all configured MCP servers with connection status and tools."""
    from engine.mcp.manager import get_mcp_manager
    manager = get_mcp_manager()
    return {"servers": manager.list_servers()}


@router.post("/api/settings/mcp")
async def add_mcp_server(request: Request) -> dict[str, Any]:
    """Add a new MCP server configuration."""
    from engine.mcp.manager import get_mcp_manager
    body = await request.json()
    name = body.get("name", "").strip()
    command = body.get("command", "").strip()
    args = body.get("args", [])
    env = body.get("env", {})
    enabled = body.get("enabled", True)

    if not name or not command:
        raise HTTPException(status_code=400, detail="Missing 'name' or 'command'")

    manager = get_mcp_manager()
    configs = manager.get_server_configs()
    if name in configs:
        raise HTTPException(status_code=409, detail=f"Server '{name}' already exists")

    manager.add_server(name, {
        "command": command,
        "args": args,
        "env": env,
        "enabled": enabled,
    })
    return {"status": "ok", "name": name}


@router.put("/api/settings/mcp/{name}")
async def update_mcp_server(name: str, request: Request) -> dict[str, Any]:
    """Update an existing MCP server configuration."""
    from engine.mcp.manager import get_mcp_manager
    body = await request.json()
    manager = get_mcp_manager()

    existing = manager.get_server_configs().get(name)
    if not existing:
        raise HTTPException(status_code=404, detail=f"Server '{name}' not found")

    # Merge: only override fields that were explicitly sent
    for key in ("command", "args", "env", "enabled"):
        if key in body:
            existing[key] = body[key]

    if not manager.update_server(name, existing):
        raise HTTPException(status_code=404, detail=f"Server '{name}' not found")

    # Disconnect if connected (config changed)
    await manager.disconnect_server(name)
    _persist_mcp_enabled(name, False)
    return {"status": "ok", "name": name}


@router.delete("/api/settings/mcp/{name}")
async def remove_mcp_server(name: str) -> dict[str, Any]:
    """Remove an MCP server configuration."""
    from engine.mcp.manager import get_mcp_manager
    manager = get_mcp_manager()

    # Disconnect first
    await manager.disconnect_server(name)

    if not manager.remove_server(name):
        raise HTTPException(status_code=404, detail=f"Server '{name}' not found")
    return {"status": "ok", "name": name}


@router.post("/api/settings/mcp/{name}/connect")
async def connect_mcp_server(name: str) -> dict[str, Any]:
    """Connect to a configured MCP server."""
    from engine.mcp.manager import get_mcp_manager
    manager = get_mcp_manager()

    conn = await manager.connect_server(name)
    if conn is None:
        raise HTTPException(status_code=404, detail=f"Server '{name}' not found in config")

    if conn.connected:
        _persist_mcp_enabled(name, True)

    return {
        "status": "ok" if conn.connected else "error",
        "name": name,
        "connected": conn.connected,
        "error": conn.error,
        "tools": conn.tools,
    }


@router.post("/api/settings/mcp/{name}/disconnect")
async def disconnect_mcp_server(name: str) -> dict[str, Any]:
    """Disconnect from an MCP server."""
    from engine.mcp.manager import get_mcp_manager
    manager = get_mcp_manager()
    await manager.disconnect_server(name)
    _persist_mcp_enabled(name, False)
    return {"status": "ok", "name": name}


@router.post("/api/settings/mcp/{name}/call")
async def call_mcp_tool(name: str, request: Request) -> dict[str, Any]:
    """Call a tool on a connected MCP server."""
    from engine.mcp.manager import get_mcp_manager
    body = await request.json()
    tool_name = body.get("tool", "").strip()
    arguments = body.get("arguments", {})

    if not tool_name:
        raise HTTPException(status_code=400, detail="Missing 'tool' field")

    manager = get_mcp_manager()
    try:
        result = await manager.call_tool(name, tool_name, arguments)
        return {"status": "ok", "result": result}
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Tool call failed: {exc}")


@router.get("/api/settings/mcp/tools")
async def list_all_mcp_tools() -> dict[str, Any]:
    """Get all tools from all connected MCP servers."""
    from engine.mcp.manager import get_mcp_manager
    manager = get_mcp_manager()
    return {"tools": manager.get_all_tools()}


@router.get("/api/settings/accounts")
async def list_accounts() -> dict[str, Any]:
    def _scan() -> list[dict[str, Any]]:
        # Load token presence maps
        waf_tokens: dict = {}
        ds_tokens: dict = {}
        try:
            waf_tokens = json.loads((_SYSTEM_DIR / ".qwen_tokens.json").read_text())
        except Exception:
            pass
        try:
            ds_tokens = json.loads((_SYSTEM_DIR / ".deepseek_tokens.json").read_text())
        except Exception:
            pass

        # Load exhaustion & captcha block status
        from engine.config import get_all_exhaustion_status, get_all_captcha_block_status
        exhaustion = get_all_exhaustion_status()
        captcha_blocks = get_all_captcha_block_status()

        # Load per-account browser config
        acc_cfg = _read_accounts_config()

        def _browser_label(path: str) -> str:
            if not path:
                return "Playwright"
            if path == "default":
                return "Default"
            bname = Path(path).stem.lower()
            for kw in ("chrome", "chromium", "thorium", "helium", "brave", "vivaldi", "msedge", "edge"):
                if kw in bname:
                    return kw.capitalize() if kw != "msedge" else "Edge"
            return Path(path).stem

        accounts: list[dict[str, Any]] = []
        for entry in _SYSTEM_DIR.iterdir():
            m = re.match(r"browser-data-acc(\d+)$", entry.name)
            if entry.is_dir() and m:
                browser_path = acc_cfg.get(entry.name, {}).get("browser_path", "")
                # Check if saved browser still exists on disk
                browser_available = True
                if browser_path and browser_path != "default":
                    browser_available = os.path.isfile(browser_path)
                # Check for actual tokens (not just key presence — empty lists mean no tokens)
                _waf_list = waf_tokens.get(entry.name, [])
                _ds_list = ds_tokens.get(entry.name, [])
                _has_waf = bool(_waf_list) if isinstance(_waf_list, list) else bool(_waf_list)
                _has_ds = bool(_ds_list) if isinstance(_ds_list, list) else bool(_ds_list)
                accounts.append({
                    "name": entry.name,
                    "num": int(m.group(1)),
                    "email": _read_profile_email(entry),
                    "label": acc_cfg.get(entry.name, {}).get("label", ""),
                    "size_mb": _dir_size_mb(entry),
                    "has_waf": _has_waf,
                    "has_ds": _has_ds,
                    "exhausted": exhaustion.get(entry.name, False),
                    "captcha_blocked": captcha_blocks.get(entry.name, False),
                    "browser_path": browser_path,
                    "browser_label": _browser_label(browser_path),
                    "browser_available": browser_available,
                    "has_backup": (_SYSTEM_DIR / f"{entry.name}.bak").is_dir(),
                })
        accounts.sort(key=lambda a: a["num"])
        return accounts
    accounts = await asyncio.to_thread(_scan)
    # Legacy endpoint: `active` reflects the PRIMARY provider (qwen) slot only.
    # The per-provider store is the source of truth for every other provider.
    from engine.config import get_provider_active_account, PRIMARY_BROWSER_PROVIDER
    active = get_provider_active_account(PRIMARY_BROWSER_PROVIDER)
    _settings = _read_system_settings()
    return {
        "accounts": accounts,
        "active": active,
        "auto_switch_enabled": _settings.get("account_auto_switch_enabled", True),
    }

@router.post("/api/settings/accounts/auto-switch-toggle")
async def toggle_auto_switch(payload: dict[str, Any]) -> dict[str, Any]:
    """Toggle account auto-switch on/off (legacy global — sets all providers)."""
    enabled = payload.get("enabled", True)
    _settings = _read_system_settings()
    _settings["account_auto_switch_enabled"] = bool(enabled)
    # Also update per-provider map so new UI stays in sync
    per_provider = _settings.get("account_auto_switch", {})
    if not isinstance(per_provider, dict):
        per_provider = {}
    for prov in ("qwen", "deepseek", "gemini", "groq", "mistral", "openai", "puter", "cloudflare"):
        per_provider[prov] = bool(enabled)
    _settings["account_auto_switch"] = per_provider
    _write_system_settings(_settings)
    return {"auto_switch_enabled": bool(enabled)}


# --- Per-provider account/key listing & auto-switch ---

# Providers that use browser profiles (accounts) vs API keys
_BROWSER_PROVIDERS = ("qwen",)
_API_KEY_PROVIDERS = ("gemini", "groq", "mistral", "openai", "puter")

# Map provider → (module_path, client_class_or_getter)
_API_KEY_PROVIDER_MODULES: dict[str, tuple[str, str]] = {
    "gemini": ("connectors.gemini.client", "get_client"),
    "groq": ("connectors.groq.client", "get_client"),
    "mistral": ("connectors.mistral.client", "get_client"),
    "openai": ("connectors.openai.client", "get_client"),
    "puter": ("connectors.puter.client", "get_client"),
}


def _get_api_key_client(provider: str):
    """Lazy-import and return the singleton client for an API key provider."""
    import importlib
    mod_path, func_name = _API_KEY_PROVIDER_MODULES[provider]
    mod = importlib.import_module(mod_path)
    return getattr(mod, func_name)()


def get_auto_switch_for_provider(provider: str) -> bool:
    """Check if auto-switch is enabled for a specific provider.

    Falls back to legacy global `account_auto_switch_enabled` if per-provider
    setting doesn't exist yet (migration path).
    """
    try:
        settings = _read_system_settings()
        per_provider = settings.get("account_auto_switch")
        if isinstance(per_provider, dict) and provider in per_provider:
            return bool(per_provider[provider])
        # Fallback to legacy global setting
        return bool(settings.get("account_auto_switch_enabled", True))
    except Exception:
        return True


@router.get("/api/settings/accounts/by-provider")
async def list_accounts_by_provider() -> dict[str, Any]:
    """Return all providers with their accounts/keys, active status, and auto-switch state."""
    from engine.config import get_provider_active_account, PRIMARY_BROWSER_PROVIDER
    active_account = get_provider_active_account(PRIMARY_BROWSER_PROVIDER)
    _settings = _read_system_settings()
    per_provider_switch = _settings.get("account_auto_switch", {})
    if not isinstance(per_provider_switch, dict):
        per_provider_switch = {}
    legacy_switch = _settings.get("account_auto_switch_enabled", True)

    def _auto_sw(prov: str) -> bool:
        if prov in per_provider_switch:
            return bool(per_provider_switch[prov])
        return bool(legacy_switch)

    providers: dict[str, Any] = {}

    # --- Browser-based providers (Qwen, DeepSeek) ---
    waf_tokens: dict = {}
    ds_tokens: dict = {}
    zai_tokens: dict = {}
    try:
        waf_tokens = json.loads((_SYSTEM_DIR / ".qwen_tokens.json").read_text())
    except Exception:
        pass
    try:
        ds_tokens = json.loads((_SYSTEM_DIR / ".deepseek_tokens.json").read_text())
    except Exception:
        pass
    try:
        zai_tokens = json.loads((_SYSTEM_DIR / ".zai_tokens.json").read_text())
    except Exception:
        pass
    # Gemini (web) auth snapshots — {account: {cookies: {...}, saved_at: ts}}
    gemini_auth: dict = {}
    try:
        gemini_auth = json.loads((_SYSTEM_DIR / ".gemini_web_auth.json").read_text())
    except Exception:
        pass

    # MiMo Studio auth snapshots — per-account {cookies: {...}}
    mimo_auth: dict = {}
    try:
        for _ma in _SYSTEM_DIR.glob("browser-data-*/.mimo_auth.json"):
            try:
                mimo_auth[_ma.parent.name] = json.loads(_ma.read_text())
            except Exception:
                pass
    except Exception:
        pass

    # Mistral (Web) auth snapshots — per-account {cookie_name: value}
    mistral_web_auth: dict = {}
    try:
        for _mw in _SYSTEM_DIR.glob("browser-data-*/.mistral_web_auth.json"):
            try:
                mistral_web_auth[_mw.parent.name] = json.loads(_mw.read_text())
            except Exception:
                pass
    except Exception:
        pass

    from engine.config import get_all_exhaustion_status, get_all_captcha_block_status
    exhaustion = get_all_exhaustion_status()
    captcha_blocks = get_all_captcha_block_status()
    acc_cfg = _read_accounts_config()

    def _browser_label(path: str) -> str:
        if not path:
            return "Playwright"
        if path == "default":
            return "Default"
        bname = Path(path).stem.lower()
        for kw in ("chrome", "chromium", "thorium", "helium", "brave", "vivaldi", "msedge", "edge"):
            if kw in bname:
                return kw.capitalize() if kw != "msedge" else "Edge"
        return Path(path).stem

    def _profile_has_mistral_web(entry: Path) -> bool:
        """True if this profile holds a *logged-in* chat.mistral.ai session.

        Mistral auth is an Ory Kratos ``ory_session_*`` cookie. Checks:
        1. ``.mistral_web_auth.json`` snapshot (written by service.py on refresh)
        2. Live SQLite query for ``ory_session_`` cookies (not raw binary grep,
           which matches stale deleted rows)
        """
        # Primary: disk snapshot from last refresh
        try:
            if (entry / ".mistral_web_auth.json").is_file():
                import json as _json
                jar = _json.loads((entry / ".mistral_web_auth.json").read_text())
                if any(k.startswith("ory_session_") and v for k, v in jar.items()):
                    return True
        except Exception:
            pass

        # Fallback: live SQLite query (avoids stale byte matches)
        try:
            import sqlite3
            ck = entry / "Default" / "Cookies"
            if ck.is_file():
                conn = sqlite3.connect(f"file:{ck}?mode=ro", uri=True)
                try:
                    cur = conn.execute(
                        "SELECT name FROM cookies WHERE host_key LIKE '%mistral%' "
                        "AND name LIKE 'ory_session_%'"
                    )
                    if cur.fetchone() is not None:
                        return True
                finally:
                    conn.close()
        except Exception:
            pass
        return False

    def _profile_has_kimi(entry: Path) -> bool:
        """True if this profile holds a kimi.ai login.

        Checks two sources:
        1. ``.kimi_tokens.json`` — the persisted token store written by the
           ghost session whenever it finds a live JWT. This is the primary
           and most reliable signal.
        2. Chromium Cookies DB — fallback that queries actual live cookie
           rows (not raw binary grep, which matches stale deleted data).
        """
        # Primary: check the persisted token store
        try:
            from connectors.kimi.ghost import TOKEN_STORE_PATH, _load_token_store
            if TOKEN_STORE_PATH.exists():
                store = _load_token_store()
                # Match by account name (e.g. "browser-data-acc1")
                acct_name = entry.name
                if acct_name in store and store[acct_name]:
                    return True
        except Exception:
            pass

        # Fallback: query the actual SQLite cookies table for live kimi cookies
        try:
            import sqlite3
            ck = entry / "Default" / "Cookies"
            if ck.is_file():
                conn = sqlite3.connect(f"file:{ck}?mode=ro", uri=True)
                try:
                    cur = conn.execute(
                        "SELECT name FROM cookies WHERE host_key LIKE '%kimi%' "
                        "AND (name='access_token' OR name='refresh_token' OR name='kimi_session')"
                    )
                    if cur.fetchone() is not None:
                        return True
                finally:
                    conn.close()
        except Exception:
            pass
        return False

    def _profile_has_chatgpt(entry: Path) -> bool:
        """True if this profile holds a chatgpt.com login.

        ChatGPT auth is a ``__Secure-next-auth.session-token`` cookie (split
        into ``.0``/``.1`` chunks). Query the live SQLite cookies table for it
        (not a binary grep, which matches stale deleted rows).
        """
        try:
            import sqlite3
            ck = entry / "Default" / "Cookies"
            if ck.is_file():
                conn = sqlite3.connect(f"file:{ck}?mode=ro", uri=True)
                try:
                    cur = conn.execute(
                        "SELECT name FROM cookies WHERE host_key LIKE '%chatgpt%' "
                        "AND name LIKE '__Secure-next-auth.session-token%'"
                    )
                    if cur.fetchone() is not None:
                        return True
                finally:
                    conn.close()
        except Exception:
            pass
        return False

    def _scan_browser_accounts() -> list[dict[str, Any]]:
        accounts: list[dict[str, Any]] = []
        for entry in _SYSTEM_DIR.iterdir():
            m = re.match(r"browser-data-acc(\d+)$", entry.name)
            if entry.is_dir() and m:
                browser_path = acc_cfg.get(entry.name, {}).get("browser_path", "")
                browser_available = True
                if browser_path and browser_path != "default":
                    browser_available = os.path.isfile(browser_path)
                _waf_list = waf_tokens.get(entry.name, [])
                _ds_list = ds_tokens.get(entry.name, [])
                _zai_list = zai_tokens.get(entry.name, [])
                _has_waf = bool(_waf_list) if isinstance(_waf_list, list) else bool(_waf_list)
                _has_ds = bool(_ds_list) if isinstance(_ds_list, list) else bool(_ds_list)
                _has_zai = any(t and t != "None" for t in _zai_list) if isinstance(_zai_list, list) else bool(_zai_list)
                _g_entry = gemini_auth.get(entry.name) if isinstance(gemini_auth, dict) else None
                _has_gemini = bool(
                    isinstance(_g_entry, dict) and _g_entry.get("cookies")
                )
                _mimo_jar = mimo_auth.get(entry.name) if isinstance(mimo_auth, dict) else None
                _has_mimo = bool(
                    isinstance(_mimo_jar, dict)
                    and _mimo_jar.get("xiaomichatbot_serviceToken")
                )
                _has_kimi = _profile_has_kimi(entry)
                _has_chatgpt = _profile_has_chatgpt(entry)
                _has_mistral_web = _profile_has_mistral_web(entry)
                accounts.append({
                    "name": entry.name,
                    "num": int(m.group(1)),
                    "email": _read_profile_email(entry),
                    "label": acc_cfg.get(entry.name, {}).get("label", ""),
                    "size_mb": _dir_size_mb(entry),
                    "has_waf": _has_waf,
                    "has_ds": _has_ds,
                    "has_zai": _has_zai,
                    "has_gemini": _has_gemini,
                    "has_mimo": _has_mimo,
                    "has_kimi": _has_kimi,
                    "has_chatgpt": _has_chatgpt,
                    "has_mistral_web": _has_mistral_web,
                    "exhausted": exhaustion.get(entry.name, False),
                    "captcha_blocked": captcha_blocks.get(entry.name, False),
                    "browser_path": browser_path,
                    "browser_label": _browser_label(browser_path),
                    "browser_available": browser_available,
                    "has_backup": (_SYSTEM_DIR / f"{entry.name}.bak").is_dir(),
                })
        accounts.sort(key=lambda a: a["num"])
        return accounts

    browser_accounts = await asyncio.to_thread(_scan_browser_accounts)

    def _scan_unregistered_browser_data() -> list[dict[str, Any]]:
        """Find browser data directories that don't match the acc{N} pattern."""
        unregistered: list[dict[str, Any]] = []
        skip_names = {".git", ".venv", "__pycache__", ".sable_backups", ".pytest_cache"}
        for entry in _SYSTEM_DIR.iterdir():
            if entry.is_symlink() or not entry.is_dir() or entry.name in skip_names:
                continue
            if entry.name.endswith(".bak"):
                continue
            if re.match(r"browser-data-acc\d+$", entry.name):
                continue
            # Only include dirs that look like browser data
            is_browser_dir = any(
                kw in entry.name.lower()
                for kw in ("browser", "playwright", "chromium", "chrome", "profile")
            )
            if not is_browser_dir:
                try:
                    children = [c.name for c in entry.iterdir()]
                    is_browser_dir = any(
                        marker in children
                        for marker in ("Default", "Preferences", "Cookies", "Local Storage", "IndexedDB")
                    )
                except Exception:
                    pass
            if not is_browser_dir:
                continue
            browser_path = acc_cfg.get(entry.name, {}).get("browser_path", "")
            browser_available = True
            if browser_path and browser_path != "default":
                browser_available = os.path.isfile(browser_path)
            unregistered.append({
                "name": entry.name,
                "num": -1,
                "email": entry.name.replace("-", " ").replace("_", " ").title(),
                "label": acc_cfg.get(entry.name, {}).get("label", ""),
                "size_mb": _dir_size_mb(entry),
                "has_waf": False,
                "has_ds": False,
                "has_zai": False,
                "has_gemini": False,
                "has_mimo": False,
                "has_kimi": _profile_has_kimi(entry),
                "has_chatgpt": _profile_has_chatgpt(entry),
                "has_mistral_web": _profile_has_mistral_web(entry),
                "exhausted": False,
                "captcha_blocked": False,
                "browser_path": browser_path,
                "browser_label": _browser_label(browser_path),
                "browser_available": browser_available,
                "has_backup": (_SYSTEM_DIR / f"{entry.name}.bak").is_dir(),
                "unregistered": True,
            })
        unregistered.sort(key=lambda a: a["name"])
        return unregistered

    unregistered_browser_data = await asyncio.to_thread(_scan_unregistered_browser_data)

    from engine.config import get_provider_active_account as _get_active_for

    def _browser_provider(pid: str, label: str, accounts: list[dict[str, Any]], avail_key: str | None = None) -> dict[str, Any]:
        """Shape a browser-backed provider payload.

        Every provider owns its OWN active account — `active` is that provider's
        slot (never the global/primary slot of another provider). The card whose
        name matches is flagged `active`; because there is no more "follow the
        global" concept, `pinned` is True for the active card (kept for UI
        back-compat with the old unpin affordance).

        Account dicts are shallow-copied because the same object appears in
        several provider lists and each provider has its own active account.
        """
        own_active = _get_active_for(pid)
        rows = [dict(a) for a in accounts]
        # Flag whether this account holds a token for THIS provider, then sort
        # available (token-holders) first. A tokenless profile is still shown
        # and still selectable — you pick it, then refresh to mint a token.
        for a in rows:
            a["provider_available"] = bool(a.get(avail_key)) if avail_key else True

        def _sort_key(a: dict[str, Any]) -> tuple:
            num = a.get("num", -1)
            num = num if isinstance(num, int) and num >= 0 else 9999
            return (0 if a["provider_available"] else 1, num, a.get("name", ""))

        rows.sort(key=_sort_key)
        names = {a["name"] for a in rows}
        active = own_active if own_active in names else None
        for a in rows:
            a["pinned"] = bool(active) and a["name"] == active
        return {
            "type": "browser",
            "label": label,
            "accounts": rows,
            "active": active,
            "pinned": active if (active and active in names) else None,
            "auto_switch_enabled": _auto_sw(pid),
        }

    # Every provider lists ALL browser profiles — token-holders first, then
    # tokenless ones — so a brand-new profile can be selected before it has a
    # token (select it, then refresh to mint one). Availability is flagged per
    # provider via provider_available.
    providers["qwen"] = _browser_provider("qwen", "Qwen", list(browser_accounts), avail_key="has_waf")

    # z.ai (GLM)
    providers["zai"] = _browser_provider("zai", "z.ai (GLM)", list(browser_accounts), avail_key="has_zai")

    # Gemini (web)
    providers["gemini_web"] = _browser_provider("gemini_web", "Gemini (Web)", list(browser_accounts), avail_key="has_gemini")

    # MiMo Studio (Xiaomi)
    providers["mimo"] = _browser_provider("mimo", "MiMo (Xiaomi)", list(browser_accounts), avail_key="has_mimo")

    # Kimi (kimi.ai): browser-based. Lists every profile holding a kimi login —
    # both standard acc* profiles AND dedicated non-acc dirs (e.g.
    # browser-data-kimi), so the connector's own profile shows up too.
    kimi_accounts = list(browser_accounts)
    kimi_accounts += [a for a in unregistered_browser_data if a.get("has_kimi")]
    providers["kimi"] = _browser_provider("kimi", "Kimi", kimi_accounts, avail_key="has_kimi")

    # ChatGPT (chatgpt.com): browser-based. Lists every profile holding a
    # chatgpt.com login — both standard acc* profiles AND dedicated non-acc dirs.
    chatgpt_accounts = list(browser_accounts)
    chatgpt_accounts += [a for a in unregistered_browser_data if a.get("has_chatgpt")]
    providers["chatgpt"] = _browser_provider("chatgpt", "ChatGPT", chatgpt_accounts, avail_key="has_chatgpt")

    # Mistral (Web): browser-based. Lists every profile holding a chat.mistral.ai
    # session — both standard acc* profiles AND dedicated non-acc dirs.
    mistral_web_accounts = list(browser_accounts)
    mistral_web_accounts += [a for a in unregistered_browser_data if a.get("has_mistral_web")]
    providers["mistral_web"] = _browser_provider("mistral_web", "Mistral (Web)", mistral_web_accounts, avail_key="has_mistral_web")

    # DeepSeek (scraper): headed-browser transport. It is its OWN browser
    # provider with its own active-account slot — separate from the DeepSeek
    # API-key provider below. Lists every standard acc* profile (any of which
    # could hold a deepseek.com login).
    ds_scraper_accounts = list(browser_accounts)
    providers["deepseek_scraper"] = _browser_provider(
        "deepseek_scraper", "DeepSeek (Scraper)", ds_scraper_accounts, avail_key="has_ds"
    )

    # DeepSeek: present as api_key type — tokens are JWTs, no browser swap needed
    try:
        from connectors.deepseek.client import get_unique_tokens_with_accounts as _ds_unique_acct
        _ds_entries = _ds_unique_acct()
        _ds_client = get_deepseek_client()
        # Use .token property (not ._token) so persisted preference is resolved
        _ds_active_token = _ds_client.token or None
        _ds_keys = []
        for _i, _entry in enumerate(_ds_entries):
            _tok = _entry["token"]
            _acct = _entry["account"]
            _masked = _tok[:8] + "..." + _tok[-4:] if len(_tok) > 12 else "***"
            # Extract account number from "browser-data-accN" for display
            _acct_match = re.match(r"browser-data-acc(\d+)$", _acct)
            _browser_tag = f"acc{_acct_match.group(1)}" if _acct_match else _acct
            _ds_keys.append({
                "index": _i,
                "masked": _masked,
                "active": _tok == _ds_active_token,
                "browser_data": _browser_tag,
            })
        _ds_active_idx = next((k["index"] for k in _ds_keys if k["active"]), None)
        # DeepSeek now offers browser-profile SELECTION (same shape as every
        # other browser provider) so a tokenless profile can be chosen and then
        # refreshed. The parsed JWT key list rides along for reference.
        _ds_prov = _browser_provider(
            "deepseek", "DeepSeek", list(browser_accounts), avail_key="has_ds")
        _ds_prov["keys"] = _ds_keys
        _ds_prov["active_index"] = _ds_active_idx
        _ds_prov["available"] = bool(_ds_entries)
        providers["deepseek"] = _ds_prov
    except Exception as exc:
        providers["deepseek"] = {
            "type": "browser",
            "label": "DeepSeek",
            "accounts": [],
            "keys": [],
            "active": None,
            "pinned": None,
            "active_index": None,
            "available": False,
            "auto_switch_enabled": _auto_sw("deepseek"),
            "error": str(exc),
        }

    # --- API key providers ---
    for prov_id in _API_KEY_PROVIDERS:
        try:
            client = _get_api_key_client(prov_id)
            keys = client.list_keys()
            active_idx = next((k["index"] for k in keys if k.get("active")), None)
            providers[prov_id] = {
                "type": "api_key",
                "label": prov_id.capitalize(),
                "keys": keys,
                "active_index": active_idx,
                "available": client.is_available,
                "auto_switch_enabled": _auto_sw(prov_id),
            }
        except Exception as exc:
            providers[prov_id] = {
                "type": "api_key",
                "label": prov_id.capitalize(),
                "keys": [],
                "active_index": None,
                "available": False,
                "auto_switch_enabled": _auto_sw(prov_id),
                "error": str(exc),
            }

    # Cloudflare: single credential
    try:
        from connectors.cloudflare.client import get_client as get_cf_client
        cf = get_cf_client()
        providers["cloudflare"] = {
            "type": "credential",
            "label": "Cloudflare",
            "available": cf.is_available,
            "auto_switch_enabled": _auto_sw("cloudflare"),
        }
    except Exception:
        providers["cloudflare"] = {
            "type": "credential",
            "label": "Cloudflare",
            "available": False,
            "auto_switch_enabled": _auto_sw("cloudflare"),
        }

    return {
        "providers": providers,
        "active_account": active_account,
        "unregistered_browser_data": unregistered_browser_data,
    }


@router.post("/api/settings/accounts/auto-switch-toggle/{provider}")
async def toggle_auto_switch_per_provider(provider: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Toggle auto-switch for a specific provider."""
    provider = provider.lower().strip()
    enabled = bool(payload.get("enabled", True))
    _settings = _read_system_settings()
    per_provider = _settings.get("account_auto_switch", {})
    if not isinstance(per_provider, dict):
        per_provider = {}
    per_provider[provider] = enabled
    _settings["account_auto_switch"] = per_provider
    _write_system_settings(_settings)
    return {"provider": provider, "auto_switch_enabled": enabled}


@router.post("/api/settings/{provider}/switch-key")
async def switch_api_key(provider: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Manually switch to a specific API key by index for an API key provider."""
    provider = provider.lower().strip()
    index = payload.get("index")
    if index is None or not isinstance(index, int):
        raise HTTPException(status_code=400, detail="Missing or invalid 'index' field")

    # DeepSeek: silent token swap — no browser profile changes
    if provider == "deepseek":
        try:
            from connectors.deepseek.client import get_unique_tokens_with_accounts as _ds_unique_acct
            entries = _ds_unique_acct()
            if index < 0 or index >= len(entries):
                raise HTTPException(status_code=404, detail="Key index out of range")
            target_token = entries[index]["token"]
            target_account = entries[index]["account"]
            client = get_deepseek_client()
            # Persist the user's manual selection so it survives restarts
            _settings = _read_system_settings()
            _settings["deepseek_preferred_token"] = target_token
            _settings["deepseek_preferred_account"] = target_account
            _write_system_settings(_settings)
            # Set token in-memory and persist under its own account
            client.set_token(target_token, account=target_account, persist=True)
            # Rebuild key list for response
            keys = []
            for i, entry in enumerate(entries):
                tok = entry["token"]
                acct = entry["account"]
                masked = tok[:8] + "..." + tok[-4:] if len(tok) > 12 else "***"
                acct_match = re.match(r"browser-data-acc(\d+)$", acct)
                browser_tag = f"acc{acct_match.group(1)}" if acct_match else acct
                keys.append({"index": i, "masked": masked, "active": i == index, "browser_data": browser_tag})
            return {"status": "ok", "keys": keys}
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"DeepSeek switch failed: {exc}")

    if provider not in _API_KEY_PROVIDER_MODULES:
        raise HTTPException(status_code=400, detail=f"Provider '{provider}' does not support key switching")
    try:
        client = _get_api_key_client(provider)
        if index < 0 or index >= len(client._keys):
            raise HTTPException(status_code=404, detail="Key index out of range")
        client._key_index = index
        return {"status": "ok", "keys": client.list_keys()}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Switch failed: {exc}")

# Every browser-backed provider now owns an independent active-account slot.
# This is the canonical set for per-provider switching (qwen included — it is
# just another provider with its own slot, no longer a special global).
_BROWSER_PROVIDERS_ALL = (
    "qwen", "deepseek", "deepseek_scraper", "zai", "gemini_web", "gemini",
    "mimo", "kimi", "mistral_web", "chatgpt",
)
# Back-compat alias (older references).
_PER_PROVIDER_SWITCH = _BROWSER_PROVIDERS_ALL


@router.post("/api/settings/accounts/switch-provider")
async def switch_provider_account(payload: dict[str, Any]) -> dict[str, Any]:
    """Set ONE provider's active browser account (independent of all others).

    Each provider owns its own active-account slot: switching zai to acc7 does
    not move qwen, gemini, or any other provider. Browser teardown: the
    provider's current BrowserManager is closed so the next use relaunches
    against the new data dir. Browsers are keyed by data dir, so a provider
    sharing the same account keeps its own handle and is unaffected.
    """
    provider = str(payload.get("provider", "")).lower().strip()
    if provider not in _BROWSER_PROVIDERS_ALL:
        raise HTTPException(
            status_code=400,
            detail=f"Provider '{provider}' does not support per-provider switching",
        )
    target_name = str(payload.get("profile", "") or "").strip()

    from engine.config import set_provider_active_account, get_provider_browser_dir

    if target_name:
        if not re.match(r"^browser-data-[a-z0-9-]+$", target_name):
            raise HTTPException(status_code=400, detail="Profile must match 'browser-data-*'")
        if not (_SYSTEM_DIR / target_name).is_dir():
            raise HTTPException(status_code=404, detail=f"Profile directory '{target_name}' not found")

    # Resolve the dir currently in use BEFORE changing the slot, so we tear down
    # the right browser.
    old_dir = str(await asyncio.to_thread(get_provider_browser_dir, provider))

    try:
        await asyncio.to_thread(set_provider_active_account, provider, target_name or "")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to persist active account: {exc}")

    # Tear down the old browser for this provider so it relaunches on the new dir.
    # The default browser (self._browser) is qwen's slot — if THIS provider was
    # using it, close the default too and repoint it, else only the scoped one.
    try:
        from engine.config import PRIMARY_BROWSER_PROVIDER
        default_dir = str(service._browser.user_data_dir)
        is_primary = provider == PRIMARY_BROWSER_PROVIDER
        if is_primary:
            # Qwen owns the default browser — close it and reset its dir so the
            # next launch uses the new slot.
            if service._headers_account != (target_name or None):
                service._headers = None
                service._headers_account = None
            await service.close()
            new_dir_for_default = str(await asyncio.to_thread(get_provider_browser_dir, provider))
            service._browser.user_data_dir = new_dir_for_default
            # Repoint the override to the resolved account (empty string → None
            # so the service re-resolves from the store on next use).
            service._account_override = Path(new_dir_for_default).name or None
        elif old_dir != default_dir:
            await service.close_scoped(old_dir)
    except Exception as exc:
        logger.warning("switch-provider teardown failed for %s: %s", provider, exc)

    # Drop the connector's in-memory cookie/token cache. Browser-auth clients
    # (mistral_web, gemini_web, mimo) cache their jar for minutes; without this
    # a fresh pin keeps serving the PREVIOUS account's session until the TTL
    # expires, so the switch appears to do nothing on the backend.
    try:
        if provider in ("gemini_web", "gemini"):
            from connectors.gemini_web.client import get_client as _gw_inv
            _gw_inv().invalidate_jar()
        elif provider == "mimo":
            from connectors.mimo.client import get_client as _mimo_inv
            _mimo_inv().invalidate_jar()
        elif provider == "mistral_web":
            from connectors.mistral_web.client import get_client as _mw_inv
            _mw_inv().invalidate_jar()
        elif provider == "zai":
            try:
                from connectors.zai.client import get_client as _zai_inv
                _zai_inv().invalidate_jar()
            except AttributeError:
                pass
    except Exception as exc:
        logger.warning("switch-provider cache invalidate failed for %s: %s", provider, exc)

    new_dir = str(await asyncio.to_thread(get_provider_browser_dir, provider))
    new_account = Path(new_dir).name
    label = _read_profile_email(Path(new_dir)) if Path(new_dir).is_dir() else new_account
    return {
        "status": "ok",
        "provider": provider,
        "pinned": target_name or None,
        "active": new_account,
        "email": label,
    }


@router.post("/api/settings/accounts/switch")
async def switch_account(payload: dict[str, str]) -> dict[str, Any]:
    """Legacy global switch — now scoped to the PRIMARY provider (qwen).

    There is no shared global browser anymore: this sets qwen's own slot and
    repoints the default browser. Other providers keep their independent
    active accounts untouched. New UI should call /accounts/switch-provider.
    """
    target_name = payload.get("profile", "")
    if not target_name.startswith("browser-data-acc"):
        raise HTTPException(status_code=400, detail="Profile must match 'browser-data-acc*'")
    target_path = _SYSTEM_DIR / target_name
    if not target_path.is_dir():
        raise HTTPException(status_code=404, detail=f"Profile directory '{target_name}' not found")
    # Resolve old profile before switching (for post-switch strip)
    from engine.config import (
        get_provider_active_account, set_provider_active_account,
        PRIMARY_BROWSER_PROVIDER, _SYSTEM as _SYS,
    )
    old_profile: Path | None = None
    current_active = get_provider_active_account(PRIMARY_BROWSER_PROVIDER)
    current_path = _SYS / current_active
    if current_path != target_path and current_path.is_dir():
        old_profile = current_path

    try:
        await service.close()
        set_provider_active_account(PRIMARY_BROWSER_PROVIDER, target_name)
        # Update the singleton service's browser profile and account override
        # so subsequent requests use the NEW account, not the stale one from init.
        service._browser.user_data_dir = str(target_path)
        service._account_override = target_name
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Switch failed: {exc}")

    email = _read_profile_email(target_path)

    # ── Optimistic response: everything below is post-switch warm-up and runs
    # OFF the request path. Ordering guarantees at most ONE browser launch even
    # when both token sets are cold: warmup may open a browser, DeepSeek
    # extraction reuses it (never closes a browser it didn't open), and persona
    # sync is pure HTTP once headers are cached.
    # Deferral is safe: _ensure_headers() self-heals lazily on first chat, and
    # the DeepSeek client falls back to its disk store / browser refresh.
    async def _post_switch_warmup(account: str) -> None:
        try:
            # Refresh every provider against ITS OWN active account (grouped so
            # providers sharing an account refresh together). Isolated per
            # provider so one failure never blocks the rest.
            _tok = await service.refresh_all_provider_tokens()
            logger.info("Post-switch provider token refresh for %s: %s", account, _tok)
            from connectors.common.instruction_builder import invalidate_cache
            invalidate_cache()
            from engine.config import mark_sync_failed, clear_sync_failed
            try:
                _sync_result = await service.sync_context()
                if _sync_result is False:
                    logger.warning("sync_context returned False after manual switch")
                    mark_sync_failed(account)
                else:
                    clear_sync_failed(account)
            except Exception as exc:
                logger.warning("sync_context after switch failed: %s", exc)
                mark_sync_failed(account)
        except Exception as exc:
            logger.warning("Post-switch warmup failed: %s: %s", type(exc).__name__, exc)

    _spawn_bg(_post_switch_warmup(target_name))

    # Strip the old profile to reclaim disk space (true fire-and-forget:
    # off the request path; guard inside refuses if it became active again)
    if old_profile and old_profile.is_dir():
        async def _strip_bg(profile: Path) -> None:
            try:
                name, before, after = await asyncio.to_thread(_strip_one_profile, profile)
                logger.info("Auto-stripped old profile %s: %.1fMB → %.1fMB", name, before, after)
            except Exception as exc:
                logger.warning("Failed to strip old profile: %s", exc)

        _spawn_bg(_strip_bg(old_profile))

    return {"status": "ok", "active": target_name, "email": email}


# ─── accounts.json helpers ──────────────────────────────────────────────────
_ACCOUNTS_JSON = _SYSTEM_DIR / "accounts.json"


def _read_accounts_config() -> dict[str, Any]:
    try:
        return json.loads(_ACCOUNTS_JSON.read_text())
    except Exception:
        return {}


def _write_accounts_config(cfg: dict[str, Any]) -> None:
    _ACCOUNTS_JSON.write_text(json.dumps(cfg, indent=2))


def _get_account_browser(profile_name: str) -> str | None:
    cfg = _read_accounts_config()
    return cfg.get(profile_name, {}).get("browser_path")


def _set_account_browser(profile_name: str, browser_path: str) -> None:
    cfg = _read_accounts_config()
    if profile_name not in cfg:
        cfg[profile_name] = {}
    cfg[profile_name]["browser_path"] = browser_path
    _write_accounts_config(cfg)


@router.get("/api/settings/accounts/available-browsers")
async def available_browsers() -> dict[str, Any]:
    """Return all detected Chrome-compatible browsers on this system."""
    from engine.platform_paths import list_available_browsers
    browsers = await asyncio.to_thread(list_available_browsers)
    return {"browsers": browsers}


@router.post("/api/settings/accounts/create")
async def create_account(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Find next available acc integer and launch headed browser for login.

    `payload.providers` (list, optional) decides which login sites open — one
    tab per entry. Falls back to `payload.provider` (single) and then qwen for
    backward compatibility. The profile is generic — a browser-data-accN dir —
    so one account can hold several providers at once.
    """
    _LOGIN_URLS = {
        "qwen": "https://chat.qwen.ai",
        "zai": "https://chat.z.ai",
        "gemini_web": "https://gemini.google.com/app",
        "gemini": "https://gemini.google.com/app",
        "deepseek": "https://chat.deepseek.com",
        "mimo": "https://aistudio.xiaomimimo.com",
        "mistral_web": "https://chat.mistral.ai/chat",
    }

    # Resolve the list of login sites to open (default: qwen).
    _req = (payload or {}).get("providers")
    if isinstance(_req, list) and _req:
        _providers = [str(p).lower().strip() for p in _req if str(p).lower().strip() in _LOGIN_URLS]
    else:
        _single = str((payload or {}).get("provider", "qwen") or "qwen").lower().strip()
        _providers = [_single] if _single in _LOGIN_URLS else ["qwen"]
    if not _providers:
        _providers = ["qwen"]

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
    profile_path = _SYSTEM_DIR / profile_name
    profile_path.mkdir(parents=True, exist_ok=True)

    # Save user's browser choice (may be 'default', a path, or empty for auto-detect)
    chosen_browser = (payload or {}).get("browser_path", "")
    if chosen_browser:
        await asyncio.to_thread(_set_account_browser, profile_name, chosen_browser)

    # Resolve actual binary to use
    from engine.platform_paths import resolve_browser_for_profile
    resolved_browser = await asyncio.to_thread(resolve_browser_for_profile, profile_name)
    if not resolved_browser:
        raise HTTPException(status_code=500, detail="No browser found. Set one in settings.")

    # Fire-and-forget: raw subprocess, no Playwright.
    # No --password-store=basic / --use-mock-keychain: the engine launches this
    # profile with the real OS keyring, so account creation must too, or the
    # cookies written here are unreadable by the engine (and vice versa).
    cmd: list[str] = [resolved_browser, f"--user-data-dir={profile_path}"]
    cmd.extend(_LOGIN_URLS[p] for p in _providers)
    try:
        import subprocess
        subprocess.Popen(cmd, start_new_session=True)
    except Exception as e:
        logger.warning(f"Account creation browser for {profile_name} failed to start: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to launch browser: {e}")
    return {"status": "ok", "profile": profile_name,
            "providers": _providers, "count": len(_providers)}


@router.delete("/api/settings/accounts/delete")
async def delete_account(payload: dict[str, str]) -> dict[str, Any]:
    """Delete a browser-data profile directory (and its .bak if present)."""
    target_name = _validate_profile_name(payload.get("profile", ""))
    target_path = _SYSTEM_DIR / target_name
    if not target_path.is_dir():
        raise HTTPException(status_code=404, detail=f"'{target_name}' not found")
    # Block deleting a profile that is active for ANY provider.
    if target_name in _active_account_names():
        raise HTTPException(status_code=400, detail="Cannot delete an active profile. Switch first.")

    def _remove() -> None:
        shutil.rmtree(target_path)
        bak = _SYSTEM_DIR / f"{target_name}.bak"
        if bak.is_dir():
            shutil.rmtree(bak)

    await asyncio.to_thread(_remove)
    return {"status": "ok", "deleted": target_name}


@router.post("/api/settings/accounts/backup")
async def backup_account(payload: dict[str, str]) -> dict[str, Any]:
    """Backup browser-data-X to browser-data-X.bak (replaces existing .bak)."""
    target_name = _validate_profile_name(payload.get("profile", ""))
    target_path = _SYSTEM_DIR / target_name
    if not target_path.is_dir():
        raise HTTPException(status_code=404, detail=f"'{target_name}' not found")
    bak_path = _SYSTEM_DIR / f"{target_name}.bak"

    def _backup() -> None:
        if bak_path.exists():
            shutil.rmtree(bak_path)
        shutil.copytree(target_path, bak_path)

    await asyncio.to_thread(_backup)
    return {"status": "ok", "profile": target_name, "backup": f"{target_name}.bak"}


@router.post("/api/settings/accounts/restore")
async def restore_account(payload: dict[str, str]) -> dict[str, Any]:
    """Restore browser-data-X from browser-data-X.bak (replaces current data)."""
    target_name = _validate_profile_name(payload.get("profile", ""))
    bak_path = _SYSTEM_DIR / f"{target_name}.bak"
    if not bak_path.is_dir():
        raise HTTPException(status_code=404, detail=f"No backup found for '{target_name}'")
    target_path = _SYSTEM_DIR / target_name

    # Block restoring over a profile that is active for ANY provider.
    if target_name in _active_account_names():
        raise HTTPException(status_code=400, detail="Cannot restore over an active profile. Switch first.")

    def _restore() -> None:
        if target_path.exists():
            shutil.rmtree(target_path)
        shutil.copytree(bak_path, target_path)

    await asyncio.to_thread(_restore)
    return {"status": "ok", "profile": target_name}


@router.post("/api/settings/accounts/rename")
async def rename_account(payload: dict[str, str]) -> dict[str, Any]:
    """Set (or clear) the display label for a browser profile.

    The label is stored in accounts.json under the profile key. An empty label
    clears the custom name so the UI falls back to the detected email/name.
    """
    target_name = _validate_profile_name(payload.get("profile", ""))
    label = (payload.get("label") or "").strip()
    target_path = _SYSTEM_DIR / target_name
    if not target_path.is_dir():
        raise HTTPException(status_code=404, detail=f"'{target_name}' not found")

    def _apply() -> None:
        cfg = _read_accounts_config()
        if target_name not in cfg:
            cfg[target_name] = {}
        if label:
            cfg[target_name]["label"] = label
        else:
            cfg[target_name].pop("label", None)
        _write_accounts_config(cfg)

    await asyncio.to_thread(_apply)
    return {"status": "ok", "profile": target_name, "label": label}


@router.post("/api/settings/accounts/backup-all")
async def backup_all_accounts() -> dict[str, Any]:
    """Backup all browser-data-accN profiles."""
    def _backup_all() -> list[str]:
        backed_up: list[str] = []
        for entry in _SYSTEM_DIR.iterdir():
            if entry.is_dir() and re.match(r"^browser-data-acc\d+$", entry.name):
                bak_path = _SYSTEM_DIR / f"{entry.name}.bak"
                if bak_path.exists():
                    shutil.rmtree(bak_path)
                shutil.copytree(entry, bak_path)
                backed_up.append(entry.name)
        return backed_up

    backed_up = await asyncio.to_thread(_backup_all)
    return {"status": "ok", "backed_up": backed_up, "count": len(backed_up)}


@router.post("/api/settings/accounts/restore-all")
async def restore_all_accounts() -> dict[str, Any]:
    """Restore all browser-data-accN profiles from their .bak counterparts."""
    active_names = _active_account_names()

    def _restore_all() -> tuple[list[str], list[str]]:
        restored: list[str] = []
        skipped: list[str] = []
        for entry in _SYSTEM_DIR.iterdir():
            m = re.match(r"^(browser-data-acc\d+)\.bak$", entry.name)
            if entry.is_dir() and m:
                acc_name = m.group(1)
                if acc_name in active_names:
                    skipped.append(acc_name)
                    continue
                target_path = _SYSTEM_DIR / acc_name
                if target_path.exists():
                    shutil.rmtree(target_path)
                shutil.copytree(entry, target_path)
                restored.append(acc_name)
        return restored, skipped

    restored, skipped = await asyncio.to_thread(_restore_all)
    return {"status": "ok", "restored": restored, "skipped": skipped}


def _kill_browsers_for_profile(profile_path: Path) -> list[int]:
    """Kill any browser process using *profile_path* as its user-data-dir.

    Matches the exact `--user-data-dir=<path>` argument token, so:
      - it covers every Chromium-family binary (helium, chrome, chromium) —
        the old `chrom.*` pgrep never matched Sable's helium build at all;
      - `browser-data-acc1` can never match `browser-data-acc11`.

    Returns the list of PIDs that were killed.
    """
    import subprocess
    from engine.process_utils import kill_process_tree

    killed: list[int] = []
    # Chromium may have been launched with either the raw or the resolved
    # profile path; match BOTH so no holder slips through the kill.
    flags = {
        f"--user-data-dir={profile_path}",
        f"--user-data-dir={profile_path.resolve()}",
    }

    try:
        result = subprocess.run(
            ["ps", "-eo", "pid=,args="],
            capture_output=True,
            text=True,
            timeout=5,
        )
        lines = result.stdout.splitlines()
    except Exception:
        return killed

    for line in lines:
        pid_str, _, args = line.strip().partition(" ")
        if not pid_str.isdigit() or not args:
            continue
        # Exact token compare — a substring test would collide acc1 with acc11.
        tokens = set(args.split())
        if not flags & tokens:
            continue
        pid = int(pid_str)
        try:
            kill_process_tree(pid)
            killed.append(pid)
        except Exception:
            pass

    return killed


def _remove_singleton_locks(profile_path: Path) -> None:
    """Force-remove Chromium SingletonLock files so a new browser can start."""
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        lock = profile_path / name
        try:
            if lock.is_symlink() or lock.exists():
                lock.unlink()
        except OSError:
            pass


@router.post("/api/settings/accounts/open")
async def open_account_browser(payload: dict[str, str]) -> dict[str, Any]:
    """Launch a headful browser with the specified profile.

    Force-closes any Sable-managed browser (default or scoped) using this
    profile, kills leftover OS processes, removes stale lock files, and then
    opens a fresh headful window — regardless of current state.
    """
    target_name = _validate_profile_name(payload.get("profile", ""))
    target_path = _SYSTEM_DIR / target_name
    if not target_path.is_dir():
        raise HTTPException(status_code=404, detail=f"'{target_name}' not found")

    url = payload.get("url", "https://chat.qwen.ai")
    target_str = str(target_path)

    # 1. Release EVERY existing holder of this profile before opening a new one:
    #    Sable's default browser, any scoped browser on this dir, and a live
    #    DeepSeek ghost session. One profile = one writer. Two writers race on
    #    the Cookies DB and the login you just made gets silently overwritten.
    try:
        await service.close_profile(target_str)
        logger.info("Released Sable-managed browser(s) for %s", target_name)
    except Exception as exc:
        logger.warning("Failed to release Sable browser for %s: %s", target_name, exc)

    # 1a. Evict any ProfileHub (CDP ghost browser) bound to this dir. The hub
    #     owns a SEPARATE browser process that close_profile does not know
    #     about; if it survives, Chromium sees the SingletonLock and forwards
    #     the new launch to the existing (often headless) instance instead of
    #     opening a fresh headed window.
    try:
        from connectors.common.profile_hub import ProfileHub
        await ProfileHub.evict(target_path)
    except Exception as exc:
        logger.warning("Failed to evict ProfileHub for %s: %s", target_name, exc)

    try:
        from connectors.deepseek.ghost import GhostSession
        ghost = GhostSession._registry.get(target_name)
        if ghost is not None:
            await ghost.close()
            GhostSession._registry.pop(target_name, None)
            logger.info("Released DeepSeek ghost session for %s", target_name)
    except Exception as exc:
        logger.warning("Failed to release ghost session for %s: %s", target_name, exc)

    # 2. Kill any remaining OS-level browser processes using this profile
    killed_pids = await asyncio.to_thread(_kill_browsers_for_profile, target_path)
    if killed_pids:
        logger.info(f"Killed existing browser(s) for {target_name}: PIDs {killed_pids}")
        await asyncio.sleep(1.5)  # give OS time to release the lock

    # 3. Force-remove SingletonLock files that may survive process kills
    await asyncio.to_thread(_remove_singleton_locks, target_path)

    # Resolve browser using centralized resolver (respects accounts.json)
    from engine.platform_paths import resolve_browser_for_profile
    resolved_browser = await asyncio.to_thread(resolve_browser_for_profile, target_name)
    if not resolved_browser:
        raise HTTPException(status_code=500, detail="No browser found. Set one in settings.")

    # Fire-and-forget: raw subprocess, no Playwright.
    # NOTE: no --password-store=basic / --use-mock-keychain here. The engine /
    # scraper launches this SAME profile with the real OS keyring, so its
    # Cookies DB is keyring-encrypted. Launching with the mock store made
    # Chromium unable to decrypt those cookies -> the user got logged out on
    # every Open. Match the engine: use the default (real keyring) store.
    cmd: list[str] = [resolved_browser, f"--user-data-dir={target_path}", url]
    try:
        import subprocess
        subprocess.Popen(cmd, start_new_session=True)
    except Exception as e:
        logger.warning(f"Browser for {target_name} failed to start: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to launch browser: {e}")
    return {"status": "opened", "profile": target_name, "url": url}


_SERVICE_NAME = "sable.service"


# ── Qwen Chat History Cleanup ───────────────────────────────────────────────

@router.post("/api/settings/accounts/clear-history")
async def clear_account_history(payload: dict[str, str]) -> dict[str, Any]:
    """Delete all Qwen chat history for a specific browser profile."""
    from engine.qwen_cleanup import delete_all_chats_for_profile

    profile = payload.get("profile", "")
    if not re.match(r"^browser-data-acc\d+$", profile):
        raise HTTPException(status_code=400, detail="Invalid profile name")

    async def _do_clear() -> None:
        result = await delete_all_chats_for_profile(profile)
        if not result["success"]:
            logger.warning("Clear history failed for %s: %s", profile, result.get("error"))

    _spawn_bg(_do_clear())
    return {"status": "started", "profile": profile}


@router.post("/api/settings/accounts/clear-history-all")
async def clear_all_accounts_history() -> dict[str, Any]:
    """Delete all Qwen chat history for every browser profile."""
    from engine.qwen_cleanup import delete_all_chats_all_profiles

    async def _do_clear_all() -> None:
        results = await delete_all_chats_all_profiles()
        succeeded = sum(1 for r in results if r["success"])
        logger.info("Bulk clear complete: %d/%d succeeded", succeeded, len(results))

    _spawn_bg(_do_clear_all())
    return {"status": "started"}


@router.get("/api/settings/accounts/clear-history-status")
async def get_clear_history_status() -> dict[str, Any]:
    """Return last_cleared timestamps for all accounts."""
    from engine.qwen_cleanup import get_last_cleared, _load_accounts

    accounts = _load_accounts()
    status = {}
    for key in sorted(accounts.keys()):
        if key.startswith("browser-data-acc"):
            lc = accounts[key].get("last_cleared")
            status[key] = {"last_cleared": lc}
    return {"accounts": status}





@router.post("/api/settings/service/stop")
async def stop_service_endpoint() -> dict[str, str]:
    from engine.service_manager import stop_service as _stop
    _stop()
    return {"status": "stopping"}


@router.post("/api/settings/service/restart")
async def restart_service_endpoint() -> dict[str, str]:
    from engine.service_manager import restart_service as _restart
    _restart()
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
            if not entry.is_dir():
                continue
            # Skip .bak directories — they're backups, not live profiles
            if entry.name.endswith(".bak"):
                continue
            if entry.name.startswith("browser-data-acc") or entry.name in (
                "browser-scraper-data",
                "automation-browser-data",
            ):
                try:
                    results.append(_strip_one_profile(entry))
                except Exception as exc:
                    logger.warning("Failed to strip profile %s: %s", entry.name, exc)
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


# ---------------------------------------------------------------------------
# Data Export / Import (full project backup to ~/.sable/backup/)
# ---------------------------------------------------------------------------

_EXPORT_DIRS = ["system", "output", "instruction", "Brain", ".sable_backups"]
_BACKUP_ROOT = Path.home() / ".sable" / "backup"

# Skip massive browser profile dirs inside system/ during export
_SKIP_PATTERNS = ("browser-data", "browser-scraper-data", "automation-browser-data",
                  "component_crx_cache", "extensions_crx_cache", "GPUPersistentCache",
                  "GraphiteDawnCache")


def _copy_dir_filtered(src: Path, dst: Path, skip_patterns: tuple[str, ...] = ()) -> None:
    """Copy a directory tree, skipping entries matching skip_patterns."""
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        if any(item.name.startswith(p) for p in skip_patterns):
            continue
        target = dst / item.name
        if item.is_dir():
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(item, target, symlinks=True)
        else:
            shutil.copy2(item, target)


@router.post("/api/settings/data/export")
async def export_data() -> StreamingResponse:
    """Export project data dirs to ~/.sable/backup/ with streaming progress."""

    async def _stream() -> AsyncGenerator[str, None]:
        _BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
        exported: dict[str, str] = {}
        errors: list[str] = []
        total = len(_EXPORT_DIRS)

        for i, dirname in enumerate(_EXPORT_DIRS, 1):
            yield json.dumps({"type": "progress", "dir": dirname, "step": i, "total": total, "status": "copying"}) + "\n"
            src = BASE_DIR / dirname
            if not src.exists():
                errors.append(f"{dirname}/ not found")
                yield json.dumps({"type": "progress", "dir": dirname, "step": i, "total": total, "status": "skipped"}) + "\n"
                continue
            dst = _BACKUP_ROOT / dirname
            try:
                def _copy() -> None:
                    if dirname == "system":
                        if dst.exists():
                            shutil.rmtree(dst)
                        _copy_dir_filtered(src, dst, _SKIP_PATTERNS)
                    else:
                        if dst.exists():
                            shutil.rmtree(dst)
                        shutil.copytree(src, dst, symlinks=True)

                await asyncio.to_thread(_copy)
                exported[dirname] = str(dst)
                yield json.dumps({"type": "progress", "dir": dirname, "step": i, "total": total, "status": "done"}) + "\n"
            except Exception as exc:
                errors.append(f"{dirname}: {exc}")
                yield json.dumps({"type": "progress", "dir": dirname, "step": i, "total": total, "status": "error", "error": str(exc)}) + "\n"

        yield json.dumps({"type": "done", "exported": exported, "errors": errors, "backup_root": str(_BACKUP_ROOT)}) + "\n"

    return StreamingResponse(_stream(), media_type="application/x-ndjson")


@router.post("/api/settings/data/import")
async def import_data() -> StreamingResponse:
    """Import data from ~/.sable/backup/ back into the project with streaming progress."""

    async def _stream() -> AsyncGenerator[str, None]:
        if not _BACKUP_ROOT.exists():
            yield json.dumps({"type": "error", "detail": f"No backup found at {_BACKUP_ROOT}"}) + "\n"
            return

        imported: list[str] = []
        errors: list[str] = []
        total = len(_EXPORT_DIRS)

        for i, dirname in enumerate(_EXPORT_DIRS, 1):
            src = _BACKUP_ROOT / dirname
            if not src.exists():
                continue
            yield json.dumps({"type": "progress", "dir": dirname, "step": i, "total": total, "status": "restoring"}) + "\n"
            dst = BASE_DIR / dirname
            try:
                def _restore() -> None:
                    if dirname == "system":
                        _copy_dir_filtered(src, dst)
                    else:
                        if dst.exists():
                            shutil.rmtree(dst)
                        shutil.copytree(src, dst, symlinks=True)

                await asyncio.to_thread(_restore)
                imported.append(dirname)
                yield json.dumps({"type": "progress", "dir": dirname, "step": i, "total": total, "status": "done"}) + "\n"
            except Exception as exc:
                errors.append(f"{dirname}: {exc}")
                yield json.dumps({"type": "progress", "dir": dirname, "step": i, "total": total, "status": "error", "error": str(exc)}) + "\n"

        yield json.dumps({"type": "done", "imported": imported, "errors": errors}) + "\n"

    return StreamingResponse(_stream(), media_type="application/x-ndjson")


# ── Context Pass Settings ──────────────────────────────────────────
_CONTEXT_PASS_SETTINGS_PATH = BASE_DIR / "system/context_pass_settings.json"
_CONTEXT_PASS_DEFAULTS: dict[str, Any] = {
    "summarizer_model": "",   # primary model (empty = use current model)
    "fallback_models": [],    # ordered list of fallback model IDs (max 2)
}

def _load_context_pass_settings() -> dict[str, Any]:
    settings = dict(_CONTEXT_PASS_DEFAULTS)
    if _CONTEXT_PASS_SETTINGS_PATH.exists():
        try:
            with open(_CONTEXT_PASS_SETTINGS_PATH, "r", encoding="utf-8") as f:
                stored = json.load(f)
            if isinstance(stored, dict):
                settings.update(stored)
        except Exception:
            pass
    return settings

def _save_context_pass_settings(settings: dict[str, Any]) -> None:
    _CONTEXT_PASS_SETTINGS_PATH.write_text(json.dumps(settings, indent=2), encoding="utf-8")

@router.get("/api/settings/context-pass")
async def get_context_pass_settings() -> dict[str, Any]:
    return _load_context_pass_settings()

@router.post("/api/settings/context-pass")
async def set_context_pass_settings(request: Request) -> dict[str, Any]:
    body = await request.json()
    settings = _load_context_pass_settings()
    if "summarizer_model" in body:
        settings["summarizer_model"] = str(body["summarizer_model"]).strip()
    if "fallback_models" in body:
        fm = body["fallback_models"]
        settings["fallback_models"] = [str(m).strip() for m in fm if isinstance(fm, list)] if isinstance(fm, list) else []
    _save_context_pass_settings(settings)
    return {"status": "ok", **settings}
# ── /Context Pass Settings ─────────────────────────────────────────

# ── Memory Consolidation Settings ─────────────────────────────────────
_CONSOLIDATION_SETTINGS_PATH = BASE_DIR / "system/consolidation_settings.json"
_CONSOLIDATION_DEFAULTS: dict[str, Any] = {
    "model": "",                    # empty = use current chat model
    "fallback_models": [],          # ordered list of fallback model IDs
}

def _load_consolidation_settings() -> dict[str, Any]:
    settings = dict(_CONSOLIDATION_DEFAULTS)
    if _CONSOLIDATION_SETTINGS_PATH.exists():
        try:
            with open(_CONSOLIDATION_SETTINGS_PATH, "r", encoding="utf-8") as f:
                stored = json.load(f)
            if isinstance(stored, dict):
                settings.update(stored)
        except Exception:
            pass
    return settings

def _save_consolidation_settings(settings: dict[str, Any]) -> None:
    _CONSOLIDATION_SETTINGS_PATH.write_text(json.dumps(settings, indent=2), encoding="utf-8")

@router.get("/api/settings/consolidation")
async def get_consolidation_settings() -> dict[str, Any]:
    return _load_consolidation_settings()

@router.post("/api/settings/consolidation")
async def set_consolidation_settings(request: Request) -> dict[str, Any]:
    body = await request.json()
    settings = _load_consolidation_settings()
    if "model" in body:
        settings["model"] = str(body["model"]).strip()
    if "fallback_models" in body:
        fm = body["fallback_models"]
        settings["fallback_models"] = [str(m).strip() for m in fm if isinstance(fm, list)] if isinstance(fm, list) else []
    _save_consolidation_settings(settings)
    return {"status": "ok", **settings}
# ── /Memory Consolidation Settings ────────────────────────────────────

# ── TTS Model Management ────────────────────────────────────────────
_TTS_DIR = _SYSTEM_DIR / "models" / "tts"
_TTS_FILES = {
    "kokoro-v1.0.onnx": {
        "url": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx",
        "size": 325525180,
        "label": "Kokoro v1.0 Model (f32)",
    },
    "voices-v1.0.bin": {
        "url": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin",
        "size": 28205511,
        "label": "Voice Embeddings (54 voices)",
    },
}


def _tts_status() -> dict[str, Any]:
    files = {}
    for name, meta in _TTS_FILES.items():
        path = _TTS_DIR / name
        if path.exists():
            actual = path.stat().st_size
            files[name] = {
                "label": meta["label"],
                "installed": actual >= meta["size"] * 0.95,
                "size": actual,
                "expected": meta["size"],
            }
        else:
            files[name] = {
                "label": meta["label"],
                "installed": False,
                "size": 0,
                "expected": meta["size"],
            }
    all_installed = all(f["installed"] for f in files.values())
    return {"installed": all_installed, "dir": str(_TTS_DIR), "files": files}


@router.get("/api/settings/tts")
async def get_tts_status() -> dict[str, Any]:
    return _tts_status()


@router.post("/api/settings/tts/download")
async def download_tts_models(request: Request) -> StreamingResponse:
    import urllib.request

    _TTS_DIR.mkdir(parents=True, exist_ok=True)

    async def _stream() -> AsyncGenerator[str, None]:
        for name, meta in _TTS_FILES.items():
            path = _TTS_DIR / name
            if path.exists() and path.stat().st_size >= meta["size"] * 0.95:
                yield json.dumps({"file": name, "status": "skip", "reason": "already installed"}) + "\n"
                continue

            yield json.dumps({"file": name, "status": "start", "total": meta["size"]}) + "\n"
            try:
                tmp = path.with_suffix(".part")
                req = urllib.request.Request(meta["url"], headers={"User-Agent": "Sable/1.0"})
                with urllib.request.urlopen(req, timeout=300) as resp:
                    downloaded = 0
                    with open(tmp, "wb") as f:
                        while True:
                            if await request.is_disconnected():
                                tmp.unlink(missing_ok=True)
                                yield json.dumps({"file": name, "status": "cancelled"}) + "\n"
                                return
                            chunk = resp.read(1024 * 256)
                            if not chunk:
                                break
                            f.write(chunk)
                            downloaded += len(chunk)
                            yield json.dumps({"file": name, "status": "progress", "downloaded": downloaded, "total": meta["size"]}) + "\n"
                            await asyncio.sleep(0)
                tmp.rename(path)
                yield json.dumps({"file": name, "status": "done", "size": downloaded}) + "\n"
            except Exception as e:
                if tmp.exists():
                    tmp.unlink()
                yield json.dumps({"file": name, "status": "error", "error": str(e)}) + "\n"

        yield json.dumps({"status": "complete"}) + "\n"

    return StreamingResponse(_stream(), media_type="application/x-ndjson")


@router.delete("/api/settings/tts")
async def delete_tts_models() -> dict[str, Any]:
    removed = []
    for name in _TTS_FILES:
        path = _TTS_DIR / name
        if path.exists():
            path.unlink()
            removed.append(name)
    return {"status": "ok", "removed": removed}

# ── TTS Preferences ──────────────────────────────────────────────────
_TTS_PREFS_PATH = BASE_DIR / "system/tts_prefs.json"
_TTS_PREFS_DEFAULTS: dict[str, Any] = {
    "provider": "kokoro",  # "kokoro" | "edge"
    "voice": "af_bella",
    "edge_voice": "en-US-AvaMultilingualNeural",
    "speed": 1.0,
}


def _load_tts_prefs() -> dict[str, Any]:
    prefs = dict(_TTS_PREFS_DEFAULTS)
    if _TTS_PREFS_PATH.exists():
        try:
            with open(_TTS_PREFS_PATH, "r", encoding="utf-8") as f:
                stored = json.load(f)
            if isinstance(stored, dict):
                prefs.update(stored)
        except Exception:
            pass
    return prefs


def _save_tts_prefs(prefs: dict[str, Any]) -> None:
    _TTS_PREFS_PATH.write_text(json.dumps(prefs, indent=2), encoding="utf-8")


@router.get("/api/settings/tts/prefs")
async def get_tts_prefs() -> dict[str, Any]:
    return _load_tts_prefs()


@router.post("/api/settings/tts/prefs")
async def set_tts_prefs(request: Request) -> dict[str, Any]:
    body = await request.json()
    prefs = _load_tts_prefs()
    if "provider" in body:
        p = str(body["provider"]).strip().lower()
        if p in ("kokoro", "edge"):
            prefs["provider"] = p
    if "voice" in body:
        prefs["voice"] = str(body["voice"]).strip()
    if "edge_voice" in body:
        prefs["edge_voice"] = str(body["edge_voice"]).strip()
    if "speed" in body:
        try:
            prefs["speed"] = round(float(body["speed"]), 2)
        except (ValueError, TypeError):
            pass
    _save_tts_prefs(prefs)
    return {"status": "ok", **prefs}
# ── /TTS Preferences ─────────────────────────────────────────────────


# ── TTS Synthesis ────────────────────────────────────────────────────
_kokoro_instance = None


def _get_kokoro():
    """Lazy-load Kokoro model (singleton)."""
    global _kokoro_instance
    if _kokoro_instance is not None:
        return _kokoro_instance
    from kokoro_onnx import Kokoro
    model_path = _TTS_DIR / "kokoro-v1.0.onnx"
    voices_path = _TTS_DIR / "voices-v1.0.bin"
    if not model_path.exists() or not voices_path.exists():
        raise HTTPException(status_code=400, detail="TTS models not installed")
    _kokoro_instance = Kokoro(str(model_path), str(voices_path))
    return _kokoro_instance


@router.post("/api/tts/synthesize")
async def tts_synthesize(request: Request) -> Response:
    """Synthesize text to speech. Returns WAV (kokoro) or MP3 (edge)."""
    import io

    body = await request.json()
    text = (body.get("text") or "").strip()
    prefs = _load_tts_prefs()
    provider = body.get("provider") or prefs.get("provider", "kokoro")
    speed = body.get("speed") if body.get("speed") is not None else prefs.get("speed", 1.0)

    if not text:
        raise HTTPException(status_code=400, detail="Empty text")
    if len(text) > 5000:
        raise HTTPException(status_code=400, detail="Text too long (max 5000 chars)")

    # Track TTS output for echo cancellation
    session_id = request.headers.get("X-Session-Id", "")
    if session_id:
        record_tts_output(session_id, text)

    if provider == "edge":
        return await _synthesize_edge(text, body, prefs, speed)

    # Kokoro (default)
    import soundfile as sf
    voice = body.get("voice") or prefs.get("voice", "af_bella")
    try:
        kokoro = _get_kokoro()
        samples, sr = kokoro.create(text, voice=voice, speed=speed)
    except Exception as e:
        logger.error(f"Kokoro TTS synthesis failed: {e}")
        raise HTTPException(status_code=500, detail=f"Synthesis failed: {e}")

    buf = io.BytesIO()
    sf.write(buf, samples, sr, format="WAV")
    buf.seek(0)
    return Response(content=buf.read(), media_type="audio/wav")


async def _synthesize_edge(
    text: str,
    body: dict[str, Any],
    prefs: dict[str, Any],
    speed: float,
) -> Response:
    """Synthesize via Edge TTS. Returns MP3 audio."""
    import io
    import edge_tts

    voice = body.get("voice") or body.get("edge_voice") or prefs.get("edge_voice", "en-US-AvaMultilingualNeural")
    # Edge TTS rate format: "+20%", "-10%", etc. Convert from multiplier.
    rate_pct = int((speed - 1.0) * 100)
    rate_str = f"+{rate_pct}%" if rate_pct >= 0 else f"{rate_pct}%"

    try:
        communicate = edge_tts.Communicate(text, voice, rate=rate_str)
        buf = io.BytesIO()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                buf.write(chunk["data"])
        buf.seek(0)
        return Response(content=buf.read(), media_type="audio/mpeg")
    except Exception as e:
        logger.error(f"Edge TTS synthesis failed: {e}")
        raise HTTPException(status_code=500, detail=f"Edge TTS failed: {e}")


_edge_voices_cache: list[dict[str, str]] | None = None


async def _get_edge_voices() -> list[dict[str, str]]:
    """Fetch Edge TTS voices (cached after first call)."""
    global _edge_voices_cache
    if _edge_voices_cache is not None:
        return _edge_voices_cache
    import edge_tts
    raw = await edge_tts.list_voices()
    # Return simplified list: {ShortName, Locale, Gender}
    _edge_voices_cache = [
        {
            "id": v["ShortName"],
            "locale": v.get("Locale", ""),
            "gender": v.get("Gender", ""),
        }
        for v in raw
    ]
    return _edge_voices_cache


@router.get("/api/tts/voices")
async def tts_voices(provider: str = "kokoro") -> dict[str, Any]:
    """List available TTS voices for the given provider."""
    if provider == "edge":
        try:
            voices = await _get_edge_voices()
            return {"voices": voices, "provider": "edge"}
        except Exception as e:
            logger.error(f"Edge TTS voice listing failed: {e}")
            return {"voices": [], "provider": "edge", "error": str(e)}

    # Kokoro (default)
    try:
        kokoro = _get_kokoro()
        if hasattr(kokoro, "voices"):
            voices = sorted(kokoro.voices.keys())
        elif hasattr(kokoro, "get_voices"):
            voices = sorted(kokoro.get_voices())
        else:
            voices = []
        return {"voices": voices, "provider": "kokoro"}
    except HTTPException:
        return {"voices": [], "provider": "kokoro", "error": "TTS models not installed"}
# ── /TTS Synthesis ───────────────────────────────────────────────────


# ---------------------------------------------------------------------------
# Search provider settings (Phase 2 — multi-provider migration)
# ---------------------------------------------------------------------------

_ALLOWED_SEARCH_PROVIDERS = {
    "searxng", "brave", "duckduckgo", "google_pse", "tavily", "serper", "disabled",
}
_ALLOWED_SAFESEARCH = {"strict", "moderate", "off"}
_SEARCH_KEY_FIELDS = ("brave_api_key", "google_pse_key", "google_pse_cx", "tavily_api_key", "serper_api_key")

def _read_system_settings() -> dict[str, Any]:
    """Read system/settings.json, returning empty dict on failure."""
    path = _SYSTEM_DIR / "settings.json"
    if not path.is_file():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        logger.warning("Failed to read system settings: %s", exc)
        return {}

def _write_system_settings(data: dict[str, Any]) -> None:
    """Atomically write system/settings.json."""
    path = _SYSTEM_DIR / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    tmp.replace(path)

@router.get("/api/settings/search")
async def get_search_settings() -> dict[str, Any]:
    """Return current search config with key-presence booleans (never actual keys)."""
    from engine.search import get_search_config as _get_cfg

    cfg = _get_cfg()
    settings = _read_system_settings()

    provider = cfg.get("active_provider", settings.get("search_provider", "searxng"))
    return {
        "search_provider": provider,
        "search_url": cfg.get("search_url", settings.get("search_url", "")),
        "search_result_count": cfg.get("result_count", int(settings.get("search_result_count", 5))),
        "search_safesearch": settings.get("search_safesearch", "strict"),
        "search_fallback_chain": settings.get("search_fallback_chain", ["duckduckgo"]),
        "has_brave_key": bool(settings.get("brave_api_key")),
        "has_google_pse_key": bool(settings.get("google_pse_key")),
        "has_google_pse_cx": bool(settings.get("google_pse_cx")),
        "has_tavily_key": bool(settings.get("tavily_api_key")),
        "tavily_key_count": len(settings["tavily_api_key"]) if isinstance(settings.get("tavily_api_key"), list) else (1 if settings.get("tavily_api_key") else 0),
        "has_serper_key": bool(settings.get("serper_api_key")),
        "serper_key_count": len(settings["serper_api_key"]) if isinstance(settings.get("serper_api_key"), list) else (1 if settings.get("serper_api_key") else 0),
    }

@router.post("/api/settings/search")
async def update_search_settings(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate and persist search configuration. Empty API keys are ignored."""
    provider = payload.get("search_provider")
    if provider is not None and provider not in _ALLOWED_SEARCH_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid search_provider '{provider}'. Allowed: {sorted(_ALLOWED_SEARCH_PROVIDERS)}",
        )

    result_count = payload.get("search_result_count")
    if result_count is not None:
        try:
            result_count = int(result_count)
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="search_result_count must be an integer")
        if not (1 <= result_count <= 20):
            raise HTTPException(status_code=400, detail="search_result_count must be between 1 and 20")

    safesearch = payload.get("search_safesearch")
    if safesearch is not None and safesearch not in _ALLOWED_SAFESEARCH:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid search_safesearch '{safesearch}'. Allowed: {sorted(_ALLOWED_SAFESEARCH)}",
        )

    settings = _read_system_settings()

    scalar_fields = {
        "search_provider": provider,
        "search_url": payload.get("search_url"),
        "search_result_count": result_count,
        "search_safesearch": safesearch,
    }
    for key, val in scalar_fields.items():
        if val is not None:
            settings[key] = val

    if "search_fallback_chain" in payload:
        chain = payload["search_fallback_chain"]
        if isinstance(chain, list):
            settings["search_fallback_chain"] = chain

    for field in _SEARCH_KEY_FIELDS:
        val = payload.get(field)
        if val is not None:
            # Accept string (single key) or list of strings (multi-key)
            if isinstance(val, list):
                cleaned = [str(v).strip() for v in val if isinstance(v, str) and str(v).strip()]
                if cleaned:
                    settings[field] = cleaned if len(cleaned) > 1 else cleaned[0]
                else:
                    settings.pop(field, None)
            elif isinstance(val, str) and val.strip():
                settings[field] = val.strip()

    _write_system_settings(settings)

    from engine.search import update_search_config as _update_cfg
    _update_cfg(primary_provider=settings.get("search_provider", "searxng"))

    logger.info("Search settings updated: provider=%s", settings.get("search_provider"))
    return {"status": "ok", **{k: v for k, v in settings.items() if k not in _SEARCH_KEY_FIELDS}}

_ALL_SEARCH_PROVIDERS = ["searxng", "duckduckgo", "brave", "google_pse", "tavily", "serper"]


@router.get("/api/settings/search/providers")
async def list_search_providers() -> dict[str, Any]:
    """Return all available search provider names."""
    return {"providers": _ALL_SEARCH_PROVIDERS}


@router.post("/api/settings/search/test")
async def test_search(request: Request) -> dict[str, Any]:
    """Run a real test query against a specific or default search provider."""
    import time as _time
    from engine.search.core import _call_provider, searxng_search_results
    from engine.search.config import _get_search_settings

    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    query = (body.get("query") or "").strip() or "SearXNG test query"
    count = min(int(body.get("count") or 5), 30)
    provider_override = (body.get("provider") or "").strip()

    settings = _get_search_settings()
    provider = provider_override or settings.get("search_provider", "searxng")

    start = _time.monotonic()
    loop = asyncio.get_running_loop()
    try:
        if provider_override:
            results = await loop.run_in_executor(None, _call_provider, provider, query, count)
        else:
            results = await loop.run_in_executor(
                None, lambda: searxng_search_results(query, count=count)
            )
        elapsed = round(_time.monotonic() - start, 3)
        return {
            "success": True,
            "query": query,
            "provider_used": provider,
            "result_count": len(results),
            "elapsed_s": elapsed,
            "results": [{"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", r.get("snippet", ""))[:200]} for r in results[:count]],
            "error": None,
        }
    except Exception as exc:
        elapsed = round(_time.monotonic() - start, 3)
        logger.error("Search test failed (%s): %s", provider, exc)
        return {
            "success": False,
            "query": query,
            "provider_used": provider,
            "result_count": 0,
            "elapsed_s": elapsed,
            "results": [],
            "error": str(exc),
        }


@router.post("/api/settings/search/compare")
async def compare_search(request: Request) -> dict[str, Any]:
    """Run the same query against two providers side-by-side."""
    import time as _time
    from engine.search.core import _call_provider

    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    query = (body.get("query") or "").strip() or "comparison test"
    count = min(int(body.get("count") or 5), 30)
    prov_a = (body.get("provider_a") or "searxng").strip()
    prov_b = (body.get("provider_b") or "duckduckgo").strip()

    loop = asyncio.get_running_loop()

    async def _run_one(provider: str) -> dict:
        start = _time.monotonic()
        try:
            results = await loop.run_in_executor(None, _call_provider, provider, query, count)
            elapsed = round(_time.monotonic() - start, 3)
            return {
                "provider": provider,
                "success": True,
                "result_count": len(results),
                "elapsed_s": elapsed,
                "results": [{"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", r.get("snippet", ""))[:200]} for r in results[:count]],
                "error": None,
            }
        except Exception as exc:
            elapsed = round(_time.monotonic() - start, 3)
            return {
                "provider": provider,
                "success": False,
                "result_count": 0,
                "elapsed_s": elapsed,
                "results": [],
                "error": str(exc),
            }

    res_a, res_b = await asyncio.gather(_run_one(prov_a), _run_one(prov_b))
    return {"query": query, "count": count, "a": res_a, "b": res_b}

@router.post("/api/settings/search/cache/clear")
async def clear_search_cache() -> dict[str, Any]:
    """Clear all cached search results."""
    from engine.search import invalidate_cache

    invalidate_cache()
    logger.info("Search cache cleared via API")
    return {"cleared": True}


# ---------------------------------------------------------------------------
# Search Provider Multi-Key Management (Tavily, Serper, Brave, Google PSE)
# ---------------------------------------------------------------------------

_SEARCH_KEY_MAP = {
    "tavily": "tavily_api_key",
    "serper": "serper_api_key",
    "brave": "brave_api_key",
    "google_pse": "google_pse_key",
}

_SEARCH_KEY_PLACEHOLDERS = {
    "tavily": "tvly-…",
    "serper": "serper-…",
    "brave": "BSA…",
    "google_pse": "AIza…",
}

def _mask_search_key(key: str) -> str:
    if len(key) <= 8:
        return key[:2] + "•" * (len(key) - 2)
    return key[:4] + "•" * (len(key) - 8) + key[-4:]

def _get_search_keys_list(provider: str) -> list[str]:
    """Get keys for a search provider as a list (handles legacy single-string)."""
    settings = _read_system_settings()
    field = _SEARCH_KEY_MAP.get(provider)
    if not field:
        return []
    raw = settings.get(field, "")
    if isinstance(raw, list):
        return [k for k in raw if isinstance(k, str) and k.strip()]
    elif isinstance(raw, str) and raw.strip():
        return [raw.strip()]
    return []

def _save_search_keys_list(provider: str, keys: list[str]) -> None:
    """Save keys for a search provider (single string if 1 key, list if >1)."""
    settings = _read_system_settings()
    field = _SEARCH_KEY_MAP.get(provider)
    if not field:
        return
    cleaned = [k.strip() for k in keys if k.strip()]
    if len(cleaned) == 0:
        settings.pop(field, None)
    elif len(cleaned) == 1:
        settings[field] = cleaned[0]
    else:
        settings[field] = cleaned
    _write_system_settings(settings)

@router.get("/api/settings/search/{provider}/keys")
async def list_search_provider_keys(provider: str) -> dict[str, Any]:
    """List all configured keys for a search provider (masked)."""
    if provider not in _SEARCH_KEY_MAP:
        raise HTTPException(status_code=404, detail=f"Unknown search provider '{provider}'")
    keys = _get_search_keys_list(provider)
    masked = [
        {"index": i, "masked": _mask_search_key(k), "active": i == 0}
        for i, k in enumerate(keys)
    ]
    return {"keys": masked, "available": len(keys) > 0}

@router.post("/api/settings/search/{provider}/api-key")
async def add_search_provider_key(provider: str, request: Request) -> dict[str, Any]:
    """Add a key to a search provider's pool."""
    if provider not in _SEARCH_KEY_MAP:
        raise HTTPException(status_code=404, detail=f"Unknown search provider '{provider}'")
    body = await request.json()
    key = body.get("api_key", "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="Missing 'api_key' field")
    keys = _get_search_keys_list(provider)
    if key in keys:
        raise HTTPException(status_code=409, detail="Key already exists")
    keys.append(key)
    _save_search_keys_list(provider, keys)
    # Invalidate search cache so new key is picked up
    from engine.search import invalidate_cache
    invalidate_cache()
    logger.info("Added %s search key (total: %d)", provider, len(keys))
    return {"status": "ok", "keys": [{"index": i, "masked": _mask_search_key(k), "active": i == 0} for i, k in enumerate(keys)], "available": True}

@router.delete("/api/settings/search/{provider}/api-key/{index}")
async def remove_search_provider_key(provider: str, index: int) -> dict[str, Any]:
    """Remove a key from a search provider's pool by index."""
    if provider not in _SEARCH_KEY_MAP:
        raise HTTPException(status_code=404, detail=f"Unknown search provider '{provider}'")
    keys = _get_search_keys_list(provider)
    if index < 0 or index >= len(keys):
        raise HTTPException(status_code=404, detail="Key not found at that index")
    keys.pop(index)
    _save_search_keys_list(provider, keys)
    from engine.search import invalidate_cache
    invalidate_cache()
    logger.info("Removed %s search key at index %d (remaining: %d)", provider, index, len(keys))
    return {"status": "ok", "keys": [{"index": i, "masked": _mask_search_key(k), "active": i == 0} for i, k in enumerate(keys)], "available": len(keys) > 0}


# ─── General Settings (tool output limit, etc.) ─────────────────────────────

@router.get("/api/settings/general")
async def get_general_settings() -> dict[str, Any]:
    """Return general app settings."""
    from engine.notifications.policy import CATEGORY_META, DEFAULT_CATEGORIES
    settings = _read_system_settings()
    cats = settings.get("notification_categories") or {}
    return {
        "max_tool_output_chars": settings.get("max_tool_output_chars", 100_000),
        "desktop_notifications": settings.get("desktop_notifications", True),
        "notification_categories": {
            k: bool(cats.get(k, DEFAULT_CATEGORIES.get(k, True))) for k in CATEGORY_META
        },
    }


# ── Voice Filters ─────────────────────────────────────────────────────
import re
from difflib import SequenceMatcher

_WHISPER_HALLUCINATION_PHRASES = [
    "thank you for watching",
    "please subscribe",
    "like and subscribe",
    "don't forget to subscribe",
    "thanks for watching",
    "see you in the next video",
    "in this video",
    "welcome back to my channel",
    "hit that subscribe button",
    "smash that like button",
    "link in the description",
    "check out the link",
    "let me know in the comments",
    "drop a comment below",
    "you're listening to",
    "this has been",
    "stay tuned",
    "up next",
]

_REPEATED_WORD_THRESHOLD = 0.6  # fraction of words that are the same token


def is_whisper_hallucination(text: str) -> bool:
    """Detect known Whisper failure modes."""
    if not text or not text.strip():
        return True
    lower = text.lower().strip()
    for phrase in _WHISPER_HALLUCINATION_PHRASES:
        if phrase in lower:
            return True
    # Repeated word/phrase detection: "the the the the"
    words = lower.split()
    if len(words) >= 4:
        unique_ratio = len(set(words)) / len(words)
        if unique_ratio < (1 - _REPEATED_WORD_THRESHOLD):
            return True
    # All punctuation / no real content
    alpha_chars = sum(1 for c in lower if c.isalpha())
    if alpha_chars < 3:
        return True
    return False


# Per-session TTS echo tracking: {session_id: [recent_tts_texts]}
_tts_echo_history: dict[str, list[str]] = {}
_TTS_ECHO_MAX_HISTORY = 5
_TTS_ECHO_SIMILARITY_THRESHOLD = 0.75


def record_tts_output(session_id: str, text: str) -> None:
    """Track recent TTS output for echo cancellation."""
    if session_id not in _tts_echo_history:
        _tts_echo_history[session_id] = []
    history = _tts_echo_history[session_id]
    normalized = re.sub(r'[^a-z0-9\s]', '', text.lower()).strip()
    if normalized:
        history.append(normalized)
        if len(history) > _TTS_ECHO_MAX_HISTORY:
            history.pop(0)


def is_tts_echo(transcript: str, session_id: str) -> bool:
    """Check if transcript matches recent TTS output (mic picked up speaker)."""
    if session_id not in _tts_echo_history:
        return False
    normalized = re.sub(r'[^a-z0-9\s]', '', transcript.lower()).strip()
    if not normalized or len(normalized) < 10:
        return False
    for prev in _tts_echo_history[session_id]:
        ratio = SequenceMatcher(None, normalized, prev).ratio()
        if ratio >= _TTS_ECHO_SIMILARITY_THRESHOLD:
            return True
    return False


# ── STT (Speech-to-Text) ───────────────────────────────────────────────
_STT_DIR = _SYSTEM_DIR / "models" / "stt"
_STT_HF_REPO = "Systran/faster-whisper-small.en"
_STT_FILES = {
    "model.bin": {
        "url": f"https://huggingface.co/{_STT_HF_REPO}/resolve/main/model.bin",
        "size": 483_545_366,
        "label": "Whisper small.en Model (CTranslate2)",
    },
    "config.json": {
        "url": f"https://huggingface.co/{_STT_HF_REPO}/resolve/main/config.json",
        "size": 2_657,
        "label": "Model Config",
    },
    "tokenizer.json": {
        "url": f"https://huggingface.co/{_STT_HF_REPO}/resolve/main/tokenizer.json",
        "size": 2_128_466,
        "label": "Tokenizer",
    },
    "vocabulary.txt": {
        "url": f"https://huggingface.co/{_STT_HF_REPO}/resolve/main/vocabulary.txt",
        "size": 422_309,
        "label": "Vocabulary",
    },
}
_STT_PREFS_PATH = _SYSTEM_DIR / "stt_prefs.json"
_STT_PREFS_DEFAULTS: dict[str, Any] = {
    "model": "small.en",
    "device": "cpu",
    "compute_type": "int8",
    "language": "en",
    "beam_size": 5,
}
_whisper_model = None


def _stt_status() -> dict[str, Any]:
    files = {}
    for name, meta in _STT_FILES.items():
        path = _STT_DIR / name
        if path.exists():
            actual = path.stat().st_size
            files[name] = {
                "label": meta["label"],
                "installed": actual >= meta["size"] * 0.90,
                "size": actual,
                "expected": meta["size"],
            }
        else:
            files[name] = {
                "label": meta["label"],
                "installed": False,
                "size": 0,
                "expected": meta["size"],
            }
    all_installed = all(f["installed"] for f in files.values())
    return {"installed": all_installed, "dir": str(_STT_DIR), "files": files}


def _load_stt_prefs() -> dict[str, Any]:
    prefs = dict(_STT_PREFS_DEFAULTS)
    if _STT_PREFS_PATH.exists():
        try:
            with open(_STT_PREFS_PATH, "r", encoding="utf-8") as f:
                stored = json.load(f)
            if isinstance(stored, dict):
                prefs.update(stored)
        except Exception:
            pass
    return prefs


def _save_stt_prefs(prefs: dict[str, Any]) -> None:
    _STT_PREFS_PATH.write_text(json.dumps(prefs, indent=2), encoding="utf-8")


def _get_whisper_model():
    """Lazy-load faster-whisper model (singleton)."""
    global _whisper_model
    if _whisper_model is not None:
        return _whisper_model
    from faster_whisper import WhisperModel
    prefs = _load_stt_prefs()
    model_path = str(_STT_DIR)
    if not (_STT_DIR / "model.bin").exists():
        raise HTTPException(status_code=400, detail="STT model not installed")
    _whisper_model = WhisperModel(
        model_path,
        device=prefs.get("device", "cpu"),
        compute_type=prefs.get("compute_type", "int8"),
    )
    return _whisper_model


@router.get("/api/settings/stt")
async def get_stt_status() -> dict[str, Any]:
    return _stt_status()


@router.get("/api/settings/stt/prefs")
async def get_stt_prefs() -> dict[str, Any]:
    return _load_stt_prefs()


@router.post("/api/settings/stt/prefs")
async def set_stt_prefs(request: Request) -> dict[str, Any]:
    body = await request.json()
    prefs = _load_stt_prefs()
    if "language" in body:
        prefs["language"] = str(body["language"]).strip()
    if "beam_size" in body:
        try:
            prefs["beam_size"] = int(body["beam_size"])
        except (ValueError, TypeError):
            pass
    if "compute_type" in body:
        prefs["compute_type"] = str(body["compute_type"]).strip()
    _save_stt_prefs(prefs)
    # Reset singleton so next transcription uses new prefs
    global _whisper_model
    _whisper_model = None
    return {"status": "ok", **prefs}


@router.post("/api/settings/stt/download")
async def download_stt_models(request: Request) -> StreamingResponse:
    """Download STT model files from HuggingFace with streaming progress."""
    import urllib.request

    _STT_DIR.mkdir(parents=True, exist_ok=True)

    async def _stream() -> AsyncGenerator[str, None]:
        for name, meta in _STT_FILES.items():
            path = _STT_DIR / name
            if path.exists() and path.stat().st_size >= meta["size"] * 0.90:
                yield json.dumps({"file": name, "status": "skip", "reason": "already installed"}) + "\n"
                continue

            yield json.dumps({"file": name, "status": "start", "total": meta["size"]}) + "\n"
            try:
                tmp = path.with_suffix(".part")
                req = urllib.request.Request(meta["url"], headers={"User-Agent": "Sable/1.0"})
                with urllib.request.urlopen(req, timeout=600) as resp:
                    downloaded = 0
                    with open(tmp, "wb") as f:
                        while True:
                            if await request.is_disconnected():
                                tmp.unlink(missing_ok=True)
                                yield json.dumps({"file": name, "status": "cancelled"}) + "\n"
                                return
                            chunk = resp.read(1024 * 256)
                            if not chunk:
                                break
                            f.write(chunk)
                            downloaded += len(chunk)
                            yield json.dumps({"file": name, "status": "progress", "downloaded": downloaded, "total": meta["size"]}) + "\n"
                            await asyncio.sleep(0)
                tmp.rename(path)
                yield json.dumps({"file": name, "status": "done", "size": downloaded}) + "\n"
            except Exception as e:
                if tmp.exists():
                    tmp.unlink()
                yield json.dumps({"file": name, "status": "error", "error": str(e)}) + "\n"

        yield json.dumps({"status": "complete"}) + "\n"

    return StreamingResponse(_stream(), media_type="application/x-ndjson")


@router.delete("/api/settings/stt")
async def delete_stt_models() -> dict[str, Any]:
    removed = []
    for name in _STT_FILES:
        path = _STT_DIR / name
        if path.exists():
            path.unlink()
            removed.append(name)
    global _whisper_model
    _whisper_model = None
    return {"status": "ok", "removed": removed}


@router.post("/api/stt/transcribe")
async def stt_transcribe(request: Request, file: UploadFile = File(...)) -> dict[str, Any]:
    """Transcribe an audio file using faster-whisper. Returns text + segments."""
    import tempfile
    import os

    status = _stt_status()
    if not status["installed"]:
        raise HTTPException(status_code=400, detail="STT model not installed")

    # Read uploaded audio to temp file
    suffix = Path(file.filename or "audio.wav").suffix or ".wav"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        content = await file.read()
        if len(content) > 100 * 1024 * 1024:  # 100MB limit
            os.unlink(tmp.name)
            raise HTTPException(status_code=413, detail="Audio file too large (max 100MB)")
        tmp.write(content)
        tmp_path = tmp.name

    wav_path = None
    try:
        # Convert non-WAV formats (webm, mp4, ogg, etc.) to WAV via ffmpeg
        if suffix.lower() not in (".wav",):
            import subprocess
            wav_fd, wav_path = tempfile.mkstemp(suffix=".wav")
            os.close(wav_fd)
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-i", tmp_path,
                "-ar", "16000", "-ac", "1", "-f", "wav", wav_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            if proc.returncode != 0:
                raise HTTPException(status_code=400, detail=f"Audio conversion failed (unsupported format: {suffix})")
            transcribe_path = wav_path
        else:
            transcribe_path = tmp_path

        model = _get_whisper_model()
        prefs = _load_stt_prefs()
        segments_iter, info = model.transcribe(
            transcribe_path,
            language=prefs.get("language", "en"),
            beam_size=prefs.get("beam_size", 5),
        )
        segments = []
        full_text_parts = []
        for seg in segments_iter:
            segments.append({
                "start": round(seg.start, 2),
                "end": round(seg.end, 2),
                "text": seg.text.strip(),
            })
            full_text_parts.append(seg.text.strip())

        full_text = " ".join(full_text_parts)

        # Apply voice filters
        filtered_reason = None
        if is_whisper_hallucination(full_text):
            filtered_reason = "hallucination"
            full_text = ""
            segments = []
        elif request.headers.get("X-Session-Id"):
            session_id = request.headers["X-Session-Id"]
            if is_tts_echo(full_text, session_id):
                filtered_reason = "tts_echo"
                full_text = ""
                segments = []

        result: dict[str, Any] = {
            "text": full_text,
            "segments": segments,
            "duration": round(info.duration, 2),
            "language": info.language,
            "language_probability": round(info.language_probability, 3),
        }
        if filtered_reason:
            result["filtered"] = filtered_reason
        return result
    except Exception as e:
        logger.error(f"STT transcription failed: {e}")
        raise HTTPException(status_code=500, detail=f"Transcription failed: {e}")
    finally:
        os.unlink(tmp_path)
        if wav_path and os.path.exists(wav_path):
            os.unlink(wav_path)
# ── /STT ──────────────────────────────────────────────────────────────


@router.post("/api/settings/general")
async def update_general_settings(payload: dict[str, Any]) -> dict[str, Any]:
    """Update general app settings."""
    settings = _read_system_settings()

    val = payload.get("max_tool_output_chars")
    if val is not None:
        try:
            val = int(val)
            if val < 1000:
                raise ValueError
            settings["max_tool_output_chars"] = val
        except (ValueError, TypeError):
            raise HTTPException(400, "max_tool_output_chars must be an integer >= 1000")

    if "desktop_notifications" in payload:
        settings["desktop_notifications"] = bool(payload["desktop_notifications"])

    if "notification_categories" in payload:
        from engine.notifications.policy import CATEGORY_META
        incoming = payload["notification_categories"] or {}
        if not isinstance(incoming, dict):
            raise HTTPException(400, "notification_categories must be an object")
        current = settings.get("notification_categories") or {}
        for k in CATEGORY_META:
            if k in incoming:
                current[k] = bool(incoming[k])
        settings["notification_categories"] = current

    _write_system_settings(settings)
    logger.info("General settings updated: max_tool_output_chars=%s, desktop_notifications=%s",
                settings.get("max_tool_output_chars"), settings.get("desktop_notifications"))
    from engine.notifications.policy import CATEGORY_META as _CM, DEFAULT_CATEGORIES as _DC
    _cats = settings.get("notification_categories") or {}
    return {
        "status": "ok",
        "max_tool_output_chars": settings.get("max_tool_output_chars", 100_000),
        "desktop_notifications": settings.get("desktop_notifications", True),
        "notification_categories": {
            k: bool(_cats.get(k, _DC.get(k, True))) for k in _CM
        },
    }

