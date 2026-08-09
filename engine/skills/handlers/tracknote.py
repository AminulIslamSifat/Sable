"""TrackNote handler: processes tracknote tag by calling the CLI script."""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any

from engine.skills.handlers.common import _end_event, _output_event

_SCRIPT = str(Path(__file__).resolve().parent.parent.parent.parent / "skills" / "tracknote_manager" / "tracknote.py")


def _run_tracknote(section: str, command: str, cli_args: list[str]) -> tuple[bool, str]:
    """Run tracknote.py <section> <command> [args], return (ok, output)."""
    cmd = ["python3", _SCRIPT, section, command] + cli_args
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        return False, "Error: tracknote timed out after 15s"
    except Exception as e:
        return False, f"Error: {e}"

    output = proc.stdout.strip()
    if proc.returncode != 0:
        return False, proc.stderr.strip() or output or "tracknote failed"
    return True, output


def handle_tracknote(
    tag_id: str, name: str, attrs: dict[str, str], content: str
) -> Generator[dict[str, Any], None, None]:
    started = time.time()
    action = attrs.get("action", "")

    yield _output_event(tag_id, f"$ tracknote {action}\n", "command")

    if action == "add_note":
        title = attrs.get("title", "")
        note_type = attrs.get("type", "note")
        cli = ["--title", title, "--type", note_type]
        if attrs.get("content"):
            cli += ["--content", attrs["content"]]
        ok, out = _run_tracknote("notes", "add", cli)

    elif action == "add_todo":
        title = attrs.get("title", "")
        items = attrs.get("items", "[]")
        cli = ["--title", title, "--type", "checklist", "--items", items]
        ok, out = _run_tracknote("notes", "add", cli)

    elif action == "add_schedule":
        title = attrs.get("title", "")
        cli = ["--title", title]
        if attrs.get("type"):
            cli += ["--type", attrs["type"]]
        if attrs.get("time"):
            cli += ["--time", attrs["time"]]
        if attrs.get("day_of_week"):
            cli += ["--day", attrs["day_of_week"]]
        if attrs.get("start_date"):
            cli += ["--start", attrs["start_date"]]
        if attrs.get("description"):
            cli += ["--desc", attrs["description"]]
        ok, out = _run_tracknote("schedules", "add", cli)

    elif action == "toggle_item":
        note_id = attrs.get("note_id", "")
        index = attrs.get("index", "0")
        ok, out = _run_tracknote("notes", "toggle", [note_id, index])

    elif action == "add_op":
        name = attrs.get("name", "")
        prompt = attrs.get("prompt", "")
        cli = ["--name", name, "--prompt", prompt]
        if attrs.get("model"):
            cli += ["--model", attrs["model"]]
        if attrs.get("type"):
            cli += ["--type", attrs["type"]]
        if attrs.get("time"):
            cli += ["--time", attrs["time"]]
        if attrs.get("day_of_week"):
            cli += ["--day", attrs["day_of_week"]]
        if attrs.get("cron"):
            cli += ["--cron", attrs["cron"]]
        if attrs.get("policy"):
            cli += ["--policy", attrs["policy"]]
        if attrs.get("enabled") is not None:
            cli += ["--enabled", attrs["enabled"]]
        ok, out = _run_tracknote("ops", "add", cli)

    elif action == "toggle_op":
        op_id = attrs.get("op_id", "")
        ok, out = _run_tracknote("ops", "toggle", [op_id])

    elif action == "delete":
        kind = attrs.get("kind", "notes")
        item_id = attrs.get("id", "")
        section = {"schedules": "schedules", "ops": "ops"}.get(kind, "notes")
        ok, out = _run_tracknote(section, "remove", [item_id])

    else:
        ok, out = False, f"Unknown tracknote action: {action!r}. Valid: add_note, add_todo, add_schedule, add_op, toggle_item, toggle_op, delete"

    yield _output_event(tag_id, out + "\n")
    yield _end_event(tag_id, name, ok, started, {"action": action})
