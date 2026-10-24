from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from engine.scraper import scraper as scraper_service
from connectors import get_connector

router = APIRouter()


def _resolve_connector(backend: str):
    """Resolve a browser connector backend by name.

    Thin wrapper over connectors.get_connector so the route module has a
    single seam to stub in tests.
    """
    return get_connector(backend)

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


# ---------------------------------------------------------------------------
# Per-model browser connectors (connectors/ registry) — dynamic UI metadata
#
# The legacy /api/scraper/models above serves engine/scraper_engines (the
# global scraper-mode toggle). These endpoints serve the newer per-model
# browser backends, which are selectable as their own models and are NOT tied
# to that toggle. Model list is read LIVE from the page so plan-locked models
# are never advertised.
# ---------------------------------------------------------------------------

@router.get("/api/connectors/{backend}/models")
async def get_connector_models(backend: str) -> dict[str, Any]:
    """Live model list + active model for a browser connector backend."""
    connector = _resolve_connector(backend)
    meta_fn = getattr(connector, "get_ui_metadata", None)
    if meta_fn is None:
        raise HTTPException(
            status_code=400,
            detail=f"backend '{backend}' does not expose UI metadata",
        )
    return await meta_fn()


@router.post("/api/connectors/{backend}/model")
async def switch_connector_model(backend: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Switch the active model on a browser connector backend."""
    connector = _resolve_connector(backend)
    switch_fn = getattr(connector, "switch_model", None)
    if switch_fn is None:
        raise HTTPException(
            status_code=400,
            detail=f"backend '{backend}' does not support model switching",
        )
    model_type = str(payload.get("model_type") or "").strip()
    if not model_type:
        raise HTTPException(status_code=422, detail="model_type is required")
    return await switch_fn(model_type)


# ---------------------------------------------------------------------------
# Diagnostics endpoints — internal session health monitoring
# ---------------------------------------------------------------------------

@router.get("/api/scraper/diagnostics")
async def get_scraper_diagnostics() -> dict[str, Any]:
    """Return diagnostic info about active scraper sessions."""
    try:
        from engine.scraper.diagnostics import get_alive_sessions, get_all_sessions, get_recent_events
        alive = get_alive_sessions(max_age=120)
        all_sessions = get_all_sessions()
        events = get_recent_events(limit=20)
        return {
            "active_sessions": len(alive),
            "total_tracked": len(all_sessions),
            "sessions": alive,
            "recent_events": events,
        }
    except Exception as exc:
        return {"error": str(exc), "active_sessions": 0}


@router.post("/api/scraper/diagnostics/replay")
async def start_diagnostic_replay(payload: dict[str, Any]) -> dict[str, Any]:
    """Start a diagnostic replay session."""
    try:
        from engine.scraper.diagnostics import start_replay
        engine_type = str(payload.get("engine_type", "qwen")).strip()
        prompt = str(payload.get("prompt", "")).strip()
        chat_id = payload.get("chat_id")
        metadata = payload.get("metadata")

        rid = start_replay(
            engine_type,
            prompt=prompt,
            chat_id=chat_id,
            metadata=metadata,
        )
        if rid is None:
            return {"error": "Diagnostics helper unavailable"}
        return {"replay_id": rid, "status": "started"}
    except Exception as exc:
        return {"error": str(exc)}


@router.get("/api/scraper/diagnostics/replay/{replay_id}")
async def get_diagnostic_replay_result(replay_id: str) -> dict[str, Any]:
    """Get the result of a diagnostic replay session."""
    try:
        from engine.scraper.diagnostics import get_replay_result
        result = get_replay_result(replay_id)
        if result is None:
            return {"error": "Replay session not found"}
        return result
    except Exception as exc:
        return {"error": str(exc)}


@router.get("/api/scraper/diagnostics/replays")
async def list_diagnostic_replays() -> dict[str, Any]:
    """List recent diagnostic replay sessions."""
    try:
        from engine.scraper.diagnostics import list_replays
        entries = list_replays(limit=20)
        return {"replays": entries, "count": len(entries)}
    except Exception as exc:
        return {"error": str(exc), "replays": []}