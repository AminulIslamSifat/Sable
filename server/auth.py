"""Authentication middleware and endpoints."""

import os
from pathlib import Path

from fastapi import HTTPException, Request, Response
from fastapi.responses import JSONResponse

from server.models import LoginRequest

_AUTH_TOKEN_FILE = Path(__file__).resolve().parent.parent / "system/.auth_token"


def load_auth_token() -> str:
    if _AUTH_TOKEN_FILE.exists():
        token = _AUTH_TOKEN_FILE.read_text(encoding="utf-8").strip()
        if token:
            return token
    return os.environ.get("SABLE_TOKEN", "sable")


AUTH_TOKEN = load_auth_token()
AUTH_EXEMPT_PREFIXES = ("/api/login", "/api/health", "/static/", "/uploads/")


async def auth_middleware(request: Request, call_next) -> Response:
    path = request.url.path
    # Skip auth for exempt paths and non-API routes (index.html, etc.)
    if not path.startswith("/api/") or any(path.startswith(p) for p in AUTH_EXEMPT_PREFIXES):
        return await call_next(request)
    auth_header = request.headers.get("authorization", "")
    authorized = auth_header.startswith("Bearer ") and auth_header[7:] == AUTH_TOKEN
    if not authorized and path == "/api/logs":
        # EventSource can't set custom headers — allow ?token= for the log stream only
        authorized = request.query_params.get("token", "") == AUTH_TOKEN
    if not authorized:
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    return await call_next(request)


def get_auth_token() -> str:
    return AUTH_TOKEN