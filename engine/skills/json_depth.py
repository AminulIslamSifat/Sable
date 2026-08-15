"""JSON structural completeness checker for stream parser.

Used by SkillParser to verify that content between opening and closing
tool_call tags contains complete JSON before accepting the boundary.
Prevents false splits when tag-like patterns appear inside incomplete JSON.
"""
from __future__ import annotations

_OPEN = frozenset((chr(123), chr(91)))   # { [
_CLOSE = frozenset((chr(125), chr(93)))  # } ]
_QUOTE = chr(34)                          # "
_ESCAPE = chr(92)                         # \\


def json_structurally_complete(raw: str) -> bool:
    """Check if raw text contains structurally complete JSON.

    Tracks brace/bracket depth while respecting string literals and escapes.
    Returns True only when depth returns to zero after entering a structure.
    """
    stripped = raw.strip()
    if not stripped or stripped[0] not in _OPEN:
        return False

    depth = 0
    in_string = False
    escape_next = False

    for ch in stripped:
        if escape_next:
            escape_next = False
            continue
        if ch == _ESCAPE and in_string:
            escape_next = True
            continue
        if ch == _QUOTE:
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch in _OPEN:
            depth += 1
        elif ch in _CLOSE:
            depth -= 1
            if depth == 0:
                return True
    return False
