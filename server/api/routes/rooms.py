"""Study rooms — Sable's front door to the bridge's room service.

Rooms are persistent, account-owned, and sync by chain ID. The bridge owns the
op-log; this router proxies REST so the browser never needs the bridge token in
a URL, and hands back the WebSocket URL for live fan-out.

Split of responsibilities:
  * REST (create/join/sync/contents/chat) -> proxied here, token stays server-side
  * live op fan-out -> the browser connects to the bridge WebSocket directly

Why the WS goes direct: proxying it would add a hop, a second auth surface, and
reconnection bookkeeping inside Sable for no gain. The bridge already accepts
the same session token the browser holds.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

import server.bridge as bridge

router = APIRouter()


# ── models ──────────────────────────────────────────────────────
class CreateRoomBody(BaseModel):
    name: str = "Study room"
    description: str = ""
    # 'private' = join by code · 'lan' = discoverable on the same network
    visibility: str = "private"


class OpBody(BaseModel):
    kind: str
    payload: dict[str, Any] = {}


class ChatBody(BaseModel):
    text: str


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="missing bearer token")
    return token


def _fail(exc: bridge.BridgeError):
    """Map a bridge failure straight onto the same HTTP status."""
    raise HTTPException(status_code=exc.status, detail=exc.message)


# ── routes ──────────────────────────────────────────────────────
@router.get("/api/rooms")
async def rooms_list(authorization: str | None = Header(None)) -> dict:
    """Rooms this account belongs to, with pending-op counts."""
    token = _bearer(authorization)
    try:
        return await bridge.rooms_list(token)
    except bridge.BridgeError as exc:
        _fail(exc)


@router.get("/api/rooms/public")
async def rooms_public(authorization: str | None = Header(None)) -> dict:
    """LAN-discoverable rooms — lets peers on one network find each other."""
    token = _bearer(authorization)
    try:
        return await bridge.rooms_public(token)
    except bridge.BridgeError as exc:
        _fail(exc)


@router.post("/api/rooms")
async def rooms_create(body: CreateRoomBody,
                       authorization: str | None = Header(None)) -> dict:
    token = _bearer(authorization)
    try:
        return await bridge.room_create(token, body.name, body.description,
                                        body.visibility)
    except bridge.BridgeError as exc:
        _fail(exc)


@router.get("/api/rooms/{room_id}/ws")
async def room_ws_url(room_id: str,
                      authorization: str | None = Header(None)) -> dict:
    """WebSocket URL for live fan-out, plus the token to pass as a query param.

    Browsers cannot set an Authorization header on a WebSocket handshake, so the
    bridge accepts the session token as ``?token=``. That is why the token is
    returned here rather than only living server-side: the browser needs it to
    open the socket. It is the same token the browser already holds.
    """
    token = _bearer(authorization)
    return {"url": bridge.room_ws_url(room_id), "token": token}


@router.get("/api/rooms/{room_id}")
async def room_get(room_id: str,
                   authorization: str | None = Header(None)) -> dict:
    token = _bearer(authorization)
    try:
        return await bridge.room_get(token, room_id)
    except bridge.BridgeError as exc:
        _fail(exc)


@router.post("/api/rooms/{room_id}/join")
async def room_join(room_id: str,
                    authorization: str | None = Header(None)) -> dict:
    token = _bearer(authorization)
    try:
        return await bridge.room_join(token, room_id)
    except bridge.BridgeError as exc:
        _fail(exc)


@router.post("/api/rooms/{room_id}/leave")
async def room_leave(room_id: str,
                     authorization: str | None = Header(None)) -> dict:
    token = _bearer(authorization)
    try:
        return await bridge.room_leave(token, room_id)
    except bridge.BridgeError as exc:
        _fail(exc)


@router.delete("/api/rooms/{room_id}")
async def room_delete(room_id: str,
                      authorization: str | None = Header(None)) -> dict:
    token = _bearer(authorization)
    try:
        return await bridge.room_delete(token, room_id)
    except bridge.BridgeError as exc:
        _fail(exc)


@router.get("/api/rooms/{room_id}/sync")
async def room_sync(room_id: str, since: int = 0, limit: int = 500,
                    authorization: str | None = Header(None)) -> dict:
    """Delta fetch — only ops appended after ``since``."""
    token = _bearer(authorization)
    try:
        return await bridge.room_sync(token, room_id, since, limit)
    except bridge.BridgeError as exc:
        _fail(exc)


@router.post("/api/rooms/{room_id}/ack")
async def room_ack(room_id: str, seq: int = 0, chain: str = "",
                   authorization: str | None = Header(None)) -> dict:
    """Record how far this member has consumed, so pending stays accurate."""
    token = _bearer(authorization)
    try:
        return await bridge.room_ack(token, room_id, seq, chain)
    except bridge.BridgeError as exc:
        _fail(exc)


@router.get("/api/rooms/{room_id}/contents")
async def room_contents(room_id: str,
                        authorization: str | None = Header(None)) -> dict:
    """Full room state (replayed op-log) — for a first-time join."""
    token = _bearer(authorization)
    try:
        return await bridge.room_contents(token, room_id)
    except bridge.BridgeError as exc:
        _fail(exc)


@router.post("/api/rooms/{room_id}/op")
async def room_op(room_id: str, body: OpBody,
                  authorization: str | None = Header(None)) -> dict:
    token = _bearer(authorization)
    try:
        return await bridge.room_op(token, room_id, body.kind, body.payload)
    except bridge.BridgeError as exc:
        _fail(exc)


@router.get("/api/rooms/{room_id}/chat")
async def room_chat_history(room_id: str, limit: int = 200,
                            authorization: str | None = Header(None)) -> dict:
    token = _bearer(authorization)
    try:
        return await bridge.room_chat_history(token, room_id, limit)
    except bridge.BridgeError as exc:
        _fail(exc)


@router.post("/api/rooms/{room_id}/chat")
async def room_chat_send(room_id: str, body: ChatBody,
                         authorization: str | None = Header(None)) -> dict:
    token = _bearer(authorization)
    try:
        return await bridge.room_chat_send(token, room_id, body.text)
    except bridge.BridgeError as exc:
        _fail(exc)
