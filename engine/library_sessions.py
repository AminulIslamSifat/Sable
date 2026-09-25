"""Study-session analytics -- dwell time, streaks, and per-document totals.

The reader reports a session when a document closes (or the tab is hidden for
an idle beat): how long the user actually had it open, and which pages they
moved through. Those sessions land here so the dashboard can show real study
time instead of a localStorage counter that dies with the browser cache.

Storage is one JSON file (``engine.config.LIBRARY_SESSIONS_PATH``) guarded by
an exclusive flock and written through ``.tmp`` -> ``replace()``, matching the
meta/SRS stores. A rolling retention window keeps the file bounded.
"""
from __future__ import annotations

import fcntl
import json
import logging
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from engine.config import LIBRARY_SESSIONS_PATH

logger = logging.getLogger("sable.study.sessions")

_VERSION = 1
_MAX_SESSIONS = 20_000
_RETENTION_DAYS = 400
_MIN_SECONDS = 5          # ignore accidental open/close blips
_MAX_SECONDS = 6 * 3600   # a session longer than this is a forgotten tab
_DAY = 86_400.0


def _lock_path() -> Path:
    return LIBRARY_SESSIONS_PATH.with_suffix(".lock")


@contextmanager
def _lock() -> Iterator[None]:
    LIBRARY_SESSIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    fh = open(_lock_path(), "w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


def _empty() -> dict[str, Any]:
    return {"version": _VERSION, "sessions": []}


def _read() -> dict[str, Any]:
    path = LIBRARY_SESSIONS_PATH
    if not path.exists():
        return _empty()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("sessions store unreadable - starting fresh")
        return _empty()
    if not isinstance(data, dict) or not isinstance(data.get("sessions"), list):
        return _empty()
    data.setdefault("version", _VERSION)
    return data


def _write(data: dict[str, Any]) -> None:
    LIBRARY_SESSIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    data["version"] = _VERSION
    target = LIBRARY_SESSIONS_PATH
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(target)


def _prune(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cutoff = time.time() - _RETENTION_DAYS * _DAY
    kept = [s for s in sessions if float(s.get("ended") or 0) >= cutoff]
    if len(kept) > _MAX_SESSIONS:
        kept = kept[-_MAX_SESSIONS:]
    return kept


# ── Recording ───────────────────────────────────────────────────────────────


def record(
    doc_id: str,
    seconds: float,
    *,
    started: float | None = None,
    ended: float | None = None,
    pages: list[int] | None = None,
) -> dict[str, Any] | None:
    """Persist one reading session. Returns the stored record, or None if ignored.

    Sub-5-second sessions are dropped: they are almost always an accidental
    open, and letting them through would drown the analytics in noise.
    """
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        return None
    if seconds < _MIN_SECONDS:
        return None
    seconds = min(seconds, _MAX_SECONDS)

    now = time.time()
    clean_pages = sorted({int(p) for p in (pages or []) if isinstance(p, (int, float)) and p >= 1})
    entry = {
        "id": uuid.uuid4().hex[:16],
        "doc_id": str(doc_id or "")[:64],
        "started": float(started or (now - seconds)),
        "ended": float(ended or now),
        "seconds": round(seconds, 2),
        "pages": clean_pages[:400],
    }

    with _lock():
        data = _read()
        data["sessions"].append(entry)
        data["sessions"] = _prune(data["sessions"])
        _write(data)
    return entry


# ── Reporting ───────────────────────────────────────────────────────────────


def _series(sessions: list[dict[str, Any]], days: int) -> list[dict[str, Any]]:
    """Per-day totals for the last ``days`` days, oldest first, gaps filled."""
    today = time.localtime()
    midnight = time.mktime(
        (today.tm_year, today.tm_mon, today.tm_mday, 0, 0, 0, 0, 0, -1)
    )
    buckets: dict[str, float] = {}
    for offset in range(days):
        day = time.strftime("%Y-%m-%d", time.localtime(midnight - offset * _DAY))
        buckets[day] = 0.0
    for session in sessions:
        day = time.strftime("%Y-%m-%d", time.localtime(float(session.get("ended") or 0)))
        if day in buckets:
            buckets[day] += float(session.get("seconds") or 0)
    return [
        {"date": day, "seconds": round(secs, 1), "minutes": round(secs / 60, 1)}
        for day, secs in sorted(buckets.items())
    ]


def _streak(series: list[dict[str, Any]]) -> int:
    """Consecutive days ending today (or yesterday) with at least a minute."""
    streak = 0
    for point in reversed(series):
        if point["seconds"] >= 60:
            streak += 1
        elif streak == 0 and point is series[-1]:
            continue  # today not started yet - keep looking back
        else:
            break
    return streak


def stats(days: int = 30) -> dict[str, Any]:
    """Aggregate study time over a rolling window."""
    days = max(1, min(int(days or 30), 365))
    data = _read()
    cutoff = time.time() - days * _DAY
    recent = [s for s in data["sessions"] if float(s.get("ended") or 0) >= cutoff]

    total_seconds = sum(float(s.get("seconds") or 0) for s in recent)
    per_doc: dict[str, dict[str, Any]] = {}
    for session in recent:
        doc_id = str(session.get("doc_id") or "")
        if not doc_id:
            continue
        bucket = per_doc.setdefault(doc_id, {"doc_id": doc_id, "seconds": 0.0, "sessions": 0})
        bucket["seconds"] += float(session.get("seconds") or 0)
        bucket["sessions"] += 1

    top_docs = sorted(per_doc.values(), key=lambda d: -d["seconds"])[:10]
    for doc in top_docs:
        doc["minutes"] = round(doc["seconds"] / 60, 1)
        doc["seconds"] = round(doc["seconds"], 1)

    series = _series(data["sessions"], days)
    return {
        "days": days,
        "total_seconds": round(total_seconds, 1),
        "total_minutes": round(total_seconds / 60, 1),
        "total_hours": round(total_seconds / 3600, 2),
        "sessions": len(recent),
        "active_days": sum(1 for p in series if p["seconds"] >= 60),
        "streak": _streak(series),
        "today_seconds": round(series[-1]["seconds"], 1) if series else 0.0,
        "series": series,
        "top_docs": top_docs,
    }


def doc_stats(doc_id: str) -> dict[str, Any]:
    """Per-document totals: how long, across how many sessions, over what span."""
    if not doc_id:
        return {"doc_id": "", "seconds": 0.0, "minutes": 0.0, "sessions": 0}
    data = _read()
    mine = [s for s in data["sessions"] if str(s.get("doc_id") or "") == doc_id]
    total = sum(float(s.get("seconds") or 0) for s in mine)
    return {
        "doc_id": doc_id,
        "seconds": round(total, 1),
        "minutes": round(total / 60, 1),
        "sessions": len(mine),
        "last_read": max((float(s.get("ended") or 0) for s in mine), default=0.0),
        "pages_touched": len({p for s in mine for p in (s.get("pages") or [])}),
    }


def clear(doc_id: str = "") -> int:
    """Delete sessions for one document, or all of them when doc_id is empty."""
    with _lock():
        data = _read()
        before = len(data["sessions"])
        if doc_id:
            data["sessions"] = [s for s in data["sessions"] if s.get("doc_id") != doc_id]
        else:
            data["sessions"] = []
        _write(data)
    return before - len(data["sessions"])
