"""Health and log endpoints."""

import asyncio
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from server.logging import get_log_buffer
from server.utils import sse

router = APIRouter()


@router.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/api/logs")
async def stream_logs():
    """SSE endpoint that streams live server logs to the frontend."""
    log_buffer = get_log_buffer()
    
    async def generator():
        while True:
            try:
                msg = await asyncio.wait_for(log_buffer.get(), timeout=15.0)
                yield sse({"type": "log", "message": msg})
            except asyncio.TimeoutError:
                yield sse({"type": "ping"})
    
    return StreamingResponse(generator(), media_type="text/event-stream")