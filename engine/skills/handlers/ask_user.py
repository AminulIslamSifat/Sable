
"""Handler for <ask_user> — structured MCQ-style questions.

Yields a skill_output event with the question payload, then a skill_end
with pause=True so the chat loop stops and waits for the user's next
message (which arrives as a normal user turn, not a tool result).
"""

from __future__ import annotations

import json
import time
from typing import Any, Generator

from engine.skills.events import end_event, output_event


def handle_ask_user(
    tag_id: str, name: str, attrs: dict[str, str], content: str
) -> Generator[dict[str, Any], None, None]:
    started = time.time()
    question = attrs.get("question", "").strip()
    raw_options = attrs.get("options", "[]")
    multi = attrs.get("multi", "false").lower() == "true"
    default = attrs.get("default")

    try:
        options = json.loads(raw_options)
        if not isinstance(options, list) or len(options) < 2:
            raise ValueError("options must be a JSON array with at least 2 items")
    except (json.JSONDecodeError, ValueError) as exc:
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
