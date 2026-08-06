from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from engine.scraper import get_settings as get_scraper_settings
from server.database import ensure_chat, list_chats, get_messages, delete_chat, search_messages
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
def chat_messages(chat_id: str) -> dict[str, Any]:
    return {"chat_id": chat_id, "messages": get_messages(chat_id)}

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
        "You are a context summarizer. Below is a conversation transcript. "
        "Summarize it into a concise briefing that can be used as the FIRST message "
        "in a brand-new chat session, so the new session immediately understands:\n"
        "1. What the user was working on (goal/task)\n"
        "2. Current state — what's done, what's pending\n"
        "3. Key decisions made, constraints, file paths, or technical details\n"
        "4. Any unresolved issues or next steps\n\n"
        "Format: Write it as a direct briefing to the assistant (second person). "
        "Keep it under 800 words. No preamble, no 'here's a summary' — just the briefing.\n\n"
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