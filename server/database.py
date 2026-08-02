from __future__ import annotations

import sqlite3
import json
from typing import Any
from .config import DB_PATH
from .utils import utcnow

_conn: sqlite3.Connection | None = None


def get_db() -> sqlite3.Connection:
    """Return a persistent module-level connection (WAL mode, safe for concurrent access)."""
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA busy_timeout=5000")
    return _conn

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
        existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(messages)")}
        if "skill_events" not in existing_cols:
            conn.execute("ALTER TABLE messages ADD COLUMN skill_events TEXT")
        if "memory_used" not in existing_cols:
            conn.execute("ALTER TABLE messages ADD COLUMN memory_used TEXT")
        chat_cols = {row["name"] for row in conn.execute("PRAGMA table_info(chats)")}
        if "memory_keys" not in chat_cols:
            conn.execute("ALTER TABLE chats ADD COLUMN memory_keys TEXT DEFAULT '[]'")
        if "chat_url" not in chat_cols:
            conn.execute("ALTER TABLE chats ADD COLUMN chat_url TEXT")
        if "mode" not in chat_cols:
            conn.execute("ALTER TABLE chats ADD COLUMN mode TEXT")
        if "provider" not in chat_cols:
            conn.execute("ALTER TABLE chats ADD COLUMN provider TEXT")

        # --- Multi-agent tables ---
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_runs (
                id TEXT PRIMARY KEY,
                chat_id TEXT NOT NULL,
                parent_agent_id TEXT,
                depth INTEGER NOT NULL DEFAULT 0,
                path TEXT NOT NULL,
                role TEXT NOT NULL,
                task TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'spawned',
                model TEXT,
                browser_data_dir TEXT,
                result TEXT,
                error TEXT,
                tokens_used INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                FOREIGN KEY(chat_id) REFERENCES chats(id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(agent_id) REFERENCES agent_runs(id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_runs_status ON agent_runs(status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_runs_chat ON agent_runs(chat_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_messages_agent ON agent_messages(agent_id)"
        )

def ensure_chat(chat_id: str, title: str = "New chat", parent_id: str | None = None, mode: str | None = None, provider: str | None = None) -> None:
    now = utcnow()
    with get_db() as conn:
        existing = conn.execute("SELECT id, mode, provider FROM chats WHERE id = ?", (chat_id,)).fetchone()
        if existing:
            if mode and not existing["mode"]:
                conn.execute("UPDATE chats SET mode = ? WHERE id = ?", (mode, chat_id))
            if provider and not existing["provider"]:
                conn.execute("UPDATE chats SET provider = ? WHERE id = ?", (provider, chat_id))
            return
        conn.execute(
            "INSERT INTO chats (id, title, parent_id, created_at, updated_at, mode, provider) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (chat_id, title, parent_id, now, now, mode, provider),
        )

def get_chat_mode(chat_id: str) -> str | None:
    with get_db() as conn:
        row = conn.execute("SELECT mode FROM chats WHERE id = ?", (chat_id,)).fetchone()
    return row["mode"] if row and row["mode"] else None

def get_chat_provider(chat_id: str) -> str | None:
    with get_db() as conn:
        row = conn.execute("SELECT provider FROM chats WHERE id = ?", (chat_id,)).fetchone()
    return row["provider"] if row and row["provider"] else None

def set_title_if_default(chat_id: str, title: str) -> None:
    with get_db() as conn:
        row = conn.execute("SELECT title FROM chats WHERE id = ?", (chat_id,)).fetchone()
        if row and row["title"] in ("New chat", ""):
            conn.execute("UPDATE chats SET title = ? WHERE id = ?", (title, chat_id))

def get_injected_memory_keys(chat_id: str) -> set[str]:
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
    with get_db() as conn:
        conn.execute(
            "UPDATE chats SET memory_keys = ? WHERE id = ?",
            (json.dumps(sorted(keys), ensure_ascii=False), chat_id),
        )

def touch_chat(chat_id: str, parent_id: str | None = None) -> None:
    """Update chat timestamp and optionally advance the cached tail pointer.

    When parent_id is None, derives it from the latest message in the chat so
    chats.parent_id never goes stale after auto-turns or mid-stream crashes.
    """
    now = utcnow()
    with get_db() as conn:
        if parent_id is None:
            row = conn.execute(
                "SELECT id FROM messages WHERE chat_id = ? ORDER BY id DESC LIMIT 1",
                (chat_id,),
            ).fetchone()
            parent_id = str(row["id"]) if row else None
        if parent_id is not None:
            conn.execute(
                "UPDATE chats SET updated_at = ?, parent_id = ? WHERE id = ?",
                (now, parent_id, chat_id),
            )
        else:
            conn.execute("UPDATE chats SET updated_at = ? WHERE id = ?", (now, chat_id))

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

def append_skill_event(chat_id: str, event: dict[str, Any]) -> None:
    """Append a single event to the last assistant message's skill_events.

    Uses a single atomic UPDATE with SQLite JSON1 functions — no read-modify-write race.
    """
    event_json = json.dumps(event, ensure_ascii=False)
    with get_db() as conn:
        conn.execute(
            """UPDATE messages SET skill_events = CASE
                WHEN skill_events IS NULL OR skill_events = '' THEN json_array(json(?))
                ELSE json_insert(skill_events, '$[' || json_array_length(skill_events) || ']', json(?))
            END
            WHERE id = (
                SELECT id FROM messages WHERE chat_id = ? AND role = 'assistant' ORDER BY id DESC LIMIT 1
            )""",
            (event_json, event_json, chat_id),
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

def search_messages(query: str, limit: int = 50) -> list[dict[str, Any]]:
    """Search message content across all chats. Returns matching messages with chat info.
    Strips [RELEVANT MEMORY CONTEXT] blocks and timestamp prefixes from results."""
    import re
    _mem_re = re.compile(r'^\[RELEVANT MEMORY CONTEXT\][\s\S]*?\n\n')
    _ts_re = re.compile(r'^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]\n?')
    with get_db() as conn:
        rows = conn.execute(
            "SELECT m.id, m.chat_id, m.role, m.content, m.created_at, c.title "
            "FROM messages m JOIN chats c ON m.chat_id = c.id "
            "WHERE m.content LIKE ? ORDER BY m.id DESC LIMIT ?",
            (f"%{query}%", limit),
        ).fetchall()
        results = []
        for row in rows:
            d = dict(row)
            content = d.get("content") or ""
            content = _mem_re.sub("", content)
            content = _ts_re.sub("", content)
            d["content"] = content
            results.append(d)
        return results


def list_chats() -> list[dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, title, parent_id, created_at, updated_at, provider FROM chats ORDER BY updated_at DESC"
        ).fetchall()
        return [dict(row) for row in rows]

def delete_chat(chat_id: str) -> bool:
    with get_db() as conn:
        conn.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
        cur = conn.execute("DELETE FROM chats WHERE id = ?", (chat_id,))
        return cur.rowcount > 0

def get_chat_tail_id(chat_id: str) -> str | None:
    """Return the id of the latest message in a chat (server-side canonical tail).

    Used by chat route and auto-turn as the authoritative parent for new messages,
    instead of trusting client-supplied parent_id or the cached chats.parent_id.
    Returns None if the chat has no messages yet.
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT id FROM messages WHERE chat_id = ? ORDER BY id DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
        return str(row["id"]) if row else None


def get_parent_id(chat_id: str, requested_parent_id: str | None) -> str | None:
    if requested_parent_id:
        return requested_parent_id
    with get_db() as conn:
        row = conn.execute("SELECT parent_id FROM chats WHERE id = ?", (chat_id,)).fetchone()
        return row["parent_id"] if row else None


# --- Agent persistence ---

def recover_stale_agents() -> int:
    """Mark any agents left in spawned/running state as failed (server restart recovery)."""
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE agent_runs SET status = 'failed', error = 'server_restart', completed_at = ? "
            "WHERE status IN ('spawned', 'running')",
            (utcnow(),),
        )
        return cur.rowcount


def insert_agent_run(
    agent_id: str,
    chat_id: str,
    role: str,
    task: str,
    path: str,
    depth: int = 0,
    parent_agent_id: str | None = None,
    model: str | None = None,
    browser_data_dir: str | None = None,
) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO agent_runs (id, chat_id, parent_agent_id, depth, path, role, task, status, model, browser_data_dir, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'spawned', ?, ?, ?)",
            (agent_id, chat_id, parent_agent_id, depth, path, role, task, model, browser_data_dir, utcnow()),
        )


def update_agent_status(agent_id: str, status: str, result: str | None = None, error: str | None = None, tokens_used: int | None = None) -> None:
    now = utcnow()
    with get_db() as conn:
        if status in ("completed", "failed", "timed_out", "killed"):
            conn.execute(
                "UPDATE agent_runs SET status = ?, result = ?, error = ?, tokens_used = COALESCE(?, tokens_used), completed_at = ? WHERE id = ?",
                (status, result, error, tokens_used, now, agent_id),
            )
        else:
            conn.execute(
                "UPDATE agent_runs SET status = ?, tokens_used = COALESCE(?, tokens_used) WHERE id = ?",
                (status, tokens_used, agent_id),
            )


def add_agent_message(agent_id: str, role: str, content: str) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO agent_messages (agent_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (agent_id, role, content, utcnow()),
        )


def get_agent_runs(chat_id: str) -> list[dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM agent_runs WHERE chat_id = ? ORDER BY created_at ASC",
            (chat_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def get_agent_messages(agent_id: str) -> list[dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, role, content, created_at FROM agent_messages WHERE agent_id = ? ORDER BY id ASC",
            (agent_id,),
        ).fetchall()
        return [dict(row) for row in rows]