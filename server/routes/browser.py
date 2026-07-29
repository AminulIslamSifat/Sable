"""Browser control endpoints."""

from fastapi import APIRouter, HTTPException
from engine.service import ChatService
from connectors.deepseek.client import get_client as get_deepseek_client

router = APIRouter()

# Global service instance
_service: ChatService | None = None


def set_service(service: ChatService) -> None:
    global _service
    _service = service


@router.post("/api/sync-context")
async def sync_context_route() -> dict[str, Any]:
    service = _service
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")
    
    success = await service.sync_context()
    if success:
        return {"status": "ok", "message": "Context synced successfully"}
    raise HTTPException(status_code=500, detail="Failed to sync context")


@router.post("/api/settings/deepseek/refresh-token")
async def refresh_deepseek_token() -> dict[str, Any]:
    """Force-refresh the DeepSeek API token from the browser profile."""
    service = _service
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")
    
    try:
        token = await get_deepseek_client().refresh_token()
        return {"status": "ok", "token_preview": token[:20] + "...", "active": True}
    except Exception as exc:
        logger.error("DeepSeek token refresh failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Refresh failed: {exc}")