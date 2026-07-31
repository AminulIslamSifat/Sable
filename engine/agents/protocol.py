
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class AgentStatus(str, Enum):
    SPAWNED = "spawned"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    KILLED = "killed"
    DEGRADED = "degraded"


@dataclass
class AgentResult:
    agent_id: str
    role: str
    status: AgentStatus
    summary: str
    artifacts: list[str] = field(default_factory=list)
    tokens_used: int = 0
    error: str | None = None
    duration_seconds: float = 0.0
    skills_used: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return self.status in (AgentStatus.COMPLETED, AgentStatus.DEGRADED)


@dataclass
class TaskAssignment:
    task: str
    role: str
    context: str | None = None
    instruction: str | None = None
    model: str | None = None
    browser_data_dir: str | None = None
    timeout: float | None = None
    collect: bool = False
    parent_agent_id: str | None = None


@dataclass
class AgentEvent:
    type: str  # agent_spawned | agent_progress | agent_completed | agent_failed
    agent_id: str
    data: dict[str, Any] = field(default_factory=dict)
