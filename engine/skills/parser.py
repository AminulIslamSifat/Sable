
"""Stream parser for agentic tags within action blocks.

Extracts complete tags from streamed LLM output. Tags are only parsed
inside action open/close boundaries. Yields structured events:
- {"type": "text", "text": ...} for prose
- {"type": "tool_pending", "tag": ..., "attrs": ...} for activity indicators
- {"type": "tool_progress", "tag": ..., "lines": ..., "bytes": ...} for live progress
- {"type": "tag_found", "name": ..., "attrs": ..., "content": ...} for complete tags
"""

from __future__ import annotations

import re
from typing import Any, Generator

# All recognized tag names — built from registry at engine init,
# but hardcoded here as fallback for standalone use.
KNOWN_TAGS = (
    "execute_command",
    "execute_background_command",
    "get_file",
    "read_file",
    "search-online",
    "search_online",
    "check_command",
    "openweb",
    "create_note",
    "save_svg",
    "view_file",
    "edit_file",
    "create_file",
    "insert_file",
)

TAG_ALTERNATION = "|".join(re.escape(tag) for tag in KNOWN_TAGS)


def parse_attrs(raw: str) -> dict[str, str]:
    """Parse XML-style attributes from a tag's attribute string."""
    raw = raw.strip()
    if raw.endswith("/"):
        raw = raw[:-1]
    attrs: dict[str, str] = {}
    for match in re.finditer(r'([\w-]+)\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s>]+))', raw):
        key = match.group(1).lower()
        value = match.group(2)
        if value is None:
            value = match.group(3)
        if value is None:
            value = match.group(4)
        attrs[key] = value or ""
    return attrs


class SkillParser:
    """Extracts complete agentic tags from streamed answer text.

    Tags are only parsed when they appear inside an action block.
    Text outside action blocks is emitted as plain prose.
    Complete tags are yielded as {"type": "tag_found", ...} events
    for the engine to dispatch through the middleware pipeline.
    """

    _ACTION_OPEN = re.compile(r"<\s*action\s*>", re.I)
    _ACTION_CLOSE = re.compile(r"<\s*/\s*action\s*>", re.I)

    def __init__(self, known_tags: tuple[str, ...] | None = None) -> None:
        tags = known_tags or KNOWN_TAGS
        alternation = "|".join(re.escape(tag) for tag in tags)
        self._known_tags = tags
        self.buf = ""
        self.open_re = re.compile(r"<\s*(" + alternation + r")\b([^>]*)>", re.I)
        self._in_action = False
        self._pending_tag: str | None = None
        self._last_progress: tuple[int, int] = (0, 0)

    def feed(self, text: str) -> Generator[dict[str, Any], None, None]:
        """Feed a chunk of streamed text. Yields events as tags/prose are resolved."""
        self.buf += text
        while True:
            if not self._in_action:
                m = self._ACTION_OPEN.search(self.buf)
                if m is None:
                    # No action open in buffer — flush as prose (hold trailing partial)
                    idx = self.buf.rfind("<")
                    if idx >= 0 and ">" not in self.buf[idx:]:
                        tail = self.buf[idx:].lstrip("<").strip().lower()
                        if tail == "" or "action".startswith(tail):
                            if idx > 0:
                                yield {"type": "text", "text": self.buf[:idx]}
                            self.buf = self.buf[idx:]
                            break
                    if self.buf:
                        yield {"type": "text", "text": self.buf}
                        self.buf = ""
                    break
                # Found action open — emit prose before it, enter action mode
                before = self.buf[:m.start()]
                if before:
                    yield {"type": "text", "text": before}
                self.buf = self.buf[m.end():]
                self._in_action = True

            # Check for action close boundary
            close_m = self._ACTION_CLOSE.search(self.buf)
            if close_m is not None:
                after = self.buf[close_m.end():]
                self.buf = self.buf[:close_m.start()]
                self._in_action = False
                yield from self._extract_loop()
                self.buf = after
                continue

            # Still inside action, no close yet — extract what we can
            yield from self._extract_loop()
            break

    def _extract_loop(self) -> Generator[dict[str, Any], None, None]:
        """Find complete tags in buffer, yield tag_found events."""
        while True:
            found = self._find_complete()
            if found:
                start, end, name, attrs_raw, content = found
                before = self.buf[:start]
                if before:
                    yield {"type": "text", "text": before}
                self.buf = self.buf[end:]
                self._pending_tag = None
                self._last_progress = (0, 0)
                # Yield structured tag event for engine dispatch
                yield {
                    "type": "tag_found",
                    "name": name,
                    "attrs": parse_attrs(attrs_raw),
                    "content": content,
                }
                continue

            hold = self._hold_start()
            if hold is None:
                if self.buf and not self._in_action:
                    yield {"type": "text", "text": self.buf}
                    self.buf = ""
                break

            if hold > 0:
                yield {"type": "text", "text": self.buf[:hold]}
                self.buf = self.buf[hold:]

            # Emit tool_pending for frontend activity card
            pending_match = self.open_re.search(self.buf)
            if pending_match:
                tag_name = pending_match.group(1).lower()
                if tag_name != self._pending_tag:
                    self._pending_tag = tag_name
                    self._last_progress = (0, 0)
                    attrs = parse_attrs(pending_match.group(2) or "")
                    yield {"type": "tool_pending", "tag": tag_name, "attrs": attrs}
                # Stream live progress
                partial = self.buf[pending_match.end():]
                p_lines = partial.count("\n") + (1 if partial else 0)
                p_bytes = len(partial.encode("utf-8"))
                last_lines, last_bytes = self._last_progress
                if p_lines != last_lines or p_bytes - last_bytes >= 96:
                    self._last_progress = (p_lines, p_bytes)
                    yield {"type": "tool_progress", "tag": tag_name, "lines": p_lines, "bytes": p_bytes}
            break

    def flush(self) -> Generator[dict[str, Any], None, None]:
        """Flush remaining buffer as text. Called at end of stream."""
        if self.buf:
            yield {"type": "text", "text": self.buf}
            self.buf = ""
        self._in_action = False

    def _find_complete(self) -> tuple[int, int, str, str, str] | None:
        """Find the first complete tag (open + close) in buffer."""
        unclosed: list[int] = []

        for match in self.open_re.finditer(self.buf):
            if any(match.start() > pos for pos in unclosed):
                continue

            name = match.group(1).lower()
            attrs = match.group(2) or ""
            stripped_attrs = attrs.rstrip()
            if stripped_attrs.endswith("/") or stripped_attrs == "/":
                return match.start(), match.end(), name, attrs, ""
            close_re = re.compile(r"<\s*/\s*" + re.escape(name) + r"\s*>", re.I)
            close_match = close_re.search(self.buf, match.end())
            if close_match:
                return (
                    match.start(),
                    close_match.end(),
                    name,
                    attrs,
                    self.buf[match.end():close_match.start()],
                )
            unclosed.append(match.start())
        return None

    def _hold_start(self) -> int | None:
        """Find position to hold back (partial tag start)."""
        match = self.open_re.search(self.buf)
        if match:
            return match.start()

        idx = self.buf.rfind("<")
        if idx >= 0:
            tail = self.buf[idx:]
            if ">" not in tail:
                partial = tail.lstrip("<").strip().lower()
                if partial == "" or any(tag.startswith(partial) or partial.startswith(tag) for tag in self._known_tags):
                    return idx
        return None
