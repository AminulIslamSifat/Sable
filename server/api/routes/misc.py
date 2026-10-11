from __future__ import annotations

import asyncio
import json
import re
import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse   # <-- added StreamingResponse
from engine.config import MODELS
from engine.scraper import get_settings as get_scraper_settings
from engine.skills import browse_skills, list_skills
from engine.tools_loader import browse_tools, list_tools
from engine.memory_search import get_searcher

from server.config import (
    DEEPSEEK_MODELS, INDEX_FILE, AUTH_EXEMPT_PREFIXES,
    TYPEWRITER_CHARS_PER_TICK, TYPEWRITER_TICK_MS,
)
from server.models import RevertRequest
from server.utils import logger
from ..dependencies import service, sse
from server.database import list_chats, add_response_stat, get_model_speeds

from server.logging_setup import _log_buffer

router = APIRouter()


@router.get("/api/search/image")
def search_image(q: str, count: int = 1) -> dict[str, Any]:
    """Resolve a text query to an image URL via SearXNG.

    Used by the rich-component frontend to resolve `image_query` fields at
    render time. Returns {"image": null} when nothing is found so the caller
    can draw a placeholder instead of a broken image.
    """
    query = (q or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="q is required")
    try:
        from engine.search import searxng_image_results
        results = searxng_image_results(query, count=max(1, min(count, 8)))
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("search_image failed for %r: %s", query, exc)
        results = []
    if not results:
        return {"image": None, "thumbnail": None, "results": []}
    top = results[0]
    return {
        "image": top.get("image"),
        "thumbnail": top.get("thumbnail"),
        "title": top.get("title", ""),
        "source": top.get("url", ""),
        "results": results,
    }


@router.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/api/warmup")
async def warmup() -> dict[str, Any]:
    """Kick off the one-shot browser/token warmup. Idempotent.

    Called by the UI once it has actually loaded — so a headless server with
    nobody watching never launches Chromium. Only the FIRST call starts
    anything; a tab refresh, a second tab, or a manual re-POST all return
    ``started: false`` without touching a browser.
    """
    from ..application import ensure_warmup_started
    started = ensure_warmup_started()
    return {
        "status": "ok",
        "started": started,
        "message": "Warmup started" if started else "Warmup already ran this session",
    }


@router.get("/api/env")
def env_info() -> dict[str, str]:
    """Return non-sensitive environment info for frontend path resolution."""
    from pathlib import Path
    from engine.config import USER_NAME
    return {
        "home": str(Path.home()),
        "user": USER_NAME,
    }


@router.get("/api/config/ui")
def ui_config() -> dict[str, Any]:
    return {
        "typewriter_chars_per_tick": TYPEWRITER_CHARS_PER_TICK,
        "typewriter_tick_ms": TYPEWRITER_TICK_MS,
        "model_speeds": get_model_speeds(30),
    }


@router.post("/api/stats/response")
def record_response_stat(payload: dict[str, Any]) -> dict[str, Any]:
    """Record one response's generation speed. Fire-and-forget from the client."""
    model = str(payload.get("model") or "unknown")
    tps = float(payload.get("tps") or 0)
    tokens = int(payload.get("tokens") or 0)
    gen_ms = int(payload.get("gen_ms") or 0)
    total_ms = int(payload.get("total_ms") or 0)
    # Sanity floor — ignore degenerate readings (stops, errors, sub-100ms blips)
    if tps <= 0 or tokens < 8 or gen_ms < 100:
        return {"ok": False, "skipped": True}
    add_response_stat(model, tokens, gen_ms, total_ms, tps)
    return {"ok": True}


@router.get("/api/stats/model_speeds")
def model_speeds() -> dict[str, Any]:
    return {"speeds": get_model_speeds(30)}

@router.get("/api/logs")
async def stream_logs():
    async def generator():
        while True:
            try:
                msg = await asyncio.wait_for(_log_buffer.get(), timeout=15.0)
                yield sse({"type": "log", "message": msg})
            except asyncio.TimeoutError:
                yield sse({"type": "ping"})
    return StreamingResponse(generator(), media_type="text/event-stream")

def _get_available_backends() -> set[str]:
    """Determine which backends have valid credentials configured.

    Returns a set of backend names that are ready to use.
    'qwen' = browser-based Qwen (has WAF tokens or browser profiles).
    'deepseek' = has JWT tokens in the per-account store.
    Others = have at least one API key saved.
    Always includes 'local' (custom endpoints are user-managed).
    """
    import importlib

    available: set[str] = {"local"}

    # Qwen: check for WAF tokens or browser profiles
    try:
        from engine.config import load_qwen_token_store, _SYSTEM as _sys_dir
        token_store = load_qwen_token_store()
        has_qwen_tokens = any(entries for entries in token_store.values() if entries)
        has_profiles = any(
            d.is_dir() and d.name.startswith("browser-data-acc")
            for d in _sys_dir.iterdir()
        ) if _sys_dir.is_dir() else False
        if has_qwen_tokens or has_profiles:
            available.add("qwen")
    except Exception as exc:
        logger.debug("Backend qwen unavailable: %s", exc)

    # DeepSeek: check for JWT tokens
    try:
        from connectors.deepseek.client import get_token_for_account
        if get_token_for_account():
            available.add("deepseek")
            # Experimental scraper transport reuses the same account profile.
            available.add("deepseek_scraper")
    except Exception as exc:
        logger.debug("Backend deepseek unavailable: %s", exc)

    # Qwen (scraper): headed-browser transport reuses the Qwen account profile —
    # available whenever any logged-in Qwen browser profile exists.
    if "qwen" in available:
        available.add("qwen_scraper")

    # Gemini (web): browser-assisted — available if a captured request template
    # exists and a signed-in Google account has been seen (live or on disk).
    try:
        from connectors.gemini_web.client import get_client as get_gweb_client
        from connectors.gemini_web import auth as _gwauth
        has_template = get_gweb_client().is_available
        has_auth = bool(_gwauth.list_valid_accounts())
        if has_template and has_auth:
            available.add("gemini_web")
    except Exception as exc:
        logger.debug("Backend gemini_web unavailable: %s", exc)

    # z.ai: browser-assisted (like qwen) — available if a token is cached OR
    # a logged-in browser profile exists, since the token can be pulled live.
    try:
        from connectors.zai.client import get_token_for_account as get_zai_token
        from engine.config import _SYSTEM as _sys_dir2
        has_zai_token = bool(get_zai_token())
        has_zai_profile = any(
            d.is_dir() and d.name.startswith("browser-data-acc")
            for d in _sys_dir2.iterdir()
        ) if _sys_dir2.is_dir() else False
        if has_zai_token or has_zai_profile:
            available.add("zai")
    except Exception as exc:
        logger.debug("Backend zai unavailable: %s", exc)

    # MiMo Studio: browser-auth — available if any account has a cached auth snapshot
    try:
        from engine.config import _SYSTEM as _sys_dir3
        has_mimo = any(
            (d / ".mimo_auth.json").is_file()
            for d in _sys_dir3.iterdir()
            if d.is_dir() and d.name.startswith("browser-data-acc")
        ) if _sys_dir3.is_dir() else False
        if has_mimo:
            available.add("mimo")
    except Exception as exc:
        logger.debug("Backend mimo unavailable: %s", exc)

    # Kimi (kimi.ai): ghost-tab transport. Available if a profile holds a kimi
    # login — either the ghost marker or a kimi.ai cookie. The pinned provider
    # account is checked first, then any browser-data-* profile.
    try:
        from engine.config import _SYSTEM as _sys_dir4
        from engine.config import get_provider_account as _get_kimi_pin
        candidates = []
        pinned = _get_kimi_pin("kimi")
        if pinned:
            candidates.append(_sys_dir4 / pinned)
        if _sys_dir4.is_dir():
            candidates += [d for d in _sys_dir4.iterdir()
                           if d.is_dir() and d.name.startswith("browser-data-")]
        for d in candidates:
            try:
                if (d / ".kimi_ghost.json").is_file():
                    available.add("kimi"); break
                ck = d / "Default" / "Cookies"
                if ck.is_file() and b"kimi.ai" in open(ck, "rb").read():
                    available.add("kimi"); break
            except Exception:
                continue
    except Exception as exc:
        logger.debug("Backend kimi unavailable: %s", exc)

    # Mistral (Web): browser-auth — available if any profile holds a Mistral
    # session (an ory_session_* cookie on chat.mistral.ai) or a saved snapshot.
    try:
        from engine.config import _SYSTEM as _sys_dir5
        from engine.config import get_provider_account as _get_mw_pin
        candidates = []
        pinned = _get_mw_pin("mistral_web")
        if pinned:
            candidates.append(_sys_dir5 / pinned)
        if _sys_dir5.is_dir():
            candidates += [d for d in _sys_dir5.iterdir()
                           if d.is_dir() and d.name.startswith("browser-data-")]
        for d in candidates:
            try:
                if (d / ".mistral_web_auth.json").is_file():
                    available.add("mistral_web"); break
                ck = d / "Default" / "Cookies"
                if ck.is_file() and b"chat.mistral.ai" in open(ck, "rb").read():
                    available.add("mistral_web"); break
            except Exception:
                continue
    except Exception as exc:
        logger.debug("Backend mistral_web unavailable: %s", exc)

    # ChatGPT (chatgpt.com): ghost-tab transport. Available if a profile holds
    # a chatgpt.com login — the ghost marker, or a __Secure-next-auth session
    # cookie in the profile's Cookies SQLite table. The pinned provider account
    # is checked first, then any browser-data-* profile.
    try:
        import sqlite3
        from engine.config import _SYSTEM as _sys_dir6
        from engine.config import get_provider_account as _get_cg_pin
        candidates = []
        pinned = _get_cg_pin("chatgpt")
        if pinned:
            candidates.append(_sys_dir6 / pinned)
        if _sys_dir6.is_dir():
            candidates += [d for d in _sys_dir6.iterdir()
                           if d.is_dir() and d.name.startswith("browser-data-")]
        for d in candidates:
            try:
                if (d / ".chatgpt_ghost.json").is_file():
                    available.add("chatgpt"); break
                ck = d / "Default" / "Cookies"
                if ck.is_file():
                    conn = sqlite3.connect(f"file:{ck}?mode=ro", uri=True)
                    try:
                        cur = conn.execute(
                            "SELECT name FROM cookies WHERE host_key LIKE '%chatgpt%' "
                            "AND name LIKE '__Secure-next-auth.session-token%'"
                        )
                        if cur.fetchone() is not None:
                            available.add("chatgpt"); break
                    finally:
                        conn.close()
            except Exception:
                continue
    except Exception as exc:
        logger.debug("Backend chatgpt unavailable: %s", exc)

    # API-key-based providers
    _key_providers = {
        "gemini": "connectors.gemini.client",
        "groq": "connectors.groq.client",
        "mistral": "connectors.mistral.client",
        "openai": "connectors.openai.client",
    }
    for backend, module_path in _key_providers.items():
        try:
            mod = importlib.import_module(module_path)
            client = mod.get_client()
            if client.is_available:
                available.add(backend)
        except Exception as exc:
            logger.debug("Backend %s unavailable: %s", backend, exc)

    # Cloudflare: check for saved credentials
    try:
        from connectors.cloudflare.client import get_client as get_cf_client
        if get_cf_client().is_available:
            available.add("cloudflare")
    except Exception as exc:
        logger.debug("Backend cloudflare unavailable: %s", exc)

    return available


@router.get("/api/models")
def models() -> dict[str, list[dict[str, Any]]]:
    from engine.config import get_all_models
    scraper_cfg = get_scraper_settings()
    if scraper_cfg.get("enabled") and scraper_cfg.get("engine_type") == "deepseek":
        return {"models": DEEPSEEK_MODELS}

    all_models = get_all_models()

    if scraper_cfg.get("enabled") and scraper_cfg.get("engine_type") == "qwen":
        all_models = [m for m in all_models if not m.get("api_backend")]
    else:
        # Dynamic filtering: only show models whose backend has credentials
        available = _get_available_backends()
        filtered = []
        for m in all_models:
            backend = m.get("api_backend")
            if backend is None:
                # Qwen native model — only show if qwen backend is available
                if "qwen" in available:
                    filtered.append(m)
            elif backend in available:
                filtered.append(m)
        all_models = filtered

    return {
        "models": [
            {
                "id": m["id"],
                "label": m["label"],
                "api_backend": m.get("api_backend"),
                "capabilities": m.get("capabilities", {}),
                "thinking_modes": [
                    {"id": tm["id"], "label": tm["label"]} for tm in m.get("thinking_modes", [])
                ],
                "max_session_chars": m.get("max_session_chars"),
                "custom": m.get("_custom", False),
            }
            for m in all_models
        ]
    }

@router.get("/api/skills")
def skills() -> dict[str, list[dict[str, Any]]]:
    return {"skills": list_skills()}

@router.get("/api/skills/browse")
def skills_browse() -> dict[str, list[dict[str, Any]]]:
    return {"skills": browse_skills()}

_DISABLED_SKILLS_PATH = Path(__file__).resolve().parent.parent.parent.parent / "Brain" / "disabled_skills.json"

@router.get("/api/settings/disabled-skills")
def get_disabled_skills() -> dict[str, list[str]]:
    if _DISABLED_SKILLS_PATH.exists():
        try:
            import json
            data = json.loads(_DISABLED_SKILLS_PATH.read_text(encoding="utf-8"))
            return {"disabled": data if isinstance(data, list) else []}
        except Exception:
            return {"disabled": []}
    return {"disabled": []}

@router.post("/api/settings/disabled-skills")
async def set_disabled_skills(request: Request) -> dict[str, str]:
    import json
    body = await request.json()
    disabled = body.get("disabled", [])
    if not isinstance(disabled, list):
        raise HTTPException(status_code=400, detail="disabled must be a list")
    _DISABLED_SKILLS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _DISABLED_SKILLS_PATH.write_text(json.dumps(disabled), encoding="utf-8")
    return {"status": "ok"}

@router.get("/api/tools")
def tools_list() -> dict[str, list[dict[str, Any]]]:
    return {"tools": list_tools()}

@router.get("/api/tools/browse")
def tools_browse() -> dict[str, list[dict[str, Any]]]:
    return {"tools": browse_tools()}

_DISABLED_TOOLS_PATH = Path(__file__).resolve().parent.parent.parent.parent / "Brain" / "disabled_tools.json"

@router.get("/api/settings/disabled-tools")
def get_disabled_tools() -> dict[str, list[str]]:
    if _DISABLED_TOOLS_PATH.exists():
        try:
            import json
            data = json.loads(_DISABLED_TOOLS_PATH.read_text(encoding="utf-8"))
            return {"disabled": data if isinstance(data, list) else []}
        except Exception:
            return {"disabled": []}
    return {"disabled": []}

@router.post("/api/settings/disabled-tools")
async def set_disabled_tools(request: Request) -> dict[str, str]:
    import json
    body = await request.json()
    disabled = body.get("disabled", [])
    if not isinstance(disabled, list):
        raise HTTPException(status_code=400, detail="disabled must be a list")
    _DISABLED_TOOLS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _DISABLED_TOOLS_PATH.write_text(json.dumps(disabled), encoding="utf-8")
    return {"status": "ok"}

_SETTINGS_PATH = Path(__file__).resolve().parent.parent.parent.parent / "system" / "settings.json"
_VALID_TOOL_FORMATS = {"qwen_json", "hermes", "dsml", "native", "none", ""}

@router.get("/api/settings/tool-format")
def get_tool_format_setting() -> dict[str, str]:
    """Return the current tool_call_format override from settings."""
    if _SETTINGS_PATH.is_file():
        try:
            data = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
            fmt = data.get("tool_call_format", "")
            return {"tool_call_format": fmt if fmt in _VALID_TOOL_FORMATS else ""}
        except Exception:
            pass
    return {"tool_call_format": ""}

@router.post("/api/settings/tool-format")
async def set_tool_format_setting(request: Request) -> dict[str, str]:
    """Set the tool_call_format override in system/settings.json."""
    body = await request.json()
    fmt = body.get("tool_call_format", "")
    if fmt not in _VALID_TOOL_FORMATS:
        raise HTTPException(status_code=400, detail=f"Invalid format. Must be one of: {', '.join(sorted(_VALID_TOOL_FORMATS - {''}))} or empty for default")

    # Read existing settings
    data: dict[str, Any] = {}
    if _SETTINGS_PATH.is_file():
        try:
            data = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
        except Exception:
            data = {}

    if fmt:
        data["tool_call_format"] = fmt
    else:
        data.pop("tool_call_format", None)  # Remove to use per-provider defaults

    _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _SETTINGS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(_SETTINGS_PATH)

    # Invalidate instruction cache so next chat picks up the new format
    try:
        from connectors.common.instruction_builder import invalidate_cache
        invalidate_cache()
    except Exception:
        pass

    return {"status": "ok"}

@router.post("/api/sync-context")
async def sync_context_route(request: Request) -> dict[str, Any]:
    from connectors.common.instruction_builder import invalidate_cache
    invalidate_cache()
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    layout_mode = body.get("layout_mode") or None
    try:
        success = await service.sync_context(layout_mode=layout_mode)
    except Exception as exc:
        logger.warning("sync_context raised: %s: %s", type(exc).__name__, exc)
        raise HTTPException(status_code=500, detail=str(exc))
    if success:
        return {"status": "ok", "message": "Context synced successfully"}
    # Not authenticated or skipped — not a server error
    return {"status": "skipped", "message": "Sync skipped: not authenticated or no session"}

@router.post("/api/file/revert")
def revert_file(payload: RevertRequest) -> dict[str, str]:
    from engine.skills import BACKUP_DIR
    backup = Path(payload.backup_path).expanduser()
    target = Path(payload.path).expanduser()
    try:
        backup.resolve().relative_to(BACKUP_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Backup outside managed directory")
    if not backup.is_file():
        raise HTTPException(status_code=404, detail="Backup not found")
    try:
        shutil.copy2(backup, target)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Revert failed: {exc}")
    return {"status": "ok"}

# Cache index.html in memory to avoid disk I/O on every request.
#
# The cache is keyed on the file's mtime, NOT just "is it loaded yet". A bare
# one-shot cache means editing index.html leaves the server serving the old
# bytes until a full restart — so adding a <script> tag silently does nothing
# and the feature looks broken. A stat() per request is far cheaper than the
# restart it saves.
_INDEX_CACHE: str | None = None
_INDEX_CACHE_MTIME: float = -1.0

def _get_index_html() -> str:
    global _INDEX_CACHE, _INDEX_CACHE_MTIME
    try:
        mtime = INDEX_FILE.stat().st_mtime
    except OSError:
        mtime = 0.0
    if _INDEX_CACHE is None or mtime != _INDEX_CACHE_MTIME:
        if INDEX_FILE.exists():
            _INDEX_CACHE = INDEX_FILE.read_text(encoding="utf-8")
        else:
            _INDEX_CACHE = "<html><body><h1>Sable</h1><p>index.html not found.</p></body></html>"
        _INDEX_CACHE_MTIME = mtime
    return _INDEX_CACHE

@router.get("/", response_class=HTMLResponse)
def index() -> str:
    return _get_index_html()


_INSTRUCTION_EXTRA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "instruction" / "extra"

@router.get("/api/instruction/{name}")
def get_instruction(name: str) -> dict[str, str]:
    """Serve instruction files from instruction/extra/ directory."""
    # Sanitize: strip path traversal characters
    safe = re.sub(r'[^a-zA-Z0-9._-]', '', name)
    path = _INSTRUCTION_EXTRA_DIR / safe
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"Instruction '{safe}' not found")
    return {"content": path.read_text(encoding="utf-8")}


# ── DreamForge: Full-featured AI Image Generator ─────────────────────────────
# NOTE: These MUST be above the catch-all /api/tool/{tool_name} route below,
# otherwise FastAPI matches the parameterized route first.

@router.post("/api/tool/dreamforge_generate")
async def dreamforge_generate(request: Request) -> dict[str, Any]:
    """Generate images via DreamForge (Perchance backend with full options)."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    from tools.image_generator.scripts.dreamforge import generate, DreamForgeOptions

    opts = DreamForgeOptions.from_dict(body)
    if not opts.prompt.strip():
        return {"ok": False, "error": "prompt is required", "images": [], "count": 0}

    # Run in thread to avoid blocking the event loop
    import asyncio
    result = await asyncio.to_thread(generate, opts)
    return result


@router.get("/api/tool/dreamforge_options")
async def dreamforge_options() -> dict[str, Any]:
    """Return all available DreamForge options for the frontend."""
    from tools.image_generator.scripts.dreamforge import get_all_options
    return get_all_options()


# ── Advanced SDXL: Perchance SDXL Generator with composition/model controls ──

@router.post("/api/tool/advanced_sdxl_generate")
async def advanced_sdxl_generate(request: Request) -> dict[str, Any]:
    """Generate images via Advanced SDXL (Perchance backend with model/composition options)."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    from tools.image_generator.scripts.advanced_sdxl import generate, AdvancedSdxlOptions

    opts = AdvancedSdxlOptions.from_dict(body)
    if not opts.prompt.strip():
        return {"ok": False, "error": "prompt is required", "images": [], "count": 0}

    import asyncio
    result = await asyncio.to_thread(generate, opts)
    return result


@router.get("/api/tool/advanced_sdxl_options")
async def advanced_sdxl_options() -> dict[str, Any]:
    """Return all available Advanced SDXL options for the frontend."""
    from tools.image_generator.scripts.advanced_sdxl import get_all_options
    return get_all_options()


# ── Direct Tool Invocation (for Library panel UI) ────────────────────────────

_TOOLS_DIR = Path(__file__).resolve().parent.parent.parent.parent / "tools"

@router.post("/api/tool/{tool_name}")
async def invoke_tool_direct(tool_name: str, request: Request) -> dict[str, Any]:
    """Directly invoke a tool's CLI script with JSON body as args.
    Used by Library panel UI components (e.g., Image Generator)."""
    # Whitelist allowed tools for direct invocation
    ALLOWED_DIRECT = {"image_generator", "generate_image"}
    if tool_name not in ALLOWED_DIRECT:
        raise HTTPException(status_code=403, detail=f"Direct invocation not allowed for '{tool_name}'")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    # Map to the actual script
    script_dir = _TOOLS_DIR / "image_generator" / "scripts"
    script = script_dir / "image_generator.py"
    if not script.is_file():
        raise HTTPException(status_code=404, detail="Tool script not found")

    provider = body.get("provider", "perchance")

    # ── Cloudflare Workers AI: free FLUX image generation (~260/day) ──
    if provider == "cloudflare":
        from connectors.cloudflare.client import get_client as get_cf_client
        prompt = body.get("prompt", "")
        if not prompt:
            return {"ok": False, "error": "prompt is required"}
        result = get_cf_client().generate_image(
            prompt=prompt,
            model=body.get("model", "@cf/black-forest-labs/flux-1-schnell"),
            shape=body.get("shape", "square"),
            negative_prompt=body.get("negative_prompt", ""),
            steps=body.get("steps"),
            seed=int(body.get("seed", -1)),
        )
        return result

    # ── Puter: free driver-API image generation (multi-key) ──
    if provider == "puter":
        from connectors.puter.client import get_client as get_puter_client
        prompt = body.get("prompt", "")
        if not prompt:
            return {"ok": False, "error": "prompt is required"}
        result = get_puter_client().generate_image(
            prompt=prompt,
            model=body.get("model", "openai/gpt-image-1-mini"),
            shape=body.get("shape", "square"),
            negative_prompt=body.get("negative_prompt", ""),
            count=int(body.get("count", 1)),
        )
        return result

    # ── Pollinations: direct HTTP, no script needed ──
    if provider == "pollinations":
        import urllib.parse, urllib.request, time, hashlib
        prompt = body.get("prompt", "")
        model = body.get("model", "flux")
        shape = body.get("shape", "square")
        seed = body.get("seed")

        dims = {"square": (768, 768), "portrait": (768, 1024), "landscape": (1024, 768)}
        w, h = dims.get(shape, (768, 768))

        encoded = urllib.parse.quote(prompt)
        url = f"https://image.pollinations.ai/prompt/{encoded}?width={w}&height={h}&model={model}&nologo=true"
        if seed:
            url += f"&seed={seed}"

        out_dir = Path.home() / "sable_output" / "assets"
        out_dir.mkdir(parents=True, exist_ok=True)
        fname = f"gen_poll_{int(time.time())}_{seed or 'rand'}.jpg"
        out_path = out_dir / fname

        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Sable/1.0"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read()
            out_path.write_bytes(data)
            return {
                "ok": True,
                "images": [{
                    "ok": True,
                    "path": str(out_path),
                    "filename": fname,
                    "seed": int(seed) if seed else 0,
                    "width": w,
                    "height": h,
                    "size_bytes": len(data),
                }],
                "count": 1,
                "provider": "pollinations",
                "model": model,
                "shape": shape,
                "prompt_used": prompt,
            }
        except Exception as e:
            return {"ok": False, "error": f"Pollinations failed: {e}"}

    # ── Perchance: use existing CLI script ──
    cli_args = ["generate"]
    param_map = {
        "prompt": "--prompt",
        "style": "--style",
        "shape": "--shape",
        "count": "--count",
        "negative_prompt": "--negative-prompt",
        "seed": "--seed",
        "key": "--key",
    }
    for key, flag in param_map.items():
        val = body.get(key)
        if val is not None and str(val) != "":
            cli_args += [flag, str(val)]

    import subprocess
    try:
        proc = subprocess.run(
            ["python3", str(script)] + cli_args,
            capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Timed out after 120s"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

    output = proc.stdout.strip()
    if proc.returncode != 0:
        err = proc.stderr.strip() or output or "Tool failed"
        return {"ok": False, "error": err}

    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return {"ok": True, "raw": output}



