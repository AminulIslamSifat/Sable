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
    # Accept ANY live session token, not just the primary one. A secondary
    # browser validates its cached token through here on every boot — comparing
    # only against AUTH_TOKEN used to 401 it and bounce it back to login.
    if not _auth.is_valid_token(payload.token.strip()):
        raise HTTPException(status_code=401, detail="Invalid token")
    return {"status": "ok"}


@router.post("/api/settings/change-password")
async def change_password(payload: ChangePasswordRequest) -> dict[str, str]:
    """Change the auth token. Requires current password for verification."""
    current = payload.current_password.strip()
    new = payload.new_password.strip()

    if not current or not new:
        raise HTTPException(status_code=400, detail="Both fields are required")
    if not _auth.is_valid_token(current):
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    if len(new) < 4:
        raise HTTPException(status_code=400, detail="New password must be at least 4 characters")

    _AUTH_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    _AUTH_TOKEN_FILE.write_text(new, encoding="utf-8")

    # Reload in-memory primary so the user isn't locked out immediately, and
    # register it as a live session so every other browser keeps working.
    _auth.AUTH_TOKEN = new
    _auth.VALID_TOKENS[new] = {"email": ""}
    _auth._persist_sessions()

    return {"status": "ok"}