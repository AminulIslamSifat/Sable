"""Agent LLM loop — DEPRECATED.

Subagents now use the main chat pipeline (POST /api/chat) via frontend SSE events.
This module exists only as a compatibility shim for the scheduler.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("sable")


async def run_agent_llm_loop(
    agent: Any,
    breakers: dict[str, Any] | None = None,
    limits: dict[str, int] | None = None,
) -> str:
    """DEPRECATED: Subagents now stream through POST /api/chat.

    This shim fires an internal HTTP request to the chat endpoint so the
    scheduler can still trigger agents without a browser frontend.
    """
    import httpx

    role_cfg = None
    try:
        from engine.agents.registry import get_role_config
        role_cfg = get_role_config(agent.role)
    except Exception:
        pass

    system_prompt = getattr(agent, "system_prompt", "") or ""
    if not system_prompt and role_cfg:
        system_prompt = role_cfg.system_prompt
    if agent.instruction:
        system_prompt += f"\n\nSpecial instruction from orchestrator: {agent.instruction}"

    message = f"Task: {agent.task}"
    if agent.context:
        message = f"Context: {agent.context}\n\nTask: {agent.task}"

    payload = {
        "message": message,
        "chat_id": agent.id,
        "model": agent.model,
        "stream": False,
    }
    if system_prompt:
        payload["system_prompt"] = system_prompt
    if agent.browser_data_dir:
        payload["browser_data_dir"] = agent.browser_data_dir

    try:
        async with httpx.AsyncClient(timeout=limits.get("timeout", 600) if limits else 600) as client:
            resp = await client.post("http://127.0.0.1:8080/api/chat", json=payload)
            resp.raise_for_status()
            data = resp.json()
            answer = data.get("answer", "")
            agent.mark_completed(answer)
            return answer
    except Exception as exc:
        error_msg = f"Agent loop failed: {exc}"
        logger.error(error_msg)
        agent.mark_failed(error_msg)
        raise
