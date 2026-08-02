from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException
from engine.scraper import get_settings as get_scraper_settings
from server.database import ensure_chat, list_chats, get_messages, delete_chat, search_messages
from server.utils import retry_async, make_title
from server.models import NewChatRequest
from ..dependencies import service

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