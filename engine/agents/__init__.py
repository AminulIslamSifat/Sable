
"""Multi-agent orchestration engine for Sable.

Usage:
    from engine.agents import get_runtime, TaskAssignment, current_chat_id
    runtime = get_runtime()
    agent = await runtime.spawn(TaskAssignment(task="...", role="researcher"), chat_id="...")
"""
from contextvars import ContextVar

# Per-request chat_id — safe across concurrent async tasks (each task gets its own context)
current_chat_id: ContextVar[str | None] = ContextVar("current_chat_id", default=None)

from engine.agents.agent import Agent
from engine.agents.decomposer import needs_decomposition
from engine.agents.notifications import notification_queue
from engine.agents.protocol import AgentEvent, AgentResult, AgentStatus, TaskAssignment
from engine.agents.registry import RoleConfig, get_role_config
from engine.agents.resilience import (
    CircuitBreaker,
    GuardrailDecision,
    LoopDetector,
    TurnCapTracker,
    build_recovery_prompt,
)
from engine.agents.runtime import AgentRuntime, get_runtime

__all__ = [
    "Agent",
    "current_chat_id",
    "AgentEvent",
    "AgentResult",
    "AgentRuntime",
    "AgentStatus",
    "CircuitBreaker",
    "LoopDetector",
    "TurnCapTracker",
    "RoleConfig",
    "TaskAssignment",
    "get_role_config",
    "get_runtime",
    "needs_decomposition",
    "notification_queue",
]
