
"""Multi-agent handlers: spawn_agent, agent_status, kill_agent, todo_complete, todo_skip.

spawn_agent is non-blocking: does sync setup (Agent dataclass + DB insert),
then schedules the async run via loop.create_task(). No thread pools needed —
we're always called from within FastAPI's running event loop.

Collect mode (collect="true") is awaited in chat.py after skill events are gathered —
it blocks the stream until the agent completes, then injects the result into feedback.

todo_complete / todo_skip are internal tools injected only into subagent prompts
when a TODO list is present. They advance the structured task plan.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Generator
from typing import Any

from engine.agents import current_chat_id as _chat_id_var
from engine.skills.handlers.common import _end_event, _output_event


def handle_spawn_agent(
    tag_id: str, name: str, attrs: dict[str, str], content: str
) -> Generator[dict[str, Any], None, None]:
    """Spawn a background agent. Non-blocking — returns agent_id immediately."""
    started = time.time()

    task_text = attrs.get("task", content.strip())
    if not task_text:
        yield _output_event(tag_id, "ERROR: No task specified for spawn_agent")
        yield _end_event(tag_id, name, False, started, error="No task specified")
        return

    role = attrs.get("role", "researcher")
    context = attrs.get("context")
    instruction = attrs.get("instruction")
    model = attrs.get("model")
    browser_data = attrs.get("browser_data")
    timeout = float(attrs["timeout"]) if "timeout" in attrs else None
    collect = attrs.get("collect", "false").lower() == "true"

    # Parse todo list: pipe-separated steps in the "todos" attribute
    # Use \| as escaped pipe within a step; split on unescaped |
    todos_raw = attrs.get("todos", "")
    todos_list: list[str] | None = None
    if todos_raw:
        import re as _re_pipe
        # Split on | that is NOT preceded by backslash
        parts = _re_pipe.split(r"(?<!\\)\|", todos_raw)
        todos_list = [t.strip().replace("\\|", "|") for t in parts if t.strip()]

    try:
        from engine.agents import get_runtime
        from engine.agents.agent import Agent
        from engine.agents.registry import get_role_config, get_next_account
        from engine.config import _SYSTEM as _AGENT_SYSTEM_DIR
        from server.database import insert_agent_run

        runtime = get_runtime()
        role_cfg = get_role_config(role)

        # Resolve browser_data_dir: explicit tag attr > round-robin pool > None (active)
        if not browser_data:
            assigned_account = get_next_account(role)
            if assigned_account:
                acct_profile = _AGENT_SYSTEM_DIR / assigned_account
                if acct_profile.is_dir():
                    browser_data = str(acct_profile)

        # Check capacity
        if runtime.active_count >= runtime._max_agents:
            raise RuntimeError(f"Max agents ({runtime._max_agents}) reached")

        # Create agent (sync — just a dataclass)
        agent = Agent(
            role=role,
            task=task_text,
            context=context,
            instruction=instruction,
            model=model or role_cfg.default_model,
            browser_data_dir=browser_data,
            chat_id=_chat_id_var.get(None) or "default",
            collect=collect,
        )

        # Attach todo list if provided
        if todos_list:
            from engine.agents.agent import AgentTodoList
            agent.todos = AgentTodoList.build_from_list(todos_list)

        # Attach fallback chain from role config
        agent.model_chain = role_cfg.model_chain

        runtime._agents[agent.id] = agent

        # DB insert (sync — sqlite3)
        insert_agent_run(
            agent_id=agent.id,
            chat_id=agent.chat_id,
            role=agent.role,
            task=agent.task,
            path=agent.path,
            depth=agent.depth,
            parent_agent_id=agent.parent_id,
            model=agent.model,
            browser_data_dir=agent.browser_data_dir,
        )

        # Schedule async execution on the main event loop (we may be in a thread-pool worker)
        loop = runtime._loop
        if loop is None:
            raise RuntimeError("Event loop not cached on runtime — server startup issue")
        sem = runtime._qwen_sem if "qwen" in agent.model else runtime._ds_sem
        agent_timeout = timeout or role_cfg.default_timeout
        future = asyncio.run_coroutine_threadsafe(
            runtime._run_agent(agent, sem, agent_timeout), loop
        )
        runtime._tasks[agent.id] = future

        # Safety: log unhandled exceptions from the background coroutine
        def _on_done(fut, _aid=agent.id):
            if fut.cancelled():
                return
            exc = fut.exception()
            if exc:
                import logging
                logging.getLogger("sable").error(
                    "[agent %s] unhandled exception in _run_agent: %s", _aid, exc
                )
        future.add_done_callback(_on_done)

        # Push spawn event to SSE clients (thread-safe — we're in a worker thread)
        from server.api.routes.agents import push_agent_event
        loop.call_soon_threadsafe(push_agent_event, agent.chat_id, {
            "type": "agent_spawned",
            "agent_id": agent.id,
            "data": {"role": agent.role, "task": agent.task, "model": agent.model},
        })

        result = {
            "agent_id": agent.id,
            "role": agent.role,
            "model": agent.model,
            "status": "spawned",
            "collect": collect,
            "todos_raw": todos_raw or None,
        }
        yield _output_event(tag_id, f"Agent spawned: {agent.id} ({role}) — running in background.")
        yield _end_event(tag_id, name, True, started, result=result)

    except RuntimeError as exc:
        yield _output_event(tag_id, f"SPAWN FAILED: {exc}")
        yield _end_event(tag_id, name, False, started, error=str(exc))
    except Exception as exc:
        yield _output_event(tag_id, f"ERROR: {type(exc).__name__}: {exc}")
        yield _end_event(tag_id, name, False, started, error=f"{type(exc).__name__}: {exc}")


def handle_agent_status(
    tag_id: str, name: str, attrs: dict[str, str], content: str
) -> Generator[dict[str, Any], None, None]:
    """List all agents and their status."""
    started = time.time()

    try:
        from engine.agents import get_runtime

        runtime = get_runtime()
        chat_id = _chat_id_var.get(None)
        agents = runtime.list_agents(chat_id)

        if not agents:
            yield _output_event(tag_id, "No agents spawned yet.")
            yield _end_event(tag_id, name, True, started, result={"count": 0})
            return

        lines = []
        for a in agents:
            icon = {"spawned": "⏳", "running": "🔄", "completed": "✓", "failed": "✗"}.get(
                a.status.value, "?"
            )
            dur = f"{a.duration:.1f}s" if a.completed_at else "running..."
            lines.append(f"{icon} [{a.id}] {a.role} — {a.task[:50]} ({dur})")

        yield _output_event(tag_id, "\n".join(lines))
        yield _end_event(tag_id, name, True, started, result={"count": len(agents)})

    except Exception as exc:
        yield _output_event(tag_id, f"ERROR: {type(exc).__name__}: {exc}")
        yield _end_event(tag_id, name, False, started, error=str(exc))


def handle_kill_agent(
    tag_id: str, name: str, attrs: dict[str, str], content: str
) -> Generator[dict[str, Any], None, None]:
    """Kill a running agent by cancelling its task."""
    started = time.time()
    agent_id = attrs.get("id", content.strip())
    if not agent_id:
        yield _output_event(tag_id, "ERROR: No agent ID specified")
        yield _end_event(tag_id, name, False, started, error="No ID")
        return

    try:
        from engine.agents import get_runtime
        from server.database import update_agent_status

        runtime = get_runtime()
        task = runtime._tasks.get(agent_id)
        if task and not task.done():
            task.cancel()
        if agent_id in runtime._agents:
            agent = runtime._agents[agent_id]
            agent.mark_failed("Killed by orchestrator")
            update_agent_status(agent_id, "killed", error="Killed by orchestrator")
            # Emit SSE event so frontend updates top bar + panel
            from server.api.routes.agents import push_agent_event
            loop = runtime._loop
            if loop:
                loop.call_soon_threadsafe(push_agent_event, agent.chat_id, {
                    "type": "agent_failed",
                    "agent_id": agent_id,
                    "data": {"role": agent.role, "error": "Killed by orchestrator"},
                })
            yield _output_event(tag_id, f"Agent {agent_id} killed.")
            yield _end_event(tag_id, name, True, started, result={"killed": agent_id})
        else:
            yield _output_event(tag_id, f"Agent {agent_id} not found.")
            yield _end_event(tag_id, name, False, started, error="Not found")

    except Exception as exc:
        yield _output_event(tag_id, f"ERROR: {type(exc).__name__}: {exc}")
        yield _end_event(tag_id, name, False, started, error=str(exc))


# ---------------------------------------------------------------------------
# Todo progression tools — injected only into subagent prompts with TODO lists
# ---------------------------------------------------------------------------

def handle_todo_complete(
    tag_id: str, name: str, attrs: dict[str, str], content: str,
    *, agent=None,
) -> Generator[dict[str, Any], None, None]:
    """Mark the current TODO item as completed and advance to the next.

    Called by subagents via tool_call. The `agent` kwarg is injected by the
    loop dispatcher (not from tag attrs).
    """
    started = time.time()

    if agent is None or agent.todos is None:
        yield _output_event(tag_id, "ERROR: todo_complete called without an active TODO list.")
        yield _end_event(tag_id, name, False, started, error="No TODO list")
        return

    summary = attrs.get("summary", content.strip() or "completed")

    if agent.todos.all_done:
        yield _output_event(tag_id, "All tasks are already complete.")
        yield _end_event(tag_id, name, True, started, result={"status": "already_done"})
        return

    # Mark current as completed with summary
    agent.todos.current.result = summary
    nxt = agent.todos.advance()

    # Emit SSE progress event
    agent.push_stream_event({
        "type": "todo_progress",
        "progress": agent.todos.progress,
        "current": nxt.content if nxt else None,
        "todos": [
            {"id": t.id, "content": t.content, "status": t.status, "subtasks": t.subtasks, "result": t.result}
            for t in agent.todos.todos
        ],
    })

    if agent.todos.all_done:
        msg = f"Task completed: \"{summary}\". All tasks done ({agent.todos.progress}). Provide your final answer now."
    else:
        msg = f"Task completed: \"{summary}\". Next task: \"{nxt.content}\". Continue working."

    yield _output_event(tag_id, msg)
    yield _end_event(tag_id, name, True, started, result={
        "status": "advanced",
        "completed_summary": summary,
        "next_task": nxt.content if nxt else None,
        "all_done": agent.todos.all_done,
        "progress": agent.todos.progress,
    })


def handle_todo_skip(
    tag_id: str, name: str, attrs: dict[str, str], content: str,
    *, agent=None,
) -> Generator[dict[str, Any], None, None]:
    """Skip the current TODO item with a reason and advance to the next."""
    started = time.time()

    if agent is None or agent.todos is None:
        yield _output_event(tag_id, "ERROR: todo_skip called without an active TODO list.")
        yield _end_event(tag_id, name, False, started, error="No TODO list")
        return

    reason = attrs.get("reason", content.strip() or "no reason provided")

    if agent.todos.all_done:
        yield _output_event(tag_id, "All tasks are already complete. Nothing to skip.")
        yield _end_event(tag_id, name, True, started, result={"status": "already_done"})
        return

    skipped_id = agent.todos.current.id
    skipped_content = agent.todos.current.content

    # Record skip reason
    agent.todos.skip_reasons.append((skipped_id, reason))
    agent.todos.current.result = f"SKIPPED: {reason}"

    nxt = agent.todos.skip_current_and_advance()

    # Emit SSE progress event
    agent.push_stream_event({
        "type": "todo_progress",
        "progress": agent.todos.progress,
        "current": nxt.content if nxt else None,
        "todos": [
            {"id": t.id, "content": t.content, "status": t.status, "subtasks": t.subtasks, "result": t.result}
            for t in agent.todos.todos
        ],
    })

    if agent.todos.all_done:
        msg = f"Skipped task #{skipped_id} \"{skipped_content}\": {reason}. All remaining tasks done. Provide your final answer."
    else:
        msg = f"Skipped task #{skipped_id} \"{skipped_content}\": {reason}. Next task: \"{nxt.content}\". Continue working."

    yield _output_event(tag_id, msg)
    yield _end_event(tag_id, name, True, started, result={
        "status": "skipped",
        "skipped_id": skipped_id,
        "skipped_content": skipped_content,
        "reason": reason,
        "next_task": nxt.content if nxt else None,
        "all_done": agent.todos.all_done,
        "progress": agent.todos.progress,
    })
