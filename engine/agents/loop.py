
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
    "researcher": "This is your FINAL response. Output ONLY a markdown document with these sections: ## Topic, ## Findings, ## Sources, ## Summary, ## Confidence. No JSON. No action block.",
    "coder": "This is your FINAL response. Output ONLY a markdown document with these sections: ## Description, ## Files Modified, ## Tests, ## Notes. No JSON. No action block.",
    "reviewer": "This is your FINAL response. Output ONLY a markdown document with these sections: ## File Reviewed, ## Critical Issues, ## Warnings, ## Info, ## Verdict. No JSON. No action block.",
    "writer": "This is your FINAL response. Output ONLY a markdown document with these sections: ## Title, ## Document Path, ## Structure Overview, ## Word Count, ## Notes. No JSON. No action block.",
    "utility": "This is your FINAL response. Output ONLY a markdown document with these sections: ## Task, ## Actions Taken, ## Result, ## Notes. No JSON. No action block.",
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
        "- For INTERMEDIATE responses: briefly state your next step (1 sentence max), then output the action block. Do NOT use final format headers.",
        "- Use absolute paths for all file operations.",
        "- After getting tool output, analyze it and decide next step.",
        "- ONLY when ALL tool work is done, output your final markdown answer using the required sections. No action block on the final answer.",
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
    """Execute the full agent loop. Returns final markdown answer text."""
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
    agent.system_prompt = system_prompt

    first_message = system_prompt
    if agent.context:
        first_message += f"\n\nContext: {agent.context}\n\nTask: {agent.task}"
    else:
        first_message += f"\n\nTask: {agent.task}"

    # Inject todo plan into first message if present
    if agent.todos and agent.todos.todos:
        # Emit initial todo state so panel sees it immediately (even before first <todo_done>)
        agent.push_stream_event({
            "type": "todo_progress",
            "progress": agent.todos.progress,
            "current": agent.todos.current.content if agent.todos.current else None,
            "todos": [
                {"id": t.id, "content": t.content, "status": t.status, "subtasks": t.subtasks, "result": t.result}
                for t in agent.todos.todos
            ],
        })
        plan_lines = "\n".join(f"{t.id}. {t.content}" for t in agent.todos.todos)
        first_message += (
            f"\n\nYour execution plan:\n{plan_lines}\n\n"
            f"Work through these steps in order. Start with step 1.\n\n"
            f"You must follow the todo list strictly. When completing a task, mark it done with <todo_done summary=\"...\"/> before progressing to the next task."
        )

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

        # --- Todo progression: parse <todo_done> and <todo_sub> tags ---
        if agent.todos and agent.todos.current:
            # Parse <todo_sub content="..." /> tags
            for sub_match in re.finditer(
                r'<todo_sub\s+content="([^"]*)"', response_text
            ):
                sub_desc = sub_match.group(1).strip()
                if sub_desc and sub_desc not in agent.todos.current.subtasks:
                    agent.todos.current.subtasks.append(sub_desc)

            # Parse <todo_done summary="..." /> tag
            done_match = re.search(r'<todo_done\s+summary="([^"]*)"', response_text)
            if done_match:
                agent.todos.current.result = done_match.group(1).strip()
                nxt = agent.todos.advance()
                agent.push_stream_event({
                    "type": "todo_progress",
                    "progress": agent.todos.progress,
                    "current": nxt.content if nxt else None,
                    "todos": [
                        {
                            "id": t.id,
                            "content": t.content,
                            "status": t.status,
                            "subtasks": t.subtasks,
                            "result": t.result,
                        }
                        for t in agent.todos.todos
                    ],
                })
                if agent.todos.all_done:
                    # All todos complete — next response should be the final answer
                    current_message = (
                        "All tasks in your plan are complete. "
                        "Provide your final markdown answer now."
                    )
                    agent.messages.append({"role": "user", "content": current_message})
                    await _persist_message(agent.id, "user", current_message)
                    continue
                # More todos remain — continue loop, next iteration gets updated context

            # Strip todo tags from response so they don't leak into skill parsing or message history
            response_text = re.sub(r'<todo_done\s+summary="[^"]*"\s*/?>', '', response_text)
            response_text = re.sub(r'<todo_sub\s+content="[^"]*"\s*/?>', '', response_text)
            response_text = re.sub(r'\n{3,}', '\n\n', response_text).strip()

        # Parse skill tags
        tags = _parse_skill_tags(response_text)
        if not tags:
            # No tool calls → validate as final markdown answer
            if _validate_markdown_output(response_text, role_cfg.required_sections):
                return response_text
            # Missing required sections or malformed → one re-prompt
            base_reminder = FORMAT_REMINDERS.get(agent.role, "Provide a clean markdown document with the required sections as your final answer.")
            reminder = f"{base_reminder}\n\nIMPORTANT: Output ONLY the markdown document. Do NOT include any JSON object, structured data block, or duplicate summary. Your entire response must be pure markdown with ## headers."
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
            # Try teacher escalation before generic stuck message
            teacher_guidance = await _try_teacher_escalation(
                agent, "Agent is repeating the same tool calls with identical arguments."
            )
            if teacher_guidance:
                current_message = f"[MENTOR INTERVENTION]\n{teacher_guidance}"
            else:
                current_message = STUCK_MESSAGE
            agent.messages.append({"role": "user", "content": current_message})
            await _persist_message(agent.id, "user", current_message)
            continue

        # Execute skills
        tool_results = []
        for tag in tags:
            # Check cancellation between tool calls
            if agent.cancelled:
                raise asyncio.CancelledError("Agent killed by orchestrator")

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

                # process_tag is a sync generator — run in thread so task.cancel() can interrupt
                events = await asyncio.to_thread(
                    lambda: list(engine.process_tag(tag_name, attrs_dict, content, namespace=agent.id))
                )

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
            except asyncio.CancelledError:
                agent.push_stream_event({"type": "skill_end", "name": tag_name, "ok": False, "error": "Killed"})
                raise
            except Exception as exc:
                agent.push_stream_event({"type": "skill_end", "name": tag_name, "ok": False, "error": str(exc)})
                tool_results.append(f"SKILL ERROR ({tag_name}): {type(exc).__name__}: {exc}")

        # Feed results back as next message
        combined = "\n---\n".join(tool_results)
        current_message = f"[Tool Results]\n{combined}"

        # Append todo context if agent has an active todo list (compact: skip completed items)
        if agent.todos and agent.todos.current:
            current_message += f"\n\n{agent.todos.format_progress(compact=True)}"

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

    # Hit max iterations — try teacher before forcing final answer
    teacher_guidance = await _try_teacher_escalation(
        agent, f"Agent hit max iterations ({max_iterations}) without completing."
    )
    if teacher_guidance:
        # Give the agent one more chance with teacher guidance
        guided_msg = f"[MENTOR INTERVENTION]\n{teacher_guidance}\n\nYou have ONE final attempt. Provide your best markdown answer now."
        agent.messages.append({"role": "user", "content": guided_msg})
        await _persist_message(agent.id, "user", guided_msg)
        response_text, _ = await _send_with_retry(agent, guided_msg, parent_id, breaker, False)
        return response_text

    # No teacher or teacher failed — force final answer
    force_msg = "Maximum steps reached. Provide your final markdown answer NOW with whatever you have. Use proper ## headers for each section."
    agent.messages.append({"role": "user", "content": force_msg})
    await _persist_message(agent.id, "user", force_msg)
    response_text, _ = await _send_with_retry(agent, force_msg, parent_id, breaker, False)
    return response_text


async def _try_teacher_escalation(agent: Agent, stuck_reason: str) -> str | None:
    """Attempt teacher intervention. Returns guidance text or None.

    Respects the max intervention limit to avoid infinite escalation loops.
    """
    from engine.agents.teacher import escalate_to_teacher, MAX_TEACHER_INTERVENTIONS

    if agent.teacher_interventions >= MAX_TEACHER_INTERVENTIONS:
        return None

    agent.teacher_interventions += 1
    agent.push_stream_event({
        "type": "teacher_escalation",
        "intervention": agent.teacher_interventions,
        "reason": stuck_reason,
    })

    guidance = await escalate_to_teacher(agent, stuck_reason)
    if guidance:
        logger.info("[agent %s] Teacher intervened (#%d)", agent.id, agent.teacher_interventions)
    return guidance


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
    """Route to the appropriate backend. Returns (accumulated_text, new_parent_id)."""
    from engine.config import get_model_config

    cfg = get_model_config(agent.model)
    backend = cfg.get("api_backend")

    if backend == "deepseek":
        return await _call_deepseek(agent, message)
    if backend in ("gemini", "groq", "mistral"):
        return await _call_api_backend(agent, message, backend, system_instruction=agent.system_prompt)
    # Default: Qwen (no api_backend = scraper-based)
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
        # Extract account name robustly — handle trailing slashes, symlinks, nested paths
        resolved = Path(agent.browser_data_dir).resolve()
        account = resolved.name  # "browser-data-acc7"
        # Validate it looks like a browser-data profile; fall back to None if not
        if not account.startswith("browser-data"):
            # Try parent dir (handles cases like /path/to/browser-data-acc7/user_data)
            if resolved.parent.name.startswith("browser-data"):
                account = resolved.parent.name
            else:
                logger.debug("[agent] Unrecognized browser_data_dir '%s', using default account", agent.browser_data_dir)
                account = None

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
        system_instruction=agent.system_prompt,
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


async def _call_api_backend(agent: Agent, message: str, backend: str, *, system_instruction: str | None = None) -> tuple[str, str | None]:
    """Gemini / Groq / Mistral: stateless API call with internal key rotation.

    These backends don't need browser tokens — they rotate API keys internally.
    No account assignment needed.
    """
    from connectors import get_connector
    from engine.config import get_model_config

    connector = get_connector(backend)
    cfg = get_model_config(agent.model)
    api_model_type = cfg.get("api_model_type")

    accumulated = ""
    async for event in connector.stream_chat(
        message,
        model=api_model_type,
        chat_id=f"agent-{agent.id}",
        inject_instructions=False,
        system_instruction=system_instruction,
    ):
        etype = event.get("type")
        if etype == "answer":
            chunk_text = event.get("text", "")
            accumulated += chunk_text
            if chunk_text:
                agent.push_stream_event({"type": "chunk", "text": chunk_text})
        elif etype == "error":
            raise RuntimeError(f"{backend}: {event.get('message', 'unknown error')}")

    if not accumulated.strip():
        raise RuntimeError(f"{backend} returned empty response")
    return accumulated, None


async def _get_agent_qwen_headers(agent: Agent) -> dict[str, str]:
    """Resolve Qwen WAF headers for an agent based on its assigned browser account.

    Priority: agent.browser_data_dir → role pool → shared service (active).
    Uses cached per-account tokens when available. If no cached token exists
    for the assigned account, launches a headless browser with that profile
    to extract fresh tokens, then closes it.
    """
    from pathlib import Path as _Path

    # Determine which account this agent should use
    account: str | None = None
    if agent.browser_data_dir:
        account = _Path(agent.browser_data_dir).name
    else:
        from engine.agents.registry import get_next_account
        account = get_next_account(agent.role)

    if account:
        from engine.config import get_qwen_tokens_for_account, _SYSTEM
        from engine.session import build_headers

        cached = get_qwen_tokens_for_account(account)
        if cached and cached.get("cookies"):
            return build_headers(
                cookies=cached["cookies"],
                bx_ua=cached.get("bx_ua"),
                bx_umidtoken=cached.get("bx_umidtoken"),
            )

        # No cached token — launch browser with this account's profile to get one
        profile_dir = _SYSTEM / account
        if profile_dir.is_dir():
            logger.info("Agent %s: no cached token for %s, launching browser", agent.id, account)
            try:
                from engine.session import BrowserManager
                from engine.config import save_qwen_tokens_for_account

                bm = BrowserManager(user_data_dir=str(profile_dir))
                await bm.start()
                try:
                    headers = await bm.get_fresh_headers()
                    save_qwen_tokens_for_account(
                        cookies=headers.get("Cookie", ""),
                        bx_ua=headers.get("bx-ua", ""),
                        bx_umidtoken=headers.get("bx-umidtoken", ""),
                        account=account,
                    )
                    return headers
                finally:
                    await bm.close()
            except Exception as exc:
                logger.warning("Agent %s: browser token fetch for %s failed: %s", agent.id, account, exc)

    # Fallback: shared service (active account)
    from server.api.dependencies import service
    return await service._ensure_headers()


async def _call_qwen(
    agent: Agent, message: str, parent_id: str | None, is_first_turn: bool
) -> tuple[str, str | None]:
    """Qwen: single message + parent_id per turn. Server stores history."""
    import httpx
    from engine.config import URL
    from engine.payloads import build_body
    from engine.session import create_new_chat

    # Get headers for this agent's assigned account
    headers = await _get_agent_qwen_headers(agent)

    # Create or reuse upstream Qwen session
    chat_id = agent.qwen_session_id
    if is_first_turn or not chat_id:
        chat_id = await create_new_chat(headers, model=agent.model)
        if not chat_id:
            # Retry with fresh headers (re-fetch from browser if needed)
            headers = await _get_agent_qwen_headers(agent)
            chat_id = await create_new_chat(headers, model=agent.model)
        if not chat_id:
            # Third attempt after brief backoff (transient API hiccup)
            await asyncio.sleep(1.5)
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


def _validate_markdown_output(text: str, required_sections: list[str]) -> bool:
    """Check if response is a pure markdown document containing required ## headers.

    Rejects any response that contains JSON objects/arrays or isn't pure markdown.
    """
    stripped = text.strip()
    if not stripped:
        return False
    # Reject if it starts with JSON
    if stripped.startswith("{") or stripped.startswith("["):
        return False
    # Reject if it contains standalone JSON blocks anywhere in the response
    # Matches top-level JSON objects/arrays that aren't inside code fences
    # Remove fenced code blocks first so we don't flag JSON inside ```json blocks
    no_fences = re.sub(r"```[\s\S]*?```", "", stripped)
    if re.search(r"(?m)^\s*[\{\[]\s*$", no_fences):
        return False
    # Also reject inline JSON-like structures outside code fences
    if re.search(r'\{[^{}]*"[^"]+"\s*:', no_fences):
        return False
    # Check that required section headers exist (case-insensitive)
    text_lower = stripped.lower()
    return all(f"## {s.lower()}" in text_lower for s in required_sections)


async def _persist_message(agent_id: str, role: str, content: str) -> None:
    """Write to agent_messages table. Logs failures without crashing the loop."""
    try:
        from server.database import add_agent_message
        add_agent_message(agent_id, role, content)
    except Exception as exc:
        logger.debug("Failed to persist agent message (%s/%s): %s", agent_id, role, exc)
