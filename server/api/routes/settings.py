from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, AsyncGenerator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
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


@router.post("/api/settings/browser/refresh-waf")
async def refresh_waf_token() -> dict[str, Any]:
    """Re-launch browser briefly to extract fresh Qwen WAF/cookie headers."""
    try:
        svc = service
        headers = await svc._refresh_headers()
        has_cookie = bool(headers.get("Cookie"))
        has_bx = bool(headers.get("bx-ua"))
        return {
            "status": "ok",
            "message": "WAF tokens refreshed",
            "has_cookie": has_cookie,
            "has_bx_ua": has_bx,
        }
    except Exception as exc:
        logger.error("WAF token refresh failed: %s", exc)
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
async def fetch_custom_endpoint_models(request: Request) -> dict[str, Any]:
    """Fetch the model list from an arbitrary OpenAI-compatible endpoint.

    Body: {"base_url": "https://.../v1", "api_key": "optional"}
    Hits {base_url}/models and normalizes the response.
    """
    import httpx as _httpx
    body = await request.json()
    base_url = (body.get("base_url") or "").strip().rstrip("/")
    api_key = (body.get("api_key") or "").strip()
    if not base_url:
        raise HTTPException(status_code=400, detail="Missing base_url")

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
    """Fetch + normalize models from any OpenAI-compatible /models endpoint."""
    import httpx as _httpx
    base_url = base_url.strip().rstrip("/")
    api_key = api_key.strip()
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

        # Load exhaustion status
        from engine.config import get_all_exhaustion_status
        exhaustion = get_all_exhaustion_status()

        accounts: list[dict[str, Any]] = []
        for entry in _SYSTEM_DIR.iterdir():
            m = re.match(r"browser-data-acc(\d+)$", entry.name)
            if entry.is_dir() and m:
                accounts.append({
                    "name": entry.name,
                    "num": int(m.group(1)),
                    "email": _read_profile_email(entry),
                    "size_mb": _dir_size_mb(entry),
                    "has_waf": entry.name in waf_tokens,
                    "has_ds": entry.name in ds_tokens,
                    "exhausted": exhaustion.get(entry.name, False),
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

    # Load cached DeepSeek token for new account (no browser needed if cached)
    from connectors.deepseek.client import get_token_for_account as get_ds_token
    ds_cached = get_ds_token(target_name)
    if ds_cached:
        get_deepseek_client().set_token(ds_cached, account=target_name)
        logger.info("Loaded cached DeepSeek token for %s", target_name)
    else:
        # No cached token — extract from browser
        try:
            ds_token = await service.refresh_deepseek_token()
            get_deepseek_client().set_token(ds_token)
        except Exception:
            pass

    # Warmup loads cached Qwen WAF tokens or fetches fresh via browser
    await service.warmup()
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
                await page.goto(url, timeout=120000)
                # Wait until user closes the browser window
                await context.wait_for_event("close", timeout=0)
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
    "summarizer_model": "",   # empty = use current model
    "browser_data_acc": "",   # empty = use current/default account
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
    if "browser_data_acc" in body:
        settings["browser_data_acc"] = str(body["browser_data_acc"]).strip()
    _save_context_pass_settings(settings)
    return {"status": "ok", **settings}
# ── /Context Pass Settings ─────────────────────────────────────────

# ── Memory Consolidation Settings ─────────────────────────────────────
_CONSOLIDATION_SETTINGS_PATH = BASE_DIR / "system/consolidation_settings.json"
_CONSOLIDATION_DEFAULTS: dict[str, Any] = {
    "model": "",                    # empty = use current chat model
    "fallback_models": [],          # ordered list of fallback model IDs
    "browser_profiles": [],         # ordered list of browser profile names for Qwen fallback
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
    if "browser_profiles" in body:
        bp = body["browser_profiles"]
        settings["browser_profiles"] = [str(p).strip() for p in bp if isinstance(bp, list)] if isinstance(bp, list) else []
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
    "voice": "af_bella",
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
    if "voice" in body:
        prefs["voice"] = str(body["voice"]).strip()
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
    """Synthesize text to speech. Returns WAV audio."""
    import io
    import soundfile as sf

    body = await request.json()
    text = (body.get("text") or "").strip()
    # Fall back to saved prefs if voice/speed not provided
    prefs = _load_tts_prefs()
    voice = body.get("voice") or prefs.get("voice", "af_bella")
    speed = body.get("speed") if body.get("speed") is not None else prefs.get("speed", 1.0)

    if not text:
        raise HTTPException(status_code=400, detail="Empty text")
    if len(text) > 5000:
        raise HTTPException(status_code=400, detail="Text too long (max 5000 chars)")

    try:
        kokoro = _get_kokoro()
        samples, sr = kokoro.create(text, voice=voice, speed=speed)
    except Exception as e:
        logger.error(f"TTS synthesis failed: {e}")
        raise HTTPException(status_code=500, detail=f"Synthesis failed: {e}")

    buf = io.BytesIO()
    sf.write(buf, samples, sr, format="WAV")
    buf.seek(0)
    return Response(content=buf.read(), media_type="audio/wav")


@router.get("/api/tts/voices")
async def tts_voices() -> dict[str, Any]:
    """List available TTS voices."""
    try:
        kokoro = _get_kokoro()
        if hasattr(kokoro, "voices"):
            voices = sorted(kokoro.voices.keys())
        elif hasattr(kokoro, "get_voices"):
            voices = sorted(kokoro.get_voices())
        else:
            voices = []
        return {"voices": voices}
    except HTTPException:
        return {"voices": [], "error": "TTS models not installed"}
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
        "has_serper_key": bool(settings.get("serper_api_key")),
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
        if val is not None and str(val).strip():
            settings[field] = str(val).strip()

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


# ─── General Settings (tool output limit, etc.) ─────────────────────────────

@router.get("/api/settings/general")
async def get_general_settings() -> dict[str, Any]:
    """Return general app settings."""
    settings = _read_system_settings()
    return {
        "max_tool_output_chars": settings.get("max_tool_output_chars", 100_000),
    }


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

    _write_system_settings(settings)
    logger.info("General settings updated: max_tool_output_chars=%s", settings.get("max_tool_output_chars"))
    return {"status": "ok", "max_tool_output_chars": settings.get("max_tool_output_chars", 100_000)}

