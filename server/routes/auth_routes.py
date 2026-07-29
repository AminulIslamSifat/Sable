"""Authentication endpoints."""

from fastapi import APIRouter, HTTPException
from server.models import LoginRequest
from server.auth import get_auth_token

router = APIRouter()


@router.post("/api/login")
async def login(payload: LoginRequest) -> dict[str, str]:
    if payload.token.strip() != get_auth_token():
        raise HTTPException(status_code=401, detail="Invalid token")
    return {"status": "ok"}