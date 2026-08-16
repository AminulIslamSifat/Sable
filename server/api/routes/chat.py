from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse   # <-- corrected import
from engine.config import get_model_config
from engine.memory_search import get_searcher
from engine.scraper import get_settings as get_scraper_settings, scraper as scraper_service
from engine.skills import SkillEngine, SkillParser, build_tool_feedback
from engine.agents.resilience import MainChatGuard
from engine.skills.handlers import HANDLER_MAP
from connectors import get_connector
from connectors.deepseek.client import get_client as get_deepseek_client

_skill_engine: SkillEngine | None = None

def _get_skill_engine() -> SkillEngine:
    """Lazy singleton — avoids re-discovering skills on every request."""
    global _skill_engine
    if _skill_engine is None:
        _skill_engine = SkillEngine(
            skills_dir=Path(__file__).resolve().parent.parent.parent.parent / "skills",
            handlers=HANDLER_MAP,
            agent_id="maria",
        )
    return _skill_engine

from server.config import (
    SKILL_ROUND_WARN_THRESHOLD,
    _MEMORY_SEARCH_SETTINGS,
    _DEFAULT_MAX_PROMPT_CHARS,
)
from server.database import (
    ensure_chat, get_chat_mode, get_chat_provider,
    set_title_if_default, update_chat_title, get_injected_memory_keys, save_injected_memory_keys,
    touch_chat, save_chat_url, get_chat_url,
    add_message, update_message, get_messages, list_chats, delete_chat, get_parent_id, get_db,
)
from server.utils import retry_async, retry_stream, make_title, _is_deepseek_api_model, _resolve_api_backend, _is_api_model, logger
from server.models import ChatRequest
from ..dependencies import service, sse

# Backends that read local files directly (base64 inline) — no Playwright upload needed
_DIRECT_READ_BACKENDS = frozenset({"gemini", "groq", "mistral", "openai"})

# --- Conversation file logger ---
from engine.config import OUTPUT_ROOT as _OUT
_CONV_LOG_DIR = _OUT / "conversations"
_CONV_LOG_DIR.mkdir(parents=True, exist_ok=True)

def _log_conversation(chat_id: str, model: str, role: str, content: str) -> None:
    """Append user/assistant messages to a per-chat text file."""
    try:
        log_file = _CONV_LOG_DIR / f"{chat_id}.txt"
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] [{model}] [{role}]\n{content}\n{'='*60}\n\n")
    except Exception:
        pass  # Never let logging break the stream

router = APIRouter()

@router.post("/api/chat")
async def chat(request: ChatRequest):
    scraper_enabled = get_scraper_settings().get("enabled")
    active_chat_id = request.chat_id
    _upstream_session_id: str | None = None
    if not active_chat_id and scraper_enabled:
        active_chat_id = f"browser-{uuid.uuid4().hex}"
    if not active_chat_id:
        # Always generate a local Sable UUID for the chat.
        # Upstream session (Qwen) is created separately and stored in upstream_session_id.
        active_chat_id = uuid.uuid4().hex
        if not scraper_enabled and not _is_api_model(request.model):
            # Qwen model — create upstream session and store separately
            try:
                _upstream_session_id = await retry_async(
                    lambda: service.create_chat(model=request.model),
                    label="create_chat",
                )
            except Exception as exc:
                return {"error": f"Session startup failed: {type(exc).__name__}: {exc}"}
            if not _upstream_session_id:
                return {"error": "Could not create chat session"}
    _ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    _ctx_parts = ""
    if request.cwd:
        _ctx_parts += f" | cwd: {request.cwd}"
    if request.open_file:
        _ctx_parts += f" | file: {request.open_file}"
    timestamped_message = f"[{_ts}{_ctx_parts}]\n{request.message}"
    _memory_context = None  # injected before user message in api_message
    _injected_memory_keys = get_injected_memory_keys(active_chat_id)
    _ms_cfg: dict[str, Any] = {"enabled": True, "top_k": 10}
    _searcher = get_searcher()
    _memory_used: list[dict[str, Any]] = []

    # Cookbook per-model toggles for local models
    _local_use_memory = True
    _local_use_utilities = True
    _is_local_model = False
    try:
        _cfg_check = get_model_config(request.model)
        if _cfg_check.get("api_backend") == "local":
            _is_local_model = True
            from engine.cookbook.model_settings import get_model_settings as _get_cookbook_settings
            _cookbook_cfg = _get_cookbook_settings(request.model)
            _local_use_memory = _cookbook_cfg.get("use_memory", True)
            _local_use_utilities = _cookbook_cfg.get("use_utilities", True)
    except Exception:
        pass

    # Resolve project memory toggles early for category filtering
    _allowed_mem_cats: set[str] | None = None  # None = all categories
    _proj_mem_enabled = False
    _proj_mem_path = None
    try:
        from server.database import get_chat_project_id, get_project as _get_proj
        _early_proj_id = get_chat_project_id(active_chat_id)
        if _early_proj_id:
            _early_proj = _get_proj(_early_proj_id)
            if _early_proj:
                _use_univ = _early_proj.get("use_universal_memory", True)
                if not _use_univ:
                    # Universal off → only protected memory passes through
                    _allowed_mem_cats = {"protected"}
                if _early_proj.get("project_memory_enabled"):
                    _proj_mem_enabled = True
                    _proj_mem_path = Path(__file__).resolve().parent.parent.parent / "system" / "projects" / _early_proj_id / "Memory.json"
    except Exception:
        pass

    try:
        if _MEMORY_SEARCH_SETTINGS.exists():
            _ms_cfg = json.loads(_MEMORY_SEARCH_SETTINGS.read_text(encoding="utf-8"))
        # Auto-disable memory on low-RAM systems (< 8 GB)
        try:
            _total_ram_gb = (os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")) / (1024 ** 3)
            if _total_ram_gb < 8:
                _ms_cfg["enabled"] = False
        except (ValueError, OSError):
            pass
        _max_chars = _ms_cfg.get("max_prompt_chars", _DEFAULT_MAX_PROMPT_CHARS)
        if _ms_cfg.get("enabled", True) and _local_use_memory and len(request.message) <= _max_chars:
            _mem_results = _searcher.search(
                request.message,
                top_k=_ms_cfg.get("top_k", 10),
                allowed_categories=_allowed_mem_cats,
                top_memory=_ms_cfg.get("top_memory"),
                top_procedural=_ms_cfg.get("top_procedural"),
                top_total=_ms_cfg.get("top_total"),
            )
            _new_results = [r for r in _mem_results if r.get("key") and r["key"] not in _injected_memory_keys]
            if _new_results:
                _mem_block = _searcher.format_for_prompt(_new_results)
                if _mem_block:
                    _memory_context = _mem_block  # stored separately, injected before user msg
                    for r in _new_results:
                        _injected_memory_keys.add(r["key"])
                    save_injected_memory_keys(active_chat_id, _injected_memory_keys)
                    _memory_used = [
                        {
                            "key": r.get("key", ""),
                            "value": r.get("value", ""),
                            "category": r.get("category", ""),
                            "score": round(float(r.get("score", 0.0)), 3),
                        }
                        for r in _new_results
                    ]
        # Project-scoped memory search (vector-based via dedicated project searcher)
        if _proj_mem_enabled and _early_proj_id:
            try:
                from engine.memory_search import get_project_searcher
                _proj_searcher = get_project_searcher(_early_proj_id)
                _proj_results = _proj_searcher.search(request.message, top_k=5)
                _proj_new = [r for r in _proj_results if r.get("key") and r["key"] not in _injected_memory_keys]
                if _proj_new:
                    for r in _proj_new:
                        _injected_memory_keys.add(r["key"])
                        _memory_used.append({
                            "key": r.get("key", ""),
                            "value": r.get("value", ""),
                            "category": "project:" + r.get("category", "semantic"),
                            "score": round(float(r.get("score", 0.0)), 3),
                        })
                    save_injected_memory_keys(active_chat_id, _injected_memory_keys)
                    # Rebuild memory context with project entries included
                    _all_lines = ["[RELEVANT MEMORY CONTEXT]"]
                    for _mu in _memory_used:
                        if _mu["key"]:
                            _all_lines.append(f"- **{_mu['key']}**: {_mu['value']}")
                        else:
                            _all_lines.append(f"- {_mu['value']}")
                    _memory_context = "\n".join(_all_lines)
            except Exception:
                pass
    except Exception:
        pass

    current_mode = "scraper" if scraper_enabled else "api"
    locked_mode = get_chat_mode(active_chat_id)
    if locked_mode and locked_mode != current_mode:
        return {
            "error": f"This chat was created in {locked_mode} mode. "
                     f"Switch back to {locked_mode} mode or start a new chat."
        }
    if scraper_enabled:
        current_provider = "scraping"
    elif _is_api_model(request.model):
        current_provider = _resolve_api_backend(request.model)
    else:
        current_provider = "qwen"
    locked_provider = get_chat_provider(active_chat_id)
    if locked_provider and locked_provider != current_provider:
        # Qwen chats are locked to Qwen only (upstream session coupling).
        # API model chats (deepseek/gemini/groq/mistral/openai/local) can switch freely.
        _is_qwen_locked = locked_provider == "qwen" or current_provider == "qwen"
        if _is_qwen_locked:
            return {
                "error": f"This chat is locked to {locked_provider}. "
                         f"You can't use {current_provider} here — start a new chat."
            }
    title = make_title(request.message)
    ensure_chat(active_chat_id, title, request.parent_id, mode=current_mode, provider=current_provider, upstream_session_id=_upstream_session_id)
    # For existing chats, load upstream_session_id from DB if not already set
    if not _upstream_session_id:
        from server.database import get_upstream_session_id as _get_usid
        _upstream_session_id = _get_usid(active_chat_id)
    set_title_if_default(active_chat_id, title)
    # Server tail is authoritative — client leaf is fallback for first-message only
    parent_id = get_parent_id(active_chat_id, None) or request.parent_id
    # Reject bare integer parent_ids (DB row id leak from frontend — invalid upstream token)
    if parent_id and parent_id.isdigit():
        parent_id = None
    # Stash conversation settings for auto-turn delivery
    try:
        from engine.agents.auto_turn import auto_turn as _at
        _at.set_chat_settings(active_chat_id, request.model, request.thinking_mode, current_provider)
    except Exception:
        pass
    _user_msg_id: int | None = None
    if not request.skip_user_save:
        _user_msg_id = add_message(active_chat_id, "user", timestamped_message, None, parent_id, memory_used=_memory_used or None)
    # --- Checkpoint: capture project state BEFORE agent processes this turn ---
    if _user_msg_id and request.cwd:
        try:
            from engine.checkpoint import get_checkpoint_manager
            from server.database import save_checkpoint as _save_cp
            _cp_mgr = get_checkpoint_manager(request.cwd)
            _cp_sha = _cp_mgr.save_checkpoint(active_chat_id, _user_msg_id, "turn_start")
            if _cp_sha:
                _save_cp(active_chat_id, _user_msg_id, "turn_start", _cp_sha, request.cwd)
        except Exception:
            pass  # Checkpoints are best-effort; never break the chat flow
    # Separate API payload so injections don't leak into session history
    # Build context FIRST, user message always at the very end
    _context_parts: list[str] = []
    _msg_count = get_db().execute(
        "SELECT COUNT(*) as c FROM messages WHERE chat_id = ?", (active_chat_id,)
    ).fetchone()["c"]

    if _local_use_utilities and parent_id is None and _msg_count <= 1:
        _context_parts.append('[SYSTEM: You MUST call the chat_title tool now to set a title for this new conversation. This is mandatory.]')
        try:
            from server.database import get_upcoming_schedules
            _upcoming = get_upcoming_schedules(days=10)
            if _upcoming:
                _sched_lines = []
                for _s in _upcoming:
                    _stype = _s.get("schedule_type", "daily")
                    _time = _s.get("time", "")
                    _desc = _s.get("description", "")
                    _title = _s.get("title", "")
                    if _stype == "weekly":
                        _days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
                        _dow = _s.get("day_of_week", 0)
                        _day_name = _days[_dow] if 0 <= _dow <= 6 else "?"
                        _sched_lines.append(f"- {_title} ({_day_name} {_time})" + (f" \u2014 {_desc}" if _desc else ""))
                    elif _stype == "occasional":
                        _sd = _s.get("start_date", "")[:10]
                        _sched_lines.append(f"- {_title} ({_sd} {_time})" + (f" \u2014 {_desc}" if _desc else ""))
                    else:
                        _sched_lines.append(f"- {_title} (daily {_time})" + (f" \u2014 {_desc}" if _desc else ""))
                _sched_block = "\n".join(_sched_lines)
                _context_parts.append(f'[SCHEDULE CONTEXT \u2014 next 10 days:\n{_sched_block}]')
        except Exception:
            pass
    # Relevant memory context (already searched above)
    if _memory_context:
        _context_parts.append(_memory_context)
    # Assemble: context first, user message always last
    if _context_parts:
        api_message = "\n\n".join(_context_parts) + "\n\n" + timestamped_message
    else:
        api_message = timestamped_message

    resolved_files: list[dict[str, Any]] | None = None
    _backend = _resolve_api_backend(request.model) if _is_api_model(request.model) else None
    if request.files:
        resolved_files = []
        for f in request.files:
            if scraper_enabled:
                # Scraper mode: engine handles Playwright upload internally
                if "path" in f or "url" in f:
                    resolved_files.append(f)
                continue
            # Direct-read backends: connector reads local file, no server upload needed
            if _backend in _DIRECT_READ_BACKENDS:
                if "path" in f:
                    resolved_files.append({"path": f["path"]})
                continue
            # Provider-specific upload: DeepSeek has its own endpoint
            if _backend == "deepseek":
                if "path" in f:
                    meta = await service.upload_deepseek_file(f["path"])
                    if meta:
                        resolved_files.append(meta)
                    else:
                        logger.warning("DeepSeek upload failed for: %s", f["path"])
                continue
            # Qwen / others: upload to Qwen OSS
            if "id" in f and "url" in f:
                resolved_files.append(f)
            elif "path" in f:
                meta = await service.upload_image(f["path"])
                if meta:
                    resolved_files.append(meta)
                else:
                    logger.warning("Could not resolve file: %s", f["path"])
    if not request.stream and scraper_enabled:
        result = await scraper_service.chat(
            message=api_message,
            chat_id=active_chat_id,
            parent_id=parent_id,
            files=resolved_files,
            model=request.model,
            thinking_mode=request.thinking_mode,
        )
        answer = str(result.get("answer", ""))
        thinking = str(result.get("thinking", ""))
        final_parent = result.get("parent_id") or parent_id
        error = result.get("error")
        scraper_chat_url = result.get("chat_url")
        if scraper_chat_url:
            save_chat_url(active_chat_id, scraper_chat_url)
        add_message(active_chat_id, "assistant", answer or error or "", thinking, final_parent)
        touch_chat(active_chat_id, final_parent)
        result["memory_used"] = _memory_used
        return result
    # Resolve project_id for this chat (for instruction overrides) — shared by all paths
    _project_id = None
    try:
        from server.database import get_chat_project_id
        _project_id = get_chat_project_id(active_chat_id)
    except Exception:
        pass
    if not request.stream and _is_api_model(request.model):
        # _backend already resolved above at file resolution stage
        _api_backend = _backend or _resolve_api_backend(request.model)
        _connector = get_connector(_api_backend, model_id=request.model)
        _cfg = get_model_config(request.model)
        _api_model = _cfg.get("api_model_type", _cfg["id"])
        # DeepSeek Vision ephemeral: one-shot side request, no session continuity
        _ephemeral = (_api_backend == "deepseek" and _api_model == "vision" and bool(request.ref_file_ids))
        # Collect local file paths for direct-read backends (base64 inline)
        _inline_files = None
        if _api_backend in _DIRECT_READ_BACKENDS and resolved_files:
            _inline_files = [f.get('path') for f in resolved_files if f.get('path')]
        _max_session_chars = _cfg.get("max_session_chars")
        # Load DB history for cross-provider session seeding
        _db_history = None
        if not _ephemeral and active_chat_id:
            try:
                _db_msgs = get_messages(active_chat_id)
                _db_history = [{"role": m["role"], "content": m["content"]} for m in _db_msgs if m["role"] in ("user", "assistant") and m["content"]]
            except Exception:
                pass
        _chat_kwargs: dict[str, Any] = dict(
            message=api_message,
            model=_api_model,
            thinking_mode=request.thinking_mode,
            chat_id=None if _ephemeral else active_chat_id,
            ref_file_ids=request.ref_file_ids,
            inject_instructions=not _ephemeral,
            project_id=_project_id,
            db_history=_db_history,
        )
        if _api_backend == "local":
            _chat_kwargs["model_id"] = request.model
        if _max_session_chars:
            _chat_kwargs["max_session_chars"] = _max_session_chars
        if _inline_files:
            _chat_kwargs['files'] = _inline_files
        result = await _connector.chat(**_chat_kwargs)
        answer = str(result.get("answer", ""))
        thinking = str(result.get("thinking", ""))
        final_parent = result.get("parent_id") or parent_id
        error = result.get("error")
        add_message(active_chat_id, "assistant", answer or error or "", thinking, final_parent)
        touch_chat(active_chat_id, final_parent)
        result["memory_used"] = _memory_used
        return result
    if not request.stream and not scraper_enabled:
        result = await retry_async(
            lambda: service.chat(
                message=api_message,
                chat_id=active_chat_id,
                parent_id=parent_id,
                files=resolved_files,
                model=request.model,
                thinking_mode=request.thinking_mode,
            ),
            label="chat",
        )
        answer = str(result.get("answer", ""))
        thinking = str(result.get("thinking", ""))
        final_parent = result.get("parent_id") or parent_id
        error = result.get("error")
        add_message(active_chat_id, "assistant", answer or error or "", thinking, final_parent)
        touch_chat(active_chat_id, final_parent)
        result["memory_used"] = _memory_used
        return result
    async def _drain_sync_gen(gen):
        """Run a sync generator in a thread pool, yield results back without blocking the event loop."""
        queue: asyncio.Queue = asyncio.Queue()
        _sentinel = object()

        def _run():
            try:
                for item in gen:
                    queue.put_nowait(item)
            finally:
                queue.put_nowait(_sentinel)

        loop = asyncio.get_running_loop()
        # Cache the main loop on the agent runtime so thread-pool handlers can schedule coroutines
        from engine.agents import get_runtime as _get_rt
        _get_rt()._loop = loop
        # Propagate ContextVars (chat_id etc.) into the executor thread — Python 3.14 doesn't do this automatically
        import contextvars as _cv
        _ctx = _cv.copy_context()
        loop.run_in_executor(None, _ctx.run, _run)
        while True:
            item = await queue.get()
            if item is _sentinel:
                break
            yield item

    async def event_stream():
        nonlocal active_chat_id, _upstream_session_id
        answer_parts: list[str] = []
        _raw_answer_parts: list[str] = []  # pre-parser raw text for guard checks
        thinking_parts: list[str] = []
        skill_events: list[dict[str, Any]] = []
        final_parent = parent_id
        error_message: str | None = None
        current_message = api_message
        current_parent = parent_id
        round_index = 0
        saved_message_id: int | None = None
        _pending_skill_images: list[str] = []  # image paths from get_file to inject next round
        _guard = MainChatGuard()
        _all_tool_mem_used: list[dict[str, Any]] = []
        # Always send local chat_id to frontend (authoritative for new + existing chats)
        yield sse({"type": "meta", "chat_id": active_chat_id, "parent_id": parent_id})
        yield sse({"type": "status", "message": "processing"})
        if _user_msg_id:
            yield sse({"type": "user_message_id", "id": _user_msg_id})
        if _memory_used:
            yield sse({"type": "memory_used", "memories": _memory_used})
        try:
            # Set per-request chat_id via contextvar (safe for concurrent requests)
            from engine.agents import current_chat_id as _chat_id_var
            _chat_id_var.set(active_chat_id)
            # Block auto-turn from firing while main stream is active
            from engine.agents.auto_turn import auto_turn as _at_busy
            _at_busy.mark_stream_busy(active_chat_id)

            while True:
                round_skill_events: list[dict[str, Any]] = []
                round_thinking_parts: list[str] = []
                round_answer_parts: list[str] = []
                _round_raw_parts: list[str] = []  # per-round raw text for guard checks
                pending_thinking: list[str] = []
                parser = _get_skill_engine().create_parser()
                _title_buf = ""  # buffer for partial <title> tags in text stream
                _TITLE_RE = re.compile(r"<chat_title>(.*?)</chat_title>", re.S | re.I)
                def _dispatch_events(items) -> Generator[str, None, None]:
                    """Shared logic: route parser events, execute tags via engine."""
                    nonlocal _title_buf
                    engine = _get_skill_engine()
                    for item in items:
                        itype = item.get("type")
                        if itype == "text":
                            chunk = str(item.get("text", ""))
                            if not chunk:
                                continue
                            # Detect <title> tag in plain text stream (model may emit outside tool_call block)
                            _title_buf += chunk
                            m = _TITLE_RE.search(_title_buf)
                            if m:
                                _t = m.group(1).strip()
                                if _t:
                                    update_chat_title(active_chat_id, _t[:80])
                                    yield sse({"type": "chat_title", "title": _t[:80]})
                                # Emit proper skill events with ID so build_tool_feedback picks it up
                                _ct_id2 = str(uuid.uuid4())
                                round_skill_events.append({"type": "skill_start", "name": "chat_title", "id": _ct_id2})
                                round_skill_events.append({"type": "skill_end", "name": "chat_title", "ok": True, "id": _ct_id2, "duration_ms": 0})
                                _title_buf = _title_buf[:m.start()] + _title_buf[m.end():]
                            # Hold back partial <title at end of buffer
                            lt = _title_buf.rfind("<")
                            if lt >= 0 and ">" not in _title_buf[lt:] and "chat_title".startswith(_title_buf[lt:].lstrip("<").lower()):
                                chunk = _title_buf[:lt]
                                _title_buf = _title_buf[lt:]
                            else:
                                chunk = _title_buf
                                _title_buf = ""
                            if chunk:
                                answer_parts.append(chunk)
                                round_answer_parts.append(chunk)
                                yield sse({"type": "answer", "text": chunk})
                        elif itype == "tag_found":
                            # Meta tags: intercept before skill dispatch
                            if item["name"] == "chat_title":
                                _ct_id = str(uuid.uuid4())
                                _title_text = str(item.get("content", "")).strip()
                                if _title_text:
                                    update_chat_title(active_chat_id, _title_text[:80])
                                    yield sse({"type": "chat_title", "title": _title_text[:80]})
                                # Emit proper skill events with ID so build_tool_feedback
                                # generates feedback → auto-loop continues → model sends real text
                                round_skill_events.append({"type": "skill_start", "name": "chat_title", "id": _ct_id})
                                round_skill_events.append({"type": "skill_end", "name": "chat_title", "ok": True, "id": _ct_id, "duration_ms": 0})
                                continue

                            # Track command for loop detection
                            _guard.record_command(item["name"], item.get("content", ""))
                            # Execute the tag through the middleware pipeline
                            for ev in engine.process_tag(
                                item["name"], item.get("attrs", {}), item.get("content", ""),
                                chat_id=active_chat_id,
                            ):
                                if ev.get("type") in ("skill_start", "skill_output", "skill_end", "file_edit", "permission_request"):
                                    round_skill_events.append(ev)
                                yield sse(ev)
                                # When permission is requested, emit a transient status (not saved as answer)
                                if ev.get("type") == "permission_request":
                                    yield sse({"type": "approval_pending", "text": "⏳ Waiting for your approval on that command."})
                                # Detect simulacra completion → emit sim_ready card
                                if (ev.get("type") == "skill_end"
                                        and ev.get("name") == "run_simulacra"
                                        and ev.get("ok")):
                                    _fname = item.get("attrs", {}).get("filename", "simulation.html")
                                    if not _fname.endswith(".html"):
                                        _fname += ".html"
                                    yield sse({"type": "sim_ready", "filename": _fname})
                        elif itype == "parse_error":
                            # Parser couldn't parse the tool_call JSON — feed back to model
                            _pe_reason = item.get("reason", "Malformed tool_call block")
                            _pe_raw = item.get("raw", "")[:200]
                            round_skill_events.append({
                                "type": "skill_end",
                                "name": "action_parse",
                                "ok": False,
                                "error": f"{_pe_reason} | Received: {_pe_raw}",
                            })
                            yield sse({"type": "skill_output", "name": "action_parse", "text": f"⚠️ {_pe_reason}"})
                        else:
                            # tool_pending, tool_progress, etc — forward to frontend
                            if itype in ("skill_start", "skill_output", "skill_end", "file_edit", "permission_request"):
                                round_skill_events.append(item)
                            yield sse(item)
                def emit_parsed(text: str) -> Generator[str, None, None]:
                    yield from _dispatch_events(parser.feed(text))
                def emit_flush() -> Generator[str, None, None]:
                    nonlocal _title_buf
                    yield from _dispatch_events(parser.flush())
                    # Flush any remaining title buffer text
                    if _title_buf:
                        leftover = _title_buf
                        _title_buf = ""
                        answer_parts.append(leftover)
                        round_answer_parts.append(leftover)
                        yield sse({"type": "answer", "text": leftover})
                files_for_round = resolved_files if round_index == 0 else None
                # For non-API models (Qwen/scraper): upload pending skill images to Qwen OSS
                if _pending_skill_images and not _is_api_model(request.model):
                    _uploaded_metas = []
                    for _img_path in _pending_skill_images:
                        try:
                            _meta = await service.upload_image(_img_path)
                            if _meta:
                                _uploaded_metas.append(_meta)
                            else:
                                logger.warning("Qwen image upload failed for: %s", _img_path)
                        except Exception as _up_exc:
                            logger.warning("Qwen image upload error for %s: %s", _img_path, _up_exc)
                    if _uploaded_metas:
                        files_for_round = _uploaded_metas
                    else:
                        current_message += (
                            f"\n\n[NOTE: {len(_pending_skill_images)} image(s) were produced by tools "
                            f"but could not be uploaded for this model. "
                            f"The image content is not accessible.]"
                        )
                    _pending_skill_images = []
                # Drain pending agent notifications into this turn's context
                try:
                    from engine.agents.notifications import notification_queue as _nq
                    _pending_notifs = _nq.drain(active_chat_id)
                    if _pending_notifs:
                        _notif_lines = []
                        for _nev in _pending_notifs:
                            _nd = _nev.data or {}
                            _status = "completed" if _nev.type == "agent_completed" else "failed"
                            _skill_list = ', '.join(_nd.get('skills_used', [])) or 'none'
                            _summary = (_nd.get('result', '') or _nd.get('summary', '') or _nd.get('error', ''))[:500]
                            _notif_lines.append(
                                f"### Agent {_nev.agent_id} ({_nd.get('role', 'agent')}) — {_status}\n\n"
                                f"- **Words:** {_nd.get('words', 0)}\n"
                                f"- **Duration:** {_nd.get('duration', 0):.1f}s\n"
                                f"- **Skills:** {_skill_list}\n\n"
                                f"{_summary}"
                            )
                        current_message = (
                            "[Agent Notifications]\n" + "\n".join(_notif_lines)
                            + "\n\n" + current_message
                        )
                except Exception:
                    pass
                stream_error = False
                _cmd_history_start = len(_guard._command_history)
                # Log user message to file
                if round_index == 0:
                    _log_conversation(active_chat_id, request.model, "user", current_message)
                if _is_api_model(request.model):
                    _api_backend = _backend or _resolve_api_backend(request.model)
                    _connector = get_connector(_api_backend, model_id=request.model)
                    _cfg = get_model_config(request.model)
                    _api_model = _cfg.get("api_model_type", _cfg["id"])
                    _ephemeral = (_api_backend == "deepseek" and _api_model == "vision" and bool(request.ref_file_ids))
                    # Collect local file paths for direct-read backends
                    _inline_files = None
                    if _api_backend in _DIRECT_READ_BACKENDS and round_index == 0 and resolved_files:
                        _inline_files = [f.get('path') for f in resolved_files if f.get('path')]
                    # Inject skill-produced images (from get_file) if model supports vision
                    if _pending_skill_images:
                        _caps = _cfg.get("capabilities", {})
                        if _caps.get("image", False) and _api_backend in _DIRECT_READ_BACKENDS:
                            _inline_files = (_inline_files or []) + _pending_skill_images
                        else:
                            # Model can't see images or backend doesn't support inline files
                            _reason = "does not support image input" if not _caps.get("image", False) else "does not support inline file injection"
                            current_message += (
                                f"\n\n[NOTE: {len(_pending_skill_images)} image(s) were produced by tools "
                                f"but this model ({request.model}) {_reason}. "
                                f"The image content is not accessible.]"
                            )
                        _pending_skill_images = []
                    _max_session_chars_stream = _cfg.get("max_session_chars")
                    # Load DB history for cross-provider session seeding (first round only)
                    _db_history_s = None
                    if not _ephemeral and round_index == 0 and active_chat_id:
                        try:
                            _db_msgs_s = get_messages(active_chat_id)
                            _db_history_s = [{"role": m["role"], "content": m["content"]} for m in _db_msgs_s if m["role"] in ("user", "assistant") and m["content"]]
                        except Exception:
                            pass
                    _stream_kwargs: dict[str, Any] = dict(
                        message=current_message,
                        model=_api_model,
                        thinking_mode=request.thinking_mode,
                        chat_id=None if _ephemeral else active_chat_id,
                        ref_file_ids=request.ref_file_ids if round_index == 0 else None,
                        inject_instructions=not _ephemeral,
                        project_id=_project_id,
                        db_history=_db_history_s,
                    )
                    if _api_backend == "local":
                        _stream_kwargs["model_id"] = request.model
                    if _max_session_chars_stream:
                        _stream_kwargs["max_session_chars"] = _max_session_chars_stream
                    if _inline_files:
                        _stream_kwargs['files'] = _inline_files
                    # Native tool calling: load and pass tool schemas
                    try:
                        from engine.tools_loader import get_all_tool_schemas
                        from server.api.routes.misc import get_disabled_tools as _get_dt
                        _disabled = _get_dt().get('disabled', [])
                        # For local models, filter to per-model configured tools
                        _allowed_tools = None
                        if _is_local_model:
                            _model_tools = _cookbook_cfg.get("tools")
                            if _model_tools is not None:
                                # Explicit list (even empty) = use only those tools
                                _allowed_tools = _model_tools if _model_tools else ["__none__"]
                        _tool_schemas = get_all_tool_schemas(_disabled, allowed=_allowed_tools)
                        if _tool_schemas:
                            _stream_kwargs['tools'] = _tool_schemas
                    except Exception:
                        pass  # Tools optional — fall back to text-based tool_call blocks
                    round_event_source = _connector.stream_chat(**_stream_kwargs)
                elif scraper_enabled:
                    round_event_source = scraper_service.stream_events(
                        message=current_message,
                        chat_id=active_chat_id,
                        parent_id=current_parent,
                        files=files_for_round,
                        model=request.model,
                        thinking_mode=request.thinking_mode,
                    )
                else:
                    # Qwen uses upstream_session_id for the server-side session;
                    # active_chat_id is the local Sable UUID.
                    _qwen_chat_id = _upstream_session_id or active_chat_id
                    round_event_source = retry_stream(
                        lambda: service.stream_events(
                            message=current_message,
                            chat_id=_qwen_chat_id,
                            parent_id=current_parent,
                            files=files_for_round,
                            model=request.model,
                            thinking_mode=request.thinking_mode,
                        ),
                        label=f"stream_round_{round_index}",
                    )
                async for event in round_event_source:
                    event_type = event.get("type")
                    # Suppress upstream meta events — local chat_id is authoritative
                    if event_type == "meta":
                        continue
                    if event_type == "answer":
                        pending_thinking.clear()
                        _raw_chunk = str(event.get("text", ""))
                        _raw_answer_parts.append(_raw_chunk)
                        _round_raw_parts.append(_raw_chunk)
                        async for _sse_line in _drain_sync_gen(emit_parsed(_raw_chunk)):
                            yield _sse_line
                        continue
                    if event_type == "thinking":
                        chunk = str(event.get("text", ""))
                        thinking_parts.append(chunk)
                        round_thinking_parts.append(chunk)
                        pending_thinking.append(chunk)
                        yield sse({"type": "thinking", "text": chunk})
                        continue
                    elif event_type == "done":
                        pending_thinking.clear()
                        async for _sse_line in _drain_sync_gen(emit_flush()):
                            yield _sse_line
                        final_parent = event.get("parent_id") or final_parent
                        current_parent = final_parent
                    elif event_type == "chat_not_found":
                        # Upstream Qwen session expired — create new one, inject full history, retry
                        from engine.session import create_new_chat as _create_qwen_chat
                        logger.warning("[session-recovery] CHAT_NOT_FOUND triggered for chat_id=%s (upstream=%s)", active_chat_id, _upstream_session_id)
                        yield sse({"type": "status", "message": "recovering_session"})
                        _new_headers = await service._ensure_headers()
                        _new_qwen_id = await _create_qwen_chat(_new_headers, model=request.model)
                        if not _new_qwen_id:
                            _new_headers = await service._refresh_headers()
                            _new_qwen_id = await _create_qwen_chat(_new_headers, model=request.model)
                        if _new_qwen_id:
                            logger.info("[session-recovery] New upstream session: %s -> %s (local chat stays %s)", _upstream_session_id, _new_qwen_id, active_chat_id)
                            # Update upstream_session_id in DB; local chat_id stays the same
                            from server.database import set_upstream_session_id as _set_usid
                            _set_usid(active_chat_id, _new_qwen_id)
                            _upstream_session_id = _new_qwen_id
                            # Fetch full conversation history including tool calls/results
                            _prev_msgs = get_messages(active_chat_id, include_skill_events=True)
                            _history_lines = []
                            for _pm in _prev_msgs:
                                if _pm["role"] not in ("user", "assistant"):
                                    continue
                                if _pm["content"]:
                                    _history_lines.append(f"[{_pm['role']}]: {_pm['content']}")
                                # Append tool calls and results from skill_events
                                for _sev in (_pm.get("skill_events") or []):
                                    _sev_type = _sev.get("type", "")
                                    if _sev_type == "skill_start":
                                        _tc_name = _sev.get("name", "unknown")
                                        _tc_attrs = _sev.get("data", {}).get("attrs", {})
                                        _history_lines.append(f"[tool_call]: {_tc_name}({_tc_attrs})")
                                    elif _sev_type == "skill_end":
                                        _tr_name = _sev.get("name", "unknown")
                                        _tr_ok = _sev.get("ok", True)
                                        _tr_error = _sev.get("error")
                                        _tr_result = str(_sev.get("result", ""))[:2000]
                                        if _tr_error:
                                            _history_lines.append(f"[tool_result]: {_tr_name} (ok={_tr_ok}): ERROR: {_tr_error}")
                                        else:
                                            _history_lines.append(f"[tool_result]: {_tr_name} (ok={_tr_ok}): {_tr_result}")
                            _history_injected = len(_history_lines) > 0
                            logger.info("[session-recovery] History injected: %d lines from %d messages for chat %s",
                                        len(_history_lines), len(_prev_msgs), active_chat_id)
                            _history_block = ""
                            if _history_injected:
                                _history_block = "[PREVIOUS CONVERSATION]\n" + "\n".join(_history_lines) + "\n[END PREVIOUS CONVERSATION]\n\n"
                            _recovery_msg = _history_block + current_message
                            # Re-stream with new upstream session
                            round_event_source = service.stream_events(
                                message=_recovery_msg,
                                chat_id=_upstream_session_id,
                                parent_id=None,
                                files=files_for_round,
                                model=request.model,
                                thinking_mode=request.thinking_mode,
                            )
                            # No meta event needed — local chat_id hasn't changed
                            async for _recovery_event in round_event_source:
                                _rec_type = _recovery_event.get("type")
                                if _rec_type == "meta":
                                    # Suppress upstream meta, we already sent ours
                                    continue
                                if _rec_type == "answer":
                                    pending_thinking.clear()
                                    _raw_chunk = str(_recovery_event.get("text", ""))
                                    _raw_answer_parts.append(_raw_chunk)
                                    _round_raw_parts.append(_raw_chunk)
                                    async for _sse_line in _drain_sync_gen(emit_parsed(_raw_chunk)):
                                        yield _sse_line
                                    continue
                                if _rec_type == "thinking":
                                    _chunk = str(_recovery_event.get("text", ""))
                                    thinking_parts.append(_chunk)
                                    round_thinking_parts.append(_chunk)
                                    pending_thinking.append(_chunk)
                                    yield sse({"type": "thinking", "text": _chunk})
                                    continue
                                if _rec_type == "done":
                                    pending_thinking.clear()
                                    async for _sse_line in _drain_sync_gen(emit_flush()):
                                        yield _sse_line
                                    final_parent = _recovery_event.get("parent_id") or final_parent
                                    current_parent = final_parent
                                elif _rec_type == "error":
                                    pending_thinking.clear()
                                    async for _sse_line in _drain_sync_gen(emit_flush()):
                                        yield _sse_line
                                    error_message = str(_recovery_event.get("message", "Unknown error"))
                                    stream_error = True
                                yield sse(_recovery_event)
                            # Skip normal post-round processing since we handled it inline
                            break
                        else:
                            logger.error("[session-recovery] FAILED to create new upstream session for chat_id=%s", _old_chat_id)
                            error_message = "Failed to recover: could not create new upstream session"
                            stream_error = True
                    elif event_type == "parent_not_found":
                        # Stale parent_id — chat still exists upstream, just retry with parent_id=None
                        logger.warning("[parent-recovery] PARENT_NOT_FOUND for chat_id=%s, retrying with parent_id=None", active_chat_id)
                        yield sse({"type": "status", "message": "recovering_parent"})
                        # Clear stale parent_id from DB
                        touch_chat(active_chat_id, None)
                        current_parent = None
                        final_parent = None
                        # Re-stream same message with no parent
                        round_event_source = service.stream_events(
                            message=current_message,
                            chat_id=_upstream_session_id,
                            parent_id=None,
                            files=files_for_round,
                            model=request.model,
                            thinking_mode=request.thinking_mode,
                        )
                        async for _recovery_event in round_event_source:
                            _rec_type = _recovery_event.get("type")
                            if _rec_type == "meta":
                                continue
                            if _rec_type == "answer":
                                pending_thinking.clear()
                                _raw_chunk = str(_recovery_event.get("text", ""))
                                _raw_answer_parts.append(_raw_chunk)
                                _round_raw_parts.append(_raw_chunk)
                                async for _sse_line in _drain_sync_gen(emit_parsed(_raw_chunk)):
                                    yield _sse_line
                                continue
                            if _rec_type == "thinking":
                                _chunk = str(_recovery_event.get("text", ""))
                                thinking_parts.append(_chunk)
                                round_thinking_parts.append(_chunk)
                                pending_thinking.append(_chunk)
                                yield sse({"type": "thinking", "text": _chunk})
                                continue
                            if _rec_type == "done":
                                pending_thinking.clear()
                                async for _sse_line in _drain_sync_gen(emit_flush()):
                                    yield _sse_line
                                final_parent = _recovery_event.get("parent_id") or final_parent
                                current_parent = final_parent
                            elif _rec_type == "error":
                                pending_thinking.clear()
                                async for _sse_line in _drain_sync_gen(emit_flush()):
                                    yield _sse_line
                                error_message = str(_recovery_event.get("message", "Unknown error"))
                                stream_error = True
                            yield sse(_recovery_event)
                        break
                    elif event_type == "error":
                        pending_thinking.clear()
                        async for _sse_line in _drain_sync_gen(emit_flush()):
                            yield _sse_line
                        error_message = str(event.get("message", "Unknown error"))
                        stream_error = True
                    elif event_type == "rate_limited":
                        pending_thinking.clear()
                        async for _sse_line in _drain_sync_gen(emit_flush()):
                            yield _sse_line
                        hours = event.get("hours", "?")
                        details = event.get("message", "Daily usage limit reached.")
                        error_message = f"⏳ Rate Limited — {details} (retry in {hours}h)"
                        stream_error = True
                    yield sse(event)
                round_thinking_text = "".join(round_thinking_parts)
                if round_thinking_text:
                    skill_events.append({"type": "round_thinking", "text": round_thinking_text})
                round_text = "".join(round_answer_parts)
                if round_text:
                    skill_events.append({"type": "round_text", "text": round_text})
                if round_skill_events:
                    skill_events.extend(round_skill_events)
                # --- MainChatGuard: track failures from this round ---
                for _sev in round_skill_events:
                    if _sev.get("type") == "skill_end":
                        _guard.record_result(_sev.get("ok", False))
                round_answer = "".join(answer_parts)
                round_thinking = "".join(thinking_parts)
                stored = round_answer or error_message or ""
                # Log assistant response to file
                if stored:
                    _log_conversation(active_chat_id, request.model, "assistant", stored)
                if saved_message_id is None:
                    saved_message_id = add_message(
                        active_chat_id, "assistant", stored, round_thinking, final_parent, skill_events,
                        memory_used=_all_tool_mem_used or None,
                    )
                else:
                    update_message(saved_message_id, stored, round_thinking, final_parent, skill_events,
                                   memory_used=_all_tool_mem_used or None)
                # --- Collect mode: await agents spawned with collect="true" ---
                _collect_ids: list[str] = []
                for _sev in round_skill_events:
                    if _sev.get("type") == "skill_end" and _sev.get("name") == "spawn_agent":
                        _res = _sev.get("result") or {}
                        # Register ALL spawned agents in current stream for notification dedup
                        if _res.get("agent_id"):
                            try:
                                from engine.agents.auto_turn import auto_turn as _at_ref
                                _at_ref.register_stream_agent(active_chat_id, _res["agent_id"])
                            except Exception:
                                pass
                        if _res.get("collect") and _res.get("agent_id"):
                            _collect_ids.append(_res["agent_id"])
                if _collect_ids:
                    try:
                        from engine.agents import get_runtime as _get_rt
                        _rt = _get_rt()
                        yield sse({"type": "status", "message": "waiting_for_agents", "ids": _collect_ids})
                        _collect_results = await _rt.wait_all(_collect_ids, timeout=300)
                        _collect_lines = []
                        for _cr in _collect_results:
                            if _cr.success:
                                _collect_lines.append(
                                    f"[Agent {_cr.agent_id} ({_cr.role}) result]:\n{_cr.summary}"
                                )
                            else:
                                _collect_lines.append(
                                    f"[Agent {_cr.agent_id} ({_cr.role}) FAILED]: {_cr.error}"
                                )
                        # Inject collected results into skill events as synthetic output
                        round_skill_events.append({
                            "type": "skill_output",
                            "name": "wait_agents",
                            "output": "\n\n---\n\n".join(_collect_lines),
                        })
                    except asyncio.TimeoutError:
                        round_skill_events.append({
                            "type": "skill_output",
                            "name": "wait_agents",
                            "output": f"TIMEOUT: Agents {', '.join(_collect_ids)} did not complete within 300s.",
                        })
                    except Exception as _cexc:
                        round_skill_events.append({
                            "type": "skill_output",
                            "name": "wait_agents",
                            "output": f"COLLECT ERROR: {type(_cexc).__name__}: {_cexc}",
                        })

                # ask_user pause: stop the skill loop, wait for user's next message
                _ask_user_pause = any(
                    ev.get("type") == "skill_end"
                    and (ev.get("result") or {}).get("pause")
                    for ev in round_skill_events
                )
                # permission_request pause: stop the stream so the user can approve/deny
                # before the model continues. The approve/deny endpoints handle re-execution.
                _permission_pause = any(
                    ev.get("type") == "permission_request"
                    for ev in round_skill_events
                )
                # --- MainChatGuard: inject warnings into feedback ---
                _guard_warnings: list[str] = []
                _guard_warnings_injected = False
                _loop_warn = _guard.check_loop()
                if _loop_warn:
                    _guard_warnings.append(_loop_warn)
                _fail_warn = _guard.check_failures()
                if _fail_warn:
                    _guard_warnings.append(_fail_warn)
                # Check malformed/incomplete using RAW text (before parser strips tags)
                _raw_round_text = "".join(_round_raw_parts)
                _malform_warn = _guard.check_malformed_action(_raw_round_text)
                if _malform_warn:
                    _guard_warnings.append(_malform_warn)
                # Only check incomplete if malformed didn't already catch it
                if not _malform_warn:
                    _incomplete_warn = _guard.check_incomplete_action(
                        _raw_round_text,
                        any(ev.get("type") == "skill_end" for ev in round_skill_events),
                    )
                    if _incomplete_warn:
                        _guard_warnings.append(_incomplete_warn)
                feedback = build_tool_feedback(round_skill_events)
                # Extract image paths from skill results for multimodal injection next round
                for _ev in round_skill_events:
                    if _ev.get("type") == "skill_end" and _ev.get("ok"):
                        _res = _ev.get("result", {})
                        if _res.get("kind") == "image" and _res.get("path"):
                            _pending_skill_images.append(_res["path"])
                # Truncate oversized tool output to protect context window
                from engine.agents.loop import _get_max_tool_output_chars
                from engine.skills.events import middle_truncate
                _tool_cap = _get_max_tool_output_chars()
                if feedback and len(feedback) > _tool_cap:
                    feedback = middle_truncate(feedback, _tool_cap)
                # If no tool feedback but guard warnings exist, use warnings as feedback
                # so the model sees them and self-corrects (auto-continue, no break)
                if not feedback and _guard_warnings:
                    feedback = "\n\n".join(_guard_warnings)
                    _guard_warnings_injected = True
                if stream_error or error_message or not feedback or _ask_user_pause or _permission_pause:
                    break


                try:
                    _max_chars_tool = _ms_cfg.get("max_prompt_chars", _DEFAULT_MAX_PROMPT_CHARS)
                    if _ms_cfg.get("enabled", True) and feedback and len(feedback) <= _max_chars_tool:
                        _top_k = _ms_cfg.get("top_k", 10)
                        _tm = _ms_cfg.get("top_memory")
                        _tp = _ms_cfg.get("top_procedural")
                        _tt = _ms_cfg.get("top_total")
                        # Multi-source memory search: tool result + model response + tool call inputs
                        _search_queries: list[str] = [feedback[:800]]
                        # Source 2: model response text this round
                        _round_resp = "".join(round_answer_parts).strip()
                        if _round_resp:
                            _search_queries.append(_round_resp[:500])
                        # Source 3: tool call inputs from this round
                        _round_cmds = _guard._command_history[_cmd_history_start:]
                        for _cmd_sig in _round_cmds[:5]:  # cap to avoid excessive searches
                            _search_queries.append(_cmd_sig[:300])
                        # Search all sources, merge and deduplicate
                        _all_mem_results: list[dict[str, Any]] = []
                        _seen_mem_keys: set[str] = set()
                        for _q in _search_queries:
                            if not _q.strip():
                                continue
                            try:
                                for r in _searcher.search(_q, top_k=_top_k, allowed_categories=_allowed_mem_cats, top_memory=_tm, top_procedural=_tp, top_total=_tt):
                                    _mk = r.get("key", "")
                                    if _mk and _mk not in _seen_mem_keys and _mk not in _injected_memory_keys:
                                        _seen_mem_keys.add(_mk)
                                        _all_mem_results.append(r)
                            except Exception:
                                continue
                        # Rank by score, take top_k overall
                        _all_mem_results.sort(key=lambda r: float(r.get("score", 0.0)), reverse=True)
                        _new_mem = _all_mem_results[:_top_k]
                        if _new_mem:
                            _tool_block = _searcher.format_for_prompt(_new_mem)
                            if _tool_block:
                                feedback = f"{_tool_block}\n\n{feedback}"
                                for r in _new_mem:
                                    _injected_memory_keys.add(r["key"])
                                save_injected_memory_keys(active_chat_id, _injected_memory_keys)
                                _tool_mem_used = [
                                    {
                                        "key": r.get("key", ""),
                                        "value": r.get("value", ""),
                                        "category": r.get("category", ""),
                                        "score": round(float(r.get("score", 0.0)), 3),
                                    }
                                    for r in _new_mem
                                ]
                                _all_tool_mem_used.extend(_tool_mem_used)
                                skill_events.append(
                                    {"type": "memory_used", "memories": _tool_mem_used, "round": round_index}
                                )
                                yield sse(
                                    {
                                        "type": "memory_used",
                                        "memories": _tool_mem_used,
                                        "source": "tool",
                                        "round": round_index,
                                    }
                                )
                except Exception:
                    pass
                if round_index >= SKILL_ROUND_WARN_THRESHOLD:
                    logger.warning(
                        "chat %s reached %d skill rounds — still running but worth checking",
                        active_chat_id, round_index,
                    )
                    yield sse({"type": "status", "message": "high_skill_round_count", "round": round_index})
                round_index += 1
                if _guard_warnings and not _guard_warnings_injected:
                    # Only prepend warnings if they weren't already used as feedback
                    _warn_block = "\n\n".join(_guard_warnings)
                    current_message = _warn_block + "\n\n" + (feedback or "")
                else:
                    current_message = feedback

                current_parent = final_parent
                yield sse(
                    {
                        "type": "status",
                        "message": "feeding_skill_results",
                        "round": round_index,
                    }
                )
        except Exception as exc:
            error_message = f"{type(exc).__name__}: {exc}"
            try:
                yield sse({"type": "error", "message": f"Server error: {error_message}"})
            except Exception:
                pass
        finally:
            answer = "".join(answer_parts)
            thinking = "".join(thinking_parts)
            if not answer and skill_events:
                summary = []
                for evt in skill_events:
                    if evt.get("type") == "skill_start":
                        summary.append(f"[skill] {evt.get('name', 'skill')}")
                    elif evt.get("type") == "skill_end":
                        status = "ok" if evt.get("ok") else "error"
                        summary.append(f"[{status}] {evt.get('name', 'skill')}")
                answer = "\n".join(summary)
            # MainChatGuard: no-op in finally — mid-loop auto-continue handles all guard feedback.
            # Warnings are injected via _guard_warnings -> current_message at end of each round.
            stored_content = answer or error_message or ""
            if saved_message_id is not None:
                update_message(saved_message_id, stored_content, thinking, final_parent, skill_events)
            else:
                add_message(active_chat_id, "assistant", stored_content, thinking, final_parent, skill_events)
            touch_chat(active_chat_id, final_parent)
            # Release auto-turn lock — drains any queued agent results
            try:
                from engine.agents.auto_turn import auto_turn as _at_done
                _at_done.mark_stream_done(active_chat_id)
            except Exception:
                pass
    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ─── Approval Gate Endpoints ──────────────────────────────────────────────────

@router.post("/api/skills/approve/{tag_id}")
async def approve_command(tag_id: str, request: Request):
    """User approved a pending command — execute and return tool feedback."""
    from engine.security.middleware import consume_pending_approval, cache_session_permission

    body = await request.json() if request else {}
    chat_id = body.get("chat_id")
    session_approve = body.get("session", False)

    pending = consume_pending_approval(tag_id)
    if pending is None:
        return {"ok": False, "error": "Approval expired or not found"}

    # Cache category for this session if "Allow for Session" was clicked
    if session_approve and chat_id:
        cache_session_permission(chat_id, pending.category)

    engine = _get_skill_engine()
    attrs = {**pending.attrs, "approved": "true"}

    loop = asyncio.get_event_loop()
    def _run():
        return list(engine.process_tag(pending.name, attrs, pending.content, chat_id=chat_id))
    try:
        events = await loop.run_in_executor(None, _run)
    except Exception as exc:
        events = [{"type": "skill_end", "id": tag_id, "name": pending.name, "ok": False, "error": str(exc)}]

    # Build tool feedback in standard format
    from engine.skills.events import build_tool_feedback
    # Prepend skill_start if missing
    has_start = any(ev.get("type") == "skill_start" for ev in events)
    if not has_start:
        events = [{"type": "skill_start", "id": tag_id, "name": pending.name}] + events
    feedback = build_tool_feedback(events) or f"[{pending.name}] OK"

    # Save as skill_event attached to the last assistant message
    from server.database import append_skill_event
    if chat_id:
        for ev in events:
            append_skill_event(chat_id, ev)

    return {"ok": True, "feedback": feedback, "session_cached": session_approve}


@router.post("/api/skills/deny/{tag_id}")
async def deny_command(tag_id: str, request: Request):
    """User denied a pending command — return tool feedback."""
    from engine.security.middleware import consume_pending_approval

    pending = consume_pending_approval(tag_id)
    if pending is None:
        return {"ok": False, "error": "Not found or expired"}

    cmd_preview = pending.content[:200]
    feedback = f"[{pending.name}] FAILED — User denied this command: {cmd_preview}"

    # Save as skill_event attached to the last assistant message
    from server.database import append_skill_event
    chat_id = (await request.json()).get("chat_id") if request else None
    if chat_id:
        append_skill_event(chat_id, {
            "type": "skill_start", "id": tag_id, "name": pending.name,
        })
        append_skill_event(chat_id, {
            "type": "skill_end", "id": tag_id, "name": pending.name,
            "ok": False, "error": "User denied this command",
        })

    return {"ok": True, "feedback": feedback}