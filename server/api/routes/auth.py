from __future__ import annotations

from fastapi import APIRouter, HTTPException
from server.models import LoginRequest
import server.auth as _auth

router = APIRouter()

@router.post("/api/login")
async def login(payload: LoginRequest) -> dict[str, str]:
    if payload.token.strip() != _auth.AUTH_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid token")
    return {"status": "ok"}