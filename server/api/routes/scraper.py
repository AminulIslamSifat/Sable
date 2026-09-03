from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from engine.scraper import scraper as scraper_service

router = APIRouter()

@router.get("/api/scraper/sessions")
async def get_scraper_sessions() -> dict[str, Any]:
    """Return info about the active browser session (chat id, pid, url, liveness)."""
    return await scraper_service.get_session_info()

@router.post("/api/scraper/sessions/kill")
async def kill_scraper_session() -> dict[str, Any]:
    """Forcefully kill the browser process and reset scraper state."""
    return await scraper_service.kill_session()

@router.post("/api/scraper/model")
async def switch_scraper_model(payload: dict[str, Any]) -> dict[str, Any]:
    """Switch the browser engine's active model type."""
    model_type = str(payload.get("model_type") or "default").strip()
    return await scraper_service.switch_model(model_type)

@router.get("/api/scraper/models")
async def get_scraper_models() -> dict[str, Any]:
    """Return engine-specific models and thinking modes for the active scraper engine."""
    return await scraper_service.get_ui_metadata()