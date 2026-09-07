
"""Deep research endpoints — start, poll, cancel, stream progress, fetch result."""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from engine.research.manager import get_research_manager

logger = logging.getLogger("sable.routes.research")
router = APIRouter(tags=["research"])


class ResearchStartRequest(BaseModel):
    query: str = Field(..., min_length=3, max_length=2000)
    model: str | None = None
    models: list[str] = Field(default_factory=list)        # ordered model fallback
    browser_data: list[str] = Field(default_factory=list)  # ordered account fallback
    max_depth: int = Field(3, ge=1, le=5)
    max_time: int = Field(1500, ge=60, le=3600)
    pages_per_topic: int = Field(3, ge=1, le=20)


@router.post("/api/research/start")
async def research_start(body: ResearchStartRequest) -> dict:
    mgr = get_research_manager()
    status = mgr.start_research(
        query=body.query.strip(),
        model=body.model,
        models=[m for m in body.models if m],
        browser_data=[b for b in body.browser_data if b],
        max_depth=body.max_depth,
        max_time=body.max_time,
        pages_per_topic=body.pages_per_topic,
    )
    return status


@router.get("/api/research/active")
async def research_active() -> dict:
    mgr = get_research_manager()
    return {"active": mgr.list_active()}


@router.get("/api/research/status/{session_id}")
async def research_status(session_id: str) -> dict:
    mgr = get_research_manager()
    status = mgr.get_status(session_id)
    if status is None:
        raise HTTPException(404, "No research found for this session")
    return status


@router.post("/api/research/cancel/{session_id}")
async def research_cancel(session_id: str) -> dict:
    mgr = get_research_manager()
    cancelled = mgr.cancel_research(session_id)
    return {"cancelled": cancelled}


@router.post("/api/research/result/{session_id}")
async def research_result(session_id: str) -> dict:
    mgr = get_research_manager()
    result = mgr.get_result(session_id)
    if result is None:
        raise HTTPException(404, "No research result available")
    return result


@router.get("/api/research/incomplete")
async def research_incomplete() -> dict:
    """List all incomplete research sessions (cancelled, error, interrupted)."""
    mgr = get_research_manager()
    return {"incomplete": mgr.list_incomplete()}


class ResearchResumeRequest(BaseModel):
    model: str | None = None
    models: list[str] = Field(default_factory=list)
    browser_data: list[str] = Field(default_factory=list)
    max_depth: int = Field(3, ge=1, le=5)
    max_time: int = Field(1500, ge=60, le=3600)
    pages_per_topic: int = Field(3, ge=1, le=20)


@router.post("/api/research/resume/{session_id}")
async def research_resume(session_id: str, body: ResearchResumeRequest) -> dict:
    """Resume an incomplete research session from its saved checkpoint."""
    mgr = get_research_manager()
    result = mgr.resume_research(
        session_id=session_id,
        model=body.model,
        models=[m for m in body.models if m],
        browser_data=[b for b in body.browser_data if b],
        max_depth=body.max_depth,
        max_time=body.max_time,
        pages_per_topic=body.pages_per_topic,
    )
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


@router.delete("/api/research/{session_id}")
async def research_delete(session_id: str) -> dict:
    """Delete a research session (in-memory + on-disk)."""
    mgr = get_research_manager()
    result = mgr.delete_research(session_id)
    if not result.get("deleted"):
        raise HTTPException(404, result.get("error", "Session not found"))
    return result


@router.get("/api/research/events/{session_id}")
async def research_events(session_id: str, request: Request):
    """SSE stream of research progress events."""
    mgr = get_research_manager()
    status = mgr.get_status(session_id)
    if status is None:
        return StreamingResponse(
            iter([f"data: {json.dumps({'phase': 'error', 'status': 'not found'})}\n\n"]),
            media_type="text/event-stream",
        )

    # If already terminal, emit a single done/error event and close.
    if status["status"] in ("done", "error", "cancelled"):
        terminal = {"phase": "done" if status["status"] == "done" else "error",
                    "status": status.get("error") or status["status"]}
        return StreamingResponse(
            iter([f"data: {json.dumps(terminal, ensure_ascii=False)}\n\n"]),
            media_type="text/event-stream",
        )

    queue = mgr.subscribe(session_id)

    async def generate():
        try:
            # emit current progress immediately
            cur = mgr.get_status(session_id)
            if cur:
                yield f"data: {json.dumps(cur['progress'], ensure_ascii=False, default=str)}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                    yield f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
                    if event.get("phase") in ("done", "error"):
                        break
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    cur = mgr.get_status(session_id)
                    if cur and cur["status"] in ("done", "error", "cancelled"):
                        yield f"data: {json.dumps({'phase': 'done', 'status': cur['status']})}\n\n"
                        break
        finally:
            mgr.unsubscribe(session_id, queue)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )
