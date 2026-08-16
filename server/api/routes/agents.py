
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
    "limits": {
        "max_iterations": 25,
        "max_consecutive_tool_calls": 15,
        "max_total_tool_calls": 50,
    },
    "defaults": {
        "analyst_model": "qwen3.7-max",
        "coder_model": "qwen3.7-max",
        "writer_model": "qwen3.7-max",
        "timeout_analyst": 300,
        "timeout_coder": 300,
        "timeout_writer": 300,
        "sysutil_model": "qwen3.7-max",
        "docs_model": "qwen3.7-max",
        "visuals_model": "qwen3.7-max",
        "tester_model": "qwen3.7-max",
        "timeout_sysutil": 300,
        "timeout_docs": 300,
        "timeout_visuals": 300,
        "timeout_tester": 300,
    },
    "roles": {
        "analyst": {
            "default_model": "qwen3.7-max",
            "default_timeout": 300,
            "max_parallel": 4,
            "allowed_skills": ["execute_command", "online_search", "code_editor", "file_uploader"],
            "default_skills": ["online_search", "code_editor"],
            "required_sections": [],
        },
        "coder": {
            "default_model": "qwen3.7-max",
            "default_timeout": 300,
            "max_parallel": 1,
            "allowed_skills": ["execute_command", "code_editor", "online_search"],
            "default_skills": ["code_editor"],
            "required_sections": ["Description", "Files Modified"],
        },

        "writer": {
            "default_model": "qwen3.7-max",
            "default_timeout": 300,
            "max_parallel": 2,
            "allowed_skills": ["execute_command", "code_editor", "online_search"],
            "default_skills": ["code_editor"],
            "required_sections": ["Title", "Structure Overview"],
        },

        "sysutil": {
            "default_model": "qwen3.7-max",
            "default_timeout": 300,
            "max_parallel": 3,
            "allowed_skills": ["execute_command", "system_repair", "phone_control", "youtube_downloader", "grep_search", "code_editor", "online_search", "file_uploader"],
            "default_skills": ["system_repair", "youtube_downloader", "code_editor"],
            "required_sections": ["Task", "Result"],
        },
        "docs": {
            "default_model": "qwen3.7-max",
            "default_timeout": 300,
            "max_parallel": 2,
            "allowed_skills": ["execute_command", "document_skills", "file_uploader", "text_humanizer", "code_editor"],
            "default_skills": ["document_skills", "file_uploader"],
            "required_sections": ["Task", "Document Path"],
        },
        "visuals": {
            "default_model": "qwen3.7-max",
            "default_timeout": 300,
            "max_parallel": 2,
            "allowed_skills": ["execute_command", "graph_master", "svg_creator", "frontend_design", "simulacra_engine", "code_editor"],
            "default_skills": ["graph_master", "svg_creator"],
            "required_sections": ["Task", "Output Path"],
        },
        "tester": {
            "default_model": "qwen3.7-max",
            "default_timeout": 300,
            "max_parallel": 2,
            "allowed_skills": ["execute_command", "testing_debugging", "code_editor", "grep_search"],
            "default_skills": ["testing_debugging", "code_editor"],
            "required_sections": ["Bug Summary", "Root Cause", "Fix Applied"],
        },
    },
    "universal_skills": ["execute_command"],
    "account_assignments": {},
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
    from engine.config import get_all_models

    config = _load_agent_config()
    config["roles"] = export_roles()
    config["universal_skills"] = get_universal_skills()
    # Include account assignments (may not exist in older configs)
    config.setdefault("account_assignments", {})
    # Include all available models for the dropdown
    config["available_models"] = [
        {"id": m["id"], "label": m["label"], "api_backend": m.get("api_backend")}
        for m in get_all_models()
    ]
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
    from engine.agents.registry import apply_role_overrides, apply_account_assignments
    apply_role_overrides(current.get("roles", {}), current.get("universal_skills"))

    # Hot-reload per-role account pools (list-based)
    if "account_assignments" in config:
        current["account_assignments"] = config["account_assignments"]
        _save_agent_config(current)
    apply_account_assignments(current.get("account_assignments", {}))

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


@router.get("/api/agents/{agent_id}/stream")
async def agent_stream(agent_id: str, request: Request, since: int = 0):
    """Per-agent SSE stream — chat-format events for the panel view.

    Drains the agent's stream_queue. Sends keepalive every 15s.
    Closes when the agent finishes (done/error event) or client disconnects.

    Reconnect support: pass ?since=N to replay missed events from the
    agent's history buffer before switching to live events.
    """
    from engine.agents import get_runtime

    rt = get_runtime()
    agent = rt.get_agent(agent_id)
    if not agent:
        return StreamingResponse(
            iter([f"data: {json.dumps({'type': 'error', 'message': 'Agent not found'})}\n\n"]),
            media_type="text/event-stream",
        )

    queue = agent.stream_queue

    async def generate():
        try:
            # Replay missed events on reconnect
            if since > 0:
                for evt in agent.get_stream_history(since_index=since):
                    yield f"data: {json.dumps(evt, ensure_ascii=False, default=str)}\n\n"

            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                    yield f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
                    # Close stream on terminal events
                    if event.get("type") in ("done", "error"):
                        break
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    # If agent is no longer running and queue is empty, close
                    if agent.status.value not in ("spawned", "running") and queue.empty():
                        yield f"data: {json.dumps({'type': 'done', 'result': (agent.result or '')[:500]})}\n\n"
                        break
        finally:
            pass

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@router.get("/api/agents/{agent_id}/messages")
async def get_agent_history(agent_id: str):
    """Full conversation history + status for an agent."""
    from server.database import get_agent_messages
    from engine.agents import get_runtime

    messages = get_agent_messages(agent_id)
    rt = get_runtime()
    agent = rt.get_agent(agent_id)
    status = agent.status.value if agent else "unknown"
    result = {
        "agent_id": agent_id,
        "messages": messages,
        "status": status,
    }
    if agent:
        result["created_at"] = agent.created_at
        result["model"] = agent.model
        result["browser_data_dir"] = agent.browser_data_dir
        if agent.completed_at:
            result["completed_at"] = agent.completed_at
        if agent.todos and agent.todos.todos:
            result["todos"] = [
                {"id": t.id, "content": t.content, "status": t.status, "subtasks": t.subtasks, "result": t.result}
                for t in agent.todos.todos
            ]
    return result


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


@router.post("/api/agents/{agent_id}/message")
async def send_agent_message(agent_id: str, request: Request):
    """Inject a user message into a running agent's conversation (guidance)."""
    from engine.agents import get_runtime
    from server.database import add_agent_message

    body = await request.json()
    text = body.get("message", "").strip()
    if not text:
        return {"error": "message is required"}

    rt = get_runtime()
    agent = rt.get_agent(agent_id)
    if not agent:
        return {"error": "Agent not found", "agent_id": agent_id}
    if agent.status.value not in ("spawned", "running"):
        return {"error": "Agent is not running", "agent_id": agent_id}

    # Queue the message — the loop picks it up between iterations
    agent.pending_user_messages.append(text)
    # Persist to DB for history replay
    add_agent_message(agent_id, "user", text)
    # Push to SSE stream so the panel shows it immediately
    agent.push_stream_event({"type": "user_message", "text": text})

    return {"status": "queued", "agent_id": agent_id}


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

    valid_roles = ("analyst", "coder", "writer", "sysutil", "docs", "visuals", "tester")
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



