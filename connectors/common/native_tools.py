"""Native tool calling support — shared utilities.

Provides format conversion between OpenAI-compatible tool schemas
and provider-specific formats (Gemini, etc.), plus response parsing
for function call events.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema conversion: OpenAI format → provider formats
# ---------------------------------------------------------------------------

def openai_to_gemini_tools(schemas: list[dict]) -> list[dict]:
    """Convert OpenAI function schemas to Gemini tool declarations.

    OpenAI format:
        {"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}

    Gemini format:
        {"functionDeclarations": [{"name": ..., "description": ..., "parameters": ...}]}
    """
    declarations = []
    for schema in schemas:
        fn = schema.get("function", schema)
        decl: dict[str, Any] = {
            "name": fn["name"],
            "description": fn.get("description", ""),
        }
        params = fn.get("parameters", {})
        if params and params.get("properties"):
            decl["parameters"] = _clean_gemini_params(params)
        declarations.append(decl)

    if not declarations:
        return []
    return [{"functionDeclarations": declarations}]


def _clean_gemini_params(params: dict) -> dict:
    """Clean OpenAI parameter schema for Gemini compatibility.

    Gemini doesn't support 'additionalProperties' or some OpenAI extensions.
    Also converts 'type' values where needed.
    """
    cleaned = {}
    if "type" in params:
        cleaned["type"] = params["type"]
    if "description" in params:
        cleaned["description"] = params["description"]
    if "properties" in params:
        props = {}
        for name, prop in params["properties"].items():
            p = dict(prop)
            # Remove unsupported fields
            p.pop("additionalProperties", None)
            # Convert type aliases
            if p.get("type") == "integer":
                p["type"] = "integer"  # Gemini accepts this
            props[name] = p
        cleaned["properties"] = props
    if "required" in params:
        cleaned["required"] = params["required"]
    return cleaned


def openai_to_openai_tools(schemas: list[dict]) -> list[dict]:
    """Pass-through for OpenAI-compatible APIs (Groq, Mistral, DeepSeek, local).

    Validates and normalizes the schema format.
    """
    tools = []
    for schema in schemas:
        if "function" in schema:
            tools.append(schema)
        elif "name" in schema:
            # Bare function definition — wrap it
            tools.append({
                "type": "function",
                "function": {
                    "name": schema["name"],
                    "description": schema.get("description", ""),
                    "parameters": schema.get("parameters", {"type": "object", "properties": {}}),
                },
            })
    return tools


# ---------------------------------------------------------------------------
# Response parsing: provider function calls → Sable events
# ---------------------------------------------------------------------------

def parse_gemini_function_call(parts: list[dict]) -> list[dict[str, Any]] | None:
    """Extract function calls from Gemini response parts.

    Returns list of {"name": str, "args": dict} or None if no function calls.
    """
    calls = []
    for part in parts:
        fc = part.get("functionCall")
        if fc:
            calls.append({
                "name": fc.get("name", ""),
                "args": fc.get("args", {}),
            })
    return calls if calls else None


def parse_openai_function_call(delta_or_message: dict) -> list[dict[str, Any]] | None:
    """Extract function calls from OpenAI-compatible response.

    Handles both streaming (delta) and non-streaming (message) formats.
    Returns list of {"name": str, "args": dict, "id": str} or None.
    """
    # Non-streaming: message.tool_calls
    tool_calls = delta_or_message.get("tool_calls")
    if tool_calls:
        calls = []
        for tc in tool_calls:
            fn = tc.get("function", {})
            args_str = fn.get("arguments", "{}")
            try:
                args = json.loads(args_str) if isinstance(args_str, str) else args_str
            except json.JSONDecodeError:
                args = {}
            calls.append({
                "name": fn.get("name", ""),
                "args": args,
                "id": tc.get("id", ""),
            })
        return calls if calls else None

    # Streaming delta: delta.tool_calls (incremental)
    delta_tc = delta_or_message.get("tool_calls")
    if delta_tc:
        calls = []
        for tc in delta_tc:
            fn = tc.get("function", {})
            name = fn.get("name", "")
            args_str = fn.get("arguments", "")
            if name:  # Only first chunk has name
                calls.append({
                    "name": name,
                    "args_partial": args_str,
                    "id": tc.get("id", ""),
                    "index": tc.get("index", 0),
                })
        return calls if calls else None

    return None


# ---------------------------------------------------------------------------
# Tool result formatting: Sable results → provider formats
# ---------------------------------------------------------------------------

def format_gemini_tool_result(name: str, result: str, ok: bool) -> dict:
    """Format a tool execution result as a Gemini functionResponse message."""
    return {
        "role": "user",
        "parts": [{
            "functionResponse": {
                "name": name,
                "response": {
                    "result": result[:75000],  # Cap size
                    "ok": ok,
                },
            }
        }],
    }


def format_openai_tool_result(name: str, result: str, ok: bool, tool_call_id: str) -> dict:
    """Format a tool execution result as an OpenAI-compatible tool message."""
    content = result[:75000]
    if not ok:
        content = f"ERROR: {content}"
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": content,
    }


# ---------------------------------------------------------------------------
# Utility: convert native function call to Sable tag_found event shape
# ---------------------------------------------------------------------------

def _partial_string_value(args_str: str, key: str) -> str | None:
    """Extract a (possibly still-open) string value from partial JSON.

    Native tool-call arguments stream as raw JSON fragments, so mid-stream the
    buffer is not parseable. This pulls one string field out anyway, tolerating
    an unterminated value, and decodes JSON escapes so newlines in a file body
    stay real newlines in the card preview.
    """
    q = chr(34)
    bs = chr(92)
    ws = set(chr(c) for c in (32, 9, 13, 10))
    esc_map = {
        "n": chr(10),
        "t": chr(9),
        "r": chr(13),
        "b": chr(8),
        "f": chr(12),
        "/": "/",
        q: q,
        bs: bs,
    }

    i = args_str.find(q + key + q)
    if i < 0:
        return None
    i += len(key) + 2

    while i < len(args_str) and args_str[i] in ws:
        i += 1
    if i >= len(args_str) or args_str[i] != ":":
        return None
    i += 1
    while i < len(args_str) and args_str[i] in ws:
        i += 1
    if i >= len(args_str) or args_str[i] != q:
        return None
    i += 1

    out = []
    n = len(args_str)
    while i < n:
        c = args_str[i]
        if c == bs:
            i += 1
            if i >= n:
                break
            e = args_str[i]
            if e == "u":
                hexs = args_str[i + 1:i + 5]
                try:
                    out.append(chr(int(hexs, 16)))
                    i += 5
                    continue
                except ValueError:
                    pass
            out.append(esc_map.get(e, e))
            i += 1
            continue
        if c == q:
            break
        out.append(c)
        i += 1
    return "".join(out)


def native_preview_lines(
    args: dict | None, args_str: str, nbytes: int
) -> list[str]:
    """Build the live card preview for a native (still-streaming) tool call.

    Mirrors SkillParser._xml_preview_lines so a native call renders the same
    card the XML/JSON text paths do: path, then command, then the trailing
    lines of the body being written.
    """

    def _get(*keys: str) -> str | None:
        if isinstance(args, dict):
            for k in keys:
                v = args.get(k)
                if isinstance(v, str) and v.strip():
                    return v
        for k in keys:
            v = _partial_string_value(args_str, k)
            if v:
                return v
        return None

    lines: list[str] = []

    path = _get("path", "filename")
    if path and path.strip():
        lines.append(path.strip()[:200])

    cmd = _get("command")
    if cmd is not None:
        for ln in cmd.splitlines():
            if ln.strip():
                lines.append(ln.strip()[:150])
                break

    body = _get("content", "body", "lines")
    if body is not None:
        body_lines = [ln for ln in body.splitlines() if ln.strip()]
        for ln in body_lines[-3:]:
            lines.append(ln[:150])

    if not lines:
        size = f"{nbytes} B" if nbytes < 1024 else f"{nbytes / 1024:.1f} KB"
        lines.append(f"streaming\u2026 {size}")
    return lines


def native_call_to_tag_event(call: dict[str, Any]) -> dict[str, Any]:
    """Convert a parsed native function call to Sable's tag_found event shape.

    This allows the existing handler pipeline to process native calls
    identically to JSON-in-action-block calls.
    """
    from engine.skills.parser import _stringify_params, _CONTENT_PARAM_KEYS

    name = call["name"]
    args = call.get("args", {})
    if not isinstance(args, dict):
        args = {}

    attrs = _stringify_params(args)

    # Extract content-bearing params
    content = ""
    for key in _CONTENT_PARAM_KEYS:
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            content = val
            break

    # Special case: chat_title
    if name == "chat_title" and not content:
        for key in ("title", "text"):
            val = args.get(key)
            if isinstance(val, str) and val.strip():
                content = val
                break

    return {
        "type": "tag_found",
        "name": name,
        "attrs": attrs,
        "content": content,
        "_native": True,  # Marker so connectors know this came from native calling
        "_call_id": call.get("id", ""),  # For OpenAI tool_call_id tracking
    }
