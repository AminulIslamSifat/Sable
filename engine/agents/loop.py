
"""Agent LLM loop — isolated from main chat.

Both Qwen and DeepSeek are session-based scraping APIs: the provider stores
conversation history server-side. We send ONE message per turn + parent_id.
The local messages list is for DB persistence and history viewing only.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from pathlib import Path
from typing import Any

from engine.agents.agent import Agent
from engine.agents.resilience import CircuitBreaker, LoopDetector
from engine.agents.registry import get_role_config

logger = logging.getLogger("sable")

# Defaults — overridden by settings > agent > limits
MAX_ITERATIONS = 25
MAX_CONTEXT_CHARS = 12000

STUCK_MESSAGE = (
    "You've called the same tool repeatedly with identical arguments. "
    "This approach isn't working. Summarize what you have and report findings, "
    "or try a completely different strategy."
)

FORMAT_REMINDERS: dict[str, str] = {
    "researcher": 'Your last response was not valid JSON. Respond with ONLY: {"topic": "...", "sources": [...], "findings": [...], "summary": "...", "confidence": "high|medium|low"}',
    "coder": 'Your last response was not valid JSON. Respond with ONLY: {"description": "...", "files_modified": [{"path": "...", "lines": "...", "change": "..."}], "tests": "pass|fail|skipped", "notes": "..."}',
    "reviewer": 'Your last response was not valid JSON. Respond with ONLY: {"file": "...", "critical": [...], "warnings": [...], "info": [...], "verdict": "approve|request_changes|needs_discussion"}',
    "writer": 'Your last response was not valid JSON. Respond with ONLY: {"title": "...", "path": "...", "structure": [...], "word_count": N, "notes": "..."}',
    "utility": 'Your last response was not valid JSON. Respond with ONLY: {"task": "...", "actions_taken": [...], "result": "...", "notes": "..."}',
}

_TAG_RE = re.compile(r"<(action)>(.*?)</\1>", re.DOTALL)
# Matches both <tag attrs>content</tag> and <tag attrs />
_INNER_TAG_RE = re.compile(
    r"<(\w+)\s*((?:[^>\"']|\"[^\"]*\"|'[^']*')*?)\s*(?:/>\s*$|>(.*?)</\1\s*>|>)",
    re.DOTALL | re.MULTILINE,
)



# --------------------------------------------------------------------------
# Tool documentation injected into agent system prompts
# --------------------------------------------------------------------------

# Universal execute_command docs — always injected for every agent
_EXECUTE_COMMAND_DOC = """\
## execute_command (universal)
Run a shell command. Returns stdout+stderr. 15s timeout.
Usage:
  <execute_command>your command here</execute_command>
Examples:
  <execute_command>ls -la /home</execute_command>
  <execute_command>python3 script.py --flag</execute_command>
  <execute_command>grep -rn "pattern" /path --include="*.py"</execute_command>
Rules:
- Always use absolute paths.
- For long-running commands (>15s), use execute_background_command if available.
- Sudo password is <pass> — use: echo <pass> | sudo -S <command>
"""

_SKILLS_DIR = Path(__file__).resolve().parent.parent.parent / "skills"


def _load_skill_instruction(skill_key: str) -> str | None:
    """Load instruction.md for a skill, replacing SKILL_DIR placeholder."""
    instr_path = _SKILLS_DIR / skill_key / "instruction.md"
    if not instr_path.is_file():
        return None
    try:
        content = instr_path.read_text(encoding="utf-8").strip()
        scripts_dir = str(_SKILLS_DIR / skill_key / "scripts")
        content = content.replace("SKILL_DIR", scripts_dir)
        return content
    except OSError:
        return None


def _build_tool_guide(allowed_skills: list[str], default_skills: list[str]) -> str:
    """Build tool usage guide from skill keys.

    - default_skills: full instruction.md auto-injected into prompt
    - allowed_skills (minus defaults): compact listing (name + instruction path)
    - execute_command: always universal, hardcoded docs
    """
    from engine.agents.registry import get_universal_skills

    A = chr(60)  # <
    Z = chr(62)  # >
    lines = [
        "\n\n## Available Tools",
        "To call a tool, output exactly this structure (one per response):",
        f"  {A}action{Z}",
        f'  {A}tag_name attr="value"{Z}content{A}/tag_name{Z}',
        f"  {A}/action{Z}",
        "",
        "Rules:",
        "- Exactly ONE action block per response. Wait for the result before continuing.",
        "- Use absolute paths for all file operations.",
        "- After getting tool output, analyze it and decide next step.",
        "- When done with all tools, output your final JSON answer with NO action block.",
        "",
    ]

    # Universal: execute_command always available
    lines.append(_EXECUTE_COMMAND_DOC)

    # Default skills: full instruction.md injected
    defaults = set(default_skills)
    for skill_key in default_skills:
        if skill_key == "execute_command":
            continue
        instr = _load_skill_instruction(skill_key)
        if instr:
            lines.append(f"\n## {skill_key} (default)\n{instr}\n")

    # Allowed but not default: compact listing with trigger/description
    extra = [s for s in allowed_skills if s not in defaults and s != "execute_command"]
    if extra:
        from engine.skills.registry import discover_skills
        skill_meta = {s.key: s for s in discover_skills(_SKILLS_DIR)}

        lines.append("\n## Additional Allowed Skills")
        lines.append("Available on demand — read their instruction.md via execute_command before use.\n")
        for skill_key in extra:
            meta = skill_meta.get(skill_key)
            if meta:
                lines.append(f"### {meta.name}")
                lines.append(f"* **Trigger:** {meta.trigger}")
                if meta.not_this_if:
                    lines.append(f"* **Not this if:** {meta.not_this_if}")
                lines.append(f"* **Instruction:** `{_SKILLS_DIR / skill_key / 'instruction.md'}`")
            else:
                lines.append(f"### {skill_key}")
                lines.append(f"* **Instruction:** `{_SKILLS_DIR / skill_key / 'instruction.md'}`")
            lines.append("")

    return "\n".join(lines)


async def run_agent_llm_loop(
    agent: Agent,
    breakers: dict[str, CircuitBreaker],
    limits: dict[str, int] | None = None,
) -> str:
    """Execute the full agent loop. Returns final JSON answer text."""
    role_cfg = get_role_config(agent.role)
    lim = limits or {}
    max_iterations = lim.get("max_iterations", MAX_ITERATIONS)
    loop_detector = LoopDetector(
        max_consecutive=lim.get("max_consecutive_tool_calls", 15),
        max_total=lim.get("max_total_tool_calls", 50),
    )

    backend = "qwen" if "qwen" in agent.model else "deepseek"
    breaker = breakers[backend]

    if not breaker.can_execute():
        raise RuntimeError(f"Circuit breaker open for {backend} — provider unavailable")

    # Build first message: system prompt + tool guide + task
    system_prompt = role_cfg.system_prompt
    system_prompt += _build_tool_guide(role_cfg.allowed_skills, role_cfg.default_skills)
    if agent.instruction:
        system_prompt += f"\n\nSpecial instruction from orchestrator: {agent.instruction}"

    first_message = system_prompt
    if agent.context:
        first_message += f"\n\nContext: {agent.context}\n\nTask: {agent.task}"
    else:
        first_message += f"\n\nTask: {agent.task}"

    # Track conversation for DB/history (not sent to API)
    agent.messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Context: {agent.context}\n\nTask: {agent.task}" if agent.context else agent.task},
    ]
    await _persist_message(agent.id, "system", system_prompt)
    await _persist_message(agent.id, "user", agent.messages[1]["content"])

    # Session state
    parent_id: str | None = None  # Qwen parent tracking
    is_first_turn = True

    # Main loop
    current_message = first_message

    for iteration in range(max_iterations):
        agent.push_stream_event({"type": "iteration", "iteration": iteration + 1})
        # Call LLM
        response_text, new_parent_id = await _send_with_retry(
            agent, current_message, parent_id, breaker, is_first_turn
        )
        if new_parent_id:
            parent_id = new_parent_id
        is_first_turn = False

        agent.messages.append({"role": "assistant", "content": response_text})
        await _persist_message(agent.id, "assistant", response_text)

        # Stream the response text to the panel
        agent.push_stream_event({"type": "answer", "text": response_text})

        # Parse skill tags
        tags = _parse_skill_tags(response_text)
        if not tags:
            # No tool calls → validate as final JSON answer
            if _validate_json_output(response_text, role_cfg.required_json_keys):
                return response_text
            # Malformed → one re-prompt
            reminder = FORMAT_REMINDERS.get(agent.role, "Provide a valid JSON object as your final answer.")
            agent.messages.append({"role": "user", "content": reminder})
            await _persist_message(agent.id, "user", reminder)
            response_text, new_parent_id = await _send_with_retry(
                agent, reminder, parent_id, breaker, False
            )
            if new_parent_id:
                parent_id = new_parent_id
            agent.messages.append({"role": "assistant", "content": response_text})
            await _persist_message(agent.id, "assistant", response_text)
            return response_text  # Accept even if still malformed (degraded)

        # Loop detection
        stuck = False
        for tag in tags:
            if not loop_detector.check(tag["name"], str(tag.get("attrs", ""))):
                stuck = True
                break
        if stuck:
            agent.messages.append({"role": "user", "content": STUCK_MESSAGE})
            await _persist_message(agent.id, "user", STUCK_MESSAGE)
            current_message = STUCK_MESSAGE
            continue

        # Execute skills
        tool_results = []
        for tag in tags:
            tag_name = tag["name"]

            try:
                from engine.skills import get_skill_engine
                from engine.skills.parser import parse_attrs
                from engine.skills.events import build_tool_feedback

                engine = get_skill_engine()
                attrs_dict = parse_attrs(tag["attrs"])
                content = tag.get("content", "")

                # Stream skill_start to panel
                agent.push_stream_event({"type": "skill_start", "name": tag_name, "attrs": tag.get("attrs", "")})

                # process_tag is a sync generator yielding SSE events
                events = list(engine.process_tag(tag_name, attrs_dict, content, namespace=agent.id))

                # Forward skill events to panel stream
                for evt in events:
                    if isinstance(evt, dict):
                        agent.push_stream_event(evt)

                feedback = build_tool_feedback(events)
                tool_results.append(feedback or "[no output]")

                # Stream skill_end to panel
                agent.push_stream_event({"type": "skill_end", "name": tag_name, "ok": True})

                if tag_name not in agent.skills_used:
                    agent.skills_used.append(tag_name)
            except Exception as exc:
                agent.push_stream_event({"type": "skill_end", "name": tag_name, "ok": False, "error": str(exc)})
                tool_results.append(f"SKILL ERROR ({tag_name}): {type(exc).__name__}: {exc}")

        # Feed results back as next message
        combined = "\n---\n".join(tool_results)
        current_message = f"[Tool Results]\n{combined}"
        agent.messages.append({"role": "user", "content": current_message})

        # Persist each tool call with clear structure for history viewing
        for tag, result in zip(tags, tool_results):
            cmd_parts = [f"<{tag['name']}"]
            if tag.get("attrs"):
                cmd_parts.append(f" {tag['attrs']}")
            if tag.get("content"):
                cmd_parts.append(f">\n{tag['content']}\n</{tag['name']}>")
            else:
                cmd_parts.append(" />")
            command_str = "".join(cmd_parts)

            tool_msg = (
                f"## Tool\n"
                f"**Name:** `{tag['name']}`\n"
                f"**Command:**\n```\n{command_str}\n```\n"
                f"**Output:**\n```\n{result[:2000]}\n```"
            )
            await _persist_message(agent.id, "tool", tool_msg)

    # Hit max iterations — force final answer
    force_msg = "Maximum steps reached. Provide your final JSON answer NOW with whatever you have."
    agent.messages.append({"role": "user", "content": force_msg})
    await _persist_message(agent.id, "user", force_msg)
    response_text, _ = await _send_with_retry(agent, force_msg, parent_id, breaker, False)
    return response_text


async def _send_with_retry(
    agent: Agent,
    message: str,
    parent_id: str | None,
    breaker: CircuitBreaker,
    is_first_turn: bool,
    max_retries: int = 3,
) -> tuple[str, str | None]:
    """Send message to LLM with exponential backoff. Returns (response_text, new_parent_id)."""
    for attempt in range(max_retries):
        try:
            text, new_pid = await _call_llm(agent, message, parent_id, is_first_turn)
            breaker.record_success()
            return text, new_pid
        except Exception as exc:
            breaker.record_failure()
            if attempt == max_retries - 1:
                raise
            delay = (2 ** attempt) + random.uniform(0, 1)
            logger.warning("Agent %s retry %d/%d: %s (%.1fs)", agent.id, attempt + 1, max_retries, exc, delay)
            await asyncio.sleep(delay)
    raise RuntimeError("Unreachable")


async def _call_llm(
    agent: Agent, message: str, parent_id: str | None, is_first_turn: bool
) -> tuple[str, str | None]:
    """Route to DeepSeek or Qwen. Returns (accumulated_text, new_parent_id)."""
    if "deepseek" in agent.model:
        return await _call_deepseek(agent, message)
    return await _call_qwen(agent, message, parent_id, is_first_turn)


async def _call_deepseek(agent: Agent, message: str) -> tuple[str, str | None]:
    """DeepSeek: client manages session + parent_id internally via chat_id=agent.id.

    Resolves the account from agent.browser_data_dir. If that account has no
    token, the client automatically falls back to any available account token.
    """
    from connectors.deepseek.client import get_client
    from engine.config import get_model_config
    from pathlib import Path

    # Resolve account from agent's browser profile (or None → active symlink)
    account: str | None = None
    if agent.browser_data_dir:
        account = Path(agent.browser_data_dir).name  # "browser-data-acc7"

    client = get_client(account=account)
    accumulated = ""
    # Resolve proper model_type from config (expert / None / vision)
    ds_cfg = get_model_config(agent.model)
    api_model_type = ds_cfg.get("api_model_type") if ds_cfg else None

    async for event in client.stream_chat(
        message,
        model=api_model_type,
        chat_id=f"agent-{agent.id}",  # unique session per agent
        inject_instructions=False,  # agents have their own system prompt
    ):
        etype = event.get("type")
        if etype == "answer":
            chunk_text = event.get("text", "")
            accumulated += chunk_text
            if chunk_text:
                agent.push_stream_event({"type": "chunk", "text": chunk_text})
        elif etype == "error":
            raise RuntimeError(f"DeepSeek: {event.get('message', 'unknown error')}")

    if not accumulated.strip():
        raise RuntimeError("DeepSeek returned empty response")
    return accumulated, None  # DeepSeek client tracks parent internally


async def _call_qwen(
    agent: Agent, message: str, parent_id: str | None, is_first_turn: bool
) -> tuple[str, str | None]:
    """Qwen: single message + parent_id per turn. Server stores history."""
    import httpx
    from engine.config import URL
    from engine.payloads import build_body
    from engine.session import create_new_chat

    # Get headers from shared service
    from server.api.dependencies import service
    headers = await service._ensure_headers()

    # Create or reuse upstream Qwen session
    chat_id = agent.qwen_session_id
    if is_first_turn or not chat_id:
        chat_id = await create_new_chat(headers, model=agent.model)
        if not chat_id:
            headers = await service._refresh_headers()
            chat_id = await create_new_chat(headers, model=agent.model)
        if not chat_id:
            raise RuntimeError("Could not create Qwen chat session for agent")
        agent.qwen_session_id = chat_id

    body = build_body(message, chat_id, parent_id, model=agent.model)
    params = {"chat_id": chat_id}
    accumulated = ""
    new_parent_id: str | None = None

    async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=15)) as client:
        async with client.stream("POST", URL, headers=headers, json=body, params=params) as resp:
            if resp.status_code in (401, 403):
                raise RuntimeError(f"Qwen auth failed ({resp.status_code})")
            if resp.status_code != 200:
                raw = (await resp.aread()).decode(errors="replace")
                raise RuntimeError(f"Qwen HTTP {resp.status_code}: {raw[:300]}")

            buffer = ""
            async for chunk in resp.aiter_bytes():
                if not chunk:
                    continue
                buffer += chunk.decode("utf-8", errors="replace")
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()
                    if not line.startswith("data: "):
                        continue
                    try:
                        data = json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue

                    # Track parent_id from response
                    created = data.get("response.created")
                    if isinstance(created, dict):
                        rid = created.get("response_id")
                        if isinstance(rid, str):
                            new_parent_id = rid

                    choices = data.get("choices", [])
                    if not choices:
                        continue
                    delta = choices[0].get("delta", {})
                    content = delta.get("content", "")
                    if content:
                        accumulated += content
                        agent.push_stream_event({"type": "chunk", "text": content})

    if not accumulated.strip():
        raise RuntimeError("Qwen returned empty response")
    return accumulated, new_parent_id


_SKIP_TAGS = frozenset(("action", "spawn_agent", "br", "hr", "json", "p", "div", "span"))


def _parse_skill_tags(text: str) -> list[dict[str, Any]]:
    """Extract skill tags from LLM response."""
    tags = []

    def _extract(source: str) -> None:
        for m in _INNER_TAG_RE.finditer(source):
            name = m.group(1)
            if name in _SKIP_TAGS:
                continue
            tags.append({
                "name": name,
                "attrs": m.group(2) or "",
                "content": m.group(3) or "",
                "raw": m.group(0),
            })

    # Prefer tags inside <action> blocks
    for action_match in _TAG_RE.finditer(text):
        _extract(action_match.group(2))

    # Fallback: bare skill tags not wrapped in <action>
    if not tags:
        _extract(text)

    return tags


def _validate_json_output(text: str, required_keys: list[str]) -> bool:
    """Check if response contains valid JSON with required keys."""
    json_str = text.strip()
    # Direct parse
    try:
        obj = json.loads(json_str)
        if isinstance(obj, dict):
            return all(k in obj for k in required_keys)
    except json.JSONDecodeError:
        pass
    # Try extracting from code fence
    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if fence_match:
        try:
            obj = json.loads(fence_match.group(1))
            if isinstance(obj, dict):
                return all(k in obj for k in required_keys)
        except json.JSONDecodeError:
            pass
    # Try finding first { ... } block
    brace_match = re.search(r"\{.*\}", text, re.DOTALL)
    if brace_match:
        try:
            obj = json.loads(brace_match.group(0))
            if isinstance(obj, dict):
                return all(k in obj for k in required_keys)
        except json.JSONDecodeError:
            pass
    return False


async def _persist_message(agent_id: str, role: str, content: str) -> None:
    """Write to agent_messages table."""
    from server.database import add_agent_message
    add_agent_message(agent_id, role, content)
