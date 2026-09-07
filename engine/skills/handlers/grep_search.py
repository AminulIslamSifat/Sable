
"""Grep search handlers: grep, glob, list_dir."""

from __future__ import annotations

import time
from collections.abc import Generator
from typing import Any

from engine.skills.handlers.common import _end_event, _output_event

# ponytail: direct import instead of subprocess — grep still uses rg binary internally,
# but we skip the Python-in-Python overhead for all three commands
from tools.grep_search.scripts.grep_search import cmd_grep, cmd_glob, cmd_list_dir


def handle_grep(
    tag_id: str, name: str, attrs: dict[str, str], content: str
) -> Generator[dict[str, Any], None, None]:
    started = time.time()
    # Attrs can come from XML attributes or from content as fallback
    args = dict(attrs)
    if not args.get("pattern") and content.strip():
        args["pattern"] = content.strip()

    yield _output_event(tag_id, f"$ grep '{args.get('pattern', '')}' in {args.get('path', '$PROJECT_ROOT')}\n", "command")
    try:
        lines = cmd_grep(args)
    except Exception as exc:
        lines = [f"Error: {exc}"]
    for line in lines:
        yield _output_event(tag_id, line + "\n")
    ok = not any(l.startswith("Error:") for l in lines)
    yield _end_event(tag_id, name, ok, started, {"matches": len(lines)})


def handle_glob(
    tag_id: str, name: str, attrs: dict[str, str], content: str
) -> Generator[dict[str, Any], None, None]:
    started = time.time()
    args = dict(attrs)
    if not args.get("pattern") and content.strip():
        args["pattern"] = content.strip()

    yield _output_event(tag_id, f"$ glob '{args.get('pattern', '')}' in {args.get('path', '$PROJECT_ROOT')}\n", "command")
    try:
        lines = cmd_glob(args)
    except Exception as exc:
        lines = [f"Error: {exc}"]
    for line in lines:
        yield _output_event(tag_id, line + "\n")
    ok = not any(l.startswith("Error:") for l in lines)
    yield _end_event(tag_id, name, ok, started, {"files": len(lines)})


def handle_list_dir(
    tag_id: str, name: str, attrs: dict[str, str], content: str
) -> Generator[dict[str, Any], None, None]:
    started = time.time()
    args = dict(attrs)
    if not args.get("path") and content.strip():
        args["path"] = content.strip()

    yield _output_event(tag_id, f"$ ls {args.get('path', '$PROJECT_ROOT')}\n", "command")
    try:
        lines = cmd_list_dir(args)
    except Exception as exc:
        lines = [f"Error: {exc}"]
    for line in lines:
        yield _output_event(tag_id, line + "\n")
    ok = not any(l.startswith("Error:") for l in lines)
    yield _end_event(tag_id, name, ok, started, {"entries": len(lines)})
