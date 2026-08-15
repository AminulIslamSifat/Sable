"""Universal response normalizer — THE single layer between connectors and Sable core.

Every connector yields raw provider-specific events. This module converts ALL of them
into a unified format that chat.py and agent loop consume directly.

Input event types from connectors:
    answer, thinking, done, error          — text/lifecycle events
    raw_function_call                      — native API tool calls (Gemini, OpenAI, etc.)

Output event types (universal):
    {"type": "answer", "text": "..."}                          — clean text (<tool_call> tags stripped)
    {"type": "thinking", "text": "..."}                        — reasoning (passed through)
    {"type": "function_call", "name": "...", "args": {...}}    — canonical tool call
    {"type": "done", ...}                                      — stream complete
    {"type": "error", ...}                                     — error

The normalizer also extracts <tool_call>...</tool_call> tags from answer text streams,
parses their JSON content, and emits function_call events instead of passing the
raw tag text through. This replaces SkillParser for tool call extraction entirely.
"""
from __future__ import annotations

from engine.skills.json_depth import json_structurally_complete

import json
import logging
import re
from typing import Any, AsyncGenerator

logger = logging.getLogger(__name__)

# Regex to find complete <tool_call>...</tool_call> tags in text
_ACTION_RE = re.compile(r"<\s*tool_call\s*>(.*?)<\s*/\s*tool_call\s*>", re.DOTALL | re.IGNORECASE)


def _parse_json_action(content: str) -> dict[str, Any] | None:
    """Parse JSON content from a <tool_call> tag into canonical function_call.

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
    """Extract <tool_call> tags from text, return cleaned text + function_call events."""
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
    - <tool_call> tags in answer text -> extract, parse JSON, emit function_call
    - OpenAI-compatible incremental tool_calls -> accumulate, flush on done
    - Everything else -> pass through unchanged
    """
    # --- State for OpenAI-compatible incremental tool calls ---
    _tc_accum: dict[int, dict[str, Any]] = {}

    # --- State for partial <tool_call> tag buffering across chunks ---
    _action_buffer = ""
    _in_action = False
    _pending_prefix = ""

    def _partial_tag_suffix(text: str, tag: str) -> str:
        """Return the longest suffix of *text* that is also a proper prefix of *tag*.

        Iterates **longest-first** so we hold back the maximum possible
        partial-tag fragment instead of the shortest one.  This prevents
        mid-tag leaks (e.g. ``tool_call>`` or ``_call>``) when a provider
        like Qwen splits ``<tool_call>`` / ``</tool_call>`` across
        chunk boundaries at awkward token positions.
        """
        for i in range(len(tag) - 1, 0, -1):
            prefix = tag[:i]
            if text.endswith(prefix):
                return prefix
        return ""

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

        # -- Answer text: extract <tool_call> tags --
        if etype == "answer":
            text = event.get("text", "")
            if not text:
                continue

            # Prepend any partial tag carried from the previous chunk
            if _pending_prefix:
                text = _pending_prefix + text

                # After reassembly, strip orphaned closing tags that formed
                # outside an action block (e.g. Qwen splits closing tag across
                # chunks and we're not currently inside a tool_call block)
                if not _in_action:
                    close_tag = "</" + "tool_call>"
                    if close_tag in text:
                        text = text.replace(close_tag, "")

                _pending_prefix = ""

            # Handle partial <tool_call> tags spanning chunks
            if _in_action:
                _action_buffer += text
                # Search for closing tag, but only accept if inner JSON is complete
                _close_tag = "</" + "tool_call>"
                _search_from = 0
                _accepted = False
                while True:
                    close_idx = _action_buffer.find(_close_tag, _search_from)
                    if close_idx == -1:
                        break
                    inner = _action_buffer[:close_idx]
                    if json_structurally_complete(inner):
                        full_tag = _action_buffer[:close_idx + len(_close_tag)]
                        remainder = _action_buffer[close_idx + len(_close_tag):]
                        _action_buffer = ""
                        _in_action = False
                        _accepted = True

                        _, calls = _extract_actions_from_text(full_tag)
                        for fc in calls:
                            yield fc

                        if remainder:
                            cleaned, more_calls = _extract_actions_from_text(remainder)
                            for fc in more_calls:
                                yield fc
                            if cleaned.strip():
                                yield {"type": "answer", "text": cleaned}
                        break
                    # JSON not complete — search past this match
                    _search_from = close_idx + len(_close_tag)
                continue

            # Check if this chunk starts/contains a <tool_call> tag
            if "<tool_call>" in text:
                before, _, after = text.partition("<tool_call>")

                if before.strip():
                    yield {"type": "answer", "text": before}

                # Search for closing tag, validate JSON completeness
                _ct = "</" + "tool_call>"
                _sf = 0
                _found_valid = False
                while True:
                    close_idx = after.find(_ct, _sf)
                    if close_idx == -1:
                        break
                    inner = after[:close_idx]
                    if json_structurally_complete(inner):
                        remainder = after[close_idx + len(_ct):]
                        fc = _parse_json_action(inner)
                        if fc:
                            yield fc
                        if remainder:
                            cleaned, more_calls = _extract_actions_from_text(remainder)
                            for fc in more_calls:
                                yield fc
                            if cleaned.strip():
                                yield {"type": "answer", "text": cleaned}
                        _found_valid = True
                        break
                    _sf = close_idx + len(_ct)
                if not _found_valid:
                    _action_buffer = after
                    _in_action = True
                continue

            # No <tool_call> tags — check for complete tags via regex
            cleaned, calls = _extract_actions_from_text(text)
            for fc in calls:
                yield fc

            # Check if this chunk ends with a partial tag prefix
            partial = _partial_tag_suffix(cleaned, "<tool_call>")
            if not partial:
                partial = _partial_tag_suffix(cleaned, "</tool_call>")
            if partial:
                _pending_prefix = partial
                cleaned = cleaned[: -len(partial)]

            if cleaned.strip():
                yield {"type": "answer", "text": cleaned}
            continue

        # -- Done: flush accumulated state --
        if etype == "done":
            if _in_action and _action_buffer:
                logger.warning("Discarding incomplete <tool_call> buffer at stream end: %s", _action_buffer[:100])
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
