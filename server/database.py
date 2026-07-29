"""Database operations for Sable."""

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "system/sable.db"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chats (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT 'New chat',
                parent_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                thinking TEXT,
                skill_events TEXT,
                parent_id TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(chat_id) REFERENCES chats(id)
            )
            """
        )
        # Migration: older DBs created before skill_events existed won't have
        # this column just from CREATE TABLE IF NOT EXISTS above (that only
        # fires on a brand-new file). Add it if missing.
        existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(messages)")}
        if "skill_events" not in existing_cols:
            conn.execute("ALTER TABLE messages ADD COLUMN skill_events TEXT")
        if "memory_used" not in existing_cols:
            conn.execute("ALTER TABLE messages ADD COLUMN memory_used TEXT")

        # Migration: add memory_keys column to chats for per-chat dedup
        chat_cols = {row["name"] for row in conn.execute("PRAGMA table_info(chats)")}
        if "memory_keys" not in chat_cols:
            conn.execute("ALTER TABLE chats ADD COLUMN memory_keys TEXT DEFAULT '[]'")
        if "chat_url" not in chat_cols:
            conn.execute("ALTER TABLE chats ADD COLUMN chat_url TEXT")
        if "mode" not in chat_cols:
            conn.execute("ALTER TABLE chats ADD COLUMN mode TEXT")
        if "provider" not in chat_cols:
            conn.execute("ALTER TABLE chats ADD COLUMN provider TEXT")


def ensure_chat(chat_id: str, title: str = "New chat", parent_id: str | None = None, mode: str | None = None) -> None:
    now = utcnow()
    with get_db() as conn:
        existing = conn.execute("SELECT id, mode FROM chats WHERE id = ?", (chat_id,)).fetchone()
        if existing:
            # Lock mode on first real interaction if not yet set
            if mode and not existing["mode"]:
                conn.execute("UPDATE chats SET mode = ? WHERE id = ?", (mode, chat_id))
            return
        conn.execute(
            "INSERT INTO chats (id, title, parent_id, created_at, updated_at, mode) VALUES (?, ?, ?, ?, ?, ?)",
            (chat_id, title, parent_id, now, now, mode),
        )


def get_chat_mode(chat_id: str) -> str | None:
    """Return the locked mode for a chat ('api' or 'scraper'), or None if unset."""
    with get_db() as conn:
        row = conn.execute("SELECT mode FROM chats WHERE id = ?", (chat_id,)).fetchone()
    return row["mode"] if row and row["mode"] else None


def set_title_if_default(chat_id: str, title: str) -> None:
    with get_db() as conn:
        row = conn.execute("SELECT title FROM chats WHERE id = ?", (chat_id,)).fetchone()
        if row and row["title"] in ("New chat", ""):
            conn.execute("UPDATE chats SET title = ? WHERE id = ?", (title, chat_id))


def get_injected_memory_keys(chat_id: str) -> set[str]:
    """Load the set of memory keys already injected into this chat."""
    with get_db() as conn:
        row = conn.execute("SELECT memory_keys FROM chats WHERE id = ?", (chat_id,)).fetchone()
    if not row or not row["memory_keys"]:
        return set()
    try:
        keys = json.loads(row["memory_keys"])
        return set(keys) if isinstance(keys, list) else set()
    except (json.JSONDecodeError, TypeError):
        return set()


def save_injected_memory_keys(chat_id: str, keys: set[str]) -> None:
    """Persist the set of injected memory keys for this chat."""
    with get_db() as conn:
        conn.execute(
            "UPDATE chats SET memory_keys = ? WHERE id = ?",
            (json.dumps(sorted(keys), ensure_ascii=False), chat_id),
        )


def touch_chat(chat_id: str, parent_id: str | None = None) -> None:
    now = utcnow()
    with get_db() as conn:
        if parent_id is None:
            conn.execute("UPDATE chats SET updated_at = ? WHERE id = ?", (now, chat_id))
        else:
            conn.execute(
                "UPDATE chats SET updated_at = ?, parent_id = ? WHERE id = ?",
                (now, parent_id, chat_id),
            )


def save_chat_url(chat_id: str, url: str) -> None:
    with get_db() as conn:
        conn.execute("UPDATE chats SET chat_url = ? WHERE id = ?", (url, chat_id))


def get_chat_url(chat_id: str) -> str | None:
    with get_db() as conn:
        row = conn.execute("SELECT chat_url FROM chats WHERE id = ?", (chat_id,)).fetchone()
        return row["chat_url"] if row and row["chat_url"] else None


def add_message(
    chat_id: str,
    role: str,
    content: str,
    thinking: str | None = None,
    parent_id: str | None = None,
    skill_events: list[dict[str, Any]] | None = None,
    memory_used: list[dict[str, Any]] | None = None,
) -> int:
    now = utcnow()
    skill_events_json = json.dumps(skill_events, ensure_ascii=False) if skill_events else None
    memory_used_json = json.dumps(memory_used, ensure_ascii=False) if memory_used else None
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO messages (chat_id, role, content, thinking, skill_events, memory_used, parent_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (chat_id, role, content, thinking, skill_events_json, memory_used_json, parent_id, now),
        )
        return int(cur.lastrowid)


def update_message(
    message_id: int,
    content: str,
    thinking: str | None = None,
    parent_id: str | None = None,
    skill_events: list[dict[str, Any]] | None = None,
    memory_used: list[dict[str, Any]] | None = None,
) -> None:
    skill_events_json = json.dumps(skill_events, ensure_ascii=False) if skill_events else None
    memory_used_json = json.dumps(memory_used, ensure_ascii=False) if memory_used else None
    with get_db() as conn:
        conn.execute(
            "UPDATE messages SET content = ?, thinking = ?, parent_id = ?, skill_events = ?, memory_used = ? WHERE id = ?",
            (content, thinking, parent_id, skill_events_json, memory_used_json, message_id),
        )


def get_messages(chat_id: str) -> list[dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, chat_id, role, content, thinking, skill_events, memory_used, parent_id, created_at "
            "FROM messages WHERE chat_id = ? ORDER BY id ASC",
            (chat_id,),
        ).fetchall()
        messages = []
        for row in rows:
            msg = dict(row)
            raw_events = msg.get("skill_events")
            try:
                msg["skill_events"] = json.loads(raw_events) if raw_events else []
            except json.JSONDecodeError:
                msg["skill_events"] = []
            raw_mem = msg.get("memory_used")
            try:
                msg["memory_used"] = json.loads(raw_mem) if raw_mem else []
            except json.JSONDecodeError:
                msg["memory_used"] = []
            messages.append(msg)
        return messages


def list_chats() -> list[dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, title, parent_id, created_at, updated_at FROM chats ORDER BY updated_at DESC"
        ).fetchall()
        return [dict(row) for row in rows]


def delete_chat(chat_id: str) -> bool:
    with get_db() as conn:
        conn.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
        cur = conn.execute("DELETE FROM chats WHERE id = ?", (chat_id,))
        return cur.rowcount > 0


def get_parent_id(chat_id: str, requested_parent_id: str | None) -> str | None:
    if requested_parent_id:
        return requested_parent_id
    with get_db() as conn:
        row = conn.execute("SELECT parent_id FROM chats WHERE id = ?", (chat_id,)).fetchone()
        return row["parent_id"] if row else None


def make_title(message: str) -> str:
    clean = " ".join(message.split())
    return clean[:48] or "New chat"