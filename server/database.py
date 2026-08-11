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
        _conn.execute("PRAGMA synchronous=NORMAL")
        _conn.execute("PRAGMA cache_size=-64000")       # 64MB page cache
        _conn.execute("PRAGMA mmap_size=268435456")      # 256MB mmap
        _conn.execute("PRAGMA temp_store=MEMORY")
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
        # --- Messages indexes (critical for performance) ---
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_chat_role ON messages(chat_id, role, id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chats_updated ON chats(updated_at DESC)"
        )

        # --- Skill events table (separated from messages for perf + ordering) ---
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS skill_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL,
                seq INTEGER NOT NULL DEFAULT 0,
                event_data TEXT NOT NULL,
                FOREIGN KEY(message_id) REFERENCES messages(id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_skill_events_msg ON skill_events(message_id, seq)"
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

        # --- TrackNote tables ---
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS notes (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL DEFAULT '',
                note_type TEXT NOT NULL DEFAULT 'note',
                items TEXT DEFAULT '[]',
                due_date TEXT,
                color TEXT,
                label TEXT,
                pinned INTEGER NOT NULL DEFAULT 0,
                archived INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_notes_type ON notes(note_type)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_notes_archived ON notes(archived)"
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schedules (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                schedule_type TEXT NOT NULL DEFAULT 'daily',
                time TEXT,
                day_of_week INTEGER,
                start_date TEXT,
                end_date TEXT,
                description TEXT DEFAULT '',
                completed INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_schedules_type ON schedules(schedule_type)"
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_ops (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                prompt TEXT NOT NULL,
                model TEXT NOT NULL DEFAULT 'qwen3.7-max',
                schedule_type TEXT NOT NULL DEFAULT 'daily',
                schedule_time TEXT,
                schedule_day INTEGER,
                cron_expression TEXT,
                last_run TEXT,
                next_run TEXT,
                last_result TEXT,
                missed_run_policy TEXT NOT NULL DEFAULT 'catch_up',
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_ops_enabled ON agent_ops(enabled)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_ops_next_run ON agent_ops(next_run)"
        )

        # --- Checkpoint table (shadow-git restore points) ---
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS checkpoints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                tool_name TEXT NOT NULL DEFAULT '',
                commit_sha TEXT NOT NULL,
                project_root TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(chat_id) REFERENCES chats(id),
                FOREIGN KEY(message_id) REFERENCES messages(id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_checkpoints_chat ON checkpoints(chat_id, message_id)"
        )

        # --- Projects table ---
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                path TEXT,
                instruction_file TEXT,
                instruction_text TEXT,
                use_universal_memory INTEGER NOT NULL DEFAULT 1,
                project_memory_enabled INTEGER NOT NULL DEFAULT 1,
                facts TEXT,
                git_repo TEXT,
                git_username TEXT,
                git_branch TEXT,
                persona_enabled INTEGER NOT NULL DEFAULT 1,
                output_format_enabled INTEGER NOT NULL DEFAULT 1,
                skills_config TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        # Migrate existing projects table — add new columns if missing
        proj_cols = {row["name"] for row in conn.execute("PRAGMA table_info(projects)").fetchall()}
        new_proj_cols = {
            "instruction_text": "TEXT",
            "project_memory_enabled": "INTEGER NOT NULL DEFAULT 1",
            "facts": "TEXT",
            "git_repo": "TEXT",
            "git_username": "TEXT",
            "git_branch": "TEXT",
            "persona_enabled": "INTEGER NOT NULL DEFAULT 1",
            "output_format_enabled": "INTEGER NOT NULL DEFAULT 1",
            "skills_config": "TEXT",
        }
        for col_name, col_def in new_proj_cols.items():
            if col_name not in proj_cols:
                conn.execute(f"ALTER TABLE projects ADD COLUMN {col_name} {col_def}")
        if "project_id" not in chat_cols:
            conn.execute("ALTER TABLE chats ADD COLUMN project_id TEXT")

def ensure_chat(chat_id: str, title: str = "New chat", parent_id: str | None = None, mode: str | None = None, provider: str | None = None, project_id: str | None = None) -> None:
    import logging as _logging
    _db_log = _logging.getLogger(__name__)
    now = utcnow()
    with get_db() as conn:
        existing = conn.execute("SELECT id, mode, provider, project_id FROM chats WHERE id = ?", (chat_id,)).fetchone()
        if existing:
            _db_log.info("[DB-ENSURE-CHAT] EXISTS chat=%s mode=%s provider=%s", chat_id, existing["mode"], existing["provider"])
            if mode and not existing["mode"]:
                conn.execute("UPDATE chats SET mode = ? WHERE id = ?", (mode, chat_id))
            if provider and existing["provider"] != provider:
                conn.execute("UPDATE chats SET provider = ? WHERE id = ?", (provider, chat_id))
            if project_id is not None and existing["project_id"] != project_id:
                conn.execute("UPDATE chats SET project_id = ? WHERE id = ?", (project_id, chat_id))
            return
        conn.execute(
            "INSERT INTO chats (id, title, parent_id, created_at, updated_at, mode, provider, project_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (chat_id, title, parent_id, now, now, mode, provider, project_id),
        )
        _db_log.info("[DB-ENSURE-CHAT] CREATED chat=%s title=%.40s parent=%s mode=%s provider=%s", chat_id, title, parent_id, mode, provider)

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
    import logging as _logging
    _db_log = _logging.getLogger(__name__)
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
            _db_log.info("[DB-TOUCH] chat=%s parent=%s", chat_id, parent_id)
        else:
            conn.execute("UPDATE chats SET updated_at = ? WHERE id = ?", (now, chat_id))
            _db_log.info("[DB-TOUCH] chat=%s parent=None (timestamp only)", chat_id)

def save_chat_url(chat_id: str, url: str) -> None:
    with get_db() as conn:
        conn.execute("UPDATE chats SET chat_url = ? WHERE id = ?", (url, chat_id))

def get_chat_url(chat_id: str) -> str | None:
    with get_db() as conn:
        row = conn.execute("SELECT chat_url FROM chats WHERE id = ?", (chat_id,)).fetchone()
        return row["chat_url"] if row and row["chat_url"] else None

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
    import logging as _logging
    _db_log = _logging.getLogger(__name__)
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
        _db_log.info("[DB-ADD-MSG] id=%d chat=%s role=%s parent=%s len=%d", msg_id, chat_id, role, parent_id, len(content or ""))
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
        # Get next seq number
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
    """Fetch messages for a chat with optional pagination.

    Args:
        chat_id: The chat to load messages for.
        limit: Max number of messages to return (None = all).
        before_id: If set, return messages with id < before_id (for loading older messages).
        include_skill_events: If True, include skill_events from the separate table.
    """
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
        # Reverse to get ascending order (we queried DESC for pagination)
        msg_ids = [row["id"] for row in rows]

        # Batch-check which messages have skill events (single query)
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
        # Deserialize skills_config JSON string back to dict
        if d.get("skills_config") and isinstance(d["skills_config"], str):
            try:
                d["skills_config"] = _json.loads(d["skills_config"])
            except (ValueError, TypeError):
                d["skills_config"] = {}
        # Convert integer booleans to actual bools
        for bool_col in ("use_universal_memory", "project_memory_enabled", "persona_enabled", "output_format_enabled"):
            if bool_col in d:
                d[bool_col] = bool(d[bool_col])
        return d

def update_project(project_id: str, **kwargs: Any) -> dict[str, Any] | None:
    import json as _json
    # Serialize skills_config dict to JSON string for storage
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
        # Delete all chats belonging to this project
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
        # Delete the project record
        cur = conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    # Clean up project folder on disk (memory, protected, cache, instruction)
    proj_dir = _Path(__file__).resolve().parent.parent / "system" / "projects" / project_id
    if proj_dir.exists():
        shutil.rmtree(proj_dir, ignore_errors=True)
    # Evict cached project searcher so it doesn't hold stale references
    try:
        from engine.memory_search import _project_searchers
        _project_searchers.pop(project_id, None)
    except Exception:
        pass
    return cur.rowcount > 0

def delete_chat(chat_id: str) -> bool:
    with get_db() as conn:
        # Clean up skill_events for messages in this chat
        conn.execute(
            "DELETE FROM skill_events WHERE message_id IN (SELECT id FROM messages WHERE chat_id = ?)",
            (chat_id,),
        )
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
    import logging as _logging
    _db_log = _logging.getLogger(__name__)
    if requested_parent_id:
        _db_log.info("[DB-GET-PARENT] chat=%s using requested=%s", chat_id, requested_parent_id)
        return requested_parent_id
    with get_db() as conn:
        row = conn.execute("SELECT parent_id FROM chats WHERE id = ?", (chat_id,)).fetchone()
        _result = row["parent_id"] if row else None
        _db_log.info("[DB-GET-PARENT] chat=%s db_parent=%s chat_exists=%s", chat_id, _result, bool(row))
        return _result


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


# ── TrackNote: Notes/Todos CRUD ─────────────────────────────────────────────

def list_notes(note_type: str | None = None, archived: bool = False) -> list[dict[str, Any]]:
    with get_db() as conn:
        if note_type:
            rows = conn.execute(
                "SELECT * FROM notes WHERE note_type = ? AND archived = ? ORDER BY pinned DESC, updated_at DESC",
                (note_type, int(archived)),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM notes WHERE archived = ? ORDER BY pinned DESC, updated_at DESC",
                (int(archived),),
            ).fetchall()
        results = []
        for row in rows:
            d = dict(row)
            try:
                d["items"] = json.loads(d.get("items") or "[]")
            except (json.JSONDecodeError, TypeError):
                d["items"] = []
            results.append(d)
        return results


def get_note(note_id: str) -> dict[str, Any] | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["items"] = json.loads(d.get("items") or "[]")
    except (json.JSONDecodeError, TypeError):
        d["items"] = []
    return d


def create_note(title: str = "", content: str = "", note_type: str = "note",
                items: list | None = None, due_date: str | None = None,
                color: str | None = None, label: str | None = None) -> str:
    import uuid
    now = utcnow()
    note_id = uuid.uuid4().hex[:12]
    items_json = json.dumps(items or [], ensure_ascii=False)
    with get_db() as conn:
        conn.execute(
            "INSERT INTO notes (id, title, content, note_type, items, due_date, color, label, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (note_id, title, content, note_type, items_json, due_date, color, label, now, now),
        )
    return note_id


def update_note(note_id: str, **kwargs) -> bool:
    allowed = {"title", "content", "note_type", "due_date", "color", "label", "pinned", "archived"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if "items" in kwargs:
        fields["items"] = json.dumps(kwargs["items"], ensure_ascii=False)
    if not fields:
        return False
    fields["updated_at"] = utcnow()
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [note_id]
    with get_db() as conn:
        cur = conn.execute(f"UPDATE notes SET {set_clause} WHERE id = ?", values)
        return cur.rowcount > 0


def delete_note(note_id: str) -> bool:
    with get_db() as conn:
        cur = conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        return cur.rowcount > 0


def toggle_note_item(note_id: str, item_index: int) -> bool:
    """Toggle done state of a checklist item by index."""
    note = get_note(note_id)
    if not note or not note.get("items"):
        return False
    items = note["items"]
    if item_index < 0 or item_index >= len(items):
        return False
    items[item_index]["done"] = not items[item_index].get("done", False)
    return update_note(note_id, items=items)


# ── TrackNote: Schedules CRUD ───────────────────────────────────────────────

def list_schedules() -> list[dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM schedules ORDER BY "
            "CASE schedule_type WHEN 'daily' THEN 0 WHEN 'weekly' THEN 1 WHEN 'occasional' THEN 2 END, "
            "time ASC, start_date ASC"
        ).fetchall()
        return [dict(row) for row in rows]


def get_upcoming_schedules(days: int = 10) -> list[dict[str, Any]]:
    """Return schedules relevant for the next N days (for startup injection)."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    cutoff = (now + timedelta(days=days)).isoformat()
    now_iso = now.isoformat()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM schedules WHERE completed = 0 AND ("
            "  schedule_type IN ('daily', 'weekly')"
            "  OR (schedule_type = 'occasional' AND start_date <= ? AND (end_date IS NULL OR end_date >= ?))"
            ") ORDER BY start_date ASC, time ASC",
            (cutoff, now_iso),
        ).fetchall()
        return [dict(row) for row in rows]


def create_schedule(title: str, schedule_type: str = "daily", time: str | None = None,
                    day_of_week: int | None = None, start_date: str | None = None,
                    end_date: str | None = None, description: str = "") -> str:
    import uuid
    now = utcnow()
    sched_id = uuid.uuid4().hex[:12]
    with get_db() as conn:
        conn.execute(
            "INSERT INTO schedules (id, title, schedule_type, time, day_of_week, start_date, end_date, description, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (sched_id, title, schedule_type, time, day_of_week, start_date, end_date, description, now, now),
        )
    return sched_id


def update_schedule(sched_id: str, **kwargs) -> bool:
    allowed = {"title", "schedule_type", "time", "day_of_week", "start_date", "end_date", "description", "completed"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return False
    fields["updated_at"] = utcnow()
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [sched_id]
    with get_db() as conn:
        cur = conn.execute(f"UPDATE schedules SET {set_clause} WHERE id = ?", values)
        return cur.rowcount > 0


def delete_schedule(sched_id: str) -> bool:
    with get_db() as conn:
        cur = conn.execute("DELETE FROM schedules WHERE id = ?", (sched_id,))
        return cur.rowcount > 0


# ── TrackNote: Agent Ops CRUD ───────────────────────────────────────────────

def list_agent_ops() -> list[dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM agent_ops ORDER BY enabled DESC, created_at DESC"
        ).fetchall()
        return [dict(row) for row in rows]


def get_agent_op(op_id: str) -> dict[str, Any] | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM agent_ops WHERE id = ?", (op_id,)).fetchone()
    return dict(row) if row else None


def create_agent_op(name: str, prompt: str, model: str = "qwen3.7-max",
                    schedule_type: str = "daily", schedule_time: str | None = None,
                    schedule_day: int | None = None, cron_expression: str | None = None,
                    missed_run_policy: str = "catch_up") -> str:
    import uuid
    now = utcnow()
    op_id = uuid.uuid4().hex[:12]
    with get_db() as conn:
        conn.execute(
            "INSERT INTO agent_ops (id, name, prompt, model, schedule_type, schedule_time, "
            "schedule_day, cron_expression, missed_run_policy, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (op_id, name, prompt, model, schedule_type, schedule_time,
             schedule_day, cron_expression, missed_run_policy, now, now),
        )
    return op_id


def update_agent_op(op_id: str, **kwargs) -> bool:
    allowed = {"name", "prompt", "model", "schedule_type", "schedule_time",
               "schedule_day", "cron_expression", "missed_run_policy",
               "enabled", "last_run", "next_run", "last_result"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return False
    fields["updated_at"] = utcnow()
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [op_id]
    with get_db() as conn:
        cur = conn.execute(f"UPDATE agent_ops SET {set_clause} WHERE id = ?", values)
        return cur.rowcount > 0


def delete_agent_op(op_id: str) -> bool:
    with get_db() as conn:
        cur = conn.execute("DELETE FROM agent_ops WHERE id = ?", (op_id,))
        return cur.rowcount > 0


def get_due_agent_ops() -> list[dict[str, Any]]:
    """Return enabled agent ops whose next_run is past and need firing."""
    now = utcnow()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM agent_ops WHERE enabled = 1 AND next_run IS NOT NULL AND next_run <= ? "
            "ORDER BY next_run ASC",
            (now,),
        ).fetchall()
        return [dict(row) for row in rows]


def migrate_skill_events_to_table() -> int:
    """One-time migration: move skill_events from messages.skill_events column to skill_events table.

    Returns the number of messages migrated. Safe to call multiple times (skips already-migrated).
    """
    migrated = 0
    with get_db() as conn:
        # Check if migration is needed (messages still have skill_events data)
        rows = conn.execute(
            "SELECT id, skill_events FROM messages WHERE skill_events IS NOT NULL AND skill_events != '' AND skill_events != '[]'"
        ).fetchall()
        for row in rows:
            msg_id = row["id"]
            # Skip if already migrated to the table
            existing = conn.execute(
                "SELECT 1 FROM skill_events WHERE message_id = ? LIMIT 1", (msg_id,)
            ).fetchone()
            if existing:
                continue
            raw = row["skill_events"]
            try:
                events = json.loads(raw)
                if not isinstance(events, list) or not events:
                    continue
            except (json.JSONDecodeError, TypeError):
                continue
            conn.executemany(
                "INSERT INTO skill_events (message_id, seq, event_data) VALUES (?, ?, ?)",
                [(msg_id, i, json.dumps(ev, ensure_ascii=False)) for i, ev in enumerate(events)],
            )
            migrated += 1
        # Clear the old column to reclaim space (after VACUUM)
        if migrated > 0:
            conn.execute("UPDATE messages SET skill_events = NULL")
    return migrated


# --- Checkpoint helpers ---

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
    """Get a checkpoint by commit SHA."""
    with get_db() as conn:
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