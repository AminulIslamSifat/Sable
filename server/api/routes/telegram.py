
"""Telegram mini-client endpoints — read-only chat browser via Telethon."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from engine.config import _ROOT, PERSISTENT_ROOT

router = APIRouter(prefix="/api/telegram", tags=["telegram"])

_CONFIG_PATH = PERSISTENT_ROOT / "system" / ".telegram_config.json"
_SESSION_DIR = PERSISTENT_ROOT / "system" / "telegram_sessions"

# ── Lazy Telethon client (singleton, only imported when enabled) ──────────────

_client = None
_client_lock = asyncio.Lock()


def _load_config() -> dict[str, Any] | None:
    if not _CONFIG_PATH.exists():
        return None
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_config(cfg: dict[str, Any]) -> None:
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


async def _get_client():
    """Return connected+authorized Telethon client or raise."""
    global _client
    cfg = _load_config()
    if not cfg or not cfg.get("enabled"):
        raise HTTPException(403, "Telegram is disabled in settings")
    # Fast path: reuse only if connected AND still authorized
    if _client is not None and _client.is_connected():
        try:
            if await _client.is_user_authorized():
                return _client
        except Exception:
            pass
        # Connected but not authorized — stale session, tear it down
        try:
            await _client.disconnect()
        except Exception:
            pass
        _client = None
    async with _client_lock:
        # Double-check after acquiring lock
        if _client is not None and _client.is_connected():
            try:
                if await _client.is_user_authorized():
                    return _client
            except Exception:
                pass
            try:
                await _client.disconnect()
            except Exception:
                pass
            _client = None
        try:
            from telethon import TelegramClient
        except ImportError:
            raise HTTPException(500, "telethon not installed. Run: uv pip install telethon")
        _SESSION_DIR.mkdir(parents=True, exist_ok=True)
        session_path = str(_SESSION_DIR / "sable_tg")
        c = TelegramClient(
            session_path,
            api_id=int(cfg["api_id"]),
            api_hash=cfg["api_hash"],
        )
        await c.connect()
        if not await c.is_user_authorized():
            await c.disconnect()
            raise HTTPException(401, "Not authorized. Please sign in first.")
        _client = c
        return _client


async def disconnect_client():
    """Cleanly disconnect the Telegram client (call on shutdown)."""
    global _client, _auth_client
    for label, cl in [("client", _client), ("auth_client", _auth_client)]:
        if cl is not None:
            try:
                await cl.disconnect()
            except Exception:
                pass
    _client = None
    _auth_client = None


# ── Models ────────────────────────────────────────────────────────────────────

class TelegramConfig(BaseModel):
    api_id: int
    api_hash: str
    enabled: bool = True


class SignInRequest(BaseModel):
    phone: str
    code: str | None = None
    password: str | None = None
    phone_code_hash: str | None = None


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/status")
async def telegram_status():
    """Check if Telegram is configured and connected.

    On fresh startup the in-memory ``_client`` is None even though a valid
    session file exists on disk.  Instead of reporting *disconnected* (which
    forces the user to re-login every restart), we attempt a silent reconnect
    using the persisted session before giving up.
    """
    global _client
    cfg = _load_config()
    if not cfg:
        return {"configured": False, "enabled": False, "connected": False}
    connected = False
    if cfg.get("enabled"):
        # Fast path: reuse existing in-memory client
        if _client:
            try:
                connected = _client.is_connected() and await _client.is_user_authorized()
            except Exception:
                pass
        # Slow path: no client yet — try restoring from session file
        if not connected:
            try:
                c = await _get_client()
                connected = True
                _client = c
            except HTTPException:
                pass  # 401 / 403 → genuinely not authorized or disabled
            except Exception:
                pass
    return {
        "configured": True,
        "enabled": cfg.get("enabled", False),
        "connected": connected,
    }


@router.post("/config")
async def save_telegram_config(req: TelegramConfig):
    """Save Telegram API credentials."""
    _save_config({"api_id": req.api_id, "api_hash": req.api_hash, "enabled": req.enabled})
    return {"ok": True}


@router.get("/chats")
async def list_chats(limit: int = 50):
    """List recent chats/dialogs."""
    client = await _get_client()
    dialogs = []
    async for d in client.iter_dialogs(limit=limit):
        entity = d.entity
        name = getattr(entity, "title", None) or getattr(entity, "first_name", None) or str(entity.id)
        photo_url = None
        try:
            if hasattr(entity, "photo") and entity.photo:
                # We'll fetch photos on-demand via /chat/photo endpoint
                photo_url = f"/api/telegram/chat/{d.id}/photo"
        except Exception:
            pass
        last_msg = ""
        last_date = None
        if d.message:
            last_msg = (d.message.text or d.message.message or "")[:120]
            last_date = d.message.date.isoformat() if d.message.date else None
        dialogs.append({
            "id": d.id,
            "name": name,
            "is_group": d.is_group,
            "is_channel": d.is_channel,
            "unread": d.unread_count,
            "last_message": last_msg,
            "last_date": last_date,
            "has_photo": photo_url is not None,
            "photo_url": photo_url,
        })
    return dialogs


def _classify_media(m):
    """Return (type_str, has_downloadable) for a Telethon message media."""
    if not m:
        return None, False
    from telethon.tl.types import (
        MessageMediaPhoto, MessageMediaDocument,
        MessageMediaGeo, MessageMediaContact,
        MessageMediaWebPage, MessageMediaPoll,
        MessageMediaGame, MessageMediaInvoice,
        DocumentAttributeSticker, DocumentAttributeAnimated,
        DocumentAttributeVideo, DocumentAttributeAudio,
    )
    if isinstance(m, MessageMediaPhoto):
        return "photo", True
    if isinstance(m, MessageMediaDocument):
        doc = m.document
        if doc and doc.attributes:
            for attr in doc.attributes:
                if isinstance(attr, DocumentAttributeSticker):
                    return "sticker", True
                if isinstance(attr, DocumentAttributeAnimated):
                    return "gif", True
                if isinstance(attr, DocumentAttributeVideo):
                    return "video", True
                if isinstance(attr, DocumentAttributeAudio):
                    return "voice" if attr.voice else "audio", True
        mime = getattr(doc, "mime_type", "") or ""
        if mime.startswith("image/"):
            return "photo", True
        if mime.startswith("video/"):
            return "video", True
        if mime.startswith("audio/"):
            return "audio", True
        return "document", True
    if isinstance(m, MessageMediaGeo):
        return "location", False
    if isinstance(m, MessageMediaContact):
        return "contact", False
    if isinstance(m, MessageMediaWebPage):
        return "webpage", False
    if isinstance(m, MessageMediaPoll):
        return "poll", False
    return "other", False


@router.get("/chat/{chat_id}/messages")
async def get_messages(chat_id: int, limit: int = 50, offset_id: int = 0):
    """Get messages from a specific chat."""
    client = await _get_client()
    msgs = []
    async for m in client.iter_messages(chat_id, limit=limit, min_id=offset_id):
        sender_name = ""
        try:
            s = await m.get_sender()
            if s:
                sender_name = getattr(s, "first_name", None) or getattr(s, "title", None) or ""
        except Exception:
            pass
        media_type, has_media = _classify_media(m.media)
        entry = {
            "id": m.id,
            "sender": sender_name,
            "text": m.text or "",
            "date": m.date.isoformat() if m.date else None,
            "is_out": m.out,
            "media_type": media_type,
            "has_media": has_media,
        }
        # For webpages, include URL
        if media_type == "webpage" and hasattr(m.media, "webpage") and m.media.webpage:
            wp = m.media.webpage
            entry["webpage_url"] = getattr(wp, "url", None)
            entry["webpage_title"] = getattr(wp, "title", None)
            entry["webpage_desc"] = (getattr(wp, "description", None) or "")[:200]
        msgs.append(entry)
    return list(reversed(msgs))


def _detect_mime(buf: bytes, fallback: str) -> str:
    """Detect MIME type from magic bytes; fall back to provided default."""
    if len(buf) < 12:
        return fallback
    if buf[:8] == b'\x89PNG\r\n\x1a\n':
        return "image/png"
    if buf[:3] == b'\xff\xd8\xff':
        return "image/jpeg"
    if buf[:4] == b'RIFF' and buf[8:12] == b'WEBP':
        return "image/webp"
    if buf[:4] == b'GIF8':
        return "image/gif"
    if buf[:4] == b'\x00\x00\x00\x18' or buf[:4] == b'\x00\x00\x00\x1c' or buf[:4] == b'\x00\x00\x00\x20' or buf[4:8] == b'ftyp':
        return "video/mp4"
    if buf[:4] == b'OggS':
        return "audio/ogg"
    if buf[:4] == b'ID3' or buf[:2] == b'\xff\xfb':
        return "audio/mpeg"
    if buf[:4] == b'%PDF':
        return "application/pdf"
    return fallback


@router.get("/chat/{chat_id}/media/{msg_id}")
async def get_message_media(chat_id: int, msg_id: int):
    """Download and serve media from a specific message."""
    from fastapi.responses import Response
    client = await _get_client()
    msg = await client.get_messages(chat_id, ids=msg_id)
    if not msg or not msg.media:
        raise HTTPException(404, "No media")
    buf = await client.download_media(msg, file=bytes)
    if not buf:
        raise HTTPException(404, "Download failed")
    # Detect actual content type from magic bytes
    media_type, _ = _classify_media(msg.media)
    mime_fallback = {
        "photo": "image/jpeg", "sticker": "image/webp", "gif": "image/gif",
        "video": "video/mp4", "audio": "audio/ogg", "voice": "audio/ogg",
        "document": "application/octet-stream",
    }.get(media_type, "application/octet-stream")
    ct = _detect_mime(buf, mime_fallback)
    return Response(content=buf, media_type=ct)


class SendMessageRequest(BaseModel):
    chat_id: int
    text: str


@router.post("/send")
async def send_message(req: SendMessageRequest):
    """Send a text message to a chat."""
    client = await _get_client()
    if not req.text.strip():
        raise HTTPException(400, "Message cannot be empty")
    await client.send_message(req.chat_id, req.text)
    return {"ok": True}


@router.get("/chat/{chat_id}/photo")
async def get_chat_photo(chat_id: int):
    """Download and serve a chat's profile photo."""
    from fastapi.responses import Response
    client = await _get_client()
    entity = await client.get_entity(chat_id)
    buf = await client.download_profile_photo(entity, file=bytes)
    if not buf:
        raise HTTPException(404, "No photo")
    ct = _detect_mime(buf, "image/jpeg")
    return Response(content=buf, media_type=ct)


# Keep the auth client alive between send-code and verify
_auth_client = None


@router.post("/signin/send-code")
async def send_code(req: SignInRequest):
    """Send auth code to phone number."""
    global _auth_client
    cfg = _load_config()
    if not cfg:
        raise HTTPException(400, "Configure API credentials first")
    from telethon import TelegramClient
    _SESSION_DIR.mkdir(parents=True, exist_ok=True)
    session_path = str(_SESSION_DIR / "sable_tg")
    # Disconnect any previous auth client
    if _auth_client:
        try: await _auth_client.disconnect()
        except Exception: pass

    last_err: Exception | None = None
    for attempt in range(3):
        try:
            c = TelegramClient(
                session_path,
                api_id=int(cfg["api_id"]),
                api_hash=cfg["api_hash"],
                connection_retries=5,
            )
            await c.connect()
            # Small stabilization delay — MTProto handshake may not be
            # fully ready immediately after connect(), causing "network
            # error" on the very first RPC call.
            await asyncio.sleep(0.3)
            result = await c.send_code_request(req.phone)
            _auth_client = c  # keep alive for verify step
            return {"phone_code_hash": result.phone_code_hash}
        except Exception as e:
            last_err = e
            try:
                await c.disconnect()
            except Exception:
                pass
            if attempt < 2:
                await asyncio.sleep(1.5)

    raise HTTPException(502, f"Failed to send code after 3 attempts: {last_err}")


@router.post("/signin/verify")
async def verify_code(req: SignInRequest):
    """Verify auth code and complete sign-in."""
    global _client, _auth_client
    cfg = _load_config()
    if not cfg:
        raise HTTPException(400, "Configure API credentials first")
    if not req.code:
        raise HTTPException(400, "Code is required")
    if not req.phone_code_hash:
        raise HTTPException(400, "Missing phone_code_hash — send code first")
    # Reuse the auth client from send-code (it has the pending code request)
    c = _auth_client
    if not c or not c.is_connected():
        from telethon import TelegramClient
        _SESSION_DIR.mkdir(parents=True, exist_ok=True)
        session_path = str(_SESSION_DIR / "sable_tg")
        c = TelegramClient(session_path, api_id=int(cfg["api_id"]), api_hash=cfg["api_hash"])
        await c.connect()
    from telethon.errors import SessionPasswordNeededError
    try:
        await c.sign_in(phone=req.phone, code=req.code, phone_code_hash=req.phone_code_hash)
    except SessionPasswordNeededError:
        # 2FA required — keep auth client alive, tell frontend to ask for password
        _auth_client = c
        return {"ok": False, "needs_password": True}
    except Exception as e:
        try: await c.disconnect()
        except Exception: pass
        _auth_client = None
        raise HTTPException(400, f"Sign-in failed: {e}")
    _client = c
    _auth_client = None
    return {"ok": True, "me": (await c.get_me()).first_name}


@router.post("/signin/password")
async def verify_password(req: SignInRequest):
    """Complete sign-in with 2FA password."""
    global _client, _auth_client
    if not _auth_client or not _auth_client.is_connected():
        raise HTTPException(400, "No pending sign-in session")
    if not req.password:
        raise HTTPException(400, "Password is required")
    try:
        await _auth_client.sign_in(password=req.password)
    except Exception as e:
        raise HTTPException(400, f"Wrong password: {e}")
    _client = _auth_client
    _auth_client = None
    return {"ok": True, "me": (await _client.get_me()).first_name}


@router.post("/disconnect")
async def disconnect():
    """Disconnect the Telegram client without disabling."""
    global _client
    if _client:
        try:
            await _client.disconnect()
        except Exception:
            pass
        _client = None
    return {"ok": True}
