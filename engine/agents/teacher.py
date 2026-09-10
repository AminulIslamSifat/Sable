"""Teacher escalation — when an agent is stuck, Maria or a stronger model intervenes.

Simplified: no todo management. The teacher reviews the agent's task and recent
attempts, then provides actionable guidance text that gets injected into the
agent's conversation via auto_turn engine.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from engine.agents.agent import Agent

logger = logging.getLogger("sable")

# Default teacher model — always the strongest available
DEFAULT_TEACHER_MODEL = "qwen3.8-max"

# Max teacher interventions per agent before giving up
MAX_TEACHER_INTERVENTIONS = 2

_TEACHER_SYSTEM_PROMPT = """\
You are a senior mentor intervening to help a stuck AI agent.

The agent below is failing to complete its task. Analyze what it's doing wrong, \
then respond with a JSON object:

{
  "diagnosis": "What the agent is doing wrong (1-2 sentences)",
  "guidance": "Specific actionable instructions for the agent (2-4 sentences)"
}

Rules:
- Keep guidance concrete and actionable. No vague "try harder" advice.
- If the agent is looping on the same approach, tell it exactly what to try instead.
- Respond ONLY with the JSON object. No markdown, no explanation outside it.
"""


def _load_teacher_config() -> dict[str, Any]:
    """Load teacher settings from agent_config.json."""
    from engine.config import AGENT_CONFIG_PATH
    try:
        cfg = json.loads(AGENT_CONFIG_PATH.read_text(encoding="utf-8"))
        return cfg.get("teacher", {})
    except Exception:
        return {}


def _build_teacher_prompt(agent: Agent, stuck_reason: str) -> str:
    """Construct the prompt sent to the teacher model."""
    parts = [f"STUCK REASON: {stuck_reason}"]
    parts.append(f"AGENT ROLE: {agent.role}")
    parts.append(f"AGENT MODEL: {agent.model}")
    parts.append(f"TASK: {agent.task}")

    if agent.context:
        parts.append(f"CONTEXT: {agent.context}")

    # Include last few messages for context (truncated)
    recent = agent.messages[-6:] if len(agent.messages) > 6 else agent.messages
    msg_lines = []
    for m in recent:
        role = m.get("role", "?")
        content = m.get("content", "")[:500]
        msg_lines.append(f"[{role}]: {content}")
    parts.append("RECENT CONVERSATION:\n" + "\n".join(msg_lines))

    return "\n\n".join(parts)


async def escalate_to_teacher(agent: Agent, stuck_reason: str) -> str | None:
    """Call a stronger model to analyze why the agent is stuck.

    Returns guidance text to inject into the agent's conversation, or None on failure.
    """
    teacher_cfg = _load_teacher_config()
    if not teacher_cfg.get("enabled", True):
        return None

    teacher_model = teacher_cfg.get("model", DEFAULT_TEACHER_MODEL)
    teacher_browser = teacher_cfg.get("browser_data_dir")

    prompt = _build_teacher_prompt(agent, stuck_reason)
    logger.info(
        "[teacher] Escalating agent %s (%s) — reason: %s",
        agent.id, agent.role, stuck_reason,
    )

    try:
        from engine.config import get_model_config

        cfg = get_model_config(teacher_model)
        backend = cfg.get("api_backend")

        if backend == "deepseek":
            response = await _call_teacher_deepseek(prompt, teacher_model, teacher_browser)
        elif backend in ("gemini", "groq", "mistral"):
            response = await _call_teacher_api(prompt, teacher_model, backend)
        else:
            # Qwen scraper
            response = await _call_teacher_qwen(prompt, teacher_model, teacher_browser)

        if not response or not response.strip():
            return None

        guidance = _parse_teacher_response(response)
        return guidance

    except Exception as exc:
        logger.error("[teacher] Escalation failed for agent %s: %s", agent.id, exc)
        return None


async def _call_teacher_qwen(prompt: str, model: str, browser_data_dir: str | None) -> str:
    """One-shot Qwen call for the teacher."""
    import uuid
    from engine.config import get_qwen_tokens_for_account, _SYSTEM, URL
    from engine.session import build_headers
    from pathlib import Path
    import httpx

    account = None
    if browser_data_dir:
        account = Path(browser_data_dir).name

    headers = None
    if account:
        cached = get_qwen_tokens_for_account(account)
        if cached and cached.get("cookies"):
            headers = build_headers(
                cookies=cached["cookies"],
                bx_ua=cached.get("bx_ua"),
                bx_umt=cached.get("bx_umt"),
            )

    if not headers:
        from engine.session import get_headers
        headers = await get_headers()

    chat_id = f"teacher-{uuid.uuid4().hex[:8]}"
    payload = {
        "chat_id": chat_id,
        "content": prompt,
        "model": model,
        "feature_config": {"thinking_mode": "Fast"},
    }

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(URL, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        return data.get("content", "") or data.get("message", {}).get("content", "")


async def _call_teacher_deepseek(prompt: str, model: str, browser_data_dir: str | None) -> str:
    """One-shot DeepSeek call for the teacher."""
    import uuid
    from connectors.deepseek.client import get_client
    from engine.config import get_model_config
    from pathlib import Path

    account = None
    if browser_data_dir:
        resolved = Path(browser_data_dir).resolve()
        if resolved.name.startswith("browser-data"):
            account = resolved.name

    client = get_client(account=account)
    ds_cfg = get_model_config(model)
    api_model_type = ds_cfg.get("api_model_type") if ds_cfg else None

    accumulated = ""
    async for event in client.stream_chat(
        prompt,
        model=api_model_type,
        chat_id=f"teacher-{uuid.uuid4().hex[:8]}",
        inject_instructions=False,
        system_instruction=_TEACHER_SYSTEM_PROMPT,
    ):
        if event.get("type") == "answer":
            accumulated += event.get("text", "")
        elif event.get("type") == "error":
            raise RuntimeError(f"DeepSeek teacher: {event.get('message')}")

    return accumulated


async def _call_teacher_api(prompt: str, model: str, backend: str) -> str:
    """One-shot API call (Gemini/Groq/Mistral) for the teacher."""
    import uuid
    from connectors import get_connector
    from engine.config import get_model_config

    connector = get_connector(backend, model_id=model)
    cfg = get_model_config(model)
    api_model_type = cfg.get("api_model_type")

    accumulated = ""
    async for event in connector.stream_chat(
        prompt,
        model=api_model_type,
        chat_id=f"teacher-{uuid.uuid4().hex[:8]}",
        inject_instructions=False,
        system_instruction=_TEACHER_SYSTEM_PROMPT,
    ):
        if event.get("type") == "answer":
            accumulated += event.get("text", "")
        elif event.get("type") == "error":
            raise RuntimeError(f"{backend} teacher: {event.get('message')}")

    return accumulated


def _parse_teacher_response(response: str) -> str | None:
    """Parse the teacher's JSON response, return guidance text."""
    try:
        text = response.strip()
        if "```" in text:
            start = text.find("{")
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                text = text[start:end]

        data = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("[teacher] Could not parse teacher JSON, using raw response")
        return response[:2000]

    diagnosis = data.get("diagnosis", "")
    guidance = data.get("guidance", "")

    parts = []
    if diagnosis:
        parts.append(f"DIAGNOSIS: {diagnosis}")
    if guidance:
        parts.append(f"GUIDANCE: {guidance}")

    return "\n".join(parts) if parts else None
