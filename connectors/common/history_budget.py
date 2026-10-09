"""Shared DB-history budgeting for connectors.

Every connector that seeds a fresh session from Sable's local DB history must
cap what it injects. Without a cap, a long agentic chat (hundreds of tool
calls) hands the connector the ENTIRE message table at once — which is what
ballooned RAM to 16GB + 20GB swap and OOM-killed the box.

Policy: keep the newest messages, drop the oldest, but always preserve the
first user message as a context anchor (it usually carries the task framing).
"""

from __future__ import annotations

from typing import Any

# LIVE WORKING SET — the only history kept in RAM.
#
# The DB holds the full transcript. RAM only ever holds the newest ~50
# messages (recent tool calls + user turns). Everything older stays on
# disk and is NEVER materialized into a Python list. This is what keeps
# a 700-tool-call chat from ballooning memory — the working set is
# constant regardless of how deep the conversation goes.
MAX_DB_HISTORY_MESSAGES = 50
MAX_DB_HISTORY_CHARS = 1_000_000  # secondary guard; the count is primary


def _content_of(msg: dict[str, Any]) -> str:
    """Best-effort text extraction across message shapes."""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            p.get("text", "") if isinstance(p, dict) else str(p) for p in content
        )
    return ""


def trim_history_messages(
    messages: list[dict[str, Any]],
    max_messages: int = MAX_DB_HISTORY_MESSAGES,
    max_chars: int = MAX_DB_HISTORY_CHARS,
) -> list[dict[str, Any]]:
    """Return a bounded copy of `messages`.

    Keeps the newest messages, drops the oldest. The first message in the
    original list is pinned (kept as the leading entry) when it would
    otherwise be trimmed, so the task framing survives.

    Never mutates the input.
    """
    if not messages:
        return []

    msgs = list(messages)

    # 1) Trim by count from the front, pinning the first entry.
    if len(msgs) > max_messages:
        pinned = msgs[0]
        tail = msgs[-(max_messages - 1):]
        msgs = [pinned, *tail]

    # 2) Trim by char budget from the front (again pinning the first entry).
    def _total(items: list[dict[str, Any]]) -> int:
        return sum(len(_content_of(m)) for m in items)

    if _total(msgs) > max_chars:
        pinned = msgs[0]
        tail: list[dict[str, Any]] = []
        used = 0
        for m in reversed(msgs[1:]):
            c = len(_content_of(m))
            if used + c > max_chars:
                break
            tail.append(m)
            used += c
        tail.reverse()
        msgs = [pinned, *tail] if tail else [pinned]

    return msgs


def trim_openai_history(
    messages: list[dict[str, Any]],
    max_messages: int = MAX_DB_HISTORY_MESSAGES,
    max_chars: int = MAX_DB_HISTORY_CHARS,
) -> list[dict[str, Any]]:
    """Convenience wrapper for OpenAI-shaped ({role, content}) history."""
    return trim_history_messages(messages, max_messages, max_chars)
