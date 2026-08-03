from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, UploadFile

from server.config import UPLOAD_DIR


router = APIRouter()

_MAX_UPLOAD_SIZE = 100 * 1024 * 1024  # 100 MB

@router.post("/api/upload")
async def upload(file: UploadFile = File(...)) -> dict[str, Any]:
    """Save file locally. Backend-specific upload (Playwright etc.) happens in /api/chat."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing filename")
    suffix = Path(file.filename).suffix or ".bin"
    stored_name = f"{uuid.uuid4().hex}{suffix}"
    target = UPLOAD_DIR / stored_name
    raw = await file.read()
    if len(raw) > _MAX_UPLOAD_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"File too large ({len(raw)} bytes, max {_MAX_UPLOAD_SIZE})",
        )
    target.write_bytes(raw)
    return {"uploaded": True, "path": str(target)}