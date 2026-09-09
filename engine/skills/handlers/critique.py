"""Critique tool — triggers a normal chat turn rendered inside a critique card.

No separate LLM pipeline. No custom system prompt. No manual tool loop.
Constructs the critique message and emits a `critique_trigger` event.
The frontend fires a standard POST /api/chat with the same model, thinking_mode,
provider, parent_id, and session as the current chat. The response streams
into the critique card via the normal SSE pipeline.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Generator

logger = logging.getLogger("sable")


def handle_critique(
    tag_id: str,
    name: str,
    attrs: dict[str, str],
    content: str,
) -> Generator[dict[str, Any], None, None]:
    """Emit critique_start + critique_trigger for the frontend to run a normal chat turn."""
    from engine.skills.events import end_event as _end_event, output_event as _output_event
    from engine.platform_paths import home_dir

    started = time.time()

    try:
        args = json.loads(content) if isinstance(content, str) else content
    except (json.JSONDecodeError, TypeError):
        yield _output_event(tag_id, f"Invalid JSON arguments: {str(content)[:200]}")
        yield _end_event(tag_id, name, False, started, error=f"Invalid JSON: {str(content)[:200]}")
        return

    context = args.get("context", "")
    criteria = args.get("criteria", "")
    focus = args.get("focus", "general")
    cwd = args.get("cwd", "") or home_dir()

    if not context or not criteria:
        yield _output_event(tag_id, "Both 'context' and 'criteria' are required.")
        yield _end_event(tag_id, name, False, started, error="Missing context or criteria")
        return

    # Build the user message — override persona so the model acts as a focused
    # code reviewer instead of trying to be conversational/Maria during critique.
    # ponytail: without this, the model gets Maria's system prompt but a clinical
    # evaluation request, gets confused, outputs one sentence, and stops.
    message = (
        f"[SYSTEM OVERRIDE: You are a strict, thorough code reviewer. Drop all "
        f"persona, roleplay, and conversational behavior. Your ONLY job is to "
        f"evaluate the context below against the criteria. Use tools aggressively "
        f"(read files, search code, run commands) to inspect the actual codebase "
        f"before writing your report. Do NOT respond conversationally. Do NOT "
        f"stop after one sentence. Do the actual work.]\n\n"
        f"## Context to Evaluate\n{context}\n\n"
        f"## Evaluation Criteria\n{criteria}\n\n"
        f"## Focus Area\n{focus}\n\n"
        f"Use tools NOW to inspect the codebase. Read the relevant files. "
        f"Search for patterns. Then produce your structured critique report.\n\n"
        f"### Report Format\n"
        f"**Mark:** [X/10]\n"
        f"**Why:** (Detailed explanation)\n"
        f"**Suggestions:** (Numbered list of actionable improvements)"
    )
    if cwd:
        message += f"\n\nWorking directory: {cwd}"

    # Resolve a separate browser profile for critique so it doesn't share
    # the main chat's browser data (prevents recursive tool access, WAF
    # contention, and auto-switch collisions).
    browser_data_dir: str | None = None
    try:
        from engine.agents.registry import get_next_account
        from engine.config import _SYSTEM as _SYS
        assigned = get_next_account("critique")
        if assigned:
            _path = _SYS / assigned
            if _path.is_dir():
                browser_data_dir = str(_path)
    except Exception as _bdd_exc:
        logger.warning("Failed to resolve scoped browser for critique: %s", _bdd_exc)

    # Frontend creates the critique card
    yield {
        "type": "critique_start",
        "id": tag_id,
        "name": name,
        "context": context[:500],
        "criteria": criteria[:500],
        "focus": focus,
    }

    # Frontend fires POST /api/chat with this message + browser_data_dir,
    # streams response into the card using an isolated browser profile.
    trigger: dict[str, Any] = {
        "type": "critique_trigger",
        "id": tag_id,
        "message": message,
    }
    if browser_data_dir:
        trigger["browser_data_dir"] = browser_data_dir
    yield trigger

    # skill_end so the main loop knows the tool call completed
    yield _end_event(tag_id, name, True, started, result={"triggered": True})
