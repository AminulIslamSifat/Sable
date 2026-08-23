
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from server.database import (
    list_notes, get_note, create_note, update_note, delete_note, toggle_note_item,
    list_schedules, get_upcoming_schedules, create_schedule, update_schedule, delete_schedule,
    list_agent_ops, get_agent_op, create_agent_op, update_agent_op, delete_agent_op,
)

logger = logging.getLogger("sable.tracknote")
router = APIRouter()


# ── Request Models ──────────────────────────────────────────────────────────

class NoteCreate(BaseModel):
    title: str = ""
    content: str = ""
    note_type: str = "note"
    items: list[dict[str, Any]] | None = None
    due_date: str | None = None
    color: str | None = None
    label: str | None = None

class NoteUpdate(BaseModel):
    title: str | None = None
    content: str | None = None
    note_type: str | None = None
    items: list[dict[str, Any]] | None = None
    due_date: str | None = None
    color: str | None = None
    label: str | None = None
    pinned: int | None = None
    archived: int | None = None

class ScheduleCreate(BaseModel):
    title: str
    schedule_type: str = "daily"
    time: str | None = None
    day_of_week: int | None = None
    start_date: str | None = None
    end_date: str | None = None
    description: str = ""

class ScheduleUpdate(BaseModel):
    title: str | None = None
    schedule_type: str | None = None
    time: str | None = None
    day_of_week: int | None = None
    start_date: str | None = None
    end_date: str | None = None
    description: str | None = None
    completed: int | None = None

class AgentOpCreate(BaseModel):
    name: str
    prompt: str
    model: str = "qwen3.7-max"
    schedule_type: str = "daily"
    schedule_time: str | None = None
    schedule_day: int | None = None
    cron_expression: str | None = None
    missed_run_policy: str = "catch_up"

class AgentOpUpdate(BaseModel):
    name: str | None = None
    prompt: str | None = None
    model: str | None = None
    schedule_type: str | None = None
    schedule_time: str | None = None
    schedule_day: int | None = None
    cron_expression: str | None = None
    missed_run_policy: str | None = None
    enabled: int | None = None


# ── Notes/Todos ─────────────────────────────────────────────────────────────

@router.get("/api/notes")
def get_notes(note_type: str | None = None, archived: bool = False) -> dict[str, Any]:
    return {"notes": list_notes(note_type=note_type, archived=archived)}


@router.get("/api/notes/{note_id}")
def get_note_route(note_id: str) -> dict[str, Any]:
    note = get_note(note_id)
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    return note


@router.post("/api/notes")
def create_note_route(body: NoteCreate) -> dict[str, str]:
    note_id = create_note(
        title=body.title, content=body.content, note_type=body.note_type,
        items=body.items, due_date=body.due_date, color=body.color, label=body.label,
    )
    return {"id": note_id}


@router.put("/api/notes/{note_id}")
def update_note_route(note_id: str, body: NoteUpdate) -> dict[str, Any]:
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        return {"updated": False}
    ok = update_note(note_id, **updates)
    if not ok:
        raise HTTPException(status_code=404, detail="Note not found")
    return {"updated": True}


@router.delete("/api/notes/{note_id}")
def delete_note_route(note_id: str) -> dict[str, Any]:
    ok = delete_note(note_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Note not found")
    return {"deleted": True}


@router.post("/api/notes/{note_id}/toggle-item")
def toggle_item_route(note_id: str, index: int = 0) -> dict[str, Any]:
    ok = toggle_note_item(note_id, index)
    return {"toggled": ok}


# ── Schedules ───────────────────────────────────────────────────────────────

@router.get("/api/schedules")
def get_schedules() -> dict[str, Any]:
    return {"schedules": list_schedules()}


@router.get("/api/schedules/upcoming")
def upcoming_schedules(days: int = 10) -> dict[str, Any]:
    return {"schedules": get_upcoming_schedules(days=days)}


@router.post("/api/schedules")
def create_schedule_route(body: ScheduleCreate) -> dict[str, str]:
    sched_id = create_schedule(
        title=body.title, schedule_type=body.schedule_type, time=body.time,
        day_of_week=body.day_of_week, start_date=body.start_date,
        end_date=body.end_date, description=body.description,
    )
    return {"id": sched_id}


@router.put("/api/schedules/{sched_id}")
def update_schedule_route(sched_id: str, body: ScheduleUpdate) -> dict[str, Any]:
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        return {"updated": False}
    ok = update_schedule(sched_id, **updates)
    if not ok:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return {"updated": True}


@router.delete("/api/schedules/{sched_id}")
def delete_schedule_route(sched_id: str) -> dict[str, Any]:
    ok = delete_schedule(sched_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return {"deleted": True}


# ── Calendar Events (computed) ─────────────────────────────────────────────

@router.get("/api/calendar/events")
def get_calendar_events(year: int | None = None, month: int | None = None) -> dict[str, Any]:
    """Return events for a given month, expanding schedules into per-day occurrences.

    Response: { "events": { "YYYY-MM-DD": [ { id, title, time, type, description, schedule_type }, ... ] } }
    """
    from datetime import date, timedelta
    import calendar as cal_mod

    today = date.today()
    y = year or today.year
    m = month or today.month

    # Clamp
    if m < 1 or m > 12:
        m = today.month
    if y < 2000 or y > 2100:
        y = today.year

    first_day = date(y, m, 1)
    last_day_num = cal_mod.monthrange(y, m)[1]
    last_day = date(y, m, last_day_num)

    events: dict[str, list[dict[str, Any]]] = {}

    def add_event(d: date, evt: dict[str, Any]) -> None:
        key = d.isoformat()
        events.setdefault(key, []).append(evt)

    # 1. Schedules
    schedules = list_schedules()
    current = first_day
    while current <= last_day:
        for s in schedules:
            if s.get("completed"):
                continue
            stype = s.get("schedule_type", "daily")
            match = False
            if stype == "daily":
                # Check start/end bounds
                if s.get("start_date"):
                    sd = date.fromisoformat(s["start_date"][:10])
                    if current < sd:
                        continue
                if s.get("end_date"):
                    ed = date.fromisoformat(s["end_date"][:10])
                    if current > ed:
                        continue
                match = True
            elif stype == "weekly":
                dow = s.get("day_of_week")  # 0=Mon .. 6=Sun
                if dow is not None and current.weekday() == int(dow):
                    if s.get("start_date"):
                        sd = date.fromisoformat(s["start_date"][:10])
                        if current < sd:
                            continue
                    if s.get("end_date"):
                        ed = date.fromisoformat(s["end_date"][:10])
                        if current > ed:
                            continue
                    match = True
            elif stype == "occasional":
                if s.get("start_date"):
                    sd = date.fromisoformat(s["start_date"][:10])
                    if sd == current:
                        match = True

            if match:
                add_event(current, {
                    "id": s["id"],
                    "title": s.get("title", ""),
                    "time": s.get("time"),
                    "type": "schedule",
                    "description": s.get("description", ""),
                    "schedule_type": stype,
                })
        current += timedelta(days=1)

    # 2. Notes with due_date
    notes = list_notes()
    for n in notes:
        dd = n.get("due_date")
        if not dd:
            continue
        try:
            nd = date.fromisoformat(dd[:10])
        except (ValueError, TypeError):
            continue
        if first_day <= nd <= last_day:
            add_event(nd, {
                "id": n["id"],
                "title": n.get("title", "Untitled"),
                "time": None,
                "type": "note",
                "description": (n.get("content") or "")[:120],
                "schedule_type": None,
                "note_type": n.get("note_type", "note"),
            })

    # 3. Agent ops (enabled ones with next_run in this month)
    ops = list_agent_ops()
    for op in ops:
        nr = op.get("next_run")
        if not nr or not op.get("enabled"):
            continue
        try:
            nr_date = date.fromisoformat(nr[:10])
        except (ValueError, TypeError):
            continue
        if first_day <= nr_date <= last_day:
            add_event(nr_date, {
                "id": op["id"],
                "title": f"🤖 {op.get('name', 'Agent Op')}",
                "time": op.get("schedule_time"),
                "type": "agent_op",
                "description": (op.get("prompt") or "")[:120],
                "schedule_type": op.get("schedule_type", "daily"),
            })

    return {"events": events, "year": y, "month": m}


# ── Agent Ops ───────────────────────────────────────────────────────────────

@router.get("/api/agent-ops")
def get_agent_ops() -> dict[str, Any]:
    return {"ops": list_agent_ops()}


@router.get("/api/agent-ops/{op_id}")
def get_agent_op_route(op_id: str) -> dict[str, Any]:
    op = get_agent_op(op_id)
    if not op:
        raise HTTPException(status_code=404, detail="Agent op not found")
    return op


@router.post("/api/agent-ops")
def create_agent_op_route(body: AgentOpCreate) -> dict[str, str]:
    from server.scheduler import compute_next_run, schedule_op
    from datetime import timezone, datetime

    # Compute initial next_run before inserting
    nr = compute_next_run(body.schedule_type, body.schedule_time, body.schedule_day, body.cron_expression)
    nr_iso = nr.isoformat() if nr else None

    op_id = create_agent_op(
        name=body.name, prompt=body.prompt, model=body.model,
        schedule_type=body.schedule_type, schedule_time=body.schedule_time,
        schedule_day=body.schedule_day, cron_expression=body.cron_expression,
        missed_run_policy=body.missed_run_policy,
    )
    # Set next_run and enable by default
    update_agent_op(op_id, next_run=nr_iso, enabled=1)
    schedule_op(op_id)
    return {"id": op_id}


@router.put("/api/agent-ops/{op_id}")
def update_agent_op_route(op_id: str, body: AgentOpUpdate) -> dict[str, Any]:
    from server.scheduler import compute_next_run, schedule_op, cancel_op

    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        return {"updated": False}

    # Recompute next_run if schedule params changed
    sched_changed = any(k in updates for k in ("schedule_type", "schedule_time", "schedule_day", "cron_expression"))
    if sched_changed:
        op = get_agent_op(op_id)
        if op:
            merged = {**op, **updates}
            nr = compute_next_run(
                merged.get("schedule_type", "daily"),
                merged.get("schedule_time"),
                merged.get("schedule_day"),
                merged.get("cron_expression"),
            )
            if nr:
                updates["next_run"] = nr.isoformat()

    ok = update_agent_op(op_id, **updates)
    if not ok:
        raise HTTPException(status_code=404, detail="Agent op not found")

    # Reschedule: cancel old task, start new one if enabled
    cancel_op(op_id)
    final = get_agent_op(op_id)
    if final and final.get("enabled"):
        schedule_op(op_id)

    return {"updated": True}


@router.delete("/api/agent-ops/{op_id}")
def delete_agent_op_route(op_id: str) -> dict[str, Any]:
    from server.scheduler import cancel_op
    cancel_op(op_id)
    ok = delete_agent_op(op_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Agent op not found")
    return {"deleted": True}
