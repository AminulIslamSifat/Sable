"""File upload endpoints."""

import uuid
from pathlib import Path
from fastapi import APIRouter, HTTPException, UploadFile, File

from engine.service import ChatService

router = APIRouter()

# Global service instance
_service: ChatService | None = None


def set_service(service: ChatService) -> None:
    global _service
    _service = service


@router.post("/api/upload")
async def upload(file: UploadFile = File(...)) -> dict[str, Any]:
    service = _service
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")
    
    from server.utils import UPLOAD_DIR
    
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing filename")

    suffix = Path(file.filename).suffix or ".bin"
    stored_name = f"{uuid.uuid4().hex}{suffix}"
    target = UPLOAD_DIR / stored_name

    raw = await file.read()
    target.write_bytes(raw)

    result = await service.upload_image(str(target))
    if result is None:
        return {"uploaded": False, "path": str(target)}

    return {"uploaded": True, "path": str(target), "meta": result}