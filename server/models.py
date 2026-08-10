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
    cwd: str | None = None
    open_file: str | None = None
    skip_user_save: bool = False

class NewChatRequest(BaseModel):
    model: str | None = None
    project_id: str | None = None

class ProjectCreate(BaseModel):
    name: str
    path: str | None = None

class ProjectUpdate(BaseModel):
    name: str | None = None
    path: str | None = None
    instruction_file: str | None = None
    instruction_text: str | None = None
    use_universal_memory: bool | None = None
    project_memory_enabled: bool | None = None
    facts: str | None = None
    git_repo: str | None = None
    git_username: str | None = None
    git_branch: str | None = None
    persona_enabled: bool | None = None
    output_format_enabled: bool | None = None
    skills_config: dict | None = None

class ContextPassRequest(BaseModel):
    chat_id: str
    model: str | None = None