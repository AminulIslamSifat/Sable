"""Diagnostics HTTP client — talks to the Go beacon's local diag server.

Replaces the old in-process Python monitor/replay modules.
All calls are fire-and-forget with short timeouts. Failures return
safe defaults so scraper operations are never blocked.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from typing import Any

logger = logging.getLogger("sable.scraper.diagnostics")

DIAG_URL = "http://127.0.0.1:18923"
_TIMEOUT = 5  # seconds — diag calls must be fast


def _post(path: str, data: dict[str, Any]) -> dict[str, Any] | None:
    try:
        payload = json.dumps(data).encode()
        req = urllib.request.Request(
            f"{DIAG_URL}{path}",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            body = resp.read().decode()
            return json.loads(body) if body else {}
    except Exception as exc:
        logger.debug("Diag POST %s failed: %s", path, exc)
        return None


def _get(path: str) -> Any | None:
    try:
        req = urllib.request.Request(f"{DIAG_URL}{path}", method="GET")
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            body = resp.read().decode()
            return json.loads(body) if body else None
    except Exception as exc:
        logger.debug("Diag GET %s failed: %s", path, exc)
        return None


# ── Monitor API ──────────────────────────────────────────────

def register_session(engine_type: str, *, chat_id: str | None = None, metadata: dict | None = None) -> str | None:
    result = _post("/diag/monitor/register", {
        "engine_type": engine_type,
        "chat_id": chat_id or "",
        "metadata": metadata or {},
    })
    return result.get("session_id") if result else None


def heartbeat(session_id: str) -> None:
    _post("/diag/monitor/heartbeat", {"session_id": session_id})


def mark_inactive(session_id: str) -> None:
    _post("/diag/monitor/inactive", {"session_id": session_id})


def unregister_session(session_id: str) -> None:
    _post("/diag/monitor/unregister", {"session_id": session_id})


def get_alive_sessions(max_age: float = 120.0) -> list[dict]:
    # max_age is handled server-side (fixed at 120s), this is for API compat
    result = _get("/diag/monitor/alive")
    return result if isinstance(result, list) else []


def get_all_sessions() -> list[dict]:
    result = _get("/diag/monitor/sessions")
    return result if isinstance(result, list) else []


def get_recent_events(limit: int = 50) -> list[dict]:
    result = _get("/diag/monitor/events")
    return result if isinstance(result, list) else []


def probe_engine_pid(pid: int) -> dict:
    result = _post("/diag/monitor/probe", {"pid": pid})
    return result or {"ts": "", "has_browser": False, "browser_pid": None, "connected": False}


def clear_monitor() -> None:
    _post("/diag/monitor/clear", {})


# ── Replay API ───────────────────────────────────────────────

def start_replay(engine_type: str, *, prompt: str = "", chat_id: str | None = None, metadata: dict | None = None) -> str | None:
    result = _post("/diag/replay/start", {
        "engine_type": engine_type,
        "prompt": prompt,
        "chat_id": chat_id or "",
        "metadata": metadata or {},
    })
    return result.get("replay_id") if result else None


def get_replay_result(replay_id: str) -> dict | None:
    return _get(f"/diag/replay/{replay_id}")


def stop_replay(replay_id: str) -> None:
    _post("/diag/replay/stop", {"replay_id": replay_id})


def list_replays(limit: int = 20) -> list[dict]:
    result = _get("/diag/replays")
    if isinstance(result, dict):
        return result.get("replays", [])
    return []


def clear_replays() -> None:
    _post("/diag/replay/clear", {})
