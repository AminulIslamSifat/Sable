"""Guard against parsing tool call tags inside code blocks or inline backticks.

Prevents the streaming parser from interpreting literal <tool_call> text that
appears in code examples, markdown tables, or inline code spans as real tool calls.
"""
from __future__ import annotations

import re

_CODE_FENCE_RE = re.compile(r"```[^\n]*\n.*?```", re.DOTALL)
_BACKSLASH = chr(92)  # avoid literal backslash in source to prevent parser leaks
_BACKTICK = chr(96)


def is_inside_code_context(text: str, tag_pos: int) -> bool:
    """Return True if *tag_pos* falls inside a fenced code block or inline backtick span."""
    # Check fenced code blocks first (most common leak source)
    for m in _CODE_FENCE_RE.finditer(text):
        if m.start() <= tag_pos < m.end():
            return True

    # Check inline backtick spans by counting backticks before tag_pos.
    # Odd count means we are inside an inline code span.
    preceding = text[:tag_pos]
    backtick_count = 0
    i = len(preceding) - 1
    while i >= 0:
        ch = preceding[i]
        if ch == _BACKTICK:
            backtick_count += 1
        elif ch == _BACKSLASH and i > 0 and preceding[i - 1] != _BACKSLASH:
            pass  # escaped backtick, skip
        i -= 1
    return backtick_count % 2 == 1
