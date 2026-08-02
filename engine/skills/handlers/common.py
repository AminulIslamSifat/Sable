
"""Shared constants and helper functions for skill handlers."""

from __future__ import annotations

import html as html_lib
import os
import re
import shutil
import signal
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from engine.skills.events import end_event, output_event

# Re-export for handler convenience
_output_event = output_event
_end_event = end_event

# --- Paths ---
SABLE_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SKILLS_DIR = SABLE_ROOT / "skills"
INSTRUCTION_DIR = SABLE_ROOT / "instruction"
OUTPUT_ROOT = SABLE_ROOT / "output"
NOTES_DIR = OUTPUT_ROOT / "notes"
ASSETS_DIR = OUTPUT_ROOT / "assets"
SESSIONS_DIR = OUTPUT_ROOT / "sessions"
UPLOAD_DIR = SABLE_ROOT / "system" / "uploads"
BACKUP_DIR = SABLE_ROOT / ".sable_backups"
EDITOR_TOOLS = SKILLS_DIR / "code_editor" / "scripts" / "editor_tools.py"

# --- Constants ---
SUDO_PASSWORD = os.environ.get("SABLE_SUDO_PASSWORD", "")
DEFAULT_TIMEOUT = 15
MAX_TIMEOUT = 180
MAX_TEXT_BYTES = 2 * 1024 * 1024
PREVIEW_BYTES = 64 * 1024
RESULT_PREVIEW_CHARS = 20_000

TEXT_EXTENSIONS = {
    ".txt", ".md", ".py", ".js", ".ts", ".jsx", ".tsx", ".json", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".conf", ".css", ".html", ".htm", ".xml", ".csv",
    ".sh", ".bash", ".fish", ".log", ".sql", ".env", ".gitignore", ".dockerfile",
}

_EDITOR_OPS = {"create", "edit", "insert", "view"}
_EDITOR_OUTPUT_CAP = 64 * 1024
_EDITOR_LINE_CAP = 500


# --- Helpers ---

def safe_under(base: Path, raw: str) -> Path:
    """Resolve a path ensuring it stays under base directory."""
    base = base.resolve()
    candidate = (base / raw).resolve()
    if candidate != base and base not in candidate.parents:
        raise ValueError(f"Path escapes target directory: {raw}")
    return candidate


def strip_html(text: str) -> str:
    """Remove HTML tags and unescape entities."""
    return html_lib.unescape(re.sub(r"<[^>]+>", "", text)).strip()


def kill_process_group(proc: subprocess.Popen[str]) -> None:
    """Kill a process and its entire process group."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def make_backup(path: str) -> str | None:
    """Snapshot a file before mutating it so the UI can offer a one-click revert."""
    try:
        src = Path(path)
        if not src.is_file():
            return None
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", src.name)
        backup = BACKUP_DIR / f"{stamp}_{uuid.uuid4().hex[:8]}_{safe}"
        shutil.copy2(src, backup)
        return str(backup)
    except Exception:
        return None


def run_editor(args: list[str], stdin_data: str | None = None, timeout: int = DEFAULT_TIMEOUT) -> tuple[bool, str]:
    """Run editor_tools.py with the given args. Returns (ok, output_text)."""
    cmd = ["python3", str(EDITOR_TOOLS)] + args
    try:
        proc = subprocess.run(
            cmd,
            input=stdin_data,
            capture_output=True,
            text=True,
            timeout=timeout,
            errors="replace",
        )
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        output = stdout + (f"\n{stderr}" if stderr else "")
        return proc.returncode == 0, output
    except subprocess.TimeoutExpired:
        return False, f"editor_tools timed out after {timeout}s"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def parse_editor_command(cmd: str) -> tuple[str, str] | None:
    """Detect if a shell command invokes editor_tools.py and extract (op, path)."""
    if "editor_tools.py" not in cmd:
        return None
    tokens = cmd.split()
    for i, token in enumerate(tokens):
        if not token.endswith("editor_tools.py"):
            continue
        j = i + 1
        if j >= len(tokens):
            return None
        op = tokens[j].strip().strip("'\"")
        if op not in _EDITOR_OPS:
            return None
        j += 1
        value_flags = {"--content-file", "--json-file", "--start", "--end"}
        while j < len(tokens):
            arg = tokens[j].strip().strip("'\"")
            if arg.startswith("--"):
                if arg in value_flags:
                    j += 2
                else:
                    j += 1
                continue
            path = os.path.expandvars(os.path.expanduser(arg))
            return op, path
        return op, ""
    return None


def _diff_line_payload(kind: str, text: str) -> dict[str, str]:
    if len(text) > _EDITOR_LINE_CAP:
        text = text[:_EDITOR_LINE_CAP] + "\u2026"
    return {"t": kind, "text": text}


def build_file_edit_event(
    tag_id: str, op: str, path: str, output: str, backup_path: str | None = None
) -> dict[str, Any] | None:
    """Parse editor output into a structured file_edit SSE event for the diff sidebar."""
    try:
        first, _, rest = output.partition("\n")
        first = first.strip()
        resolved = path
        if not resolved:
            match = re.search(r"'([^']+)'", first)
            resolved = match.group(1) if match else ""
        if resolved:
            resolved = os.path.abspath(os.path.expandvars(os.path.expanduser(resolved)))
        evt: dict[str, Any] = {
            "type": "file_edit",
            "id": tag_id,
            "op": op,
            "path": resolved,
            "name": os.path.basename(resolved) or resolved or "file",
            "added": 0,
            "removed": 0,
            "truncated": False,
            "lines": [],
            "backup_path": backup_path or "",
        }
        lines: list[dict[str, str]] = []

        if op in ("edit", "insert"):
            match = re.search(r"'([^']+)'", first)
            if match:
                header_path = match.group(1)
                evt["path"] = header_path
                evt["name"] = os.path.basename(header_path) or header_path
            for raw_line in rest.splitlines():
                if raw_line.startswith("+"):
                    evt["added"] += 1
                    lines.append(_diff_line_payload("add", raw_line[1:]))
                elif raw_line.startswith("-"):
                    evt["removed"] += 1
                    lines.append(_diff_line_payload("del", raw_line[1:]))
                elif raw_line.startswith("@@"):
                    lines.append(_diff_line_payload("hunk", raw_line))
                elif raw_line.startswith("..."):
                    evt["truncated"] = True
                    lines.append(_diff_line_payload("meta", raw_line))
                elif raw_line.strip():
                    lines.append(_diff_line_payload("ctx", raw_line[1:] if raw_line.startswith(" ") else raw_line))
            if not lines:
                lines.append(_diff_line_payload("meta", first or "No diff returned"))
        else:
            match = re.search(r"Created '([^']+)' \((\d+) bytes, (\d+) lines\)", first)
            if match:
                evt["path"] = match.group(1)
                evt["name"] = os.path.basename(evt["path"]) or evt["path"]
                evt["added"] = int(match.group(3))
            target = evt["path"] or resolved
            try:
                if target:
                    target = os.path.abspath(os.path.expandvars(os.path.expanduser(target)))
                    evt["path"] = target
                    evt["name"] = os.path.basename(target) or target
                    with open(target, "r", encoding="utf-8", errors="replace") as fh:
                        preview = fh.read(_EDITOR_OUTPUT_CAP)
                    preview_lines = preview.splitlines()
                    if len(preview) >= _EDITOR_OUTPUT_CAP:
                        evt["truncated"] = True
                    lines.extend(_diff_line_payload("add", line) for line in preview_lines)
                else:
                    lines.append(_diff_line_payload("meta", first or "Created file"))
            except Exception:
                lines.append(_diff_line_payload("meta", first or "Created file"))

        evt["lines"] = lines
        return evt
    except Exception:
        return None
