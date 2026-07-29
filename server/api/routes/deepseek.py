from __future__ import annotations

import uuid as _uuid
from pathlib import Path as _Path

from fastapi import APIRouter, File, HTTPException, Query, UploadFile

from server.config import UPLOAD_DIR
from ..dependencies import service

router = APIRouter()

@router.post("/api/deepseek/upload-file")
async def deepseek_upload_file(
    file: UploadFile = File(...),
    model_type: str = Query("vision"),
    thinking_enabled: bool = Query(False),
) -> dict[str, Any]:
    """Upload a file for DeepSeek Vision via shared browser context."""
    suffix = _Path(file.filename or "image.png").suffix
    dest = UPLOAD_DIR / f"ds_{_uuid.uuid4().hex}{suffix}"
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