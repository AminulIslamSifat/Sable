"""Account routes — Sable's local front door to the bridge identity service.

Flow: the bridge owns users + sessions. On a successful bridge login/register
the returned session token is stored as this install's local auth token, so the
app's existing bearer middleware keeps working untouched. One install = one
user; logging out clears the local token (the bridge session is stateless and
simply expires).

Endpoints:
  GET  /api/account/config     public — bridge URL + Google availability
  POST /api/account/register   email + password -> session
  POST /api/account/login      email + password -> session
  POST /api/account/google     OAuth code -> session
  GET  /api/account/me         current user (live bridge check)
  POST /api/account/logout     clear local session
  POST /api/account/provision  mint/reuse this user's DEK
"""
from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

import server.auth as _auth
import server.bridge as bridge
from server.utils import logger

router = APIRouter()


# ── request models ──────────────────────────────────────────────
class RegisterBody(BaseModel):
    email: str
    password: str
    name: str = ""


class LoginBody(BaseModel):
    email: str
    password: str


class GoogleBody(BaseModel):
    code: str
    redirect_uri: str


class BridgeUrlBody(BaseModel):
    url: str


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="missing bearer token")
    return token


def _store_session(result: dict) -> dict:
    """Persist a bridge session as the local auth token and return the payload."""
    token = result.get("token")
    if not token:
        raise HTTPException(status_code=502, detail="bridge returned no session token")
    _auth.set_auth_token(token)
    return result


# ── routes ──────────────────────────────────────────────────────
@router.get("/oauth/callback")
async def oauth_callback():
    """Google redirect target — serve the SPA, which reads ?code= and finishes."""
    from fastapi.responses import FileResponse
    from server.config import INDEX_FILE
    if INDEX_FILE.is_file():
        return FileResponse(INDEX_FILE)
    raise HTTPException(status_code=404, detail="index not found")


@router.get("/api/account/config")
async def account_config() -> dict:
    """Public: where the bridge lives + whether 'Continue with Google' is on."""
    enabled = False
    client_id = ""
    reachable = True
    try:
        info = await bridge.google_enabled()
        enabled = bool(info.get("enabled", False))
        client_id = info.get("client_id", "") or ""
    except bridge.BridgeError as exc:
        reachable = False
        logger.warning("bridge config probe failed: %s", exc.message)
    return {
        "bridge_url": bridge.bridge_url(),
        "google_enabled": enabled and bool(client_id),
        "google_client_id": client_id,
        "bridge_reachable": reachable,
    }


@router.post("/api/account/register")
async def account_register(body: RegisterBody) -> dict:
    try:
        res = await bridge.register(body.email, body.password, body.name)
    except bridge.BridgeError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message)
    return _store_session(res)


@router.post("/api/account/login")
async def account_login(body: LoginBody) -> dict:
    try:
        res = await bridge.login(body.email, body.password)
    except bridge.BridgeError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message)
    return _store_session(res)


@router.post("/api/account/google")
async def account_google(body: GoogleBody) -> dict:
    try:
        res = await bridge.google_login(body.code, body.redirect_uri)
    except bridge.BridgeError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message)
    return _store_session(res)


@router.get("/api/account/me")
async def account_me(authorization: str | None = Header(None)) -> dict:
    token = _bearer(authorization)
    try:
        return await bridge.me(token)
    except bridge.BridgeError as exc:
        # Session no longer valid — drop the stale local token too.
        if exc.status == 401:
            _auth.clear_auth_token()
        raise HTTPException(status_code=exc.status, detail=exc.message)


@router.post("/api/account/logout")
async def account_logout() -> dict:
    """Clear the local session. Bridge sessions are stateless — nothing to revoke."""
    _auth.clear_auth_token()
    return {"status": "logged_out"}


@router.post("/api/account/provision")
async def account_provision(authorization: str | None = Header(None)) -> dict:
    token = _bearer(authorization)
    try:
        return await bridge.provision(token)
    except bridge.BridgeError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message)


@router.get("/api/account/bridge")
async def account_bridge_info(authorization: str | None = Header(None)) -> dict:
    """Authed: bridge health + current URL (shown in Settings -> Profile)."""
    _bearer(authorization)
    info = {"bridge_url": bridge.bridge_url(), "ok": False, "store": None, "version": None}
    try:
        h = await bridge.health()
        info.update({
            "ok": bool(h.get("ok")),
            "store": h.get("store"),
            "version": h.get("version"),
            "google": h.get("google"),
        })
    except bridge.BridgeError as exc:
        info["error"] = exc.message
    return info


@router.post("/api/account/bridge-url")
async def set_bridge_url(body: BridgeUrlBody,
                        authorization: str | None = Header(None)) -> dict:
    """Authed: override the bridge URL (self-hosted / local dev)."""
    _bearer(authorization)
    url = (body.url or "").strip()
    if url and not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(status_code=400, detail="URL must start with http:// or https://")
    bridge.save_bridge_url(url)
    return {"status": "ok", "bridge_url": bridge.bridge_url()}
