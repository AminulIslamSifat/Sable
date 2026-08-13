
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
from engine.agents.registry import get_role_config, get_account_pool

logger = logging.getLogger("sable")

# Defaults — overridden by settings > agent > limits
MAX_ITERATIONS = 25
MAX_CONTEXT_CHARS = 12000
DEFAULT_MAX_TOOL_OUTPUT_CHARS = 100_000


def _get_max_tool_output_chars() -> int:
    """Read tool output cap from system/settings.json (default 100k)."""
    try:
        from engine.config import _SYSTEM
        import json as _json
        p = _SYSTEM / "settings.json"
        if p.is_file():
            data = _json.loads(p.read_text(encoding="utf-8"))
            val = data.get("max_tool_output_chars")
            if isinstance(val, int) and val > 0:
                return val
    except Exception:
        pass
    return DEFAULT_MAX_TOOL_OUTPUT_CHARS

STUCK_MESSAGE = (
    "You've called the same tool repeatedly with identical arguments. "
    "This approach isn't working. Summarize what you have and report findings, "
    "or try a completely different strategy."
)

FORMAT_REMINDERS: dict[str, str] = {
    "analyst": "This is your FINAL response. Output ONLY a markdown document. For research: ## Topic, ## Findings, ## Sources, ## Summary, ## Confidence. For code review: ## File Reviewed, ## Critical Issues, ## Warnings, ## Info, ## Verdict. No JSON. No action block.",
    "coder": "This is your FINAL response. Output ONLY a markdown document with these sections: ## Description, ## Files Modified, ## Tests, ## Notes. No JSON. No action block.",
    "writer": "This is your FINAL response. Output ONLY a markdown document with these sections: ## Title, ## Document Path, ## Structure Overview, ## Word Count, ## Notes. No JSON. No action block.",
}

_TAG_RE = re.compile(r"<(action)>(.*?)</\1>", re.DOTALL)
# Matches both <tag attrs>content</tag> and <tag attrs />
_INNER_TAG_RE = re.compile(
    r"<(\w+)\s*((?:[^>\"']|\"[^\"]*\"|'[^']*')*?)\s*(?:/>\s*$|>(.*?)</\1\s*>|>)",
    re.DOTALL | re.MULTILINE,
)

# Warning messages injected as user messages when format violations are detected
ACTION_WRAPPER_WARNING = (
    "[FORMAT WARNING] You used a tool tag without wrapping it in an <act" + "ion> block. "
    "All tool tags MUST be wrapped like this:\n"
    "<act" + "ion>\n<your_tag>...</your_tag>\n</act" + "ion>\n"
    "Please retry with the correct format."
)

ORPHAN_CLOSE_TAG_WARNING = (
    "[FORMAT WARNING] Found a closing </act" + "ion> tag without a matching opening <act" + "ion> tag. "
    "Make sure every tool call is properly wrapped:\n"
    "<act" + "ion>\n<your_tag>...</your_tag>\n</act" + "ion>\n"
    "Please retry with the correct format."
)

REPEAT_LOOP_WARNING = (
    "[LOOP WARNING] The same command structure has been repeated 5+ times. "
    "This approach is not working. Stop repeating and either:\n"
    "1. Try a completely different strategy\n"
    "2. Summarize what you have and provide your final answer\n"
    "Do NOT repeat the same command again."
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

    # Initial breaker check (fallback chain handles mid-loop failures)
    initial_breaker = _resolve_breaker(agent, breakers)
    if not initial_breaker.can_execute():
        # Check if any fallback model has a viable breaker
        has_viable_fallback = False
        if agent.model_chain:
            for fb_model in agent.model_chain:
                fb_key = _get_backend_key(fb_model)
                fb_breaker = breakers.get(fb_key)
                if fb_breaker and fb_breaker.can_execute():
                    has_viable_fallback = True
                    break
                # No breaker registered for this backend = not tripped = viable
                if fb_breaker is None:
                    has_viable_fallback = True
                    break
        if not has_viable_fallback:
            key = _get_backend_key(agent.model)
            raise RuntimeError(f"Circuit breaker open for {key} — provider unavailable")

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
            f"CRITICAL TODO RULES:\n"
            f"1. You MUST work through EVERY task in order. Do NOT skip or stop early.\n"
            f"2. When you finish a task, you MUST output <todo_done summary=\"...\"/> BEFORE doing anything else.\n"
            f"3. After marking a task done, IMMEDIATELY start the next task. Do NOT pause or provide a final answer.\n"
            f"4. Only provide your final markdown answer AFTER all tasks are marked complete.\n"
            f"5. If you are unsure whether a task is done, err on the side of doing more work, not less."
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
            agent, current_message, parent_id, breakers, is_first_turn
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
                else:
                    # More todos remain — explicitly acknowledge completion and direct to next task
                    completed_content = agent.todos.todos[agent.todos.current_index - 1].content if agent.todos.current_index > 0 else "previous task"
                    next_task = nxt.content if nxt else "next task"
                    todo_ack = (
                        f"[TODO PROGRESS] Task \"{completed_content}\" marked complete. ✅\n"
                        f"Now work on: \"{next_task}\"\n"
                        f"Continue executing. Do NOT stop or provide a final answer until ALL tasks are done."
                    )
                    agent.messages.append({"role": "user", "content": todo_ack})
                    await _persist_message(agent.id, "user", todo_ack)

            # Strip todo tags from response so they don't leak into skill parsing or message history
            response_text = re.sub(r'<todo_done\s+summary="[^"]*"\s*/?>', '', response_text)
            response_text = re.sub(r'<todo_sub\s+content="[^"]*"\s*/?>', '', response_text)
            response_text = re.sub(r'\n{3,}', '\n\n', response_text).strip()

        # Check for action wrapper format violations BEFORE parsing tags
        format_warning = _check_action_wrapper_violations(response_text)
        if format_warning:
            agent.messages.append({"role": "user", "content": format_warning})
            await _persist_message(agent.id, "user", format_warning)
            response_text, new_parent_id = await _send_with_retry(
                agent, format_warning, parent_id, breakers, False
            )
            if new_parent_id:
                parent_id = new_parent_id
            agent.messages.append({"role": "assistant", "content": response_text})
            await _persist_message(agent.id, "assistant", response_text)
            # Re-check after retry — if still bad, accept degraded
            format_warning2 = _check_action_wrapper_violations(response_text)
            if format_warning2:
                return response_text  # Accept even if still malformed

        # Parse skill tags
        tags = _parse_skill_tags(response_text)

        # Strip action blocks from display text so raw tags never reach the frontend.
        # Tags are already parsed above; the raw markup is just leftover garbage.
        response_text = _TAG_RE.sub("", response_text)
        response_text = re.sub(r'\n{3,}', '\n\n', response_text).strip()

        if not tags:
            # No tool calls → check if this is a premature stop (thin response while todos remain)
            has_active_todos = agent.todos and not agent.todos.all_done
            is_thin_response = len(response_text.strip()) < 200

            if has_active_todos and is_thin_response:
                # Agent stopped mid-plan with a short non-action response — nudge to continue
                continue_msg = (
                    "[CONTINUE REQUIRED] You stopped before completing all tasks.\n"
                    f"You still have {len(agent.todos.todos) - agent.todos.current_index} task(s) remaining.\n"
                    f"Current task: \"{agent.todos.current.content}\"\n"
                    "Do NOT provide a final answer yet. Continue working on the current task using tools.\n"
                    "Only stop when ALL tasks are marked done."
                )
                agent.messages.append({"role": "user", "content": continue_msg})
                await _persist_message(agent.id, "user", continue_msg)
                continue  # Loop back — give the agent another turn

            # No tool calls → validate as final markdown answer
            if _validate_markdown_output(response_text, role_cfg.required_sections):
                return response_text
            # Missing required sections or malformed → one re-prompt
            base_reminder = FORMAT_REMINDERS.get(agent.role, "Provide a clean markdown document with the required sections as your final answer.")
            reminder = f"{base_reminder}\n\nIMPORTANT: Output ONLY the markdown document. Do NOT include any JSON object, structured data block, or duplicate summary. Your entire response must be pure markdown with ## headers."
            agent.messages.append({"role": "user", "content": reminder})
            await _persist_message(agent.id, "user", reminder)
            response_text, new_parent_id = await _send_with_retry(
                agent, reminder, parent_id, breakers, False
            )
            if new_parent_id:
                parent_id = new_parent_id
            agent.messages.append({"role": "assistant", "content": response_text})
            await _persist_message(agent.id, "assistant", response_text)

            # Second chance: if still thin with active todos, nudge again instead of accepting
            if has_active_todos and len(response_text.strip()) < 200:
                continue_msg2 = (
                    "[STILL INCOMPLETE] Your response does not contain a valid final answer or tool calls.\n"
                    f"Remaining tasks: {len(agent.todos.todos) - agent.todos.current_index}\n"
                    "Continue working. Use tools to complete the current task."
                )
                agent.messages.append({"role": "user", "content": continue_msg2})
                await _persist_message(agent.id, "user", continue_msg2)
                continue

            return response_text  # Accept even if still malformed (degraded)

        # Loop detection — warning-based (mirrors MainChatGuard, never hard-kills)
        for tag in tags:
            loop_detector.check(tag["name"], str(tag.get("attrs", "")))

        loop_warning = loop_detector.get_warning()
        if loop_warning:
            # Try teacher escalation before generic warning
            teacher_guidance = await _try_teacher_escalation(
                agent, "Agent is repeating the same tool calls with identical arguments."
            )
            if teacher_guidance:
                warning_msg = f"[MENTOR INTERVENTION]\n{teacher_guidance}"
            else:
                warning_msg = loop_warning
            agent.messages.append({"role": "user", "content": warning_msg})
            await _persist_message(agent.id, "user", warning_msg)
            # Don't skip execution — let the agent see the warning AND execute its tools
            # The warning is injected as context for the NEXT iteration

        # Execute skills
        tool_results = []
        for tag in tags:
            # Check cancellation between tool calls
            if agent.cancelled:
                raise asyncio.CancelledError("Agent killed by orchestrator")

            tag_name = tag["name"]
            agent.tool_calls_total += 1

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
                # Truncate oversized tool output to protect context window
                max_chars = _get_max_tool_output_chars()
                if feedback and len(feedback) > max_chars:
                    feedback = feedback[:max_chars] + f"\n\n[OUTPUT TRUNCATED: {len(feedback)} chars → {max_chars} limit]"
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
                agent.error_recoveries += 1

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
        response_text, _ = await _send_with_retry(agent, guided_msg, parent_id, breakers, False)
        return response_text

    # No teacher or teacher failed — force final answer
    force_msg = "Maximum steps reached. Provide your final markdown answer NOW with whatever you have. Use proper ## headers for each section."
    agent.messages.append({"role": "user", "content": force_msg})
    await _persist_message(agent.id, "user", force_msg)
    response_text, _ = await _send_with_retry(agent, force_msg, parent_id, breakers, False)
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


def _get_backend_type(model: str) -> str:
    """Classify a model as 'qwen' or 'api' based on actual API backend config."""
    from engine.config import get_model_config
    cfg = get_model_config(model)
    backend = cfg.get("api_backend", "")
    logger.info("[FALLBACK-DEBUG] _get_backend_type: model=%s cfg_id=%s api_backend='%s'",
                model, cfg.get("id", "?"), backend)
    if backend in ("gemini", "groq", "mistral", "deepseek"):
        return "api"
    if backend == "qwen":
        return "qwen"
    # Fallback: check model name only if no explicit backend configured
    if "qwen" in model.lower() and not backend:
        logger.warning("[FALLBACK-DEBUG] _get_backend_type: FALLBACK to 'qwen' via name match (no api_backend configured)")
        return "qwen"
    return "api"


async def _migrate_conversation(agent: Agent, old_model: str, new_model: str) -> str | None:
    """Migrate conversation history when switching between backend types.

    Qwen→API: serialize agent.messages into a context prefix for the new backend.
    API→Qwen: inject accumulated history as a single first message, reset parent_id.

    Returns a migration context string to prepend, or None if no migration needed.
    """
    old_type = _get_backend_type(old_model)
    new_type = _get_backend_type(new_model)

    if old_type == new_type:
        return None  # Same backend type — no migration needed

    # Collect meaningful conversation content (skip system prompts)
    conv_parts = []
    for msg in agent.messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role == "system":
            continue
        if not content.strip():
            continue
        conv_parts.append(f"[{role.upper()}]: {content}")

    if not conv_parts:
        return None

    history_text = "\n\n".join(conv_parts)

    if old_type == "qwen" and new_type == "api":
        # Qwen had server-side history; agent.messages has our local copy.
        # Inject as context prefix so the API backend knows what happened.
        migration = (
            f"[CONVERSATION MIGRATION] Previous model ({old_model}) became unavailable.\n"
            f"Here is the conversation history so far:\n\n{history_text}\n\n"
            f"[END HISTORY] Continue from where you left off using the new model."
        )
        logger.info("[agent %s] Migrated Qwen→API (%s→%s), %d messages", agent.id, old_model, new_model, len(conv_parts))
        return migration

    if old_type == "api" and new_type == "qwen":
        # API backend had client-side history. Qwen needs this as first message context.
        # Reset upstream session so a fresh one is created.
        agent.qwen_session_id = None
        try:
            from server.database import set_upstream_session_id as _set_usid
            _set_usid(agent.chat_id, None)
        except Exception:
            pass
        migration = (
            f"[CONVERSATION MIGRATION] Previous model ({old_model}) became unavailable.\n"
            f"Here is the conversation history so far:\n\n{history_text}\n\n"
            f"[END HISTORY] Continue from where you left off using the new model."
        )
        logger.info("[agent %s] Migrated API→Qwen (%s→%s), %d messages, session reset", agent.id, old_model, new_model, len(conv_parts))
        return migration

    return None


async def _try_fallback_model(agent: Agent, failed_model: str) -> str | None:
    """Try the next model in the fallback chain. Returns new model name or None if exhausted."""
    logger.info("[FALLBACK-DEBUG] _try_fallback_model: agent=%s failed=%s chain=%s idx=%d",
                agent.id, failed_model, agent.model_chain, agent._fallback_index)
    if not agent.model_chain:
        logger.warning("[FALLBACK-DEBUG] _try_fallback_model: EMPTY model_chain!")
        return None

    # Find next untried model in chain
    for i, fallback_model in enumerate(agent.model_chain):
        if i < agent._fallback_index:
            continue  # Already tried
        if fallback_model == failed_model:
            continue  # Skip the one that just failed
        agent._fallback_index = i + 1
        logger.info("[FALLBACK-DEBUG] _try_fallback_model: selected %s (idx now %d)", fallback_model, agent._fallback_index)
        return fallback_model

    logger.warning("[FALLBACK-DEBUG] _try_fallback_model: ALL models exhausted!")
    return None  # All fallbacks exhausted


def _try_browser_fallback(agent: Agent) -> str | None:
    """Try the next browser profile from the account pool (Qwen only).

    Returns the next available profile or None if exhausted.
    """
    pool = get_account_pool(agent.role)
    if not pool:
        return None
    current = agent.browser_data_dir or ""
    try:
        idx = pool.index(current) + 1
    except ValueError:
        idx = 0
    if idx >= len(pool):
        return None
    return pool[idx]


def _get_backend_key(model: str) -> str:
    """Map model name to its circuit breaker key."""
    from engine.config import get_model_config
    cfg = get_model_config(model)
    backend = cfg.get("api_backend", "")
    if backend in ("gemini", "groq", "mistral", "deepseek"):
        return backend
    if "qwen" in model:
        return "qwen"
    return backend or "qwen"


def _resolve_breaker(agent: Agent, breakers: dict[str, CircuitBreaker]) -> CircuitBreaker:
    """Get the correct circuit breaker for the agent's current model."""
    key = _get_backend_key(agent.model)
    return breakers.get(key, list(breakers.values())[0])


async def _send_with_retry(
    agent: Agent,
    message: str,
    parent_id: str | None,
    breakers: dict[str, CircuitBreaker],
    is_first_turn: bool,
    max_retries: int = 3,
) -> tuple[str, str | None]:
    """Send message to LLM with exponential backoff + model fallback chain.

    On persistent failure (all retries exhausted OR circuit breaker open),
    tries the next model in agent.model_chain with conversation migration.
    Returns (response_text, new_parent_id).
    """
    last_exc: Exception | None = None
    breaker = _resolve_breaker(agent, breakers)

    logger.info("[FALLBACK-DEBUG] agent=%s model=%s model_chain=%s _fallback_index=%d",
                agent.id, agent.model, agent.model_chain, agent._fallback_index)

    for attempt in range(max_retries):
        try:
            text, new_pid = await _call_llm(agent, message, parent_id, is_first_turn)
            breaker.record_success()
            return text, new_pid
        except Exception as exc:
            last_exc = exc
            breaker.record_failure()
            logger.warning("[FALLBACK-DEBUG] agent=%s attempt=%d/%d FAILED: %s",
                           agent.id, attempt + 1, max_retries, exc)
            if attempt == max_retries - 1:
                break  # Exhausted retries — try fallback below
            delay = (2 ** attempt) + random.uniform(0, 1)
            logger.warning("Agent %s retry %d/%d: %s (%.1fs)", agent.id, attempt + 1, max_retries, exc, delay)
            await asyncio.sleep(delay)

    # All retries failed with current model+profile.
    backend_type = _get_backend_type(agent.model)
    logger.info("[FALLBACK-DEBUG] agent=%s retries exhausted. backend_type=%s, attempting fallback",
                agent.id, backend_type)
    # Qwen: exhaust ALL browser profiles first, THEN switch model.
    # Non-Qwen: go straight to model fallback.
    if _get_backend_type(agent.model) == "qwen":
        browser_profile = _try_browser_fallback(agent)
        if browser_profile:
            old_profile = agent.browser_data_dir or "default"
            logger.info("[agent %s] Browser fallback: %s → %s", agent.id, old_profile, browser_profile)
            agent.push_stream_event({
                "type": "browser_fallback",
                "from": old_profile,
                "to": browser_profile,
                "reason": str(last_exc)[:200] if last_exc else "retries exhausted",
            })
            agent.browser_data_dir = browser_profile
            agent.qwen_session_id = None
            try:
                from server.database import set_upstream_session_id as _set_usid
                _set_usid(agent.chat_id, None)
            except Exception:
                pass
            parent_id = None
            is_first_turn = True

            breaker = _resolve_breaker(agent, breakers)
            for attempt in range(max_retries):
                try:
                    text, new_pid = await _call_llm(agent, message, parent_id, is_first_turn)
                    breaker.record_success()
                    logger.info("[agent %s] Browser fallback to %s succeeded", agent.id, browser_profile)
                    return text, new_pid
                except Exception as exc3:
                    last_exc = exc3
                    breaker.record_failure()
                    if attempt == max_retries - 1:
                        break
                    delay = (2 ** attempt) + random.uniform(0, 1)
                    logger.warning("Agent %s browser fallback retry %d/%d: %s (%.1fs)", agent.id, attempt + 1, max_retries, exc3, delay)
                    await asyncio.sleep(delay)

            # This browser profile also failed — try next one recursively
            return await _send_with_retry(agent, message, parent_id, breakers, is_first_turn, max_retries)

    # All browser profiles exhausted (or not Qwen) — try model fallback chain
    logger.info("[FALLBACK-DEBUG] agent=%s entering model fallback section", agent.id)
    fallback_model = await _try_fallback_model(agent, agent.model)
    if fallback_model:
        old_model = agent.model
        logger.info("[agent %s] Falling back: %s → %s", agent.id, old_model, fallback_model)
        agent.push_stream_event({
            "type": "model_fallback",
            "from": old_model,
            "to": fallback_model,
            "reason": str(last_exc)[:200] if last_exc else "unknown",
        })

        migration_ctx = await _migrate_conversation(agent, old_model, fallback_model)
        agent.model = fallback_model

        if _get_backend_type(fallback_model) == "qwen":
            agent.qwen_session_id = None
            try:
                from server.database import set_upstream_session_id as _set_usid
                _set_usid(agent.chat_id, None)
            except Exception:
                pass
            parent_id = None
            is_first_turn = True

        if migration_ctx:
            message = f"{migration_ctx}\n\n---\n\n{message}"

        breaker = _resolve_breaker(agent, breakers)

        for attempt in range(max_retries):
            try:
                text, new_pid = await _call_llm(agent, message, parent_id, is_first_turn)
                breaker.record_success()
                logger.info("[agent %s] Fallback to %s succeeded", agent.id, fallback_model)
                return text, new_pid
            except Exception as exc2:
                last_exc = exc2
                breaker.record_failure()
                if attempt == max_retries - 1:
                    break
                delay = (2 ** attempt) + random.uniform(0, 1)
                logger.warning("Agent %s fallback retry %d/%d: %s (%.1fs)", agent.id, attempt + 1, max_retries, exc2, delay)
                await asyncio.sleep(delay)

        # Model fallback also failed — try next in chain recursively
        return await _send_with_retry(agent, message, parent_id, breakers, is_first_turn, max_retries)

    # No fallbacks left at all — raise
    logger.error("[FALLBACK-DEBUG] agent=%s NO FALLBACK AVAILABLE. Raising: %s", agent.id, last_exc)
    raise last_exc or RuntimeError("All retries and fallbacks exhausted")


async def _call_llm(
    agent: Agent, message: str, parent_id: str | None, is_first_turn: bool
) -> tuple[str, str | None]:
    """Route to the appropriate backend. Returns (accumulated_text, new_parent_id)."""
    from engine.config import get_model_config

    cfg = get_model_config(agent.model)
    backend = cfg.get("api_backend")

    # Debug: log every LLM dispatch to see what's actually being routed
    try:
        from engine.config import OUTPUT_ROOT as _out_root
        _dbg = _out_root / "llm_dispatch_log.txt"
        with open(_dbg, "a") as _df:
            from datetime import datetime as _dt
            _df.write(f"{_dt.now().isoformat()} | model={agent.model} | backend={backend} | msg_len={len(message)}\n")
    except Exception:
        pass

    if backend == "deepseek":
        return await _call_deepseek(agent, message)
    if backend in ("gemini", "groq", "mistral"):
        return await _call_api_backend(agent, message, backend, system_instruction=agent.system_prompt)
    if backend == "local":
        return await _call_local(agent, message)
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

    connector = get_connector(backend, model_id=agent.model)
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



async def _call_local(agent: Agent, message: str) -> tuple[str, str | None]:
    """Local/OpenAI-compatible cookbook model: full messages array per request.

    llama-server is stateless — it needs the entire conversation every call.
    We send agent.messages (system + all turns) to /v1/chat/completions.
    """
    import httpx
    from engine.config import get_model_config

    cfg = get_model_config(agent.model)
    endpoint = cfg.get("local_endpoint", "http://127.0.0.1:8080/v1").rstrip("/")
    api_model = cfg.get("api_model_type", agent.model)

    # Full history — stateless API requires it
    messages = list(agent.messages)

    payload = {
        "model": api_model,
        "messages": messages,
        "stream": True,
    }

    # Debug: dump full payload to log file for local model inspection
    try:
        from engine.config import OUTPUT_ROOT as _out_root
        _log_file = _out_root / "local_model_payload.txt"
        _log_file.parent.mkdir(parents=True, exist_ok=True)
        with open(_log_file, "a", encoding="utf-8") as _f:
            from datetime import datetime as _dt
            _f.write(f"\n{'='*80}\n")
            _f.write(f"TIMESTAMP: {_dt.now().isoformat()}\n")
            _f.write(f"MODEL: {api_model}\n")
            _f.write(f"ENDPOINT: {endpoint}\n")
            _f.write(f"MESSAGE COUNT: {len(messages)}\n")
            _f.write(f"{'='*80}\n")
            _f.write(json.dumps(payload, indent=2, ensure_ascii=False))
            _f.write("\n")
    except Exception:
        pass  # never let logging break inference

    accumulated = ""
    async with httpx.AsyncClient(timeout=300.0) as client:
        async with client.stream(
            "POST",
            f"{endpoint}/chat/completions",
            json=payload,
            headers={"Authorization": "Bearer sable-local"},
        ) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                raise RuntimeError(f"Local model HTTP {resp.status_code}: {body.decode()[:300]}")
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                    delta = chunk["choices"][0].get("delta", {})
                    token = delta.get("content", "")
                    if token:
                        accumulated += token
                        agent.push_stream_event({"type": "chunk", "text": token})
                except (json.JSONDecodeError, KeyError, IndexError):
                    pass

    if not accumulated.strip():
        raise RuntimeError("Local model returned empty response")
    return accumulated, None


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
    # Prefer DB-stored upstream_session_id; fall back to in-memory cache
    from server.database import get_upstream_session_id as _get_usid, set_upstream_session_id as _set_usid
    chat_id = _get_usid(agent.chat_id) or agent.qwen_session_id
    if is_first_turn or not chat_id:
        # Clear stale system instruction so it doesn't conflict with agent prompt
        # Only if agent has its own browser profile — never touch Maria's active session
        if agent.browser_data_dir:
            try:
                import uuid as _uuid
                import httpx as _httpx
                _hdrs = dict(headers)
                _hdrs.update({
                    "Content-Type": "application/json",
                    "Version": "0.2.80",
                    "source": "web",
                    "Origin": "https://chat.qwen.ai",
                    "Referer": "https://chat.qwen.ai/settings/personalization",
                    "X-Request-Id": str(_uuid.uuid4()),
                })
                async with _httpx.AsyncClient(timeout=15) as _client:
                    await _client.post(
                        "https://chat.qwen.ai/api/v2/users/user/settings/update",
                        json={"personalization": {"name": "", "description": "", "style": "Default", "instruction": ""}},
                        headers=_hdrs,
                    )
            except Exception as exc:
                logger.warning("Agent %s: clear instruction failed: %s", agent.id, exc)
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
        _set_usid(agent.chat_id, chat_id)

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



def _check_action_wrapper_violations(text: str) -> str | None:
    """Check for action wrapper format violations in LLM response.

    Returns a warning message string if violations found, None otherwise.
    Checks:
    1. Bare skill tags not wrapped in action blocks
    2. Orphan closing action tags without matching opener
    """
    from engine.skills.parser import KNOWN_TAGS

    has_action_open = "<act" + "ion>" in text
    has_action_close = "</act" + "ion>" in text

    # Check for orphan closing tag
    if has_action_close and not has_action_open:
        return ORPHAN_CLOSE_TAG_WARNING

    # Check for bare skill tags outside action blocks
    # Strip all proper action blocks first
    stripped = _TAG_RE.sub("", text)

    # Now check if any known skill tags remain in the stripped text
    for tag_name in KNOWN_TAGS:
        pattern = re.compile(r"<" + re.escape(tag_name) + r"[\s/>]", re.IGNORECASE)
        if pattern.search(stripped):
            return ACTION_WRAPPER_WARNING

    return None


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
    When required_sections is empty, applies a minimum quality gate: the response
    must contain at least one ## header to be considered a valid final answer.
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
    if required_sections:
        # Threshold: at least 2 required sections must be present
        # (handles roles with dual formats like analyst where only one set applies)
        found = sum(1 for s in required_sections if f"## {s.lower()}" in text_lower)
        return found >= 2
    # No required sections — minimum quality gate: must have at least one ## header
    # Catches mid-thought responses that aren't actual final answers
    return "## " in stripped


async def _persist_message(agent_id: str, role: str, content: str) -> None:
    """Write to agent_messages table. Logs failures without crashing the loop."""
    try:
        from server.database import add_agent_message
        add_agent_message(agent_id, role, content)
    except Exception as exc:
        logger.debug("Failed to persist agent message (%s/%s): %s", agent_id, role, exc)
