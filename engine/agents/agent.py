
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

from engine.agents.protocol import AgentStatus


@dataclass
class Agent:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    role: str = "researcher"
    task: str = ""
    context: str | None = None
    instruction: str | None = None
    model: str = "deepseek-instant"
    browser_data_dir: str | None = None
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

    @property
    def path(self) -> str:
        return f"/root/{self.role}/{self.id}"

    @property
    def duration(self) -> float:
        if self.completed_at:
            return self.completed_at - self.created_at
        return time.time() - self.created_at

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
