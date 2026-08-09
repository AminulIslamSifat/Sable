from __future__ import annotations

import asyncio
import json
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Generator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from engine.memory_search import get_searcher
from connectors.deepseek.client import get_client as get_deepseek_client

from .dependencies import service
from server.database import init_db, recover_stale_agents, migrate_skill_events_to_table
import server.auth as _auth_mod
from server.config import (
    AUTH_EXEMPT_PREFIXES, WEB_DIR, UPLOAD_DIR,
    _MEMORY_SEARCH_SETTINGS,
)
from engine.config import ASSETS_DIR
from server.utils import logger

# Import routers
from .routes.auth import router as auth_router
from .routes.chats import router as chats_router
from .routes.memory import router as memory_router
from .routes.settings import router as settings_router
from .routes.scraper import router as scraper_router
from .routes.upload import router as upload_router
from .routes.deepseek import router as deepseek_router
from .routes.chat import router as chat_router
from .routes.misc import router as misc_router
from .routes.agents import router as agents_router
from .routes.library import router as library_router
from .routes.email import router as email_router
from .routes.filesystem import router as filesystem_router
from .routes.terminal import router as terminal_router
from .routes.telegram import router as telegram_router
from .routes.research import router as research_router
from .routes.tracknote import router as tracknote_router
from .routes.setup import router as setup_router
from .routes.cookbook import router as cookbook_router

def _raise_nofile_limit() -> None:
    """Raise open file limit for agentic workloads (browsers, agents, streams)."""
    import resource
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        target = min(65536, hard)
        if soft < target:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
            logger.info("Raised RLIMIT_NOFILE: %d → %d (hard=%d)", soft, target, hard)
    except Exception as exc:
        logger.warning("Could not raise RLIMIT_NOFILE: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI) -> Generator[None, None, None]:
    _raise_nofile_limit()

    import asyncio as _aio
    from engine.agents import get_runtime as _get_rt_startup
    _get_rt_startup()._loop = _aio.get_running_loop()

    # Auto-connect enabled MCP servers in the background
    try:
        from engine.mcp.manager import get_mcp_manager
        _aio.create_task(get_mcp_manager().connect_all_enabled())
    except Exception as exc:
        logger.warning("MCP auto-connect failed: %s: %s", type(exc).__name__, exc)

    init_db()
    # One-time migration: move skill_events from messages column to dedicated table
    migrated = migrate_skill_events_to_table()
    if migrated:
        logger.info("Migrated skill_events for %d messages to dedicated table", migrated)
    stale = recover_stale_agents()
    if stale:
        logger.info("Recovered %d stale agent(s) from previous session", stale)
    # Load saved role overrides + account assignments into registry
    try:
        from engine.config import AGENT_CONFIG_PATH
        from engine.agents.registry import apply_role_overrides, apply_account_assignments
        if AGENT_CONFIG_PATH.exists():
            _acfg = json.loads(AGENT_CONFIG_PATH.read_text(encoding="utf-8"))
            if _acfg.get("roles"):
                apply_role_overrides(_acfg["roles"])
            if _acfg.get("account_assignments"):
                apply_account_assignments(_acfg["account_assignments"])
    except Exception:
        pass
    if _MEMORY_SEARCH_SETTINGS.exists():
        try:
            _ms = json.loads(_MEMORY_SEARCH_SETTINGS.read_text(encoding="utf-8"))
            _s = get_searcher()
            if _ms.get("model"):
                _s.set_model(str(_ms["model"]))
            if isinstance(_ms.get("model_thresholds"), dict):
                _s.set_thresholds(_ms["model_thresholds"])
        except Exception:
            pass
    # Start agent ops scheduler (event-driven, no polling)
    try:
        from server.scheduler import start_scheduler
        _aio.create_task(start_scheduler())
    except Exception as exc:
        logger.warning("Agent ops scheduler failed to start: %s: %s", type(exc).__name__, exc)

    await service.warmup()
    try:
        ds_token = await service.refresh_deepseek_token()
        get_deepseek_client().set_token(ds_token)
    except Exception as exc:
        logger.warning("DeepSeek startup token refresh failed: %s: %s", type(exc).__name__, exc)
    # Auto-sync context for the active browser profile on startup
    try:
        await service.sync_context()
        logger.info("Context synced on startup")
    except Exception as exc:
        logger.warning("Startup sync_context failed: %s: %s", type(exc).__name__, exc)
    yield
    # ── Fast shutdown (≤1s budget) ──
    # 1. Cancel running agents immediately
    try:
        from engine.agents import get_runtime as _get_rt
        _rt = _get_rt()
        for _task in list(getattr(_rt, '_tasks', {}).values()):
            if not _task.done():
                _task.cancel()
    except Exception:
        pass

    # 2. Scheduler
    try:
        from server.scheduler import cancel_all
        cancel_all()
    except Exception:
        pass

    # 3. Main browser — 0.5s timeout
    try:
        await asyncio.wait_for(service.close(), timeout=0.5)
    except Exception:
        pass

    # 4. Scraper browser — 0.5s timeout
    try:
        from engine.scraper import scraper as scraper_service
        await asyncio.wait_for(scraper_service.stop(kill_browser=True), timeout=0.5)
    except Exception:
        pass

    # 5. MCP — 0.5s timeout
    try:
        from engine.mcp.manager import get_mcp_manager
        await asyncio.wait_for(get_mcp_manager().shutdown(), timeout=0.5)
    except Exception:
        pass

    # 6. Telegram — 0.3s timeout
    try:
        from server.api.routes.telegram import disconnect_client
        await asyncio.wait_for(disconnect_client(), timeout=0.3)
    except Exception:
        pass

app = FastAPI(title="Sable API", version="0.4.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static assets must always revalidate — a stale cached app.js once served a
# broken markdown renderer while the server already had the fixed file.
@app.middleware("http")
async def no_cache_static(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static/") or request.url.path in ("/", "/index.html"):
        response.headers["Cache-Control"] = "no-cache"
    return response

if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

if UPLOAD_DIR.exists():
    app.mount("/system/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

if ASSETS_DIR.exists():
    app.mount("/assets", StaticFiles(directory=ASSETS_DIR), name="assets")

# ---------- Auth Middleware ----------
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if not path.startswith("/api/") or any(path.startswith(p) for p in AUTH_EXEMPT_PREFIXES):
        return await call_next(request)
    auth_header = request.headers.get("authorization", "")
    authorized = auth_header.startswith("Bearer ") and auth_header[7:] == _auth_mod.AUTH_TOKEN
    if not authorized and (
        path == "/api/logs"
        or path.endswith("/agent-events")
        or path.startswith("/api/research/events/")
    ):
        authorized = request.query_params.get("token", "") == _auth_mod.AUTH_TOKEN
    if not authorized:
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    return await call_next(request)

# Include routers
app.include_router(auth_router)
app.include_router(chats_router)
app.include_router(memory_router)
app.include_router(settings_router)
app.include_router(scraper_router)
app.include_router(upload_router)
app.include_router(deepseek_router)
app.include_router(chat_router)
app.include_router(misc_router)
app.include_router(agents_router)
app.include_router(library_router)
app.include_router(email_router)
app.include_router(filesystem_router)
app.include_router(terminal_router)
app.include_router(telegram_router)
app.include_router(research_router)
app.include_router(tracknote_router)
app.include_router(setup_router)
app.include_router(cookbook_router)

# Wire agent runtime event callback → SSE push
from .routes.agents import _async_push_agent_event, push_agent_event
from engine.agents import get_runtime as _get_agent_runtime
_get_agent_runtime().set_event_callback(_async_push_agent_event)

# Wire auto-turn engine: agent results → signal frontend to run a normal chat turn
from engine.agents.auto_turn import auto_turn as _auto_turn


async def _auto_turn_signal(chat_id: str, message: str) -> None:
    """Push an auto_turn_trigger event so the frontend initiates a normal /api/chat call."""
    push_agent_event(chat_id, {
        "type": "auto_turn_trigger",
        "agent_id": None,
        "data": {"message": message},
    })


_auto_turn.set_signal_fn(_auto_turn_signal)