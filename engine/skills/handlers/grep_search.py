
"""Grep search handlers: grep, glob, list_dir."""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any

from engine.skills.handlers.common import _end_event, _output_event

_SCRIPT = str(Path(__file__).resolve().parent.parent.parent / "skills" / "grep_search" / "scripts" / "grep_search.py")


def _run_script(command: str, args: dict[str, str]) -> list[str]:
    """Run grep_search.py with JSON stdin, return result lines or error."""
    payload = json.dumps({"command": command, "args": args})
    try:
        proc = subprocess.run(
            ["python3", _SCRIPT],
            input=payload,
            capture_output=True,
            text=True,
            timeout=35,
        )
    except subprocess.TimeoutExpired:
        return [f"Error: {command} timed out after 35s"]
    except Exception as e:
        return [f"Error: {e}"]

    if proc.returncode != 0 and not proc.stdout.strip():
        return [f"Error: {proc.stderr.strip() or 'script failed'}"]

    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return [proc.stdout[:2000] if proc.stdout else f"Error: invalid output from script"]

    if "error" in result:
        return [f"Error: {result['error']}"]
    return result.get("lines", [])


def handle_grep(
    tag_id: str, name: str, attrs: dict[str, str], content: str
) -> Generator[dict[str, Any], None, None]:
    started = time.time()
    # Attrs can come from XML attributes or from content as fallback
    args = dict(attrs)
    if not args.get("pattern") and content.strip():
        args["pattern"] = content.strip()

    yield _output_event(tag_id, f"$ grep '{args.get('pattern', '')}' in {args.get('path', '$PROJECT_ROOT')}\n", "command")
    lines = _run_script("grep", args)
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
    lines = _run_script("glob", args)
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
    lines = _run_script("list_dir", args)
    for line in lines:
        yield _output_event(tag_id, line + "\n")
    ok = not any(l.startswith("Error:") for l in lines)
    yield _end_event(tag_id, name, ok, started, {"entries": len(lines)})
