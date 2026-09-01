
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
import uuid
from pathlib import Path
from typing import Any

from engine.agents.agent import Agent
from engine.agents.resilience import CircuitBreaker, LoopDetector, TurnCapTracker
from engine.agents.registry import get_role_config, get_account_pool

logger = logging.getLogger("sable")

# Defaults — overridden by settings > agent > limits
MAX_ITERATIONS = 25
MAX_CONTEXT_CHARS = 12000
DEFAULT_MAX_TOOL_OUTPUT_CHARS = 100_000


def _get_teacher_failure_threshold() -> int:
    """Load teacher.failure_threshold from agent_config.json (default 3)."""
    from engine.config import AGENT_CONFIG_PATH
    try:
        cfg = json.loads(AGENT_CONFIG_PATH.read_text(encoding="utf-8"))
        return int(cfg.get("teacher", {}).get("failure_threshold", 3))
    except Exception:
        return 3


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
    "analyst": "This is your FINAL response. Output ONLY a markdown document. For research: ## Topic, ## Findings, ## Sources, ## Summary, ## Confidence. For code review: ## File Reviewed, ## Critical Issues, ## Warnings, ## Info, ## Verdict. No JSON. No tool_call block.",
    "coder": "This is your FINAL response. Output ONLY a markdown document with these sections: ## Description, ## Files Modified, ## Tests, ## Notes. No JSON. No tool_call block.",
    "writer": "This is your FINAL response. Output ONLY a markdown document with these sections: ## Title, ## Document Path, ## Structure Overview, ## Word Count, ## Notes. No JSON. No tool_call block.",
}

_TAG_RE = re.compile(r"<\s*tool_calls?\s*>(.*?)<\s*/\s*tool_calls?\s*>", re.DOTALL | re.IGNORECASE)
# Matches both <tag attrs>content</tag> and <tag attrs />
_INNER_TAG_RE = re.compile(
    r"<(\w+)\s*((?:[^>\"']|\"[^\"]*\"|'[^']*')*?)\s*(?:/>\s*$|>(.*?)</\1\s*>|>)",
    re.DOTALL | re.MULTILINE,
)

# Warning messages injected as user messages when format violations are detected
ACTION_WRAPPER_WARNING = (
    "[FORMAT WARNING] You used a tool call without wrapping it in a <tool" + "_call> block. "
    "All tool calls MUST be wrapped like this:\n"
    "<tool" + "_call>{\"name\": \"tool_name\", \"arguments\": {...}}</tool" + "_call>\n"
    "Please retry with the correct format."
)

ORPHAN_CLOSE_TAG_WARNING = (
    "[FORMAT WARNING] Found a closing </tool" + "_call> tag without a matching opening <tool" + "_call> tag. "
    "Make sure every tool call is properly wrapped:\n"
    "<tool" + "_call>{\"name\": \"tool_name\", \"arguments\": {...}}</tool" + "_call>\n"
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
### execute_command
Run a shell command. Returns stdout+stderr. 15s timeout.

Tool call:
```json
{"name": "execute_command", "arguments": {"command": "ls -la /home"}}
```

Rules:
- Always use absolute paths.
- For long-running commands (>15s), set `"bg": true` in arguments.
- Sudo password is `<pass>` — use: `echo <pass> | sudo -S <command>`
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


def _resolve_tool_groups(allowed_tools: list[str]) -> list[str]:
    """Resolve tool group keys to flat list of function names.

    Only explicitly listed group keys are included (respecting disabled_tools.json).
    """
    from engine.tools_loader import browse_tools
    from server.api.routes.misc import get_disabled_tools

    disabled = set(get_disabled_tools().get("disabled", []))
    all_groups = browse_tools()

    # Filter to requested groups that aren't disabled
    allowed_set = set(allowed_tools)
    groups = [g for g in all_groups if g["key"] in allowed_set and g["key"] not in disabled]

    # Flatten to function names
    funcs = []
    for g in groups:
        for fn in g.get("tools", []):
            name = fn.get("name", fn) if isinstance(fn, dict) else fn
            if name not in funcs:
                funcs.append(name)
    return funcs


_NATIVE_TOOL_BACKENDS = frozenset({"gemini", "mistral", "groq", "openai", "local", "cloudflare"})


def _build_tool_guide(allowed_tools: list[str], allowed_skills: list[str], *, native_tools: bool = False) -> str:
    """Build tool usage guide from tool group keys and skill keys.

    - allowed_tools: tool group keys (empty = all enabled groups)
    - allowed_skills: loaded from /skills/{key}/instruction.md (skipped if missing)
    """
    import logging
    logger = logging.getLogger(__name__)

    lines: list[str] = []

    if not native_tools:
        # Text-based Hermes instructions + JSON schema dump (non-native backends only)
        tool_funcs = _resolve_tool_groups(allowed_tools)
        TC_O = "<" + "tool_call" + ">"
        TC_C = "</" + "tool_call" + ">"
        example = '[{"name": "tool_name", "arguments": {"param": "value"}}]'
        lines.extend([
            "\n## Available Tools",
            "To call a tool, output exactly this structure (one per response):",
            f"  {TC_O}",
            f"  {example}",
            f"  {TC_C}",
            "",
            "Rules:",
            "- Exactly ONE tool_call block per response. Wait for the result before continuing.",
            "- For INTERMEDIATE responses: briefly state your next step (1 sentence max), then output the tool_call block. Do NOT use final format headers.",
            "- Use absolute paths for all file operations.",
            "- After getting tool output, analyze it and decide next step.",
            "- ONLY when ALL tool work is done, output your final markdown answer using the required sections. No tool_call block on the final answer.",
            "",
        ])

        from engine.tools_loader import get_all_tool_schemas
        from server.api.routes.misc import get_disabled_tools
        disabled = get_disabled_tools().get("disabled", [])
        schemas = get_all_tool_schemas(disabled=disabled, allowed=allowed_tools)
        if schemas:
            lines.append("\n<tools>")
            for s in schemas:
                lines.append(json.dumps(s, ensure_ascii=False))
            lines.append("</tools>")
            lines.append("")

    # Skill documentation: load instruction.md only if file exists
    valid_skills = []
    for skill_key in allowed_skills:
        instr_path = _SKILLS_DIR / skill_key / "instruction.md"
        if instr_path.is_file():
            valid_skills.append(skill_key)
        else:
            logger.warning("Skipping missing agent skill in prompt: %s", skill_key)

    if valid_skills:
        from engine.skills.registry import discover_skills
        skill_meta = {s.key: s for s in discover_skills(_SKILLS_DIR)}

        lines.append("\n## Available Skills")
        lines.append("Read their instruction.md via view_file before first use.\n")
        for skill_key in valid_skills:
            meta = skill_meta.get(skill_key)
            instr_path = _SKILLS_DIR / skill_key / "instruction.md"
            if meta:
                lines.append(f"### {meta.name}")
                lines.append(f"* **Trigger:** {meta.trigger}")
                if meta.not_this_if:
                    lines.append(f"* **Not this if:** {meta.not_this_if}")
            else:
                lines.append(f"### {skill_key}")
            lines.append(f"* **Instruction:** `{instr_path}`")
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
    turn_caps = TurnCapTracker()  # Caps reset per run_agent_llm_loop invocation

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

    # Store allowed tool groups on agent for native tool passing to API backends
    agent.allowed_tool_groups = list(role_cfg.allowed_tools)

    # Build first message: system prompt + tool guide + task
    # Skip text-based tool instructions for backends with native tool calling support
    from engine.config import get_model_config as _get_agent_model_cfg
    _agent_backend = _get_agent_model_cfg(agent.model).get("api_backend", "")
    _use_native_tools = _agent_backend in _NATIVE_TOOL_BACKENDS

    system_prompt = role_cfg.system_prompt
    system_prompt += _build_tool_guide(role_cfg.allowed_tools, role_cfg.allowed_skills, native_tools=_use_native_tools)
    if agent.instruction:
        system_prompt += f"\n\nSpecial instruction from orchestrator: {agent.instruction}"

    # Inject TODO instructions + tools into system prompt (only for agents with a plan)
    if agent.todos and agent.todos.todos:
        plan_lines = "\n".join(f"{t.id}. {t.content}" for t in agent.todos.todos)
        system_prompt += (
            "\n\n## Task Plan\n"
            "You have a structured execution plan. Work through it in order.\n\n"
            f"Steps:\n{plan_lines}\n\n"
            "### Progress Tracking Tools\n"
            "You have two tools for managing your task plan:\n\n"
            "**todo_complete** — Call when you finish the current task.\n"
            '  {"name": "todo_complete", "arguments": {"summary": "what you accomplished"}}\n\n'
            "**todo_skip** — Call to skip the current task if it's unnecessary or blocked.\n"
            '  {"name": "todo_skip", "arguments": {"reason": "why you are skipping"}}\n\n'
            "Rules:\n"
            "- A single task may require multiple tool calls. Only call todo_complete when the task is fully done.\n"
            "- After calling todo_complete or todo_skip, continue working on the next task immediately.\n"
            "- Provide your final markdown answer only after ALL tasks are complete or skipped.\n"
            "- Your current task and progress are shown after each tool result."
        )

    agent.system_prompt = system_prompt

    first_message = system_prompt
    if agent.context:
        first_message += f"\n\nContext: {agent.context}\n\nTask: {agent.task}"
    else:
        first_message += f"\n\nTask: {agent.task}"

    # Emit initial todo state so panel sees it immediately
    if agent.todos and agent.todos.todos:
        agent.push_stream_event({
            "type": "todo_progress",
            "progress": agent.todos.progress,
            "current": agent.todos.current.content if agent.todos.current else None,
            "todos": [
                {"id": t.id, "content": t.content, "status": t.status, "subtasks": t.subtasks, "result": t.result}
                for t in agent.todos.todos
            ],
        })

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
    consecutive_failures = 0
    failure_threshold = _get_teacher_failure_threshold()

    # Main loop
    current_message = first_message
    _pending_agent_images: list[str] = []  # image paths from get_file to inject next round
    _round_tool_errors: dict[str, str] = {}  # persists across iterations for exact-failure detection

    for iteration in range(max_iterations):
        agent.push_stream_event({"type": "iteration", "iteration": iteration + 1})

        # Check for user-injected guidance messages
        if agent.pending_user_messages:
            guidance = "\n\n".join(agent.pending_user_messages)
            agent.pending_user_messages.clear()
            guidance_msg = f"[USER GUIDANCE]\n{guidance}"
            agent.messages.append({"role": "user", "content": guidance_msg})
            await _persist_message(agent.id, "user", guidance_msg)
            current_message = guidance_msg

        # Call LLM (inject pending skill images if model supports vision)
        _files_for_round: list[str] | None = _pending_agent_images or None
        _pending_agent_images = []
        response_text, new_parent_id = await _send_with_retry(
            agent, current_message, parent_id, breakers, is_first_turn, files=_files_for_round
        )
        if new_parent_id:
            parent_id = new_parent_id
        is_first_turn = False

        agent.messages.append({"role": "assistant", "content": response_text})
        await _persist_message(agent.id, "assistant", response_text)

        # Stream the response text to the panel (strip tool_call + DSML blocks + bare JSON)
        _panel_text = re.sub(r"<\s*tool_calls?\s*>.*?<\s*/\s*tool_calls?\s*>", "", response_text, flags=re.DOTALL | re.IGNORECASE)
        _panel_text = re.sub(
            r'<\uff5c?DSML\uff5ctool_calls>.*?</\uff5c?DSML\uff5ctool_calls>',
            "", _panel_text, flags=re.DOTALL,
        )
        _panel_text = re.sub(
            r'\{\s*"name"\s*:\s*"[\w-]+"\s*,\s*"arguments"\s*:\s*\{.*?\}\s*\}',
            "", _panel_text, flags=re.DOTALL,
        ).strip()
        agent.push_stream_event({"type": "answer", "text": _panel_text})

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

        # Strip tool_call + DSML blocks from display text so raw tags never reach the frontend.
        # Tags are already parsed above; the raw markup is just leftover garbage.
        response_text = _TAG_RE.sub("", response_text)
        response_text = _DSML_BLOCK_RE.sub("", response_text)
        response_text = re.sub(r'\n{3,}', '\n\n', response_text).strip()

        if not tags:
            # No tool calls → validate as final markdown answer
            if _validate_markdown_output(response_text, role_cfg.required_sections):
                return response_text
            # Missing required sections or malformed → one re-prompt
            base_reminder = FORMAT_REMINDERS.get(agent.role, "Provide a clean markdown document with the required sections as your final answer.")
            reminder = f"{base_reminder}\n\nIMPORTANT: Output ONLY the markdown document. Do NOT include any JSON object, structured data block, or duplicate summary. Your entire response must be pure markdown with ## headers. No tool_call blocks."
            agent.messages.append({"role": "user", "content": reminder})
            await _persist_message(agent.id, "user", reminder)
            response_text, new_parent_id = await _send_with_retry(
                agent, reminder, parent_id, breakers, False
            )
            if new_parent_id:
                parent_id = new_parent_id
            agent.messages.append({"role": "assistant", "content": response_text})
            await _persist_message(agent.id, "assistant", response_text)

            return response_text  # Accept even if still malformed (degraded)

        # Loop detection + per-turn caps
        _loop_blocked_tools: set[int] = set()  # tool indices blocked by guards
        for _tag_idx, tag in enumerate(tags):
            _tag_name = tag["name"]
            _tag_args = str(tag.get("attrs", ""))

            # Per-turn cap check (before execution)
            _cap_warn = turn_caps.check_and_record(_tag_name)
            if _cap_warn:
                tool_results.append(_cap_warn)
                _loop_blocked_tools.add(_tag_idx)
                continue

            # Loop detection with error context + recovery support
            _prev_err = _round_tool_errors.get(_tag_name, "")
            _decision = loop_detector.check_decision(_tag_name, _tag_args, error_msg=_prev_err)

            if _decision.action == "block":
                tool_results.append(_decision.message or f"[HARD STOP] {_tag_name} blocked.")
                _loop_blocked_tools.add(_tag_idx)

            elif _decision.action == "recover":
                # Recovery session: reset LLM state, inject recovery prompt
                logger.info("[agent %s] Guardrail recovery triggered for '%s'", agent.id, _tag_name)
                agent.push_stream_event({"type": "guardrail_recovery", "tool": _tag_name})
                # Reset session state
                agent.qwen_session_id = None
                parent_id = None
                is_first_turn = True
                # Build recovery prompt with original task context
                _recovery_prompt = loop_detector.get_recovery_prompt(
                    _decision.recovery_key,
                    original_task=agent.task or "",
                )
                # Reset messages to system + task only (fresh context)
                agent.messages = [
                    {"role": "system", "content": agent.messages[0]["content"]},
                    {"role": "user", "content": agent.messages[1]["content"]},
                ]
                # Inject recovery prompt as new user message
                agent.messages.append({"role": "user", "content": _recovery_prompt})
                await _persist_message(agent.id, "user", _recovery_prompt)
                current_message = _recovery_prompt
                # Block ALL tools this round — force model to re-think first
                for _blk_idx in range(len(tags)):
                    _loop_blocked_tools.add(_blk_idx)
                break  # Skip remaining tag checks; go straight to feedback

            elif _decision.action == "warn":
                # Warning: try teacher escalation, then inject as context
                teacher_guidance = await _try_teacher_escalation(
                    agent, "Agent is repeating the same tool calls with identical arguments."
                )
                if teacher_guidance:
                    warning_msg = f"[MENTOR INTERVENTION]\n{teacher_guidance}"
                else:
                    warning_msg = _decision.message or ""
                agent.messages.append({"role": "user", "content": warning_msg})
                await _persist_message(agent.id, "user", warning_msg)

        # Execute skills
        tool_results = []
        _round_image_paths: list[str] = []
        for _exec_idx, tag in enumerate(tags):
            # Skip tools blocked by loop guard / turn caps
            if _exec_idx in _loop_blocked_tools:
                continue

            # Check cancellation between tool calls
            if agent.cancelled:
                raise asyncio.CancelledError("Agent killed by orchestrator")

            tag_name = tag["name"]
            agent.tool_calls_total += 1

            try:
                from engine.skills import get_skill_engine
                from engine.skills.parser import parse_attrs  # compat: attrs already dict in Hermes format
                from engine.skills.events import build_tool_feedback

                engine = get_skill_engine()
                attrs_dict = parse_attrs(tag["attrs"])
                content = tag.get("content", "")

                # Stream skill_start to panel
                agent.push_stream_event({"type": "skill_start", "name": tag_name, "attrs": tag.get("attrs", "")})

                # Todo tools are dispatched directly with agent context (not via skill engine)
                if tag_name in ("todo_complete", "todo_skip"):
                    from engine.skills.handlers.agents import handle_todo_complete, handle_todo_skip
                    _todo_handler = handle_todo_complete if tag_name == "todo_complete" else handle_todo_skip
                    _todo_tag_id = uuid.uuid4().hex[:12]
                    events = list(_todo_handler(_todo_tag_id, tag_name, attrs_dict, content, agent=agent))
                else:
                    # process_tag is a sync generator — run in thread so task.cancel() can interrupt
                    events = await asyncio.to_thread(
                        lambda: list(engine.process_tag(tag_name, attrs_dict, content, namespace=agent.id))
                    )

                # Forward skill events to panel stream (skip duplicate skill_start —
                # the explicit one above already created the live "running" card)
                for evt in events:
                    if isinstance(evt, dict) and evt.get("type") != "skill_start":
                        agent.push_stream_event(evt)

                # Collect image paths from skill results
                for _evt in events:
                    if isinstance(_evt, dict) and _evt.get("type") == "skill_end" and _evt.get("ok"):
                        _res = _evt.get("result", {})
                        if _res.get("kind") == "image" and _res.get("path"):
                            _round_image_paths.append(_res["path"])

                feedback = build_tool_feedback(events)
                # Result stubbing + no-progress detection via LoopDetector
                feedback = loop_detector.record_result(
                    tag_name, str(tag.get("attrs", "")), feedback or ""
                )
                # Truncate oversized tool output to protect context window
                max_chars = _get_max_tool_output_chars()
                if feedback and len(feedback) > max_chars:
                    feedback = feedback[:max_chars] + f"\n\n[OUTPUT TRUNCATED: {len(feedback)} chars → {max_chars} limit]"
                tool_results.append(feedback or "[no output]")

                # skill_end already forwarded from handler events (carries result)

                if tag_name not in agent.skills_used:
                    agent.skills_used.append(tag_name)

                # Check if any skill_end event reported failure (ok=False)
                _tool_failed = any(
                    isinstance(_e, dict) and _e.get("type") == "skill_end" and not _e.get("ok", True)
                    for _e in events
                )
                if _tool_failed:
                    consecutive_failures += 1
                    _last_err = next(
                        (_e.get("error", "unknown") for _e in events
                         if isinstance(_e, dict) and _e.get("type") == "skill_end" and not _e.get("ok", True)),
                        "unknown",
                    )
                    # Track error for exact-failure loop detection next iteration
                    _round_tool_errors[tag_name] = str(_last_err)
                    if consecutive_failures >= failure_threshold:
                        guidance = await _try_teacher_escalation(
                            agent,
                            f"Agent hit {consecutive_failures} consecutive tool failures. "
                            f"Last error: {_last_err}"
                        )
                        if guidance:
                            tool_results.append(f"[TEACHER GUIDANCE]: {guidance}")
                        consecutive_failures = 0  # reset after intervention attempt
                else:
                    consecutive_failures = 0  # reset on success

            except asyncio.CancelledError:
                agent.push_stream_event({"type": "skill_end", "name": tag_name, "ok": False, "error": "Killed"})
                raise
            except Exception as exc:
                agent.push_stream_event({"type": "skill_end", "name": tag_name, "ok": False, "error": str(exc)})
                tool_results.append(f"SKILL ERROR ({tag_name}): {type(exc).__name__}: {exc}")
                _round_tool_errors[tag_name] = f"{type(exc).__name__}: {exc}"
                agent.error_recoveries += 1
                consecutive_failures += 1
                if consecutive_failures >= failure_threshold:
                    guidance = await _try_teacher_escalation(
                        agent,
                        f"Agent hit {consecutive_failures} consecutive tool failures. "
                        f"Last error: {type(exc).__name__}: {exc}"
                    )
                    if guidance:
                        tool_results.append(f"[TEACHER GUIDANCE]: {guidance}")
                    consecutive_failures = 0  # reset after intervention attempt

        # Extract image paths from skill results for multimodal injection next round
        from engine.config import get_model_config as _get_agent_model_cfg
        _agent_cfg = _get_agent_model_cfg(agent.model)
        _agent_caps = _agent_cfg.get("capabilities", {})
        _agent_backend = _agent_cfg.get("api_backend")
        _AGENT_DIRECT_READ = {"gemini", "groq", "mistral", "openai"}
        if _round_image_paths:
            if _agent_caps.get("image", False) and _agent_backend in _AGENT_DIRECT_READ:
                _pending_agent_images.extend(_round_image_paths)
            else:
                _reason = "does not support image input" if not _agent_caps.get("image", False) else "does not support inline file injection"
                tool_results.append(
                    f"[NOTE: {len(_round_image_paths)} image(s) were produced by tools "
                    f"but this model ({agent.model}) {_reason}. "
                    f"The image content is not accessible.]"
                )

        # Feed results back as next message
        combined = "\n---\n".join(tool_results)
        current_message = f"<tool_response>\\n{combined}\\n</tool_response>"

        # Append minimal TODO state (no rules — just facts)
        if agent.todos:
            state_line = agent.todos.format_state()
            if state_line:
                current_message += f"\n\n{state_line}"

        agent.messages.append({"role": "user", "content": current_message})

        # Persist each tool call with clear structure for history viewing
        for tag, result in zip(tags, tool_results):
            import json as _j
            _attrs = tag.get("attrs", {})
            if isinstance(_attrs, str):
                try:
                    _attrs = _j.loads(_attrs) if _attrs else {}
                except Exception:
                    _attrs = {}
            command_str = f'{{"name": "{tag['name']}", "arguments": {_j.dumps(_attrs)}}}' 

            tool_msg = (
                f"## Tool\n"
                f"**Name:** `{tag['name']}`\n"
                f"**Command:**\n```\n{command_str}\n```\n"
                f"**Output:**\n```\n{result[:2000]}\n```"
            )
            await _persist_message(agent.id, "tool", tool_msg)

    # Hit max iterations — force final answer (no teacher; running out of steps isn't a failure)
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

    Searches from the back (highest number first) to avoid competing with
    main chat auto-switch which searches forward. Falls back to the global
    reverse pool if the role-specific pool is empty or exhausted.

    Tracks failed profiles on the agent via `_fallback_tried` to prevent
    ping-ponging between two accounts when both fail.

    Returns the full path to the next available profile or None if exhausted.
    """
    from engine.config import _SYSTEM as _AGENT_SYSTEM_DIR, get_available_accounts_reverse

    current_name = Path(agent.browser_data_dir).name if agent.browser_data_dir else ""

    # Accumulate tried-and-failed profiles across recursive fallback attempts
    if not hasattr(agent, "_fallback_tried"):
        agent._fallback_tried: set[str] = set()
    agent._fallback_tried.add(current_name)

    # Try role-specific pool first (reverse order)
    pool = get_account_pool(agent.role)
    if pool:
        from engine.config import is_account_exhausted, is_account_captcha_blocked
        # Search pool in reverse, skipping tried/exhausted/captcha-blocked accounts
        for entry in reversed(pool):
            if entry in agent._fallback_tried:
                continue
            if is_account_exhausted(entry):
                continue
            if is_account_captcha_blocked(entry):
                continue
            acct_profile = _AGENT_SYSTEM_DIR / entry
            if acct_profile.is_dir():
                return str(acct_profile)

    # Fall back to global reverse pool (highest number first, up to 10)
    for acc_name in get_available_accounts_reverse(exclude=agent._fallback_tried, limit=10):
        acct_profile = _AGENT_SYSTEM_DIR / acc_name
        if acct_profile.is_dir():
            return str(acct_profile)

    return None


async def _clear_qwen_account_settings(headers: dict[str, str], agent_id: str) -> None:
    """Disable Qwen built-in tools and clear personalization for an agent account.

    Mirrors the account-prep done by BrowserManager.sync_context(), but writes an
    empty instruction so Qwen's cached personalization cannot conflict with the
    agent system prompt. This must run for the initial assigned browser profile
    and again whenever Qwen browser fallback switches to another profile.
    """
    import uuid as _uuid
    import httpx as _httpx

    settings_url = "https://chat.qwen.ai/api/v2/users/user/settings/update"
    hdrs = dict(headers)
    hdrs.update({
        "Content-Type": "application/json",
        "Version": "0.2.80",
        "source": "web",
        "Origin": "https://chat.qwen.ai",
        "Referer": "https://chat.qwen.ai/settings/personalization",
        "X-Request-Id": str(_uuid.uuid4()),
    })

    async with _httpx.AsyncClient(timeout=15) as client:
        # Step 1: disable default Qwen tools that conflict with Sable/agent tools.
        tools_payload = {
            "tools_enabled": {
                "web_extractor": False,
                "web_search_image": False,
                "web_search": False,
                "image_gen_tool": False,
                "code_interpreter": False,
                "history_retriever": False,
                "image_edit_tool": False,
                "bio": False,
                "image_zoom_in_tool": False,
            }
        }
        r1 = await client.post(settings_url, json=tools_payload, headers=hdrs)
        try:
            d1 = r1.json()
        except Exception:
            d1 = {}
        if r1.status_code >= 400 or (d1 and not d1.get("success", False)):
            raise RuntimeError(f"disable tools failed: HTTP {r1.status_code} {str(d1)[:200]}")

        # Step 2: clear personalization instruction for agent-only prompt control.
        hdrs["X-Request-Id"] = str(_uuid.uuid4())
        instr_payload = {
            "personalization": {
                "name": "",
                "description": "",
                "style": "Default",
                "instruction": "",
            }
        }
        r2 = await client.post(settings_url, json=instr_payload, headers=hdrs)
        try:
            d2 = r2.json()
        except Exception:
            d2 = {}
        if r2.status_code >= 400 or (d2 and not d2.get("success", False)):
            raise RuntimeError(f"clear instruction failed: HTTP {r2.status_code} {str(d2)[:200]}")

    logger.info("Agent %s: Qwen account settings cleared (tools disabled + empty instruction)", agent_id)


def _classify_and_mark_account_error(exc: Exception, account_name: str) -> None:
    """Inspect an exception message and mark the account if WAF-blocked or rate-limited.

    Mirrors the detection logic in engine/service.py so subagent failures
    persist globally — preventing future agents from retrying blocked accounts.
    """
    msg = str(exc).lower()
    if not account_name:
        return

    # Rate-limit patterns (same keywords as service.py defense-in-depth)
    rate_kw = ("ratelimit", "rate_limit", "rate limit", "quota", "daily usage", "exceeded", "429")
    if any(kw in msg for kw in rate_kw):
        try:
            from engine.config import mark_account_exhausted
            mark_account_exhausted(account_name)
            logger.warning("Subagent marked account %s as exhausted (rate-limited): %s",
                           account_name, str(exc)[:200])
        except Exception as e:
            logger.error("Failed to mark account %s exhausted: %s", account_name, e)
        return

    # WAF/captcha patterns (same keywords as service.py defense-in-depth)
    waf_kw = ("captcha", "waf", "validate", "rgv587", "blocked", "forbidden",
              "fail_sys_user_validate", "403", "401")
    if any(kw in msg for kw in waf_kw):
        try:
            from engine.config import mark_account_captcha_blocked
            mark_account_captcha_blocked(account_name)
            logger.warning("Subagent marked account %s as captcha/WAF-blocked: %s",
                           account_name, str(exc)[:200])
        except Exception as e:
            logger.error("Failed to mark account %s captcha-blocked: %s", account_name, e)


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
    files: list[str] | None = None,
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
            text, new_pid = await _call_llm(agent, message, parent_id, is_first_turn, files=files)
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

    # Mark the failed account globally so other agents/subagents skip it.
    # Only for Qwen — non-Qwen backends (Gemini/Groq/DeepSeek) don't use browser accounts.
    if backend_type == "qwen" and last_exc and agent.browser_data_dir:
        from pathlib import Path as _Path
        _failed_acct = _Path(agent.browser_data_dir).name
        _classify_and_mark_account_error(last_exc, _failed_acct)

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

            # Clear stale settings on the fallback account (tools + instruction)
            try:
                fb_headers = await _get_agent_qwen_headers(agent)
                await _clear_qwen_account_settings(fb_headers, agent.id)
            except Exception as exc:
                logger.warning("Agent %s: clear settings on fallback account %s failed: %s", agent.id, browser_profile, exc)
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
                    # Reset fallback tracking on success so next request starts fresh
                    agent._fallback_tried = set()
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
    agent: Agent, message: str, parent_id: str | None, is_first_turn: bool,
    files: list[str] | None = None,
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
    if backend in ("gemini", "groq", "mistral", "openai", "cloudflare"):
        return await _call_api_backend(agent, message, backend, system_instruction=agent.system_prompt, files=files)
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


async def _call_api_backend(agent: Agent, message: str, backend: str, *, system_instruction: str | None = None, files: list[str] | None = None) -> tuple[str, str | None]:
    """Gemini / Groq / Mistral: stateless API call with internal key rotation.

    These backends don't need browser tokens — they rotate API keys internally.
    No account assignment needed. Native tool schemas are passed via the tools parameter.
    """
    from connectors import get_connector
    from engine.config import get_model_config
    from engine.tools_loader import get_all_tool_schemas
    from server.api.routes.misc import get_disabled_tools

    connector = get_connector(backend, model_id=agent.model)
    cfg = get_model_config(agent.model)
    api_model_type = cfg.get("api_model_type")

    # Load native tool schemas for this agent's allowed tool groups
    disabled = get_disabled_tools().get("disabled", [])
    native_tools = get_all_tool_schemas(disabled=disabled, allowed=agent.allowed_tool_groups) or None

    accumulated = ""
    # Track native tool calls for agent.messages + conversation file
    _native_tool_calls: list[dict[str, Any]] = []  # [{name, attrs, result, ok}]
    _current_tool: dict[str, Any] | None = None

    async for event in connector.stream_chat(
        message,
        model=api_model_type,
        chat_id=f"agent-{agent.id}",
        inject_instructions=False,
        system_instruction=system_instruction,
        files=files,
        tools=native_tools,
    ):
        etype = event.get("type")
        if etype == "answer":
            chunk_text = event.get("text", "")
            accumulated += chunk_text
            if chunk_text:
                agent.push_stream_event({"type": "chunk", "text": chunk_text})
        elif etype == "error":
            raise RuntimeError(f"{backend}: {event.get('message', 'unknown error')}")
        elif etype == "skill_start":
            # Native tool execution started — forward to panel and start tracking
            agent.push_stream_event(event)
            _current_tool = {
                "name": event.get("name", ""),
                "attrs": event.get("attrs", ""),
                "result_parts": [],
            }
        elif etype == "skill_output":
            # Forward output to panel and collect for conversation
            agent.push_stream_event(event)
            if _current_tool is not None:
                _current_tool["result_parts"].append(event.get("text", ""))
        elif etype == "skill_end":
            # Forward end event and finalize tool tracking
            agent.push_stream_event(event)
            if _current_tool is not None:
                _current_tool["ok"] = event.get("ok", True)
                _current_tool["result"] = "".join(_current_tool["result_parts"])
                _native_tool_calls.append(_current_tool)
                _current_tool = None

    # Append native tool calls/results to agent.messages for conversation file
    import json as _j
    for tc in _native_tool_calls:
        attrs = tc["attrs"]
        if isinstance(attrs, str):
            try:
                attrs = _j.loads(attrs) if attrs else {}
            except Exception:
                attrs = {}
        command_str = _j.dumps({"name": tc["name"], "arguments": attrs}, ensure_ascii=False)
        tool_msg = (
            f"## Tool\n"
            f"**Name:** `{tc['name']}`\n"
            f"**Command:**\n```\n{command_str}\n```\n"
            f"**Output:**\n```\n{tc['result'][:2000]}\n```"
        )
        agent.messages.append({"role": "tool", "content": tool_msg})
        await _persist_message(agent.id, "tool", tool_msg)

    if not accumulated.strip() and not _native_tool_calls:
        raise RuntimeError(f"{backend} returned empty response")
    return accumulated, None


def _cookies_have_identity(cookies: str) -> bool:
    """Check if cookie string contains user-identity cookies required for Qwen history.

    Qwen requires `aui` or `cnaui` cookies to associate chats with an account.
    Without these, requests pass WAF but are treated as anonymous (no history saved).
    """
    return "aui=" in cookies or "cnaui=" in cookies


async def _get_agent_qwen_headers(agent: Agent) -> dict[str, str]:
    """Resolve Qwen WAF headers for an agent based on its assigned browser account.

    Priority: agent.browser_data_dir → role pool → shared service (active).
    Uses cached per-account tokens when available. If no cached token exists
    for the assigned account, launches a headless browser with that profile
    to extract fresh tokens, then closes it.
    Rejects anonymous tokens (missing aui/cnaui) — they pass WAF but don't save history.
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
            cookies = cached["cookies"]
            has_identity = _cookies_have_identity(cookies)
            logger.info(
                "[WAF-TOKEN] agent=%s account=%s cookies_len=%d has_identity=%s bx_ua=%s",
                agent.id, account, len(cookies), has_identity,
                "yes" if cached.get("bx_ua") else "no",
            )
            if not has_identity:
                logger.warning(
                    "[WAF-TOKEN] agent=%s account=%s: cookies lack aui/cnaui (anonymous). "
                    "Launching browser to get authenticated tokens.",
                    agent.id, account,
                )
                # Fall through to browser launch below
            else:
                return build_headers(
                    cookies=cookies,
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

    # Load native tool schemas for this agent's allowed tool groups
    from engine.tools_loader import get_all_tool_schemas
    from server.api.routes.misc import get_disabled_tools
    disabled = get_disabled_tools().get("disabled", [])
    native_tools = get_all_tool_schemas(disabled=disabled, allowed=agent.allowed_tool_groups)

    payload = {
        "model": api_model,
        "messages": messages,
        "stream": True,
    }
    if native_tools:
        payload["tools"] = native_tools

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

    # Tool execution loop — mirrors main chat's LocalConnector
    _max_tool_rounds = 20
    _tool_round = 0
    final_accumulated = ""

    while _tool_round < _max_tool_rounds:
        _tool_round += 1
        accumulated = ""
        _tc_buffers: dict[int, dict] = {}

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

                        # Text content
                        token = delta.get("content", "")
                        if token:
                            accumulated += token
                            agent.push_stream_event({"type": "chunk", "text": token})

                        # Native tool call deltas
                        tc_deltas = delta.get("tool_calls")
                        if tc_deltas:
                            for tcd in tc_deltas:
                                idx = tcd.get("index", 0)
                                if idx not in _tc_buffers:
                                    _tc_buffers[idx] = {"id": tcd.get("id", ""), "name": "", "args_str": ""}
                                buf = _tc_buffers[idx]
                                if tcd.get("id"):
                                    buf["id"] = tcd["id"]
                                fn = tcd.get("function", {})
                                if fn.get("name"):
                                    buf["name"] = fn["name"]
                                if fn.get("arguments"):
                                    buf["args_str"] += fn["arguments"]
                    except (json.JSONDecodeError, KeyError, IndexError):
                        pass

        # If no native tool calls, we're done
        if not _tc_buffers:
            final_accumulated = accumulated
            break

        # Convert native tool calls to <tool_call> tags for the agent loop's parser
        for idx in sorted(_tc_buffers.keys()):
            buf = _tc_buffers[idx]
            try:
                args = json.loads(buf["args_str"]) if buf["args_str"] else {}
            except json.JSONDecodeError:
                args = {}

            # Build <tool_call> tag matching Hermes format
            args_json = json.dumps(args, ensure_ascii=False)
            tag_block = f'<tool_call>{{"name": "{buf["name"]}", "arguments": {args_json}}}</tool_call>'
            accumulated += "\n" + tag_block

        # Save assistant message with tool_calls to messages for proper history
        assistant_msg: dict[str, Any] = {"role": "assistant", "content": accumulated or None}
        assistant_msg["tool_calls"] = [
            {
                "id": _tc_buffers[idx]["id"],
                "type": "function",
                "function": {
                    "name": _tc_buffers[idx]["name"],
                    "arguments": _tc_buffers[idx]["args_str"] or "{}",
                },
            }
            for idx in sorted(_tc_buffers.keys())
        ]
        messages.append(assistant_msg)

        # Execute each tool call and append results to messages
        from engine.skills import get_skill_engine
        from connectors.common.native_tools import native_call_to_tag_event, format_openai_tool_result
        engine = get_skill_engine()

        for idx in sorted(_tc_buffers.keys()):
            buf = _tc_buffers[idx]
            try:
                args = json.loads(buf["args_str"]) if buf["args_str"] else {}
            except json.JSONDecodeError:
                args = {}

            fc = {"name": buf["name"], "args": args, "id": buf["id"]}
            tag_event = native_call_to_tag_event(fc)

            agent.push_stream_event({"type": "skill_start", "name": buf["name"], "attrs": str(args)})

            try:
                events = await asyncio.to_thread(
                    lambda: list(engine.process_tag(
                        tag_event["name"], tag_event["attrs"],
                        tag_event["content"], namespace=agent.id,
                    ))
                )
            except Exception as exc:
                events = [{"type": "skill_end", "name": buf["name"], "ok": False, "error": str(exc)}]

            result_text = ""
            ok = True
            for evt in events:
                if isinstance(evt, dict):
                    if evt.get("type") == "skill_output":
                        result_text += evt.get("text", "")
                    elif evt.get("type") == "skill_end":
                        ok = evt.get("ok", True)
                    if evt.get("type") != "skill_start":
                        agent.push_stream_event(evt)

            tool_result = format_openai_tool_result(buf["name"], result_text, ok, buf["id"])
            messages.append(tool_result)

            # Also update agent.messages + persist for conversation file / side panel
            _attrs_str = json.dumps(args, ensure_ascii=False)
            _tool_msg = (
                f"## Tool\n"
                f"**Name:** `{buf['name']}`\n"
                f"**Command:**\n```\n{_attrs_str}\n```\n"
                f"**Output:**\n```\n{result_text[:2000]}\n```"
            )
            agent.messages.append({"role": "tool", "content": _tool_msg})
            await _persist_message(agent.id, "tool", _tool_msg)

            logger.info("Agent %s: native tool %s executed (ok=%s), continuing loop", agent.id, buf["name"], ok)

        # Update payload with updated messages for next round
        payload["messages"] = messages
        # Don't accumulate across rounds — only the final text-only response matters
        final_accumulated = accumulated

    if not final_accumulated.strip():
        raise RuntimeError("Local model returned empty response")
    return final_accumulated, None


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

    # Diagnostic: print which WAF token is actually being used for this call
    from pathlib import Path as _P
    _acct = _P(agent.browser_data_dir).name if agent.browser_data_dir else "default"
    _ck = headers.get("Cookie", "")
    logger.info(
        "[WAF-CALL] agent=%s account=%s model=%s is_first=%s cookies_len=%d has_aui=%s",
        agent.id, _acct, agent.model, is_first_turn, len(_ck),
        "aui=" in _ck or "cnaui=" in _ck,
    )

    # Create or reuse upstream Qwen session
    # Prefer DB-stored upstream_session_id; fall back to in-memory cache
    from server.database import get_upstream_session_id as _get_usid, set_upstream_session_id as _set_usid
    chat_id = _get_usid(agent.chat_id) or agent.qwen_session_id
    if is_first_turn or not chat_id:
        # Disable Qwen built-in tools and clear personalization for the assigned account.
        # Only if agent has its own browser profile — never touch Maria's active session.
        if agent.browser_data_dir:
            try:
                await _clear_qwen_account_settings(headers, agent.id)
            except Exception as exc:
                logger.warning("Agent %s: clear account settings failed: %s", agent.id, exc)
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

    max_attempts = 3
    last_error: str | None = None

    for attempt in range(1, max_attempts + 1):
        accumulated = ""
        new_parent_id: str | None = None

        try:
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
        except (httpx.ConnectError, httpx.ReadTimeout) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            logger.warning("Agent %s Qwen call attempt %d/%d failed: %s", agent.id, attempt, max_attempts, last_error)
            if attempt < max_attempts:
                await asyncio.sleep(1 * attempt)
                continue
            raise RuntimeError(f"Qwen call failed after {max_attempts} attempts: {last_error}")

        if accumulated.strip():
            return accumulated, new_parent_id

        # Empty response — retry
        last_error = "empty response (HTTP 200 with zero content)"
        logger.warning(
            "Agent %s Qwen call attempt %d/%d returned empty response, retrying...",
            agent.id, attempt, max_attempts,
        )
        if attempt < max_attempts:
            await asyncio.sleep(1 * attempt)
            continue

    raise RuntimeError(f"Qwen returned empty response after {max_attempts} attempts")


_SKIP_TAGS = frozenset(("action", "spawn_agent", "br", "hr", "json", "p", "div", "span"))



def _check_action_wrapper_violations(text: str) -> str | None:
    """Check for tool_call format violations in LLM response.

    Returns a warning message string if violations found, None otherwise.
    Checks:
    1. JSON tool calls not wrapped in tool_call blocks
    2. Orphan closing tool_call tags without matching opener
    """
    from engine.skills.parser import KNOWN_TAGS

    # Support both <tool_call (Hermes) and <action> (Qwen native) wrappers.
    has_open = ("<tool" + "_call>" in text) or "<action>" in text
    has_close = ("</tool" + "_call>" in text) or "</action>" in text

    # Check for orphan closing tag
    if has_close and not has_open:
        return ORPHAN_CLOSE_TAG_WARNING

    # Check for JSON tool calls outside tool_call blocks
    # Strip all proper tool_call blocks first
    stripped = _TAG_RE.sub("", text)

    # Check if any known tool names appear in JSON format outside blocks
    tool_pat = re.compile(
        r'"name"\s*:\s*"(' + '|'.join(re.escape(t) for t in KNOWN_TAGS) + r')"',
        re.IGNORECASE
    )
    if tool_pat.search(stripped):
        return ACTION_WRAPPER_WARNING

    return None


# DSML regexes for DeepSeek V4 tool calling (optional leading ｜)
_DSML_BLOCK_RE = re.compile(
    r'<\uff5c?DSML\uff5ctool_calls>(.*?)</\uff5c?DSML\uff5ctool_calls>',
    re.DOTALL,
)
_DSML_INVOKE_RE = re.compile(
    r'<\uff5c?DSML\uff5cinvoke\s+name="([^"]+)">(.*?)</\uff5c?DSML\uff5cinvoke>',
    re.DOTALL,
)
_DSML_PARAM_RE = re.compile(
    r'<\uff5c?DSML\uff5cparameter\s+name="([^"]+)"\s+string="(true|false)">(.*?)</\uff5c?DSML\uff5cparameter>',
    re.DOTALL,
)


def _parse_dsml_block(block_text: str) -> list[dict[str, Any]]:
    """Parse a DSML tool_calls block into canonical tag dicts."""
    import json as _json
    results = []
    for inv_match in _DSML_INVOKE_RE.finditer(block_text):
        fn_name = inv_match.group(1)
        params_text = inv_match.group(2)
        args: dict[str, Any] = {}
        for p_match in _DSML_PARAM_RE.finditer(params_text):
            pname = p_match.group(1)
            is_string = p_match.group(2) == "true"
            raw_val = p_match.group(3).strip()
            if is_string:
                args[pname] = raw_val
            else:
                try:
                    args[pname] = _json.loads(raw_val)
                except (_json.JSONDecodeError, TypeError):
                    args[pname] = raw_val
        results.append({
            "name": fn_name,
            "attrs": _json.dumps(args, ensure_ascii=False),
            "content": "",
            "raw": inv_match.group(0),
        })
    return results


def _parse_skill_tags(text: str) -> list[dict[str, Any]]:
    """Extract tool calls from LLM response.

    Tries in order:
    1. Hermes JSON format: <tool_call>{JSON}</tool_call>
    2. DSML format: <｜DSML｜tool_calls><｜DSML｜invoke>...</｜DSML｜invoke></｜DSML｜tool_calls>
    3. Legacy XML fallback: bare <tag attrs>content</tag>
    """
    import json as _json
    from engine.skills.parser import _parse_action_payload

    tags = []

    # 1. Parse JSON tool calls from <tool_call> blocks
    for match in _TAG_RE.finditer(text):
        content = match.group(1).strip()
        if not content:
            continue
        calls = _parse_action_payload(content)
        for call in calls:
            tags.append({
                "name": call["name"],
                "attrs": call["attrs"],
                "content": call.get("content", ""),
                "raw": match.group(0),
            })

    # 2. Try DSML format (DeepSeek V4)
    if not tags:
        for block_match in _DSML_BLOCK_RE.finditer(text):
            dsml_tags = _parse_dsml_block(block_match.group(0))
            tags.extend(dsml_tags)

    # 3. Fallback: try legacy XML tag extraction if nothing found
    if not tags:
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
