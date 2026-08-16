"""Agent runs, agent messages, and agent ops persistence."""

from __future__ import annotations

import uuid
from typing import Any

from ..utils import utcnow
from .core import get_db


# ── Agent Runs ───────────────────────────────────────────────────────────────

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


# ── Agent Ops (scheduled autonomous operations) ──────────────────────────────

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
