
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


# ---------------------------------------------------------------------------
# Todo system — structured task tracking for spawned agents
# ---------------------------------------------------------------------------

@dataclass
class TodoItem:
    """A single task in an agent's execution plan."""
    id: int
    content: str
    status: str = "pending"  # pending | in_progress | completed | skipped
    result: str | None = None
    subtasks: list[str] = field(default_factory=list)


@dataclass
class AgentTodoList:
    """Ordered task list attached to an agent. System-managed progression."""
    todos: list[TodoItem] = field(default_factory=list)
    current_index: int = 0
    skip_reasons: list[tuple[int, str]] = field(default_factory=list)  # (todo_id, reason)

    @property
    def current(self) -> TodoItem | None:
        if 0 <= self.current_index < len(self.todos):
            return self.todos[self.current_index]
        return None

    @property
    def all_done(self) -> bool:
        return self.current_index >= len(self.todos) or len(self.todos) == 0

    @classmethod
    def build_from_list(cls, items: list[str]) -> "AgentTodoList":
        """Create a todo list from plain strings. First item starts in_progress."""
        todos = [
            TodoItem(id=i + 1, content=t.strip(), status="pending")
            for i, t in enumerate(items)
            if t.strip()
        ]
        if todos:
            todos[0].status = "in_progress"
        return cls(todos=todos)

    @property
    def progress(self) -> str:
        done = sum(1 for t in self.todos if t.status == "completed")
        return f"{done}/{len(self.todos)}"

    def advance(self) -> TodoItem | None:
        """Mark current as completed (unless skipped), move to next. Returns new current or None."""
        if self.current and self.current.status != "skipped":
            self.current.status = "completed"
        self.current_index += 1
        # Auto-skip: advance past any skipped items
        while self.current and self.current.status == "skipped":
            self.current_index += 1
        nxt = self.current
        if nxt and nxt.status == "pending":
            nxt.status = "in_progress"
        return nxt

    def skip_current_and_advance(self) -> TodoItem | None:
        """Mark current as skipped and move to next non-skipped item."""
        if self.current:
            self.current.status = "skipped"
        self.current_index += 1
        while self.current and self.current.status == "skipped":
            self.current_index += 1
        nxt = self.current
        if nxt and nxt.status == "pending":
            nxt.status = "in_progress"
        return nxt

    def format_state(self) -> str:
        """Minimal state block for per-iteration injection. No rules — just facts."""
        if not self.todos:
            return ""
        done = sum(1 for t in self.todos if t.status in ("completed", "skipped"))
        total = len(self.todos)
        current = self.current
        if self.all_done:
            return f"[TODO] All {total} tasks complete ({done}/{total}). Provide your final answer."
        line = f"[TODO] Progress: {done}/{total} | Current: {current.content}" if current else f"[TODO] Progress: {done}/{total}"
        return line


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
    tool_calls_total: int = 0  # Total tool invocations (all tags)
    error_recoveries: int = 0  # Tool calls that returned SKILL ERROR
    created_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    parent_id: str | None = None
    chat_id: str | None = None
    depth: int = 0
    collect: bool = False
    cancelled: bool = False  # Set by kill() — checked between tool calls
    system_prompt: str | None = None  # Built skill registry + output format for API backends
    todos: AgentTodoList | None = None  # Structured task plan (None = simple task, no tracking)
    teacher_interventions: int = 0  # How many times the teacher has intervened
    model_chain: list[str] = field(default_factory=list)  # Fallback models from role config
    _fallback_index: int = 0  # Current position in model_chain (0 = primary model)

    # Per-agent SSE stream queue — frontend panel subscribes via /api/agents/{id}/stream
    stream_queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=200), repr=False)
    # Replay buffer: keeps last N events so reconnecting panels can catch up
    _stream_history: deque[dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=_STREAM_HISTORY_SIZE), repr=False
    )
    # User-injected messages (guidance) — checked between loop iterations
    pending_user_messages: list[str] = field(default_factory=list, repr=False)

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
