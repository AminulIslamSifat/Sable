"""DiagnosticsMonitor — passive session health observer.

Tracks active browser scraper sessions, connection heartbeats,
and provides a query interface for session liveness. Designed to
run silently alongside normal scraper operations.

All data is ephemeral (in-memory only) and cleared on process exit.
No persistent storage, no external network calls.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("sable.scraper.diagnostics")

# ---------------------------------------------------------------------------
# Internal state
# ---------------------------------------------------------------------------

_sessions: dict[str, dict[str, Any]] = {}
_events: list[dict[str, Any]] = []
_MAX_EVENTS = 500
_lock = asyncio.Lock()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _epoch() -> float:
    return time.time()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class DiagnosticsMonitor:
    """Passive observer for scraper session health.

    Usage:
        monitor = get_monitor()
        sid = monitor.register_session("qwen", chat_id="abc123")
        monitor.heartbeat(sid)
        alive = monitor.get_alive_sessions()
        monitor.unregister_session(sid)
    """

    async def register_session(
        self,
        engine_type: str,
        *,
        chat_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Register a new scraper session. Returns session ID."""
        sid = uuid.uuid4().hex[:12]
        async with _lock:
            _sessions[sid] = {
                "session_id": sid,
                "engine_type": engine_type,
                "chat_id": chat_id,
                "registered_at": _now(),
                "last_heartbeat": _epoch(),
                "alive": True,
                "metadata": metadata or {},
            }
            _events.append({
                "ts": _now(),
                "type": "session_registered",
                "session_id": sid,
                "engine_type": engine_type,
                "chat_id": chat_id,
            })
        logger.debug("Diagnostics: session registered %s (%s)", sid, engine_type)
        return sid

    async def heartbeat(self, session_id: str) -> None:
        """Update last-seen timestamp for a session."""
        async with _lock:
            sess = _sessions.get(session_id)
            if sess:
                sess["last_heartbeat"] = _epoch()
                sess["alive"] = True

    async def mark_inactive(self, session_id: str) -> None:
        """Mark a session as no longer active."""
        async with _lock:
            sess = _sessions.get(session_id)
            if sess:
                sess["alive"] = False
                _events.append({
                    "ts": _now(),
                    "type": "session_inactive",
                    "session_id": session_id,
                })

    async def unregister_session(self, session_id: str) -> None:
        """Remove a session from tracking."""
        async with _lock:
            removed = _sessions.pop(session_id, None)
            if removed:
                _events.append({
                    "ts": _now(),
                    "type": "session_unregistered",
                    "session_id": session_id,
                    "engine_type": removed.get("engine_type"),
                })

    async def get_alive_sessions(self, *, max_age: float = 120.0) -> list[dict[str, Any]]:
        """Return sessions with heartbeat within max_age seconds."""
        now = _epoch()
        async with _lock:
            result = []
            for sid, sess in _sessions.items():
                age = now - sess["last_heartbeat"]
                if sess["alive"] and age <= max_age:
                    entry = dict(sess)
                    entry["age_seconds"] = round(age, 1)
                    result.append(entry)
            return result

    async def get_all_sessions(self) -> list[dict[str, Any]]:
        """Return all tracked sessions regardless of liveness."""
        async with _lock:
            return [dict(s) for s in _sessions.values()]

    async def get_recent_events(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return recent diagnostic events."""
        async with _lock:
            return list(_events[-limit:])

    async def probe_engine(self, engine: Any) -> dict[str, Any]:
        """Probe a live engine instance for health status."""
        result: dict[str, Any] = {
            "ts": _now(),
            "has_browser": False,
            "browser_pid": None,
            "connected": False,
        }
        try:
            browser = getattr(engine, "browser", None)
            if browser is not None:
                result["has_browser"] = True
                pid = getattr(browser, "pid", None)
                if pid:
                    result["browser_pid"] = pid
                    # Check if process is actually alive
                    try:
                        os.kill(pid, 0)
                        result["connected"] = True
                    except OSError:
                        result["connected"] = False
        except Exception as exc:
            result["error"] = str(exc)
        return result

    async def clear(self) -> None:
        """Reset all diagnostic state."""
        async with _lock:
            _sessions.clear()
            _events.clear()


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------

_monitor: DiagnosticsMonitor | None = None


def get_monitor() -> DiagnosticsMonitor:
    """Get or create the global DiagnosticsMonitor singleton."""
    global _monitor
    if _monitor is None:
        _monitor = DiagnosticsMonitor()
    return _monitor
