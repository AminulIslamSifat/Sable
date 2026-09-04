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

    "view_file",
    "edit_file",
    "create_file",
    "insert_file",
    "run_simulacra",
    "spawn_agent",
    "agent_status",
    "kill_agent",
    "todo_complete",
    "todo_skip",
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


# Matches fenced code blocks: ```lang\n...\n``` or ```\n...\n```
# Used to exclude code examples from bare JSON tool call detection.
_CODE_FENCE_RE = re.compile(r"```[^\n]*\n.*?```", re.DOTALL)


def _strip_code_fences(text: str) -> str:
    """Remove markdown fenced code blocks from text.

    Prevents bare JSON detection from triggering on code examples
    that contain tool call syntax inside triple-backtick fences.
    """
    return _CODE_FENCE_RE.sub("", text)


def _sanitize_json_escapes(raw: str) -> str:
    """Remove invalid backslash escapes that models sometimes produce."""
    # Replace \' with just ' (most common offender)
    raw = raw.replace("\\'", "'")
    # Catch any other invalid escapes: backslash not followed by valid escape char
    return _INVALID_ESCAPE_RE.sub("", raw)

def _extract_first_json_array(raw: str) -> str:
    """Extract the first valid JSON array from potentially concatenated arrays.

    Models (especially DeepSeek) sometimes emit multiple JSON arrays back-to-back
    like '[{...}][{...}]' or '[{...}][{...}]trailing prose'. This function finds
    the first complete [...] boundary and returns just that substring.

    Returns the original string if no concatenation is detected.
    """
    raw = raw.strip()
    if not raw.startswith("["):
        return raw

    depth = 0
    in_string = False
    escape_next = False
    for i, ch in enumerate(raw):
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                # Found end of first array — check if there's junk after
                remainder = raw[i + 1:].strip()
                if remainder:
                    # Concatenated arrays or trailing garbage detected
                    return raw[: i + 1]
                return raw  # Clean single array
    return raw  # No complete array found, return as-is


def _parse_action_payload(raw: str) -> list[dict[str, Any]]:
    """Parse tool_call JSON content into a list of normalized tool call dicts.

    Returns list of {"name": str, "attrs": dict[str,str], "content": str}.
    Accepts both Hermes format {"name": ..., "arguments": ...} and legacy
    Legacy {"tool": ..., "params": ...} keys are normalized to Hermes format internally.
    """
    raw = raw.strip()
    if not raw:
        return []

    # Fix transport-level corruption (backslashes, mixed quotes)
    raw = sanitize_transport(raw)

    # Handle concatenated JSON arrays (DeepSeek retry anti-pattern)
    raw = _extract_first_json_array(raw)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Only attempt repair if JSON is structurally complete (balanced brackets).
        # Streaming chunks like '[{"name":' are NOT complete — repairing them
        # produces valid-but-wrong JSON that gets consumed as real tool calls.
        if not json_structurally_complete(raw):
            return []
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
        params = item.get("arguments") or item.get("params")
        if not isinstance(params, dict):
            # Fallback: treat top-level keys (besides name/tool) as implicit params.
            # Models sometimes flatten {"name": "x", "command": "..."} instead of
            # nesting under "arguments". This prevents silent data loss.
            _RESERVED_KEYS = {"name", "tool"}
            params = {k: v for k, v in item.items() if k not in _RESERVED_KEYS}

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


def _find_json_end(raw: str) -> int:
    """Find the index where the first complete JSON structure ends.

    Tracks brace/bracket depth respecting strings and escapes.
    Returns the index past the closing bracket/brace, or -1 if incomplete.
    """
    stripped = raw.strip()
    if not stripped or stripped[0] not in ('{', '['):
        return -1

    depth = 0
    in_string = False
    escape_next = False

    for i, ch in enumerate(stripped):
        if escape_next:
            escape_next = False
            continue
        if ch == chr(92) and in_string:
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch in ('{', '['):
            depth += 1
        elif ch in ('}', ']'):
            depth -= 1
            if depth == 0:
                # Return offset relative to original raw (account for leading whitespace)
                leading = len(raw) - len(raw.lstrip())
                return leading + i + 1
    return -1


class SkillParser:
    """Extracts complete Hermes-style tool calls from streamed answer text.

    Tool calls are parsed from <tool_call>...</tool_call> boundaries.
    Text outside tool_call blocks is emitted as plain prose.
    Complete calls are yielded as {"type": "tag_found", ...} events
    for the engine to dispatch through the middleware pipeline.
    """

    # Supports both <tool_call (Hermes) and <action> (Qwen native) wrappers.
    _ACTION_OPEN = re.compile(r"(?:<\s*tool_calls?\s*>|<\s*action\s*>)", re.I)
    _ACTION_CLOSE = re.compile(r"(?:<\s*/\s*tool_calls?\s*>|<\s*/\s*action\s*>)", re.I)
    _TOOL_NAME_RE = re.compile(r'"name"\s*:\s*"([^"]+)"')

    # DSML (DeepSeek Markup Language) patterns — tolerates missing leading ｜,
    # underscore variants (dsml_tool_calls), ASCII pipe |, and mixed delimiters.
    # DeepSeek sometimes uses ASCII | (U+007C) instead of fullwidth ｜ (U+FF5C).
    _DSML_OPEN = re.compile(r"<[｜|_]?DSML[｜_|]tool_calls\s*>", re.I)
    _DSML_CLOSE = re.compile(r"</[｜|_]?DSML[｜_|]tool_calls\s*>", re.I)
    _DSML_INVOKE_RE = re.compile(
        r'<[｜|_]?DSML[｜|_]invoke\s+name="([^"]+)"\s*>(.*?)</[｜|_]?DSML[｜|_]invoke\s*>',
        re.DOTALL,
    )
    _DSML_PARAM_RE = re.compile(
        r'<[｜|_]?DSML[｜|_]parameter\s+name="([^"]+)"(?:\s+string="(true|false)")?\s*>(.*?)</[｜|_]?DSML[｜|_]parameter\s*>',
        re.DOTALL,
    )
    # Legacy XML invoke/parameter (Qwen3 XML fallback, older DeepSeek drift)
    _LEGACY_INVOKE_RE = re.compile(
        r'<invoke\s+name="([^"]+)"\s*>(.*?)</invoke\s*>',
        re.DOTALL,
    )
    _LEGACY_PARAM_RE = re.compile(
        r'<parameter\s+name="([^"]+)"(?:\s+string="(?:true|false)")?\s*>(.*?)</parameter\s*>',
        re.DOTALL,
    )
    _LEGACY_BLOCK_OPEN = re.compile(r"<tool_calls\s*>", re.I)
    _LEGACY_BLOCK_CLOSE = re.compile(r"</tool_calls\s*>", re.I)

    # Safety net regex for orphaned tags in bare-JSON prefix text.
    # Also catches bare fragments like "action>" or "tool_call>" that result
    # from </ being consumed by partial-tag detection in a prior chunk.
    _ORPHAN_TAG_RE = re.compile(
        r'(?:</?\s*(?:action|tool_calls?)\s*>|(?:^|(?<=[\s</]))(?:action|tool_calls?)\s*>)',
        re.IGNORECASE,
    )

    def __init__(self, known_tags: tuple[str, ...] | None = None) -> None:
        self._known_tags = set(known_tags or KNOWN_TAGS)
        self.buf = ""
        self._in_action = False
        self._in_dsml = False
        self._pending_tag: str | None = None
        self._last_progress: tuple[int, int] = (0, 0)

    @classmethod
    def _strip_orphan_tags(cls, text: str) -> str:
        """Remove orphaned action/tool_call tags from prose text."""
        return cls._ORPHAN_TAG_RE.sub("", text)

    def feed(self, text: str) -> Generator[dict[str, Any], None, None]:
        """Feed a chunk of streamed text. Yields events as calls/prose are resolved."""
        self.buf += text

        while True:
            if not self._in_action:
                # Strip orphaned closing tag fragments that arrive after
                # partial extraction already consumed the tool call.
                # Handle both complete closing tags and partial suffixes
                # like "_call>" or "call>" that are remnants of </tool_call>
                # Skip if an opening tag is also present — the close tag belongs
                # to it and will be handled by the normal close-boundary logic.
                _orphan_close = self._ACTION_CLOSE.search(self.buf)
                if _orphan_close is not None and not self._ACTION_OPEN.search(self.buf):
                    # Silently strip orphan close tags — they're leftover fragments, not prose
                    _before_close = self.buf[:_orphan_close.start()]
                    _after_close = self.buf[_orphan_close.end():]
                    if _before_close:
                        yield {"type": "text", "text": _before_close}
                    self.buf = _after_close
                    if not self.buf:
                        break
                # Check for orphaned suffix fragments (no < prefix)
                _stripped_buf = self.buf.strip()
                if _stripped_buf and "<" not in _stripped_buf:
                    # Could be a fragment like "_call>", "call>", ">"
                    _suffix_variants = ["</" + "tool_call>", "</" + "tool_calls>"]
                    _is_fragment = False
                    for _suffix_of_close in _suffix_variants:
                        for _flen in range(1, len(_suffix_of_close)):
                            if _stripped_buf == _suffix_of_close[-_flen:] or _stripped_buf == _suffix_of_close[_flen:]:
                                _is_fragment = True
                                break
                        if _is_fragment:
                            break
                    if _is_fragment:
                        self.buf = ""
                        break


                # --- DSML / Legacy XML detection (DeepSeek, Qwen3 XML) ---
                # Check for DSML or legacy XML blocks before Hermes/bare JSON.
                # These use invoke/parameter XML instead of JSON.
                _dsml_open_m = self._DSML_OPEN.search(self.buf)
                _legacy_open_m = self._LEGACY_BLOCK_OPEN.search(self.buf) if not _dsml_open_m else None
                _xml_open_m = _dsml_open_m or _legacy_open_m
                if _xml_open_m is not None:
                    _is_dsml = _dsml_open_m is not None
                    _xml_close = self._DSML_CLOSE if _is_dsml else self._LEGACY_BLOCK_CLOSE
                    _close_m = _xml_close.search(self.buf, _xml_open_m.end())
                    # Hybrid fallback: DSML open + legacy close (or vice versa)
                    # DeepSeek sometimes mixes <DSML|tool_calls> with </tool_calls>
                    if _close_m is None:
                        _alt_close = self._LEGACY_BLOCK_CLOSE if _is_dsml else self._DSML_CLOSE
                        _close_m = _alt_close.search(self.buf, _xml_open_m.end())
                    if _close_m is not None:
                        # Complete block found — extract and parse
                        _before_xml = self.buf[:_xml_open_m.start()]
                        _block_content = self.buf[_xml_open_m.start():_close_m.end()]
                        _after_xml = self.buf[_close_m.end():]
                        # Guard: if legacy <tool_calls> wraps JSON (not XML invoke tags),
                        # skip XML extraction — let the Hermes handler parse it as JSON.
                        _inner = _block_content[_xml_open_m.end() - _xml_open_m.start():_close_m.start() - _xml_open_m.start()].strip() if len(_block_content) > (_xml_open_m.end() - _xml_open_m.start()) else ""
                        _inner_stripped = _block_content[len(_xml_open_m.group()):].lstrip()
                        _is_json_in_legacy = (not _is_dsml and _inner_stripped and _inner_stripped[0] in ('{', '['))
                        if _is_json_in_legacy:
                            _plog(f"LEGACY_BLOCK_WITH_JSON: skipping XML extraction, falling through to Hermes handler")
                            # Don't consume — let the Hermes _ACTION_OPEN handler below pick it up
                            # But we need to avoid infinite loop: the legacy regex matches same position.
                            # Solution: strip the legacy open tag and replace with _ACTION_OPEN-compatible form
                            # Actually simpler: just don't enter this branch. Remove legacy match so Hermes gets it.
                            _xml_open_m = None  # force fallthrough
                        else:
                            if _before_xml.strip():
                                yield {"type": "text", "text": _before_xml}
                            self.buf = _after_xml
                            _plog(f"{'DSML' if _is_dsml else 'LEGACY_XML'}_BLOCK_FOUND: len={len(_block_content)}")
                            yield from self._extract_dsml(_block_content)
                            continue  # re-evaluate buffer
                    else:
                        # Open tag found but no close yet — hold buffer, wait for more
                        _before_xml = self.buf[:_xml_open_m.start()]
                        if _before_xml.strip():
                            yield {"type": "text", "text": _before_xml}
                            self.buf = self.buf[_xml_open_m.start():]
                        # Emit pending indicator for first invoke if visible
                        # Try both DSML and legacy invoke patterns (hybrid blocks)
                        _inv_m = self._DSML_INVOKE_RE.search(self.buf) or self._LEGACY_INVOKE_RE.search(self.buf)
                        if _inv_m:
                            _tag_name = _inv_m.group(1).strip().lower()
                            if _tag_name != self._pending_tag:
                                self._pending_tag = _tag_name
                                yield {"type": "tool_pending", "tag": _tag_name, "attrs": {}}
                        break  # wait for closing tag

                # --- Bare JSON detection (no <tool_call> wrapper) ---
                # Models sometimes emit [{...}] or {...} directly without tags.
                # Scan entire buffer for JSON structures that look like tool calls,
                # not just at the start — text often precedes the JSON in streams.
                if not self._in_action and self._ACTION_OPEN.search(self.buf) is None:
                    _bare_done = False   # parsed or errored → continue while loop
                    _bare_hold = False   # incomplete JSON → break while loop (wait for more)
                    # Build set of character indices inside code fences to skip
                    _fenced_indices: set[int] = set()
                    for _fence_match in _CODE_FENCE_RE.finditer(self.buf):
                        _fenced_indices.update(range(_fence_match.start(), _fence_match.end()))
                    for _scan_i, _scan_ch in enumerate(self.buf):
                        if _scan_i in _fenced_indices:
                            continue  # Skip characters inside code fences
                        if _scan_ch in ('{', '['):
                            _candidate = self.buf[_scan_i:]
                            _json_end_idx = _find_json_end(_candidate)
                            if _json_end_idx > 0:
                                _json_str = _candidate[:_json_end_idx]
                                _bare_calls = _parse_action_payload(_json_str)
                                if _bare_calls:
                                    _plog(f"BARE_JSON_TOOL_CALL: parsed {len(_bare_calls)} calls at offset {_scan_i}")
                                    if _scan_i > 0 and self.buf[:_scan_i].strip():
                                        yield {"type": "text", "text": self._strip_orphan_tags(self.buf[:_scan_i])}
                                    self.buf = _candidate[_json_end_idx:]
                                    self._pending_tag = None
                                    self._last_progress = (0, 0)
                                    for call in _bare_calls:
                                        yield {
                                            "type": "tag_found",
                                            "name": call["name"],
                                            "attrs": call["attrs"],
                                            "content": call["content"],
                                        }
                                    _bare_done = True
                                    break
                                elif json_structurally_complete(_json_str):
                                    # Only emit parse_error if content looks like a tool call attempt.
                                    # Prose with balanced brackets (e.g. [!CAUTION], [...]) should be
                                    # treated as text, not failed tool calls — emitting errors for
                                    # non-tool-call content creates feedback loops when error messages
                                    # containing brackets get re-injected into conversation context.
                                    _looks_like_tool_call = (
                                        '"name"' in _json_str
                                        or '"tool"' in _json_str
                                        or '"tag"' in _json_str
                                    )
                                    if _looks_like_tool_call:
                                        _plog(f"BARE_JSON_PARSE_ERROR: offset={_scan_i} len={len(_json_str)}")
                                        if _scan_i > 0 and self.buf[:_scan_i].strip():
                                            yield {"type": "text", "text": self._strip_orphan_tags(self.buf[:_scan_i])}
                                        self.buf = _candidate[_json_end_idx:]
                                        yield {
                                            "type": "parse_error",
                                            "raw": _json_str[:500],
                                            "reason": "Bare JSON detected without <tool_call> wrapper. Could not parse as tool calls. Use <tool_call>[{...}]</tool_call> format.",
                                        }
                                        _bare_done = True
                                        break
                                    else:
                                        # Not a tool call — treat as prose, skip past this bracket block
                                        _plog(f"BARE_JSON_NON_TOOL: offset={_scan_i} len={len(_json_str)} | preview={repr(_json_str[:80])}")
                                        if _scan_i > 0 and self.buf[:_scan_i].strip():
                                            yield {"type": "text", "text": self._strip_orphan_tags(self.buf[:_scan_i])}
                                        # Emit the bracket content as text and continue scanning
                                        yield {"type": "text", "text": _json_str}
                                        self.buf = _candidate[_json_end_idx:]
                                        _bare_done = True
                                        break
                            else:
                                # Incomplete JSON at _scan_i — emit preceding text, hold rest
                                if _scan_i > 0:
                                    yield {"type": "text", "text": self._strip_orphan_tags(self.buf[:_scan_i])}
                                    self.buf = self.buf[_scan_i:]
                                _bare_hold = True
                                break
                    if _bare_done:
                        continue
                    if _bare_hold:
                        break  # exit while loop, wait for more chunks


                m = self._ACTION_OPEN.search(self.buf)
                if m is None:
                    # No action open — flush as prose (hold trailing partial)
                    idx = self.buf.rfind("<")
                    if idx >= 0 and ">" not in self.buf[idx:]:
                        tail = self.buf[idx:].lstrip("<").strip().lower()
                        # Strip optional leading pipe (fullwidth ｜, ASCII |) or underscore
                        _tail_norm = tail.lstrip("\uff5c|_")
                        # Check for partial opening OR closing tag
                        # Covers: <tool_call, <DSML|tool_calls, <|DSML|tool_calls,
                        #         <dsml_tool_calls, <DSML_tool_calls, <DSML|..., <|, <|
                        _dsml_prefixes = (
                            "dsml", "dsml\uff5c", "\uff5cdsml", "dsml_", "_dsml",
                            "dsml|", "|dsml", "dsml\uff5c", "\uff5cdsml",
                        )
                        # If tail is ONLY delimiter chars (｜, |, _) after stripping <,
                        # it's a partial DSML tag like "<｜" or "<|" — hold it.
                        _is_dsml_delimiters_only = bool(tail) and not _tail_norm and all(
                            c in "\uff5c|_" for c in tail
                        )
                        _legacy_prefixes = ("action", "tool_call", "tool_calls", "invoke", "parameter")
                        _open_match = (
                            tail == ""
                            or _is_dsml_delimiters_only
                            or any(p.startswith(tail) or tail.startswith(p) for p in _legacy_prefixes if tail)
                            or any(_tail_norm.startswith(p) or p.startswith(_tail_norm)
                                   for p in _dsml_prefixes if _tail_norm)
                        )
                        _close_tail = self.buf[idx:].lstrip("<").lstrip("/").strip().lower()
                        _close_tail_norm = _close_tail.lstrip("\uff5c|_")
                        _close_match = ("/" in self.buf[idx:idx+2] and
                                        (_close_tail == ""
                                         or any(p.startswith(_close_tail) or _close_tail.startswith(p)
                                                for p in _legacy_prefixes if _close_tail)
                                         or any(_close_tail_norm.startswith(p) or p.startswith(_close_tail_norm)
                                                for p in _dsml_prefixes if _close_tail_norm)))
                        if _open_match or _close_match:
                            if idx > 0:
                                yield {"type": "text", "text": self.buf[:idx]}
                            self.buf = self.buf[idx:]
                            break
                    if self.buf:
                        yield {"type": "text", "text": self._strip_orphan_tags(self.buf)}
                        self.buf = ""
                    break
                # Found action open — emit prose before it, validate JSON before entering action mode
                before = self.buf[:m.start()]
                if before:
                    yield {"type": "text", "text": before}
                self.buf = self.buf[m.end():]

                # JSON validation gate: only enter action mode if content looks like JSON
                stripped_ahead = self.buf.lstrip()
                if not stripped_ahead:
                    # Tag found but no content yet — hold and wait for next chunk
                    # Don't enter action mode blindly; re-prepend the tag so it's
                    # re-evaluated when more data arrives.
                    self.buf = m.group(0) + self.buf
                    break
                # Strip markdown backtick wrappers models sometimes add around JSON
                # Handles both single ` and triple ``` fences (with optional lang tag)
                if stripped_ahead[0] == '`':
                    _fence_len = 1
                    while _fence_len < len(stripped_ahead) and stripped_ahead[_fence_len] == '`':
                        _fence_len += 1
                    # Find matching closing fence
                    _close_fence = '`' * _fence_len
                    _bt_end = stripped_ahead.find(_close_fence, _fence_len)
                    if _bt_end > 0:
                        _inner = stripped_ahead[_fence_len:_bt_end].strip()
                        # Strip optional language tag like "json\n" from start of fence content
                        if _inner and not _inner[0] in ('{', '['):
                            _nl = _inner.find('\n')
                            if _nl > 0 and _nl < 20:
                                _inner = _inner[_nl+1:].strip()
                        if _inner and _inner[0] in ('{', '['):
                            # Reconstruct buffer with unwrapped JSON
                            self.buf = _inner + stripped_ahead[_bt_end+_fence_len:].lstrip()
                            stripped_ahead = self.buf
                            _plog(f"STRIPPED_BACKTICK_WRAPPER: fence={_fence_len}, inner starts with {repr(_inner[:20])}")
                if stripped_ahead[0] not in ('{', '['):
                    # Not JSON — preserve the ENTIRE sequence (tags + content) as visible text
                    # Use self.buf (not stripped_ahead) to preserve whitespace between tag and content
                    _open_tag = m.group(0)
                    _ahead = self.buf  # original buffer with whitespace intact
                    _plog(f"NON_JSON_TOOL_CALL: starts with {repr(stripped_ahead[:30])}, emitting as text")
                    # Check if there's a closing tag — if so, emit full block as text
                    _close_in_ahead = self._ACTION_CLOSE.search(_ahead)
                    if _close_in_ahead is not None:
                        # Emit open tag + inner text + close tag as visible prose
                        _full_block = _open_tag + _ahead[:_close_in_ahead.end()]
                        yield {"type": "text", "text": _full_block}
                        self.buf = _ahead[_close_in_ahead.end():]
                    else:
                        # No closing tag yet — emit open tag as text, hold rest
                        yield {"type": "text", "text": _open_tag}
                        # Look for next < that might be a tag start
                        next_lt = _ahead.find("<")
                        if next_lt >= 0:
                            _before_lt = _ahead[:next_lt]
                            if _before_lt:
                                yield {"type": "text", "text": _before_lt}
                            self.buf = _ahead[next_lt:]
                        else:
                            # No more tags — emit all remaining text
                            if _ahead:
                                yield {"type": "text", "text": _ahead}
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

    @classmethod
    def _parse_dsml_params(cls, inner: str) -> dict[str, Any]:
        """Parse DSML parameter tags into a dict. Handles string="true|false" typing."""
        attrs: dict[str, Any] = {}
        for m in cls._DSML_PARAM_RE.finditer(inner):
            pname, is_str, pval = m.group(1), m.group(2), m.group(3).strip()
            if is_str == "true":
                attrs[pname] = pval
            else:
                try:
                    attrs[pname] = json.loads(pval)
                except (json.JSONDecodeError, ValueError):
                    attrs[pname] = pval
        return attrs

    # Extended legacy param regex that captures optional string="true|false"
    _LEGACY_PARAM_TYPED_RE = re.compile(
        r'<parameter\s+name="([^"]+)"(?:\s+string="(true|false)")?\s*>(.*?)</parameter\s*>',
        re.DOTALL,
    )

    @classmethod
    def _parse_legacy_params(cls, inner: str) -> dict[str, Any]:
        """Parse legacy XML <parameter> tags. Respects string="true|false" when present."""
        attrs: dict[str, Any] = {}
        for m in cls._LEGACY_PARAM_TYPED_RE.finditer(inner):
            pname, is_str, pval = m.group(1), m.group(2), m.group(3).strip()
            if is_str == "true":
                attrs[pname] = pval
            elif is_str == "false":
                try:
                    attrs[pname] = json.loads(pval)
                except (json.JSONDecodeError, ValueError):
                    attrs[pname] = pval
            else:
                # No type annotation — auto-detect
                try:
                    attrs[pname] = json.loads(pval)
                except (json.JSONDecodeError, ValueError):
                    attrs[pname] = pval
        return attrs

    def _extract_dsml(self, block: str) -> Generator[dict[str, Any], None, None]:
        """Parse a complete DSML/legacy tool_calls block and yield tag_found events.
        
        Always tries BOTH DSML and legacy invoke patterns since DeepSeek can produce
        hybrid blocks (e.g. <DSML|tool_calls> with plain <invoke> inside).
        """
        found = False
        # Try DSML invoke pattern first
        for m in self._DSML_INVOKE_RE.finditer(block):
            name = m.group(1).strip()
            params = self._parse_dsml_params(m.group(2))
            content = ""
            for ck in _CONTENT_PARAM_KEYS:
                if ck in params:
                    content = str(params[ck])
                    break
            str_attrs = _stringify_params(params)
            _plog(f"DSML_TAG_FOUND: {name} | attrs_keys={list(str_attrs.keys())}")
            yield {
                "type": "tag_found",
                "name": name,
                "attrs": str_attrs,
                "content": content,
            }
            found = True
        # ALSO try legacy invoke/parameter format (handles hybrid blocks)
        for m in self._LEGACY_INVOKE_RE.finditer(block):
            name = m.group(1).strip()
            params = self._parse_legacy_params(m.group(2))
            content = ""
            for ck in _CONTENT_PARAM_KEYS:
                if ck in params:
                    content = str(params[ck])
                    break
            str_attrs = _stringify_params(params)
            _plog(f"LEGACY_XML_TAG_FOUND: {name} | attrs_keys={list(str_attrs.keys())}")
            yield {
                "type": "tag_found",
                "name": name,
                "attrs": str_attrs,
                "content": content,
            }
            found = True
        if not found:
            _plog(f"DSML_PARSE_FAIL: no invokes found in block len={len(block)}")

    def _extract_json(self, raw: str, partial: bool) -> Generator[dict[str, Any], None, None]:
        """Attempt to parse tool_call content as JSON tool calls."""
        raw_stripped = raw.strip()

        # Try to parse complete JSON
        calls = _parse_action_payload(raw_stripped)
        if calls:
            # Emit a final progress snapshot for file-writing tools so the card
            # shows content preview even when the entire call arrives in one chunk.
            for call in calls:
                cname = call["name"]
                if cname in ("create_file", "edit_file", "insert_file"):
                    preview_lines: list[str] = []
                    cpath = call["attrs"].get("path", "")
                    if cpath:
                        preview_lines.append(cpath)
                    ccontent = call.get("content", "") or call["attrs"].get("body", "")
                    if ccontent:
                        all_lines = [ln for ln in ccontent.splitlines() if ln.strip()]
                        for ln in all_lines[-3:]:
                            preview_lines.append(ln[:150])
                    if preview_lines:
                        yield {
                            "type": "tool_progress",
                            "tag": cname,
                            "bytes": len(raw_stripped.encode("utf-8")),
                            "preview_lines": preview_lines,
                        }
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

        # Discard obvious placeholder/ellipsis content instead of surfacing parse_error.
        # Models sometimes emit <action>[...]</action> when summarizing or truncating.
        if re.fullmatch(r'\[?\s*\.{2,}\s*\]?', raw_stripped):
            _plog(f"ELLIPSIS_PLACEHOLDER: discarding {repr(raw_stripped[:50])}")
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

            # Emit progress with live content preview
            p_bytes = len(raw_stripped.encode("utf-8"))
            last_lines, last_bytes = self._last_progress
            if p_bytes - last_bytes >= 32 or last_bytes == 0:
                self._last_progress = (0, p_bytes)
                preview_lines: list[str] = []
                tag = self._pending_tag or "unknown"
                # Extract path/filename for file tools
                path_m = re.search(r'"(?:path|filename)"\s*:\s*"([^"]*)', raw_stripped)
                if path_m:
                    preview_lines.append(path_m.group(1))
                # For edit/create/insert, show last few lines of content being written
                content_m = re.search(r'"(?:content|body|lines)"\s*:\s*"(.*?)$', raw_stripped, re.DOTALL)
                if content_m:
                    raw_tail = content_m.group(1)[-500:]
                    try:
                        snippet = raw_tail.encode("utf-8").decode("unicode_escape")
                    except Exception:
                        snippet = raw_tail
                    all_lines = [ln for ln in snippet.splitlines() if ln.strip()]
                    # Show last 3 non-empty lines
                    for ln in all_lines[-3:]:
                        preview_lines.append(ln[:150])
                if not preview_lines:
                    size = f"{p_bytes} B" if p_bytes < 1024 else f"{p_bytes/1024:.1f} KB"
                    preview_lines.append(f"streaming\u2026 {size}")
                yield {
                    "type": "tool_progress",
                    "tag": tag,
                    "bytes": p_bytes,
                    "preview_lines": preview_lines,
                }

    def flush(self) -> Generator[dict[str, Any], None, None]:
        """Flush remaining buffer. Extracts any complete tool_call or DSML blocks."""
        # Reset progress state so stale events don't emit after stream ends
        self._pending_tag = None
        self._last_progress = (0, 0)
        if self.buf:
            # Try DSML/legacy extraction on remaining buffer
            _dsml_m = self._DSML_OPEN.search(self.buf)
            _legacy_m = self._LEGACY_BLOCK_OPEN.search(self.buf) if not _dsml_m else None
            _xml_m = _dsml_m or _legacy_m
            if _xml_m is not None:
                _is_dsml = _dsml_m is not None
                _xml_close = self._DSML_CLOSE if _is_dsml else self._LEGACY_BLOCK_CLOSE
                _close_m = _xml_close.search(self.buf, _xml_m.end())
                # Hybrid fallback: try alternate close tag
                if _close_m is None:
                    _alt_close = self._LEGACY_BLOCK_CLOSE if _is_dsml else self._DSML_CLOSE
                    _close_m = _alt_close.search(self.buf, _xml_m.end())
                if _close_m is not None:
                    _block = self.buf[_xml_m.start():_close_m.end()]
                    _before = self.buf[:_xml_m.start()]
                    _after = self.buf[_close_m.end():]
                    if _before.strip():
                        yield {"type": "text", "text": _before}
                    yield from self._extract_dsml(_block)
                    self.buf = _after
                else:
                    # Incomplete DSML block at flush — try to parse what we have
                    _plog(f"FLUSH_INCOMPLETE_DSML: buf_len={len(self.buf)}")
                    yield from self._extract_dsml(self.buf[_xml_m.start():])
                    self.buf = self.buf[:_xml_m.start()]

            if self._in_action:
                _plog(f"FLUSH_IN_ACTION: buf_len={len(self.buf)} | first_100={repr(self.buf[:100])}")
                yield from self._extract_json(self.buf, partial=False)
            # Strip any remaining tool_call / DSML remnants
            if self.buf:
                cleaned = self._ACTION_OPEN.sub("", self.buf)
                cleaned = self._ACTION_CLOSE.sub("", cleaned)
                cleaned = self._DSML_OPEN.sub("", cleaned)
                cleaned = self._DSML_CLOSE.sub("", cleaned)
                cleaned = self._LEGACY_BLOCK_OPEN.sub("", cleaned)
                cleaned = self._LEGACY_BLOCK_CLOSE.sub("", cleaned).strip()
                if cleaned:
                    _plog(f"FLUSH_TEXT_REMNANT: len={len(cleaned)} | preview={repr(cleaned[:150])}")
                    yield {"type": "text", "text": cleaned}
                self.buf = ""
        self._in_action = False
