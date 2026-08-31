"""Universal response normalizer — THE single layer between connectors and Sable core.

Every connector yields raw provider-specific events. This module converts ALL of them
into a unified format that chat.py and agent loop consume directly.

Input event types from connectors:
    answer, thinking, done, error          — text/lifecycle events
    raw_function_call                      — native API tool calls (Gemini, OpenAI, etc.)

Output event types (universal):
    {"type": "answer", "text": "..."}                          — clean text (tool tags stripped)
    {"type": "thinking", "text": "..."}                        — reasoning (passed through)
    {"type": "function_call", "name": "...", "args": {...}}    — canonical tool call
    {"type": "done", ...}                                      — stream complete
    {"type": "error", ...}                                     — error

Supported tool call formats (selected by *provider* parameter):
    - Default/Hermes:  <tool_call>{JSON}</tool_call>
    - DeepSeek DSML:   <｜DSML｜tool_calls><｜DSML｜invoke name="...">...</｜DSML｜invoke></｜DSML｜tool_calls>
    - Native API:      raw_function_call events from Gemini/OpenAI connectors

All formats are converted to the same canonical function_call dict.
"""
from __future__ import annotations

from engine.skills.json_depth import json_structurally_complete

import json
import logging
import re
from typing import Any, AsyncGenerator

logger = logging.getLogger(__name__)

# Regex to find complete <tool_call>...</tool_call> tags in text.
# Covers both Sable custom and native Hermes format (same tags).
_ACTION_RE = re.compile(r"<\s*tool_calls?\s*>(.*?)<\s*/\s*tool_calls?\s*>", re.DOTALL | re.IGNORECASE)

# Fallback: catch bare JSON tool calls when models drop the <tool_call> wrapper.
# Matches standalone JSON objects with a "name" key at the top level.
_BARE_TOOL_RE = re.compile(
    r'(?<!\w)(\{\s*"(?:name|tool|tag)"\s*:.*?\})(?!\w)',
    re.DOTALL,
)


def _parse_json_action(content: str) -> dict[str, Any] | None:
    """Parse JSON content from a <tool_call> tag into canonical function_call.

    Supports multiple key conventions models might use:
        {"name": "execute_command", "args": {"command": "ls"}}
        {"name": "web_search", "arguments": {"query": "..."}}
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
    """Extract tool call tags from text, return cleaned text + function_call events.

    Handles both Sable custom format and native Hermes format.
    """
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


# --- DSML (DeepSeek Markup Language) tool call parser ---
# Handles V4 format (with or without leading ｜ before DSML):
#   <｜DSML｜tool_calls>  OR  <DSML｜tool_calls>
#     <｜DSML｜invoke name="fn">
#       <｜DSML｜parameter name="p" string="true">val</｜DSML｜parameter>
#     </｜DSML｜invoke>
#   </｜DSML｜tool_calls>
#
# Models sometimes drop the leading ｜, so we make it optional.

# Optional leading fullwidth bar: matches both <｜DSML｜ and <DSML｜
_D = r'\uff5c?'  # optional ｜

_DSML_OPEN_RE = re.compile(r'<\uff5c?DSML\uff5ctool_calls>')
_DSML_CLOSE_RE = re.compile(r'</\uff5c?DSML\uff5ctool_calls>')

# For streaming buffer detection — exact strings for both variants
_DSML_OPEN_VARIANTS = ["<\uff5cDSML\uff5ctool_calls>", "<DSML\uff5ctool_calls>"]
_DSML_CLOSE_VARIANTS = ["</\uff5cDSML\uff5ctool_calls>", "</DSML\uff5ctool_calls>"]

_DSML_INVOKE_RE = re.compile(
    r'<\uff5c?DSML\uff5cinvoke\s+name="([^"]+)">(.*?)</\uff5c?DSML\uff5cinvoke>',
    re.DOTALL,
)

_DSML_PARAM_RE = re.compile(
    r'<\uff5c?DSML\uff5cparameter\s+name="([^"]+)"\s+string="(true|false)">(.*?)</\uff5c?DSML\uff5cparameter>',
    re.DOTALL,
)


def _parse_dsml_block(block_text: str) -> list[dict[str, Any]]:
    """Parse a complete DSML tool_calls block into canonical function_call dicts."""
    calls: list[dict[str, Any]] = []
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
                    args[pname] = json.loads(raw_val)
                except (json.JSONDecodeError, TypeError):
                    args[pname] = raw_val
        calls.append({"type": "function_call", "name": fn_name, "args": args})
    return calls


def _extract_dsml_from_text(text: str) -> tuple[str, list[dict[str, Any]]]:
    """Extract all complete DSML tool_calls blocks from text.

    Returns (cleaned_text_without_blocks, list_of_function_calls).
    """
    calls: list[dict[str, Any]] = []

    def _replace(match: re.Match) -> str:
        inner = match.group(0)
        parsed = _parse_dsml_block(inner)
        if parsed:
            calls.extend(parsed)
            return ""
        return inner  # malformed — leave as-is

    # Match both <｜DSML｜...> and <DSML｜...> variants
    pattern = r'<\uff5c?DSML\uff5ctool_calls>.*?</\uff5c?DSML\uff5ctool_calls>'
    cleaned = re.sub(pattern, _replace, text, flags=re.DOTALL)
    return cleaned, calls


# --- Legacy canonical XML invoke/parameter parser (fallback) ---
# Catches models that drift from DSML to plain XML:
#   <tool_calls>
#     <invoke name="fn">
#       <parameter name="p">value</parameter>
#     </invoke>
#   </tool_calls>
# Also handles the bare <invoke>...</invoke> without outer wrapper.

_LEGACY_TOOL_CALLS_RE = re.compile(
    r"<\s*tool_calls\s*>(.*?)<\s*/\s*tool_calls\s*>",
    re.DOTALL | re.IGNORECASE,
)

_LEGACY_INVOKE_RE = re.compile(
    r'<\s*invoke\s+name="([^"]+)">(.*?)<\s*/\s*invoke\s*>',
    re.DOTALL | re.IGNORECASE,
)

_LEGACY_PARAM_RE = re.compile(
    r'<\s*parameter\s+name="([^"]+)"(?:\s+string="(true|false)")?>(.*?)<\s*/\s*parameter\s*>',
    re.DOTALL | re.IGNORECASE,
)


def _parse_legacy_invoke(invoke_text: str) -> list[dict[str, Any]]:
    """Parse legacy XML invoke/parameter blocks into canonical function_call dicts.

    Handles both typed (string="true|false") and untyped parameter values.
    Untyped values are attempted as JSON first, falling back to raw string.
    """
    calls: list[dict[str, Any]] = []
    for inv_match in _LEGACY_INVOKE_RE.finditer(invoke_text):
        fn_name = inv_match.group(1)
        params_text = inv_match.group(2)
        args: dict[str, Any] = {}
        for p_match in _LEGACY_PARAM_RE.finditer(params_text):
            pname = p_match.group(1)
            type_hint = p_match.group(2)  # may be None
            raw_val = p_match.group(3).strip()
            if type_hint == "true":
                args[pname] = raw_val
            elif type_hint == "false":
                try:
                    args[pname] = json.loads(raw_val)
                except (json.JSONDecodeError, TypeError):
                    args[pname] = raw_val
            else:
                # No type hint — try JSON, fall back to string
                try:
                    args[pname] = json.loads(raw_val)
                except (json.JSONDecodeError, TypeError):
                    args[pname] = raw_val
        calls.append({"type": "function_call", "name": fn_name, "args": args})
    return calls


def _extract_legacy_from_text(text: str) -> tuple[str, list[dict[str, Any]]]:
    """Extract legacy XML tool_calls/invoke blocks from text.

    Returns (cleaned_text, list_of_function_calls).
    Used as a fallback when DSML parsing finds nothing.
    """
    calls: list[dict[str, Any]] = []

    def _replace(match: re.Match) -> str:
        inner = match.group(1)
        parsed = _parse_legacy_invoke(inner)
        if parsed:
            calls.extend(parsed)
            return ""
        return match.group(0)

    cleaned = _LEGACY_TOOL_CALLS_RE.sub(_replace, text)

    # Also catch bare <invoke> blocks not wrapped in <tool_calls>
    if not calls:
        bare_calls = _parse_legacy_invoke(text)
        if bare_calls:
            calls.extend(bare_calls)
            cleaned = _LEGACY_INVOKE_RE.sub("", cleaned)

    return cleaned, calls




async def normalize_stream(
    stream: AsyncGenerator[dict[str, Any], None],
    provider: str | None = None,
) -> AsyncGenerator[dict[str, Any], None]:
    """Wrap ANY connector stream and produce universal output events.

    Args:
        stream: Raw event stream from a connector.
        provider: Backend name ("deepseek", "gemini", etc.) to select
                  tool call extraction strategy.  None = default Hermes format.

    Handles:
    - Native API function calls -> canonical function_call
    - <tool_call> tags in answer text -> extract, parse JSON, emit function_call
    - DeepSeek DSML blocks -> extract invoke/parameter, emit function_call
    - OpenAI-compatible incremental tool_calls -> accumulate, flush on done
    - Everything else -> pass through unchanged
    """
    _use_dsml = provider == "deepseek"

    # --- State for OpenAI-compatible incremental tool calls ---
    _tc_accum: dict[int, dict[str, Any]] = {}

    # --- State for partial tag buffering across chunks ---
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
                # outside a tool block (e.g. Qwen splits closing tag across
                # chunks and we're not currently inside a tool block)
                if not _in_action:
                    if _use_dsml:
                        for _cv in _DSML_CLOSE_VARIANTS:
                            if _cv in text:
                                text = text.replace(_cv, "")
                    else:
                        close_tag = "</" + "tool_call>"
                        if close_tag in text:
                            text = text.replace(close_tag, "")

                _pending_prefix = ""

            # Handle partial tool tags spanning chunks
            if _in_action:
                _action_buffer += text

                # Select closing tag and extraction function based on provider
                if _use_dsml:
                    # Try both close tag variants
                    _close_tag = None
                    for _cv in _DSML_CLOSE_VARIANTS:
                        if _cv in _action_buffer:
                            _close_tag = _cv
                            break
                    if not _close_tag:
                        _close_tag = _DSML_CLOSE_VARIANTS[0]  # default
                    _extract_fn = _extract_dsml_from_text
                    _check_complete = None  # DSML uses XML structure, not JSON completeness
                else:
                    _close_tag = "</" + "tool_call>"
                    _extract_fn = _extract_actions_from_text
                    _check_complete = json_structurally_complete

                _search_from = 0
                _accepted = False
                while True:
                    close_idx = _action_buffer.find(_close_tag, _search_from)
                    if close_idx == -1:
                        break
                    inner = _action_buffer[:close_idx]
                    # For Hermes: validate JSON completeness; for DSML: accept on closing tag
                    if _check_complete is None or _check_complete(inner):
                        full_tag = _action_buffer[:close_idx + len(_close_tag)]
                        remainder = _action_buffer[close_idx + len(_close_tag):]
                        _action_buffer = ""
                        _in_action = False
                        _accepted = True

                        _, calls = _extract_fn(full_tag)
                        for fc in calls:
                            yield fc

                        if remainder:
                            cleaned, more_calls = _extract_fn(remainder)
                            for fc in more_calls:
                                yield fc
                            if cleaned.strip():
                                yield {"type": "answer", "text": cleaned}
                        break
                    # JSON not complete — search past this match
                    _search_from = close_idx + len(_close_tag)
                continue

            # Check if this chunk starts/contains a    <tool_call>  tag
            _open_tag = "<" + "tool_call>"
            if _open_tag in text:
                before, _, after = text.partition(_open_tag)

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

            # --- DSML extraction (DeepSeek) ---
            # Check for any DSML open tag variant
            _dsml_open_found = None
            if _use_dsml:
                for _ov in _DSML_OPEN_VARIANTS:
                    if _ov in text:
                        _dsml_open_found = _ov
                        break
            if _dsml_open_found:
                before_dsml, _, after_dsml = text.partition(_dsml_open_found)
                if before_dsml.strip():
                    yield {"type": "answer", "text": before_dsml}

                # Buffer until we find the closing tag
                full_block = _dsml_open_found + after_dsml
                # Find matching close tag variant
                _dsml_close_found = None
                for _cv in _DSML_CLOSE_VARIANTS:
                    if _cv in full_block:
                        _dsml_close_found = _cv
                        break
                if _dsml_close_found:
                    end_idx = full_block.find(_dsml_close_found) + len(_dsml_close_found)
                    block = full_block[:end_idx]
                    remainder = full_block[end_idx:]
                    _, dsml_calls = _extract_dsml_from_text(block)
                    for fc in dsml_calls:
                        yield fc
                    if remainder:
                        cleaned_r, more_calls = _extract_dsml_from_text(remainder)
                        for fc in more_calls:
                            yield fc
                        if cleaned_r.strip():
                            yield {"type": "answer", "text": cleaned_r}
                else:
                    # Incomplete DSML block — buffer it
                    _action_buffer = full_block
                    _in_action = True
                continue

            # No tool tags found — check for complete tags via regex
            if _use_dsml:
                cleaned, calls = _extract_dsml_from_text(text)
                # Fallback: try legacy XML invoke/parameter if DSML found nothing
                if not calls:
                    cleaned, calls = _extract_legacy_from_text(cleaned)
            else:
                cleaned, calls = _extract_actions_from_text(text)
            for fc in calls:
                yield fc

            # Check if this chunk ends with a partial tag prefix
            if _use_dsml:
                partial = None
                for _v in _DSML_OPEN_VARIANTS + _DSML_CLOSE_VARIANTS:
                    p = _partial_tag_suffix(cleaned, _v)
                    if p:
                        partial = p
                        break
            else:
                partial = _partial_tag_suffix(cleaned, "<" + "tool_call>")
                if not partial:
                    partial = _partial_tag_suffix(cleaned, "</" + "tool_call>")
            if partial:
                _pending_prefix = partial
                cleaned = cleaned[: -len(partial)]

            if cleaned.strip():
                yield {"type": "answer", "text": cleaned}
            continue

        # -- Done: flush accumulated state --
        if etype == "done":
            if _in_action and _action_buffer:
                fmt = "DSML" if _use_dsml else "tool_call"
                logger.warning("Discarding incomplete %s buffer at stream end: %s", fmt, _action_buffer[:100])
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
