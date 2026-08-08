from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from engine.scraper import get_settings as get_scraper_settings
from server.database import ensure_chat, list_chats, get_messages, delete_chat, search_messages, get_skill_events_for_message
from server.utils import retry_async, make_title, _resolve_api_backend
from server.models import NewChatRequest, ContextPassRequest
from connectors import get_connector
from ..dependencies import service

logger = logging.getLogger("sable.context_pass")

router = APIRouter()

@router.get("/api/chats/search")
def search_chats(q: str = "") -> dict[str, Any]:
    if not q.strip():
        return {"results": []}
    return {"results": search_messages(q.strip())}

@router.get("/api/chats")
def chats() -> dict[str, list[dict[str, Any]]]:
    return {"chats": list_chats()}

@router.post("/api/chat/new")
async def new_chat(request: NewChatRequest = NewChatRequest()) -> dict[str, str | None]:
    if get_scraper_settings().get("enabled"):
        chat_id = f"browser-{uuid.uuid4().hex}"
        ensure_chat(chat_id, "New chat", None)
        return {"chat_id": chat_id}
    try:
        chat_id = await retry_async(
            lambda: service.create_chat(model=request.model),
            label="create_chat",
        )
    except Exception as exc:
        return {"error": f"Session startup failed: {type(exc).__name__}: {exc}"}
    if not chat_id:
        return {"error": "Could not create chat session"}
    ensure_chat(chat_id, "New chat", None)
    return {"chat_id": chat_id}

@router.get("/api/chats/{chat_id}/messages")
def chat_messages(
    chat_id: str,
    limit: int | None = None,
    before_id: int | None = None,
    include_skill_events: bool = False,
) -> dict[str, Any]:
    """Load messages with optional pagination.

    - limit: max messages to return (default: all)
    - before_id: load messages older than this id (for infinite scroll)
    - include_skill_events: if true, embed skill_events in response (heavy)
    """
    messages = get_messages(chat_id, limit=limit, before_id=before_id, include_skill_events=include_skill_events)
    # Compute total context chars for the full chat (not just the paginated slice)
    from server.database import get_db
    with get_db() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(LENGTH(content)) + SUM(LENGTH(COALESCE(thinking, ''))), 0) AS total "
            "FROM messages WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
        context_chars = row["total"] if row else 0
    return {"chat_id": chat_id, "messages": messages, "context_chars": context_chars}


@router.get("/api/chats/{chat_id}/messages/{message_id}/events")
def message_skill_events(chat_id: str, message_id: int) -> dict[str, Any]:
    """Lazy-load skill events for a specific message."""
    events = get_skill_events_for_message(message_id)
    return {"message_id": message_id, "skill_events": events}

@router.delete("/api/chats/{chat_id}")
def delete_chat_route(chat_id: str) -> dict[str, Any]:
    deleted = delete_chat(chat_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Chat not found")
    return {"deleted": True, "chat_id": chat_id}

_CONTEXT_PASS_SETTINGS_PATH = Path(__file__).resolve().parents[3] / "system" / "context_pass_settings.json"

def _load_ctx_pass_settings() -> dict[str, str]:
    defaults = {"summarizer_model": "", "browser_data_acc": ""}
    if _CONTEXT_PASS_SETTINGS_PATH.exists():
        try:
            stored = json.loads(_CONTEXT_PASS_SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                defaults.update(stored)
        except Exception:
            pass
    return defaults

@router.post("/api/context/pass")
async def context_pass(req: ContextPassRequest) -> dict[str, Any]:
    messages = get_messages(req.chat_id)
    if not messages:
        return {"error": "No messages in this chat"}

    # Build a compact transcript (skip empty/system noise)
    lines: list[str] = []
    for m in messages:
        role = m.get("role", "unknown")
        content = (m.get("content") or "").strip()
        if not content:
            continue
        if len(content) > 3000:
            content = content[:3000] + "… [truncated]"
        lines.append(f"[{role}]: {content}")

    if len(lines) < 2:
        return {"error": "Not enough context to summarize"}

    transcript = "\n".join(lines)
    if len(transcript) > 60000:
        transcript = transcript[:60000] + "\n… [transcript truncated]"

    prompt = (
        "You are a context handoff summarizer. Below is a conversation transcript from "
        "a session that is being switched to a different model. Produce a focused "
        "operational briefing so the new model can continue the work with zero loss "
        "of state. No filler, no meta-commentary, no 'here is a summary' — jump "
        "straight to substance.\n\n"

        "HARD RULES (apply to all sections):\n"
        "- Never invent, infer, or smooth over details that are not explicitly present "
        "in the transcript. If something is ambiguous, unstated, or uncertain, write "
        "[unclear] instead of guessing.\n"
        "- Preserve all code snippets, file paths, commands, config values, error "
        "messages, and stack traces VERBATIM — never paraphrase or reword these. "
        "Quote them exactly as they appear in the transcript, in code blocks.\n"
        "- Preserve exact technical details: package/library versions, OS, device/"
        "environment specifics, variable names, function signatures.\n"
        "- The word limit below applies to prose only. Verbatim code/error/config "
        "blocks are exempt from the word count and should be included in full when "
        "they represent the current working state.\n\n"

        "Structure (each section progressively more detailed than the last, except "
        "verbatim blocks which are always complete regardless of section):\n\n"

        "• Working topic — Concise but complete: what the topic is, the motive/goal, "
        "the plan, what's been done, what's pending. State clearly whether the task "
        "is finished, abandoned, or stopped mid-way — and if mid-way, the exact point "
        "of interruption.\n\n"

        "• Background — Only the context needed to understand the current task "
        "(prior decisions, constraints, why this approach was chosen over others). "
        "Skip anything not relevant to continuing the work.\n\n"

        "• Last exchange (most detailed so far) — The user's most recent prompt "
        "passed near-verbatim, plus what was actually attempted in response: what "
        "was tried, why that approach was chosen, what succeeded, what failed (with "
        "exact error output), and the precise current state of any files/code/"
        "commands at the point the session stopped.\n\n"

        "• Planned next move (MOST detailed) — Concrete next steps in order, open "
        "questions, known blockers, and every specific file/path/config/command "
        "involved. If the previous model had a next action in mind but didn't "
        "execute it, state exactly what that action was.\n\n"

        "Target ~800 words of prose across all sections combined (verbatim blocks "
        "excluded from this count). Omit anything irrelevant to resuming the work.\n\n"
        f"---\n{transcript}"
    )

    # Load settings: model + browser-data-acc
    settings = _load_ctx_pass_settings()
    model = settings.get("summarizer_model") or req.model  # fallback to current
    browser_acc = settings.get("browser_data_acc", "").strip()

    logger.info(
        "[context-pass] chat_id=%s | model=%r | browser_acc=%r | settings=%s | transcript_len=%d",
        req.chat_id, model, browser_acc, settings, len(transcript),
    )

    try:
        # Route: API connector (gemini/deepseek/groq/mistral) vs Qwen ChatService
        api_backend = _resolve_api_backend(model)

        if api_backend:
            # Non-Qwen model → use the appropriate API connector directly
            logger.info("[context-pass] routing via connector: %s", api_backend)
            connector = get_connector(api_backend)
            result = await connector.chat(
                message=prompt,
                model=model,
                thinking_mode="fast",
            )
        elif browser_acc:
            # Qwen model with dedicated browser profile
            from engine.service import ChatService
            from engine.config import _SYSTEM
            acc_dir = _SYSTEM / browser_acc
            if not acc_dir.exists():
                logger.error("[context-pass] browser profile dir not found: %s", acc_dir)
                return {"error": f"Browser profile '{browser_acc}' not found"}
            logger.info("[context-pass] using dedicated ChatService with profile: %s", acc_dir)
            temp_service = ChatService(user_data_dir=str(acc_dir))
            try:
                result = await temp_service.chat(
                    message=prompt,
                    model=model,
                    thinking_mode="fast",
                )
            finally:
                await temp_service.close()
        else:
            # Qwen model, default service
            logger.info("[context-pass] using default service, model=%r", model)
            result = await service.chat(
                message=prompt,
                model=model,
                thinking_mode="fast",
            )

        logger.info("[context-pass] result keys=%s, answer_len=%d, error=%r",
                    list(result.keys()), len(result.get("answer", "")), result.get("error"))
        answer = result.get("answer", "").strip()
        if not answer:
            err = result.get("error", "")
            logger.warning("[context-pass] empty answer. error=%r, full result: %s", err, result)
            return {"error": f"Summarization returned empty response. {err}".strip()}
        return {"summary": answer}
    except Exception as exc:
        logger.exception("[context-pass] summarization failed")
        return {"error": f"Summarization failed: {type(exc).__name__}: {exc}"}