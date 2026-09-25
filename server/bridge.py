"""Sable Bridge client — account identity, sessions, per-user DEK provisioning.

The bridge (Sable-bridge: Render + MongoDB) is the identity authority for one
Sable install (one install = one user). This module is the ONLY place that
talks to it. All calls are async (httpx) and raise BridgeError so routes can
map failures straight onto HTTP status codes.

Bridge URL resolution order:
  1. SABLE_BRIDGE_URL env var
  2. system/.bridge_config.json  -> {"url": "..."}
  3. DEFAULT_BRIDGE_URL
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx

DEFAULT_BRIDGE_URL = "https://sable-bridge.onrender.com"
_TIMEOUT = 20.0

_BASE_DIR = Path(__file__).resolve().parent.parent
_CONFIG_FILE = _BASE_DIR / "system" / ".bridge_config.json"


class BridgeError(Exception):
    """Bridge returned an error, or was unreachable."""

    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.message = message
        self.status = status


def bridge_url() -> str:
    env = os.environ.get("SABLE_BRIDGE_URL", "").strip()
    if env:
        return env.rstrip("/")
    try:
        if _CONFIG_FILE.is_file():
            cfg = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
            u = (cfg.get("url") or "").strip()
            if u:
                return u.rstrip("/")
    except Exception:
        pass
    return DEFAULT_BRIDGE_URL


def save_bridge_url(url: str) -> None:
    """Persist a bridge URL override (Settings -> Profile)."""
    _CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    _CONFIG_FILE.write_text(
        json.dumps({"url": (url or "").strip().rstrip("/")}, indent=2),
        encoding="utf-8",
    )


def _handle(resp: httpx.Response) -> dict[str, Any]:
    """Turn a bridge response into a dict, raising BridgeError on failure."""
    if resp.status_code >= 400:
        detail = "bridge error"
        try:
            body = resp.json()
            detail = body.get("detail") or body.get("message") or detail
        except Exception:
            detail = (resp.text or detail)[:200]
        raise BridgeError(str(detail), resp.status_code)
    try:
        return resp.json()
    except Exception:
        raise BridgeError("bridge returned invalid JSON", 502)


async def _request(
    method: str,
    path: str,
    *,
    json_body: dict | None = None,
    token: str | None = None,
) -> dict[str, Any]:
    url = bridge_url() + path
    headers = {"Accept": "application/json"}
    if json_body is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.request(method, url, json=json_body, headers=headers)
    except Exception as exc:  # network / DNS / timeout
        raise BridgeError(f"bridge unreachable ({type(exc).__name__})", 502)
    return _handle(resp)


# ── account auth ────────────────────────────────────────────────
async def register(email: str, password: str, name: str = "") -> dict[str, Any]:
    """Create an account. Returns {token, user}."""
    return await _request(
        "POST", "/v1/auth/register",
        json_body={"email": email, "password": password, "name": name},
    )


async def login(email: str, password: str) -> dict[str, Any]:
    """Email + password login. Returns {token, user}."""
    return await _request(
        "POST", "/v1/auth/login",
        json_body={"email": email, "password": password},
    )


async def google_login(code: str, redirect_uri: str) -> dict[str, Any]:
    """Exchange a Google OAuth code. Returns {token, user}."""
    return await _request(
        "POST", "/v1/auth/google",
        json_body={"code": code, "redirect_uri": redirect_uri},
    )


async def google_enabled() -> dict[str, Any]:
    """Whether the bridge has Google OAuth credentials configured."""
    return await _request("GET", "/v1/auth/google/enabled")


async def me(token: str) -> dict[str, Any]:
    """Fetch the current user for a session token. Returns {user}."""
    return await _request("GET", "/v1/auth/me", token=token)


async def provision(token: str) -> dict[str, Any]:
    """Mint (or reuse) this user's vault DEK. Returns {dek, hash, reused}."""
    return await _request("POST", "/v1/auth/provision", token=token)


async def health() -> dict[str, Any]:
    """Bridge liveness + store status."""
    return await _request("GET", "/health")


# ── rooms ───────────────────────────────────────────────────────
# Rooms live on the bridge (persistent, account-owned, chain-ID sync). These
# are the only calls Sable makes; the browser talks to the bridge's WebSocket
# directly for live fan-out, so REST goes through here and live goes direct.

async def rooms_list(token: str) -> dict[str, Any]:
    """Rooms this account belongs to, with pending-op counts."""
    return await _request("GET", "/v1/rooms", token=token)


async def rooms_public(token: str) -> dict[str, Any]:
    """LAN-discoverable rooms (the local same-network join path)."""
    return await _request("GET", "/v1/rooms/public", token=token)


async def room_create(token: str, name: str, description: str = "",
                      visibility: str = "private") -> dict[str, Any]:
    return await _request(
        "POST", "/v1/rooms", token=token,
        json_body={"name": name, "description": description, "visibility": visibility},
    )


async def room_get(token: str, room_id: str) -> dict[str, Any]:
    return await _request("GET", f"/v1/rooms/{room_id}", token=token)


async def room_join(token: str, room_id: str) -> dict[str, Any]:
    return await _request("POST", f"/v1/rooms/{room_id}/join", token=token, json_body={})


async def room_leave(token: str, room_id: str) -> dict[str, Any]:
    return await _request("POST", f"/v1/rooms/{room_id}/leave", token=token, json_body={})


async def room_delete(token: str, room_id: str) -> dict[str, Any]:
    return await _request("DELETE", f"/v1/rooms/{room_id}", token=token)


async def room_sync(token: str, room_id: str, since: int = 0,
                    limit: int = 500) -> dict[str, Any]:
    """Delta fetch — only ops appended after ``since``."""
    return await _request(
        "GET", f"/v1/rooms/{room_id}/sync?since={int(since)}&limit={int(limit)}",
        token=token,
    )


async def room_ack(token: str, room_id: str, seq: int, chain: str = "") -> dict[str, Any]:
    """Record how far this member has consumed."""
    return await _request(
        "POST", f"/v1/rooms/{room_id}/ack?seq={int(seq)}&chain={chain}",
        token=token, json_body={},
    )


async def room_contents(token: str, room_id: str) -> dict[str, Any]:
    """Full room state (replayed op-log) — for a first-time join."""
    return await _request("GET", f"/v1/rooms/{room_id}/contents", token=token)


async def room_op(token: str, room_id: str, kind: str,
                  payload: dict[str, Any]) -> dict[str, Any]:
    """Append one op (REST path; the WS path is preferred for live edits)."""
    return await _request(
        "POST", f"/v1/rooms/{room_id}/op", token=token,
        json_body={"kind": kind, "payload": payload},
    )


async def room_chat_history(token: str, room_id: str, limit: int = 200) -> dict[str, Any]:
    return await _request(
        "GET", f"/v1/rooms/{room_id}/chat?limit={int(limit)}", token=token,
    )


async def room_chat_send(token: str, room_id: str, text: str) -> dict[str, Any]:
    return await _request(
        "POST", f"/v1/rooms/{room_id}/chat", token=token,
        json_body={"text": text},
    )


def room_ws_url(room_id: str) -> str:
    """WebSocket URL for a room, derived from the bridge HTTP URL.

    The browser connects straight to the bridge for live fan-out — proxying a
    WebSocket through Sable would add a hop and a second auth surface for no
    benefit. http->ws and https->wss so it follows the bridge's scheme.
    """
    base = bridge_url()
    if base.startswith("https://"):
        base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://"):]
    return f"{base}/ws/room/{room_id}"
