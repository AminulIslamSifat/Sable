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


# ---------------------------------------------------------------------------
# Diagnostics endpoints — internal session health monitoring
# ---------------------------------------------------------------------------

@router.get("/api/scraper/diagnostics")
async def get_scraper_diagnostics() -> dict[str, Any]:
    """Return diagnostic info about active scraper sessions.

    Internal endpoint for troubleshooting stale browser connections.
    Returns session liveness, heartbeat age, and recent diagnostic events.
    """
    try:
        from engine.scraper.diagnostics import get_monitor
        monitor = get_monitor()
        alive = await monitor.get_alive_sessions(max_age=120)
        all_sessions = await monitor.get_all_sessions()
        events = await monitor.get_recent_events(limit=20)
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
    """Start a diagnostic replay session for reproducing scraper issues.

    Creates an isolated test context and optionally injects a prompt
    through the chat pipeline. Used internally to reproduce intermittent
    failures without affecting user-visible chat history.
    """
    try:
        from engine.scraper.diagnostics.replay import get_replay_controller
        ctrl = get_replay_controller()
        engine_type = str(payload.get("engine_type", "qwen")).strip()
        prompt = str(payload.get("prompt", "")).strip()
        chat_id = payload.get("chat_id")
        metadata = payload.get("metadata")

        rid = await ctrl.start_replay(
            engine_type,
            prompt=prompt,
            chat_id=chat_id,
            metadata=metadata,
        )
        return {"replay_id": rid, "status": "started"}
    except Exception as exc:
        return {"error": str(exc)}


@router.get("/api/scraper/diagnostics/replay/{replay_id}")
async def get_diagnostic_replay_result(replay_id: str) -> dict[str, Any]:
    """Get the result of a diagnostic replay session."""
    try:
        from engine.scraper.diagnostics.replay import get_replay_controller
        ctrl = get_replay_controller()
        result = await ctrl.get_result(replay_id)
        if result is None:
            return {"error": "Replay session not found"}
        return result
    except Exception as exc:
        return {"error": str(exc)}


@router.get("/api/scraper/diagnostics/replays")
async def list_diagnostic_replays() -> dict[str, Any]:
    """List recent diagnostic replay sessions."""
    try:
        from engine.scraper.diagnostics.replay import get_replay_controller
        ctrl = get_replay_controller()
        replays = await ctrl.list_replays(limit=20)
        return {"replays": replays, "count": len(replays)}
    except Exception as exc:
        return {"error": str(exc), "replays": []}