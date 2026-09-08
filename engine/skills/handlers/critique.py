"""Critique tool — spawns an inline review session in the current chat.

Runs a fresh LLM conversation with critique-specific system prompt,
streams events back as critique_* SSE types, and returns the final
report for injection into the parent chat.

Architecture:
- Handler is a sync generator (like all handlers): (tag_id, name, attrs, content)
- Runs in a thread-pool worker via ExecutionMiddleware
- Uses asyncio.run_coroutine_threadsafe to call the LLM on the main loop
- Yields critique_* events that flow through middleware → SSE stream
- Final report returned in skill_end result, injected as user message
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Generator

logger = logging.getLogger("sable")

_CRITIQUE_SYSTEM_PROMPT = """\
You are a Critique Agent. Your job is to evaluate work that has been done and produce a structured review report.

You have access to the same tools as the main assistant (file reading, command execution, browser, etc.).
Use them to thoroughly inspect the work before judging.

## Your Process
1. Read/inspect the files or context provided
2. Run tests, check builds, view files as needed
3. Evaluate against the given criteria
4. Produce your final report

## Final Report Format
When you are done inspecting, your FINAL response (no tool calls after) must be exactly this format:

### Mark: [X/10]
(One-line summary of the score)

### Why
(Detailed explanation of why you gave this mark. Be specific about what works and what doesn't.)

### Suggestions
(Numbered list of concrete, actionable improvements)

Rules:
- Be honest and critical. Don't inflate scores.
- Back every criticism with specifics (file names, line numbers, exact issues).
- Suggestions must be actionable, not vague.
- Your final response IS the report. Do not add anything after it.
"""


def handle_critique(
    tag_id: str,
    name: str,
    attrs: dict[str, str],
    content: str,
) -> Generator[dict[str, Any], None, None]:
    """Execute a critique session inline.

    Yields critique_* events that the frontend renders as an inline box.
    The final report is returned in the skill_end result.
    """
    from engine.skills.events import end_event as _end_event, output_event as _output_event
    from engine.platform_paths import home_dir

    started = time.time()

    try:
        args = json.loads(content) if isinstance(content, str) else content
    except (json.JSONDecodeError, TypeError):
        yield _output_event(tag_id, f"Invalid JSON arguments: {str(content)[:200]}")
        yield _end_event(tag_id, name, False, started, error=f"Invalid JSON: {str(content)[:200]}")
        return

    context = args.get("context", "")
    criteria = args.get("criteria", "")
    focus = args.get("focus", "general")
    cwd = args.get("cwd", "") or home_dir()

    if not context or not criteria:
        yield _output_event(tag_id, "Both 'context' and 'criteria' are required.")
        yield _end_event(tag_id, name, False, started, error="Missing context or criteria")
        return

    # Emit start event — frontend creates the critique box
    yield {
        "type": "critique_start",
        "id": tag_id,
        "name": name,
        "context": context[:500],
        "criteria": criteria[:500],
        "focus": focus,
    }

    # Run the critique session — yields intermediate events + returns report
    report = ""
    error = ""
    try:
        for event in _run_critique_session(
            context=context,
            criteria=criteria,
            focus=focus,
            cwd=cwd,
            tag_id=tag_id,
            tool_name=name,
        ):
            if event.get("_report"):
                report = event["_report"]
            else:
                yield event
    except Exception as exc:
        logger.exception("Critique session failed")
        error = str(exc)

    if error:
        yield {
            "type": "critique_done",
            "id": tag_id,
            "name": name,
            "error": error,
        }
        yield _output_event(tag_id, f"Critique failed: {error}")
        yield _end_event(tag_id, name, False, started, error=error)
    else:
        yield {
            "type": "critique_done",
            "id": tag_id,
            "name": name,
            "report": report,
        }
        yield _output_event(tag_id, f"Critique complete.\n\n{report}")
        yield _end_event(tag_id, name, True, started, result={"report": report})


def _run_critique_session(
    context: str,
    criteria: str,
    focus: str,
    cwd: str,
    tag_id: str,
    tool_name: str,
) -> Generator[dict[str, Any], None, None]:
    """Run a full critique LLM session, yielding progress events.

    Yields critique_tool events during execution.
    Yields a special {_report: ...} dict when done.
    """
    from engine.agents import get_runtime
    from engine.config import get_model_config
    from engine.skills.parser import SkillParser
    from engine.skills.handlers import HANDLER_MAP
    from connectors import get_connector

    model_cfg = get_model_config()
    model_name = model_cfg.get("model", "")
    backend = model_cfg.get("api_backend", "deepseek")

    user_message = (
        f"## Context to Evaluate\n{context}\n\n"
        f"## Evaluation Criteria\n{criteria}\n\n"
        f"## Focus Area\n{focus}\n\n"
        f"Inspect the work described above. Use any tools you need to verify "
        f"the claims (read files, run commands, check the browser). "
        f"Then produce your structured critique report."
    )
    if cwd:
        user_message += f"\n\nWorking directory: {cwd}"

    connector = get_connector(backend, model_id=model_name)

    # Unique chat_id gives us a fresh session (no history contamination)
    critique_chat_id = f"critique-{uuid.uuid4().hex[:12]}"

    runtime = get_runtime()
    loop = runtime._loop
    if loop is None:
        raise RuntimeError("No event loop available for critique session")

    max_rounds = 15

    current_message = user_message

    for round_idx in range(max_rounds):
        future = asyncio.run_coroutine_threadsafe(
            _collect_llm_response(connector, current_message, critique_chat_id, model_name),
            loop,
        )
        try:
            response_text, thinking_text = future.result(timeout=300)
        except asyncio.TimeoutError:
            raise RuntimeError(f"Critique LLM timed out on round {round_idx}")
        except Exception as exc:
            raise RuntimeError(f"LLM call failed on round {round_idx}: {exc}") from exc

        if not response_text.strip():
            raise RuntimeError(f"Empty LLM response on round {round_idx}")

        # Parse tool calls using SkillParser
        parser = SkillParser()
        tool_calls: list[dict[str, Any]] = []
        for event in parser.feed(response_text):
            if event.get("type") == "tag_found":
                tc_name = event.get("name", "")
                tc_attrs = event.get("attrs", {})
                # Reconstruct arguments dict from attrs (stringified by _build_calls)
                tool_calls.append({"name": tc_name, "arguments": tc_attrs})

        # No tool calls = final response (the report)
        if not tool_calls:
            yield {"_report": response_text.strip()}
            return

        # Has tool calls — execute them and feed results back as next message
        feedback_parts: list[str] = []
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            t_name = call.get("name", "")
            t_args = call.get("arguments", {})

            # Skip critique recursion
            if t_name == "critique":
                feedback_parts.append("[critique]: Cannot nest critique sessions.")
                continue

            handler = HANDLER_MAP.get(t_name)
            if not handler:
                feedback_parts.append(f"[{t_name}]: Tool not available in critique mode.")
                continue

            try:
                args_str = json.dumps(t_args) if isinstance(t_args, dict) else str(t_args)
                sub_tag_id = f"cr-{uuid.uuid4().hex[:8]}"
                tool_output = ""
                # Handlers take exactly (tag_id, name, attrs, content)
                for evt in handler(sub_tag_id, t_name, {}, args_str):
                    if evt.get("type") == "skill_output":
                        tool_output += evt.get("text", "")
                    elif evt.get("type") == "skill_end" and not evt.get("ok"):
                        tool_output += f"\nError: {evt.get('error', '')}"

                feedback_parts.append(f"[{t_name} result]:\n{tool_output}")

                # Yield tool progress event to frontend
                yield {
                    "type": "critique_tool",
                    "id": tag_id,
                    "name": tool_name,
                    "tool": t_name,
                    "output": tool_output[:500],
                }
            except Exception as exc:
                feedback_parts.append(f"[{t_name} error]: {exc}")

        # Feed tool results back as the next user message
        feedback = "\n\n".join(feedback_parts)
        current_message = (
            f"Here are the results of your tool calls:\n\n{feedback}\n\n"
            f"Continue your evaluation. If you're done, provide your final report "
            f"in the exact format specified (Mark, Why, Suggestions) with NO tool calls."
        )

    yield {"_report": "Critique session ended without a final report (max rounds reached)."}


async def _collect_llm_response(
    connector: Any,
    message: str,
    chat_id: str,
    model: str,
) -> tuple[str, str]:
    """Async: stream LLM response via connector.stream_chat, return (text, thinking).

    The connector maintains session state via chat_id across rounds.
    """
    text_parts: list[str] = []
    think_parts: list[str] = []

    kwargs: dict[str, Any] = {
        "model": model if model else None,
        "chat_id": chat_id,
        "inject_instructions": False,
        "system_instruction": _CRITIQUE_SYSTEM_PROMPT,
    }

    event_source = connector.stream_chat(message=message, **kwargs)

    async for event in event_source:
        etype = event.get("type", "")
        if etype in ("answer", "chunk"):
            chunk = event.get("text", "")
            if chunk:
                text_parts.append(chunk)
        elif etype == "thinking":
            chunk = event.get("text", "")
            if chunk:
                think_parts.append(chunk)
        elif etype == "error":
            raise RuntimeError(event.get("message", "Unknown LLM error"))
        elif etype == "done":
            break

    return "".join(text_parts), "".join(think_parts)
