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
import uuid
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
    "computer_use",
    "load_tool",
    "teacher_guidance",
)

# Params that map to the content field for handler compatibility.
_CONTENT_PARAM_KEYS = ("body", "content", "command")

# Presentation tags a model may write BARE (no <action>/<tool_call> wrapper)
# with a markup body, e.g. <render_component>...</render_component>. These are
# NOT skills — they are a response format. The parser turns the body into a
# native `ui_block` event the frontend renders directly; nothing goes through
# the skill engine, middleware, or tool feedback.
_BARE_TAG_NAMES = ("render_component", "render_html")
_HTML_MAX_BYTES = 200_000
_HTML_MIN_CHUNK_CHARS = 120


def _read_local_html(src: str) -> tuple[str | None, str | None]:
    """Read a local file for a render_html src= pane. Returns (html, error).

    Inlined on purpose - one fewer module to import, and this is the only
    caller. Local app, so no hostile-tenant model: the only person who can
    point a pane at a file is the user. The body still lands in the
    opaque-origin sandbox, so its scripts cannot reach the host DOM.
    """
    try:
        p = Path(src).expanduser()
        if not p.is_absolute():
            try:
                from engine.config import OUTPUT_ROOT
                p = Path(OUTPUT_ROOT) / src
            except Exception:
                p = Path.cwd() / src
        if not p.exists():
            return None, f"file not found: {p}"
        if p.is_dir():
            return None, f"not a file: {p}"
        data = p.read_bytes()
        if len(data) > _HTML_MAX_BYTES:
            data = data[:_HTML_MAX_BYTES]
        return data.decode("utf-8", errors="replace"), None
    except Exception as exc:
        return None, f"read failed: {exc}"

# Streaming partial-render tuning. While a <render_component> is still open we
# re-parse the body and emit `partial` ui_block events so the card builds up
# live instead of appearing all at once on the close tag. Re-parse only when
# the body has grown by this many chars (keeps it O(n), not O(n^2)).
_UI_PARTIAL_FIRST_CHARS = 40
_UI_PARTIAL_MIN_CHARS = 150


def _scan_for_unterminated(s: str) -> tuple[int, str | None]:
    """Find the first tag that never closes before end-of-string.

    Returns ``(index, open_quote)`` where ``open_quote`` is the quote char left
    dangling inside the tag (or ``None`` when the tag simply has no '>' yet).
    ``(-1, None)`` when every tag is closed.

    Quote-aware: a '>' inside an OPEN attribute value does not count as the
    tag's end. That was the raw-SVG leak — `markup="<svg …>"` looked complete
    because its value is full of '>' while its closing quote was still
    streaming. Only '<'+letter (or '</') counts as a tag start, so prose like
    `2 < 3` is ignored.
    """
    i, n = 0, len(s)
    while i < n:
        if s[i] != "<":
            i += 1
            continue
        k = i + 1
        if k < n and s[k] == "/":
            k += 1
        if k >= n or not s[k].isalpha():
            i += 1
            continue
        j, q, depth, closed = i + 1, None, 0, False
        while j < n:
            ch = s[j]
            if q:
                if ch == q:
                    q = None
            elif ch in "\"'":
                q = ch
            elif ch == "{":
                depth += 1
            elif ch == "}":
                if depth > 0:
                    depth -= 1
            elif ch == ">" and depth == 0:
                closed = True
                break
            j += 1
        if not closed:
            return i, q
        i = j + 1
    return -1, None


def _trim_incomplete_markup(s: str) -> str:
    """Make a partial <render_component> body parseable.

    A tag left open at end-of-string is either:
      * mid-attribute-value with a dangling quote (`markup="<svg …`) — we
        CLOSE the quote and the tag so the partial value still parses. This is
        what lets a big inline SVG stream: each update renders the SVG-so-far
        instead of staying blank until the final quote lands.
      * a half-written tag with no '>' yet (`<car`) — we DROP it; there is
        nothing meaningful to close.

    Also drops a trailing unterminated `{expr}`. Best-effort, never raises.
    """
    if not s:
        return s
    try:
        cut, q = _scan_for_unterminated(s)
        if cut != -1:
            if q:
                # Close the dangling attribute value, then the tag itself.
                s = s + q + ">"
            else:
                s = s[:cut]
        lb = s.rfind("{")
        if lb != -1 and "}" not in s[lb:]:
            s = s[:lb]
    except Exception:
        pass
    return s


def _bare_tag_attr(buf: str, start: int, name: str) -> str | None:
    """Pull an attribute off a bare open tag at `start`. Best-effort, never raises."""
    try:
        gt = buf.find(">", start)
        if gt < 0:
            return None
        head = buf[start:gt]
        m = re.search(rf'{re.escape(name)}\s*=\s*"([^"]*)"', head)
        if not m:
            m = re.search(rf"{re.escape(name)}\s*=\s*'([^']*)'", head)
        return m.group(1) if m else None
    except Exception:
        return None


def _scan_tag_end(buf: str, i: int) -> int:
    """From index `i` (just past the tag name), find the '>' that ends the tag.

    Quote-aware, so a '>' inside an attribute value doesn't close it early —
    `<render_html title="a>b">` ends at the real '>', not the one in the
    value. Returns the index AFTER '>' or -1 if the tag is still open.
    """
    q = None
    j, n = i, len(buf)
    while j < n:
        ch = buf[j]
        if q:
            if ch == q:
                q = None
        elif ch in "\"'":
            q = ch
        elif ch == ">":
            return j + 1
        j += 1
    return -1


def _find_bare_open(buf: str):
    """Locate the first bare markup tag. Returns (name, start, end) or None.

    Attributes ARE allowed (`<render_html title="…">`): the tag name must be
    followed by whitespace, '>', or '/', then a quote-aware scan finds the
    real end of the open tag.
    """
    low = buf.lower()
    best = None
    for name in _BARE_TAG_NAMES:
        needle = "<" + name
        i = 0
        while True:
            j = low.find(needle, i)
            if j < 0:
                break
            k = j + len(needle)
            if k < len(buf) and (buf[k].isspace() or buf[k] in ">/"):
                end = _scan_tag_end(buf, k)
                if end != -1:
                    if best is None or j < best[1]:
                        best = (name, j, end)
                break
            i = j + 1
    return best


def _find_bare_close(buf: str, name: str, start: int):
    """Locate the closing tag for a bare markup tag. Returns (start, end) or None."""
    low = buf.lower()
    needle = "</" + name
    i = start
    while True:
        j = low.find(needle, i)
        if j < 0:
            return None
        k = j + len(needle)
        if k < len(buf) and (buf[k].isspace() or buf[k] == ">"):
            end = _scan_tag_end(buf, k)
            if end != -1:
                return (j, end)
            return None
        i = j + 1


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

        # Extract content-bearing params into content field.
        # If no known content key matches, serialize the full params dict so
        # handlers that expect raw JSON args (e.g. critique) still receive them.
        content = ""
        for key in _CONTENT_PARAM_KEYS:
            val = params.get(key)
            if isinstance(val, str) and val.strip():
                content = val
                break

        # Special case: chat_title uses title/text param as content.
        # Must run BEFORE the json.dumps fallback or the whole args dict
        # gets serialized and stored as the literal title string.
        if not content and tool_name == "chat_title":
            for key in ("title", "text"):
                val = params.get(key)
                if isinstance(val, str) and val.strip():
                    content = val
                    break

        if not content and params:
            content = json.dumps(params, ensure_ascii=False)

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


def _looks_like_bare_tool_call(s: str) -> bool:
    """Cheap shape gate for bare (unwrapped) JSON tool calls.

    Only two shapes qualify:

      * ``[{...}]`` — an array whose first element is an object. Bare ``[]``,
        ``[1, 2]``, ``[true]`` etc. never do.
      * ``{...}`` that declares a top-level ``"name"`` / ``"tool"`` / ``"tag"``
        key within the first 400 chars. Empty ``{}`` and arbitrary JSON
        objects never qualify.

    Anything else is prose. This keeps stray ``[`` / ``{`` out of the bare
    tool-call path — the parser no longer holds the stream or emits spurious
    parse errors on markdown like ``[!CAUTION]`` or ``{key: value}``.
    """
    if not s:
        return False
    c0 = s[0]
    if c0 == "[":
        i, n = 1, len(s)
        while i < n and s[i].isspace():
            i += 1
        if i >= n:
            # Lone '[' (+ optional whitespace). No evidence of a tool call —
            # holding here would stall the stream waiting for a '{' that may
            # never come. If a '{' does arrive on the next feed, the buffer
            # becomes '[{' and the check below catches it then. So: do NOT hold.
            return False
        return s[i] == "{"
    if c0 == "{":
        head = s[:400]
        return '"name"' in head or '"tool"' in head or '"tag"' in head
    return False


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
    # Delimiter class: one or more of fullwidth ｜, ASCII |, or underscore.
    # Models vary wildly — single pipe, double pipe, mixed. Accept them all.
    # NOTE: Use (?:...)? for optional delimiter groups. Writing [chars]+? creates
    # a lazy quantifier, NOT "one-or-more optionally". That breaks matching.
    _D = r"(?:[｜|_]+)"
    # \s* after delimiter handles models that insert spaces: <｜｜DSML｜｜ calls>
    _DSML_OPEN = re.compile(rf"<{_D}?DSML{_D}\s*(?:tool_)?calls\s*>", re.I)
    # /? on close tags: handles both proper </DSML...> and bare <DSML...> as close
    _DSML_CLOSE = re.compile(rf"</?{_D}?DSML{_D}\s*(?:tool_)?calls\s*>", re.I)
    _DSML_INVOKE_RE = re.compile(
        rf'<{_D}?DSML{_D}\s*invoke\s+name="([^"]+)"\s*>(.*?)</?{_D}?DSML{_D}\s*invoke\s*>',
        re.DOTALL,
    )
    _DSML_PARAM_RE = re.compile(
        rf'<{_D}?DSML{_D}\s*parameter\s+name="([^"]+)"(?:\s+string="(true|false)")?\s*>(.*?)</?{_D}?DSML{_D}\s*parameter\s*>',
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

    # MiMo-style XML: <function=name><parameter=key>value</parameter></function>
    # MiMo uses attribute-equals syntax instead of name="..." for both functions and params.
    _MIMO_FUNCTION_RE = re.compile(
        r'<function\s*=\s*([^>\s]+)\s*>(.*?)</function\s*>',
        re.DOTALL | re.IGNORECASE,
    )
    _MIMO_PARAM_RE = re.compile(
        r'<parameter\s*=\s*([^>\s]+)\s*>(.*?)</parameter\s*>',
        re.DOTALL | re.IGNORECASE,
    )

    # Safety net regex for orphaned tags in bare-JSON prefix text.
    # Also catches bare fragments like "action>" or "tool_call>" that result
    # from </ being consumed by partial-tag detection in a prior chunk.
    # Also strips orphaned DSML tags with any pipe count.
    _ORPHAN_TAG_RE = re.compile(
        r'(?:</?\s*(?:action|tool_calls?|calls)\s*>|(?:^|(?<=[\s</]))(?:action|tool_calls?|calls)\s*>|'
        r'</?[｜|_]*DSML[｜|_]*\s*(?:tool_)?calls\s*>|</?[｜|_]*DSML[｜|_]*\s*(?:invoke|parameter)[^>]*>)',
        re.IGNORECASE,
    )

    def __init__(self, known_tags: tuple[str, ...] | None = None) -> None:
        self._known_tags = set(known_tags or KNOWN_TAGS)
        self.buf = ""
        self._in_action = False
        self._in_dsml = False
        self._pending_tag: str | None = None
        self._last_progress: tuple[int, int] = (0, 0)
        # Partial-render bookkeeping for <render_component>.
        self._ui_seq = 0
        self._ui_cur_id: str | None = None
        self._ui_last_parse_len = 0
        self._ui_last_hash: str | None = None
        self._html_seq = 0
        self._html_cur_id: str | None = None
        self._html_sent_len = 0

    @classmethod
    def _strip_orphan_tags(cls, text: str) -> str:
        """Remove orphaned action/tool_call tags from prose text."""
        return cls._ORPHAN_TAG_RE.sub("", text)

    def _next_ui_id(self) -> str:
        # Same reasoning as _next_html_id — see the comment there.
        self._ui_seq += 1
        self._ui_cur_id = f"uib_{uuid.uuid4().hex[:12]}"
        return self._ui_cur_id

    def _reset_ui_partial(self) -> None:
        self._ui_cur_id = None
        self._ui_last_parse_len = 0
        self._ui_last_hash = None

    def _next_html_id(self) -> str:
        # MUST be unique across the whole session, not per-parser-instance.
        # A parser is rebuilt every round, so a counter would restart at 1 and
        # hand round N the same id as round 1 — the client keys panes by id and
        # would reuse (and corrupt) the previous pane's node.
        self._html_seq += 1
        self._html_cur_id = f"html_{uuid.uuid4().hex[:12]}"
        return self._html_cur_id

    def _reset_html_partial(self) -> None:
        self._html_cur_id = None
        self._html_sent_len = 0

    def _emit_html_partial(self, body: str, title: str, height=None) -> Generator[dict[str, Any], None, None]:
        """Stream a still-open <render_html> body as DELTAS.

        Only the newly-appended slice is sent each time (html_chunk), so a
        200 KB block costs 200 KB total over the wire, not O(n^2). The first
        emit carries html_start so the client can mount the pane skeleton.
        """
        if len(body) > _HTML_MAX_BYTES:
            body = body[:_HTML_MAX_BYTES]
        if self._html_cur_id is None:
            _bid = self._next_html_id()
            yield {"type": "html_start", "id": _bid, "title": title, "height": height}
        delta = body[self._html_sent_len:]
        if not delta or len(delta) < _HTML_MIN_CHUNK_CHARS:
            return
        self._html_sent_len = len(body)
        yield {"type": "html_chunk", "id": self._html_cur_id, "text": delta}

    def _emit_ui_partial(self, body: str) -> Generator[dict[str, Any], None, None]:
        """Parse a still-open <render_component> body and stream a partial tree.

        Fires only when the body grew enough to be worth re-parsing, and only
        when the resulting tree actually changed. The client keys containers by
        `id` so each partial re-renders into the same node; the final
        (partial=False) event seals it.
        """
        blen = len(body)
        first = self._ui_last_parse_len == 0
        need = _UI_PARTIAL_FIRST_CHARS if first else _UI_PARTIAL_MIN_CHARS
        if blen < need or (blen - self._ui_last_parse_len) < need:
            return
        trimmed = _trim_incomplete_markup(body)
        if not trimmed.strip():
            return
        try:
            from engine.ui_format import parse_ui_block
            _ui = parse_ui_block(trimmed, {})
        except Exception:
            _ui = None
        if not _ui:
            return
        try:
            _h = json.dumps(_ui, sort_keys=True)
        except Exception:
            _h = None
        if _h is not None and _h == self._ui_last_hash:
            self._ui_last_parse_len = blen
            return
        _bid = self._ui_cur_id or self._next_ui_id()
        self._ui_last_hash = _h
        self._ui_last_parse_len = blen
        yield {"type": "ui_block", "payload": _ui, "id": _bid, "partial": True}

    def feed(self, text: str) -> Generator[dict[str, Any], None, None]:
        """Feed a chunk of streamed text. Yields events as calls/prose are resolved."""
        self.buf += text

        while True:
            if not self._in_action:
                # --- Bare markup tags (<render_component> ... </render_component>) ---
                # The model may write presentation tags bare, with no <action>
                # wrapper. Intercept them here so the markup body reaches the
                # handler instead of leaking into the transcript as raw text.
                _bare = _find_bare_open(self.buf)
                if _bare is not None:
                    # A bare tag that falls inside an UNCLOSED tool-call
                    # wrapper is part of the call's arguments (e.g. a file
                    # edit whose body writes a render block), not a
                    # presentation block. Intercepting it corrupts the call.
                    _bsp = _bare[1]
                    _lo = None
                    for _m in self._ACTION_OPEN.finditer(self.buf, 0, _bsp):
                        _lo = _m
                    if _lo is not None and self._ACTION_CLOSE.search(
                            self.buf, _lo.end(), _bsp) is None:
                        _bare = None
                if _bare is not None:
                    _bname, _bs, _be = _bare
                    _bclose = _find_bare_close(self.buf, _bname, _be)
                    if _bclose is not None:
                        _bcs, _bce = _bclose
                        _bbefore = self.buf[:_bs]
                        _bbody = self.buf[_be:_bcs]
                        _open_head = self.buf[_bs:_be]
                        self.buf = self.buf[_bce:]
                        if _bbefore.strip():
                            yield {"type": "text", "text": self._strip_orphan_tags(_bbefore)}
                        _plog(f"BARE_TAG: {_bname} body_len={len(_bbody)}")
                        # Rich UI block — a response format, not a tool. Parse
                        # it here and emit a native event; never route it through
                        # the skill engine.
                        if _bname == "render_component":
                            # Seal: reuse the same id any partial streamed under,
                            # so the client replaces the partial with the final
                            # tree in the SAME node.
                            _bid = self._ui_cur_id or self._next_ui_id()
                            self._reset_ui_partial()
                            try:
                                from engine.ui_format import parse_ui_block
                                _ui = parse_ui_block(_bbody, {})
                            except Exception:
                                _ui = None
                            if _ui:
                                yield {"type": "ui_block", "payload": _ui, "id": _bid, "partial": False}
                            else:
                                # Final parse produced nothing usable — tell the
                                # client to drop whatever partial it drew.
                                yield {"type": "ui_block", "id": _bid, "remove": True}
                            continue
                        if _bname == "render_html":
                            # Raw HTML pane. Three ways to fill it:
                            #   (inline body)  the model writes the document
                            #   src="path"     a local file, read + inlined here
                            #   url="http…"    a LIVE page, framed directly
                            # src/url are read off the OPEN tag; the body (if
                            # any) is ignored when either is present.
                            _title = _bare_tag_attr(_open_head, 0, "title") or ""
                            _height = _bare_tag_attr(_open_head, 0, "height")
                            _src = _bare_tag_attr(_open_head, 0, "src")
                            _url = _bare_tag_attr(_open_head, 0, "url")

                            # url= -> direct iframe src. The pane loads the real
                            # page (local dev server, external site) with its own
                            # scripts intact. No body streams; the frame fetches
                            # it itself. `sandbox="allow-scripts"` still applies,
                            # so it stays isolated from the host.
                            if _url and not _src:
                                _hid = self._html_cur_id or self._next_html_id()
                                self._reset_html_partial()
                                yield {"type": "html_start", "id": _hid,
                                       "title": _title, "height": _height,
                                       "url": _url}
                                yield {"type": "html_end", "id": _hid}
                                continue

                            # src= -> local file. A file:// URL cannot be framed
                            # from an http page, so read it server-side and inline
                            # it through the normal chunk path.
                            _body = _bbody[:_HTML_MAX_BYTES]
                            _err = None
                            if _src:
                                _resolved, _rerr = _read_local_html(_src)
                                if _resolved is not None:
                                    _body = _resolved[:_HTML_MAX_BYTES]
                                else:
                                    _body = ""
                                    _err = _rerr or "src failed"
                            elif len(_bbody) > _HTML_MAX_BYTES:
                                _err = "too_large"
                            _hid = self._html_cur_id
                            if _hid is None:
                                _hid = self._next_html_id()
                                yield {"type": "html_start", "id": _hid,
                                       "title": _title, "height": _height}
                            _delta = _body[self._html_sent_len:]
                            self._reset_html_partial()
                            if _delta:
                                yield {"type": "html_chunk", "id": _hid, "text": _delta}
                            _end = {"type": "html_end", "id": _hid}
                            if _err:
                                _end["error"] = _err
                            yield _end
                            continue
                        yield {"type": "tag_found", "name": _bname, "attrs": {}, "content": _bbody}
                        continue
                    # Incomplete — emit preceding prose, then STREAM a partial
                    # render of the block as it grows, instead of holding the
                    # whole thing until the close tag lands.
                    if _bs > 0:
                        yield {"type": "text", "text": self._strip_orphan_tags(self.buf[:_bs])}
                        self.buf = self.buf[_bs:]
                        # Shift the tag start left by the prose we just dropped so
                        # the body slice below still lines up.
                        _be -= _bs
                        _bs = 0
                    if _bname == "render_component":
                        yield from self._emit_ui_partial(self.buf[_be:])
                    elif _bname == "render_html":
                        # src=/url= carry no streamed body — the source is read
                        # (src) or framed (url) at seal time. Streaming the
                        # half-written tag body here would paint junk first.
                        if (_bare_tag_attr(self.buf, 0, "src")
                                or _bare_tag_attr(self.buf, 0, "url")):
                            break
                        _t = _bare_tag_attr(self.buf, 0, "title") or ""
                        _hgt = _bare_tag_attr(self.buf, 0, "height")
                        yield from self._emit_html_partial(self.buf[_be:], _t, _hgt)
                    break

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
                        # Guard: if text before the DSML block contains <action> tags,
                        # process those first via the Hermes handler below. Otherwise
                        # the entire _before_xml (including action tags) gets yielded as prose.
                        _action_before_dsml = self._ACTION_OPEN.search(_before_xml)
                        if _is_json_in_legacy:
                            _plog(f"LEGACY_BLOCK_WITH_JSON: skipping XML extraction, falling through to Hermes handler")
                            _xml_open_m = None  # force fallthrough
                        elif _action_before_dsml:
                            _plog(f"ACTION_BEFORE_DSML: deferring DSML, processing action tags first")
                            _xml_open_m = None  # force fallthrough to Hermes handler
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
                        # Stream live progress while the block is still open —
                        # otherwise XML calls render as one silent lump at close.
                        yield from self._emit_xml_progress(self.buf)
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
                        if _scan_ch not in ('{', '['):
                            continue
                        # Shape gate: only proceed for genuine bare tool-call
                        # shapes — [{...}] or {...} declaring "name"/"tool"/"tag".
                        # Bare [] or standalone {} (or any non-tool JSON) is
                        # skipped here and left to flush as prose below.
                        if not _looks_like_bare_tool_call(self.buf[_scan_i:]):
                            continue
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
                                # Structurally complete but not parseable. Only
                                # surface a parse_error when the body actually
                                # carries a tool-call key (name/tool/tag) — that
                                # is a genuinely malformed tool call. Anything
                                # else (e.g. literal `[{}]`, `[{"foo":1}]`) is
                                # left to flush as prose below.
                                _is_tool_attempt = (
                                    '"name"' in _json_str
                                    or '"tool"' in _json_str
                                    or '"tag"' in _json_str
                                )
                                if _is_tool_attempt:
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
                                # else: fall through — leave buf untouched,
                                # continue scanning (the outer prose flush will
                                # emit this bracket run as text).
                            else:
                                # Passed the shape gate but not structurally complete yet.
                                _bare_hold = True
                                break
                        else:
                            # Complete shape prefix, still streaming — hold & wait.
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
                        _legacy_prefixes = ("action", "tool_call", "tool_calls", "calls", "invoke", "parameter")
                        # Bare presentation tags (render_component) must also be
                        # held when split across chunks — else the opening half
                        # flushes as prose and the block never parses.
                        _bare_prefixes = _BARE_TAG_NAMES
                        _open_match = (
                            tail == ""
                            or _is_dsml_delimiters_only
                            or any(p.startswith(tail) or tail.startswith(p) for p in _legacy_prefixes if tail)
                            or any(p.startswith(tail) or tail.startswith(p) for p in _bare_prefixes if tail)
                            or any(_tail_norm.startswith(p) or p.startswith(_tail_norm)
                                   for p in _dsml_prefixes if _tail_norm)
                        )
                        _close_tail = self.buf[idx:].lstrip("<").lstrip("/").strip().lower()
                        _close_tail_norm = _close_tail.lstrip("\uff5c|_")
                        _close_match = ("/" in self.buf[idx:idx+2] and
                                        (_close_tail == ""
                                         or any(p.startswith(_close_tail) or _close_tail.startswith(p)
                                                for p in _legacy_prefixes if _close_tail)
                                         or any(p.startswith(_close_tail) or _close_tail.startswith(p)
                                                for p in _bare_prefixes if _close_tail)
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
                    # Not JSON inside <tool_call> — but check if it's XML invoke/parameter
                    # tags (hybrid format: <tool_call> wrapping <invoke> instead of JSON).
                    # Models sometimes mix hermes wrapper with legacy XML content.
                    _open_tag = m.group(0)
                    _ahead = self.buf  # original buffer with whitespace intact
                    _close_in_ahead = self._ACTION_CLOSE.search(_ahead)
                    # Check for invoke/parameter tags — including partial prefixes
                    # that may be split across SSE chunks (e.g. '<inv' + 'oke name=...').
                    # If stripped_ahead is a prefix of '<invoke', we must hold the buffer
                    # rather than emitting as text, because the next chunk may complete it.
                    def _is_prefix_of_any(s: str, targets: tuple[str, ...]) -> bool:
                        return any(t.startswith(s) or s.startswith(t) for t in targets)

                    _has_invoke = (
                        self._LEGACY_INVOKE_RE.search(_ahead)
                        or self._DSML_INVOKE_RE.search(_ahead)
                        or self._MIMO_FUNCTION_RE.search(_ahead)
                        or _is_prefix_of_any(stripped_ahead, ('<invoke', '<parameter', '<function'))
                    )
                    if _has_invoke:
                        # Hybrid: <tool_call>/<action> wrapping XML invoke tags.
                        # Route through the legacy/DSML XML extractor instead of leaking.
                        if _close_in_ahead is not None:
                            _inner_xml = _ahead[:_close_in_ahead.start()]
                            _after_close = _ahead[_close_in_ahead.end():]
                            _plog(f"HYBRID_XML_TOOL_CALL: found invoke tags inside {_open_tag}, routing to XML extractor")
                            self.buf = _after_close
                            yield from self._extract_dsml(_inner_xml)
                        else:
                            # No closing tag yet — hold buffer, wait for more stream
                            _plog(f"HYBRID_XML_TOOL_CALL: incomplete, holding buffer")
                            self.buf = _open_tag + _ahead
                            # Live progress for the hybrid Hermes/XML block.
                            yield from self._emit_xml_progress(self.buf)
                            break
                        continue
                    # Truly not a tool call — preserve the ENTIRE sequence as visible text
                    _plog(f"NON_JSON_TOOL_CALL: starts with {repr(stripped_ahead[:30])}, emitting as text")
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

    @classmethod
    def _parse_mimo_params(cls, inner: str) -> dict[str, Any]:
        """Parse MiMo-style <parameter=key>value</parameter> tags."""
        attrs: dict[str, Any] = {}
        for m in cls._MIMO_PARAM_RE.finditer(inner):
            pname, pval = m.group(1).strip(), m.group(2).strip()
            # Auto-detect JSON types (numbers, booleans, arrays, objects)
            try:
                attrs[pname] = json.loads(pval)
            except (json.JSONDecodeError, ValueError):
                attrs[pname] = pval
        return attrs

    def _emit_xml_progress(self, block: str) -> Generator[dict[str, Any], None, None]:
        """Stream live progress for an INCOMPLETE XML tool block.

        The XML path (Hermes / DSML / MiMo) has no ``partial`` mode — without
        this, every XML tool call arrives as one silent lump and only renders
        once the closing tag lands, while the JSON path streams a live card the
        whole way. This mirrors :meth:`_extract_json`'s partial branch: emit
        ``tool_pending`` once for the tool name, then ``tool_progress`` as the
        block grows, so the card builds up live exactly like JSON does.

        Safe on any prefix: it degrades to a byte counter when no parameter
        value is complete enough to preview yet.
        """
        # Tool name — ONLY from a complete, terminated match. A half-streamed
        # name (e.g. `cre` on the way to `create_file`) must not be emitted:
        # it would fire a second tool_pending when the real name lands.
        name = ""
        _m = (
            self._DSML_INVOKE_RE.search(block)
            or self._LEGACY_INVOKE_RE.search(block)
            or self._MIMO_FUNCTION_RE.search(block)
        )
        if _m:
            name = _m.group(1).strip().lower()
        else:
            # Terminated name attribute / function tag — quote or '>' closed.
            _pm = (
                re.search(r'<invoke\s+name="([^"\s>]+)"', block)
                or re.search(r'<function\s*=\s*([^>\s]+)\s*>', block)
            )
            if _pm:
                name = _pm.group(1).strip().lower()
        if not name:
            return  # name still streaming — stay silent, like the JSON path

        if name != self._pending_tag:
            self._pending_tag = name
            self._last_progress = (0, 0)
            yield {"type": "tool_pending", "tag": name, "attrs": {}}

        p_bytes = len(block.encode("utf-8"))
        _last_lines, last_bytes = self._last_progress
        # Throttle: only re-emit once the block has grown meaningfully.
        if last_bytes and p_bytes - last_bytes < 32:
            return
        self._last_progress = (0, p_bytes)

        preview_lines = self._xml_preview_lines(block, p_bytes)

        yield {
            "type": "tool_progress",
            "tag": name,
            "bytes": p_bytes,
            "preview_lines": preview_lines,
        }

    @staticmethod
    def _xml_preview_lines(block: str, p_bytes: int) -> list[str]:
        """Build the card preview for an XML tool block (partial or complete).

        Shared by the streaming progress path and the final snapshot emitted
        from :meth:`_extract_dsml`, so an atomically-arrived block still shows
        the same preview a streamed one would.
        """
        def _trim_partial_close(val: str) -> str:
            """Drop a trailing partial close tag (e.g. `</para`) mid-stream.

            Only trims when the trailing `<...` is a strict PREFIX of the
            closing tag. Legitimate `<` inside content (HTML, code) is kept.
            """
            _i = val.rfind("<")
            if _i >= 0:
                _tail = val[_i:].lower()
                if "</parameter".startswith(_tail):
                    return val[:_i]
            return val

        preview_lines: list[str] = []

        # path / filename → first line of the card
        _pm = re.search(r'<parameter\s+name="(?:path|filename)"\s*>([^<]*)', block)
        if _pm and _pm.group(1).strip():
            preview_lines.append(_pm.group(1).strip()[:200])

        # command → show it verbatim (short; first non-empty line)
        _cm = re.search(r'<parameter\s+name="command"\s*>(.*)$', block, re.DOTALL)
        if _cm:
            _cmd = _cm.group(1)
            _cut = _cmd.find("</parameter")
            if _cut >= 0:
                _cmd = _cmd[:_cut]
            else:
                _cmd = _trim_partial_close(_cmd)
            for _ln in _cmd.splitlines():
                if _ln.strip():
                    preview_lines.append(_ln.strip()[:150])
                    break

        # content / body / lines → trailing lines (what is being written now)
        _bm = re.search(r'<parameter\s+name="(?:content|body|lines)"\s*>(.*)$', block, re.DOTALL)
        if _bm:
            _body = _bm.group(1)
            _cut = _body.find("</parameter")
            if _cut >= 0:
                _body = _body[:_cut]
            else:
                _body = _trim_partial_close(_body)
            _lines = [ln for ln in _body.splitlines() if ln.strip()]
            for _ln in _lines[-3:]:
                preview_lines.append(_ln[:150])

        if not preview_lines:
            _size = f"{p_bytes} B" if p_bytes < 1024 else f"{p_bytes / 1024:.1f} KB"
            preview_lines.append(f"streaming\u2026 {_size}")
        return preview_lines

    def _extract_dsml(self, block: str) -> Generator[dict[str, Any], None, None]:
        """Parse a complete DSML/legacy/MiMo tool_calls block and yield tag_found events.
        
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
        # ALSO try MiMo function/parameter format (<function=name><parameter=key>val</parameter></function>)
        for m in self._MIMO_FUNCTION_RE.finditer(block):
            name = m.group(1).strip()
            params = self._parse_mimo_params(m.group(2))
            content = ""
            for ck in _CONTENT_PARAM_KEYS:
                if ck in params:
                    content = str(params[ck])
                    break
            str_attrs = _stringify_params(params)
            _plog(f"MIMO_XML_TAG_FOUND: {name} | attrs_keys={list(str_attrs.keys())}")
            yield {
                "type": "tag_found",
                "name": name,
                "attrs": str_attrs,
                "content": content,
            }
            found = True
        if found:
            # Final progress snapshot — mirrors the JSON path's pre-execution
            # emit. Matters when the whole block arrived in ONE chunk (no
            # streaming happened): without this the card would pop in with no
            # preview at all.
            _p_bytes = len(block.encode("utf-8"))
            _preview = self._xml_preview_lines(block, _p_bytes)
            _tag = self._pending_tag or "unknown"
            yield {
                "type": "tool_progress",
                "tag": _tag,
                "bytes": _p_bytes,
                "preview_lines": _preview,
            }
            self._pending_tag = None
            self._last_progress = (0, 0)
        else:
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
            # An <render_component> still open at end-of-stream (model stopped
            # early, or the close tag never arrived): render what we have as a
            # FINAL block so the client seals any partial it already drew,
            # instead of flushing the whole thing as dead text.
            _b = _find_bare_open(self.buf)
            if _b is not None and _b[0] == "render_component":
                _bname, _bs, _be = _b
                _had_partial = self._ui_cur_id is not None
                _bid = self._ui_cur_id or self._next_ui_id()
                self._reset_ui_partial()
                if _bs > 0:
                    _before = self.buf[:_bs]
                    if _before.strip():
                        yield {"type": "text", "text": self._strip_orphan_tags(_before)}
                try:
                    from engine.ui_format import parse_ui_block
                    _ui = parse_ui_block(_trim_incomplete_markup(self.buf[_be:]), {})
                except Exception:
                    _ui = None
                if _ui:
                    yield {"type": "ui_block", "payload": _ui, "id": _bid, "partial": False}
                elif _had_partial:
                    yield {"type": "ui_block", "id": _bid, "remove": True}
                self.buf = ""

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
                _flush_content = self._ACTION_CLOSE.sub("", self.buf).strip()
                _plog(f"FLUSH_IN_ACTION: buf_len={len(self.buf)} | first_100={repr(_flush_content[:100])}")
                yield from self._extract_json(_flush_content, partial=False)
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
