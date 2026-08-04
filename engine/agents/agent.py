
from __future__ import annotations

import asyncio
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from engine.agents.protocol import AgentStatus

# Max events kept for reconnect replay
_STREAM_HISTORY_SIZE = 300


@dataclass
class Agent:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    role: str = "researcher"
    task: str = ""
    context: str | None = None
    instruction: str | None = None
    model: str = "deepseek-instant"
    browser_data_dir: str | None = None
    qwen_session_id: str | None = None  # upstream Qwen chat_id (distinct from Sable chat_id)
    status: AgentStatus = AgentStatus.SPAWNED
    messages: list[dict[str, str]] = field(default_factory=list)
    result: str | None = None
    error: str | None = None
    tokens_used: int = 0
    skills_used: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    parent_id: str | None = None
    chat_id: str | None = None
    depth: int = 0
    collect: bool = False
    cancelled: bool = False  # Set by kill() — checked between tool calls
    system_prompt: str | None = None  # Built skill registry + output format for API backends

    # Per-agent SSE stream queue — frontend panel subscribes via /api/agents/{id}/stream
    stream_queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=200), repr=False)
    # Replay buffer: keeps last N events so reconnecting panels can catch up
    _stream_history: deque[dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=_STREAM_HISTORY_SIZE), repr=False
    )

    def push_stream_event(self, event: dict[str, Any]) -> None:
        """Push a chat-format SSE event to the agent's stream queue (non-blocking)."""
        self._stream_history.append(event)
        try:
            self.stream_queue.put_nowait(event)
        except asyncio.QueueFull:
            pass  # Panel disconnected or too slow — event is still in history

    def get_stream_history(self, since_index: int = 0) -> list[dict[str, Any]]:
        """Return buffered events for reconnect replay.

        Args:
            since_index: client sends last event index it received; we return
                         everything after that point.
        """
        history = list(self._stream_history)
        if since_index <= 0:
            return history
        return history[since_index:]

    @property
    def path(self) -> str:
        return f"/root/{self.role}/{self.id}"

    @property
    def duration(self) -> float:
        if self.completed_at:
            return self.completed_at - self.created_at
        return time.time() - self.created_at

    @property
    def word_count(self) -> int:
        return len((self.result or "").split())

    def mark_running(self) -> None:
        self.status = AgentStatus.RUNNING

    def mark_completed(self, result: str, degraded: bool = False) -> None:
        self.status = AgentStatus.DEGRADED if degraded else AgentStatus.COMPLETED
        self.result = result
        self.completed_at = time.time()

    def mark_failed(self, error: str) -> None:
        self.status = AgentStatus.FAILED
        self.error = error
        self.completed_at = time.time()
