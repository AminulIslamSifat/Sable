"""Multi-agent handlers: spawn_agent, agent_status, kill_agent, teacher_guidance.

Architecture: Subagents use the EXACT same pipeline as main chat.
spawn_agent emits SSE events (agent_start + agent_trigger) that cause the
frontend to call POST /api/chat with the agent's chat_id, model, and system prompt.
No isolated LLM loop, no skill filtering, no extra parsing logic.
The only difference from main chat: persona comes from instruction/agents/{role}.md.

teacher_guidance routes Maria's guidance back to a waiting agent stream.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Generator
from typing import Any

from engine.agents import current_chat_id as _chat_id_var
from engine.skills.handlers.common import _end_event, _output_event

logger = logging.getLogger("sable")


def handle_spawn_agent(
    tag_id: str, name: str, attrs: dict[str, str], content: str
) -> Generator[dict[str, Any], None, None]:
    """Spawn a subagent using the main chat pipeline.

    Emits agent_start (UI card creation) and agent_trigger (frontend fires
    POST /api/chat) SSE events. The frontend handles streaming exactly like
    a normal chat turn.
    """
    started = time.time()

    task_text = attrs.get("task", content.strip())
    if not task_text:
        yield _output_event(tag_id, "ERROR: No task specified for spawn_agent")
        yield _end_event(tag_id, name, False, started, error="No task specified")
        return

    role = attrs.get("role", "researcher")
    context = attrs.get("context")
    instruction = attrs.get("instruction")
    model_override = attrs.get("model")
    browser_data = attrs.get("browser_data")
    collect = attrs.get("collect", "false").lower() == "true"

    try:
        from engine.agents import get_runtime
        from engine.agents.agent import Agent
        from engine.agents.registry import get_role_config, get_next_account
        from engine.config import _SYSTEM as _AGENT_SYSTEM_DIR
        from server.database import insert_agent_run, ensure_chat, touch_chat

        runtime = get_runtime()
        role_cfg = get_role_config(role)
        parent_chat_id = _chat_id_var.get(None) or "default"

        # Resolve model: explicit override > role default > main chat model
        agent_model = model_override or role_cfg.default_model

        # Resolve browser_data_dir: explicit > reverse pool account (skip busy) > None
        if not browser_data:
            _busy_accounts: set[str] = set()
            for a in runtime._agents.values():
                if a.browser_data_dir and a.status.value == "running":
                    from pathlib import PurePosixPath
                    _busy_accounts.add(PurePosixPath(a.browser_data_dir).name)
            assigned_account = get_next_account(role, in_use=_busy_accounts)
            if assigned_account:
                acct_profile = _AGENT_SYSTEM_DIR / assigned_account
                if acct_profile.is_dir():
                    browser_data = str(acct_profile)

        # Check capacity
        if runtime.active_count >= runtime._max_agents:
            raise RuntimeError(f"Max agents ({runtime._max_agents}) reached")

        # Build system prompt: role persona + optional orchestrator instruction
        # No skill filtering, no tool guide injection — main chat pipeline handles all of it
        system_prompt = role_cfg.system_prompt
        if instruction:
            system_prompt += f"\n\nSpecial instruction from orchestrator: {instruction}"

        # Create agent dataclass for lifecycle tracking
        agent = Agent(
            role=role,
            task=task_text,
            context=context,
            instruction=instruction,
            model=agent_model,
            browser_data_dir=browser_data,
            chat_id=parent_chat_id,
            collect=collect,
        )
        agent.system_prompt = system_prompt
        agent.model_chain = role_cfg.model_chain
        agent.mark_running()
        runtime._agents[agent.id] = agent

        # Ensure agent chat exists in DB (for history/sidebar)
        ensure_chat(
            chat_id=agent.id,
            title=f"[{role}] {task_text[:60]}",
            parent_id=parent_chat_id,
            mode="agent",
        )
        touch_chat(agent.id)

        # Persist agent run metadata
        insert_agent_run(
            agent_id=agent.id,
            chat_id=parent_chat_id,
            role=role,
            task=task_text,
            path=agent.path,
            depth=agent.depth,
            parent_agent_id=agent.parent_id,
            model=agent_model,
            browser_data_dir=browser_data,
        )

        # Sync context with agent's role instruction (not main chat persona)
        if browser_data and "qwen" in agent_model:
            try:
                import asyncio as _asyncio
                loop = runtime._loop
                if loop:
                    _asyncio.run_coroutine_threadsafe(
                        _sync_agent_context(browser_data, system_prompt),
                        loop,
                    )
            except Exception as exc:
                logger.warning("Agent %s: sync_context dispatch failed: %s", agent.id, exc)

        # Build the message for the agent's chat turn
        if context:
            agent_message = f"Context: {context}\n\nTask: {task_text}"
        else:
            agent_message = f"Task: {task_text}"

        # Emit agent_start - frontend creates the UI card in top bar
        yield {
            "type": "agent_start",
            "id": agent.id,
            "role": role,
            "task": task_text[:500],
            "model": agent_model,
        }

        # Emit agent_trigger - frontend fires POST /api/chat for this agent
        yield {
            "type": "agent_trigger",
            "id": agent.id,
            "message": agent_message,
            "system_prompt": system_prompt,
            "model": agent_model,
            "browser_data_dir": browser_data or None,
            "collect": collect,
        }

        # Signal skill_end so the main chat tool loop continues
        yield _end_event(tag_id, name, True, started, result={
            "agent_id": agent.id,
            "role": role,
            "model": agent_model,
            "status": "spawned",
            "collect": collect,
        })

    except RuntimeError as exc:
        yield _output_event(tag_id, f"SPAWN FAILED: {exc}")
        yield _end_event(tag_id, name, False, started, error=str(exc))
    except Exception as exc:
        logger.error("spawn_agent failed: %s", exc, exc_info=True)
        yield _output_event(tag_id, f"ERROR: {type(exc).__name__}: {exc}")
        yield _end_event(tag_id, name, False, started, error=f"{type(exc).__name__}: {exc}")


async def _sync_agent_context(browser_data_dir: str, system_prompt: str) -> bool:
    """Push agent instructions via Qwen personalization API before first turn."""
    try:
        from engine.service import ChatService
        svc = ChatService(user_data_dir=browser_data_dir)
        try:
            synced = await svc.sync_context(custom_instructions=system_prompt)
            if synced:
                logger.info("Agent sync_context succeeded for profile %s", browser_data_dir)
            return synced
        finally:
            await svc.close()
    except Exception as exc:
        logger.warning("Agent sync_context failed: %s", exc)
        return False


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
            lines.append(f"{icon} [{a.id}] {a.role} - {a.task[:50]} ({dur})")

        yield _output_event(tag_id, "\n".join(lines))
        yield _end_event(tag_id, name, True, started, result={"count": len(agents)})

    except Exception as exc:
        yield _output_event(tag_id, f"ERROR: {type(exc).__name__}: {exc}")
        yield _end_event(tag_id, name, False, started, error=str(exc))


def handle_kill_agent(
    tag_id: str, name: str, attrs: dict[str, str], content: str
) -> Generator[dict[str, Any], None, None]:
    """Kill a running agent by ID."""
    started = time.time()
    agent_id = attrs.get("agent_id", content.strip())

    if not agent_id:
        yield _output_event(tag_id, "ERROR: No agent_id specified")
        yield _end_event(tag_id, name, False, started, error="No agent_id")
        return

    try:
        from engine.agents import get_runtime
        runtime = get_runtime()
        agent = runtime.get_agent(agent_id)

        if not agent:
            yield _output_event(tag_id, f"Agent {agent_id} not found.")
            yield _end_event(tag_id, name, False, started, error="Not found")
            return

        agent.cancelled = True
        agent.mark_failed("Killed by orchestrator")

        from server.database import update_agent_status
        update_agent_status(agent_id, "killed", error="Killed by orchestrator")

        yield _output_event(tag_id, f"Agent {agent_id} killed.")
        yield _end_event(tag_id, name, True, started, result={"agent_id": agent_id, "status": "killed"})

    except Exception as exc:
        yield _output_event(tag_id, f"ERROR: {type(exc).__name__}: {exc}")
        yield _end_event(tag_id, name, False, started, error=str(exc))


def handle_teacher_guidance(
    tag_id: str, name: str, attrs: dict[str, str], content: str,
) -> Generator[dict[str, Any], None, None]:
    """Handle teacher_guidance tool call from main chat.

    Main chat (Maria) responds to a subagent's teacher escalation request
    by calling this tool with guidance. The response is routed back to the
    waiting subagent via auto_turn engine.
    """
    started = time.time()

    agent_id = attrs.get("agent_id", "").strip()
    guidance = attrs.get("guidance", content.strip())

    if not agent_id:
        yield _output_event(tag_id, "ERROR: teacher_guidance requires agent_id")
        yield _end_event(tag_id, name, False, started, error="Missing agent_id")
        return

    if not guidance:
        yield _output_event(tag_id, "ERROR: teacher_guidance requires guidance text")
        yield _end_event(tag_id, name, False, started, error="Missing guidance")
        return

    try:
        from engine.agents.auto_turn import auto_turn
        auto_turn.resolve_teacher_guidance(agent_id, guidance)
        yield _output_event(tag_id, f"Teacher guidance delivered to agent {agent_id}.")
        yield _end_event(tag_id, name, True, started, result={
            "agent_id": agent_id,
            "delivered": True,
        })
    except Exception as exc:
        yield _output_event(tag_id, f"ERROR delivering guidance: {exc}")
        yield _end_event(tag_id, name, False, started, error=str(exc))
