
"""Per-chat notification queue for agent completion/failure events."""
from __future__ import annotations

import asyncio

from engine.agents.protocol import AgentEvent


class NotificationQueue:
    """In-process queue: agents push events, Maria drains them at turn start."""

    def __init__(self) -> None:
        self._queues: dict[str, asyncio.Queue[AgentEvent]] = {}

    def push(self, chat_id: str, event: AgentEvent) -> None:
        q = self._queues.setdefault(chat_id, asyncio.Queue(maxsize=50))
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            # Drop oldest to make room, log the eviction
            import logging
            logging.getLogger("sable").warning(
                "[notifications] Queue full for chat %s — dropping oldest, adding agent %s (%s)",
                chat_id, event.agent_id, event.type,
            )
            try:
                q.get_nowait()  # evict oldest
                q.put_nowait(event)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass

    def drain(self, chat_id: str) -> list[AgentEvent]:
        """Called at start of Maria's next turn. Returns all pending notifications."""
        q = self._queues.get(chat_id)
        if not q:
            return []
        events: list[AgentEvent] = []
        while not q.empty():
            events.append(q.get_nowait())
        return events

    def has_pending(self, chat_id: str) -> bool:
        q = self._queues.get(chat_id)
        return bool(q and not q.empty())


# Module-level singleton
notification_queue = NotificationQueue()
