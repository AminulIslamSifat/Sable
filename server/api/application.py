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
from server.database import init_db, recover_stale_agents
from server.auth import AUTH_TOKEN
from server.config import (
    AUTH_EXEMPT_PREFIXES, WEB_DIR, UPLOAD_DIR,
    _MEMORY_SEARCH_SETTINGS,
)
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

@asynccontextmanager
async def lifespan(app: FastAPI) -> Generator[None, None, None]:
    import asyncio as _aio
    from engine.agents import get_runtime as _get_rt_startup
    _get_rt_startup()._loop = _aio.get_running_loop()

    init_db()
    stale = recover_stale_agents()
    if stale:
        logger.info("Recovered %d stale agent(s) from previous session", stale)
    # Load saved role overrides into registry
    try:
        from engine.config import AGENT_CONFIG_PATH
        from engine.agents.registry import apply_role_overrides
        if AGENT_CONFIG_PATH.exists():
            _acfg = json.loads(AGENT_CONFIG_PATH.read_text(encoding="utf-8"))
            if _acfg.get("roles"):
                apply_role_overrides(_acfg["roles"])
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
    await service.warmup()
    try:
        ds_token = await service.refresh_deepseek_token()
        get_deepseek_client().set_token(ds_token)
    except Exception as exc:
        logger.warning("DeepSeek startup token refresh failed: %s: %s", type(exc).__name__, exc)
    yield
    await service.close()
    from engine.scraper import scraper as scraper_service
    await scraper_service.stop(kill_browser=True)

app = FastAPI(title="Sable API", version="0.4.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

if UPLOAD_DIR.exists():
    app.mount("/system/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

# ---------- Auth Middleware ----------
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if not path.startswith("/api/") or any(path.startswith(p) for p in AUTH_EXEMPT_PREFIXES):
        return await call_next(request)
    auth_header = request.headers.get("authorization", "")
    authorized = auth_header.startswith("Bearer ") and auth_header[7:] == AUTH_TOKEN
    if not authorized and (path == "/api/logs" or path.endswith("/agent-events")):
        authorized = request.query_params.get("token", "") == AUTH_TOKEN
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

# Wire agent runtime event callback → SSE push
from .routes.agents import _async_push_agent_event, push_agent_event
from engine.agents import get_runtime as _get_agent_runtime
_get_agent_runtime().set_event_callback(_async_push_agent_event)

# Wire auto-turn engine: agent results → autonomous model turns
from engine.agents.auto_turn import auto_turn as _auto_turn
from .dependencies import service as _chat_service


_AUTO_TURN_MAX_ROUNDS = 5


async def _auto_turn_fn(chat_id: str, messages: list[dict], on_chunk) -> str:
    """Skill-loop turn for agent results.

    Streams model output through SkillParser, executes any tool tags,
    and loops feedback up to _AUTO_TURN_MAX_ROUNDS times.
    parent_id is the UPSTREAM continuation token — NEVER a DB row id.
    """
    from engine.skills import get_skill_engine, build_tool_feedback
    from server.database import add_message, touch_chat, get_parent_id
    from server.utils import _is_deepseek_api_model

    notification_text = messages[-1]["content"] if messages else ""
    model, thinking_mode, provider = _auto_turn.get_chat_settings(chat_id)
    parent_id: str | None = get_parent_id(chat_id, None)

    # Frontend: a notification arrived
    push_agent_event(chat_id, {
        "type": "auto_turn_notification", "agent_id": None,
        "data": {"content": notification_text},
    })

    engine = get_skill_engine()
    all_answer_parts: list[str] = []
    all_skill_events: list[dict] = [{"type": "auto_turn", "trigger": "agent_completion"}]
    current_message = notification_text
    round_index = 0

    try:
        while round_index < _AUTO_TURN_MAX_ROUNDS:
            round_answer: list[str] = []
            round_skill_events: list[dict] = []
            parser = engine.create_parser()

            # Build event source for this round
            if provider == "deepseek" or (model and _is_deepseek_api_model(model)):
                from connectors.deepseek.client import get_client as _get_ds
                from engine.config import get_model_config
                ds_cfg = get_model_config(model) if model else {}
                api_type = ds_cfg.get("api_model_type", "default")
                event_source = _get_ds().stream_chat(
                    message=current_message, model=api_type,
                    thinking_mode=thinking_mode, chat_id=chat_id,
                )
            else:
                event_source = _chat_service.stream_events(
                    current_message, chat_id=chat_id, parent_id=parent_id,
                    model=model, thinking_mode=thinking_mode,
                )

            async for ev in event_source:
                ev_type = ev.get("type")
                if ev_type == "answer":
                    tok = ev.get("text", "")
                    if not tok:
                        continue
                    for item in parser.feed(tok):
                        itype = item.get("type")
                        if itype == "text":
                            chunk = str(item.get("text", ""))
                            if chunk:
                                round_answer.append(chunk)
                                await on_chunk(chunk)
                        elif itype == "tag_found":
                            for sev in engine.process_tag(
                                item["name"], item.get("attrs", {}), item.get("content", "")
                            ):
                                if sev.get("type") in ("skill_start", "skill_output", "skill_end", "file_edit"):
                                    round_skill_events.append(sev)
                                push_agent_event(chat_id, {"type": "auto_turn_skill", "agent_id": None, "data": sev})
                elif ev_type in ("meta", "done"):
                    parent_id = ev.get("parent_id") or parent_id
                elif ev_type == "error":
                    raise RuntimeError(ev.get("message", "stream error"))

            # Flush remaining parser buffer
            for item in parser.flush():
                itype = item.get("type")
                if itype == "text":
                    chunk = str(item.get("text", ""))
                    if chunk:
                        round_answer.append(chunk)
                        await on_chunk(chunk)
                elif itype == "tag_found":
                    for sev in engine.process_tag(
                        item["name"], item.get("attrs", {}), item.get("content", "")
                    ):
                        if sev.get("type") in ("skill_start", "skill_output", "skill_end", "file_edit"):
                            round_skill_events.append(sev)
                        push_agent_event(chat_id, {"type": "auto_turn_skill", "agent_id": None, "data": sev})

            all_answer_parts.extend(round_answer)
            all_skill_events.extend(round_skill_events)

            # Build feedback for next round
            feedback = build_tool_feedback(round_skill_events)
            if not feedback:
                break
            round_index += 1
            current_message = feedback

    finally:
        response_text = "".join(all_answer_parts)
        if response_text:
            add_message(
                chat_id, "assistant", response_text, None, parent_id,
                skill_events=all_skill_events,
            )
            touch_chat(chat_id, parent_id)
            push_agent_event(chat_id, {
                "type": "auto_turn_saved", "agent_id": None,
                "data": {"parent_id": parent_id},
            })
        await _auto_turn_done(chat_id)
    return response_text


async def _auto_turn_chunk(chat_id: str, token: str) -> None:
    """Push auto-turn tokens to frontend via agent-events SSE."""
    push_agent_event(chat_id, {"type": "auto_turn_chunk", "agent_id": None, "data": {"token": token}})


async def _auto_turn_done(chat_id: str) -> None:
    """Signal end of auto-turn stream."""
    push_agent_event(chat_id, {"type": "auto_turn_end", "agent_id": None, "data": {}})


_auto_turn.set_turn_fn(_auto_turn_fn)
_auto_turn.set_chunk_callback(_auto_turn_chunk)