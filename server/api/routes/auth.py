from __future__ import annotations

from pydantic import BaseModel
from fastapi import APIRouter, HTTPException
from server.models import LoginRequest
from server.config import _AUTH_TOKEN_FILE
import server.auth as _auth

router = APIRouter()


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


@router.post("/api/login")
async def login(payload: LoginRequest) -> dict[str, str]:
    if payload.token.strip() != _auth.AUTH_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid token")
    return {"status": "ok"}


@router.post("/api/settings/change-password")
async def change_password(payload: ChangePasswordRequest) -> dict[str, str]:
    """Change the auth token. Requires current password for verification."""
    current = payload.current_password.strip()
    new = payload.new_password.strip()

    if not current or not new:
        raise HTTPException(status_code=400, detail="Both fields are required")
    if current != _auth.AUTH_TOKEN:
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    if len(new) < 4:
        raise HTTPException(status_code=400, detail="New password must be at least 4 characters")

    _AUTH_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    _AUTH_TOKEN_FILE.write_text(new, encoding="utf-8")

    # Reload in-memory token so the user isn't locked out immediately
    _auth.AUTH_TOKEN = new

    return {"status": "ok"}