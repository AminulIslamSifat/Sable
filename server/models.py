from __future__ import annotations

from typing import Any
from pydantic import BaseModel

class LoginRequest(BaseModel):
    token: str

class RevertRequest(BaseModel):
    path: str
    backup_path: str

class ChatRequest(BaseModel):
    message: str
    chat_id: str | None = None
    parent_id: str | None = None
    files: list[dict[str, Any]] | None = None
    model: str | None = None
    thinking_mode: str | None = None
    stream: bool = True
    ref_file_ids: list[str] | None = None

class NewChatRequest(BaseModel):
    model: str | None = None