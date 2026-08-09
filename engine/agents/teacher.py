
"""Teacher escalation — when an agent is stuck, a stronger model intervenes.

The teacher reviews the agent's task, todo list, and recent attempts,
then provides guidance and optionally restructures the todo list.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from engine.agents.agent import Agent, AgentTodoList, TodoItem

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
  "guidance": "Specific actionable instructions for the agent (2-4 sentences)",
  "todo_updates": [
    {"action": "add", "content": "new step to add"},
    {"action": "remove", "id": 3},
    {"action": "replace", "id": 2, "content": "replacement text"},
    {"action": "skip", "id": 4}
  ]
}

Rules:
- todo_updates is optional. Only include it if the plan itself is flawed.
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

    if agent.todos and agent.todos.todos:
        todo_lines = []
        for t in agent.todos.todos:
            status_icon = {"completed": "✅", "in_progress": "🔧", "pending": "❌", "skipped": "⏭️"}.get(t.status, "?")
            line = f"  {status_icon} [{t.id}] {t.content}"
            if t.result:
                line += f" → {t.result}"
            for sub in t.subtasks:
                line += f"\n     • {sub}"
            todo_lines.append(line)
        parts.append("TODO LIST:\n" + "\n".join(todo_lines))

    # Include last few messages for context (truncated)
    recent = agent.messages[-6:] if len(agent.messages) > 6 else agent.messages
    msg_lines = []
    for m in recent:
        role = m.get("role", "?")
        content = m.get("content", "")[:500]
        msg_lines.append(f"[{role}]: {content}")
    parts.append("RECENT CONVERSATION:\n" + "\n".join(msg_lines))

    return "\n\n".join(parts)


def _apply_todo_updates(agent: Agent, updates: list[dict[str, Any]]) -> None:
    """Apply the teacher's todo modifications to the agent's todo list."""
    if not agent.todos or not updates:
        return

    for upd in updates:
        action = upd.get("action", "").lower()

        if action == "add":
            content = upd.get("content", "").strip()
            if content:
                new_id = max((t.id for t in agent.todos.todos), default=0) + 1
                agent.todos.todos.append(TodoItem(id=new_id, content=content, status="pending"))
                logger.info("[teacher] Added todo #%d: %s", new_id, content)

        elif action == "remove":
            tid = upd.get("id")
            agent.todos.todos = [t for t in agent.todos.todos if t.id != tid]
            logger.info("[teacher] Removed todo #%s", tid)

        elif action == "replace":
            tid = upd.get("id")
            content = upd.get("content", "").strip()
            if content:
                for t in agent.todos.todos:
                    if t.id == tid:
                        t.content = content
                        logger.info("[teacher] Replaced todo #%d: %s", tid, content)
                        break

        elif action == "skip":
            tid = upd.get("id")
            for t in agent.todos.todos:
                if t.id == tid:
                    t.status = "skipped"
                    logger.info("[teacher] Skipped todo #%d", tid)
                    break

    # Post-update fixup: remove skipped items that are AFTER current position,
    # keep skipped items before/at current (for progress display).
    # Use positional index, not ID, to determine "before current".
    current_id = agent.todos.todos[agent.todos.current_index].id if (
        0 <= agent.todos.current_index < len(agent.todos.todos)
    ) else None

    filtered = []
    for pos, t in enumerate(agent.todos.todos):
        if t.status == "skipped" and pos > agent.todos.current_index:
            continue  # Drop skipped items ahead of current position
        filtered.append(t)
    agent.todos.todos = filtered

    # Re-locate current_index by finding the item that was current
    if current_id is not None:
        for i, t in enumerate(agent.todos.todos):
            if t.id == current_id:
                agent.todos.current_index = i
                break
        else:
            # Current item was removed — clamp to valid range
            agent.todos.current_index = min(agent.todos.current_index, len(agent.todos.todos) - 1)
    else:
        agent.todos.current_index = 0

    # Handle empty list after removals
    if not agent.todos.todos:
        agent.todos.current_index = 0  # all_done will be True (empty list check)
        return

    # Clamp index to valid range
    if agent.todos.current_index >= len(agent.todos.todos):
        agent.todos.current_index = len(agent.todos.todos) - 1

    # Auto-advance past skipped items at current position
    while agent.todos.current and agent.todos.current.status == "skipped":
        agent.todos.current_index += 1
    if agent.todos.current_index >= len(agent.todos.todos):
        agent.todos.current_index = len(agent.todos.todos)  # all_done = True
        return

    # Ensure current item is in_progress
    if agent.todos.current and agent.todos.current.status == "pending":
        agent.todos.current.status = "in_progress"


async def escalate_to_teacher(agent: Agent, stuck_reason: str) -> str | None:
    """Call the teacher model for intervention. Returns guidance text or None on failure.

    The teacher analyzes the agent's state and returns structured guidance.
    If todo_updates are provided, they're applied to the agent's todo list.
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

        # Parse the teacher's JSON response
        guidance = _parse_teacher_response(agent, response)
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
        # Fall back to default tokens
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
        # Qwen returns content in the response
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


def _parse_teacher_response(agent: Agent, response: str) -> str | None:
    """Parse the teacher's JSON response, apply todo updates, return guidance text."""
    try:
        # Try to extract JSON from the response
        text = response.strip()
        # Handle case where model wraps JSON in markdown code fences
        if "```" in text:
            start = text.find("{")
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                text = text[start:end]

        data = json.loads(text)
    except json.JSONDecodeError:
        # If JSON parsing fails, return the raw response as guidance
        logger.warning("[teacher] Could not parse teacher JSON, using raw response")
        return response[:2000]

    diagnosis = data.get("diagnosis", "")
    guidance = data.get("guidance", "")
    todo_updates = data.get("todo_updates", [])

    # Apply todo modifications
    if todo_updates:
        _apply_todo_updates(agent, todo_updates)

    # Build the guidance message to inject into the agent
    parts = []
    if diagnosis:
        parts.append(f"DIAGNOSIS: {diagnosis}")
    if guidance:
        parts.append(f"GUIDANCE: {guidance}")
    if todo_updates:
        parts.append("Your todo list has been updated. Check the progress tracker below.")

    return "\n".join(parts) if parts else None
