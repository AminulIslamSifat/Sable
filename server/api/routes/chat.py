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
from engine.agents.resilience import LoopDetector, MainChatGuard, TurnCapTracker
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
    except Exception:
        pass  # Never let logging break the stream

router = APIRouter()


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
_tool_call_re = _re.compile(r'<tool_call[\s>]', _re.IGNORECASE)
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

    # --- Format head ---
    head_msgs = messages[first_user_idx:head_end_idx]
    head_lines = []
    first_user_content = (messages[first_user_idx].get("content") or "").strip()
    head_lines.append(f"## First User Message\n{first_user_content}")

    # Remaining head messages are early tool calls
    early_tc_parts = []
    for m in head_msgs[1:]:
        content = (m.get("content") or "").strip()
        if content:
            early_tc_parts.append(content)
    if early_tc_parts:
        head_lines.append("## Early Tool Calls\n" + "\n".join(early_tc_parts))

    head_text = "\n\n".join(head_lines)

    # --- Format tail ---
    tail_msgs = messages[tail_start_idx:]
    tail_lines = []

    # Find last user message content in tail
    last_user_content = (messages[last_user_idx].get("content") or "").strip()

    # Messages after last user message are recent tool calls
    recent_tc_parts = []
    for m in messages[last_user_idx + 1:]:
        content = (m.get("content") or "").strip()
        if content:
            recent_tc_parts.append(content)

    tail_lines.append(f"## Last User Message\n{last_user_content}")
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

    messages = get_messages(chat_id)
    if not messages:
        return ""

    # --- Calculate total length ---
    total_chars = sum(len((m.get("content") or "").strip()) for m in messages)

    # Under threshold — no summarization needed
    if total_chars <= 500_000:
        lines = []
        for m in messages:
            role = m.get("role", "unknown")
            content = (m.get("content") or "").strip()
            if content:
                lines.append(f"[{role}]: {content}")
        return "\n".join(lines)

    # --- Check cache ---
    now = _time.monotonic()
    cached = _summarizer_cache.get(chat_id)
    if cached and (now - cached[0]) < _SUMMARIZER_CACHE_TTL:
        logger.info("[auto-switch] Using cached summary for %s", chat_id)
        return cached[1]

    # --- Extract head/tail programmatically ---
    head_text, tail_text, middle_messages = _extract_head_tail(messages)

    # Format middle into transcript for summarizer
    middle_lines = []
    for m in middle_messages:
        role = m.get("role", "unknown")
        content = (m.get("content") or "").strip()
        if content:
            middle_lines.append(f"[{role}]: {content}")
    middle_text = "\n".join(middle_lines)

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
    _system_instruction_for_tokens = ""
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
        # Fork history injection: for Qwen forks, prepend conversation history
        # to the first message so the fresh upstream session has context.
        if _upstream_session_id and parent_id is None:
            try:
                with get_db() as _fh_conn:
                    _fh_row = _fh_conn.execute(
                        "SELECT fork_history FROM chats WHERE id = ?", (active_chat_id,)
                    ).fetchone()
                    if _fh_row and _fh_row["fork_history"]:
                        current_message = _fh_row["fork_history"] + current_message
                        _fh_conn.execute(
                            "UPDATE chats SET fork_history = NULL WHERE id = ?",
                            (active_chat_id,),
                        )
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
                    nonlocal _title_buf, current_parent, final_parent
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
                                    yield sse({"type": "approval_pending", "text": "⏳ Waiting for your approval on that command."})
                                # When CWD warning is emitted, emit a transient status
                                if ev.get("type") == "cwd_warning":
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
                _round_prompt_tokens = 0  # initialized before branching; set in each path
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
                    try:
                        _round_prompt_tokens = count_prompt_tokens(
                            system_instruction=_system_instruction_for_tokens,
                            user_message=current_message,
                            memory_context=_memory_context if round_index == 0 else "",
                        )
                    except Exception:
                        _round_prompt_tokens = 0
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
                    # --- Token counting: prompt side (Qwen) ---
                    try:
                        _qwen_history = []
                        if round_index == 0 and active_chat_id:
                            _qwen_msgs = get_messages(active_chat_id)
                            _qwen_history = [{"role": m["role"], "content": m["content"]} for m in _qwen_msgs if m["role"] in ("user", "assistant") and m["content"]]
                        _round_prompt_tokens = count_prompt_tokens(
                            system_instruction=_system_instruction_for_tokens,
                            history=_qwen_history,
                            user_message=current_message,
                            memory_context=_memory_context if round_index == 0 else "",
                        )
                    except Exception:
                        _round_prompt_tokens = 0
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
                # --- Chunk timeout for main stream ---
                print(f"[MAIN-STREAM] ▶ Starting main stream loop (first_chunk_timeout=15s, stall_timeout=30s)")
                _MAIN_FIRST_CHUNK_TIMEOUT = 15.0
                _MAIN_STALL_TIMEOUT = 30.0
                _main_got_first = False
                _main_timeout_retries = 0
                _main_iter = round_event_source.__aiter__()

                while True:
                    try:
                        if not _main_got_first:
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
                            print(f"[MAIN-STREAM]   ⏰ FIRST-CHUNK TIMEOUT ({_MAIN_FIRST_CHUNK_TIMEOUT}s), attempt {_main_timeout_retries}/2")
                            logger.warning("[main-stream] First-chunk timeout (%ds), attempt %d/2 for chat %s",
                                           _MAIN_FIRST_CHUNK_TIMEOUT, _main_timeout_retries, active_chat_id)
                            if _main_timeout_retries >= 2:
                                # Synthesize waf_blocked to trigger auto-switch
                                yield sse({"type": "status", "message": "first_chunk_timeout_triggering_switch"})
                                event = {"type": "waf_blocked", "message": "No response within 15s after 2 attempts — connection hung"}
                                # Fall through to normal event handling below
                            else:
                                # Retry: close current service, re-create stream
                                yield sse({"type": "status", "message": f"retrying_timeout_{_main_timeout_retries + 1}"})
                                try:
                                    await service.close()
                                    await service._ensure_headers()
                                except Exception as _retry_exc:
                                    logger.warning("[main-stream] Retry refresh failed: %s", _retry_exc)
                                round_event_source = retry_stream(
                                    lambda: service.stream_events(
                                        message=current_message,
                                        chat_id=_qwen_chat_id,
                                        parent_id=current_parent,
                                        files=files_for_round,
                                        model=request.model,
                                        thinking_mode=request.thinking_mode,
                                    ),
                                    label=f"stream_round_{round_index}_retry{_main_timeout_retries}",
                                )
                                _main_iter = round_event_source.__aiter__()
                                _main_got_first = False
                                continue
                        else:
                            # Mid-stream stall — connection died after partial response
                            logger.warning("[main-stream] Stall timeout (%ds) after first chunk for chat %s",
                                           _MAIN_STALL_TIMEOUT, active_chat_id)
                            yield sse({"type": "status", "message": "stream_stall_timeout_triggering_switch"})
                            event = {"type": "waf_blocked", "message": f"Stream stalled for {_MAIN_STALL_TIMEOUT}s mid-response — connection died"}
                            # Fall through to auto-switch handler
                    except StopAsyncIteration:
                        break

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
                        # Escalate generic errors that look like rate-limit/captcha to auto-switch
                        _err_lower_check = error_message.lower()
                        _is_rate_limit = any(kw in _err_lower_check for kw in (
                            "ratelimit", "rate_limit", "rate limit", "quota",
                            "daily usage", "exceeded", "429", "too many requests",
                        ))
                        _is_captcha = any(kw in _err_lower_check for kw in (
                            "captcha", "waf", "validate", "rgv587", "blocked", "forbidden",
                        ))
                        if _is_rate_limit or _is_captcha:
                            print(f"[MAIN-STREAM]   ⚡ ESCALATING generic error to {'rate_limited' if _is_rate_limit else 'waf_blocked'}: {error_message[:100]}")
                            logger.warning("[main-stream] Generic error looks like %s, escalating to auto-switch: %s",
                                           "rate_limit" if _is_rate_limit else "captcha", error_message[:200])
                            # Rewrite event type so the auto-switch block below catches it
                            event = {**event, "type": "rate_limited" if _is_rate_limit else "waf_blocked"}
                            event_type = event["type"]
                            # Fall through to rate_limited/waf_blocked handler below
                        else:
                            stream_error = True
                    if event_type in ("rate_limited", "waf_blocked"):
                        print(f"[AUTO-SWITCH] ▶ TRIGGERED by {event_type} — msg={str(event.get('message',''))[:100]}")
                        pending_thinking.clear()
                        async for _sse_line in _drain_sync_gen(emit_flush()):
                            yield _sse_line
                        # --- Auto-switch to next available account (with retry loop) ---
                        _switch_reason = "rate_limit" if event_type == "rate_limited" else "waf_block"
                        logger.info("[auto-switch] Triggered by %s for chat %s", _switch_reason, active_chat_id)
                        yield sse({"type": "account_switch", "step": "triggered", "reason": _switch_reason})

                        from engine.config import (
                            get_next_available_account,
                            _resolve_active_account as _get_active,
                            mark_account_captcha_blocked,
                            mark_account_exhausted,
                        )
                        _current_acc = _get_active()

                        # Mark captcha-blocked accounts so they're deprioritized
                        if event_type == "waf_blocked":
                            mark_account_captcha_blocked(_current_acc)

                        _tried_accounts: set[str] = {_current_acc}
                        _switch_max_retries = 10
                        _switch_attempt = 0
                        _switch_success = False

                        while _switch_attempt < _switch_max_retries and not _switch_success:
                            _switch_attempt += 1
                            yield sse({"type": "account_switch", "step": "searching", "current": _current_acc, "attempt": _switch_attempt})
                            _next_acc = get_next_available_account(exclude=_tried_accounts)

                            if not _next_acc:
                                # No accounts left — fall back to original error behavior
                                print(f"[AUTO-SWITCH]   ✗ NO ACCOUNTS AVAILABLE (tried {_tried_accounts})")
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
                            print(f"[AUTO-SWITCH]   ↳ attempt {_switch_attempt}: switching {_current_acc} → {_next_acc}")
                            logger.info("[auto-switch] Attempt %d: Switching from %s → %s", _switch_attempt, _current_acc, _next_acc)
                            yield sse({"type": "account_switch", "step": "switching", "from": _current_acc, "to": _next_acc, "attempt": _switch_attempt})
                            try:
                                await service.switch_account(_next_acc)
                                logger.info("[auto-switch] Service switched to %s", _next_acc)

                                # Strip old profile in background (fire-and-forget, non-blocking)
                                from pathlib import Path as _Path
                                from engine.config import _SYSTEM as _SYS
                                _old_profile = _SYS / _current_acc
                                if _old_profile.is_dir():
                                    from server.api.routes.settings import _spawn_bg, _strip_one_profile
                                    async def _auto_strip_bg(profile: _Path) -> None:
                                        try:
                                            name, before, after = await asyncio.to_thread(_strip_one_profile, profile)
                                            logger.info("[auto-switch] Stripped old profile %s: %.1fMB → %.1fMB", name, before, after)
                                        except Exception as exc:
                                            logger.warning("[auto-switch] Failed to strip old profile: %s", exc)
                                    _spawn_bg(_auto_strip_bg(_old_profile))
                            except Exception as _sw_exc:
                                logger.error("[auto-switch] Symlink switch failed: %s", _sw_exc)
                                yield sse({"type": "account_switch", "step": "failed", "error": str(_sw_exc)})
                                error_message = f"Account switch failed: {_sw_exc}"
                                stream_error = True
                                yield sse(event)
                                continue

                            # Sync system instructions to new account before first message
                            yield sse({"type": "account_switch", "step": "syncing", "account": _next_acc})
                            try:
                                await service.sync_context()
                                logger.info("[auto-switch] sync_context completed for %s", _next_acc)
                            except Exception as _sync_exc:
                                logger.warning("[auto-switch] sync_context failed for %s: %s", _next_acc, _sync_exc)

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

                            # Per-account retry: try up to 3 times on this account before switching to next
                            _PER_ACCOUNT_MAX_RETRIES = 3
                            _per_account_attempt = 0
                            _per_account_success = False

                            while _per_account_attempt < _PER_ACCOUNT_MAX_RETRIES and not _per_account_success:
                                _per_account_attempt += 1
                                _switch_msg = _switch_ctx if _switch_ctx else current_message

                                if _per_account_attempt > 1:
                                    logger.info("[auto-switch] Per-account retry %d/%d for %s",
                                                _per_account_attempt, _PER_ACCOUNT_MAX_RETRIES, _next_acc)
                                    yield sse({"type": "account_switch", "step": "retrying",
                                               "account": _next_acc, "reason": "empty_response",
                                               "attempt": _switch_attempt,
                                               "per_account_attempt": _per_account_attempt})
                                    # Brief pause before retry to let transient issues clear
                                    await asyncio.sleep(2)

                                print(f"[AUTO-SWITCH]     ↳ starting stream on {_next_acc} (per_account_attempt={_per_account_attempt})")
                                round_event_source = service.stream_events(
                                    message=_switch_msg,
                                    chat_id=_upstream_session_id,
                                    parent_id=None,
                                    files=files_for_round,
                                    model=request.model,
                                    thinking_mode=request.thinking_mode,
                                )

                                # First-chunk timeout: if no event arrives within 15s,
                                # treat as hung connection → mark account + retry next
                                _FIRST_CHUNK_TIMEOUT = 15.0
                                _got_first_event = False
                                _restream_iter = round_event_source.__aiter__()
                                _restream_timed_out = False
                                _got_any_answer = False
                                _skip_to_next_account = False

                                while True:
                                    try:
                                        if not _got_first_event:
                                            _sw_event = await asyncio.wait_for(
                                                _restream_iter.__anext__(),
                                                timeout=_FIRST_CHUNK_TIMEOUT,
                                            )
                                            _got_first_event = True
                                        else:
                                            _sw_event = await _restream_iter.__anext__()
                                    except asyncio.TimeoutError:
                                        # No data within timeout — connection hung
                                        logger.warning("[auto-switch] First-chunk timeout (%ds) for %s, marking captcha-blocked",
                                                       _FIRST_CHUNK_TIMEOUT, _next_acc)
                                        mark_account_captcha_blocked(_next_acc)
                                        _tried_accounts.add(_next_acc)
                                        _restream_timed_out = True
                                        _skip_to_next_account = True
                                        yield sse({"type": "account_switch", "step": "retrying",
                                                   "account": _next_acc, "reason": "timeout", "attempt": _switch_attempt})
                                        _current_acc = _next_acc
                                        break  # break inner while → skip to next account
                                    except StopAsyncIteration:
                                        break  # generator exhausted normally

                                    _sw_type = _sw_event.get("type")
                                    if _sw_type == "meta":
                                        continue
                                    if _sw_type == "answer":
                                        _got_any_answer = True
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
                                        _err_msg = str(_sw_event.get("message", ""))
                                        # Defense-in-depth: detect rate-limit/captcha in generic errors
                                        _err_lower = _err_msg.lower()
                                        _is_rl = any(kw in _err_lower for kw in ("ratelimit", "rate_limit", "rate limit", "quota", "daily usage", "exceeded", "429"))
                                        _is_cap = any(kw in _err_lower for kw in ("captcha", "waf", "validate", "rgv587", "blocked", "forbidden"))
                                        if _is_rl or _is_cap:
                                            _mark_reason = "rate_limited" if _is_rl else "waf_blocked"
                                            print(f"[AUTO-SWITCH]     ⚡ MARKING {_next_acc} as {_mark_reason} (rl={_is_rl}, cap={_is_cap})")
                                            logger.warning("[auto-switch] Generic error looks like %s for %s: %s", _mark_reason, _next_acc, _err_msg[:200])
                                            if _is_rl:
                                                mark_account_exhausted(_next_acc)
                                            if _is_cap:
                                                mark_account_captcha_blocked(_next_acc)
                                            _tried_accounts.add(_next_acc)
                                            _skip_to_next_account = True
                                            yield sse({"type": "account_switch", "step": "retrying",
                                                       "account": _next_acc, "reason": _mark_reason, "attempt": _switch_attempt})
                                            _current_acc = _next_acc
                                            break  # skip to next account
                                        # Non-fatal error: treat as retryable on same account
                                        logger.warning("[auto-switch] Non-fatal error on %s (per-account attempt %d): %s",
                                                       _next_acc, _per_account_attempt, _err_msg[:200])
                                        error_message = _err_msg or "Unknown error after switch"
                                        stream_error = True
                                    elif _sw_type in ("rate_limited", "waf_blocked"):
                                        # New account also blocked — mark and skip to next account
                                        print(f"[AUTO-SWITCH]     ⚡ EXPLICIT {_sw_type} from {_next_acc} — marking & skipping")
                                        _tried_accounts.add(_next_acc)
                                        if _sw_type == "waf_blocked":
                                            mark_account_captcha_blocked(_next_acc)
                                        if _sw_type == "rate_limited":
                                            mark_account_exhausted(_next_acc)
                                        logger.warning("[auto-switch] New account %s also blocked (%s), will retry", _next_acc, _sw_type)
                                        pending_thinking.clear()
                                        async for _sse_line in _drain_sync_gen(emit_flush()):
                                            yield _sse_line
                                        _skip_to_next_account = True
                                        yield sse({"type": "account_switch", "step": "retrying", "account": _next_acc, "reason": _sw_type, "attempt": _switch_attempt})
                                        _current_acc = _next_acc
                                        break  # break inner while → skip to next account
                                    yield sse(_sw_event)

                                # Decide what to do after this per-account attempt
                                if _skip_to_next_account:
                                    # Fatal issue (timeout/rate-limit/captcha) → go to next account immediately
                                    break  # break per-account loop → continue outer account loop

                                if _got_any_answer and not stream_error:
                                    # Got actual content — success!
                                    print(f"[AUTO-SWITCH]   ✓ SUCCESS on {_next_acc} (attempt {_switch_attempt})")
                                    _per_account_success = True
                                    _switch_success = True
                                    break  # break per-account loop → exit outer loop

                                # Empty response or non-fatal error → retry on same account (if attempts remain)
                                if _per_account_attempt < _PER_ACCOUNT_MAX_RETRIES:
                                    logger.warning("[auto-switch] Empty/error response on %s (attempt %d/%d), retrying same account",
                                                   _next_acc, _per_account_attempt, _PER_ACCOUNT_MAX_RETRIES)
                                    # Reset stream_error for retry — only keep it if final attempt fails
                                    stream_error = False
                                    continue  # retry same account
                                else:
                                    # Exhausted per-account retries → move to next account
                                    logger.warning("[auto-switch] All %d per-account retries exhausted for %s, moving to next account",
                                                   _PER_ACCOUNT_MAX_RETRIES, _next_acc)
                                    _tried_accounts.add(_next_acc)
                                    _current_acc = _next_acc
                                    break  # break per-account loop → continue outer account loop

                            if _per_account_success:
                                break  # Exit outer while loop — we got a response
                            # Otherwise continue outer while to try next account

                        # All retries exhausted without success
                        if not _switch_success and not stream_error:
                            print(f"[AUTO-SWITCH] ✗ ALL {_switch_attempt} ATTEMPTS FAILED — giving up")
                            yield sse({"type": "account_switch", "step": "failed", "error": f"all_{_switch_attempt}_attempts_failed"})
                            error_message = f"All {_switch_attempt} account switch attempts failed."
                            stream_error = True
                            yield sse(event)
                    else:
                        yield sse(event)
                round_thinking_text = "".join(round_thinking_parts)
                if round_thinking_text:
                    skill_events.append({"type": "round_thinking", "text": round_thinking_text})
                round_text = "".join(round_answer_parts)
                if round_text:
                    skill_events.append({"type": "round_text", "text": round_text})
                if round_skill_events:
                    skill_events.extend(round_skill_events)
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
            # Desktop notification: fire when 3+ tool calls were executed this turn
            try:
                from server.api.routes.settings import _read_system_settings as _rs
                _dn_enabled = _rs().get("desktop_notifications", True)
                if _dn_enabled:
                    _tool_call_count = sum(
                        1 for _ev in skill_events
                        if _ev.get("type") == "skill_end"
                        and _ev.get("name") not in ("chat_title", "_loop_warning", "action_parse")
                    )
                    if _tool_call_count >= 3:
                        from engine.notifications.desktop import notify_desktop
                        _ok_count = sum(
                            1 for _ev in skill_events
                            if _ev.get("type") == "skill_end" and _ev.get("ok")
                            and _ev.get("name") not in ("chat_title", "_loop_warning", "action_parse")
                        )
                        asyncio.ensure_future(notify_desktop(
                            "Sable · Turn Complete",
                            f"{_tool_call_count} tools used ({_ok_count} succeeded)",
                        ))
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
