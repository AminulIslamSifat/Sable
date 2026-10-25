"""Shared mid-turn user-message queue.

Why this module exists
----------------------
A user can type while the model is still working. Those messages are stored
here instead of aborting the stream.

It has to live outside ``server/api/routes/chat.py`` because the *connector*
(openai_compat / gemini / local) runs its OWN agentic tool loop: it re-enters
the API call with tool results and only returns to the chat route when the
loop is finished. The chat route's round boundary is therefore NOT reached
while tools are running, so a queue drained only there would sit untouched
until the turn ended (or be discarded on stop).

Both sides import this module so there is exactly one queue per chat.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger("sable.chat_queue")

# chat_ids with a live /api/chat stream in flight.
ACTIVE_CHAT_STREAMS: set[str] = set()

# chat_id -> queued user messages (FIFO).
USER_QUEUES: dict[str, list[dict[str, Any]]] = {}

# chat_id -> callable(text) -> db_id, used to persist a message the connector
# injects so history replay keeps the interjection.
_PERSIST: dict[str, Callable[[str], Any]] = {}

# chat_id -> mutable cell holding the current tail parent_id.
_PARENT: dict[str, dict[str, Any]] = {}

# chat_ids where the CONNECTOR delivered tool results + a queued interjection
# to the model inside its own tool loop. The chat route must then NOT re-feed
# build_tool_feedback() for that round: the connector already gave the model
# the tool results, so a text copy would arrive AFTER the user's message and
# the model reads it as "the user pasted the tool output back at me".
_CONNECTOR_INJECTED: set[str] = set()


def mark_connector_injected(chat_id: str) -> None:
    _CONNECTOR_INJECTED.add(chat_id)


def consume_connector_injected(chat_id: str) -> bool:
    """True once if the connector injected this round. Clears the flag."""
    if chat_id in _CONNECTOR_INJECTED:
        _CONNECTOR_INJECTED.discard(chat_id)
        return True
    return False


def set_persist(chat_id: str, fn: Callable[[str], Any] | None) -> None:
    """Register the DB-persist callback for this chat (None clears it)."""
    if fn is None:
        _PERSIST.pop(chat_id, None)
    else:
        _PERSIST[chat_id] = fn


def set_parent(chat_id: str, parent: Any) -> None:
    """Track the current tail parent so injected rows branch correctly."""
    _PARENT.setdefault(chat_id, {})["id"] = parent


def enqueue(chat_id: str, text: str, temp_id: Any = None) -> int:
    q = USER_QUEUES.setdefault(chat_id, [])
    q.append({"text": text, "temp_id": temp_id})
    return len(q)


def drain(chat_id: str) -> list[dict[str, Any]]:
    """Pop every queued message for this chat (FIFO)."""
    return USER_QUEUES.pop(chat_id, [])


def take_queued_for(chat_id: str) -> list[dict[str, Any]]:
    """Drain AND persist the queued messages, returning what was taken.

    Used by the connector's internal tool loop. Persistence is best-effort —
    the message has already been handed to the model either way, and losing
    the DB row is far better than losing the message itself.
    """
    msgs = drain(chat_id)
    if not msgs:
        return []
    persist = _PERSIST.get(chat_id)
    if persist:
        for m in msgs:
            text = (m.get("text") or "").strip()
            if not text:
                continue
            try:
                m["db_id"] = persist(text)
            except Exception as exc:  # pragma: no cover - persistence is best effort
                logger.warning("[chat-queue] persist failed for %s: %s", chat_id, exc)
    logger.info("[chat-queue] connector injected %d msg(s) for %s", len(msgs), chat_id)
    return msgs


def clear(chat_id: str) -> None:
    """Drop the queue and any per-chat hooks (stream teardown)."""
    USER_QUEUES.pop(chat_id, None)
    _PERSIST.pop(chat_id, None)
    _PARENT.pop(chat_id, None)
