"""Universal response normalizer — THE single layer between connectors and Sable core.

Every connector yields raw provider-specific events. This module converts ALL of them
into a unified format that chat.py and agent loop consume directly.

Input event types from connectors:
    answer, thinking, done, error          — text/lifecycle events
    raw_function_call                      — native API tool calls (Gemini, OpenAI, etc.)

Output event types (universal):
    {"type": "answer", "text": "..."}                          — clean text (<action> tags stripped)
    {"type": "thinking", "text": "..."}                        — reasoning (passed through)
    {"type": "function_call", "name": "...", "args": {...}}    — canonical tool call
    {"type": "done", ...}                                      — stream complete
    {"type": "error", ...}                                     — error

The normalizer also extracts <action>...</action> tags from answer text streams,
parses their JSON content, and emits function_call events instead of passing the
raw tag text through. This replaces SkillParser for tool call extraction entirely.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, AsyncGenerator

logger = logging.getLogger(__name__)

# Regex to find complete <action>...</action> tags in text
_ACTION_RE = re.compile(r"<action>(.*?)</action>", re.DOTALL)


def _parse_json_action(content: str) -> dict[str, Any] | None:
    """Parse JSON content from an <action> tag into canonical function_call.

    Supports multiple key conventions models might use:
        {"name": "execute_command", "args": {"command": "ls"}}
        {"tool": "web_search", "params": {"query": "..."}}
    """
    content = content.strip()
    if not content.startswith("{"):
        return None

    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None

    if not isinstance(data, dict):
        return None

    name = data.get("name") or data.get("tool") or data.get("tag") or ""
    args = data.get("args") or data.get("params") or data.get("parameters") or {}

    if not name:
        return None
    if not isinstance(args, dict):
        args = {}

    return {"type": "function_call", "name": name, "args": args}


def _extract_actions_from_text(text: str) -> tuple[str, list[dict[str, Any]]]:
    """Extract <action> tags from text, return cleaned text + function_call events."""
    calls: list[dict[str, Any]] = []

    def _replace(match: re.Match) -> str:
        inner = match.group(1)
        fc = _parse_json_action(inner)
        if fc:
            calls.append(fc)
            return ""  # Strip the tag from output text
        return match.group(0)  # Not valid JSON — leave as-is

    cleaned = _ACTION_RE.sub(_replace, text)
    return cleaned, calls


async def normalize_stream(
    stream: AsyncGenerator[dict[str, Any], None],
) -> AsyncGenerator[dict[str, Any], None]:
    """Wrap ANY connector stream and produce universal output events.

    Handles:
    - Native API function calls -> canonical function_call
    - <action> tags in answer text -> extract, parse JSON, emit function_call
    - OpenAI-compatible incremental tool_calls -> accumulate, flush on done
    - Everything else -> pass through unchanged
    """
    # --- State for OpenAI-compatible incremental tool calls ---
    _tc_accum: dict[int, dict[str, Any]] = {}

    # --- State for partial <action> tag buffering across chunks ---
    _action_buffer = ""
    _in_action = False

    async for event in stream:
        etype = event.get("type")

        # -- Native function calls from connectors --
        if etype == "raw_function_call":
            provider = event.get("provider", "")
            data = event.get("data", {})

            if provider == "gemini":
                name = data.get("name", "")
                args = data.get("args", {})
                if name:
                    yield {"type": "function_call", "name": name, "args": args}
                continue

            # OpenAI/Mistral/Groq: arguments stream incrementally
            idx = data.get("index", 0)
            fn = data.get("function", {})
            name_chunk = fn.get("name", "")
            args_chunk = fn.get("arguments", "")

            if idx not in _tc_accum:
                _tc_accum[idx] = {"name": "", "args_str": ""}
            if name_chunk:
                _tc_accum[idx]["name"] += name_chunk
            if args_chunk:
                _tc_accum[idx]["args_str"] += args_chunk
            continue

        # -- Answer text: extract <action> tags --
        if etype == "answer":
            text = event.get("text", "")
            if not text:
                continue

            # Handle partial <action> tags spanning chunks
            if _in_action:
                _action_buffer += text
                close_idx = _action_buffer.find("</action>")
                if close_idx != -1:
                    full_tag = _action_buffer[:close_idx + len("</action>")]
                    remainder = _action_buffer[close_idx + len("</action>"):]
                    _action_buffer = ""
                    _in_action = False

                    _, calls = _extract_actions_from_text(full_tag)
                    for fc in calls:
                        yield fc

                    if remainder:
                        cleaned, more_calls = _extract_actions_from_text(remainder)
                        for fc in more_calls:
                            yield fc
                        if cleaned.strip():
                            yield {"type": "answer", "text": cleaned}
                continue

            # Check if this chunk starts/contains an <action> tag
            if "<action>" in text:
                before, _, after = text.partition("<action>")

                if before.strip():
                    yield {"type": "answer", "text": before}

                close_idx = after.find("</action>")
                if close_idx != -1:
                    inner = after[:close_idx]
                    remainder = after[close_idx + len("</action>"):]

                    fc = _parse_json_action(inner)
                    if fc:
                        yield fc

                    if remainder:
                        cleaned, more_calls = _extract_actions_from_text(remainder)
                        for fc in more_calls:
                            yield fc
                        if cleaned.strip():
                            yield {"type": "answer", "text": cleaned}
                else:
                    _action_buffer = after
                    _in_action = True
                continue

            # No <action> tags — check for complete tags via regex
            cleaned, calls = _extract_actions_from_text(text)
            for fc in calls:
                yield fc
            if cleaned.strip():
                yield {"type": "answer", "text": cleaned}
            continue

        # -- Done: flush accumulated state --
        if etype == "done":
            if _in_action and _action_buffer:
                logger.warning("Discarding incomplete <action> buffer at stream end: %s", _action_buffer[:100])
                _action_buffer = ""
                _in_action = False

            for _idx in sorted(_tc_accum):
                acc = _tc_accum[_idx]
                if acc["name"]:
                    try:
                        args = json.loads(acc["args_str"]) if acc["args_str"] else {}
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                    yield {"type": "function_call", "name": acc["name"], "args": args}
            _tc_accum.clear()

            yield event
            continue

        # -- Everything else passes through --
        yield event
