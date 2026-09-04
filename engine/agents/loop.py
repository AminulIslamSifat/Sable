
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
from engine.agents.registry import get_role_config
from engine.skills.parser import SkillParser

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
    """Execute the full agent loop using the SAME pipeline as main chat.

    Same LLM call, same SkillParser, same DB schema (messages table with skill_events),
    same guards (MainChatGuard), same format/todo warnings, same history loading.
    Only difference: chat_id = agent.id, provider = 'agent', and timeout wrapping.
    """
    from engine.agents.resilience import MainChatGuard
    from server.database import add_message, update_message, touch_chat, get_messages
    from server.database import get_upstream_session_id, set_upstream_session_id
    from engine.token_counter import count_prompt_tokens, count_completion_tokens

    role_cfg = get_role_config(agent.role)
    lim = limits or {}
    max_iterations = lim.get("max_iterations", MAX_ITERATIONS)

    # --- Same guards as main chat ---
    _guard = MainChatGuard(provider=_get_backend_type(agent.model))
    _loop_detector = LoopDetector()
    _turn_caps = TurnCapTracker()

    # Store allowed tool groups on agent for native tool passing to API backends
    agent.allowed_tool_groups = list(role_cfg.allowed_tools)

    # --- Build system prompt (same as before — shared instruction builder) ---
    from engine.config import get_model_config as _get_agent_model_cfg
    _agent_backend = _get_agent_model_cfg(agent.model).get("api_backend", "")

    system_prompt = role_cfg.system_prompt
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

    # --- Ensure chat exists in DB (same as main chat's ensure_chat) ---
    from server.database import ensure_chat as _ensure_chat
    _ensure_chat(agent.id, f"Agent: {agent.role}", mode="api", provider="agent")

    # --- Load history from DB (SAME as main chat) ---
    _history = get_messages(agent.id, include_skill_events=True)
    agent.messages = []
    if _history:
        for msg in _history:
            agent.messages.append({"role": msg["role"], "content": msg["content"]})
    else:
        # First run: save system + user messages to DB
        agent.messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Context: {agent.context}\n\nTask: {agent.task}" if agent.context else agent.task},
        ]
        add_message(agent.id, "system", system_prompt)
        add_message(agent.id, "user", agent.messages[1]["content"])
        touch_chat(agent.id)

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

    # --- Session state ---
    parent_id: str | None = get_upstream_session_id(agent.id)
    is_first_turn = parent_id is None
    consecutive_failures = 0
    failure_threshold = _get_teacher_failure_threshold()

    # --- Main loop ---
    first_message = system_prompt
    if agent.context:
        first_message += f"\n\nContext: {agent.context}\n\nTask: {agent.task}"
    else:
        first_message += f"\n\nTask: {agent.task}"

    current_message = first_message
    _pending_agent_images: list[str] = []
    _round_tool_errors: dict[str, str] = {}
    _saved_msg_id: int | None = None
    _all_skill_events: list[dict[str, Any]] = []
    _todo_stall_counter: int = 0  # Track iterations without todo progress
    _last_todo_index: int = agent.todos.current_index if agent.todos else -1

    # Skill event types to persist (same as main chat)
    _SKILL_EVENT_TYPES = frozenset({
        "skill_start", "skill_output", "skill_end",
        "file_edit", "permission_request", "cwd_warning",
        "round_thinking", "round_text",
    })

    for iteration in range(max_iterations):
        agent.push_stream_event({"type": "iteration", "iteration": iteration + 1})

        # --- Check for user-injected guidance messages ---
        if agent.pending_user_messages:
            guidance = "\n\n".join(agent.pending_user_messages)
            agent.pending_user_messages.clear()
            guidance_msg = f"[USER GUIDANCE]\n{guidance}"
            agent.messages.append({"role": "user", "content": guidance_msg})
            add_message(agent.id, "user", guidance_msg)
            touch_chat(agent.id)
            current_message = guidance_msg
            _guard.reset()  # Reset guards on new user input (same as main chat)

        # --- Todo stall detection ---
        if agent.todos and not agent.todos.all_done:
            if agent.todos.current_index == _last_todo_index:
                _todo_stall_counter += 1
                if _todo_stall_counter >= 3:
                    _stall_warn = (
                        f"[GUARD][TODO WARNING] You have been on task {agent.todos.current.id} "
                        f"('{agent.todos.current.content}') for {_todo_stall_counter} iterations without progress. "
                        f"Either complete it with todo_complete, skip it with todo_skip, or try a different approach."
                    )
                    agent.messages.append({"role": "system", "content": _stall_warn})
                    add_message(agent.id, "system", _stall_warn)
                    agent.push_stream_event({"type": "guard_warning", "text": _stall_warn})
            else:
                _todo_stall_counter = 0
                _last_todo_index = agent.todos.current_index

        # --- Call LLM with SkillParser streaming (SAME as main chat) ---
        _files_for_round: list[str] | None = _pending_agent_images or None
        _pending_agent_images = []

        _parser = SkillParser()
        _parser_events: list[dict[str, Any]] = []
        _response_chunks: list[str] = []
        _raw_chunks: list[str] = []  # Raw text before parser stripping (for guard checks)
        _original_push = agent.push_stream_event

        def _persist_skill_event(event: dict[str, Any]) -> None:
            etype = event.get("type", "")
            if etype not in _SKILL_EVENT_TYPES:
                return
            _all_skill_events.append(event)

        # --- Get LLM event source with browser profile fallback (Qwen only) ---
        _browser_retry_max = 3
        _browser_retry_count = 0
        _llm_success = False

        while not _llm_success:
            _event_source, _initial_pid = await _get_llm_event_source(
                agent, current_message, parent_id, is_first_turn, files=_files_for_round
            )
            new_parent_id: str | None = _initial_pid
            response_text = ""
            _hit_blockable_error = False
            _block_error_type = ""

            # Iterate events directly — feed chunks through SkillParser inline
            try:
                async for event in _event_source:
                    etype = event.get("type")

                    if etype in ("chunk", "answer"):
                        chunk_text = event.get("text", "")
                        if chunk_text:
                            _raw_chunks.append(chunk_text)
                            for parsed_event in _parser.feed(chunk_text):
                                _parser_events.append(parsed_event)
                                _persist_skill_event(parsed_event)
                                if parsed_event.get("type") == "text":
                                    _response_chunks.append(parsed_event["text"])
                                    _original_push({"type": "chunk", "text": parsed_event["text"]})
                                elif parsed_event.get("type") == "tool_pending":
                                    _original_push(parsed_event)

                    elif etype == "thinking":
                        chunk_text = event.get("text", "")
                        if chunk_text:
                            _original_push({"type": "thinking", "text": chunk_text})

                    elif etype == "meta":
                        sid = event.get("chat_id")
                        if sid:
                            agent.qwen_session_id = sid
                            set_upstream_session_id(agent.chat_id, sid)
                        pid = event.get("parent_id")
                        if pid:
                            new_parent_id = pid

                    elif etype == "done":
                        pid = event.get("parent_id")
                        if pid:
                            new_parent_id = pid

                    elif etype == "error":
                        _err_msg = str(event.get("message", "")).lower()
                        # Chat-in-progress: IMMEDIATELY stop upstream, then retry with fresh session
                        if any(kw in _err_msg for kw in (
                            "chat in progress", "generation in progress", "already generating",
                            "task in progress", "please wait", "busy", "concurrent request",
                        )):
                            logger.info("[agent %s] Chat-in-progress detected — calling stop API immediately", agent.id)
                            _cip_svc = getattr(agent, '_qwen_service', None)
                            if _cip_svc:
                                try:
                                    _cip_chat = agent.qwen_session_id or get_upstream_session_id(agent.chat_id)
                                    if _cip_chat:
                                        await _cip_svc._stop_upstream_generation(_cip_chat)
                                except Exception as _cip_exc:
                                    logger.warning("[agent %s] Stop API failed: %s", agent.id, _cip_exc)
                            # Reset session so next attempt creates a fresh chat
                            agent.qwen_session_id = None
                            parent_id = None  # Reset parent_id to avoid stale reference
                            is_first_turn = True  # Fresh session needs account setup
                            try:
                                set_upstream_session_id(agent.chat_id, None)
                            except Exception:
                                pass
                            agent.push_stream_event({
                                "type": "guard_warning",
                                "text": "[CHAT IN PROGRESS] Stopped upstream generation, retrying with fresh session...",
                            })
                            # Clear parser state and retry on SAME profile (not a profile issue)
                            _parser = SkillParser()
                            _parser_events.clear()
                            _response_chunks.clear()
                            _raw_chunks.clear()
                            await asyncio.sleep(3)
                            continue  # Retry same iteration with fresh session
                        raise RuntimeError(f"LLM error: {event.get('message', 'unknown')}")

                    elif etype in ("rate_limited", "waf_blocked"):
                        _hit_blockable_error = True
                        _block_error_type = etype
                        break  # Exit stream to retry with different profile

                    elif etype in ("chat_not_found", "parent_not_found"):
                        # Stale session/parent — reset and retry with fresh session
                        logger.info("[agent %s] %s detected — resetting session for fresh start", agent.id, etype)
                        agent.qwen_session_id = None
                        parent_id = None
                        is_first_turn = True  # Fresh session needs account setup
                        try:
                            set_upstream_session_id(agent.chat_id, None)
                        except Exception:
                            pass
                        agent.push_stream_event({
                            "type": "guard_warning",
                            "text": f"[{etype.upper()}] Resetting session and retrying with fresh chat...",
                        })
                        _parser = SkillParser()
                        _parser_events.clear()
                        _response_chunks.clear()
                        _raw_chunks.clear()
                        continue  # Retry with fresh session

                    # status, debug, request_sent — internal, skip

            finally:
                # Clean up Qwen service if created
                _svc = getattr(agent, '_qwen_service', None)
                if _svc:
                    try:
                        await _svc.close()
                    except Exception:
                        pass
                    agent._qwen_service = None

            # --- Browser profile fallback on rate_limited / waf_blocked ---
            if _hit_blockable_error and _browser_retry_count < _browser_retry_max:
                from engine.agents.registry import get_next_account
                from engine.config import _SYSTEM as _AGENT_SYSTEM_DIR
                from pathlib import PurePosixPath

                _current_name = PurePosixPath(agent.browser_data_dir).name if agent.browser_data_dir else ""
                _in_use = {_current_name}  # Skip current blocked profile
                _next_profile = get_next_account(agent.role, _in_use)

                if _next_profile:
                    _browser_retry_count += 1
                    _old_profile = agent.browser_data_dir
                    _new_path = _AGENT_SYSTEM_DIR / _next_profile
                    agent.browser_data_dir = str(_new_path) if _new_path.is_dir() else _next_profile

                    # Reset upstream session so a NEW chat ID is created on the new profile
                    agent.qwen_session_id = None
                    parent_id = None  # Reset parent_id to avoid stale reference on new profile
                    is_first_turn = True  # New profile needs fresh account setup
                    try:
                        set_upstream_session_id(agent.chat_id, None)
                    except Exception:
                        pass

                    logger.info(
                        "[agent %s] Browser profile fallback #%d: %s → %s (reason: %s)",
                        agent.id, _browser_retry_count, _old_profile, agent.browser_data_dir, _block_error_type,
                    )
                    agent.push_stream_event({
                        "type": "guard_warning",
                        "text": f"[PROFILE SWITCH] {_block_error_type} on {_current_name}, switching to {_next_profile} (attempt {_browser_retry_count}/{_browser_retry_max})",
                    })

                    # Clear parser state for retry
                    _parser = SkillParser()
                    _parser_events.clear()
                    _response_chunks.clear()
                    _raw_chunks.clear()
                    continue  # Retry with new profile
                else:
                    # No more profiles available — propagate the error
                    raise RuntimeError(f"{_block_error_type}: all browser profiles exhausted")

            elif _hit_blockable_error:
                raise RuntimeError(f"{_block_error_type}: all browser profile retries exhausted")

            _llm_success = True

        # Flush remaining parser buffer
        for parsed_event in _parser.flush():
            _parser_events.append(parsed_event)
            _persist_skill_event(parsed_event)
            if parsed_event.get("type") == "text":
                _response_chunks.append(parsed_event["text"])
                _original_push({"type": "chunk", "text": parsed_event["text"]})

        # --- Use parser-cleaned text (same as main chat) ---
        _clean_response = "".join(_response_chunks)
        _raw_round_text = "".join(_raw_chunks)
        response_text = _clean_response
        _original_push({"type": "answer", "text": _clean_response})

        if new_parent_id:
            parent_id = new_parent_id
            set_upstream_session_id(agent.id, new_parent_id)
        is_first_turn = False

        agent.messages.append({"role": "assistant", "content": _clean_response})

        # --- Persist to messages table ONLY (same as main chat, NO agent_messages) ---
        try:
            if _saved_msg_id is None:
                _saved_msg_id = add_message(
                    agent.id, "assistant", _clean_response,
                    skill_events=_all_skill_events or None,
                )
            else:
                update_message(
                    _saved_msg_id, _clean_response,
                    skill_events=_all_skill_events or None,
                )
            touch_chat(agent.id)
        except Exception as exc:
            logger.debug("Failed to persist agent assistant msg: %s", exc)

        # Extract parsed tool calls from SkillParser events
        tags = [
            {"name": ev["name"], "attrs": ev.get("attrs", {}), "content": ev.get("content", "")}
            for ev in _parser_events
            if ev.get("type") == "tag_found"
        ]

        # --- SAME guard checks as main chat (malformed + incomplete) ---
        _guard_warnings: list[str] = []
        _tools_executed = len(tags) > 0
        _malform_warn = _guard.check_malformed_action(_raw_round_text)
        if _malform_warn:
            _guard_warnings.append(_malform_warn)
        if not _malform_warn:
            _incomplete_warn = _guard.check_incomplete_action(_raw_round_text, _tools_executed)
            if _incomplete_warn:
                _guard_warnings.append(_incomplete_warn)

        if not tags:
            # Inject any guard warnings before validation
            if _guard_warnings:
                _warn_msg = "\n\n".join(_guard_warnings)
                agent.messages.append({"role": "system", "content": _warn_msg})
                add_message(agent.id, "system", _warn_msg)

            # If output is already valid markdown, we're done
            if _validate_markdown_output(response_text, role_cfg.required_sections):
                return response_text

            # Otherwise nudge: continue working OR provide final report
            nudge = (
                "Your last response did not include tool calls and does not look like a complete final report. "
                "If the task is still incomplete, continue by calling the appropriate tools. "
                "If you are finished, provide your final markdown report now."
            )
            agent.messages.append({"role": "system", "content": nudge})
            add_message(agent.id, "system", nudge)
            agent.push_stream_event({"type": "guard_warning", "text": nudge})
            continue

        # --- Loop detection + per-turn caps (same as main chat) ---
        tool_results: list[str] = []
        _loop_blocked_tools: set[int] = set()
        for _tag_idx, tag in enumerate(tags):
            _tag_name = tag["name"]
            _tag_args = str(tag.get("attrs", ""))

            # Record for MainChatGuard loop detection (same as main chat)
            _guard.record_command(_tag_name, _tag_args)

            # Per-turn cap check
            _cap_warn = _turn_caps.check_and_record(_tag_name)
            if _cap_warn:
                tool_results.append(_cap_warn)
                _loop_blocked_tools.add(_tag_idx)
                continue

            # LoopDetector with error context + recovery support
            _prev_err = _round_tool_errors.get(_tag_name, "")
            _decision = _loop_detector.check_decision(_tag_name, _tag_args, error_msg=_prev_err)

            if _decision.action == "block":
                tool_results.append(_decision.message or f"[HARD STOP] {_tag_name} blocked.")
                _loop_blocked_tools.add(_tag_idx)

            elif _decision.action == "recover":
                logger.info("[agent %s] Guardrail recovery triggered for '%s'", agent.id, _tag_name)
                agent.push_stream_event({"type": "guardrail_recovery", "tool": _tag_name})
                agent.qwen_session_id = None
                parent_id = None
                is_first_turn = True
                set_upstream_session_id(agent.id, None)
                _recovery_prompt = _loop_detector.get_recovery_prompt(
                    _decision.recovery_key,
                    original_task=agent.task or "",
                )
                agent.messages = [
                    {"role": "system", "content": agent.messages[0]["content"]},
                    {"role": "user", "content": agent.messages[1]["content"]},
                ]
                agent.messages.append({"role": "user", "content": _recovery_prompt})
                add_message(agent.id, "user", _recovery_prompt)
                touch_chat(agent.id)
                current_message = _recovery_prompt
                for _blk_idx in range(len(tags)):
                    _loop_blocked_tools.add(_blk_idx)
                break

            elif _decision.action == "warn":
                teacher_guidance = await _try_teacher_escalation(
                    agent, "Agent is repeating the same tool calls with identical arguments."
                )
                if teacher_guidance:
                    warning_msg = f"[MENTOR INTERVENTION]\n{teacher_guidance}"
                else:
                    warning_msg = _decision.message or ""
                agent.messages.append({"role": "system", "content": warning_msg})
                add_message(agent.id, "system", warning_msg)

        # --- MainChatGuard loop/failure warnings (same as main chat) ---
        _loop_warn = _guard.check_loop()
        if _loop_warn:
            _guard_warnings.append(_loop_warn)
        _fail_warn = _guard.check_failures()
        if _fail_warn:
            _guard_warnings.append(_fail_warn)

        # --- Execute skills ---
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

                # Stream skill_start to panel (handler events with matching id are persisted below)
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

                # Forward skill events to panel stream and persist for history replay
                # (skip duplicate skill_start — the explicit one above already created the live card)
                for evt in events:
                    if isinstance(evt, dict):
                        _persist_skill_event(evt)
                        if evt.get("type") != "skill_start":
                            agent.push_stream_event(evt)

                # Collect image paths from skill results
                for _evt in events:
                    if isinstance(_evt, dict) and _evt.get("type") == "skill_end" and _evt.get("ok"):
                        _res = _evt.get("result", {})
                        if _res.get("kind") == "image" and _res.get("path"):
                            _round_image_paths.append(_res["path"])

                feedback = build_tool_feedback(events)
                feedback = _loop_detector.record_result(
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

                # Track failures for MainChatGuard (same as main chat)
                _tool_failed = any(
                    isinstance(_e, dict) and _e.get("type") == "skill_end" and not _e.get("ok", True)
                    for _e in events
                )
                _guard.record_result(not _tool_failed)

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
                _guard.record_result(False)
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

        # --- Build feedback message (same as main chat) ---
        combined = "\n---\n".join(tool_results)

        # If no tool feedback but guard warnings exist, use warnings as feedback
        if not combined.strip() and _guard_warnings:
            combined = "\n\n".join(_guard_warnings)
        elif _guard_warnings:
            combined += "\n\n" + "\n\n".join(_guard_warnings)

        current_message = f"<tool_response>\\n{combined}\\n</tool_response>"

        # Append TODO state
        if agent.todos:
            state_line = agent.todos.format_state()
            if state_line:
                current_message += f"\n\n{state_line}"

        agent.messages.append({"role": "user", "content": current_message})

        # --- Update assistant message with accumulated skill events ---
        if _saved_msg_id is not None:
            try:
                update_message(_saved_msg_id, _clean_response, skill_events=_all_skill_events or None)
            except Exception:
                pass

    # Hit max iterations — force final answer
    force_msg = "Maximum steps reached. Provide your final markdown answer NOW with whatever you have. Use proper ## headers for each section."
    agent.messages.append({"role": "user", "content": force_msg})
    add_message(agent.id, "user", force_msg)
    touch_chat(agent.id)
    response_text, _ = await _call_llm_simple(agent, force_msg, parent_id)
    return response_text


async def _try_teacher_escalation(agent: Agent, stuck_reason: str) -> str | None:
    """Request teacher guidance from main chat (Maria) via auto_turn engine.

    Instead of calling a separate LLM, this sends the escalation to the main
    chat session. Maria responds using the teacher_guidance tool call, which
    gets routed back to the waiting subagent.

    Respects the max intervention limit to avoid infinite escalation loops.
    """
    from engine.agents.teacher import MAX_TEACHER_INTERVENTIONS

    if agent.teacher_interventions >= MAX_TEACHER_INTERVENTIONS:
        return None

    agent.teacher_interventions += 1
    agent.push_stream_event({
        "type": "teacher_escalation",
        "intervention": agent.teacher_interventions,
        "reason": stuck_reason,
    })

    # Build context snapshot for Maria
    todo_snapshot = None
    if agent.todos:
        todo_snapshot = [
            {"id": t.id, "content": t.content, "status": t.status, "result": t.result}
            for t in agent.todos.todos
        ]

    recent_messages = agent.messages[-6:] if agent.messages else None

    # Request guidance from main chat — blocks until Maria responds via tool call
    from engine.agents.auto_turn import auto_turn
    guidance = await auto_turn.request_teacher_guidance(
        chat_id=agent.chat_id,
        agent_id=agent.id,
        role=agent.role,
        task=agent.task,
        stuck_reason=stuck_reason,
        todo_snapshot=todo_snapshot,
        context=agent.context,
        recent_messages=recent_messages,
    )

    if guidance:
        logger.info("[agent %s] Teacher (main chat) intervened (#%d)", agent.id, agent.teacher_interventions)
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


async def _call_llm_simple(
    agent: Agent,
    message: str,
    parent_id: str | None,
) -> tuple[str, str | None]:
    """One-shot LLM call using the same event source as main chat.

    Returns (accumulated_text, new_parent_id).
    Used for format reminders and forced final answers.
    """
    from server.database import set_upstream_session_id

    event_source, _ = await _get_llm_event_source(agent, message, parent_id, is_first_turn=False)
    accumulated = ""
    new_parent_id: str | None = None

    try:
        async for event in event_source:
            etype = event.get("type")
            if etype in ("chunk", "answer"):
                accumulated += event.get("text", "")
            elif etype == "meta":
                sid = event.get("chat_id")
                if sid:
                    agent.qwen_session_id = sid
                    set_upstream_session_id(agent.chat_id, sid)
                pid = event.get("parent_id")
                if pid:
                    new_parent_id = pid
            elif etype == "done":
                pid = event.get("parent_id")
                if pid:
                    new_parent_id = pid
            elif etype == "error":
                _err_msg = str(event.get("message", "")).lower()
                if any(kw in _err_msg for kw in (
                    "chat not found", "parent_not_found", "not exist",
                )):
                    # Stale session in simple call — reset and return empty
                    agent.qwen_session_id = None
                    try:
                        set_upstream_session_id(agent.chat_id, None)
                    except Exception:
                        pass
                    break  # Return whatever we have (likely empty)
                raise RuntimeError(f"LLM error: {event.get('message', 'unknown')}")
            elif etype in ("rate_limited", "waf_blocked"):
                raise RuntimeError(f"{etype}: {event.get('message', '')}")
            elif etype in ("chat_not_found", "parent_not_found"):
                # Stale session in simple call — reset and return empty
                agent.qwen_session_id = None
                try:
                    set_upstream_session_id(agent.chat_id, None)
                except Exception:
                    pass
                break  # Return whatever we have (likely empty)
    finally:
        _svc = getattr(agent, '_qwen_service', None)
        if _svc:
            try:
                await _svc.close()
            except Exception:
                pass
            agent._qwen_service = None

    return accumulated, new_parent_id


async def _get_llm_event_source(
    agent: Agent,
    message: str,
    parent_id: str | None,
    is_first_turn: bool,
    files: list[str] | None = None,
) -> tuple[Any, str | None]:
    """Get the same LLM event source as main chat.

    Returns (async_generator_of_events, initial_parent_id).
    The caller iterates the generator directly, feeding chunks through SkillParser.
    This mirrors chat.py's round_event_source selection exactly.
    """
    from engine.config import get_model_config
    from server.utils import _is_api_model, _resolve_api_backend

    model = agent.model
    cfg = get_model_config(model)
    api_backend = cfg.get("api_backend") if cfg else None

    # --- API backends (Gemini, DeepSeek, Groq, Mistral, OpenAI, Cloudflare, Local) ---
    if _is_api_model(model):
        from connectors import get_connector

        backend = api_backend or _resolve_api_backend(model)
        connector = get_connector(backend, model_id=model)
        api_model = cfg.get("api_model_type", cfg["id"]) if cfg else model

        stream_kwargs: dict[str, Any] = dict(
            message=message,
            model=api_model,
            chat_id=agent.id,
            inject_instructions=False,  # agents have their own system prompt
            project_id=None,
        )

        # Pass system prompt for backends that support it
        if agent.system_prompt:
            stream_kwargs["system_instruction"] = agent.system_prompt

        # Pass files if supported
        if files:
            stream_kwargs["files"] = files

        # Native tool schemas
        try:
            from engine.tools_loader import get_all_tool_schemas
            tool_schemas = get_all_tool_schemas([])
            if tool_schemas:
                stream_kwargs["tools"] = tool_schemas
        except Exception:
            pass

        return connector.stream_chat(**stream_kwargs), None

    # --- Qwen (scraper-based) ---
    from engine.service import ChatService
    from server.database import get_upstream_session_id, set_upstream_session_id

    svc = ChatService(user_data_dir=agent.browser_data_dir)
    agent._qwen_service = svc  # store for cleanup

    # Clear Qwen built-in tools/personalization on first turn
    if is_first_turn and agent.browser_data_dir:
        try:
            headers = await svc._ensure_headers()
            await _clear_qwen_account_settings(headers, agent.id)
        except Exception as exc:
            logger.warning("Agent %s: clear account settings failed: %s", agent.id, exc)

    chat_id = get_upstream_session_id(agent.chat_id) or agent.qwen_session_id
    if is_first_turn or not chat_id:
        chat_id = None

    return svc.stream_events(
        message=message,
        chat_id=chat_id,
        parent_id=parent_id,
        model=model,
    ), None


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


async def _persist_message(
    agent_id: str,
    role: str,
    content: str,
    skill_events: list[dict[str, Any]] | None = None,
) -> None:
    """Write to messages table only (same as main chat). No separate agent_messages."""
    try:
        from server.database import add_message, touch_chat
        add_message(chat_id=agent_id, role=role, content=content, skill_events=skill_events)
        touch_chat(agent_id)
    except Exception as exc:
        logger.debug("Failed to persist agent message (%s/%s): %s", agent_id, role, exc)
