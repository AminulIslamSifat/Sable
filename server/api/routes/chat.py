from __future__ import annotations

import asyncio
import json
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
from engine.skills import SkillParser, build_tool_feedback
from connectors.deepseek.client import get_client as get_deepseek_client

from server.config import (
    SKILL_ROUND_WARN_THRESHOLD,
    _MEMORY_SEARCH_SETTINGS,
    _DEFAULT_MAX_PROMPT_CHARS,
)
from server.database import (
    ensure_chat, get_chat_mode, get_chat_provider,
    set_title_if_default, get_injected_memory_keys, save_injected_memory_keys,
    touch_chat, save_chat_url, get_chat_url,
    add_message, update_message, get_messages, list_chats, delete_chat, get_parent_id,
)
from server.utils import retry_async, retry_stream, make_title, _is_deepseek_api_model, logger
from server.models import ChatRequest
from ..dependencies import service, sse

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
    timestamped_message = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]\n{request.message}"
    _injected_memory_keys = get_injected_memory_keys(active_chat_id)
    _ms_cfg: dict[str, Any] = {"enabled": True, "top_k": 10}
    _searcher = get_searcher()
    _memory_used: list[dict[str, Any]] = []
    try:
        if _MEMORY_SEARCH_SETTINGS.exists():
            _ms_cfg = json.loads(_MEMORY_SEARCH_SETTINGS.read_text(encoding="utf-8"))
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
    elif _is_deepseek_api_model(request.model):
        current_provider = "deepseek"
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
    parent_id = get_parent_id(active_chat_id, request.parent_id)
    add_message(active_chat_id, "user", timestamped_message, None, parent_id, memory_used=_memory_used or None)
    resolved_files: list[dict[str, Any]] | None = None
    if request.files:
        resolved_files = []
        for f in request.files:
            if scraper_enabled:
                if "path" in f or "url" in f:
                    resolved_files.append(f)
                continue
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
    if not request.stream and _is_deepseek_api_model(request.model):
        ds_cfg = get_model_config(request.model)
        api_model_type = ds_cfg.get("api_model_type", "default")
        ds_ephemeral = api_model_type == "vision" and bool(request.ref_file_ids)
        result = await get_deepseek_client().chat(
            message=timestamped_message,
            model=api_model_type,
            thinking_mode=request.thinking_mode,
            chat_id=None if ds_ephemeral else active_chat_id,
            ref_file_ids=request.ref_file_ids,
            inject_instructions=not ds_ephemeral,
        )
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
            while True:
                round_skill_events: list[dict[str, Any]] = []
                round_thinking_parts: list[str] = []
                round_answer_parts: list[str] = []
                pending_thinking: list[str] = []
                parser = SkillParser()
                def emit_parsed(text: str) -> Generator[str, None, None]:
                    for item in parser.feed(text):
                        if item.get("type") == "text":
                            chunk = str(item.get("text", ""))
                            if chunk:
                                answer_parts.append(chunk)
                                round_answer_parts.append(chunk)
                                yield sse({"type": "answer", "text": chunk})
                        else:
                            if item.get("type") in ("skill_start", "skill_output", "skill_end", "file_edit"):
                                round_skill_events.append(item)
                            yield sse(item)
                def emit_flush() -> Generator[str, None, None]:
                    for item in parser.flush():
                        if item.get("type") == "text":
                            chunk = str(item.get("text", ""))
                            if chunk:
                                answer_parts.append(chunk)
                                round_answer_parts.append(chunk)
                                yield sse({"type": "answer", "text": chunk})
                        else:
                            if item.get("type") in ("skill_start", "skill_output", "skill_end", "file_edit"):
                                round_skill_events.append(item)
                            yield sse(item)
                files_for_round = resolved_files if round_index == 0 else None
                stream_error = False
                if _is_deepseek_api_model(request.model):
                    _ds_cfg = get_model_config(request.model)
                    _ds_api_type = _ds_cfg.get("api_model_type", "default")
                    _ds_ephemeral = _ds_api_type == "vision" and bool(request.ref_file_ids)
                    round_event_source = get_deepseek_client().stream_chat(
                        message=current_message,
                        model=_ds_api_type,
                        thinking_mode=request.thinking_mode,
                        chat_id=None if _ds_ephemeral else active_chat_id,
                        ref_file_ids=request.ref_file_ids if round_index == 0 else None,
                        inject_instructions=not _ds_ephemeral,
                    )
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
                        if pending_thinking:
                            yield sse({"type": "round_thinking", "text": "".join(pending_thinking)})
                            pending_thinking.clear()
                        for _sse_line in emit_parsed(str(event.get("text", ""))):
                            yield _sse_line
                        continue
                    if event_type == "thinking":
                        thinking_parts.append(str(event.get("text", "")))
                        round_thinking_parts.append(str(event.get("text", "")))
                        pending_thinking.append(str(event.get("text", "")))
                        continue
                    elif event_type == "done":
                        if pending_thinking:
                            yield sse({"type": "round_thinking", "text": "".join(pending_thinking)})
                            pending_thinking.clear()
                        for _sse_line in emit_flush():
                            yield _sse_line
                        final_parent = event.get("parent_id") or final_parent
                        current_parent = final_parent
                    elif event_type == "error":
                        if pending_thinking:
                            yield sse({"type": "round_thinking", "text": "".join(pending_thinking)})
                            pending_thinking.clear()
                        for _sse_line in emit_flush():
                            yield _sse_line
                        error_message = str(event.get("message", "Unknown error"))
                        stream_error = True
                    elif event_type == "rate_limited":
                        if pending_thinking:
                            yield sse({"type": "round_thinking", "text": "".join(pending_thinking)})
                            pending_thinking.clear()
                        for _sse_line in emit_flush():
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
                feedback = build_tool_feedback(round_skill_events)
                if stream_error or error_message or not feedback:
                    break
                try:
                    _max_chars_tool = _ms_cfg.get("max_prompt_chars", _DEFAULT_MAX_PROMPT_CHARS)
                    if _ms_cfg.get("enabled", True) and feedback and len(feedback) <= _max_chars_tool:
                        _tool_mem = _searcher.search(feedback, top_k=_ms_cfg.get("top_k", 10))
                        _new_mem = [r for r in _tool_mem if r.get("key") and r["key"] not in _injected_memory_keys]
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
    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )