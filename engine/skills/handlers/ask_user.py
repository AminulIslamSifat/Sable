
"""Handler for <ask_user> — structured MCQ-style questions.

Yields a skill_output event with the question payload, then a skill_end
with pause=True so the chat loop stops and waits for the user's next
message (which arrives as a normal user turn, not a tool result).
"""

from __future__ import annotations

import ast
import json
import re
import time
from typing import Any, Generator

from engine.skills.events import end_event, output_event


def _parse_options(raw: str) -> list[str]:
    """Resiliently parse options from stringified attrs.

    Models frequently mangle the options array during JSON transport:
    - Double-stringification: '[A, B]' instead of '["A", "B"]'
    - Single quotes: "['A', 'B']" instead of '["A", "B"]'
    - Python repr: "['A', 'B']" (ast.literal_eval handles this)
    - Missing outer brackets: '"A", "B"' instead of '["A", "B"]'

    Tries multiple strategies in order of reliability.
    """
    raw = raw.strip()
    if not raw:
        raise ValueError("options is empty")

    # Strategy 1: Direct JSON parse (happy path)
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(x) for x in parsed]
    except (json.JSONDecodeError, TypeError):
        pass

    # Strategy 2: Python literal eval (handles single-quoted lists)
    try:
        parsed = ast.literal_eval(raw)
        if isinstance(parsed, (list, tuple)):
            return [str(x) for x in parsed]
    except (ValueError, SyntaxError):
        pass

    # Strategy 3: Fix unquoted items like [A, B, C] -> ["A", "B", "C"]
    fixed = raw
    if fixed.startswith("[") and fixed.endswith("]"):
        inner = fixed[1:-1].strip()
        if inner:
            items = []
            for part in inner.split(","):
                part = part.strip().strip("\"'")
                if part:
                    items.append(part)
            if len(items) >= 2:
                return items

    # Strategy 4: Extract quoted strings from anywhere in the raw text
    quoted = re.findall(r'["\x27]([^"\x27]+)["\x27]', raw)
    if len(quoted) >= 2:
        return quoted

    raise ValueError(f"Cannot parse options from: {raw[:100]}")


def handle_ask_user(
    tag_id: str, name: str, attrs: dict[str, str], content: str
) -> Generator[dict[str, Any], None, None]:
    started = time.time()
    question = attrs.get("question", "").strip()
    raw_options = attrs.get("options", "[]")
    multi = attrs.get("multi", "false").lower() == "true"
    default = attrs.get("default")

    try:
        options = _parse_options(raw_options)
        if len(options) < 2:
            raise ValueError("options must have at least 2 items")
    except ValueError as exc:
        err = output_event(tag_id, f"ask_user error: {exc}")
        err["name"] = name
        yield err
        yield end_event(tag_id, name, ok=False, started=started, error=str(exc))
        return

    payload = {
        "question": question,
        "options": options,
        "multi": multi,
    }
    if default is not None:
        try:
            payload["default"] = int(default)
        except ValueError:
            pass

    out = output_event(tag_id, json.dumps(payload, ensure_ascii=False))
    out["name"] = name
    yield out
    yield end_event(tag_id, name, ok=True, started=started, result={"pause": True, "question": question})
