
"""Multi-agent orchestration engine for Sable.

Usage:
    from engine.agents import get_runtime, TaskAssignment
    runtime = get_runtime()
    agent = await runtime.spawn(TaskAssignment(task="...", role="researcher"), chat_id="...")
"""
from engine.agents.agent import Agent
from engine.agents.decomposer import needs_decomposition
from engine.agents.notifications import notification_queue
from engine.agents.protocol import AgentEvent, AgentResult, AgentStatus, TaskAssignment
from engine.agents.registry import RoleConfig, get_role_config
from engine.agents.resilience import CircuitBreaker, LoopDetector
from engine.agents.runtime import AgentRuntime, get_runtime

__all__ = [
    "Agent",
    "AgentEvent",
    "AgentResult",
    "AgentRuntime",
    "AgentStatus",
    "CircuitBreaker",
    "LoopDetector",
    "RoleConfig",
    "TaskAssignment",
    "get_role_config",
    "get_runtime",
    "needs_decomposition",
    "notification_queue",
]
