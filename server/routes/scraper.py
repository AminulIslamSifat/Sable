"""DeepSeek-specific endpoints."""

import uuid
from pathlib import Path
from fastapi import APIRouter, HTTPException, UploadFile, File, Query

from engine.service import ChatService

router = APIRouter()

# Global service instance
_service: ChatService | None = None


def set_service(service: ChatService) -> None:
    global _service
    _service = service


@router.post("/api/deepseek/upload-file")
async def deepseek_upload_file(
    file: UploadFile = File(...),
    model_type: str = Query("vision"),
    thinking_enabled: bool = Query(False),
) -> dict[str, Any]:
    service = _service
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")
    
    from server.utils import UPLOAD_DIR
    
    suffix = Path(file.filename or "image.png").suffix
    dest = UPLOAD_DIR / f"ds_{uuid.uuid4().hex}{suffix}"
    content = await file.read()
    dest.write_bytes(content)

    try:
        meta = await service.upload_deepseek_file(
            str(dest),
            model_type=model_type,
            thinking_enabled=thinking_enabled,
        )
        return {
            "uploaded": True,
            "path": str(dest),
            "meta": {
                "file_id": meta.get("file_id"),
                "status": meta.get("status"),
                "file_name": meta.get("file_name"),
                "file_size": meta.get("file_size"),
                "model_kind": meta.get("model_kind"),
                "is_image": meta.get("is_image"),
            },
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"DeepSeek upload failed: {exc}")