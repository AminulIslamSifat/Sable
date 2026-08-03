from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse   # <-- added StreamingResponse
from engine.config import MODELS
from engine.scraper import get_settings as get_scraper_settings
from engine.skills import browse_skills, list_skills
from engine.memory_search import get_searcher

from server.config import (
    DEEPSEEK_MODELS, INDEX_FILE, AUTH_EXEMPT_PREFIXES,
    TYPEWRITER_CHARS_PER_TICK, TYPEWRITER_TICK_MS,
)
from server.models import RevertRequest
from server.utils import logger
from ..dependencies import service, sse
from server.database import list_chats
from server.auth import AUTH_TOKEN
from server.logging_setup import _log_buffer

router = APIRouter()

@router.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}

@router.get("/api/config/ui")
def ui_config() -> dict[str, Any]:
    return {
        "typewriter_chars_per_tick": TYPEWRITER_CHARS_PER_TICK,
        "typewriter_tick_ms": TYPEWRITER_TICK_MS,
    }

@router.get("/api/logs")
async def stream_logs():
    async def generator():
        while True:
            try:
                msg = await asyncio.wait_for(_log_buffer.get(), timeout=15.0)
                yield sse({"type": "log", "message": msg})
            except asyncio.TimeoutError:
                yield sse({"type": "ping"})
    return StreamingResponse(generator(), media_type="text/event-stream")

@router.get("/api/models")
def models() -> dict[str, list[dict[str, Any]]]:
    from engine.config import get_all_models
    scraper_cfg = get_scraper_settings()
    if scraper_cfg.get("enabled") and scraper_cfg.get("engine_type") == "deepseek":
        return {"models": DEEPSEEK_MODELS}
    all_models = get_all_models()
    return {
        "models": [
            {
                "id": m["id"],
                "label": m["label"],
                "api_backend": m.get("api_backend"),
                "capabilities": m.get("capabilities", {}),
                "thinking_modes": [
                    {"id": tm["id"], "label": tm["label"]} for tm in m.get("thinking_modes", [])
                ],
                "custom": m.get("_custom", False),
            }
            for m in all_models
        ]
    }

@router.get("/api/skills")
def skills() -> dict[str, list[dict[str, Any]]]:
    return {"skills": list_skills()}

@router.get("/api/skills/browse")
def skills_browse() -> dict[str, list[dict[str, Any]]]:
    return {"skills": browse_skills()}

@router.post("/api/sync-context")
async def sync_context_route() -> dict[str, Any]:
    success = await service.sync_context()
    if success:
        return {"status": "ok", "message": "Context synced successfully"}
    raise HTTPException(status_code=500, detail="Failed to sync context")

@router.post("/api/file/revert")
def revert_file(payload: RevertRequest) -> dict[str, str]:
    from engine.skills import BACKUP_DIR
    backup = Path(payload.backup_path).expanduser()
    target = Path(payload.path).expanduser()
    try:
        backup.resolve().relative_to(BACKUP_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Backup outside managed directory")
    if not backup.is_file():
        raise HTTPException(status_code=404, detail="Backup not found")
    try:
        shutil.copy2(backup, target)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Revert failed: {exc}")
    return {"status": "ok"}

@router.get("/", response_class=HTMLResponse)
def index() -> str:
    if INDEX_FILE.exists():
        return INDEX_FILE.read_text(encoding="utf-8")
    return "<h1>Sable API is running</h1><p>POST /api/chat</p>"