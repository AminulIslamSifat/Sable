from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile

from server.config import UPLOAD_DIR
from ..dependencies import service

router = APIRouter()

@router.post("/api/upload")
async def upload(file: UploadFile = File(...)) -> dict[str, Any]:
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