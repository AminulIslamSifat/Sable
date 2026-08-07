
#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["telethon"]
# ///
"""Telegram CLI — send/read messages via Telethon.

Reads credentials from system/.telegram_config.json (same format as Sable server).
Uses a separate session file to avoid conflicting with the running server.
All output is JSON for agent consumption.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

# ── Config ────────────────────────────────────────────────────────────────────

_CONFIG_PATHS = [
    Path(__file__).resolve().parents[3] / "system" / ".telegram_config.json",
    Path.home() / "hdd/projects/Sable/system/.telegram_config.json",
]

_SESSION_DIR = None  # resolved at runtime


def _load_config() -> dict[str, Any]:
    for p in _CONFIG_PATHS:
        if p.exists():
            try:
                cfg = json.loads(p.read_text(encoding="utf-8"))
                global _SESSION_DIR
                _SESSION_DIR = p.parent / "telegram_sessions"
                _SESSION_DIR.mkdir(parents=True, exist_ok=True)
                return cfg
            except Exception:
                continue
    print(json.dumps({"error": "No .telegram_config.json found. Configure Telegram first."}))
    sys.exit(1)


# ── Client helper ─────────────────────────────────────────────────────────────

async def _get_client(cfg: dict[str, Any]):
    """Create and connect a Telethon client. Caller must disconnect."""
    try:
        from telethon import TelegramClient
    except ImportError:
        print(json.dumps({"error": "telethon not installed. Run: uv pip install telethon"}))
        sys.exit(1)

    session_path = str(_SESSION_DIR / "sable_skill_tg")
    client = TelegramClient(
        session_path,
        api_id=int(cfg["api_id"]),
        api_hash=cfg["api_hash"],
    )
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        print(json.dumps({"error": "Not authorized. Sign in via Sable settings first."}))
        sys.exit(1)
    return client


# ── Commands ──────────────────────────────────────────────────────────────────

async def cmd_status(cfg: dict[str, Any]) -> dict[str, Any]:
    """Check if Telegram is configured and authorized."""
    if not cfg.get("enabled", False):
        return {"configured": True, "enabled": False, "authorized": False}
    try:
        client = await _get_client(cfg)
        me = await client.get_me()
        await client.disconnect()
        return {
            "configured": True,
            "enabled": True,
            "authorized": True,
            "username": getattr(me, "username", None),
            "first_name": getattr(me, "first_name", None),
            "phone": getattr(me, "phone", None),
        }
    except SystemExit:
        raise
    except Exception as e:
        return {"configured": True, "enabled": True, "authorized": False, "error": str(e)}


async def cmd_chats(cfg: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    """List recent chats/dialogs."""
    client = await _get_client(cfg)
    dialogs = []
    async for d in client.iter_dialogs(limit=limit):
        entity = d.entity
        name = getattr(entity, "title", None) or getattr(entity, "first_name", None) or str(entity.id)
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
        })
    await client.disconnect()
    return dialogs


async def cmd_messages(cfg: dict[str, Any], chat_id: int, limit: int, offset_id: int) -> list[dict[str, Any]]:
    """Get messages from a specific chat."""
    client = await _get_client(cfg)
    msgs = []
    kwargs: dict[str, Any] = {"limit": limit}
    if offset_id:
        kwargs["max_id"] = offset_id
    async for m in client.iter_messages(chat_id, **kwargs):
        sender_name = ""
        try:
            s = await m.get_sender()
            if s:
                sender_name = getattr(s, "first_name", None) or getattr(s, "title", None) or ""
        except Exception:
            pass

        media_type = None
        if m.media:
            from telethon.tl.types import (
                MessageMediaPhoto, MessageMediaDocument,
                MessageMediaGeo, MessageMediaContact,
                MessageMediaWebPage, MessageMediaPoll,
                DocumentAttributeSticker, DocumentAttributeAnimated,
                DocumentAttributeVideo, DocumentAttributeAudio,
            )
            if isinstance(m.media, MessageMediaPhoto):
                media_type = "photo"
            elif isinstance(m.media, MessageMediaDocument):
                doc = m.media.document
                if doc and doc.attributes:
                    for attr in doc.attributes:
                        if isinstance(attr, DocumentAttributeSticker):
                            media_type = "sticker"
                            break
                        if isinstance(attr, DocumentAttributeAnimated):
                            media_type = "gif"
                            break
                        if isinstance(attr, DocumentAttributeVideo):
                            media_type = "video"
                            break
                        if isinstance(attr, DocumentAttributeAudio):
                            media_type = "voice" if attr.voice else "audio"
                            break
                if not media_type:
                    mime = getattr(doc, "mime_type", "") or ""
                    if mime.startswith("image/"):
                        media_type = "photo"
                    elif mime.startswith("video/"):
                        media_type = "video"
                    elif mime.startswith("audio/"):
                        media_type = "audio"
                    else:
                        media_type = "document"
            elif isinstance(m.media, MessageMediaGeo):
                media_type = "location"
            elif isinstance(m.media, MessageMediaContact):
                media_type = "contact"
            elif isinstance(m.media, MessageMediaWebPage):
                media_type = "webpage"
            elif isinstance(m.media, MessageMediaPoll):
                media_type = "poll"
            else:
                media_type = "other"

        entry = {
            "id": m.id,
            "sender": sender_name,
            "text": m.text or "",
            "date": m.date.isoformat() if m.date else None,
            "is_out": m.out,
            "media_type": media_type,
        }
        msgs.append(entry)

    await client.disconnect()
    return list(reversed(msgs))


async def cmd_send(cfg: dict[str, Any], chat_id: int, text: str) -> dict[str, Any]:
    """Send a text message to a chat."""
    if not text.strip():
        return {"ok": False, "error": "Message cannot be empty"}
    client = await _get_client(cfg)
    await client.send_message(chat_id, text)
    await client.disconnect()
    return {"ok": True, "chat_id": chat_id}


async def cmd_search_contacts(cfg: dict[str, Any], query: str, limit: int) -> list[dict[str, Any]]:
    """Search contacts/chats by name."""
    client = await _get_client(cfg)
    results = []
    async for d in client.iter_dialogs(limit=200):
        entity = d.entity
        name = getattr(entity, "title", None) or getattr(entity, "first_name", None) or ""
        if query.lower() in name.lower():
            results.append({
                "id": d.id,
                "name": name,
                "is_group": d.is_group,
                "is_channel": d.is_channel,
            })
            if len(results) >= limit:
                break
    await client.disconnect()
    return results


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Telegram CLI for Sable")
    sub = parser.add_subparsers(dest="command", required=True)

    # status
    sub.add_parser("status", help="Check connection and auth status")

    # chats
    p_chats = sub.add_parser("chats", help="List recent chats")
    p_chats.add_argument("--limit", type=int, default=30)

    # messages
    p_msgs = sub.add_parser("messages", help="Get messages from a chat")
    p_msgs.add_argument("chat_id", type=int, help="Chat/entity ID")
    p_msgs.add_argument("--limit", type=int, default=30)
    p_msgs.add_argument("--offset-id", type=int, default=0)

    # send
    p_send = sub.add_parser("send", help="Send a text message")
    p_send.add_argument("chat_id", type=int, help="Chat/entity ID")
    p_send.add_argument("--text", required=True, help="Message text")

    # search
    p_search = sub.add_parser("search", help="Search contacts/chats by name")
    p_search.add_argument("query", help="Search query")
    p_search.add_argument("--limit", type=int, default=10)

    args = parser.parse_args()
    cfg = _load_config()

    if args.command == "status":
        result = asyncio.run(cmd_status(cfg))
    elif args.command == "chats":
        result = asyncio.run(cmd_chats(cfg, args.limit))
    elif args.command == "messages":
        result = asyncio.run(cmd_messages(cfg, args.chat_id, args.limit, args.offset_id))
    elif args.command == "send":
        result = asyncio.run(cmd_send(cfg, args.chat_id, args.text))
    elif args.command == "search":
        result = asyncio.run(cmd_search_contacts(cfg, args.query, args.limit))
    else:
        result = {"error": f"Unknown command: {args.command}"}

    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
