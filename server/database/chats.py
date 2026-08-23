"""Chat CRUD, messages, projects, and checkpoints."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..utils import utcnow
from .core import get_db


# ── Chat CRUD ────────────────────────────────────────────────────────────────

def ensure_chat(chat_id: str, title: str = "New chat", parent_id: str | None = None, mode: str | None = None, provider: str | None = None, project_id: str | None = None, upstream_session_id: str | None = None) -> None:
    now = utcnow()
    with get_db() as conn:
        existing = conn.execute("SELECT id, mode, provider, project_id, upstream_session_id FROM chats WHERE id = ?", (chat_id,)).fetchone()
        if existing:
            if mode and not existing["mode"]:
                conn.execute("UPDATE chats SET mode = ? WHERE id = ?", (mode, chat_id))
            if provider and existing["provider"] != provider:
                conn.execute("UPDATE chats SET provider = ? WHERE id = ?", (provider, chat_id))
            if project_id is not None and existing["project_id"] != project_id:
                conn.execute("UPDATE chats SET project_id = ? WHERE id = ?", (project_id, chat_id))
            if upstream_session_id and existing["upstream_session_id"] != upstream_session_id:
                conn.execute("UPDATE chats SET upstream_session_id = ? WHERE id = ?", (upstream_session_id, chat_id))
            return
        conn.execute(
            "INSERT INTO chats (id, title, parent_id, created_at, updated_at, mode, provider, project_id, upstream_session_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (chat_id, title, parent_id, now, now, mode, provider, project_id, upstream_session_id),
        )


def rename_chat(old_id: str, new_id: str) -> None:
    """Rename a chat and all its messages to a new ID (e.g. after upstream session recovery)."""
    with get_db() as conn:
        conn.execute("UPDATE chats SET id = ? WHERE id = ?", (new_id, old_id))
        conn.execute("UPDATE messages SET chat_id = ? WHERE chat_id = ?", (new_id, old_id))


def get_chat_mode(chat_id: str) -> str | None:
    with get_db() as conn:
        row = conn.execute("SELECT mode FROM chats WHERE id = ?", (chat_id,)).fetchone()
    return row["mode"] if row and row["mode"] else None


def get_chat_provider(chat_id: str) -> str | None:
    with get_db() as conn:
        row = conn.execute("SELECT provider FROM chats WHERE id = ?", (chat_id,)).fetchone()
    return row["provider"] if row and row["provider"] else None


def get_upstream_session_id(chat_id: str) -> str | None:
    """Get the upstream session ID for a chat (Qwen/DeepSeek server-side session)."""
    with get_db() as conn:
        row = conn.execute("SELECT upstream_session_id FROM chats WHERE id = ?", (chat_id,)).fetchone()
    return row["upstream_session_id"] if row and row["upstream_session_id"] else None


def set_upstream_session_id(chat_id: str, session_id: str | None) -> None:
    """Set or update the upstream session ID for a chat (pass None to clear)."""
    with get_db() as conn:
        conn.execute("UPDATE chats SET upstream_session_id = ? WHERE id = ?", (session_id, chat_id))


def set_title_if_default(chat_id: str, title: str) -> None:
    with get_db() as conn:
        row = conn.execute("SELECT title FROM chats WHERE id = ?", (chat_id,)).fetchone()
        if row and row["title"] in ("New chat", ""):
            conn.execute("UPDATE chats SET title = ? WHERE id = ?", (title, chat_id))


def update_chat_title(chat_id: str, title: str) -> None:
    """Unconditionally set chat title (used by model-driven title tag)."""
    with get_db() as conn:
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
    """Update chat timestamp and optionally advance the cached tail pointer."""
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


def get_chat_project_id(chat_id: str) -> str | None:
    """Return the project_id associated with a chat, or None."""
    with get_db() as conn:
        row = conn.execute("SELECT project_id FROM chats WHERE id = ?", (chat_id,)).fetchone()
        return row["project_id"] if row and row["project_id"] else None


def list_chats(project_id: str | None = None) -> list[dict[str, Any]]:
    with get_db() as conn:
        if project_id is not None:
            rows = conn.execute(
                "SELECT id, title, parent_id, created_at, updated_at, provider, project_id FROM chats WHERE project_id = ? ORDER BY updated_at DESC",
                (project_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, title, parent_id, created_at, updated_at, provider, project_id FROM chats ORDER BY updated_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]


def delete_chat(chat_id: str) -> bool:
    with get_db() as conn:
        conn.execute(
            "DELETE FROM skill_events WHERE message_id IN (SELECT id FROM messages WHERE chat_id = ?)",
            (chat_id,),
        )
        conn.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
        cur = conn.execute("DELETE FROM chats WHERE id = ?", (chat_id,))
        return cur.rowcount > 0


def delete_all_chats() -> int:
    """Delete ALL chats, messages, and skill_events. Returns number of chats removed."""
    with get_db() as conn:
        conn.execute("DELETE FROM skill_events")
        conn.execute("DELETE FROM messages")
        cur = conn.execute("DELETE FROM chats")
        return cur.rowcount


def get_chat_tail_id(chat_id: str) -> str | None:
    """Return the id of the latest message in a chat (server-side canonical tail)."""
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


# ── Messages ─────────────────────────────────────────────────────────────────

def _write_skill_events(conn: sqlite3.Connection, message_id: int, skill_events: list[dict[str, Any]] | None) -> None:
    """Write skill events to the dedicated skill_events table."""
    if not skill_events:
        return
    conn.execute("DELETE FROM skill_events WHERE message_id = ?", (message_id,))
    conn.executemany(
        "INSERT INTO skill_events (message_id, seq, event_data) VALUES (?, ?, ?)",
        [(message_id, i, json.dumps(ev, ensure_ascii=False)) for i, ev in enumerate(skill_events)],
    )


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
    memory_used_json = json.dumps(memory_used, ensure_ascii=False) if memory_used else None
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO messages (chat_id, role, content, thinking, memory_used, parent_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (chat_id, role, content, thinking, memory_used_json, parent_id, now),
        )
        msg_id = int(cur.lastrowid)
        _write_skill_events(conn, msg_id, skill_events)
        return msg_id


def update_message(
    message_id: int,
    content: str,
    thinking: str | None = None,
    parent_id: str | None = None,
    skill_events: list[dict[str, Any]] | None = None,
    memory_used: list[dict[str, Any]] | None = None,
) -> None:
    memory_used_json = json.dumps(memory_used, ensure_ascii=False) if memory_used else None
    with get_db() as conn:
        conn.execute(
            "UPDATE messages SET content = ?, thinking = ?, parent_id = ?, memory_used = ? WHERE id = ?",
            (content, thinking, parent_id, memory_used_json, message_id),
        )
        _write_skill_events(conn, message_id, skill_events)


def append_skill_event(chat_id: str, event: dict[str, Any]) -> None:
    """Append a single event to the last assistant message's skill_events table."""
    event_json = json.dumps(event, ensure_ascii=False)
    with get_db() as conn:
        row = conn.execute(
            "SELECT id FROM messages WHERE chat_id = ? AND role = 'assistant' ORDER BY id DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
        if not row:
            return
        msg_id = row["id"]
        seq_row = conn.execute(
            "SELECT COALESCE(MAX(seq), -1) + 1 AS next_seq FROM skill_events WHERE message_id = ?",
            (msg_id,),
        ).fetchone()
        conn.execute(
            "INSERT INTO skill_events (message_id, seq, event_data) VALUES (?, ?, ?)",
            (msg_id, seq_row["next_seq"], event_json),
        )


def get_messages(
    chat_id: str,
    limit: int | None = None,
    before_id: int | None = None,
    include_skill_events: bool = False,
) -> list[dict[str, Any]]:
    """Fetch messages for a chat with optional pagination."""
    query = "SELECT id, chat_id, role, content, thinking, memory_used, parent_id, created_at FROM messages WHERE chat_id = ?"
    params: list[Any] = [chat_id]

    if before_id is not None:
        query += " AND id < ?"
        params.append(before_id)

    query += " ORDER BY id DESC"

    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)

    with get_db() as conn:
        rows = conn.execute(query, params).fetchall()
        msg_ids = [row["id"] for row in rows]

        has_events_set: set[int] = set()
        if not include_skill_events and msg_ids:
            placeholders = ",".join("?" * len(msg_ids))
            ev_rows = conn.execute(
                f"SELECT DISTINCT message_id FROM skill_events WHERE message_id IN ({placeholders})",
                msg_ids,
            ).fetchall()
            has_events_set = {r["message_id"] for r in ev_rows}

        messages = []
        for row in reversed(rows):
            msg = dict(row)
            raw_mem = msg.get("memory_used")
            try:
                msg["memory_used"] = json.loads(raw_mem) if raw_mem else []
            except (json.JSONDecodeError, TypeError):
                msg["memory_used"] = []
            if include_skill_events:
                msg["skill_events"] = get_skill_events_for_message(msg["id"])
            else:
                msg["has_skill_events"] = msg["id"] in has_events_set
            messages.append(msg)
        return messages


def get_skill_events_for_message(message_id: int) -> list[dict[str, Any]]:
    """Load all skill events for a specific message (lazy-load endpoint)."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT event_data FROM skill_events WHERE message_id = ? ORDER BY seq ASC",
            (message_id,),
        ).fetchall()
        events = []
        for row in rows:
            try:
                events.append(json.loads(row["event_data"]))
            except (json.JSONDecodeError, TypeError):
                continue
        return events


def search_messages(query: str, limit: int = 50) -> list[dict[str, Any]]:
    """Search message content across all chats."""
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


# ── Projects ─────────────────────────────────────────────────────────────────

def list_projects() -> list[dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM projects ORDER BY name").fetchall()
        return [dict(row) for row in rows]


def create_project(project_id: str, name: str, path: str | None = None) -> dict[str, Any]:
    now = utcnow()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO projects (id, name, path, instruction_file, instruction_text,
               use_universal_memory, project_memory_enabled, persona_enabled,
               output_format_enabled, created_at, updated_at)
               VALUES (?, ?, ?, NULL, NULL, 1, 1, 1, 1, ?, ?)""",
            (project_id, name, path, now, now),
        )
    return get_project(project_id) or {}


def get_project(project_id: str) -> dict[str, Any] | None:
    import json as _json
    with get_db() as conn:
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        if d.get("skills_config") and isinstance(d["skills_config"], str):
            try:
                d["skills_config"] = _json.loads(d["skills_config"])
            except (ValueError, TypeError):
                d["skills_config"] = {}
        for bool_col in ("use_universal_memory", "project_memory_enabled", "persona_enabled", "output_format_enabled"):
            if bool_col in d:
                d[bool_col] = bool(d[bool_col])
        return d


def update_project(project_id: str, **kwargs: Any) -> dict[str, Any] | None:
    import json as _json
    if "skills_config" in kwargs and isinstance(kwargs["skills_config"], dict):
        kwargs["skills_config"] = _json.dumps(kwargs["skills_config"])
    allowed = {"name", "path", "instruction_file", "instruction_text", "use_universal_memory",
               "project_memory_enabled", "facts", "git_repo", "git_username", "git_branch",
               "persona_enabled", "output_format_enabled", "skills_config"}
    updates = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
    if not updates:
        return get_project(project_id)
    sets = ", ".join(f"{k} = ?" for k in updates)
    vals = list(updates.values()) + [utcnow(), project_id]
    with get_db() as conn:
        conn.execute(f"UPDATE projects SET {sets}, updated_at = ? WHERE id = ?", vals)
    return get_project(project_id)


def delete_project(project_id: str) -> bool:
    import shutil
    from pathlib import Path as _Path
    with get_db() as conn:
        chat_ids = [r["id"] for r in conn.execute(
            "SELECT id FROM chats WHERE project_id = ?", (project_id,)
        ).fetchall()]
        for cid in chat_ids:
            conn.execute(
                "DELETE FROM skill_events WHERE message_id IN (SELECT id FROM messages WHERE chat_id = ?)",
                (cid,),
            )
            conn.execute("DELETE FROM messages WHERE chat_id = ?", (cid,))
            conn.execute("DELETE FROM chats WHERE id = ?", (cid,))
        cur = conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    proj_dir = _Path(__file__).resolve().parent.parent.parent / "system" / "projects" / project_id
    if proj_dir.exists():
        shutil.rmtree(proj_dir, ignore_errors=True)
    try:
        from engine.memory_search import _project_searchers
        _project_searchers.pop(project_id, None)
    except Exception:
        pass
    return cur.rowcount > 0


# ── Checkpoints ──────────────────────────────────────────────────────────────

def save_checkpoint(chat_id: str, message_id: int, tool_name: str, commit_sha: str, project_root: str) -> int:
    """Save a checkpoint record. Returns the checkpoint id."""
    now = utcnow()
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO checkpoints (chat_id, message_id, tool_name, commit_sha, project_root, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (chat_id, message_id, tool_name, commit_sha, project_root, now),
        )
        return cur.lastrowid


def get_checkpoints_for_chat(chat_id: str) -> list[dict]:
    """Get all checkpoints for a chat, ordered by message_id."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM checkpoints WHERE chat_id = ? ORDER BY message_id ASC",
            (chat_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def get_checkpoint_by_sha(sha: str) -> dict | None:
    """Get a checkpoint by commit SHA. Supports prefix matching (e.g. 12-char short SHA)."""
    with get_db() as conn:
        if len(sha) < 40:
            row = conn.execute(
                "SELECT * FROM checkpoints WHERE commit_sha LIKE ? LIMIT 1",
                (f"{sha}%",),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM checkpoints WHERE commit_sha = ?", (sha,)
            ).fetchone()
        return dict(row) if row else None


def get_latest_checkpoint_for_message(chat_id: str, message_id: int) -> dict | None:
    """Get the latest checkpoint for a specific message (last tool call in that turn)."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM checkpoints WHERE chat_id = ? AND message_id <= ? ORDER BY id DESC LIMIT 1",
            (chat_id, message_id),
        ).fetchone()
        return dict(row) if row else None


def list_checkpoints_with_preview(chat_id: str | None = None, limit: int = 20) -> list[dict]:
    """List checkpoints joined with message preview text.

    Returns [{sha, timestamp, chat_id, tool_name, message_id, message_preview}].
    If chat_id is None, returns across all chats (most recent first).
    """
    sql = """
        SELECT c.commit_sha AS sha,
               c.created_at AS timestamp,
               c.chat_id,
               c.tool_name,
               c.message_id,
               SUBSTR(m.content, 1, 100) AS message_preview
        FROM checkpoints c
        LEFT JOIN messages m ON m.id = c.message_id AND m.chat_id = c.chat_id
    """
    params: tuple = ()
    if chat_id:
        sql += " WHERE c.chat_id = ?"
        params = (chat_id,)
    sql += " ORDER BY c.id DESC LIMIT ?"
    params = (*params, limit)
    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

