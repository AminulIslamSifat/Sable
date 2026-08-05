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
    add_message, update_message, get_messages, list_chats, delete_chat, get_parent_id,
)
from server.utils import retry_async, retry_stream, make_title, _is_deepseek_api_model, _resolve_api_backend, _is_api_model, logger
from server.models import ChatRequest
from ..dependencies import service, sse

# Backends that read local files directly (base64 inline) — no Playwright upload needed
_DIRECT_READ_BACKENDS = frozenset({"gemini", "groq", "mistral"})

router = APIRouter()

@router.post("/api/chat")
async def chat(request: ChatRequest):
    scraper_enabled = get_scraper_settings().get("enabled")
    active_chat_id = request.chat_id
    if not active_chat_id and scraper_enabled:
        active_chat_id = f"browser-{uuid.uuid4().hex}"
    if not active_chat_id:
        try:
            active_chat_id = await retry_async(
                lambda: service.create_chat(model=request.model),
                label="create_chat",
            )
        except Exception as exc:
            return {"error": f"Session startup failed: {type(exc).__name__}: {exc}"}
        if not active_chat_id:
            return {"error": "Could not create chat session"}
    _ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    _ctx_parts = ""
    if request.cwd:
        _ctx_parts += f" | cwd: {request.cwd}"
    if request.open_file:
        _ctx_parts += f" | file: {request.open_file}"
    timestamped_message = f"[{_ts}{_ctx_parts}]\n{request.message}"
    _injected_memory_keys = get_injected_memory_keys(active_chat_id)
    _ms_cfg: dict[str, Any] = {"enabled": True, "top_k": 10}
    _searcher = get_searcher()
    _memory_used: list[dict[str, Any]] = []
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
        if _ms_cfg.get("enabled", True) and len(request.message) <= _max_chars:
            _mem_results = _searcher.search(request.message, top_k=_ms_cfg.get("top_k", 10))
            _new_results = [r for r in _mem_results if r.get("key") and r["key"] not in _injected_memory_keys]
            if _new_results:
                _mem_block = _searcher.format_for_prompt(_new_results)
                if _mem_block:
                    timestamped_message = f"{_mem_block}\n\n{timestamped_message}"
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
        return {
            "error": f"This chat is locked to {locked_provider}. "
                     f"You can't use {current_provider} here — start a new chat."
        }
    title = make_title(request.message)
    ensure_chat(active_chat_id, title, request.parent_id, mode=current_mode, provider=current_provider)
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
    add_message(active_chat_id, "user", timestamped_message, None, parent_id, memory_used=_memory_used or None)
    # Inject title instruction on first message (model-only, not saved to DB)
    if parent_id is None:
        timestamped_message += '\n\n[SYSTEM: First message of a new chat. Respond normally, but also emit <ation><chat_title>Short descriptive title</chat_title></action> at the end of your response. If you are running another command, then put chat_title and that command in one action block.]'
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
            message=timestamped_message,
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
    if not request.stream and _is_api_model(request.model):
        # _backend already resolved above at file resolution stage
        _api_backend = _backend or _resolve_api_backend(request.model)
        _connector = get_connector(_api_backend)
        _cfg = get_model_config(request.model)
        _api_model = _cfg.get("api_model_type", _cfg["id"])
        # DeepSeek Vision ephemeral: one-shot side request, no session continuity
        _ephemeral = (_api_backend == "deepseek" and _api_model == "vision" and bool(request.ref_file_ids))
        # Collect local file paths for direct-read backends (base64 inline)
        _inline_files = None
        if _api_backend in _DIRECT_READ_BACKENDS and resolved_files:
            _inline_files = [f.get('path') for f in resolved_files if f.get('path')]
        _chat_kwargs: dict[str, Any] = dict(
            message=timestamped_message,
            model=_api_model,
            thinking_mode=request.thinking_mode,
            chat_id=None if _ephemeral else active_chat_id,
            ref_file_ids=request.ref_file_ids,
            inject_instructions=not _ephemeral,
        )
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
                message=timestamped_message,
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
        answer_parts: list[str] = []
        thinking_parts: list[str] = []
        skill_events: list[dict[str, Any]] = []
        final_parent = parent_id
        error_message: str | None = None
        current_message = timestamped_message
        current_parent = parent_id
        round_index = 0
        saved_message_id: int | None = None
        _all_tool_mem_used: list[dict[str, Any]] = []
        yield sse({"type": "status", "message": "processing"})
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
                            # Detect <title> tag in plain text stream (model may emit outside action block)
                            _title_buf += chunk
                            m = _TITLE_RE.search(_title_buf)
                            if m:
                                _t = m.group(1).strip()
                                if _t:
                                    update_chat_title(active_chat_id, _t[:80])
                                    yield sse({"type": "chat_title", "title": _t[:80]})
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
                                _title_text = str(item.get("content", "")).strip()
                                if _title_text:
                                    update_chat_title(active_chat_id, _title_text[:80])
                                    yield sse({"type": "chat_title", "title": _title_text[:80]})
                                continue
                            # Execute the tag through the middleware pipeline
                            for ev in engine.process_tag(
                                item["name"], item.get("attrs", {}), item.get("content", "")
                            ):
                                if ev.get("type") in ("skill_start", "skill_output", "skill_end", "file_edit"):
                                    round_skill_events.append(ev)
                                yield sse(ev)
                        else:
                            # tool_pending, tool_progress, etc — forward to frontend
                            if itype in ("skill_start", "skill_output", "skill_end", "file_edit"):
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
                if _is_api_model(request.model):
                    _api_backend = _backend or _resolve_api_backend(request.model)
                    _connector = get_connector(_api_backend)
                    _cfg = get_model_config(request.model)
                    _api_model = _cfg.get("api_model_type", _cfg["id"])
                    _ephemeral = (_api_backend == "deepseek" and _api_model == "vision" and bool(request.ref_file_ids))
                    # Collect local file paths for direct-read backends (first round only)
                    _inline_files = None
                    if _api_backend in _DIRECT_READ_BACKENDS and round_index == 0 and resolved_files:
                        _inline_files = [f.get('path') for f in resolved_files if f.get('path')]
                    _stream_kwargs: dict[str, Any] = dict(
                        message=current_message,
                        model=_api_model,
                        thinking_mode=request.thinking_mode,
                        chat_id=None if _ephemeral else active_chat_id,
                        ref_file_ids=request.ref_file_ids if round_index == 0 else None,
                        inject_instructions=not _ephemeral,
                    )
                    if _inline_files:
                        _stream_kwargs['files'] = _inline_files
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
                    round_event_source = retry_stream(
                        lambda: service.stream_events(
                            message=current_message,
                            chat_id=active_chat_id,
                            parent_id=current_parent,
                            files=files_for_round,
                            model=request.model,
                            thinking_mode=request.thinking_mode,
                        ),
                        label=f"stream_round_{round_index}",
                    )
                async for event in round_event_source:
                    event_type = event.get("type")
                    if event_type == "answer":
                        pending_thinking.clear()
                        async for _sse_line in _drain_sync_gen(emit_parsed(str(event.get("text", "")))):
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
                round_answer = "".join(answer_parts)
                round_thinking = "".join(thinking_parts)
                stored = round_answer or error_message or ""
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
                feedback = build_tool_feedback(round_skill_events)
                if stream_error or error_message or not feedback or _ask_user_pause:
                    break


                try:
                    _max_chars_tool = _ms_cfg.get("max_prompt_chars", _DEFAULT_MAX_PROMPT_CHARS)
                    if _ms_cfg.get("enabled", True) and feedback and len(feedback) <= _max_chars_tool:
                        _tool_mem = _searcher.search(feedback, top_k=_ms_cfg.get("top_k", 10))
                        _new_mem = [r for r in _tool_mem if r.get("key") and r["key"] not in _injected_memory_keys]
                        if _new_mem:
                            _tool_block = _searcher.format_for_prompt(_new_mem)
                            if _tool_block:
                                feedback = f"{_tool_block}\n\n{feedback}"  # memory stays unwrapped, tool part is already wrapped
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