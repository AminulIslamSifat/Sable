"""Stream parser for Hermes-style tool calls.

Extracts complete tool calls from streamed LLM output. Tool calls use
<tool_call>...</tool_call> boundaries. Yields structured events:
- {"type": "text", "text": ...} for prose
- {"type": "tool_pending", "tag": ..., "attrs": ...} for activity indicators
- {"type": "tool_progress", "tag": ..., "lines": ..., "bytes": ...} for live progress
- {"type": "tag_found", "name": ..., "attrs": ..., "content": ...} for complete calls

Hermes Tool Call Format:
  Single:  <tool_call>{"name": "grep", "arguments": {"pattern": "foo"}}</tool_call>
  Multiple: <tool_call>[{"name": "a", ...}, {"name": "b", ...}]</tool_call> (ONE wrapper, JSON array)
"""

from __future__ import annotations
from engine.skills.json_depth import json_structurally_complete


import json
from engine.skills.json_repair import repair_json
from engine.skills.json_sanitize import sanitize_transport
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Generator

logger = logging.getLogger("sable.parser")

# Debug log file for parser failures — use central output root
from engine.config import OUTPUT_ROOT as _OUT
_PARSER_LOG = _OUT / "parser_debug.log"

def _plog(msg: str) -> None:
    """Write parser debug info to file."""
    try:
        _PARSER_LOG.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        with open(_PARSER_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass

# All recognized tool names — built from tools/*/tool.json at engine init,
# but hardcoded here as fallback for standalone use.
KNOWN_TAGS = (
    "execute_command",
    "get_file",
    "read_file",
    "check_command",
    "openweb",
    "create_note",
    "save_svg",
    "create_svg",
    "view_file",
    "edit_file",
    "create_file",
    "insert_file",
    "run_simulacra",
    "spawn_agent",
    "agent_status",
    "kill_agent",
    "ask_user",
    "grep",
    "glob",
    "list_dir",
    "tracknote",
    "mcp_call",
    "web_search",
    "web_fetch",
    "online_search",
    "chat_title",
)

# Params that map to the content field for handler compatibility.
_CONTENT_PARAM_KEYS = ("body", "content", "command")

def parse_attrs(raw) -> dict[str, str]:
    """Compatibility stub. With JSON format, attrs are already a dict.
    If called with a string (legacy), return empty dict."""
    if isinstance(raw, dict):
        return raw
    return {}



def _stringify_params(params: dict[str, Any]) -> dict[str, str]:
    """Convert JSON params to string dict for handler compatibility."""
    result: dict[str, str] = {}
    for key, value in params.items():
        if value is None:
            result[key] = ""
        elif isinstance(value, bool):
            result[key] = str(value).lower()
        elif isinstance(value, (int, float)):
            result[key] = str(value)
        elif isinstance(value, str):
            result[key] = value
        else:
            result[key] = json.dumps(value)
    return result


# Regex to fix invalid JSON escape sequences models commonly produce.
# JSON only allows: \" \\ \/ \b \f \n \r \t \uXXXX
# Models often emit \' which is invalid.
_INVALID_ESCAPE_RE = re.compile(r"\\(?!['\"\\\/bfnrtu])")


def _sanitize_json_escapes(raw: str) -> str:
    """Remove invalid backslash escapes that models sometimes produce."""
    # Replace \' with just ' (most common offender)
    raw = raw.replace("\\'", "'")
    # Catch any other invalid escapes: backslash not followed by valid escape char
    return _INVALID_ESCAPE_RE.sub("", raw)

def _parse_action_payload(raw: str) -> list[dict[str, Any]]:
    """Parse tool_call JSON content into a list of normalized tool call dicts.

    Returns list of {"name": str, "attrs": dict[str,str], "content": str}.
    Accepts both Hermes format {"name": ..., "arguments": ...} and legacy
    {"tool": ..., "params": ...} for backwards compatibility.
    """
    raw = raw.strip()
    if not raw:
        return []

    # Fix transport-level corruption (backslashes, mixed quotes)
    raw = sanitize_transport(raw)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Attempt repair of common LLM JSON malformations
        try:
            data = json.loads(repair_json(raw))
        except json.JSONDecodeError:
            pass
        else:
            return _build_calls(data)

        # Recovery: find outermost JSON structure
        start_obj = raw.find("{")
        start_arr = raw.find("[")
        if start_arr >= 0 and (start_obj < 0 or start_arr < start_obj):
            start = start_arr
        elif start_obj >= 0:
            start = start_obj
        else:
            return []
        # Match closing bracket to opening type
        if raw[start] == "[":
            end = raw.rfind("]")
        else:
            end = raw.rfind("}")
        if end <= start:
            return []
        try:
            data = json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            # Last resort: try repair on extracted substring
            try:
                data = json.loads(repair_json(raw[start : end + 1]))
            except json.JSONDecodeError:
                return []

    return _build_calls(data)


def _diagnose_json_failure(raw: str) -> str:
    """Return a human-readable diagnosis of why JSON parsing failed.

    Tries json.loads and returns error position + surrounding context.
    Used by resilience guard to give actionable FORMAT WARNING messages.
    """
    if not raw or not raw.strip():
        return "Empty tool_call content."
    sanitized = sanitize_transport(raw)
    try:
        json.loads(sanitized)
        return "JSON is valid (unexpected diagnostic call)."
    except json.JSONDecodeError as e:
        pos = e.pos
        start = max(0, pos - 30)
        end = min(len(sanitized), pos + 30)
        snippet = sanitized[start:end]
        marker = " " * (pos - start) + "^"
        return (
            f"{e.msg} at position {pos}.\n"
            f"Context: ...{snippet}...\n"
            f"         {marker}\n"
            f"Hint: Backslashes, regex patterns, and */ in strings often break "
            f"JSON transport. Write complex code to a separate file instead."
        )


def _build_calls(data: Any) -> list[dict[str, Any]]:
    """Convert parsed JSON data into normalized tool call dicts."""
    if isinstance(data, dict):
        data = [data]
    elif not isinstance(data, list):
        return []

    calls: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        tool_name = str(item.get("name") or item.get("tool", "")).strip().lower()
        if not tool_name:
            continue
        params = item.get("arguments") or item.get("params", {})
        if not isinstance(params, dict):
            params = {}

        attrs = _stringify_params(params)

        # Extract content-bearing params into content field
        content = ""
        for key in _CONTENT_PARAM_KEYS:
            val = params.get(key)
            if isinstance(val, str) and val.strip():
                content = val
                break

        # Special case: chat_title uses title/text param as content
        if tool_name == "chat_title" and not content:
            for key in ("title", "text"):
                val = params.get(key)
                if isinstance(val, str) and val.strip():
                    content = val
                    break

        calls.append({"name": tool_name, "attrs": attrs, "content": content})

    return calls


class SkillParser:
    """Extracts complete Hermes-style tool calls from streamed answer text.

    Tool calls are parsed from <tool_call>...</tool_call> boundaries.
    Text outside tool_call blocks is emitted as plain prose.
    Complete calls are yielded as {"type": "tag_found", ...} events
    for the engine to dispatch through the middleware pipeline.
    """

    _ACTION_OPEN = re.compile(r"<\s*tool_call\s*>", re.I)
    _ACTION_CLOSE = re.compile(r"<\s*/\s*tool_call\s*>", re.I)
    _TOOL_NAME_RE = re.compile(r'"name"\s*:\s*"([^"]+)"')

    def __init__(self, known_tags: tuple[str, ...] | None = None) -> None:
        self._known_tags = set(known_tags or KNOWN_TAGS)
        self.buf = ""
        self._in_action = False
        self._pending_tag: str | None = None
        self._last_progress: tuple[int, int] = (0, 0)

    def feed(self, text: str) -> Generator[dict[str, Any], None, None]:
        """Feed a chunk of streamed text. Yields events as calls/prose are resolved."""
        self.buf += text

        while True:
            if not self._in_action:
                # Strip orphaned closing tag fragments that arrive after
                # partial extraction already consumed the tool call.
                # Handle both complete closing tags and partial suffixes
                # like "_call>" or "call>" that are remnants of </tool_call>
                _orphan_close = self._ACTION_CLOSE.search(self.buf)
                if _orphan_close is not None:
                    self.buf = self.buf[:_orphan_close.start()] + self.buf[_orphan_close.end():]
                    if not self.buf:
                        break
                # Check for orphaned suffix fragments (no < prefix)
                _stripped_buf = self.buf.strip()
                if _stripped_buf and "<" not in _stripped_buf:
                    # Could be a fragment like "_call>", "call>", ">"
                    _suffix_of_close = "</" + "tool_call>"
                    _is_fragment = False
                    for _flen in range(1, len(_suffix_of_close)):
                        if _stripped_buf == _suffix_of_close[-_flen:] or _stripped_buf == _suffix_of_close[_flen:]:
                            _is_fragment = True
                            break
                    if _is_fragment:
                        self.buf = ""
                        break

                m = self._ACTION_OPEN.search(self.buf)
                if m is None:
                    # No action open — flush as prose (hold trailing partial)
                    idx = self.buf.rfind("<")
                    if idx >= 0 and ">" not in self.buf[idx:]:
                        tail = self.buf[idx:].lstrip("<").strip().lower()
                        # Check for partial opening OR closing tag
                        _open_match = (tail == "" or "tool_call".startswith(tail))
                        _close_tail = self.buf[idx:].lstrip("<").lstrip("/").strip().lower()
                        _close_match = ("/" in self.buf[idx:idx+2] and
                                        (_close_tail == "" or "tool_call".startswith(_close_tail)))
                        if _open_match or _close_match:
                            if idx > 0:
                                yield {"type": "text", "text": self.buf[:idx]}
                            self.buf = self.buf[idx:]
                            break
                    if self.buf:
                        yield {"type": "text", "text": self.buf}
                        self.buf = ""
                    break
                # Found action open — emit prose before it, validate JSON before entering action mode
                before = self.buf[:m.start()]
                if before:
                    yield {"type": "text", "text": before}
                self.buf = self.buf[m.end():]

                # JSON validation gate: only enter action mode if content looks like JSON
                stripped_ahead = self.buf.lstrip()
                if stripped_ahead and stripped_ahead[0] not in ('{', '['):
                    # Not JSON — silently discard the opening tag, skip to next < if any
                    _plog(f"NON_JSON_TOOL_CALL: starts with {repr(stripped_ahead[:30])}, discarding")
                    # Find the next potential tag start or just continue
                    next_lt = stripped_ahead.find("<")
                    if next_lt >= 0:
                        self.buf = stripped_ahead[next_lt:]
                    else:
                        self.buf = ""
                    continue
                self._in_action = True

            # Check for action close boundary — but only accept it if the
            # JSON content between open and close is structurally complete.
            # This prevents false splits when tag-like patterns appear inside
            # incomplete JSON (e.g. Qwen streaming splitting issues).
            close_m = self._ACTION_CLOSE.search(self.buf)
            while close_m is not None:
                action_content = self.buf[:close_m.start()]
                if json_structurally_complete(action_content):
                    after = self.buf[close_m.end():]
                    self.buf = after
                    self._in_action = False
                    yield from self._extract_json(action_content, partial=False)
                    break  # accepted this close tag, exit while loop
                # JSON not complete — this close tag is inside content,
                # search for the next one past this position
                close_m = self._ACTION_CLOSE.search(self.buf, close_m.end())
            else:
                # No valid close tag found — fall through to partial extraction
                pass
            if not self._in_action:
                continue

            # Still inside action, no close yet — try partial extraction
            _partial_events = list(self._extract_json(self.buf, partial=True))
            yield from _partial_events
            # If we successfully extracted complete calls, clear buffer
            # AND exit action mode. The closing tag may arrive in a later
            # chunk as an orphaned fragment (e.g. Qwen splits </tool_call>
            # across chunks and the JSON was already complete before the
            # closing tag arrived). Without this, the orphaned fragment
            # leaks as visible text.
            if any(e.get("type") == "tag_found" for e in _partial_events):
                self.buf = ""
                self._in_action = False
            break

    def _extract_json(self, raw: str, partial: bool) -> Generator[dict[str, Any], None, None]:
        """Attempt to parse tool_call content as JSON tool calls."""
        raw_stripped = raw.strip()

        # Try to parse complete JSON
        calls = _parse_action_payload(raw_stripped)
        if calls:
            self._pending_tag = None
            self._last_progress = (0, 0)
            for call in calls:
                _plog(f"TAG_FOUND: {call['name']} | attrs_keys={list(call['attrs'].keys())} | content_len={len(call.get('content',''))}")
                yield {
                    "type": "tag_found",
                    "name": call["name"],
                    "attrs": call["attrs"],
                    "content": call["content"],
                }
            return

        if not raw_stripped:
            return

        # If content doesn't start with JSON delimiter, it's not a tool call — discard silently
        if raw_stripped[0] not in ('{', '['):
            _plog(f"NON_JSON_CONTENT: discarding {repr(raw_stripped[:50])}")
            return

        # Log parse failure on final (non-partial) attempts
        if not partial:
            _plog(f"PARSE_FAIL: len={len(raw_stripped)} | first_200={repr(raw_stripped[:200])} | last_100={repr(raw_stripped[-100:])}")
            # Emit parse_error so the model gets feedback about the malformed tool_call
            yield {
                "type": "parse_error",
                "raw": raw_stripped[:500],
                "reason": "Tool call JSON could not be parsed. Check for invalid escapes (use \\n not literal newlines in strings), unmatched brackets, or trailing characters.",
            }
            return

        if partial:
            # Emit tool_pending if we can identify the tool name
            m = self._TOOL_NAME_RE.search(raw_stripped)
            if m:
                tag_name = m.group(1).lower()
                if tag_name != self._pending_tag:
                    self._pending_tag = tag_name
                    self._last_progress = (0, 0)
                    yield {"type": "tool_pending", "tag": tag_name, "attrs": {}}

            # Emit progress for large content
            p_lines = raw_stripped.count("\n") + (1 if raw_stripped else 0)
            p_bytes = len(raw_stripped.encode("utf-8"))
            last_lines, last_bytes = self._last_progress
            if p_lines != last_lines or p_bytes - last_bytes >= 96:
                self._last_progress = (p_lines, p_bytes)
                yield {
                    "type": "tool_progress",
                    "tag": self._pending_tag or "unknown",
                    "lines": p_lines,
                    "bytes": p_bytes,
                }

    def flush(self) -> Generator[dict[str, Any], None, None]:
        """Flush remaining buffer. Extracts any complete tool_call blocks."""
        if self.buf:
            if self._in_action:
                _plog(f"FLUSH_IN_ACTION: buf_len={len(self.buf)} | first_100={repr(self.buf[:100])}")
                yield from self._extract_json(self.buf, partial=False)
            # Strip any remaining tool_call remnants
            if self.buf:
                cleaned = self._ACTION_OPEN.sub("", self.buf)
                cleaned = self._ACTION_CLOSE.sub("", cleaned).strip()
                if cleaned:
                    _plog(f"FLUSH_TEXT_REMNANT: len={len(cleaned)} | preview={repr(cleaned[:150])}")
                    yield {"type": "text", "text": cleaned}
                self.buf = ""
        self._in_action = False
