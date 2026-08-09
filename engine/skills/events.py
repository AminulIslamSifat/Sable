
"""SSE event builders for skill execution feedback."""

from __future__ import annotations

import time
from typing import Any


def start_event(tag_id: str, name: str, attrs: dict[str, str], content_preview: str) -> dict[str, Any]:
    """Emitted when a tag is dispatched to a handler."""
    return {
        "type": "skill_start",
        "id": tag_id,
        "name": name,
        "data": {"attrs": attrs, "content": content_preview[:2000]},
    }


def output_event(tag_id: str, text: str, stream: str = "stdout") -> dict[str, Any]:
    """Streaming output chunk from a running handler."""
    return {"type": "skill_output", "id": tag_id, "text": text, "stream": stream}


def end_event(
    tag_id: str,
    name: str,
    ok: bool,
    started: float,
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """Emitted when a handler completes (success or failure)."""
    event: dict[str, Any] = {
        "type": "skill_end",
        "id": tag_id,
        "name": name,
        "ok": ok,
        "duration_ms": int((time.time() - started) * 1000),
        "result": result or {},
    }
    if error:
        event["error"] = error
    return event


def permission_request_event(
    tag_id: str,
    name: str,
    content: str,
    category: str,
    reason: str,
) -> dict[str, Any]:
    """Emitted when a command requires explicit user approval before execution."""
    return {
        "type": "permission_request",
        "id": tag_id,
        "name": name,
        "data": {
            "command": content[:500],
            "category": category,
            "reason": reason,
        },
    }


def build_tool_feedback(
    skill_events: list[dict[str, Any]],
    max_output_per_skill: int = 12000,
    max_total: int = 32000,
) -> str | None:
    """Build a compact text summary of skill events for model feedback.

    Groups events by tag_id, formats each as a block with name, status,
    and truncated output. Returns None if no skill_end events found.
    """
    starts: dict[str, dict[str, Any]] = {}
    outputs: dict[str, list[str]] = {}
    ends: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    for event in skill_events:
        event_id = str(event.get("id") or "")
        if not event_id:
            continue
        event_type = event.get("type")
        if event_type == "skill_start":
            if event_id not in starts:
                order.append(event_id)
            starts[event_id] = event
            outputs.setdefault(event_id, [])
        elif event_type == "skill_output":
            outputs.setdefault(event_id, []).append(str(event.get("text", "")))
        elif event_type == "skill_end":
            ends[event_id] = event
            if event_id not in starts:
                order.append(event_id)
                starts[event_id] = {"name": event.get("name", "skill")}

    if not ends:
        return None

    blocks: list[str] = []
    total_len = 0

    for tag_id in order:
        if tag_id not in ends:
            continue
        end = ends[tag_id]
        name = end.get("name") or starts.get(tag_id, {}).get("name", "unknown")
        ok = end.get("ok", False)
        duration = end.get("duration_ms", 0)
        error = end.get("error")

        header = f"[{name}] {'OK' if ok else 'FAILED'} ({duration}ms)"
        if error:
            header += f" — {error}"

        raw_output = "".join(outputs.get(tag_id, []))
        if len(raw_output) > max_output_per_skill:
            raw_output = raw_output[:max_output_per_skill] + "\n... (truncated)"

        block = header
        if raw_output.strip():
            block += f"\n{raw_output.strip()}"

        if total_len + len(block) > max_total:
            blocks.append(f"... ({len(order) - len(blocks)} more results omitted)")
            break

        blocks.append(block)
        total_len += len(block)

    return "\n\n".join(blocks) if blocks else None
