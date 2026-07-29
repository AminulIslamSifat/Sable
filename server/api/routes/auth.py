from __future__ import annotations

from fastapi import APIRouter, HTTPException
from server.models import LoginRequest
from server.auth import AUTH_TOKEN

router = APIRouter()

@router.post("/api/login")
async def login(payload: LoginRequest) -> dict[str, str]:
    if payload.token.strip() != AUTH_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid token")
    return {"status": "ok"}