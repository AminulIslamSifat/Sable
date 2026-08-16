"""Memory Manager handler: processes memory tag by calling the CLI script."""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any

from engine.skills.handlers.common import _end_event, _output_event

_SCRIPT = str(Path(__file__).resolve().parent.parent.parent.parent / "tools" / "memory_manager" / "memory_manager.py")


def _run_memory(cli_args: list[str]) -> tuple[bool, str]:
    """Run memory_manager.py with args, return (ok, output)."""
    cmd = ["python3", _SCRIPT] + cli_args
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        return False, "Error: memory_manager timed out after 15s"
    except Exception as e:
        return False, f"Error: {e}"

    output = proc.stdout.strip()
    if proc.returncode != 0:
        return False, proc.stderr.strip() or output or "memory_manager failed"
    return True, output


def handle_memory(
    tag_id: str, name: str, attrs: dict[str, str], content: str
) -> Generator[dict[str, Any], None, None]:
    started = time.time()
    action = attrs.get("action", "")

    yield _output_event(tag_id, f"$ memory {action}\n", "command")

    cli: list[str] = [action]

    if attrs.get("category"):
        cli += ["--category", attrs["category"]]

    if attrs.get("key"):
        cli += ["--key", attrs["key"]]

    if attrs.get("value"):
        cli += ["--value", attrs["value"]]

    if attrs.get("query"):
        cli += ["--query", attrs["query"]]

    if attrs.get("target_key"):
        cli += ["--target-key", attrs["target_key"]]

    if attrs.get("trigger"):
        cli += ["--trigger", attrs["trigger"]]

    if attrs.get("top_k"):
        cli += ["--top-k", attrs["top_k"]]

    # Array params: tags, triggers, keywords, source_keys
    for param_name, cli_flag in [("tags", "--tags"), ("triggers", "--triggers"),
                                  ("keywords", "--keywords"), ("source_keys", "--source-keys")]:
        raw = attrs.get(param_name, "")
        if raw:
            try:
                items = json.loads(raw) if isinstance(raw, str) else raw
                if isinstance(items, list):
                    cli += [cli_flag] + [str(i) for i in items]
            except (json.JSONDecodeError, TypeError):
                # Treat as single value
                cli += [cli_flag, str(raw)]

    ok, out = _run_memory(cli)

    yield _output_event(tag_id, out + "\n")
    yield _end_event(tag_id, name, ok, started, {"action": action})
