"""Computer use handler — dispatches to tools/computer_use/scripts/computer_use.py."""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any

from engine.skills.handlers.common import (
    TOOLS_DIR,
    _end_event,
    _output_event,
)

_COMPUTER_USE_SCRIPT = TOOLS_DIR / "computer_use" / "scripts" / "computer_use.py"


def handle_computer_use(
    tag_id: str, name: str, attrs: dict[str, str], content: str
) -> Generator[dict[str, Any], None, None]:
    started = time.time()

    # Build params from attrs (tool calls come as JSON attributes)
    params: dict[str, Any] = {}
    for k, v in attrs.items():
        # Try to parse numeric/bool values
        if v.lower() in ("true", "false"):
            params[k] = v.lower() == "true"
        else:
            try:
                params[k] = int(v)
            except ValueError:
                params[k] = v

    # If content has JSON, merge it (some models put params in body)
    if content.strip().startswith("{"):
        try:
            params.update(json.loads(content))
        except json.JSONDecodeError:
            pass

    action = params.get("action", "")
    if not action:
        yield _output_event(tag_id, "Missing 'action' parameter\n", "stderr")
        yield _end_event(tag_id, name, False, started, error="Missing action")
        return

    if not _COMPUTER_USE_SCRIPT.exists():
        yield _output_event(tag_id, f"Script not found: {_COMPUTER_USE_SCRIPT}\n", "stderr")
        yield _end_event(tag_id, name, False, started, error="computer_use.py not found")
        return

    payload = json.dumps(params)
    yield _output_event(tag_id, f"computer_use({action})\n", "command")

    try:
        timeout = int(attrs.get("timeout", 30))
    except ValueError:
        timeout = 30

    try:
        proc = subprocess.run(
            ["python3", str(_COMPUTER_USE_SCRIPT), payload],
            capture_output=True,
            text=True,
            timeout=timeout,
            errors="replace",
        )
        stdout = proc.stdout.strip()
        stderr = proc.stderr.strip()

        if proc.returncode != 0:
            error_msg = stderr or stdout or f"exit code {proc.returncode}"
            yield _output_event(tag_id, f"{error_msg}\n", "stderr")
            yield _end_event(tag_id, name, False, started, error=error_msg)
            return

        # Parse result and check for backend-level errors
        try:
            result = json.loads(stdout) if stdout else {}
        except json.JSONDecodeError:
            result = {"raw": stdout}

        if "error" in result:
            yield _output_event(tag_id, f"{result['error']}\n", "stderr")
            yield _end_event(tag_id, name, False, started, result, error=result["error"])
            return

        # Format output for display
        output_text = json.dumps(result, indent=2, ensure_ascii=False)
        # Truncate very long outputs (e.g. screen_parse with many elements)
        if len(output_text) > 8000:
            output_text = output_text[:8000] + "\n... (truncated)"

        yield _output_event(tag_id, output_text + "\n")
        yield _end_event(tag_id, name, True, started, result)

    except subprocess.TimeoutExpired:
        yield _output_event(tag_id, f"Timed out after {timeout}s\n", "stderr")
        yield _end_event(tag_id, name, False, started, error=f"Timeout after {timeout}s")
    except Exception as e:
        yield _output_event(tag_id, f"{type(e).__name__}: {e}\n", "stderr")
        yield _end_event(tag_id, name, False, started, error=str(e))
