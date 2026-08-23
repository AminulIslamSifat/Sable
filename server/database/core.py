"""Database connection management, schema initialization, and migrations."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..config import DB_PATH
from ..utils import utcnow

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
        if "upstream_session_id" not in chat_cols:
            conn.execute("ALTER TABLE chats ADD COLUMN upstream_session_id TEXT")
        if "fork_history" not in chat_cols:
            conn.execute("ALTER TABLE chats ADD COLUMN fork_history TEXT")

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


def migrate_skill_events_to_table() -> int:
    """One-time migration: move skill_events from messages.skill_events column to skill_events table.

    Returns the number of messages migrated. Safe to call multiple times (skips already-migrated).
    """
    migrated = 0
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, skill_events FROM messages WHERE skill_events IS NOT NULL AND skill_events != '' AND skill_events != '[]'"
        ).fetchall()
        for row in rows:
            msg_id = row["id"]
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
        if migrated > 0:
            conn.execute("UPDATE messages SET skill_events = NULL")
    return migrated
