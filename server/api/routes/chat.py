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
from engine.think_parser import ThinkTagSplitter, split_thinking, strip_thinking
from engine.agents.resilience import LoopDetector, MainChatGuard, TurnCapTracker
from engine.skills.handlers import HANDLER_MAP
from connectors import get_connector
from connectors.deepseek.client import get_client as get_deepseek_client

_skill_engine: SkillEngine | None = None


def _auto_switch_enabled(provider: str = "qwen") -> bool:
    """Check if account auto-switch is enabled for a specific provider."""
    try:
        from server.api.routes.settings import get_auto_switch_for_provider
        return get_auto_switch_for_provider(provider)
    except Exception:
        return True  # default to enabled on error


def _build_account_debug_info() -> dict[str, Any]:
    """Build debug info from the LIVE service singleton, not the config file.

    This shows what the running ChatService is *actually* using — which may
    differ from .active_account if the singleton wasn't properly updated.
    """
    try:
        from server.api.dependencies import service
        return service._get_debug_account_info()
    except Exception as _dbg_exc:
        return {"account": "unknown", "error": str(_dbg_exc)}


def _reconstruct_message_content(msg: dict[str, Any]) -> str:
    """Reconstruct full message content including skill event outputs.

    For token counting, each assistant message's effective context includes
    not just the text response but also all tool call results that were fed
    back to the LLM. This rebuilds that full content from stored skill_events.
    """
    base_content = msg.get("content") or ""
    events = msg.get("skill_events")
    if not events:
        return base_content

    # Build tool feedback from skill events (same format as live feedback)
    tool_text = build_tool_feedback(events)
    if tool_text:
        return f"{base_content}\n\n{tool_text}" if base_content else tool_text
    return base_content


# Per-turn cache of the (expensive) DB history reconstruction. Keyed by
# (chat_id, message_row_count) so it auto-invalidates the moment a new
# message is persisted, but is reused across every round of a single turn.
# Without this, a 700-tool-call chat re-parsed the entire message table +
# all skill_events on EVERY loop iteration — the quadratic flood that
# OOM-killed a 16GB+20GB-swap machine.
_HISTORY_CACHE: dict[tuple[str, int], list[dict[str, str]]] = {}


def _chat_row_count(chat_id: str) -> int:
    """Cheap COUNT(*) — used as a cache key so we never serve stale history."""
    try:
        with get_db() as _conn:
            row = _conn.execute(
                "SELECT COUNT(*) AS c FROM messages WHERE chat_id = ?", (chat_id,)
            ).fetchone()
            return int(row["c"]) if row else 0
    except Exception:
        return -1


def _load_history_with_skills(chat_id: str) -> list[dict[str, str]]:
    """Load conversation history with skill event content.

    Bounded: the result is trimmed to the shared history budget so a deep
    chat can never hand the whole message table (plus every tool payload)
    to a connector or the token counter at once.

    Cached per (chat_id, row_count) for the lifetime of the turn so the
    reconstruction does not repeat on every round of the tool loop.
    """
    from connectors.common.history_budget import (
        MAX_DB_HISTORY_MESSAGES,
        trim_history_messages,
    )

    cache_key = (chat_id, _chat_row_count(chat_id))
    cached = _HISTORY_CACHE.get(cache_key)
    if cached is not None:
        return cached

    # CRITICAL: fetch only the newest N messages from SQLite. Loading the
    # whole table then trimming still materializes every row + every skill
    # event in RAM — that was the crash. The LIMIT must be in the query.
    msgs = get_messages(
        chat_id,
        limit=MAX_DB_HISTORY_MESSAGES,
        include_skill_events=True,
    )
    history: list[dict[str, str]] = []
    for m in msgs:
        if m["role"] not in ("user", "assistant"):
            continue
        content = _reconstruct_message_content(m)
        if content:
            history.append({"role": m["role"], "content": content})

    # Safety trim (query already bounded it, but keep the invariant explicit).
    history = trim_history_messages(history)

    # Keep the cache from growing without bound across many chats.
    if len(_HISTORY_CACHE) > 32:
        _HISTORY_CACHE.clear()
    _HISTORY_CACHE[cache_key] = history
    return history


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
    ensure_chat, get_chat_mode, get_chat_provider, is_chat_fork, save_chat_model_settings,
    set_title_if_default, update_chat_title, get_injected_memory_keys, save_injected_memory_keys,
    touch_chat, save_chat_url, get_chat_url,
    add_message, update_message, get_messages, list_chats, delete_chat, get_parent_id, get_db,
)
from server.utils import retry_async, make_title, _is_deepseek_api_model, _resolve_api_backend, _is_api_model, logger
from engine.token_counter import count_prompt_tokens, count_completion_tokens
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
    except OSError as exc:
        # Never let conversation logging break the stream — but do surface it.
        logger.debug("conversation log write failed for %s: %s", chat_id, exc)

router = APIRouter()

# chat_id -> latest upstream response_id (the Qwen node id of the in-flight
# assistant turn). Captured the moment the `response_id` SSE event fires so
# /api/chat/stop always has one, even if the frontend pressed Stop before its
# own copy landed. Qwen's stop endpoint REQUIRES response_id and returns a
# RequestValidationError without it.
_ACTIVE_RESPONSE_IDS: dict[str, str] = {}


@router.post("/api/chat/stop")
async def stop_generation(request: Request):
    """Stop upstream Qwen generation via server-side API call.

    Called by the frontend BEFORE aborting the SSE stream. Returns success/failure
    so the frontend knows whether to clear the streaming state or keep the stop button.
    """
    from pydantic import BaseModel

    class StopRequest(BaseModel):
        chat_id: str
        response_id: str | None = None

    body = await request.json()
    chat_id = body.get("chat_id", "")
    response_id = body.get("response_id")

    if not chat_id:
        return {"success": False, "error": "chat_id required"}

    # z.ai (GLM): no upstream stop API — the SPA aborts the in-flight fetch
    # with an AbortController. We mirror that: fire the in-page abort hook the
    # connector exposed. No response_id needed, so handle it BEFORE the
    # response_id guard below.
    _prov = None
    try:
        from server.database.chats import get_chat_provider as _get_prov
        _prov = _get_prov(chat_id)
    except Exception:
        _prov = None
    if _prov == "zai":
        try:
            from connectors.zai.client import get_client as _zai_get
            _ok = await _zai_get().cancel()
            logger.info("z.ai stop: chat=%s aborted=%s", chat_id, _ok)
            return {"success": bool(_ok)}
        except Exception as exc:
            logger.warning("z.ai stop failed: %s", exc)
            return {"success": False, "error": str(exc)}
    # Gemini Web: same story — no stop endpoint, abort the streaming response.
    if _prov == "gemini_web":
        try:
            from connectors.gemini_web.client import get_client as _gw_get
            _ok = await _gw_get().cancel()
            logger.info("gemini_web stop: chat=%s aborted=%s", chat_id, _ok)
            return {"success": bool(_ok)}
        except Exception as exc:
            logger.warning("gemini_web stop failed: %s", exc)
            return {"success": False, "error": str(exc)}
    # Mistral (Web): real upstream stop endpoint (tRPC chat.cancelGeneration)
    # plus a local abort of the in-flight SSE stream.
    if _prov == "mistral_web":
        try:
            from connectors.mistral_web.client import get_client as _mw_get
            _ok = await _mw_get().cancel(chat_id)
            logger.info("mistral_web stop: chat=%s aborted=%s", chat_id, _ok)
            return {"success": bool(_ok)}
        except Exception as exc:
            logger.warning("mistral_web stop failed: %s", exc)
            return {"success": False, "error": str(exc)}
    # MiMo Studio: same — SPA aborts an AbortSignal, no stop endpoint.
    if _prov == "mimo":
        try:
            from connectors.mimo.client import get_client as _mimo_get
            _ok = await _mimo_get().cancel()
            logger.info("mimo stop: chat=%s aborted=%s", chat_id, _ok)
            return {"success": bool(_ok)}
        except Exception as exc:
            logger.warning("mimo stop failed: %s", exc)
            return {"success": False, "error": str(exc)}
    # DeepSeek: ghost-tab in-page fetch has no upstream stop endpoint. Abort
    # the in-page AbortController + force-release the per-account ghost stream
    # lock. Without this the lock stayed held forever and EVERY subsequent
    # message (any chat, same account) wedged on "deepseek via ghost tab".
    if _prov == "deepseek":
        try:
            from connectors.deepseek.client import cancel_active_streams as _ds_cancel
            _ok = await _ds_cancel()
            logger.info("deepseek stop: chat=%s aborted=%s", chat_id, _ok)
            # Always report success — the abort is best-effort and the lock is
            # reclaimed by the bounded acquire in open_stream() regardless.
            return {"success": True, "aborted": bool(_ok)}
        except Exception as exc:
            logger.warning("deepseek stop failed: %s", exc)
            return {"success": True, "aborted": False, "error": str(exc)}

    # DeepSeek (scraper): headed-browser transport. Click the SPA's stop button
    # via the per-account engine.
    if _prov == "deepseek_scraper":
        try:
            from connectors.deepseek_scraper.client import cancel_active_streams as _dss_cancel
            _ok = await _dss_cancel()
            logger.info("deepseek_scraper stop: chat=%s aborted=%s", chat_id, _ok)
            return {"success": True, "aborted": bool(_ok)}
        except Exception as exc:
            logger.warning("deepseek_scraper stop failed: %s", exc)
            return {"success": True, "aborted": False, "error": str(exc)}

    # Qwen (scraper): headed-browser transport. Same stop-button approach.
    if _prov == "qwen_scraper":
        try:
            from connectors.qwen_scraper.client import cancel_active_streams as _qs_cancel
            _ok = await _qs_cancel()
            logger.info("qwen_scraper stop: chat=%s aborted=%s", chat_id, _ok)
            return {"success": True, "aborted": bool(_ok)}
        except Exception as exc:
            logger.warning("qwen_scraper stop failed: %s", exc)
            return {"success": True, "aborted": False, "error": str(exc)}

    # Frontend may not have the response_id yet (Stop pressed before the first
    # chunk landed). Fall back to the id captured server-side when the
    # response_id event fired. Qwen's stop endpoint REQUIRES response_id.
    if not response_id:
        response_id = _ACTIVE_RESPONSE_IDS.get(chat_id)
    if not response_id:
        logger.info("Stop request for %s has no response_id yet — upstream stop skipped", chat_id)
        return {"success": False, "error": "response_id not available yet"}

    # Resolve upstream session ID — Qwen's stop API needs the upstream ID, not local UUID
    from server.database import get_upstream_session_id as _get_usid
    upstream_id = _get_usid(chat_id) or chat_id

    # Determine which service to use
    scraper_enabled = get_scraper_settings().get("enabled")

    stopped = False
    if scraper_enabled and scraper_service:
        # Browser scraper mode — use engine's stop_generation
        try:
            engine = getattr(scraper_service, "_engine", None)
            if engine:
                stopped = await engine.stop_generation(chat_id=upstream_id, response_id=response_id)
        except Exception as exc:
            logger.warning("Scraper stop failed: %s", exc)
    else:
        # Direct API mode — use ChatService._stop_upstream_generation
        try:
            stopped = await service._stop_upstream_generation(upstream_id, response_id)
        except Exception as exc:
            logger.warning("API stop failed: %s", exc)

    if stopped:
        _ACTIVE_RESPONSE_IDS.pop(chat_id, None)
    logger.info("Stop request: local=%s upstream=%s response_id=%s success=%s", chat_id, upstream_id, response_id, stopped)
    return {"success": stopped}


# ---------------------------------------------------------------------------
# Auto-switch context builder (summarization for account switching)
# ---------------------------------------------------------------------------

_SUMMARIZER_CHUNK_LIMIT = 300_000   # max chars per summarizer call
_TAIL_PRESERVE_LIMIT = 250_000      # max chars to keep verbatim at the end
_TAIL_MIN_CHARS = 100_000           # minimum chars to preserve from the end (expand beyond tool call count)
_SUMMARIZER_MIN_HEAD = 5_000        # skip summarization if head is smaller than this
_SUMMARIZER_MAX_CONCURRENCY = 3     # max parallel summarizer API calls
_SUMMARIZER_CACHE_TTL = 300         # seconds to cache summaries per chat_id
_HEAD_TOOL_CALLS = 3                # first N tool_call/result pairs to preserve verbatim
_TAIL_TOOL_CALLS = 5                # last N tool_call/result pairs to preserve verbatim

_summarizer_semaphore: asyncio.Semaphore | None = None
_summarizer_cache: dict[str, tuple[float, str]] = {}  # chat_id → (timestamp, result)

import re as _re
_tool_call_re = _re.compile(r'<\s*tool_calls?[\s>]', _re.IGNORECASE)
_tool_result_re = _re.compile(r'<tool_result[\s>]', _re.IGNORECASE)


def _extract_head_tail(messages: list[dict]) -> tuple[str, str, list[dict]]:
    """Extract verbatim head/tail from messages, return (head_text, tail_text, middle_messages).

    Head: first user message + first N tool_call/result pairs.
    Tail: last user message + last N tool_call/result pairs.
    Middle: everything between head and tail (sent to summarizer).
    """
    if not messages:
        return "", "", []

    # --- Find first user message ---
    first_user_idx = None
    for i, m in enumerate(messages):
        if m.get("role") == "user":
            first_user_idx = i
            break

    # --- Find last user message ---
    last_user_idx = None
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            last_user_idx = i
            break

    if first_user_idx is None or last_user_idx is None:
        return "", "", messages

    # --- Collect first N tool_call/result pairs after first user message ---
    head_end_idx = first_user_idx + 1  # at minimum, include first user message
    tc_count = 0
    for i in range(first_user_idx + 1, len(messages)):
        content = (messages[i].get("content") or "")
        role = messages[i].get("role", "")
        if _tool_call_re.search(content) or _tool_result_re.search(content) or role == "tool":
            head_end_idx = i + 1
            tc_count += 1
            if tc_count >= _HEAD_TOOL_CALLS * 2:  # pairs = call + result
                break
        elif role == "assistant" and not _tool_call_re.search(content):
            # Non-tool assistant message — still include in head if within range
            head_end_idx = i + 1

    # --- Collect last N tool_call/result pairs before/at last user message ---
    tail_start_idx = last_user_idx
    tc_count = 0
    for i in range(len(messages) - 1, last_user_idx - 1, -1):
        content = (messages[i].get("content") or "")
        role = messages[i].get("role", "")
        if _tool_call_re.search(content) or _tool_result_re.search(content) or role == "tool":
            tail_start_idx = i
            tc_count += 1
            if tc_count >= _TAIL_TOOL_CALLS * 2:
                break

    # Ensure tail doesn't overlap head
    if tail_start_idx < head_end_idx:
        tail_start_idx = head_end_idx

    # --- Expand tail to meet minimum char threshold ---
    # If the current tail (by tool call count) is under _TAIL_MIN_CHARS,
    # walk backward from tail_start_idx to include more messages.
    tail_char_count = sum(
        len((messages[i].get("content") or "").strip())
        for i in range(tail_start_idx, len(messages))
    )
    while tail_char_count < _TAIL_MIN_CHARS and tail_start_idx > head_end_idx:
        tail_start_idx -= 1
        tail_char_count += len((messages[tail_start_idx].get("content") or "").strip())

    # Cap at _TAIL_PRESERVE_LIMIT to avoid oversized tails
    if tail_char_count > _TAIL_PRESERVE_LIMIT:
        # Walk forward to trim excess
        while tail_char_count > _TAIL_PRESERVE_LIMIT and tail_start_idx < len(messages) - 1:
            tail_char_count -= len((messages[tail_start_idx].get("content") or "").strip())
            tail_start_idx += 1

    def _msg_enriched(m: dict) -> str:
        """Build enriched text for a single message including memory + skill events."""
        parts: list[str] = []
        content = (m.get("content") or "").strip()
        if content:
            parts.append(content)
        mem_used = m.get("memory_used") or []
        if mem_used:
            mem_lines = [f"  - {mu.get('key', '')}: {mu.get('value', '')}" for mu in mem_used if mu.get("value")]
            if mem_lines:
                parts.append("[Memory used]:\n" + "\n".join(mem_lines))
        for sev in (m.get("skill_events") or []):
            sev_type = sev.get("type", "")
            if sev_type == "skill_start":
                attrs = sev.get("data", {}).get("attrs", {})
                parts.append(f"[Tool call]: {sev.get('name', 'unknown')}({attrs})")
            elif sev_type == "skill_end":
                err = sev.get("error")
                res = str(sev.get("result", ""))[:2000]
                ok = sev.get("ok", True)
                if err:
                    parts.append(f"[Tool result]: {sev.get('name', 'unknown')} (ok={ok}): ERROR: {err}")
                else:
                    parts.append(f"[Tool result]: {sev.get('name', 'unknown')} (ok={ok}): {res}")
        return "\n".join(parts)

    # --- Format head ---
    head_msgs = messages[first_user_idx:head_end_idx]
    head_lines = []
    first_user_enriched = _msg_enriched(messages[first_user_idx])
    head_lines.append(f"## First User Message\n{first_user_enriched}")

    # Remaining head messages are early tool calls
    early_tc_parts = []
    for m in head_msgs[1:]:
        enriched = _msg_enriched(m)
        if enriched:
            early_tc_parts.append(enriched)
    if early_tc_parts:
        head_lines.append("## Early Tool Calls\n" + "\n".join(early_tc_parts))

    head_text = "\n\n".join(head_lines)

    # --- Format tail ---
    tail_msgs = messages[tail_start_idx:]
    tail_lines = []

    # Find last user message content in tail
    last_user_enriched = _msg_enriched(messages[last_user_idx])

    # Messages after last user message are recent tool calls
    recent_tc_parts = []
    for m in messages[last_user_idx + 1:]:
        enriched = _msg_enriched(m)
        if enriched:
            recent_tc_parts.append(enriched)

    tail_lines.append(f"## Last User Message\n{last_user_enriched}")
    if recent_tc_parts:
        tail_lines.append("## Recent Tool Calls\n" + "\n".join(recent_tc_parts))

    tail_text = "\n\n".join(tail_lines)

    # --- Middle messages (between head end and tail start) ---
    middle = messages[head_end_idx:tail_start_idx]

    return head_text, tail_text, middle


def _chunk_text(text: str, limit: int) -> list[str]:
    """Split text into sequential chunks of at most `limit` chars.

    Tries to break on newline boundaries when possible to avoid splitting
    mid-message. Falls back to hard cut if no newline found within tolerance.
    """
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = start + limit
        if end >= len(text):
            chunks.append(text[start:])
            break
        # Try to find a newline near the boundary (within last 10% of chunk)
        search_start = max(start, end - limit // 10)
        nl = text.rfind("\n", search_start, end)
        if nl > start:
            end = nl + 1
        chunks.append(text[start:end])
        start = end
    return chunks


async def _build_switch_context(chat_id: str, model: str) -> str:
    """Build a formatted context string for passing to a new account after switch.

    Hermes-style format:
    - Head (first user msg + first 3 tool calls) extracted verbatim in code
    - Tail (last user msg + last 5 tool calls) extracted verbatim in code
    - Only the middle portion is sent to the summarizer model
    - Model produces ## Conversation Flow + optional ## Intermediary Summary
    - Final output assembled: head + flow + intermediary + tail + 'continue'

    Falls back to raw transcript if total <= 500k chars.
    """
    import asyncio as _asyncio
    import time as _time

    global _summarizer_semaphore
    if _summarizer_semaphore is None:
        _summarizer_semaphore = _asyncio.Semaphore(_SUMMARIZER_MAX_CONCURRENCY)

    messages = get_messages(chat_id, include_skill_events=True)
    if not messages:
        return ""

    def _enrich_message(m: dict) -> str:
        """Build a text representation including memory_used and skill events."""
        role = m.get("role", "unknown")
        content = (m.get("content") or "").strip()
        parts: list[str] = []
        if content:
            parts.append(f"[{role}]: {content}")
        # Append memory context so consolidation data survives summarization
        mem_used = m.get("memory_used") or []
        if mem_used:
            mem_lines = [f"  - **{mu.get('key', '')}**: {mu.get('value', '')}" for mu in mem_used if mu.get("value")]
            if mem_lines:
                parts.append(f"[memory_context]:\n" + "\n".join(mem_lines))
        # Append skill events (tool calls) so they aren't lost
        for sev in (m.get("skill_events") or []):
            sev_type = sev.get("type", "")
            if sev_type == "skill_start":
                attrs = sev.get("data", {}).get("attrs", {})
                parts.append(f"[tool_call]: {sev.get('name', 'unknown')}({attrs})")
            elif sev_type == "skill_end":
                err = sev.get("error")
                res = str(sev.get("result", ""))[:2000]
                if err:
                    parts.append(f"[tool_result]: {sev.get('name', 'unknown')} (ok={sev.get('ok', True)}): ERROR: {err}")
                else:
                    parts.append(f"[tool_result]: {sev.get('name', 'unknown')} (ok={sev.get('ok', True)}): {res}")
        return "\n".join(parts)

    # --- Calculate total length ---
    total_chars = sum(len(_enrich_message(m)) for m in messages)

    # Under threshold — no summarization needed
    if total_chars <= 500_000:
        lines = [_enrich_message(m) for m in messages]
        return "\n".join(l for l in lines if l)

    # --- Check cache ---
    now = _time.monotonic()
    cached = _summarizer_cache.get(chat_id)
    if cached and (now - cached[0]) < _SUMMARIZER_CACHE_TTL:
        logger.info("[auto-switch] Using cached summary for %s", chat_id)
        return cached[1]

    # --- Extract head/tail programmatically ---
    head_text, tail_text, middle_messages = _extract_head_tail(messages)

    # Format middle into transcript for summarizer (include memory + tool calls)
    middle_lines = [_enrich_message(m) for m in middle_messages]
    middle_text = "\n".join(l for l in middle_lines if l)

    # Middle too small — skip summarization, include inline
    if len(middle_text) < _SUMMARIZER_MIN_HEAD:
        parts = [head_text]
        if middle_text.strip():
            parts.append(f"## Conversation Flow\n{middle_text}")
        parts.append(tail_text)
        parts.append("continue")
        result = "\n\n".join(parts)
        _summarizer_cache[chat_id] = (now, result)
        return result

    # --- Chunked parallel summarization of MIDDLE only ---
    chunks = _chunk_text(middle_text, _SUMMARIZER_CHUNK_LIMIT)
    total_chunks = len(chunks)
    logger.info(
        "[auto-switch] Summarizing middle %d chars in %d chunk(s) (head=%d, tail=%d, total=%d)",
        len(middle_text), total_chunks, len(head_text), len(tail_text), total_chars,
    )

    async def _guarded_summarize(chunk: str, idx: int) -> str:
        assert _summarizer_semaphore is not None
        async with _summarizer_semaphore:
            return await _run_summarizer(chunk, model, chunk_index=idx, total_chunks=total_chunks)

    tasks = [_guarded_summarize(chunk, i) for i, chunk in enumerate(chunks)]
    flow_summaries = await _asyncio.gather(*tasks)

    # Stitch conversation flow parts
    if total_chunks > 1:
        stitched_flow = "\n\n".join(
            f"### Part {i + 1}/{total_chunks}\n{s}"
            for i, s in enumerate(flow_summaries)
        )
    else:
        stitched_flow = flow_summaries[0] if flow_summaries else ""

    # --- Assemble final output ---
    parts = [head_text, f"## Conversation Flow\n{stitched_flow}"]

    # Count tool calls after last user message for intermediary summary decision
    tail_tc_count = sum(
        1 for m in messages
        if (_tool_call_re.search(m.get("content") or "") or
            _tool_result_re.search(m.get("content") or "") or
            m.get("role") == "tool")
    )
    # The summarizer handles intermediary summary in its output;
    # if it produced one, it's already in stitched_flow

    parts.append(tail_text)
    parts.append("continue")
    result = "\n\n".join(parts)

    # Cache the result
    _summarizer_cache[chat_id] = (now, result)

    # Evict stale cache entries
    stale_keys = [k for k, (ts, _) in _summarizer_cache.items() if (now - ts) > _SUMMARIZER_CACHE_TTL]
    for k in stale_keys:
        del _summarizer_cache[k]

    return result


async def _run_summarizer(
    transcript: str,
    model: str,
    *,
    chunk_index: int = 0,
    total_chunks: int = 1,
) -> str:
    """Run the context summarizer model on a transcript chunk.

    Args:
        transcript: The text chunk to summarize.
        model: Fallback model if no dedicated summarizer is configured.
        chunk_index: 0-based index of this chunk (for structured output ordering).
        total_chunks: Total number of chunks being summarized in parallel.
    """
    from server.api.routes.chats import _load_ctx_pass_settings
    settings = _load_ctx_pass_settings()
    sum_model = settings.get("summarizer_model") or model

    # Multi-chunk awareness for the prompt
    if total_chunks > 1:
        position_note = (
            f"This is chunk {chunk_index + 1} of {total_chunks}. "
            "Summarize ONLY this chunk's content. Do not reference other chunks. "
            "The output will be stitched sequentially with other chunk summaries, "
            "so maintain chronological flow within this chunk.\n\n"
        )
    else:
        position_note = ""

    prompt = (
        "You are compressing the MIDDLE portion of a conversation transcript. "
        "The first user message, early tool calls, last user message, and recent tool "
        "calls are already extracted separately — DO NOT reproduce them.\n\n"
        f"{position_note}"
        "YOUR ONLY JOB: Compress this transcript into labeled turn pairs.\n\n"

        "OUTPUT FORMAT:\n\n"
        "## user\n"
        "[1-2 sentence summary: what the user asked/requested]\n"
        "## model\n"
        "[Compressed actions: which files were viewed/edited/created, which tools were "
        "called, compressed tool result, any errors or problems encountered]\n\n"
        "Repeat ## user / ## model pairs as needed. Single-sided turns are allowed "
        "(e.g., only ## model if the model acted without a new user prompt).\n"
        "Be DENSE — name files, tools, and outcomes. Skip pleasantries and meta-talk.\n\n"

        "If this transcript segment contains MORE THAN 5 tool calls, also add at the end:\n\n"
        "## Intermediary Summary\n"
        "[For each tool call beyond the last 5: what tool was called, compressed result, "
        "any problems or improvements needed]\n\n"

        "HARD RULES:\n"
        "- Output ONLY the ## user / ## model pairs (and optional ## Intermediary Summary).\n"
        "- NEVER output ## First User Message, ## Early Tool Calls, ## Last User Message, "
        "## Recent Tool Calls, or any other section headers.\n"
        "- NEVER add high-level summaries, synthesis, overview, or commentary.\n"
        "- Never invent details. Write [unclear] if ambiguous.\n"
        "- Preserve code, paths, commands, errors VERBATIM in code blocks within turn pairs.\n"
        "- BUG FIX REPORTING (MANDATORY): When a bug was found AND fixed in a specific file, "
        "the ## model turn MUST include the actual code — not just a prose description. Format:\n"
        "    File: path/to/file.py:L###\n"
        "    PROBLEM: [2-5 line code snippet showing the broken code]\n"
        "    FIX: [2-5 line code snippet showing the corrected code]\n"
        "  If an issue was identified but NOT resolved, do NOT mention it at all.\n"
        "  Only include fix details for bugs actually fixed in that turn.\n\n"
        f"---\n{transcript}"
    )

    # Build fallback chain: primary → fallback_models → dynamic browser pool (reverse)
    fallback_models: list[str] = settings.get("fallback_models", [])

    async def _try_summarizer_call(mdl: str, browser_acc_name: str = "") -> str | None:
        """Try a single summarizer call. Returns answer string or None on failure."""
        try:
            api_backend = _resolve_api_backend(mdl)
            if api_backend:
                connector = get_connector(api_backend)
                result = await connector.chat(message=prompt, model=mdl, thinking_mode="fast")
            elif browser_acc_name:
                from engine.service import ChatService
                from engine.config import _SYSTEM
                acc_dir = _SYSTEM / browser_acc_name
                if acc_dir.exists():
                    temp_service = ChatService(user_data_dir=str(acc_dir))
                    try:
                        result = await temp_service.chat(message=prompt, model=mdl, thinking_mode="fast")
                    finally:
                        await temp_service.close()
                else:
                    logger.warning("[summarizer] Browser profile dir not found: %s", acc_dir)
                    return None
            else:
                result = await service.chat(message=prompt, model=mdl, thinking_mode="fast")

            answer = result.get("answer", "").strip()
            if answer:
                return answer
            logger.warning("[summarizer] %s returned empty: %s", mdl, result.get("error", ""))
            return None
        except Exception as exc:
            logger.warning("[summarizer] %s failed: %s: %s", mdl, type(exc).__name__, exc)
            return None

    # Step 1: Primary model (no specific browser acc — uses active account)
    answer = await _try_summarizer_call(sum_model)
    if answer:
        return answer

    # Step 2: Fallback models (API-based, no browser needed)
    for fb_model in fallback_models[:2]:
        answer = await _try_summarizer_call(fb_model)
        if answer:
            logger.info("[summarizer] Fallback model succeeded: %s", fb_model)
            return answer

    # Step 3: Dynamic browser pool — search from back (highest number first)
    # This avoids competing with main chat auto-switch which searches forward
    from engine.config import get_available_accounts_reverse
    _tried: set[str] = set()
    for acc_name in get_available_accounts_reverse(exclude=_tried, limit=5):
        _tried.add(acc_name)
        answer = await _try_summarizer_call(sum_model, acc_name)
        if answer:
            logger.info("[summarizer] Browser pool account succeeded: %s", acc_name)
            return answer

    return "[Summary unavailable — all summarizer fallbacks exhausted]"


# Qwen stores its instruction in a server-side personalization slot (pushed via
# sync_context), unlike API connectors where the system prompt rides every
# request. The slot is global per account, so it only needs a re-sync when the
# layout mode actually changes — normal chat <-> study. Tracking the last mode
# keeps a study turn from re-pushing the whole directive on every message.
_QWEN_LAYOUT_SYNCED: dict[str, str | None] = {"mode": None, "persona": None}

# Study chats whose upstream session already received the study directive as a
# first-message fallback (only used when the Qwen layout sync failed). Keyed by
# upstream session id so the fallback fires ONCE per session, never per turn.
_QWEN_STUDY_FALLBACK_SENT: set[str] = set()


def _active_persona_name() -> str | None:
    """Read the currently-active persona name from disk. Never raises."""
    try:
        import json as _json
        from pathlib import Path as _P
        cfg = _P(__file__).resolve().parent.parent.parent.parent / "instruction" / ".persona_config.json"
        return _json.loads(cfg.read_text(encoding="utf-8")).get("active")
    except Exception:
        return None


def mark_qwen_synced(layout_mode: str | None = None) -> None:
    """Record that Qwen's personalization slot now holds the current state.

    Called after an explicit sync (e.g. the persona-change endpoint) so the
    next turn's lazy auto-sync doesn't re-push the same instruction.
    """
    _QWEN_LAYOUT_SYNCED["mode"] = layout_mode
    _QWEN_LAYOUT_SYNCED["persona"] = _active_persona_name()


async def _ensure_qwen_layout_sync(layout_mode: str) -> bool:
    """Push the layout- and persona-appropriate instruction into Qwen.

    No-ops when Qwen's slot already holds BOTH the requested layout mode and
    the currently-active persona. This makes it safe to call on every Qwen
    turn: after a persona change made on a non-Qwen provider, the very next
    Qwen turn re-syncs automatically — no explicit push needed at change time.

    Returns True when the slot already holds (or was just updated to) the
    requested state. A False return means the caller should fall back to
    sending the directive on the first message of the session.
    """
    persona = _active_persona_name()
    if _QWEN_LAYOUT_SYNCED["mode"] == layout_mode and _QWEN_LAYOUT_SYNCED["persona"] == persona:
        return True
    try:
        _sync_mode = None if layout_mode in ("agent", "", None) else layout_mode
        # Bounded so a slow sync (cold browser fallback) can never wedge the
        # first study message — on timeout the caller uses the first-message
        # fallback instead, and the next turn retries.
        ok = await asyncio.wait_for(service.sync_context(layout_mode=_sync_mode), timeout=15.0)
    except Exception as exc:
        logger.warning("[qwen-layout] sync failed for %s: %s", layout_mode, exc)
        return False
    if ok:
        _QWEN_LAYOUT_SYNCED["mode"] = layout_mode
        _QWEN_LAYOUT_SYNCED["persona"] = persona
    return bool(ok)


@router.post("/api/chat")
async def chat(request: ChatRequest):
    scraper_enabled = get_scraper_settings().get("enabled")
    active_chat_id = request.chat_id
    _layout_mode = request.layout_mode or "agent"  # "agent" | "chat" | "study"
    bdd = request.browser_data_dir  # scoped browser profile for isolated streams

    # ── Study mode ──────────────────────────────────────────────────────────
    # Study chats are REAL chats (study-<doc>[-<thread>] ids) that run through
    # this same pipeline, so messages + tool calls persist in `messages` /
    # `skill_events` and replay with inline tool cards like any other chat.
    # We only swap the system directive and prepend the document grounding;
    # the chat harness (chat_title, persona, agents) is suppressed below.
    _study_system: str | None = None
    _study_grounding: str | None = None
    _study_sync_failed = False
    if _layout_mode == "study":
        # Provider selects the tool-format primer inside the shared builder.
        # deepseek_scraper shares DeepSeek's format, not its own key.
        _study_provider = "qwen"
        try:
            if _is_api_model(request.model):
                _study_provider = _resolve_api_backend(request.model) or "qwen"
        except Exception:
            pass
        if _study_provider == "deepseek_scraper":
            _study_provider = "deepseek"
        elif _study_provider == "qwen_scraper":
            _study_provider = "qwen"
        try:
            from .study_ai import build_study_turn
            _study_system, _study_grounding = build_study_turn(
                request.study_context, request.message, provider=_study_provider,
            )
        except Exception:
            logger.warning("study turn build failed", exc_info=True)
        # Qwen keeps its instruction in a server-side personalization slot, so
        # study mode needs a background re-sync. If it fails we fall back to
        # sending the directive on the first message of the session (below).
        if not _is_api_model(request.model) and not scraper_enabled:
            try:
                _study_sync_failed = not await _ensure_qwen_layout_sync("study")
            except Exception:
                _study_sync_failed = True
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
                    lambda: service.create_chat(model=request.model, bdd=bdd),
                    label="create_chat",
                )
            except Exception as exc:
                return {"error": f"Session startup failed: {type(exc).__name__}: {exc}"}
            if not _upstream_session_id:
                return {"error": "Could not create chat session"}
    # NOTE: per-provider prompt-injection toggles are resolved *after*
    # current_provider is computed below — see `_inj = ...` further down.
    _ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    _ctx_parts = ""
    if request.cwd:
        _ctx_parts += f" | cwd: {request.cwd}"
    if request.open_file:
        _ctx_parts += f" | file: {request.open_file}"
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
            # Build a short context window from recent user turns so thin follow-ups
            # ("go on", "move to /legacy") can still retrieve the right memory.
            _mem_context = ""
            try:
                # get_messages returns newest-first (ORDER BY id DESC).
                _ctx_msgs = get_messages(active_chat_id, limit=8)
                _ctx_users = [
                    (m.get("content") or "").strip()
                    for m in _ctx_msgs
                    if m.get("role") == "user" and (m.get("content") or "").strip()
                ]
                # Newest user message first; drop it if it's the one being sent now.
                if _ctx_users and _ctx_users[0] == request.message.strip():
                    _ctx_users = _ctx_users[1:]
                # Keep the 2 most recent prior turns, oldest-first for readability.
                _mem_context = " ".join(reversed(_ctx_users[:2]))[:600]
            except Exception as _ctx_err:
                logger.debug("[memory] context build failed: %s", _ctx_err)

            _mem_results = _searcher.search(
                request.message,
                top_k=_ms_cfg.get("top_k", 10),
                allowed_categories=_allowed_mem_cats,
                top_memory=_ms_cfg.get("top_memory"),
                top_procedural=_ms_cfg.get("top_procedural"),
                top_total=_ms_cfg.get("top_total"),
                context=_mem_context,
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
    # Agent chats are always allowed through — they use the main pipeline
    # regardless of scraper/api mode. The layout_mode field distinguishes them.
    if locked_mode and locked_mode != current_mode and locked_mode != "agent":
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

    # ── Lazy Qwen persona/layout auto-sync ──────────────────────────────
    # Qwen keeps its instruction in a SERVER-SIDE personalization slot, so a
    # persona change made while the user is on a DIFFERENT provider never
    # reaches it. Rather than push on every persona change (which spams the
    # Qwen settings API even when Qwen isn't in use), we reconcile lazily:
    # the first Qwen turn after a change re-syncs the slot. `_ensure_qwen_
    # layout_sync` compares BOTH layout_mode and active persona, so it no-ops
    # once they match. Scraper mode uses a real browser DOM and re-syncs via
    # its own path, so skip it here.
    if current_provider == "qwen" and not scraper_enabled and _layout_mode != "study":
        try:
            await _ensure_qwen_layout_sync(_layout_mode)
        except Exception:
            logger.warning("[qwen-persona] lazy sync failed", exc_info=True)

    # Per-provider prompt-injection toggles (Settings -> Providers).
    #
    # CRITICAL: every custom endpoint resolves to api_backend="local", so
    # looking up `current_provider` alone would make all endpoints share the
    # "local" row and silently ignore their own per-endpoint config. Resolve
    # the endpoint-specific key first and fall back to the backend name.
    # Unknown providers -> all-on defaults (unchanged behaviour).
    _inj_provider = current_provider
    try:
        from connectors.common.prompt_injection import (
            endpoint_key as _ep_key, injection_enabled_map as _inj_map,
        )
        _model_cfg = get_model_config(request.model) or {}
        _ep = _ep_key(_model_cfg.get("local_endpoint"))
        if _ep:
            _inj_provider = _ep
        _inj = _inj_map(_inj_provider)
    except Exception:
        logger.warning("prompt_injection: config lookup failed", exc_info=True)
        _inj = {}

    # Timestamp + cwd/file prefix — built AFTER the toggles so "timestamp:off"
    # strips the whole `[YYYY-MM-DD HH:MM:SS | cwd: ...]` line from the message.
    if _inj.get("timestamp", True):
        timestamped_message = f"[{_ts}{_ctx_parts}]\n{request.message}"
    else:
        timestamped_message = request.message

    locked_provider = get_chat_provider(active_chat_id)
    _forked = is_chat_fork(active_chat_id)
    if locked_provider and locked_provider != current_provider and not _forked:
        # Qwen chats are locked to Qwen only (upstream session coupling).
        # Forked chats are unlocked — any provider switch is allowed.
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

    # ── Lazy upstream-session creation (fixes forked Qwen chats) ──
    # A forked chat is created WITHOUT an upstream_session_id, because the fork
    # may be used with any provider. If the user stays on Qwen, message 1 appears
    # to work — but only because fork_history is injected inline. Message 2 has
    # neither a session nor an injection, so the context silently vanishes.
    # Create the session on the first send and persist it so every later turn
    # reuses it. No-op for any chat that already has a session.
    if (
        not _upstream_session_id
        and not scraper_enabled
        and not _is_api_model(request.model)
        and current_provider == "qwen"
    ):
        try:
            _upstream_session_id = await retry_async(
                lambda: service.create_chat(model=request.model, bdd=bdd),
                label="lazy_create_chat",
            )
        except Exception as _sess_exc:
            logger.warning("[session-lazy] create_chat failed for %s: %s", active_chat_id, _sess_exc)
            _upstream_session_id = None
        if _upstream_session_id:
            try:
                from server.database import set_upstream_session_id as _set_usid_lazy
                _set_usid_lazy(active_chat_id, _upstream_session_id)
                logger.info(
                    "[session-lazy] Created upstream session %s for chat %s (forked=%s)",
                    _upstream_session_id, active_chat_id, _forked,
                )
            except Exception as _set_exc:
                logger.warning("[session-lazy] Failed to persist session id: %s", _set_exc)

    set_title_if_default(active_chat_id, title)
    # Server tail is authoritative — client leaf is fallback for first-message only.
    #
    # `chats.parent_id` MUST hold an upstream node token (Qwen response_id), never
    # a local SQLite row id. A digit-only value is a legacy corruption from an
    # earlier stop-path bug: Qwen rejects it with PARENT_NOT_FOUND, and the old
    # recovery nulled the parent and branched from root — losing the whole
    # conversation. Treat any digit-only tail as "no parent" and fall back to the
    # client leaf so the turn at least starts from a real upstream node.
    _stored_parent = get_parent_id(active_chat_id, None)
    # The digit-only guard exists because Qwen parent IDs are UUIDs/hashes —
    # a bare integer means someone accidentally stored a local SQLite row id.
    # However, DeepSeek uses small integers (2, 4, 6…) as response_message_ids,
    # which ARE valid upstream tokens. Only drop digits for Qwen chats.
    if _stored_parent and str(_stored_parent).isdigit() and current_provider == "qwen":
        logger.warning(
            "[parent-guard] Dropping digit-only tail %r for chat %s (local row id, not an upstream token)",
            _stored_parent, active_chat_id,
        )
        touch_chat(active_chat_id, None)
        _stored_parent = None
    parent_id = _stored_parent or request.parent_id
    # Persist last-used model and thinking mode per-chat so switching tabs restores them
    try:
        save_chat_model_settings(active_chat_id, request.model, request.thinking_mode)
    except Exception:
        pass
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
        except Exception as e:
            logger.warning("Checkpoint save failed for chat %s msg %s: %s", active_chat_id, _user_msg_id, e)
    # Separate API payload so injections don't leak into session history
    # Build context FIRST, user message always at the very end
    _context_parts: list[str] = []
    _msg_count = get_db().execute(
        "SELECT COUNT(*) as c FROM messages WHERE chat_id = ?", (active_chat_id,)
    ).fetchone()["c"]

    # ponytail: skip chat_title injection for critique/agent sub-chats — they're ephemeral
    # and forcing a title tool call wastes a round on every sub-chat turn
    _is_sub_chat = "-critique-" in active_chat_id or "-agent-" in active_chat_id
    if _local_use_utilities and parent_id is None and _msg_count <= 1 and not _is_sub_chat:
        # ponytail: study mode has no chat_title tool (get_study_tool_schemas) and
        # its directive replaces the chat harness — injecting the mandatory-title
        # nudge just makes the model hallucinate a tool call it can't make.
        if _layout_mode != "study" and _inj.get("chat_title", True):
            _context_parts.append('[SYSTEM: You MUST call the chat_title tool now to set a title for this new conversation. This is mandatory.]')
        # Inject platform info so the model uses correct paths and commands
        if _inj.get("platform", True):
            import platform as _plat
            _sys = _plat.system()  # 'Linux', 'Windows', 'Darwin'
            _plat_hints = {
                'Linux': 'Use Linux paths (/home/user/..., /tmp/...) and commands (ls, cat, grep, etc.). Shell is typically bash/fish/zsh.',
                'Windows': r'Use Windows paths (C:\Users\..., %TEMP%\...) and commands (dir, type, findstr, PowerShell cmdlets). Use backslashes or forward slashes.',
                'Darwin': 'Use macOS paths (/Users/..., /tmp/...) and commands (ls, cat, grep). Shell is zsh by default. Homebrew is the common package manager.',
            }
            _hint = _plat_hints.get(_sys, f'Platform: {_sys}. Use appropriate paths and commands for this OS.')
            _context_parts.append(f'[SYSTEM PLATFORM: The user is on {_sys}. {_hint}]')
        try:
            from server.database import get_upcoming_schedules
            _upcoming = get_upcoming_schedules(days=10) if _inj.get("schedule", True) else []
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
    if _memory_context and _inj.get("memory", True):
        _context_parts.append(_memory_context)
    # Study grounding rides in the API payload only — it must never be written
    # to the stored user message (timestamped_message is what gets persisted),
    # so reopening the chat shows the user's real question, not the doc dump.
    if _study_grounding:
        _context_parts.append(_study_grounding)
    # Assemble: context first, user message always last
    if _context_parts:
        api_message = "\n\n".join(_context_parts) + "\n\n" + timestamped_message
    else:
        api_message = timestamped_message

    resolved_files: list[dict[str, Any]] | None = None
    _ref_files_full: list[dict[str, Any]] | None = None
    _backend = _resolve_api_backend(request.model) if _is_api_model(request.model) else None

    # Guard: a model that DECLARES an api_backend but whose connector is not
    # available must NOT silently fall through to the Qwen browser path. That
    # silent fallback is how deepseek-scraper ended up running on Qwen when
    # its active-account profile was missing: _is_api_model() returned False,
    # the router took the else-branch, and the user got a different provider
    # with no warning. Fail loudly and point at the real cause.
    try:
        from connectors import resolve_backend as _declared_backend, is_backend_available as _bavail
        _declared = _declared_backend(request.model)
        if _declared and not _bavail(_declared):
            _msg = (
                "Model '" + str(request.model) + "' routes to backend '" + str(_declared)
                + "', but that connector is not available (its browser profile or "
                + "credentials are missing). Refusing to silently fall back to Qwen. "
                + "Fix the account/credentials in Settings, then retry."
            )
            logger.error('backend_unavailable model=%s declared=%s', request.model, _declared)
            return {'error': _msg}
    except Exception:
        pass
    if request.files:
        # --- Doc→Image conversion for models that accept images but not native docs ---
        _model_caps = get_model_config(request.model).get("capabilities", {})
        _should_convert_docs = (
            _model_caps.get("image", False)
            and not _model_caps.get("document", False)
        )
        # Qwen accepts documents natively (STS filetype="file", parsed
        # server-side) — no doc→image conversion needed.

        _expanded_files: list[dict[str, Any]] = []
        if _should_convert_docs:
            from connectors.common.media import is_convertible_doc, convert_doc_to_images
            for f in request.files:
                fpath = f.get("path", "")
                if fpath and is_convertible_doc(fpath):
                    _img_paths = convert_doc_to_images(fpath)
                    if _img_paths:
                        for _ip in _img_paths:
                            _expanded_files.append({"path": _ip})
                        logger.info(
                            "Converted doc %s → %d images for model %s",
                            Path(fpath).name, len(_img_paths), request.model,
                        )
                    else:
                        # Conversion failed or too many pages — pass original through
                        _expanded_files.append(f)
                else:
                    _expanded_files.append(f)
        else:
            _expanded_files = list(request.files)

        resolved_files = []
        for f in _expanded_files:
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
            # Experimental scraper: engine uploads via Playwright internally
            if _backend in ("deepseek_scraper", "qwen_scraper"):
                if "path" in f:
                    resolved_files.append({"path": f["path"]})
                continue
            # ChatGPT ghost: stage via the SPA's own composer (#upload-files).
            # Keep the RAW local path — the connector uploads it through the
            # ghost tab (session.upload_file). Without this branch the file
            # fell through to the Qwen OSS upload, whose metadata has no
            # 'path', so `files` never reached the connector and every
            # attachment was silently dropped.
            if _backend == "chatgpt":
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
            # z.ai (GLM): multipart POST /api/v1/files/, returns the full file
            # object the completion body's `files` array expects.
            if _backend == "zai":
                if "id" in f and "url" in f:
                    resolved_files.append(f)
                elif "path" in f:
                    try:
                        from connectors.zai.client import get_client as _zai_get
                        meta = await _zai_get().upload_file(f["path"])
                        resolved_files.append(meta)
                    except Exception as exc:
                        logger.warning("z.ai upload failed for %s: %s", f["path"], exc)
                continue
            # Gemini Web: resumable upload to push.clients6.google.com, returns
            # the blob path that rides inner[0][3] of the StreamGenerate body.
            if _backend == "gemini_web":
                if "path" in f:
                    try:
                        from connectors.gemini_web.client import get_client as _gw_get
                        meta = await _gw_get().upload_file(f["path"])
                        resolved_files.append(meta)
                    except Exception as exc:
                        logger.warning("gemini_web upload failed for %s: %s", f["path"], exc)
                continue
            # MiMo Studio: 3-step genUploadInfo -> FDS PUT -> parse, returns the
            # multiMedias object the bot/chat body expects.
            if _backend == "mimo":
                if "path" in f:
                    try:
                        from connectors.mimo.client import get_client as _mimo_get
                        meta = await _mimo_get().upload_file(f["path"])
                        resolved_files.append(meta)
                    except Exception as exc:
                        logger.warning("mimo upload failed for %s: %s", f["path"], exc)
                continue
            # Mistral (Web): 3-step Azure Blob upload (uploadFile -> PUT ->
            # confirmUpload), returns the {type,url,name} object the chat
            # body's messageFiles array expects.
            if _backend == "mistral_web":
                if "path" in f:
                    try:
                        from connectors.mistral_web.client import get_client as _mw_get
                        meta = await _mw_get().upload_file(f["path"])
                        resolved_files.append(meta)
                    except Exception as exc:
                        logger.warning("mistral_web upload failed for %s: %s", f["path"], exc)
                continue
            # Kimi (kimi.ai): ghost-session multipart upload, returns `file_id`
            # which rides on the Chat message's file blocks via ref_file_ids.
            if _backend == "kimi":
                if "path" in f:
                    try:
                        from connectors.kimi.client import get_client as _kimi_get
                        meta = await _kimi_get().upload_file(f["path"])
                        resolved_files.append(meta)
                    except Exception as exc:
                        logger.warning("kimi upload failed for %s: %s", f["path"], exc)
                continue
            # Qwen / others: upload to Qwen OSS (images + documents)
            if "id" in f and "url" in f:
                resolved_files.append(f)
            elif "path" in f:
                meta = await service.upload_file(f["path"])
                if meta:
                    resolved_files.append(meta)
                else:
                    logger.warning("Could not resolve file: %s", f["path"])

        # Derive ref_file_ids from freshly uploaded files. The frontend only sends
        # `ref_file_ids` for already-uploaded files (e.g. re-sent history); a brand
        # new upload returns its id in resolved_files and MUST be forwarded or the
        # model receives no attachment reference at all.
        # Uploads return `id` (and `file.id`), not `file_id` — accept either so
        # freshly attached files are forwarded to the model's attachment refs.
        _uploaded_file_ids = [
            str(rf.get("file_id") or rf.get("id")) for rf in resolved_files
            if (rf.get("file_id") or rf.get("id")) and str(rf.get("file_id") or rf.get("id")).strip()
        ]
        if _uploaded_file_ids:
            _merged = list(request.ref_file_ids or [])
            for _fid in _uploaded_file_ids:
                if _fid not in _merged:
                    _merged.append(_fid)
            request.ref_file_ids = _merged
            logger.info("Attaching %d uploaded file(s) to request: %s", len(_uploaded_file_ids), _uploaded_file_ids)
        # z.ai (GLM) + Gemini Web need the FULL uploaded file objects, not just
        # ids: z.ai puts them in the completion `files` array, gemini_web maps
        # each blob into StreamGenerate's inner[0][3]. Pass them verbatim.
        _ref_files_full = (
            [rf for rf in resolved_files if rf.get("id") or rf.get("blob")]
            if _backend in ("zai", "gemini_web") else None
        )
        if _backend == "mimo":
            # MiMo media objects use `url` (parse id) + `fileUrl`; keep them verbatim.
            _ref_files_full = [rf for rf in resolved_files if rf.get("fileUrl")] or None
        if _backend == "mistral_web":
            # Mistral messageFiles use `url` + `name`; keep them verbatim.
            _ref_files_full = [rf for rf in resolved_files if rf.get("url")] or None
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
        # Safety net: pull any leaked <think>…</think> out of the answer.
        answer, _leaked_think = split_thinking(answer)
        if _leaked_think:
            thinking = (thinking + "\n" + _leaked_think).strip() if thinking else _leaked_think
        final_parent = result.get("parent_id") or parent_id
        error = result.get("error")
        scraper_chat_url = result.get("chat_url")
        if scraper_chat_url:
            save_chat_url(active_chat_id, scraper_chat_url)
        _scraper_pt = count_prompt_tokens(system_instruction=_system_instruction_for_tokens, user_message=api_message, memory_context=_memory_context or "")
        _scraper_ct = count_completion_tokens(answer or error or "", thinking)
        add_message(active_chat_id, "assistant", answer or error or "", thinking, final_parent,
                    prompt_tokens=_scraper_pt, completion_tokens=_scraper_ct)
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
    # Build system instruction for token counting (includes persona + tools schema)
    # Agent sub-chats override the system prompt entirely (no Maria persona)
    _system_instruction_for_tokens = ""
    if _study_system:
        # Study mode replaces the whole chat/agent harness (no chat_title, no
        # persona) with the study directive built above.
        _system_instruction_for_tokens = _study_system
    elif request.system_prompt:
        _system_instruction_for_tokens = request.system_prompt
    else:
        try:
            from connectors.common.instruction_builder import build_instructions
            _system_instruction_for_tokens = build_instructions(project_id=_project_id)
        except Exception:
            pass
    if not request.stream and _is_api_model(request.model):
        # _backend already resolved above at file resolution stage
        _api_backend = _backend or _resolve_api_backend(request.model)
        _connector = get_connector(_api_backend, model_id=request.model)
        _cfg = get_model_config(request.model)
        _api_model = _cfg.get("api_model_type", _cfg["id"])
        # ponytail: DeepSeek unified — no separate vision model, no ephemeral mode needed
        _ephemeral = False
        # Collect local file paths for direct-read backends (base64 inline)
        _inline_files = None
        if _api_backend in _DIRECT_READ_BACKENDS and resolved_files:
            _inline_files = [f.get('path') for f in resolved_files if f.get('path')]
        _max_session_chars = _cfg.get("max_session_chars")
        # Load DB history for cross-provider session seeding
        _db_history = None
        if not _ephemeral and active_chat_id:
            try:
                _db_history = _load_history_with_skills(active_chat_id)
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
        if _ref_files_full:
            _chat_kwargs["ref_files"] = _ref_files_full
        if _api_backend == "local":
            _chat_kwargs["model_id"] = request.model
        if _max_session_chars:
            _chat_kwargs["max_session_chars"] = _max_session_chars
        if _inline_files:
            _chat_kwargs['files'] = _inline_files
        result = await _connector.chat(**_chat_kwargs)
        answer = str(result.get("answer", ""))
        thinking = str(result.get("thinking", ""))
        # Safety net: pull any leaked <think>…</think> out of the answer.
        answer, _leaked_think = split_thinking(answer)
        if _leaked_think:
            thinking = (thinking + "\n" + _leaked_think).strip() if thinking else _leaked_think
        final_parent = result.get("parent_id") or parent_id
        error = result.get("error")
        _api_pt = count_prompt_tokens(
            system_instruction=_system_instruction_for_tokens,
            history=_db_history or [], tools=_chat_kwargs.get('tools'),
            user_message=api_message, memory_context=_memory_context or "",
        )
        _api_ct = count_completion_tokens(answer or error or "", thinking)
        add_message(active_chat_id, "assistant", answer or error or "", thinking, final_parent,
                    prompt_tokens=_api_pt, completion_tokens=_api_ct)
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
        # Safety net: pull any leaked <think>…</think> out of the answer.
        answer, _leaked_think = split_thinking(answer)
        if _leaked_think:
            thinking = (thinking + "\n" + _leaked_think).strip() if thinking else _leaked_think
        final_parent = result.get("parent_id") or parent_id
        error = result.get("error")
        _qwen_pt = count_prompt_tokens(system_instruction=_system_instruction_for_tokens, user_message=api_message, memory_context=_memory_context or "")
        _qwen_ct = count_completion_tokens(answer or error or "", thinking)
        add_message(active_chat_id, "assistant", answer or error or "", thinking, final_parent,
                    prompt_tokens=_qwen_pt, completion_tokens=_qwen_ct)
        touch_chat(active_chat_id, final_parent)
        result["memory_used"] = _memory_used
        return result
    async def _drain_sync_gen(gen):
        """Run a sync generator in a thread pool, yield results back without blocking the event loop.

        CANCELLATION-SAFE: the executor future is retained and cancelled in the
        finally block. Without this, a client disconnect (Stop button) killed the
        async reader while the worker thread kept iterating ``gen`` forever,
        pushing into an orphaned queue — holding the upstream scraper/connector
        locks and wedging the NEXT request at "processing" indefinitely.

        On teardown we also close the sync generator so any ``finally`` inside it
        (and the async producer it wraps) runs. A timeout on the queue read is a
        last-resort backstop so a wedged producer can never hang the stream.
        """
        queue: asyncio.Queue = asyncio.Queue()
        _sentinel = object()

        def _run():
            try:
                for item in gen:
                    queue.put_nowait(item)
            except Exception as _run_exc:  # never let the thread die silently
                logger.debug("_drain_sync_gen worker raised: %s", _run_exc)
            finally:
                queue.put_nowait(_sentinel)

        loop = asyncio.get_running_loop()
        # Cache the main loop on the agent runtime so thread-pool handlers can schedule coroutines
        from engine.agents import get_runtime as _get_rt
        _get_rt()._loop = loop
        # Propagate ContextVars (chat_id etc.) into the executor thread — Python 3.14 doesn't do this automatically
        import contextvars as _cv
        _ctx = _cv.copy_context()
        _fut = loop.run_in_executor(None, _ctx.run, _run)
        try:
            while True:
                # Timeout is a backstop: a genuinely wedged producer (dead browser,
                # stuck lock) would otherwise hang here forever. 300s is far beyond
                # any real inter-chunk gap but short of "infinite".
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=300.0)
                except (asyncio.TimeoutError, TimeoutError):
                    logger.error("_drain_sync_gen: producer stalled >300s — abandoning")
                    break
                if item is _sentinel:
                    break
                yield item
        finally:
            # Cancel the worker future, then try to close the sync generator so
            # its (and the wrapped producer's) cleanup runs. Both are best-effort:
            # the thread may be mid-iteration and close() may raise ValueError
            # ("generator already executing"), which is harmless.
            try:
                _fut.cancel()
            except Exception:
                pass
            try:
                gen.close()
            except (ValueError, RuntimeError, GeneratorExit):
                pass
            except Exception as _close_exc:
                logger.debug("_drain_sync_gen gen.close() raised: %s", _close_exc)

    async def _close_upstream_gen(gen) -> None:
        """Close an async upstream generator (or its async iterator) safely.

        Called before a retry REPLACES the in-flight upstream stream. Without it
        the abandoned stream_events()/stream_chat() generator never runs its own
        cancel/finally handler — leaving _response_lock / _lock held, so the next
        retry (and the next user turn) deadlocks. Best-effort: a close racing an
        in-flight __anext__ raises RuntimeError, which is harmless here.
        """
        if gen is None:
            return
        try:
            _aclose = getattr(gen, "aclose", None)
            if _aclose is not None:
                await _aclose()
            elif hasattr(gen, "close"):
                gen.close()
        except (asyncio.CancelledError, GeneratorExit):
            pass
        except Exception as _exc:
            logger.debug("_close_upstream_gen failed: %s", _exc)

    async def event_stream():
        nonlocal active_chat_id, _upstream_session_id
        # Study tools (highlight / note / pdf read) read their document via a
        # ContextVar. Async generators run in their own context, so setting it
        # here is isolated to this turn and discarded when the generator ends.
        _study_ctx = None
        if _layout_mode == "study":
            try:
                from engine.study.context import StudyContext, set_study_context
                _sc = request.study_context or {}
                try:
                    _sc_page = int(_sc.get("page") or 0)
                except (TypeError, ValueError):
                    _sc_page = 0
                _study_ctx = StudyContext(doc_id=str(_sc.get("doc_id") or ""), page=_sc_page)
                set_study_context(_study_ctx)
            except Exception:
                logger.warning("study context init failed", exc_info=True)
        answer_parts: list[str] = []
        _raw_answer_parts: list[str] = []  # pre-parser raw text for guard checks
        thinking_parts: list[str] = []
        skill_events: list[dict[str, Any]] = []
        # Current round's tool events. They are merged into skill_events only AFTER
        # a round fully completes, so this must survive until the cancel handler runs.
        round_skill_events: list[dict[str, Any]] = []
        final_parent = parent_id
        error_message: str | None = None
        current_message = api_message
        current_parent = parent_id
        # Fork history injection: prepend conversation history to the first
        # message so the fresh upstream session has context. Works for both
        # Qwen (server-side upstream_session_id) and DeepSeek (client-side
        # chat_session_id keyed on the local chat_id). fork_history is written
        # by the fork endpoint and NULLed after injection — its presence is
        # the signal, no provider gate needed here.
        # NOTE: read + prepend only. fork_history is NOT cleared here — it is
        # NULLed after a fully successful turn (see success path below). Clearing
        # at injection time meant any failed / empty-200 / rotated first send lost
        # the fork context permanently — the "context vanishes for no reason" bug.
        _fork_history_injected = False
        if parent_id is None:
            try:
                with get_db() as _fh_conn:
                    _fh_row = _fh_conn.execute(
                        "SELECT fork_history FROM chats WHERE id = ?", (active_chat_id,)
                    ).fetchone()
                    if _fh_row and _fh_row["fork_history"]:
                        current_message = _fh_row["fork_history"] + current_message
                        _fork_history_injected = True
                        logger.info("[fork-history] Injected fork history into first message for %s", active_chat_id)
            except Exception as _fh_err:
                logger.warning("[fork-history] Failed to inject fork history: %s", _fh_err)
        round_index = 0
        saved_message_id: int | None = None
        _pending_skill_images: list[str] = []  # image paths from get_file to inject next round
        _guard = MainChatGuard(provider=_backend)
        _loop_detector = LoopDetector()  # Error-aware loop detection + no-progress + stubbing
        _turn_caps = TurnCapTracker()  # Per-turn caps on web searches and subagent spawns
        _round_tool_errors: dict[str, str] = {}  # tool_name → error_msg from previous round
        _all_tool_mem_used: list[dict[str, Any]] = []
        # Bound before the loop so the cancel handler can always close whatever
        # upstream generator is in flight (see except (CancelledError, GeneratorExit)).
        _main_iter: Any = None
        round_event_source: Any = None
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
            _agent_run = None
            _agent_killed = False  # Set True on CancelledError to prevent finally from marking completed
            if "-agent-" in active_chat_id:
                try:
                    from engine.agents import get_runtime
                    from server.database import update_agent_status
                    _agent_run = get_runtime().get_agent(active_chat_id)
                    if _agent_run:
                        _agent_run.mark_running()
                        update_agent_status(active_chat_id, "running")
                except Exception:
                    _agent_run = None

            # Teacher escalation state — set inside sync _dispatch_events,
            # consumed in async context after dispatch completes.
            _pending_teacher_escalation: dict[str, Any] = {"needed": False}

            while True:
                _pending_teacher_escalation = {"needed": False}  # Reset each round
                round_skill_events: list[dict[str, Any]] = []
                round_thinking_parts: list[str] = []
                round_answer_parts: list[str] = []
                # Ordered text/ui_block for this round so history replays in the
                # exact order they were emitted (was: all text, then all tools).
                round_flow: list[dict[str, Any]] = []
                # <render_html> panes stream as html_start/chunk/end deltas.
                # Accumulate per block id so the whole pane can be persisted
                # ONCE at seal, instead of a thousand chunk events in the DB.
                _html_acc: dict[str, dict[str, Any]] = {}
                _round_raw_parts: list[str] = []  # per-round raw text for guard checks
                pending_thinking: list[str] = []
                parser = _get_skill_engine().create_parser()
                # Provider-agnostic safety net: split any <think>…</think> that
                # leaked into the answer stream into proper thinking events.
                # Connectors that already separate reasoning are a no-op here.
                _think_splitter = ThinkTagSplitter()
                _title_buf = ""  # buffer for partial <title> tags in text stream
                _TITLE_RE = re.compile(r"<chat_title>(.*?)</chat_title>", re.S | re.I)
                _halt_dispatch = False  # Set True when permission/cwd gate fires — stops remaining tools in this round
                def _dispatch_events(items) -> Generator[str, None, None]:
                    """Shared logic: route parser events, execute tags via engine."""
                    nonlocal _title_buf, current_parent, final_parent, _halt_dispatch
                    engine = _get_skill_engine()
                    for item in items:
                        if _halt_dispatch:
                            break  # Stop processing further tools after a permission/cwd gate
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
                                if round_flow and round_flow[-1].get("type") == "round_text":
                                    round_flow[-1]["text"] += chunk
                                else:
                                    round_flow.append({"type": "round_text", "text": chunk})
                                yield sse({"type": "answer", "text": chunk})
                        elif itype == "tag_found":
                            # Meta tags: intercept before skill dispatch
                            if item["name"] == "chat_title":
                                _ct_id = str(uuid.uuid4())
                                _title_text = str(
                                    item.get("content")
                                    or item.get("attrs", {}).get("title")
                                    or item.get("attrs", {}).get("text")
                                    or ""
                                ).strip()
                                if _title_text:
                                    update_chat_title(active_chat_id, _title_text[:80])
                                    yield sse({"type": "chat_title", "title": _title_text[:80]})
                                # Emit proper skill events with ID so build_tool_feedback
                                # generates feedback → auto-loop continues → model sends real text
                                round_skill_events.append({"type": "skill_start", "name": "chat_title", "id": _ct_id})
                                round_skill_events.append({"type": "skill_end", "name": "chat_title", "ok": True, "id": _ct_id, "duration_ms": 0})
                                continue

                            # Subagent spawn: intercept before engine.process_tag so raw XML
                            # never leaks into the chat as visible text. The handler yields
                            # agent_start / agent_trigger / skill_end events directly.
                            if item["name"] == "spawn_agent":
                                _sa_id = str(uuid.uuid4())[:12]
                                round_skill_events.append({"type": "skill_start", "name": "spawn_agent", "id": _sa_id})
                                yield sse({"type": "skill_start", "name": "spawn_agent", "id": _sa_id})
                                for ev in engine.process_tag(
                                    item["name"], item.get("attrs", {}), item.get("content", ""),
                                    chat_id=active_chat_id,
                                    cwd=request.cwd,
                                ):
                                    if ev.get("type") in ("skill_start", "skill_output", "skill_end"):
                                        round_skill_events.append(ev)
                                    yield sse(ev)
                                continue

                            # Chat mode: block all tools except web_search/online_search/chat_title
                            if _layout_mode == "chat" and item["name"] not in (
                                "online_search", "chat_title",
                            ):
                                _cm_id = str(uuid.uuid4())[:12]
                                _cm_err = f"[Chat mode] Tool '{item['name']}' is disabled. Only web search is available."
                                round_skill_events.append({"type": "skill_start", "name": item["name"], "id": _cm_id})
                                round_skill_events.append({"type": "skill_end", "name": item["name"], "ok": False, "error": _cm_err, "id": _cm_id})
                                yield sse({"type": "skill_end", "name": item["name"], "ok": False, "error": _cm_err, "id": _cm_id})
                                continue

                            # Subagent guard: block interactive/multiagent tools
                            _SUBAGENT_BLOCKED = frozenset({"ask_user", "multi_agent", "spawn_agent"})
                            if "-agent-" in str(active_chat_id) and item["name"] in _SUBAGENT_BLOCKED:
                                _sa_block_id = str(uuid.uuid4())[:12]
                                _sa_err = f"[GUARD] Tool '{item['name']}' is blocked for subagents. Subagents cannot prompt users or spawn nested agents."
                                round_skill_events.append({"type": "skill_start", "name": item["name"], "id": _sa_block_id})
                                round_skill_events.append({"type": "skill_end", "name": item["name"], "ok": False, "error": _sa_err, "id": _sa_block_id})
                                yield sse({"type": "skill_end", "name": item["name"], "ok": False, "error": _sa_err, "id": _sa_block_id})
                                continue

                            # Track command for loop detection (legacy MainChatGuard)
                            _guard.record_command(item["name"], item.get("content", ""))
                            # LoopDetector: error-aware check with recovery support
                            _tool_args_str = item.get("content", "")
                            _prev_error = _round_tool_errors.get(item["name"])
                            _decision = _loop_detector.check_decision(
                                item["name"], _tool_args_str, error_msg=_prev_error or "",
                            )
                            if _decision.action == "block":
                                _stop_msg = _decision.message or f"[HARD STOP] '{item['name']}' blocked."
                                _lp_id = str(uuid.uuid4())[:12]
                                round_skill_events.append({"type": "skill_start", "name": item["name"], "id": _lp_id})
                                round_skill_events.append({"type": "skill_end", "name": item["name"], "ok": False, "error": _stop_msg, "id": _lp_id})
                                yield sse({"type": "skill_end", "name": item["name"], "ok": False, "error": _stop_msg, "id": _lp_id})
                                continue
                            elif _decision.action == "recover":
                                # Recovery: reset session, inject recovery prompt
                                logger.info("[main-chat] Guardrail recovery triggered for '%s'", item["name"])
                                yield sse({"type": "guardrail_recovery", "tool": item["name"]})
                                current_parent = None  # Fresh upstream session
                                final_parent = None
                                _recovery_prompt = _loop_detector.get_recovery_prompt(
                                    _decision.recovery_key,
                                    original_task=api_message or "",
                                )

                                # Flag for teacher escalation — collected here (sync),
                                # executed after _dispatch_events returns (async context).
                                if _agent_run:
                                    _pending_teacher_escalation["needed"] = True
                                    _pending_teacher_escalation["tool_name"] = item["name"]
                                    _pending_teacher_escalation["reason"] = (
                                        _decision.message or "repeated failures"
                                    )
                                    _pending_teacher_escalation["recovery_prompt"] = _recovery_prompt

                                # Block this tool and all remaining tools this round
                                _lp_id = str(uuid.uuid4())[:12]
                                round_skill_events.append({"type": "skill_start", "name": item["name"], "id": _lp_id})
                                round_skill_events.append({"type": "skill_end", "name": item["name"], "ok": False, "error": _recovery_prompt, "id": _lp_id})
                                yield sse({"type": "skill_end", "name": item["name"], "ok": False, "error": _recovery_prompt, "id": _lp_id})
                                # Skip remaining tools this round — force re-think
                                break
                            elif _decision.action == "warn":
                                _lw_id = str(uuid.uuid4())[:12]
                                _warn_text = _decision.message or ""
                                round_skill_events.append({"type": "skill_start", "name": "_loop_warning", "id": _lw_id})
                                round_skill_events.append({"type": "skill_output", "text": _warn_text, "id": _lw_id})
                                round_skill_events.append({"type": "skill_end", "name": "_loop_warning", "ok": True, "id": _lw_id})
                                yield sse({"type": "skill_output", "text": _warn_text, "id": _lw_id})
                            # Per-turn cap check
                            _cap_warn = _turn_caps.check_and_record(item["name"])
                            if _cap_warn:
                                _cap_id = str(uuid.uuid4())[:12]
                                round_skill_events.append({"type": "skill_start", "name": item["name"], "id": _cap_id})
                                round_skill_events.append({"type": "skill_end", "name": item["name"], "ok": False, "error": _cap_warn, "id": _cap_id})
                                yield sse({"type": "skill_end", "name": item["name"], "ok": False, "error": _cap_warn, "id": _cap_id})
                                continue
                            # Execute the tag through the middleware pipeline
                            for ev in engine.process_tag(
                                item["name"], item.get("attrs", {}), item.get("content", ""),
                                chat_id=active_chat_id,
                                cwd=request.cwd,
                            ):
                                if ev.get("type") in ("skill_start", "skill_output", "skill_end", "file_edit", "permission_request", "cwd_warning"):
                                    round_skill_events.append(ev)
                                yield sse(ev)
                                # When permission is requested, emit a transient status (not saved as answer)
                                if ev.get("type") == "permission_request":
                                    _halt_dispatch = True  # Stop processing remaining tools in this round
                                    yield sse({"type": "approval_pending", "text": "⏳ Waiting for your approval on that command."})
                                # When CWD warning is emitted, emit a transient status
                                if ev.get("type") == "cwd_warning":
                                    _halt_dispatch = True  # Stop processing remaining tools in this round
                                    yield sse({"type": "cwd_warning_pending", "text": "⚠️ File operation outside project folder detected."})
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
                            _pe_id = str(uuid.uuid4())[:12]
                            # Emit skill_start so frontend transitions out of pending animation
                            _pe_start = {"type": "skill_start", "id": _pe_id, "name": "action_parse"}
                            round_skill_events.append(_pe_start)
                            yield sse(_pe_start)
                            _pe_end = {
                                "type": "skill_end",
                                "id": _pe_id,
                                "name": "action_parse",
                                "ok": False,
                                "error": f"{_pe_reason} | Received: {_pe_raw}",
                            }
                            round_skill_events.append(_pe_end)
                            yield sse({"type": "skill_output", "name": "action_parse", "text": f"⚠️ {_pe_reason}", "id": _pe_id})
                            yield sse(_pe_end)
                        elif itype == "ui_block":
                            # Rich UI block — a response format, not a tool.
                            # Forward straight to the frontend; no skill lifecycle,
                            # no tool feedback, turn never pauses.
                            #
                            # PARTIAL events (streaming build-up) go to the
                            # client ONLY — they must NOT persist, or history
                            # would replay a dozen half-built copies of the card.
                            # The FINAL event (partial=False) is what round_flow
                            # keeps, so replay draws the card once, finished.
                            _ui_partial = bool(item.get("partial"))
                            _ui_payload = item.get("payload")
                            _fwd = {"type": "ui_block"}
                            if item.get("id") is not None:
                                _fwd["id"] = item.get("id")
                            # MUST forward `partial` — without it the client
                            # treats every streaming update as a final seal and
                            # starts a fresh node each chunk (the 200-SVGs bug).
                            _fwd["partial"] = _ui_partial
                            if item.get("remove"):
                                _fwd["remove"] = True
                            if _ui_payload:
                                _fwd["payload"] = _ui_payload
                            if not _ui_partial:
                                if _ui_payload:
                                    round_flow.append({"type": "ui_block", "payload": _ui_payload})
                            yield sse(_fwd)
                        elif itype in ("html_start", "html_chunk", "html_end"):
                            # Sandboxed HTML pane — a response format, not a tool.
                            # Live events forward straight to the client; the
                            # body is accumulated and persisted at seal so a
                            # reload replays the pane exactly as it streamed.
                            _hhid = item.get("id") or "html_hist"
                            if itype == "html_start":
                                _html_acc[_hhid] = {
                                    "title": item.get("title") or "",
                                    "height": item.get("height"),
                                    # url panes frame a live page instead of
                                    # accumulating a body; keep it so a reload
                                    # replays the same pane.
                                    "url": item.get("url") or "",
                                    "src": "",
                                }
                            elif itype == "html_chunk":
                                _hacc = _html_acc.get(_hhid)
                                if _hacc is not None:
                                    _hacc["src"] += str(item.get("text") or "")
                            else:  # html_end
                                _hacc = _html_acc.pop(_hhid, None)
                                if _hacc is not None:
                                    _hstart = {
                                        "type": "html_start",
                                        "id": _hhid,
                                        "title": _hacc["title"],
                                        "height": _hacc["height"],
                                    }
                                    if _hacc.get("url"):
                                        _hstart["url"] = _hacc["url"]
                                    round_flow.append(_hstart)
                                    round_flow.append({
                                        "type": "html_chunk",
                                        "id": _hhid,
                                        "text": _hacc["src"],
                                    })
                                    _hend = {"type": "html_end", "id": _hhid}
                                    if item.get("error"):
                                        _hend["error"] = item["error"]
                                    round_flow.append(_hend)
                            yield sse(item)
                        else:
                            # tool_pending, tool_progress, etc — forward to frontend
                            if itype in ("skill_start", "skill_output", "skill_end", "file_edit", "permission_request"):
                                round_skill_events.append(item)
                            yield sse(item)
                def emit_parsed(text: str) -> Generator[str, None, None]:
                    # Route through the <think> splitter first so leaked
                    # reasoning never reaches the skill parser as prose.
                    for _kind, _seg in _think_splitter.feed(text):
                        if _kind == "thinking":
                            thinking_parts.append(_seg)
                            round_thinking_parts.append(_seg)
                            yield sse({"type": "thinking", "text": _seg})
                        else:
                            yield from _dispatch_events(parser.feed(_seg))
                def emit_flush() -> Generator[str, None, None]:
                    nonlocal _title_buf
                    # Drain any held-back partial tag from the splitter first.
                    for _kind, _seg in _think_splitter.flush():
                        if _kind == "thinking":
                            thinking_parts.append(_seg)
                            round_thinking_parts.append(_seg)
                            yield sse({"type": "thinking", "text": _seg})
                        else:
                            yield from _dispatch_events(parser.feed(_seg))
                    yield from _dispatch_events(parser.flush())
                    # Flush any remaining title buffer text
                    if _title_buf:
                        leftover = _title_buf
                        _title_buf = ""
                        answer_parts.append(leftover)
                        round_answer_parts.append(leftover)
                        if round_flow and round_flow[-1].get("type") == "round_text":
                            round_flow[-1]["text"] += leftover
                        else:
                            round_flow.append({"type": "round_text", "text": leftover})
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
                _round_prompt_tokens = 0  # initialized before branching; set in each path
                _qwen_chat_id = active_chat_id  # default; overridden in Qwen branch below

                # Structured request trace — one line per round so the log
                # always shows which model/provider/config handled this turn.
                _round_backend = (
                    _resolve_api_backend(request.model) if _is_api_model(request.model)
                    else ("qwen-scraper" if scraper_enabled else "qwen-service")
                )
                _round_cfg = get_model_config(request.model)
                logger.info(
                    "request chat=%s round=%d model=%s api_model=%s backend=%s "
                    "profile=%s thinking=%s tools=%s files=%d msg_len=%d",
                    str(active_chat_id)[:8] if active_chat_id else "new",
                    round_index, request.model,
                    _round_cfg.get("api_model_type", _round_cfg.get("id", "?")),
                    _round_backend or "qwen", bdd or "default",
                    request.thinking_mode or "default",
                    "yes" if _round_cfg.get("supports_tools") else "no",
                    len(resolved_files or []), len(current_message or ""),
                )
                # Log user message to file
                if round_index == 0:
                    _log_conversation(active_chat_id, request.model, "user", current_message)
                if _is_api_model(request.model):
                    _api_backend = _backend or _resolve_api_backend(request.model)
                    _connector = get_connector(_api_backend, model_id=request.model)
                    _cfg = get_model_config(request.model)
                    _api_model = _cfg.get("api_model_type", _cfg["id"])
                    # ponytail: DeepSeek unified — no separate vision model
                    _ephemeral = False
                    # deepseek_scraper critique sub-chats: share the parent's LIVE
                    # scraper browser on a new tab instead of a separate profile.
                    # The connector sees a pinned dir equal to the scraper's own
                    # active account and attaches an adopted sub-engine (new tab,
                    # same context/session). Non-scraper backends keep the
                    # critique handler's scoped profile.
                    _round_bdd = bdd
                    if _api_backend in ("deepseek_scraper", "qwen_scraper") and "-critique-" in str(active_chat_id or ""):
                        try:
                            from engine.config import _SYSTEM as _SYS, get_provider_active_account as _gpaa
                            _sc_acct = _gpaa(_api_backend)
                            _sc_path = _SYS / _sc_acct
                            if _sc_acct and _sc_path.is_dir():
                                _round_bdd = str(_sc_path)
                        except Exception:
                            logger.warning("critique scraper-profile override failed", exc_info=True)
                    # Collect local file paths for direct-read backends
                    _inline_files = None
                    if _api_backend in _DIRECT_READ_BACKENDS and round_index == 0 and resolved_files:
                        _inline_files = [f.get('path') for f in resolved_files if f.get('path')]
                    # Experimental scraper: engine uploads these via Playwright
                    elif _api_backend in ("deepseek_scraper", "qwen_scraper") and round_index == 0 and resolved_files:
                        _inline_files = [f.get('path') for f in resolved_files if f.get('path')]
                    # ChatGPT ghost: the connector stages files via the SPA's
                    # #upload-files input (set_input_files) and lets the SPA
                    # build messages[0].metadata.attachments itself.
                    elif _api_backend == "chatgpt" and round_index == 0 and resolved_files:
                        _inline_files = [f.get('path') for f in resolved_files if f.get('path')]
                    # Inject skill-produced images (from get_file) if model supports vision
                    _extra_ref_ids: list[str] = []
                    if _pending_skill_images:
                        _caps = _cfg.get("capabilities", {})
                        if _caps.get("image", False) and _api_backend in _DIRECT_READ_BACKENDS:
                            _inline_files = (_inline_files or []) + _pending_skill_images
                        elif _caps.get("image", False) and _api_backend in ("deepseek_scraper", "qwen_scraper"):
                            # Scraper transport: no inline-file path either, but the
                            # engine uploads these through Playwright (upload_file),
                            # so they ride the same `files` channel as UI attachments.
                            _inline_files = (_inline_files or []) + _pending_skill_images
                        elif _caps.get("image", False) and _api_backend == "deepseek":
                            # DeepSeek has no inline-file path: images must go through its own
                            # upload endpoint, and the returned file_id rides on ref_file_ids
                            # (which is otherwise None past round 0, i.e. exactly when tools run).
                            _ds_thinking = str(request.thinking_mode or "").lower() in ("thinking", "deepthink")
                            for _img_path in _pending_skill_images:
                                try:
                                    _meta = await service.upload_deepseek_file(
                                        _img_path, thinking_enabled=_ds_thinking
                                    )
                                    if _meta and _meta.get("file_id"):
                                        _extra_ref_ids.append(str(_meta["file_id"]))
                                    else:
                                        logger.warning("DeepSeek skill-image upload returned no id: %s", _img_path)
                                except Exception as _ds_up_exc:
                                    logger.warning("DeepSeek skill-image upload failed for %s: %s", _img_path, _ds_up_exc)
                            if not _extra_ref_ids:
                                current_message += (
                                    f"\n\n[NOTE: {len(_pending_skill_images)} image(s) were produced by tools "
                                    f"but could not be uploaded to DeepSeek. "
                                    f"The image content is not accessible.]"
                                )
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
                            _db_history_s = _load_history_with_skills(active_chat_id)
                        except Exception:
                            pass
                    _stream_kwargs: dict[str, Any] = dict(
                        message=current_message,
                        model=_api_model,
                        thinking_mode=request.thinking_mode,
                        chat_id=None if _ephemeral else active_chat_id,
                        ref_file_ids=((list(request.ref_file_ids or []) if round_index == 0 else []) + _extra_ref_ids) or None,
                        inject_instructions=not _ephemeral,
                        project_id=_project_id,
                        db_history=_db_history_s,
                        layout_mode=_layout_mode,
                    )
                    if _ref_files_full and round_index == 0:
                        _stream_kwargs["ref_files"] = _ref_files_full
                    # Study chats and agent sub-chats override the system prompt
                    # (skip the Maria persona + chat harness).
                    if _study_system:
                        _stream_kwargs["system_instruction"] = _study_system
                    elif request.system_prompt:
                        _stream_kwargs["system_instruction"] = request.system_prompt
                    if _api_backend == "local":
                        _stream_kwargs["model_id"] = request.model
                    if _max_session_chars_stream:
                        _stream_kwargs["max_session_chars"] = _max_session_chars_stream
                    if _inline_files:
                        _stream_kwargs['files'] = _inline_files
                    # Scoped browser profile (critique / agent sub-chats). Passed
                    # straight through to the scraper connector, which keys its
                    # engine map by it — without this the sub-chat silently
                    # shares the parent's browser and the two fight over
                    # active_chat_id, replaying new_chat() + hello forever.
                    if _round_bdd:
                        _stream_kwargs["browser_data_dir"] = _round_bdd
                    # Scraper transports: resume the stored upstream chat URL on
                    # the first round so a reopened chat continues its browser
                    # conversation instead of starting a fresh one.
                    if _api_backend in ("deepseek_scraper", "qwen_scraper") and round_index == 0 and active_chat_id:
                        try:
                            _stored_url = get_chat_url(active_chat_id)
                            if _stored_url:
                                _stream_kwargs["chat_url"] = _stored_url
                        except Exception:
                            pass
                    # Native tool calling: core/outer tier system
                    try:
                        from engine.tools_loader import (
                            get_all_tool_schemas,
                            get_outer_tool_stubs,
                            _build_load_tool_schema,
                        )
                        from server.api.routes.misc import get_disabled_tools as _get_dt
                        _disabled = _get_dt().get('disabled', [])

                        if _layout_mode == "study":
                            # Study mode: study tools + shared web/image tools, no
                            # chat_title and no agent tooling. The schemas match
                            # the study system directive exactly.
                            try:
                                from engine.tools_loader import get_study_tool_schemas
                                _tool_schemas = get_study_tool_schemas(_disabled)
                            except Exception:
                                _tool_schemas = []
                        elif _layout_mode == "chat":
                            # Chat mode: only web search + chat title, no tier filtering
                            _chat_allowed = ["online_search", "chat_title"]
                            _tool_schemas = get_all_tool_schemas(_disabled, allowed=_chat_allowed, tier=None)
                        else:
                            # Agent mode: full core/outer tier system
                            # For local models, filter to per-model configured tools
                            _allowed_tools = None
                            if _is_local_model:
                                _model_tools = _cookbook_cfg.get("tools")
                                if _model_tools is not None:
                                    # Explicit list (even empty) = use only those tools
                                    _allowed_tools = _model_tools if _model_tools else ["__none__"]
                            # Core tools: full schemas always loaded
                            _core_schemas = get_all_tool_schemas(_disabled, allowed=_allowed_tools, tier="core")
                            # Outer tools: stubs only (name + description)
                            _outer_stubs = get_outer_tool_stubs(_disabled) if not _allowed_tools else []
                            # load_tool: dynamic core tool for upgrading outer stubs
                            _load_tool = _build_load_tool_schema(_disabled) if not _allowed_tools else None
                            _tool_schemas = _core_schemas + _outer_stubs
                            if _load_tool:
                                _tool_schemas.append(_load_tool)
                        if _tool_schemas:
                            _stream_kwargs['tools'] = _tool_schemas
                    except Exception:
                        pass  # Tools optional — fall back to text-based tool_call blocks
                    # --- Token counting: prompt side ---
                    try:
                        # On round > 0, use the connector's live session history
                        # (which includes all previous rounds) instead of the stale
                        # DB snapshot that's only loaded on round 0.
                        if round_index > 0 and active_chat_id:
                            _pt_history = getattr(_connector, '_sessions', {}).get(active_chat_id, []) or []
                        else:
                            _pt_history = _db_history_s or []
                        _pt_tools = _stream_kwargs.get('tools')
                        _round_prompt_tokens = count_prompt_tokens(
                            system_instruction=_system_instruction_for_tokens,
                            history=_pt_history,
                            tools=_pt_tools,
                            user_message=current_message,
                            memory_context=_memory_context if round_index == 0 else "",
                        )
                    except Exception:
                        _round_prompt_tokens = 0
                    round_event_source = _connector.stream_chat(**_stream_kwargs)
                elif scraper_enabled:
                    # --- Token counting: prompt side (scraper) ---
                    # Load DB history on every round so cumulative context is counted.
                    try:
                        _scraper_history = []
                        if active_chat_id:
                            _scraper_history = _load_history_with_skills(active_chat_id)
                        _round_prompt_tokens = count_prompt_tokens(
                            system_instruction=_system_instruction_for_tokens,
                            history=_scraper_history,
                            user_message=current_message,
                            memory_context=_memory_context if round_index == 0 else "",
                        )
                    except Exception:
                        _round_prompt_tokens = 0
                    _scraper_msg = current_message
                    # Agent sub-chats still carry their own system prompt inline.
                    if request.system_prompt and "-agent-" in str(active_chat_id):
                        _scraper_msg = f"[SYSTEM INSTRUCTION]\n{request.system_prompt}\n\n[USER MESSAGE]\n{current_message}"
                    round_event_source = scraper_service.stream_events(
                        message=_scraper_msg,
                        chat_id=active_chat_id,
                        parent_id=current_parent,
                        files=files_for_round,
                        model=request.model,
                        thinking_mode=request.thinking_mode,
                        # Study mode: the scraper engine injects this ONCE via its
                        # system_injected flag — never per message. layout_mode
                        # lets the engine build study tools + directive itself.
                        system_instruction=_study_system,
                        layout_mode=_layout_mode,
                    )
                else:
                    # Qwen uses upstream_session_id for the server-side session;
                    # active_chat_id is the local Sable UUID.
                    _qwen_chat_id = _upstream_session_id or active_chat_id
                    # --- Token counting: prompt side (Qwen) ---
                    # On round > 0, Qwen's server-side session accumulates context
                    # but we can't inspect it directly. Reconstruct from DB history
                    # so prompt_tokens reflects the actual growing context window.
                    try:
                        _qwen_history = []
                        if active_chat_id:
                            _qwen_history = _load_history_with_skills(active_chat_id)
                        _round_prompt_tokens = count_prompt_tokens(
                            system_instruction=_system_instruction_for_tokens,
                            history=_qwen_history,
                            user_message=current_message,
                            memory_context=_memory_context if round_index == 0 else "",
                        )
                    except Exception:
                        _round_prompt_tokens = 0
                    # ponytail: removed retry_stream wrapper — _stream_request() already
                    # retries 3× internally, and the outer main-stream loop handles
                    # timeout-based reconnection. Triple-nesting caused 9-27 HTTP calls
                    # per user message.
                    _qwen_msg = current_message
                    # Qwen keeps its instruction server-side (personalization
                    # slot, synced once via sync_context). Study mode re-syncs
                    # that slot on entry; only if the sync FAILED do we fall
                    # back to injecting the directive on the first message of
                    # the session. Agent sub-chats still carry theirs inline.
                    if request.system_prompt and "-agent-" in str(active_chat_id):
                        _qwen_msg = f"[SYSTEM INSTRUCTION]\n{request.system_prompt}\n\n[USER MESSAGE]\n{current_message}"
                    elif (
                        _study_system
                        and _study_sync_failed
                        and round_index == 0
                        and _qwen_chat_id not in _QWEN_STUDY_FALLBACK_SENT
                    ):
                        # Sync failed: seed the directive on the FIRST message of
                        # this upstream session only. Later turns rely on the
                        # session history — re-sending it every turn was the bug.
                        _qwen_msg = f"[SYSTEM INSTRUCTION]\n{_study_system}\n\n[USER MESSAGE]\n{current_message}"
                        _QWEN_STUDY_FALLBACK_SENT.add(_qwen_chat_id)
                    round_event_source = service.stream_events(
                        message=_qwen_msg,
                        chat_id=_qwen_chat_id,
                        parent_id=current_parent,
                        files=files_for_round,
                        model=request.model,
                        thinking_mode=request.thinking_mode,
                        bdd=bdd,
                    )
                # --- Chunk timeout for main stream ---
                # NOTE: first-chunk timeout starts AFTER "request_sent" sentinel,
                # so session setup time (_ensure_headers, create_new_chat) is excluded.
                print(f"[MAIN-STREAM] ▶ Starting main stream loop (first_chunk_timeout=15s, stall_timeout=30s)")
                _MAIN_FIRST_CHUNK_TIMEOUT = 15.0
                # Must exceed service.py's total retry budget (3 × 30s = 90s)
                # to avoid racing with internal retries and orphaning generators.
                _MAIN_STALL_TIMEOUT = 120.0
                # API backends already retry once inside the connector; stacking the
                # backend's 3x empty-response retry on top multiplies a single locked
                # endpoint into ~8 calls and deepens the rate-limit lockout.
                # API backends get 0 backend-level empty retries; the connector owns that.
                _EMPTY_RESPONSE_MAX_RETRIES = 3
                _empty_response_retries = 0
                _main_got_first = False
                _main_request_sent = False
                _main_timeout_retries = 0
                _main_stall_retries = 0
                _main_iter = round_event_source.__aiter__()

                while True:
                    try:
                        if not _main_request_sent:
                            # Pre-request phase: consume setup events without timeout pressure
                            event = await _main_iter.__anext__()
                            if event.get("type") == "request_sent":
                                _main_request_sent = True
                                print(f"[MAIN-STREAM]   ✓ request_sent — starting first-chunk timer ({_MAIN_FIRST_CHUNK_TIMEOUT}s)")
                                continue
                            # meta/status events during setup pass through normally
                        elif not _main_got_first:
                            print(f"[MAIN-STREAM]   ↳ waiting for first chunk (timeout={_MAIN_FIRST_CHUNK_TIMEOUT}s)...")
                            event = await asyncio.wait_for(
                                _main_iter.__anext__(),
                                timeout=_MAIN_FIRST_CHUNK_TIMEOUT,
                            )
                            _main_got_first = True
                            print(f"[MAIN-STREAM]   ✓ first chunk received: type={event.get('type')}")
                        else:
                            event = await asyncio.wait_for(
                                _main_iter.__anext__(),
                                timeout=_MAIN_STALL_TIMEOUT,
                            )
                    except asyncio.TimeoutError:
                        if not _main_got_first:
                            _main_timeout_retries += 1
                            print(f"[MAIN-STREAM]   ⏰ FIRST-CHUNK TIMEOUT ({_MAIN_FIRST_CHUNK_TIMEOUT}s), attempt {_main_timeout_retries}/3")
                            logger.warning("[main-stream] First-chunk timeout (%ds), attempt %d/3 for chat %s",
                                           _MAIN_FIRST_CHUNK_TIMEOUT, _main_timeout_retries, active_chat_id)
                            if _main_timeout_retries >= 3:
                                # Synthesize waf_blocked to trigger auto-switch
                                yield sse({"type": "status", "message": "first_chunk_timeout_triggering_switch"})
                                event = {"type": "waf_blocked", "message": "No response within 15s after 3 attempts — connection hung", **_build_account_debug_info()}
                                # Fall through to normal event handling below
                            else:
                                # Retry: close current service, re-create stream
                                yield sse({"type": "status", "message": f"retrying_timeout_{_main_timeout_retries + 1}"})
                                if _is_api_model(request.model):
                                    # API backend (DeepSeek, Gemini, etc.) — retry via connector
                                    try:
                                        round_event_source = _connector.stream_chat(**_stream_kwargs)
                                    except Exception as _retry_exc:
                                        logger.warning("[main-stream] API retry failed: %s", _retry_exc)
                                        event = {"type": "waf_blocked", "message": f"API retry failed: {_retry_exc}", **_build_account_debug_info()}
                                else:
                                    # Qwen scraper path
                                    try:
                                        if bdd:
                                            await service.close_scoped(bdd)
                                        else:
                                            await service.close()
                                        await service._ensure_headers(bdd)
                                    except Exception as _retry_exc:
                                        logger.warning("[main-stream] Retry refresh failed: %s", _retry_exc)
                                    # ponytail: removed retry_stream wrapper (see L1475)
                                    round_event_source = service.stream_events(
                                        message=current_message,
                                        chat_id=_qwen_chat_id,
                                        parent_id=current_parent,
                                        files=files_for_round,
                                        model=request.model,
                                        thinking_mode=request.thinking_mode,
                                        bdd=bdd,
                                    )
                                    await _close_upstream_gen(_main_iter)
                                _main_iter = round_event_source.__aiter__()
                                _main_got_first = False
                                _main_request_sent = False
                                continue
                        else:
                            # Mid-stream stall — connection died after partial response
                            _main_stall_retries += 1
                            print(f"[MAIN-STREAM]   ⏰ STALL TIMEOUT ({_MAIN_STALL_TIMEOUT}s), attempt {_main_stall_retries}/3")
                            logger.warning("[main-stream] Stall timeout (%ds) after first chunk for chat %s, attempt %d/3",
                                           _MAIN_STALL_TIMEOUT, active_chat_id, _main_stall_retries)
                            if _main_stall_retries >= 3:
                                yield sse({"type": "status", "message": "stream_stall_timeout_triggering_switch"})
                                event = {"type": "waf_blocked", "message": f"Stream stalled for {_MAIN_STALL_TIMEOUT}s mid-response after 3 attempts — connection died", **_build_account_debug_info()}
                                # Fall through to auto-switch handler
                            else:
                                # Retry: drop connection and reconnect
                                yield sse({"type": "status", "message": f"retrying_stall_{_main_stall_retries + 1}"})
                                if _is_api_model(request.model):
                                    try:
                                        round_event_source = _connector.stream_chat(**_stream_kwargs)
                                    except Exception as _retry_exc:
                                        logger.warning("[main-stream] API stall-retry failed: %s", _retry_exc)
                                        event = {"type": "waf_blocked", "message": f"API stall-retry failed: {_retry_exc}", **_build_account_debug_info()}
                                else:
                                    try:
                                        if bdd:
                                            await service.close_scoped(bdd)
                                        else:
                                            await service.close()
                                        await service._ensure_headers(bdd)
                                    except Exception as _retry_exc:
                                        logger.warning("[main-stream] Stall-retry refresh failed: %s", _retry_exc)
                                    # ponytail: removed retry_stream wrapper (see L1475)
                                    round_event_source = service.stream_events(
                                        message=current_message,
                                        chat_id=_qwen_chat_id,
                                        parent_id=current_parent,
                                        files=files_for_round,
                                        model=request.model,
                                        thinking_mode=request.thinking_mode,
                                        bdd=bdd,
                                    )
                                await _close_upstream_gen(_main_iter)
                                _main_iter = round_event_source.__aiter__()
                                _main_got_first = False
                                _main_request_sent = False
                                continue
                    except StopAsyncIteration:
                        # --- Empty response detection & retry ---
                        _got_answer_content = bool("".join(_round_raw_parts).strip())
                        _should_break = True  # default: exit inner loop normally
                        if not _got_answer_content and not stream_error:
                            _empty_response_retries += 1
                            if _empty_response_retries <= _EMPTY_RESPONSE_MAX_RETRIES:
                                print(f"[MAIN-STREAM]   ⚠ EMPTY RESPONSE (retry {_empty_response_retries}/{_EMPTY_RESPONSE_MAX_RETRIES})")
                                logger.warning("[main-stream] Empty response for chat %s, retry %d/%d",
                                               active_chat_id, _empty_response_retries, _EMPTY_RESPONSE_MAX_RETRIES)
                                yield sse({"type": "status", "message": f"empty_response_retry_{_empty_response_retries}"})
                                await asyncio.sleep(2)
                                if _is_api_model(request.model):
                                    # API backend — retry via connector
                                    try:
                                        round_event_source = _connector.stream_chat(**_stream_kwargs)
                                    except Exception as _retry_exc:
                                        logger.warning("[main-stream] API empty-retry failed: %s", _retry_exc)
                                        event = {"type": "empty_exhausted", "message": f"API retry failed: {_retry_exc}", **_build_account_debug_info()}
                                else:
                                    # Qwen scraper path
                                    try:
                                        if bdd:
                                            await service.close_scoped(bdd)
                                        else:
                                            await service.close()
                                        await service._ensure_headers(bdd)
                                    except Exception as _retry_exc:
                                        logger.warning("[main-stream] Empty-response retry refresh failed: %s", _retry_exc)
                                    # ponytail: removed retry_stream wrapper (see L1475)
                                    round_event_source = service.stream_events(
                                        message=current_message,
                                        chat_id=_qwen_chat_id,
                                        parent_id=current_parent,
                                        files=files_for_round,
                                        model=request.model,
                                        thinking_mode=request.thinking_mode,
                                        bdd=bdd,
                                    )
                                await _close_upstream_gen(_main_iter)
                                _main_iter = round_event_source.__aiter__()
                                _main_got_first = False
                                _main_request_sent = False
                                continue  # restart inner while with new stream
                            else:
                                # All empty-response retries exhausted
                                if _auto_switch_enabled("qwen") and not _is_api_model(request.model):
                                    # Auto-switch ON → escalate to account switching
                                    print(f"[MAIN-STREAM]   ✗ ALL {_EMPTY_RESPONSE_MAX_RETRIES} EMPTY-RESPONSE RETRIES EXHAUSTED — triggering auto-switch")
                                    logger.warning("[main-stream] All %d empty-response retries exhausted for chat %s, escalating to auto-switch",
                                                   _EMPTY_RESPONSE_MAX_RETRIES, active_chat_id)
                                    yield sse({"type": "status", "message": "empty_response_exhausted_triggering_switch"})
                                    # Synthesize empty_exhausted event — triggers auto-switch WITHOUT
                                    # marking captcha-blocked (empty ≠ captcha; could be transient)
                                    event = {"type": "empty_exhausted", "message": f"Empty response after {_EMPTY_RESPONSE_MAX_RETRIES} retries — possible silent block", **_build_account_debug_info()}
                                    _should_break = False  # don't break — let event handling below catch this
                                else:
                                    # Auto-switch OFF or API backend → just fail cleanly
                                    print(f"[MAIN-STREAM]   ✗ ALL {_EMPTY_RESPONSE_MAX_RETRIES} EMPTY-RESPONSE RETRIES EXHAUSTED — no auto-switch")
                                    logger.warning("[main-stream] All %d empty-response retries exhausted for chat %s, auto-switch disabled",
                                                   _EMPTY_RESPONSE_MAX_RETRIES, active_chat_id)
                                    # Yield the REAL error instead of letting the frontend
                                    # fabricate "Response finished with no content".
                                    error_message = (
                                        f"Upstream '{request.model}' returned an empty response "
                                        f"(no content). Likely rate limit / expired auth — check the provider."
                                    )
                                    stream_error = True
                                    yield sse({"type": "error", "message": error_message})
                        if _should_break:
                            break

                    event_type = event.get("type")
                    # Suppress internal/setup events
                    if event_type in ("meta", "request_sent"):
                        continue
                    if event_type == "response_id":
                        # Capture for stop call, forward to frontend.
                        # Mirror into _ACTIVE_RESPONSE_IDS so /api/chat/stop can
                        # resolve it even if the frontend hasn't stored it yet.
                        _rid = event.get("id")
                        if _rid:
                            final_parent = str(_rid)
                            _ACTIVE_RESPONSE_IDS[active_chat_id] = str(_rid)
                        yield sse(event)
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
                    if event_type == "token_rotation":
                        # DeepSeek token rotation — forward as status to frontend
                        _rot_reason = event.get("reason", "unknown")
                        _rot_from = event.get("from_token", "?")
                        _rot_to = event.get("to_token", "?")
                        _rot_total = event.get("total_tokens", 0)
                        logger.info("[deepseek-rotation] %s → %s (reason: %s, pool: %d)",
                                    _rot_from, _rot_to, _rot_reason, _rot_total)
                        yield sse({"type": "status", "message": f"Rotating DeepSeek token ({_rot_reason})... [{event.get('to_index', '?')}/{_rot_total}]"})
                        continue
                    if event_type == "status":
                        # Per-token retry status from DeepSeek connector
                        yield sse(event)
                        continue
                    elif event_type == "done":
                        pending_thinking.clear()
                        async for _sse_line in _drain_sync_gen(emit_flush()):
                            yield _sse_line
                        final_parent = event.get("parent_id") or final_parent
                        current_parent = final_parent
                        # Persist the upstream chat URL (scraper transports) so a
                        # reopened chat can resume the same upstream conversation.
                        _done_url = event.get("chat_url")
                        if _done_url:
                            try:
                                save_chat_url(active_chat_id, _done_url)
                            except Exception as _url_exc:
                                logger.debug("save_chat_url failed: %s", _url_exc)
                    elif event_type == "chat_not_found":
                        # Upstream Qwen session expired — create new one, inject full history, retry
                        from engine.session import create_new_chat as _create_qwen_chat
                        logger.warning("[session-recovery] CHAT_NOT_FOUND triggered for chat_id=%s (upstream=%s)", active_chat_id, _upstream_session_id)
                        yield sse({"type": "status", "message": "recovering_session"})
                        _new_headers = await service._ensure_headers(bdd)
                        _new_qwen_id = await _create_qwen_chat(_new_headers, model=request.model)
                        if not _new_qwen_id:
                            _new_headers = await service._refresh_headers(bdd)
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
                                bdd=bdd,
                            )
                            # No meta event needed — local chat_id hasn't changed
                            async for _recovery_event in round_event_source:
                                _rec_type = _recovery_event.get("type")
                                if _rec_type in ("meta", "request_sent"):
                                    # Suppress internal/setup events
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
                        # Stale/unknown parent_id — the upstream session still exists
                        # but has no node with that token. Re-stream from the root BUT
                        # inject the full local history, otherwise the model sees a
                        # single message and the whole conversation is lost (this is
                        # exactly what caused "it forgets everything" after a stop).
                        logger.warning("[parent-recovery] PARENT_NOT_FOUND for chat_id=%s — re-streaming from root WITH history", active_chat_id)
                        yield sse({"type": "status", "message": "recovering_parent"})
                        # Clear stale parent_id from DB
                        touch_chat(active_chat_id, None)
                        current_parent = None
                        final_parent = None
                        _pn_msg = current_message
                        try:
                            _pn_prev = get_messages(active_chat_id, include_skill_events=True)
                            _pn_lines: list[str] = []
                            for _pm in _pn_prev:
                                if _pm["role"] not in ("user", "assistant"):
                                    continue
                                if _pm["content"]:
                                    _pn_lines.append(f"[{_pm['role']}]: {_pm['content']}")
                                for _sev in (_pm.get("skill_events") or []):
                                    _sev_type = _sev.get("type", "")
                                    if _sev_type == "skill_start":
                                        _pn_lines.append(f"[tool_call]: {_sev.get('name', 'unknown')}({_sev.get('data', {}).get('attrs', {})})")
                                    elif _sev_type == "skill_end":
                                        _tr_error = _sev.get("error")
                                        if _tr_error:
                                            _pn_lines.append(f"[tool_result]: {_sev.get('name')} (ok=False): ERROR: {_tr_error}")
                                        else:
                                            _pn_lines.append(f"[tool_result]: {_sev.get('name')} (ok=True): {str(_sev.get('result', ''))[:2000]}")
                            if _pn_lines:
                                _pn_msg = "[PREVIOUS CONVERSATION]\n" + "\n".join(_pn_lines) + "\n[END PREVIOUS CONVERSATION]\n\n" + current_message
                                logger.info("[parent-recovery] Injected %d history lines for chat %s", len(_pn_lines), active_chat_id)
                        except Exception as _pn_hist_err:
                            logger.warning("[parent-recovery] History injection failed: %s", _pn_hist_err)
                        # Re-stream same message with no parent, history prepended
                        round_event_source = service.stream_events(
                            message=_pn_msg,
                            chat_id=_upstream_session_id,
                            parent_id=None,
                            files=files_for_round,
                            model=request.model,
                            thinking_mode=request.thinking_mode,
                            bdd=bdd,
                        )
                        async for _recovery_event in round_event_source:
                            _rec_type = _recovery_event.get("type")
                            if _rec_type in ("meta", "request_sent"):
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
                        # API backends: a hard upstream error (rate limit, quota, auth)
                        # MUST stop immediately. The Qwen retry machinery below
                        # (first-chunk / stall / empty-response, 3x each) would otherwise
                        # re-hit the same locked endpoint up to ~8x and deepen the
                        # lockout. Surface the REAL error to the frontend and break —
                        # no retries, no masking.
                        if _is_api_model(request.model) and any(kw in error_message.lower() for kw in (
                            "rate limit", "ratelimit", "rate_limit", "too many requests",
                            "quota", "usage limit", "daily usage", "429",
                            "unauthorized", "forbidden", "401", "403",
                        )):
                            logger.warning("[main-stream] API backend hard error — aborting with real error (no retries): %s", error_message[:200])
                            yield sse({"type": "error", "message": error_message})
                            stream_error = True
                            break
                        # --- "Chat in progress" retry: 30s total, 3s delay between attempts ---
                        _cip_lower = error_message.lower()
                        _is_chat_in_progress = any(kw in _cip_lower for kw in (
                            "chat in progress", "generation in progress", "already generating",
                            "task in progress", "please wait", "busy", "concurrent request",
                        ))
                        if _is_chat_in_progress and not _is_api_model(request.model):
                            # IMMEDIATELY call stop API — don't wait for retries
                            print(f"[MAIN-STREAM]   🔨 'chat in progress' detected — calling stop API immediately")
                            try:
                                # response_id is mandatory upstream — use the cached
                                # in-flight node id, falling back to the last known
                                # parent token. Without it Qwen rejects the call.
                                _cip_rid = _ACTIVE_RESPONSE_IDS.get(_qwen_chat_id) or _ACTIVE_RESPONSE_IDS.get(active_chat_id) or final_parent
                                _stop_ok = await service._stop_upstream_generation(_qwen_chat_id, _cip_rid)
                                if _stop_ok:
                                    print(f"[MAIN-STREAM]   ✓ Stop API succeeded on first detection")
                                else:
                                    print(f"[MAIN-STREAM]   ✗ Stop API returned failure on first detection")
                            except Exception as _stop_exc:
                                logger.warning("[main-stream] Immediate stop failed: %s", _stop_exc)
                            _cip_max_retries = 10  # 10 × 3s = 30s
                            _cip_retry = 0
                            _cip_resolved = False
                            while _cip_retry < _cip_max_retries:
                                _cip_retry += 1
                                print(f"[MAIN-STREAM]   ⏳ 'chat in progress' check {_cip_retry}/{_cip_max_retries} (waiting 3s)")
                                logger.info("[main-stream] 'chat in progress' for chat %s, check %d/%d",
                                            active_chat_id, _cip_retry, _cip_max_retries)
                                yield sse({"type": "status", "message": f"chat_in_progress_retry_{_cip_retry}"})
                                await asyncio.sleep(3)
                                try:
                                    if bdd:
                                        await service.close_scoped(bdd)
                                    else:
                                        await service.close()
                                    await service._ensure_headers(bdd)
                                except Exception as _cip_exc:
                                    logger.warning("[main-stream] Chat-in-progress retry refresh failed: %s", _cip_exc)
                                # ponytail: removed retry_stream wrapper (see L1475)
                                round_event_source = service.stream_events(
                                    message=current_message,
                                    chat_id=_qwen_chat_id,
                                    parent_id=current_parent,
                                    files=files_for_round,
                                    model=request.model,
                                    thinking_mode=request.thinking_mode,
                                    bdd=bdd,
                                )
                                await _close_upstream_gen(_main_iter)
                                _main_iter = round_event_source.__aiter__()
                                _main_got_first = False
                                _main_request_sent = False
                                # Peek at first real event to check if still "chat in progress"
                                try:
                                    _peek_event = await asyncio.wait_for(
                                        _main_iter.__anext__(),
                                        timeout=_MAIN_FIRST_CHUNK_TIMEOUT,
                                    )
                                    _peek_type = _peek_event.get("type", "")
                                    _peek_msg = str(_peek_event.get("message", "")).lower()
                                    if _peek_type == "error" and any(kw in _peek_msg for kw in (
                                        "chat in progress", "generation in progress", "already generating",
                                        "task in progress", "please wait", "busy", "concurrent request",
                                    )):
                                        # Confirmed still busy → call stop reactively before next retry
                                        print(f"[MAIN-STREAM]   🔨 Still busy after probe — calling stop API (check {_cip_retry})")
                                        try:
                                            _cip_rid = _ACTIVE_RESPONSE_IDS.get(_qwen_chat_id) or _ACTIVE_RESPONSE_IDS.get(active_chat_id) or final_parent
                                            _stop_ok = await service._stop_upstream_generation(_qwen_chat_id, _cip_rid)
                                            if _stop_ok:
                                                print(f"[MAIN-STREAM]   ✓ Stop API succeeded on check {_cip_retry}")
                                            else:
                                                print(f"[MAIN-STREAM]   ✗ Stop API failed on check {_cip_retry}")
                                        except Exception as _stop_exc:
                                            logger.warning("[main-stream] Reactive stop failed: %s", _stop_exc)
                                        continue  # Still in progress, retry again
                                    else:
                                        # Resolved! Process this event normally
                                        _cip_resolved = True
                                        event = _peek_event
                                        event_type = event.get("type", "")
                                        if event_type == "request_sent":
                                            _main_request_sent = True
                                            continue
                                        break
                                except asyncio.TimeoutError:
                                    # Timeout waiting for first chunk after retry — treat as normal timeout
                                    break
                                except StopAsyncIteration:
                                    # Empty stream — let empty-response handler deal with it
                                    break
                            if not _cip_resolved and _cip_retry >= _cip_max_retries:
                                print(f"[MAIN-STREAM]   ✗ 'chat in progress' persisted after {_cip_max_retries} retries (30s)")
                                logger.warning("[main-stream] 'chat in progress' persisted after %d retries for chat %s",
                                               _cip_max_retries, active_chat_id)
                                stream_error = True
                            elif _cip_resolved:
                                # Continue processing the resolved event through normal flow
                                pass
                            else:
                                # Broke out of retry loop due to timeout/empty — fall through
                                pass
                            if _cip_resolved or _cip_retry >= _cip_max_retries:
                                pass  # Fall through to normal event handling below
                            else:
                                continue  # Retry loop continues
                        # FIX #7: Stricter keyword matching to avoid false positives.
                        # Use phrase-level matching with context awareness instead of raw substrings.
                        # "blocked" alone is too broad (e.g., "content blocked by filter" ≠ WAF).
                        import re as _re
                        _err_lower_check = error_message.lower()
                        _is_rate_limit = any(kw in _err_lower_check for kw in (
                            "ratelimit", "rate_limit", "rate limit", "too many requests",
                            "daily usage", "usage limit", "quota exceeded", "429",
                        ))
                        _is_captcha = any(pat.search(_err_lower_check) for pat in (
                            _re.compile(r'\bcaptcha\b'),
                            _re.compile(r'\bwaf\b'),
                            _re.compile(r'\brgv587\b'),
                            _re.compile(r'\bverify.*identity\b'),
                            _re.compile(r'\bsecurity.*check\b'),
                            _re.compile(r'\baccess.*denied.*ip\b'),
                            _re.compile(r'\bblocked.*request\b'),
                        ))
                        if _is_rate_limit or _is_captcha:
                            print(f"[MAIN-STREAM]   ⚡ ESCALATING generic error to {'rate_limited' if _is_rate_limit else 'waf_blocked'}: {error_message[:100]}")
                            logger.warning("[main-stream] Generic error looks like %s, escalating to auto-switch: %s",
                                           "rate_limit" if _is_rate_limit else "captcha", error_message[:200])
                            # Rewrite event type so the auto-switch block below catches it
                            event = {**event, "type": "rate_limited" if _is_rate_limit else "waf_blocked", **_build_account_debug_info()}
                            event_type = event["type"]
                            # Fall through to rate_limited/waf_blocked handler below
                        else:
                            stream_error = True
                    # Auto-switch is Qwen-only — browser account switching is meaningless
                    # for native API backends (Gemini/Groq/DeepSeek) and scraper.
                    _is_qwen_stream = not _is_api_model(request.model) and not scraper_enabled
                    if event_type in ("rate_limited", "waf_blocked", "empty_exhausted") and not _is_qwen_stream:
                        # Non-Qwen backend hit rate-limit/WAF — report the REAL error to the
                        # frontend (don't silently drop it) but don't attempt account switching.
                        _real_err = str(event.get("message") or error_message or "Upstream API error")
                        logger.warning("[main-stream] API backend error (%s): %s", event_type, _real_err[:300])
                        yield sse({"type": "error", "message": _real_err})
                        stream_error = True
                    elif event_type in ("rate_limited", "waf_blocked", "empty_exhausted") and _is_qwen_stream and _auto_switch_enabled("qwen"):
                        print(f"[AUTO-SWITCH] ▶ TRIGGERED by {event_type} — msg={str(event.get('message',''))[:100]}")
                        pending_thinking.clear()
                        async for _sse_line in _drain_sync_gen(emit_flush()):
                            yield _sse_line
                        # --- Auto-switch to next available account ---
                        _switch_reason = "rate_limit" if event_type == "rate_limited" else ("empty_exhausted" if event_type == "empty_exhausted" else "waf_block")
                        logger.info("[auto-switch] Triggered by %s for chat %s", _switch_reason, active_chat_id)
                        yield sse({"type": "account_switch", "step": "triggered", "reason": _switch_reason})

                        from engine.config import (
                            get_next_available_account,
                            set_active_account,
                            _resolve_active_account as _get_active,
                            mark_account_captcha_blocked,
                            mark_account_exhausted,
                            _SYSTEM as _SYS,
                        )
                        _current_acc = _get_active()

                        # Mark the failing account so get_next_available_account skips it.
                        if event_type == "waf_blocked":
                            mark_account_captcha_blocked(_current_acc)
                        elif event_type == "empty_exhausted":
                            # Empty responses = silent rate-limit / dead session.
                            # Mark as exhausted so we don't pick this account again.
                            mark_account_exhausted(_current_acc)
                            logger.info("[auto-switch] Marked %s as exhausted (empty_exhausted trigger)", _current_acc)

                        _tried_accounts: set[str] = {_current_acc}
                        _switch_max_retries = 5
                        _switch_attempt = 0
                        _switch_success = False

                        while _switch_attempt < _switch_max_retries and not _switch_success:
                            _switch_attempt += 1
                            yield sse({"type": "account_switch", "step": "searching", "current": _current_acc, "attempt": _switch_attempt})
                            _next_acc = get_next_available_account(exclude=_tried_accounts)

                            if not _next_acc:
                                # No accounts left — fall back to original error behavior
                                yield sse({"type": "account_switch", "step": "failed", "error": "no_accounts_available"})
                                if event_type == "rate_limited":
                                    hours = event.get("hours", "?")
                                    details = event.get("message", "Daily usage limit reached.")
                                    error_message = f"⏳ Rate Limited — {details} (retry in {hours}h)"
                                else:
                                    error_message = "🚫 WAF/captcha block — no available accounts to switch to"
                                stream_error = True
                                yield sse(event)
                                break  # exit retry loop

                            # Perform account switch
                            logger.info("[auto-switch] Attempt %d: Switching from %s → %s", _switch_attempt, _current_acc, _next_acc)
                            yield sse({"type": "account_switch", "step": "switching", "from": _current_acc, "to": _next_acc, "attempt": _switch_attempt})
                            try:
                                _next_path = _SYS / _next_acc
                                if bdd:
                                    # Isolated stream: switch only the scoped browser
                                    _scoped_browser = service._get_browser(bdd)
                                    await _scoped_browser.close()
                                    _scoped_browser.user_data_dir = str(_next_path)
                                    service._scoped_headers.pop(bdd, None)
                                    service._scoped_accounts[bdd] = _next_acc
                                    logger.info("[auto-switch] Scoped account updated to %s (bdd=%s)", _next_acc, bdd)
                                else:
                                    # Default stream: existing behavior untouched
                                    await service.close()
                                    set_active_account(_next_acc)
                                    service._browser.user_data_dir = str(_next_path)
                                    service._account_override = _next_acc
                                    logger.info("[auto-switch] Active account updated to %s", _next_acc)
                            except Exception as _sw_exc:
                                logger.error("[auto-switch] Account switch failed: %s", _sw_exc)
                                yield sse({"type": "account_switch", "step": "failed", "error": str(_sw_exc)})
                                error_message = f"Account switch failed: {_sw_exc}"
                                stream_error = True
                                yield sse(event)
                                continue

                            # Sync system instructions to new account before first message
                            yield sse({"type": "account_switch", "step": "syncing", "account": _next_acc})
                            from engine.config import mark_sync_failed, clear_sync_failed
                            _sync_ok = False
                            try:
                                _sync_result = await service.sync_context(bdd=bdd)
                                if _sync_result is not False:
                                    _sync_ok = True
                                    clear_sync_failed(_next_acc)
                                    logger.info("[auto-switch] sync_context completed for %s", _next_acc)
                                else:
                                    logger.warning("[auto-switch] sync_context returned False for %s", _next_acc)
                                    mark_sync_failed(_next_acc)
                                    yield sse({"type": "account_switch", "step": "syncing", "account": _next_acc, "error": "sync failed, will inject into first message"})
                            except Exception as _sync_exc:
                                logger.warning("[auto-switch] sync_context failed for %s: %s", _next_acc, _sync_exc)
                                mark_sync_failed(_next_acc)
                                yield sse({"type": "account_switch", "step": "syncing", "account": _next_acc, "error": "sync failed, will inject into first message"})

                            # Build context for new account (with summarization if >500k)
                            yield sse({"type": "account_switch", "step": "summarizing", "account": _next_acc})
                            try:
                                _switch_ctx = await _build_switch_context(active_chat_id, request.model)
                            except Exception as _ctx_exc:
                                logger.warning("[auto-switch] Context build failed: %s", _ctx_exc)
                                _switch_ctx = current_message  # fallback to raw message

                            # Create new upstream session on switched account
                            yield sse({"type": "account_switch", "step": "creating_session", "account": _next_acc})
                            try:
                                _new_upstream = await retry_async(
                                    lambda: service.create_chat(model=request.model),
                                    label="auto_switch_create_chat",
                                )
                                if _new_upstream:
                                    _upstream_session_id = _new_upstream
                                    from server.database import set_upstream_session_id
                                    set_upstream_session_id(active_chat_id, _new_upstream)
                                    logger.info("[auto-switch] New upstream session: %s", _new_upstream)
                                else:
                                    raise RuntimeError("create_chat returned None")
                            except Exception as _sess_exc:
                                logger.error("[auto-switch] New session creation failed: %s", _sess_exc)
                                yield sse({"type": "account_switch", "step": "failed", "error": f"session_creation_failed: {_sess_exc}"})
                                error_message = f"Account switched to {_next_acc} but session creation failed: {_sess_exc}"
                                stream_error = True
                                yield sse(event)
                                continue

                            # Warm up WAF tokens for new account (non-blocking best-effort)
                            yield sse({"type": "account_switch", "step": "warming_up", "account": _next_acc})
                            try:
                                await service.force_refresh_waf(account=_next_acc)
                            except Exception as _waf_exc:
                                logger.warning("[auto-switch] WAF warmup failed for %s: %s", _next_acc, _waf_exc)

                            yield sse({"type": "account_switch", "step": "complete", "account": _next_acc, "reason": _switch_reason})

                            # Re-stream with context + new session
                            _switch_msg = _switch_ctx if _switch_ctx else current_message
                            round_event_source = service.stream_events(
                                message=_switch_msg,
                                chat_id=_upstream_session_id,
                                parent_id=None,
                                files=files_for_round,
                                model=request.model,
                                thinking_mode=request.thinking_mode,
                                bdd=bdd,
                            )
                            async for _sw_event in round_event_source:
                                _sw_type = _sw_event.get("type")
                                if _sw_type == "meta":
                                    continue
                                if _sw_type == "answer":
                                    pending_thinking.clear()
                                    _raw_chunk = str(_sw_event.get("text", ""))
                                    _raw_answer_parts.append(_raw_chunk)
                                    _round_raw_parts.append(_raw_chunk)
                                    async for _sse_line in _drain_sync_gen(emit_parsed(_raw_chunk)):
                                        yield _sse_line
                                    continue
                                if _sw_type == "thinking":
                                    _chunk = str(_sw_event.get("text", ""))
                                    thinking_parts.append(_chunk)
                                    round_thinking_parts.append(_chunk)
                                    pending_thinking.append(_chunk)
                                    yield sse({"type": "thinking", "text": _chunk})
                                    continue
                                if _sw_type == "done":
                                    pending_thinking.clear()
                                    async for _sse_line in _drain_sync_gen(emit_flush()):
                                        yield _sse_line
                                    final_parent = _sw_event.get("parent_id") or final_parent
                                    current_parent = final_parent
                                elif _sw_type == "error":
                                    pending_thinking.clear()
                                    async for _sse_line in _drain_sync_gen(emit_flush()):
                                        yield _sse_line
                                    error_message = str(_sw_event.get("message", "Unknown error after switch"))
                                    stream_error = True
                                elif _sw_type in ("rate_limited", "waf_blocked"):
                                    # New account also blocked — mark and retry with next account
                                    _tried_accounts.add(_next_acc)
                                    if _sw_type == "waf_blocked":
                                        mark_account_captcha_blocked(_next_acc)
                                    logger.warning("[auto-switch] New account %s also blocked (%s), will retry", _next_acc, _sw_type)
                                    pending_thinking.clear()
                                    async for _sse_line in _drain_sync_gen(emit_flush()):
                                        yield _sse_line
                                    yield sse({"type": "account_switch", "step": "retrying", "account": _next_acc, "reason": _sw_type, "attempt": _switch_attempt})
                                    _current_acc = _next_acc
                                    continue  # continue while loop to try next account
                                yield sse(_sw_event)
                            # Re-stream completed without fatal error
                            if not stream_error:
                                _switch_success = True
                                # Reset empty-response counter so the new account gets
                                # its own fair retry budget instead of immediately
                                # re-triggering auto-switch on the next empty response.
                                _empty_response_retries = 0
                            break  # Exit while loop (success or non-recoverable error)

                        # All retries exhausted without success
                        if not _switch_success and not stream_error:
                            yield sse({"type": "account_switch", "step": "failed", "error": f"all_{_switch_attempt}_attempts_failed"})
                            error_message = f"All {_switch_attempt} account switch attempts failed."
                            stream_error = True
                            yield sse(event)
                            break  # Break inner while True — no point retrying same dead accounts
                    else:
                        yield sse(event)
                round_thinking_text = "".join(round_thinking_parts)
                if round_thinking_text:
                    skill_events.append({"type": "round_thinking", "text": round_thinking_text})
                round_text = "".join(round_answer_parts)
                # Persist text + inline UI in emission order so the whole turn
                # replays as one coherent card. Falls back to a single round_text
                # blob when no inline UI was emitted this round.
                if round_flow:
                    skill_events.extend(round_flow)
                elif round_text:
                    skill_events.append({"type": "round_text", "text": round_text})
                if round_skill_events:
                    skill_events.extend(round_skill_events)
                # Study tools queue side-channel events (doc_changed for
                # highlights/notes/progress). Forward them so the reader/editor
                # can refresh the panes that just changed under the user.
                if _study_ctx is not None and _study_ctx.events:
                    for _sc_ev in list(_study_ctx.events):
                        round_skill_events.append(_sc_ev)
                        skill_events.append(_sc_ev)
                        yield sse(_sc_ev)
                    _study_ctx.events.clear()
                # --- Guardrails: track failures + no-progress + stubbing ---
                _new_round_errors: dict[str, str] = {}
                # Collect skill_output text per tool for record_result()
                _tool_outputs: dict[str, list[str]] = {}  # tag_id → output chunks
                for _sev in round_skill_events:
                    _sev_type = _sev.get("type")
                    if _sev_type == "skill_output":
                        _tid = str(_sev.get("id", ""))
                        _tool_outputs.setdefault(_tid, []).append(str(_sev.get("text", "")))
                    elif _sev_type == "skill_end":
                        _guard.record_result(_sev.get("ok", False))
                        _tname = _sev.get("name", "")
                        if not _sev.get("ok", False):
                            _err_msg = _sev.get("error", "unknown error")
                            _new_round_errors[_tname] = str(_err_msg)
                        # Feed result to LoopDetector for no-progress + stubbing
                        _tid = str(_sev.get("id", ""))
                        _output_text = "".join(_tool_outputs.get(_tid, []))
                        if _output_text or _sev.get("ok") is not None:
                            _stubbed = _loop_detector.record_result(
                                _tname,
                                _sev.get("attrs_content", ""),  # args preview
                                _output_text,
                            )
                            # If stubbed, we could replace the output in feedback later
                            # (feedback is built after this, so stubbing is advisory)
                _round_tool_errors = _new_round_errors  # Carry errors to next round's check()
                round_answer = "".join(answer_parts)
                round_thinking = "".join(thinking_parts)
                stored = round_answer or error_message or ""
                # Log assistant response to file
                if stored:
                    _log_conversation(active_chat_id, request.model, "assistant", stored)
                # --- Token counting: completion side ---
                _round_completion_tokens = count_completion_tokens(stored, round_thinking)
                if saved_message_id is None:
                    saved_message_id = add_message(
                        active_chat_id, "assistant", stored, round_thinking, final_parent, skill_events,
                        memory_used=_all_tool_mem_used or None,
                        prompt_tokens=_round_prompt_tokens,
                        completion_tokens=_round_completion_tokens,
                    )
                else:
                    update_message(saved_message_id, stored, round_thinking, final_parent, skill_events,
                                   memory_used=_all_tool_mem_used or None,
                                   prompt_tokens=_round_prompt_tokens,
                                   completion_tokens=_round_completion_tokens)
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
                # cwd_warning pause: stop the stream so the user can approve/change folder
                _cwd_pause = any(
                    ev.get("type") == "cwd_warning"
                    for ev in round_skill_events
                )
                # --- Guardrail warnings: LoopDetector (primary) + MainChatGuard (malformed only) ---
                _guard_warnings: list[str] = []
                _guard_warnings_injected = False
                # LoopDetector already injected warnings inline during dispatch.
                # MainChatGuard legacy loop/failure checks kept as secondary safety net.
                _loop_warn = _guard.check_loop()
                if _loop_warn:
                    _guard_warnings.append(_loop_warn)
                _fail_warn = _guard.check_failures()
                if _fail_warn:
                    _guard_warnings.append(_fail_warn)
                # Check malformed/incomplete using RAW text (before parser strips tags)
                _raw_round_text = "".join(_round_raw_parts)
                # Skip guard checks when waiting for user approval (permission or CWD)
                # — the tool call is valid but paused; flagging it as malformed/incomplete
                # causes the model to see an error and retry the same operation.
                if not _permission_pause and not _cwd_pause:
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
                # ponytail: if the only tools this round were chat_title and the model
                # already sent real answer text, don't inject [chat_title] OK back —
                # the model doesn't need a confirmation nudge when it's already talking.
                _only_chat_title = (
                    bool(round_skill_events)
                    and all(
                        _ev.get("name") == "chat_title"
                        for _ev in round_skill_events
                        if _ev.get("type") == "skill_end"
                    )
                )
                _has_answer_text = bool("".join(round_answer_parts).strip())
                if _only_chat_title and _has_answer_text:
                    round_skill_events.clear()

                feedback = build_tool_feedback(round_skill_events)

                # --- Teacher escalation for stuck subagents (async, after dispatch) ---
                # _dispatch_events flagged a guardrail recovery; now we can await the
                # parent chat's guidance from this async context.
                if _pending_teacher_escalation.get("needed") and _agent_run:
                    try:
                        from engine.config import AGENT_CONFIG_PATH as _tcp
                        import json as _json
                        _tcfg = {}
                        try:
                            _tcfg = _json.loads(_tcp.read_text(encoding="utf-8")).get("teacher", {})
                        except Exception:
                            pass
                        _teacher_on = _tcfg.get("enabled", True)
                        _max_interventions = 2
                        if _teacher_on and _agent_run.teacher_interventions < _max_interventions:
                            from engine.agents.auto_turn import auto_turn as _at
                            _parent_cid = _agent_run.chat_id
                            if _parent_cid:
                                _stuck_reason = (
                                    f"Guardrail recovery on "
                                    f"'{_pending_teacher_escalation.get('tool_name', '?')}': "
                                    f"{_pending_teacher_escalation.get('reason', 'repeated failures')}"
                                )
                                _recent_msgs = []
                                if hasattr(_agent_run, "messages"):
                                    _recent_msgs = (
                                        _agent_run.messages[-6:]
                                        if len(_agent_run.messages) > 6
                                        else list(_agent_run.messages)
                                    )
                                logger.info(
                                    "[agent %s] requesting teacher escalation (%d/%d)",
                                    _agent_run.id,
                                    _agent_run.teacher_interventions + 1,
                                    _max_interventions,
                                )
                                yield sse({"type": "status", "message": "requesting_teacher_guidance"})
                                _guidance = await _at.request_teacher_guidance(
                                    chat_id=_parent_cid,
                                    agent_id=_agent_run.id,
                                    role=_agent_run.role,
                                    task=_agent_run.task,
                                    stuck_reason=_stuck_reason,
                                    context=_agent_run.context,
                                    recent_messages=_recent_msgs,
                                )
                                if _guidance:
                                    _agent_run.teacher_interventions += 1
                                    _teacher_block = (
                                        f"[TEACHER GUIDANCE — Intervention "
                                        f"{_agent_run.teacher_interventions}/{_max_interventions}]\n"
                                        f"{_guidance}\n[END TEACHER GUIDANCE]"
                                    )
                                    feedback = (_teacher_block + "\n\n" + feedback) if feedback else _teacher_block
                                    logger.info(
                                        "[agent %s] teacher guidance injected (%d chars)",
                                        _agent_run.id, len(_guidance),
                                    )
                                else:
                                    logger.info(
                                        "[agent %s] teacher guidance not received (timeout/unavailable)",
                                        _agent_run.id,
                                    )
                        else:
                            if not _teacher_on:
                                logger.info("[agent %s] teacher escalation disabled in config", _agent_run.id)
                            else:
                                logger.info(
                                    "[agent %s] max teacher interventions reached (%d)",
                                    _agent_run.id, _agent_run.teacher_interventions,
                                )
                    except Exception as _te_exc:
                        logger.warning(
                            "[agent %s] teacher escalation failed: %s",
                            _agent_run.id if _agent_run else "?", _te_exc,
                        )

                # --- Critique report injection: prepend prominently so model acts on it ---
                _critique_reports: list[str] = []
                for _ev in round_skill_events:
                    if _ev.get("type") == "skill_end" and _ev.get("name") == "critique" and _ev.get("ok"):
                        _report = (_ev.get("result") or {}).get("report", "")
                        if _report:
                            _critique_reports.append(_report)
                if _critique_reports:
                    _critique_block = "\n\n---\n\n".join(_critique_reports)
                    _critique_injection = (
                        f"\n\n[CRITIQUE REPORT — Act on this]\n{_critique_block}\n[END CRITIQUE REPORT]"
                    )
                    feedback = (feedback + _critique_injection) if feedback else _critique_injection
                # Flush pending teacher escalation requests mid-stream so Maria
                # can respond with teacher_guidance tool call alongside tool results
                try:
                    from engine.agents.auto_turn import auto_turn as _at_flush
                    _pending_teacher = _at_flush.get_pending_teacher_prompts(active_chat_id)
                    if _pending_teacher:
                        _teacher_block = "\n\n".join(_pending_teacher)
                        feedback = (feedback + "\n\n" + _teacher_block) if feedback else _teacher_block
                        logger.info("[chat %s] flushed %d pending teacher escalation(s) mid-stream", active_chat_id, len(_pending_teacher))
                except Exception:
                    pass
                # Extract image paths from skill results for multimodal injection next round
                for _ev in round_skill_events:
                    if _ev.get("type") == "skill_end" and _ev.get("ok"):
                        _res = _ev.get("result", {})
                        if _res.get("kind") == "image" and _res.get("path"):
                            _pending_skill_images.append(_res["path"])
                # Truncate oversized tool output to protect context window
                from engine.config import get_max_tool_output_chars as _get_max_tool_output_chars
                from engine.skills.events import middle_truncate
                _tool_cap = _get_max_tool_output_chars()
                if feedback and len(feedback) > _tool_cap:
                    feedback = middle_truncate(feedback, _tool_cap)
                # If no tool feedback but guard warnings exist, use warnings as feedback
                # so the model sees them and self-corrects (auto-continue, no break)
                if not feedback and _guard_warnings:
                    feedback = "\n\n".join(_guard_warnings)
                    _guard_warnings_injected = True
                # Output format validation for subagents: check required_sections.
                # Runs independent of tool feedback — a subagent's final answer must
                # satisfy its role's required_sections regardless of whether it also
                # called tools this round. (Previously gated behind `not feedback`,
                # which silently skipped validation whenever any tool ran.)
                if _agent_run and not stream_error and not error_message:
                    try:
                        from engine.agents.registry import get_role_config as _grc
                        _rcfg = _grc(_agent_run.role)
                        _req = _rcfg.required_sections
                        if _req:
                            _round_text = "".join(round_answer_parts)
                            _found = sum(1 for s in _req if s.lower() in _round_text.lower())
                            _threshold = len(_req) * 0.5
                            if _found < _threshold:
                                _missing = [s for s in _req if s.lower() not in _round_text.lower()]
                                _fmt_error = (
                                    f"[FORMAT ERROR] Your output is missing required sections. "
                                    f"Found {_found}/{len(_req)} required headers (need ≥{int(_threshold)}).\n"
                                    f"Missing: {', '.join(_missing)}\n\n"
                                    f"Your response MUST include these section headers:\n"
                                    + "\n".join(f"## {s}" for s in _req)
                                    + "\n\nRegenerate your full response using the correct output format."
                                )
                                # Surface it as a user-visible skill event so the
                                # format correction is observable in the chat, not
                                # just injected into the model's next-round context.
                                # Appended to round_skill_events (same pattern as
                                # _loop_warning) so it is streamed + persisted once.
                                _fmt_err_id = f"fmt_err_{round_index}"
                                round_skill_events.append({"type": "skill_start", "name": "format_error", "id": _fmt_err_id})
                                round_skill_events.append({"type": "skill_output", "name": "format_error", "text": _fmt_error, "id": _fmt_err_id})
                                round_skill_events.append({"type": "skill_end", "name": "format_error", "ok": False, "error": _fmt_error, "id": _fmt_err_id})
                                feedback = (feedback + "\n\n" + _fmt_error) if feedback else _fmt_error
                                logger.info(
                                    "[agent %s] format validation failed: %d/%d sections found, injecting correction",
                                    active_chat_id, _found, len(_req),
                                )
                    except Exception:
                        pass
                # Emit permission/cwd batch summary so frontend can queue multiple approvals
                if _permission_pause or _cwd_pause:
                    _batch_perm_ids = [
                        ev.get("id", "")
                        for ev in round_skill_events
                        if ev.get("type") == "permission_request"
                    ]
                    _batch_cwd_ids = [
                        ev.get("id", "")
                        for ev in round_skill_events
                        if ev.get("type") == "cwd_warning"
                    ]
                    yield sse({
                        "type": "permission_batch",
                        "permission_ids": _batch_perm_ids,
                        "cwd_ids": _batch_cwd_ids,
                        "total": len(_batch_perm_ids) + len(_batch_cwd_ids),
                    })
                    # Desktop notification: the turn is blocked on the user.
                    try:
                        from engine.notifications.policy import notify_category as _notify_perm
                        _n_blocked = len(_batch_perm_ids) + len(_batch_cwd_ids)
                        asyncio.ensure_future(_notify_perm(
                            "permission", "Approval needed",
                            f"{_n_blocked} tool{'s' if _n_blocked != 1 else ''} waiting for your approval",
                            urgency="critical",
                        ))
                    except Exception:
                        pass

                if stream_error or error_message or not feedback or _ask_user_pause or _permission_pause or _cwd_pause:
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
                _turn_caps.reset()  # Reset per-turn caps each round
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
        except (asyncio.CancelledError, GeneratorExit):
            # Frontend disconnected (stop button / tab close) — kill upstream generation.
            #
            # A browser abort (AbortController.abort()) surfaces to this generator
            # as GeneratorExit, NOT CancelledError. Catching only CancelledError
            # meant _agent_killed stayed False on every real stop, the `finally`
            # block tried to yield a "done" event (RuntimeError on a closing
            # generator), and touch_chat() never ran — so the next user message
            # branched from a stale parent_id and the stopped tool loop was lost.
            logger.info("[event_stream] Client disconnected for chat %s — stopping upstream", active_chat_id)
            # CRITICAL: close the in-flight upstream event generator. The async-for
            # over round_event_source / _main_iter is abandoned on GeneratorExit —
            # Python does NOT auto-close the async generator when we `raise` out of
            # the awaiting frame. Without an explicit aclose(), the scraper's
            # stream_events() never reaches its own cancel handler, so
            # _response_lock / _lock stay held and the NEXT message deadlocks at
            # "processing". Closing here lets stream_events' except-block fire
            # _interrupt_generation() and release everything.
            try:
                _closer = _main_iter if _main_iter is not None else round_event_source
                _aclose = getattr(_closer, "aclose", None)
                if _aclose is not None:
                    await _aclose()
                elif hasattr(_closer, "close"):
                    _closer.close()
            except (asyncio.CancelledError, GeneratorExit):
                pass
            except Exception as _gen_close_exc:
                logger.debug("[event_stream] upstream generator close on cancel failed: %s", _gen_close_exc)
            # Flush the in-flight round's tool events into skill_events BEFORE the
            # finally-block persist. Normally these are merged at end-of-round, but a
            # cancel mid-round skips that merge entirely — leaving the assistant
            # message with no record of the tool calls that already executed, so the
            # next turn's history reconstruction (build_tool_feedback) sees nothing.
            try:
                # Merge the in-flight round's tool events so they persist even when
                # the user stops mid-tool-loop (end-of-round merge never runs).
                if round_skill_events:
                    skill_events.extend(round_skill_events)
                    round_skill_events.clear()
            except Exception as _flush_err:
                logger.warning("[event_stream] Failed to flush partial tool events on cancel: %s", _flush_err)
            # ── CRITICAL: persist + pin the tail HERE, synchronously. ──
            # The `finally` block only runs at generator teardown, which can lose
            # the race against the user's next message (observed: a stale user row
            # landing between two correct ones). Doing it now — before any `await`
            # below — guarantees the next request reads the right parent.
            #
            # A mid-round stop has saved_message_id == None (the round-level
            # persist never ran), so we must materialise the partial assistant
            # message ourselves or the tail has nothing to point at. The finally
            # block then sees saved_message_id set and UPDATEs instead of INSERTing
            # a duplicate.
            _agent_killed = True
            try:
                if saved_message_id is None:
                    _stop_answer = "".join(answer_parts)
                    _stop_thinking = "".join(thinking_parts)
                    _stop_content = _stop_answer or error_message or ""
                    if not _stop_content and skill_events:
                        _stop_content = "\n".join(
                            f"[skill] {e.get('name', 'skill')}" if e.get("type") == "skill_start"
                            else (f"[{'ok' if e.get('ok') else 'error'}] {e.get('name', 'skill')}" if e.get("type") == "skill_end" else "")
                            for e in skill_events
                        ).strip()
                    saved_message_id = add_message(
                        active_chat_id, "assistant", _stop_content, _stop_thinking,
                        final_parent, skill_events, memory_used=_all_tool_mem_used or None,
                    )
                    logger.info("[event_stream] STOP: persisted partial assistant msg id=%s for chat %s", saved_message_id, active_chat_id)
            except Exception as _persist_err:
                logger.warning("[event_stream] Failed to persist partial msg on stop: %s", _persist_err)
            # Tail pointer: ONLY an upstream token is valid here. `chats.parent_id`
            # is handed to Qwen verbatim as parentId — a local SQLite row id (digit)
            # is NOT a node in Qwen's tree and triggers PARENT_NOT_FOUND, which
            # nulls the parent and branches from root (full amnesia). `final_parent`
            # is the response_id captured at stream start, i.e. the actual last
            # node Qwen created — branch from that. If we never captured one
            # (stop before first token), leave the previous tail untouched rather
            # than clobbering it.
            try:
                # DeepSeek uses small integers (2, 4, 6…) as upstream
                # response_message_ids — a digit is a VALID node there, unlike
                # Qwen. Pinning it is what lets the next turn chain instead of
                # branching from root / raising "invalid message id".
                _is_ds_stop = current_provider == "deepseek"
                if final_parent and (_is_ds_stop or not str(final_parent).isdigit()):
                    touch_chat(active_chat_id, str(final_parent))
                    logger.info("[event_stream] STOP: tail kept at upstream token %s for chat %s", final_parent, active_chat_id)
                else:
                    touch_chat(active_chat_id, None)  # timestamp only, preserve parent
                    logger.info("[event_stream] STOP: no upstream token (final_parent=%s) — parent preserved for chat %s", final_parent, active_chat_id)
            except Exception as _pin_err:
                logger.warning("[event_stream] Failed to pin tail on stop: %s", _pin_err)
            try:
                _svc = service if not _is_api_model(request.model) else None
                if _svc and hasattr(_svc, '_stop_upstream_generation'):
                    await _svc._stop_upstream_generation(active_chat_id, final_parent)
            except Exception:
                pass
            # Mark agent as killed so the finally block doesn't overwrite with "completed"
            _agent_killed = True
            if _agent_run:
                try:
                    from server.database import update_agent_status as _uas
                    _agent_run.mark_failed("Stopped by user")
                    _uas(active_chat_id, "killed", error="Stopped by user")
                    # Broadcast so topbar card updates even if user switched chats
                    from server.api.routes.agents import broadcast_agent_event as _bae
                    _bae({
                        "type": "agent_failed",
                        "agent_id": active_chat_id,
                        "data": {"role": _agent_run.role, "error": "Stopped by user"},
                    })
                except Exception:
                    pass
            raise  # re-raise so Starlette cleans up properly
        except Exception as exc:
            error_message = f"{type(exc).__name__}: {exc}"
            try:
                yield sse({"type": "error", "message": f"Server error: {error_message}"})
            except Exception:
                pass
        finally:
            # Signal stream completion to frontend so backend-status cards collapse.
            #
            # NEVER yield inside finally during generator close: a bare yield here
            # raises RuntimeError("async generator ignored GeneratorExit") and
            # aborts the rest of this block — which is exactly where the tail
            # pointer gets advanced. Only emit "done" when the client is still
            # attached (no GeneratorExit / cancel in flight).
            if not error_message and not _agent_killed:
                try:
                    yield sse({"type": "done", "parent_id": final_parent})
                except (RuntimeError, GeneratorExit):
                    pass
                except Exception:
                    pass
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
                _tail_msg_id = saved_message_id
            else:
                _tail_msg_id = add_message(active_chat_id, "assistant", stored_content, thinking, final_parent, skill_events)
            # Tail pointer: on a clean finish the upstream token (final_parent) is a
            # valid Qwen node, so advance to it. When the user STOPS mid-tool-loop,
            # no "done" ever fired and final_parent is a stale/partial token — the
            # next request would branch from the wrong node and lose the tool loop.
            # In that case pin the tail to the last persisted message id (a digit),
            # which is exactly the point the next user message must branch from.
            # Tail pointer rules:
            #  - Stopped mid-turn: no upstream node exists for the partial round,
            #    so pin the tail to the last persisted message id (a digit).
            #  - Clean finish: advance to the upstream token so the next turn
            #    continues the server-side session naturally.
            # final_parent can be a stale/partial token on abort — never trust it
            # when _agent_killed is set.
            # DeepSeek: even on a mid-turn stop, `final_parent` is a real
            # upstream response_message_id (an integer). Pinning the local row
            # id instead makes DeepSeek reject it as "invalid message id" on
            # the next turn — so prefer the upstream token for DeepSeek.
            _is_ds_tail = current_provider == "deepseek"
            if (not _is_ds_tail and _agent_killed) or not final_parent:
                touch_chat(active_chat_id, str(_tail_msg_id))
            else:
                touch_chat(active_chat_id, final_parent)
            # Consume fork history ONLY after a clean, successful turn. A real
            # answer was produced and the upstream session is established, so the
            # injected block is no longer needed. On failure/abort the block stays
            # in the DB so the next send re-injects it instead of losing context.
            if _fork_history_injected and not error_message and not _agent_killed:
                try:
                    with get_db() as _fh_conn:
                        _fh_conn.execute(
                            "UPDATE chats SET fork_history = NULL WHERE id = ?",
                            (active_chat_id,),
                        )
                    logger.info("[fork-history] Consumed fork history after successful turn for %s", active_chat_id)
                except Exception as _fh_err:
                    logger.warning("[fork-history] Failed to clear fork history: %s", _fh_err)
            _ACTIVE_RESPONSE_IDS.pop(active_chat_id, None)
            if _agent_run and not _agent_killed:
                from server.database import update_agent_status
                if error_message:
                    _agent_run.mark_failed(error_message)
                    update_agent_status(active_chat_id, "failed", error=error_message)
                else:
                    _agent_run.mark_completed(answer)
                    update_agent_status(active_chat_id, "completed", result=answer)
                # Push completion/failure to parent chat's notification queue
                # and signal the frontend to start an auto-turn so Maria sees it
                try:
                    from engine.agents.notifications import notification_queue as _nq
                    from engine.agents.protocol import AgentEvent
                    from server.api.routes.agents import push_agent_event as _push_ae
                    from server.api.routes.agents import broadcast_agent_event as _broadcast_ae
                    _parent_chat_id = _agent_run.chat_id or final_parent
                    _nq.push(_parent_chat_id, AgentEvent(
                        type="agent_completed" if not error_message else "agent_failed",
                        agent_id=_agent_run.id,
                        data={
                            "role": _agent_run.role,
                            "result": answer if not error_message else None,
                            "summary": answer[:500] if not error_message else None,
                            "error": error_message or None,
                            "skills_used": _agent_run.skills_used,
                            "words": _agent_run.word_count,
                            "duration": _agent_run.duration,
                        },
                    ))
                    # Build a rich auto-turn message with the full agent result
                    # so Maria sees the complete response, not just a one-liner
                    if error_message:
                        _turn_msg = (
                            f"[Agent {_agent_run.id} ({_agent_run.role}) FAILED]\n\n"
                            f"Error: {error_message}\n\n"
                            f"Acknowledge this failure and take any necessary follow-up action."
                        )
                    else:
                        _turn_msg = (
                            f"[Agent {_agent_run.id} ({_agent_run.role}) COMPLETED]\n\n"
                            f"{answer}\n\n"
                            f"---\n*Skills used: {', '.join(_agent_run.skills_used) or 'none'} | "
                            f"Words: {_agent_run.word_count} | Duration: {_agent_run.duration:.1f}s*"
                        )
                    _trigger_ev = {
                        "type": "auto_turn_trigger",
                        "agent_id": _agent_run.id,
                        "data": {
                            "message": _turn_msg,
                            "parent_chat_id": _parent_chat_id,
                        },
                    }
                    # Push to parent chat's SSE queue (for when user is viewing it)
                    _push_ae(_parent_chat_id, _trigger_ev)
                    # Broadcast auto_turn_trigger to all other connected SSE clients
                    # so the notification isn't lost if user switched to a different chat
                    _broadcast_ae(_trigger_ev, exclude_chat_id=_parent_chat_id)
                    # Also broadcast agent_completed/failed so the topbar card
                    # gets cleaned up regardless of which chat the user is viewing
                    _status_ev = {
                        "type": "agent_completed" if not error_message else "agent_failed",
                        "agent_id": _agent_run.id,
                        "data": {
                            "role": _agent_run.role,
                            "summary": answer[:500] if not error_message else None,
                            "error": error_message or None,
                        },
                    }
                    _broadcast_ae(_status_ev)
                    # Desktop notification: a spawned subagent finished.
                    try:
                        from engine.notifications.policy import notify_category as _notify_ag
                        _ag_role = _agent_run.role or "agent"
                        asyncio.ensure_future(_notify_ag(
                            "agent",
                            "Subagent finished" if not error_message else "Subagent failed",
                            f"{_ag_role}: {answer[:120]}" if not error_message else f"{_ag_role} failed",
                            urgency="normal" if not error_message else "critical",
                        ))
                    except Exception:
                        pass
                except Exception:
                    pass
            # Release auto-turn lock — drains any queued agent results
            try:
                from engine.agents.auto_turn import auto_turn as _at_done
                _at_done.mark_stream_done(active_chat_id)
            except Exception:
                pass
            # Desktop notifications — event-specific (image/research), with a
            # generic long-turn fallback. Policy: engine.notifications.policy.
            try:
                from engine.notifications.policy import (
                    plan_turn_notifications as _plan, notify_category as _notify_cat,
                )
                for _cat, _title, _body, _urg in _plan(skill_events):
                    asyncio.ensure_future(_notify_cat(_cat, _title, _body, urgency=_urg))
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
@router.get("/api/skills/pending/{chat_id}")
async def list_pending(chat_id: str):
    """Return live permission/CWD prompts still awaiting a decision for a chat.

    The approval banner is a single global element, so the frontend needs a
    way to re-hydrate it when the user switches back to a chat that is still
    blocked on a prompt.
    """
    from engine.security.middleware import (
        list_pending_cwd_for_chat,
        list_pending_for_chat,
    )

    approvals = [
        {
            "id": p.tag_id,
            "name": p.name,
            "command": p.content[:500],
            "category": p.category,
            "reason": p.reason,
        }
        for p in list_pending_for_chat(chat_id)
    ]
    cwd_warnings = [
        {
            "id": p.tag_id,
            "name": p.name,
            "path": p.path,
            "cwd": p.cwd,
        }
        for p in list_pending_cwd_for_chat(chat_id)
    ]
    return {"ok": True, "approvals": approvals, "cwd_warnings": cwd_warnings}

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


@router.post("/api/skills/cwd-approve/{tag_id}")
async def cwd_approve_command(tag_id: str, request: Request):
    """User approved a CWD warning — execute the tool with cwd_approved flag."""
    from engine.security.middleware import consume_pending_cwd_warning, cache_session_permission

    body = await request.json() if request else {}
    chat_id = body.get("chat_id")
    session = body.get("session", False)

    # Cache session permission before consuming (so subsequent tools skip warning)
    if session and chat_id:
        cache_session_permission(chat_id, "cwd")

    pending = consume_pending_cwd_warning(tag_id)
    if pending is None:
        return {"ok": False, "error": "CWD warning expired or not found"}

    engine = _get_skill_engine()
    attrs = {**pending.attrs, "cwd_approved": "true"}

    loop = asyncio.get_event_loop()
    def _run():
        return list(engine.process_tag(
            pending.name, attrs, pending.content,
            chat_id=chat_id, cwd=pending.cwd,
        ))
    try:
        events = await loop.run_in_executor(None, _run)
    except Exception as exc:
        events = [{"type": "skill_end", "id": tag_id, "name": pending.name, "ok": False, "error": str(exc)}]

    from engine.skills.events import build_tool_feedback
    has_start = any(ev.get("type") == "skill_start" for ev in events)
    if not has_start:
        events = [{"type": "skill_start", "id": tag_id, "name": pending.name}] + events
    feedback = build_tool_feedback(events) or f"[{pending.name}] OK"

    from server.database import append_skill_event
    if chat_id:
        for ev in events:
            append_skill_event(chat_id, ev)

    return {"ok": True, "feedback": feedback}


@router.post("/api/skills/batch-complete/{chat_id}")
async def batch_complete(chat_id: str, request: Request):
    """All items in a permission batch have been resolved.

    The frontend accumulates individual approve/deny results and calls this
    once every item in the batch is resolved. We combine all feedback into
    one message and send it to the model as a single turn so the model sees
    the full picture instead of fragmented partial results.
    """
    body = await request.json() if request else {}
    results: list[dict] = body.get("results", [])

    if not results:
        return {"ok": False, "error": "No results provided"}

    # Combine all feedback blocks into one structured message
    parts: list[str] = []
    for i, r in enumerate(results, 1):
        status = "APPROVED" if r.get("approved") else "DENIED"
        fb = r.get("feedback", "[no feedback]")
        parts.append(f"--- Tool {i}/{len(results)} [{status}] ---\n{fb}")

    combined = (
        f"[System: All {len(results)} pending tool(s) have been resolved by the user. "
        f"Here are the combined results:]\n\n"
        + "\n\n".join(parts)
        + "\n\n[System: Continue based on ALL results above.]"
    )

    return {"ok": True, "combined_feedback": combined}


@router.post("/api/skills/cwd-deny/{tag_id}")
async def cwd_deny_command(tag_id: str, request: Request):
    """User denied a CWD warning — return tool feedback without executing."""
    from engine.security.middleware import consume_pending_cwd_warning

    pending = consume_pending_cwd_warning(tag_id)
    if pending is None:
        return {"ok": False, "error": "CWD warning expired or not found"}

    path_preview = pending.path[:200]
    feedback = f"[{pending.name}] FAILED — User denied file operation outside project: {path_preview}"

    from server.database import append_skill_event
    chat_id = (await request.json()).get("chat_id") if request else None
    if chat_id:
        append_skill_event(chat_id, {
            "type": "skill_start", "id": tag_id, "name": pending.name,
        })
        append_skill_event(chat_id, {
            "type": "skill_end", "id": tag_id, "name": pending.name,
            "ok": False, "error": "User denied file operation outside project",
        })

    return {"ok": True, "feedback": feedback}
