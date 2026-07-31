
"""Agent monitoring API: persistent SSE stream + REST endpoints.

Provides real-time agent status updates to the frontend via Server-Sent Events,
plus REST endpoints for listing agents, viewing history, killing stuck agents,
and managing agent configuration.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from engine.config import AGENT_CONFIG_PATH

router = APIRouter()

# Per-chat SSE client queues: chat_id → list of asyncio.Queue
_agent_sse_clients: dict[str, list[asyncio.Queue]] = {}


def push_agent_event(chat_id: str, event: dict[str, Any]) -> None:
    """Push an agent event to all connected SSE clients for a chat.

    Called by AgentRuntime._emit via the event callback set in application.py.
    """
    queues = _agent_sse_clients.get(chat_id, [])
    payload = json.dumps(event, ensure_ascii=False, default=str)
    for q in queues:
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            pass  # Client too slow or disconnected — skip


async def _async_push_agent_event(chat_id: str, event: dict[str, Any]) -> None:
    """Async wrapper for the runtime's event callback signature."""
    push_agent_event(chat_id, event)


# --------------------------------------------------------------------------
# SSE Stream
# --------------------------------------------------------------------------

@router.get("/api/chat/{chat_id}/agent-events")
async def agent_events_stream(chat_id: str, request: Request):
    """Persistent SSE stream for real-time agent events in a chat.

    Frontend opens this on chat load. Sends keepalive comments every 30s
    to prevent proxy/connection timeouts.
    """
    queue: asyncio.Queue = asyncio.Queue(maxsize=50)
    _agent_sse_clients.setdefault(chat_id, []).append(queue)

    async def generate():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=30)
                    yield f"data: {data}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            clients = _agent_sse_clients.get(chat_id, [])
            if queue in clients:
                clients.remove(queue)
            if not clients and chat_id in _agent_sse_clients:
                del _agent_sse_clients[chat_id]

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


# --------------------------------------------------------------------------
# REST Endpoints
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Configuration (must be BEFORE /api/agents/{agent_id} to avoid path capture)
# --------------------------------------------------------------------------

DEFAULT_AGENT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "concurrency": {
        "global_max": 5,
        "deepseek_max": 5,
        "qwen_max": 1,
    },
    "resilience": {
        "circuit_breaker_threshold": 5,
        "circuit_breaker_reset_seconds": 60,
    },
    "defaults": {
        "researcher_model": "deepseek-instant",
        "coder_model": "qwen-max",
        "reviewer_model": "deepseek-instant",
        "writer_model": "deepseek-expert",
        "utility_model": "deepseek-instant",
        "timeout_researcher": 90,
        "timeout_coder": 180,
        "timeout_reviewer": 60,
        "timeout_writer": 120,
        "timeout_utility": 120,
    },
}


def _load_agent_config() -> dict[str, Any]:
    """Load agent config from disk, falling back to defaults."""
    if AGENT_CONFIG_PATH.exists():
        try:
            return json.loads(AGENT_CONFIG_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return DEFAULT_AGENT_CONFIG.copy()


def _save_agent_config(config: dict[str, Any]) -> None:
    """Persist agent config to disk."""
    AGENT_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    AGENT_CONFIG_PATH.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")


@router.get("/api/agents/config")
async def get_agent_config():
    """Get current agent configuration including per-role details."""
    from engine.agents.registry import export_roles, get_universal_skills

    config = _load_agent_config()
    config["roles"] = export_roles()
    config["universal_skills"] = get_universal_skills()
    return config


@router.put("/api/agents/config")
async def update_agent_config(request: Request):
    """Update agent configuration. Hot-reloads runtime settings + role overrides."""
    config = await request.json()

    # Merge with defaults (partial updates allowed)
    current = _load_agent_config()
    for key, value in config.items():
        if key in ("roles", "universal_skills"):
            continue  # handled separately
        if isinstance(value, dict) and isinstance(current.get(key), dict):
            current[key].update(value)
        else:
            current[key] = value

    # Save role overrides + universal skills
    if "roles" in config:
        current["roles"] = config["roles"]
    if "universal_skills" in config:
        current["universal_skills"] = config["universal_skills"]

    _save_agent_config(current)

    # Hot-reload runtime concurrency/resilience
    from engine.agents import get_runtime
    get_runtime().update_config(current)

    # Hot-reload role overrides + universal skills
    from engine.agents.registry import apply_role_overrides
    apply_role_overrides(current.get("roles", {}), current.get("universal_skills"))

    return {"status": "updated", "config": current}


@router.get("/api/agents/active")
async def list_active_agents(chat_id: str | None = None):
    """List all agents, optionally filtered by chat."""
    from engine.agents import get_runtime

    rt = get_runtime()
    agents = rt.list_agents(chat_id)
    return [
        {
            "id": a.id,
            "role": a.role,
            "task": a.task,
            "status": a.status.value,
            "model": a.model,
            "duration": round(a.duration, 1),
            "tokens": a.tokens_used,
            "skills_used": a.skills_used,
            "chat_id": a.chat_id,
            "created_at": a.created_at,
        }
        for a in agents
    ]


@router.get("/api/agents/{agent_id}/messages")
async def get_agent_history(agent_id: str):
    """Full conversation history for an agent."""
    from server.database import get_agent_messages

    messages = get_agent_messages(agent_id)
    return {"agent_id": agent_id, "messages": messages}


@router.get("/api/agents/{agent_id}")
async def get_agent_detail(agent_id: str):
    """Single agent detail including result."""
    from engine.agents import get_runtime

    rt = get_runtime()
    agent = rt.get_agent(agent_id)
    if not agent:
        # Fall back to DB
        from server.database import get_db
        with get_db() as conn:
            row = conn.execute(
                "SELECT * FROM agent_runs WHERE id = ?", (agent_id,)
            ).fetchone()
        if not row:
            return {"error": "Agent not found"}
        return dict(row)

    return {
        "id": agent.id,
        "role": agent.role,
        "task": agent.task,
        "context": agent.context,
        "instruction": agent.instruction,
        "status": agent.status.value,
        "model": agent.model,
        "result": agent.result,
        "error": agent.error,
        "tokens_used": agent.tokens_used,
        "skills_used": agent.skills_used,
        "duration": round(agent.duration, 1),
        "chat_id": agent.chat_id,
        "depth": agent.depth,
        "parent_id": agent.parent_id,
    }


@router.post("/api/agents/{agent_id}/kill")
async def kill_agent(agent_id: str):
    """Kill a running agent."""
    from engine.agents import get_runtime
    from server.database import update_agent_status

    rt = get_runtime()
    task = rt._tasks.get(agent_id)
    if task and not task.done():
        task.cancel()
    if agent_id in rt._agents:
        rt._agents[agent_id].mark_failed("Stopped by user")
        update_agent_status(agent_id, "killed", error="Stopped by user")
        # Push event to SSE clients
        agent = rt._agents[agent_id]
        if agent.chat_id:
            push_agent_event(agent.chat_id, {
                "type": "agent_failed",
                "agent_id": agent_id,
                "data": {"role": agent.role, "error": "Stopped by user"},
            })
        return {"status": "killed", "agent_id": agent_id}
    return {"error": "Agent not found", "agent_id": agent_id}


@router.post("/api/agents/spawn")
async def spawn_agent(request: Request):
    """Manually spawn an agent from the chat UI (@ mention)."""
    from engine.agents import get_runtime
    from engine.agents.protocol import TaskAssignment

    body = await request.json()
    role = body.get("role", "").strip().lower()
    task = body.get("task", "").strip()
    chat_id = body.get("chat_id", "").strip()

    if not role or not task or not chat_id:
        return {"error": "role, task, and chat_id are required"}

    valid_roles = ("researcher", "coder", "reviewer", "writer", "utility")
    if role not in valid_roles:
        return {"error": f"Invalid role '{role}'. Must be one of: {', '.join(valid_roles)}"}

    rt = get_runtime()
    assignment = TaskAssignment(
        task=task,
        role=role,
        model=body.get("model") or None,  # None → registry default
        context=body.get("context") or None,
    )
    try:
        agent = await rt.spawn(assignment, chat_id)
    except RuntimeError as exc:
        return {"error": str(exc)}

    return {"status": "spawned", "agent_id": agent.id, "role": role, "model": agent.model}



