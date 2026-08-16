"""Notes, todos, and schedules CRUD."""

from __future__ import annotations

import json
import uuid
from typing import Any

from ..utils import utcnow
from .core import get_db


# ── Notes / Todos ────────────────────────────────────────────────────────────

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


# ── Schedules ────────────────────────────────────────────────────────────────

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
