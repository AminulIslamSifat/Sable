"""Local skill handlers for Sable agentic tags."""

from __future__ import annotations

import base64
import html as html_lib
import json
import mimetypes
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from collections.abc import Generator
from pathlib import Path
from typing import Any
import httpx

SABLE_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = SABLE_ROOT / "skills"
INSTRUCTION_DIR = SABLE_ROOT / "instruction"
OUTPUT_ROOT = Path("/home/sifat/hdd/Conversation")
NOTES_DIR = OUTPUT_ROOT / "notes"
ASSETS_DIR = OUTPUT_ROOT / "assets"
SESSIONS_DIR = OUTPUT_ROOT / "sessions"
UPLOAD_DIR = SABLE_ROOT / "uploads"
BACKUP_DIR = SABLE_ROOT / ".sable_backups"
EDITOR_TOOLS = SKILLS_DIR / "core" / "code_editor" / "scripts" / "editor_tools.py"
SUDO_PASSWORD = "sifat"
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

BG_JOBS: dict[int, dict[str, Any]] = {}

KNOWN_TAGS = (
    "execute_command",
    "execute_background_command",
    "get_file",
    "read_file",
    "search-online",
    "search_online",
    "check_command",
    "openweb",
    "create_note",
    "save_svg",
    # Native editor tags (code_editor skill)
    "view_file",
    "edit_file",
    "create_file",
    "insert_file",
)

SKILL_REGISTRY = [
    {
        "name": "Code Editor",
        "key": "code_editor",
        "trigger": "Writing to disk: create, edit, insert, or view files with precise line-numbered operations.",
        "instruction": "skills/core/code_editor/instruction.md",
        "tags": ["view_file", "edit_file", "create_file", "insert_file", "execute_command"],
    },
    {
        "name": "SVG Creator",
        "key": "svg_creator",
        "trigger": "Data structure visualizations, node/edge diagrams, algorithm state illustrations.",
        "instruction": "skills/visuals/svg_creator/instruction.md",
        "tags": ["save_svg"],
    },
    {
        "name": "Graph Master",
        "key": "graph_master",
        "trigger": "Mathematical function plots and labeled Cartesian/polar coordinate graphs.",
        "instruction": "skills/visuals/graph_master/instruction.md",
        "tags": ["save_svg", "execute_command"],
    },
    {
        "name": "Math Solver",
        "key": "math_solver",
        "trigger": "Symbolic calculus, step-by-step equation solving, derivation verification.",
        "instruction": "skills/visuals/math_solver/instruction.md",
        "tags": [],
    },
    {
        "name": "Simulacra Engine",
        "key": "simulacra_engine",
        "trigger": "Dynamic, animated, interactive visualizations of physical or mathematical systems.",
        "instruction": "skills/visuals/simulacra_engine/instruction.md",
        "tags": ["create_note", "execute_command"],
    },
    {
        "name": "Proof Verifier",
        "key": "proof_verifier",
        "trigger": "Explicit verification of handwritten math or derivation images.",
        "instruction": "skills/visuals/proof_verifier/instruction.md",
        "tags": ["get_file"],
    },
    {
        "name": "Frontend Design",
        "key": "frontend_design",
        "trigger": "Production-grade UI, web components, high-fidelity layouts.",
        "instruction": "skills/visuals/frontend_design/instruction.md",
        "tags": ["create_note", "execute_command"],
    },
    {
        "name": "Study Suite",
        "key": "study_suite",
        "trigger": "Flashcards, Anki decks, practice problems, mock exams, cheat sheets, formula sheets.",
        "instruction": "skills/study/study_suite/instruction.md",
        "tags": ["create_note", "execute_command"],
    },
    {
        "name": "Memory Sync",
        "key": "memory_sync",
        "trigger": "Diary logging or full persona/memory synchronization.",
        "instruction": "skills/core/memory_sync/instruction.md",
        "tags": ["create_note", "execute_command"],
    },
    {
        "name": "OpenWeb",
        "key": "openweb",
        "trigger": "Fetching structured data from a specific site or target URL.",
        "instruction": "skills/data/openweb/instruction.md",
        "tags": ["openweb"],
    },
    {
        "name": "Online Search",
        "key": "online_search",
        "trigger": "General web searches for quick facts, coding questions, or current events.",
        "instruction": "instruction/skills.md",
        "tags": ["search-online"],
    },
    {
        "name": "File Uploader",
        "key": "file_uploader",
        "trigger": "Loading PDFs, images, Office files, or other non-text files into context.",
        "instruction": "instruction/skills.md",
        "tags": ["get_file", "read_file"],
    },
    {
        "name": "Document Skills",
        "key": "document_skills",
        "trigger": "Creating, editing, or analyzing DOCX, PDF, PPTX, XLSX documents.",
        "instruction": "skills/data/document_skills/instruction.md",
        "tags": ["execute_command", "get_file"],
    },
    {
        "name": "Video Downloader",
        "key": "video_downloader",
        "trigger": "Downloading videos or extracting audio from media platforms.",
        "instruction": "skills/data/youtube_downloader/instruction.md",
        "tags": ["execute_command", "execute_background_command", "check_command"],
    },
    {
        "name": "File Organizer",
        "key": "file_organizer",
        "trigger": "Cleaning up directories, finding duplicates, restructuring workspace/HDD.",
        "instruction": "skills/core/file_organizer/instruction.md",
        "tags": ["execute_command"],
    },
    {
        "name": "Phone Control",
        "key": "phone_control",
        "trigger": "Controlling or automating an Android phone through ADB.",
        "instruction": "skills/core/phone_control/instruction.md",
        "tags": ["execute_command"],
    },
    {
        "name": "System Repair",
        "key": "system_repair",
        "trigger": "Arch Linux or Hyprland repair, logs, pacman/keyring errors, UI glitches.",
        "instruction": "skills/core/system_repair/instruction.md",
        "tags": ["execute_command"],
    },
    {
        "name": "Background Command Execution",
        "key": "background_command",
        "trigger": "Long-running processes, dev servers, builds, tests, downloads, and process monitoring.",
        "instruction": "instruction/skills.md",
        "tags": ["execute_background_command", "check_command"],
    },
]


def list_skills() -> list[dict[str, Any]]:
    return SKILL_REGISTRY


def _output_event(tag_id: str, text: str, stream: str = "stdout") -> dict[str, Any]:
    return {"type": "skill_output", "id": tag_id, "text": text, "stream": stream}


def _end_event(
    tag_id: str,
    name: str,
    ok: bool,
    started: float,
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "type": "skill_end",
        "id": tag_id,
        "name": name,
        "ok": ok,
        "duration_ms": int((time.time() - started) * 1000),
        "result": result or {},
    }
    if error:
        event["error"] = error
    return event


def _safe_under(base: Path, raw: str) -> Path:
    base = base.resolve()
    candidate = (base / raw).resolve()
    if candidate != base and base not in candidate.parents:
        raise ValueError(f"Path escapes target directory: {raw}")
    return candidate


def _strip_html(text: str) -> str:
    return html_lib.unescape(re.sub(r"<[^>]+>", "", text)).strip()


def _kill_process_group(proc: subprocess.Popen[str]) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


_EDITOR_OPS = {"create", "edit", "insert", "view"}
_EDITOR_OUTPUT_CAP = 64 * 1024
_EDITOR_LINE_CAP = 500
_EDITOR_MAX_LINES = 250


def _parse_editor_command(cmd: str) -> tuple[str, str] | None:
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
        text = text[:_EDITOR_LINE_CAP] + "…"
    return {"t": kind, "text": text}


def _make_backup(path: str) -> str | None:
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


def _build_file_edit_event(
    tag_id: str, op: str, path: str, output: str, backup_path: str | None = None
) -> dict[str, Any] | None:
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
                    if len(preview_lines) > _EDITOR_MAX_LINES:
                        preview_lines = preview_lines[:_EDITOR_MAX_LINES]
                        evt["truncated"] = True
                    if len(preview) >= _EDITOR_OUTPUT_CAP:
                        evt["truncated"] = True
                    lines.extend(_diff_line_payload("add", line) for line in preview_lines)
                else:
                    lines.append(_diff_line_payload("meta", first or "Created file"))
            except Exception:
                lines.append(_diff_line_payload("meta", first or "Created file"))

        if len(lines) > _EDITOR_MAX_LINES:
            lines = lines[:_EDITOR_MAX_LINES]
            evt["truncated"] = True
        evt["lines"] = lines
        return evt
    except Exception:
        return None


def handle_execute_command(
    tag_id: str, name: str, attrs: dict[str, str], content: str
) -> Generator[dict[str, Any], None, None]:
    started = time.time()
    cmd = content.strip()
    editor_target = _parse_editor_command(cmd)
    editor_chunks: list[str] = []
    editor_chars = 0
    if not cmd:
        yield _output_event(tag_id, "No command provided\n", "stderr")
        yield _end_event(tag_id, name, False, started, error="Empty command")
        return

    try:
        timeout = int(attrs.get("timeout", DEFAULT_TIMEOUT))
    except Exception:
        timeout = DEFAULT_TIMEOUT
    timeout = max(1, min(timeout, MAX_TIMEOUT))

    yield _output_event(tag_id, f"$ {cmd}\n", "command")

    use_sudo = cmd.lstrip().startswith("sudo ")
    if use_sudo and "sudo -S" not in cmd:
        cmd = cmd.replace("sudo", "sudo -S -p ''", 1)

    proc = subprocess.Popen(
        cmd,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.PIPE if use_sudo else subprocess.DEVNULL,
        text=True,
        errors="replace",
        cwd=str(Path.home()),
        start_new_session=True,
    )

    if use_sudo and proc.stdin:
        try:
            proc.stdin.write(SUDO_PASSWORD + "\n")
            proc.stdin.flush()
            proc.stdin.close()
        except Exception:
            pass

    timer = threading.Timer(timeout, _kill_process_group, args=(proc,))
    timer.start()
    try:
        if proc.stdout is not None:
            for line in proc.stdout:
                yield _output_event(tag_id, line)
                if editor_target is not None and editor_chars < _EDITOR_OUTPUT_CAP:
                    remaining = _EDITOR_OUTPUT_CAP - editor_chars
                    if remaining > 0:
                        editor_chunks.append(line[:remaining])
                        editor_chars += min(len(line), remaining)
        proc.wait()
    finally:
        timer.cancel()

    code = proc.returncode
    ok = code == 0
    error = None if ok else f"exit code {code}"
    if code == -9:
        error = f"killed after {timeout}s"
    if ok and editor_target is not None:
        file_event = _build_file_edit_event(
            tag_id,
            editor_target[0],
            editor_target[1],
            "".join(editor_chunks),
        )
        if file_event is not None:
            yield file_event
    yield _end_event(tag_id, name, ok, started, {"exit_code": code, "timeout": timeout}, error)


def handle_execute_background_command(
    tag_id: str, name: str, attrs: dict[str, str], content: str
) -> Generator[dict[str, Any], None, None]:
    started = time.time()
    cmd = content.strip()
    if not cmd:
        yield _output_event(tag_id, "No command provided\n", "stderr")
        yield _end_event(tag_id, name, False, started, error="Empty command")
        return

    # Use a fixed UUID-based log filename BEFORE launching the process so
    # there is no race window between open() and os.replace().
    log_path = Path("/tmp") / f"ghost_bg_{uuid.uuid4().hex}.log"
    log_file = log_path.open("w", encoding="utf-8")
    try:
        proc = subprocess.Popen(
            cmd,
            shell=True,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            cwd=str(Path.home()),
            start_new_session=True,
        )
    finally:
        log_file.close()

    pid = proc.pid
    BG_JOBS[pid] = {
        "pid": pid,
        "command": cmd,
        "log": str(log_path),
        "started": time.time(),
        "status": "running",
    }

    yield _output_event(tag_id, f"Started background job {pid}\nLog: {log_path}\n$ {cmd}\n")
    yield _end_event(
        tag_id,
        name,
        True,
        started,
        {"pid": pid, "log": str(log_path), "command": cmd},
    )


def handle_check_command(
    tag_id: str, name: str, attrs: dict[str, str], content: str
) -> Generator[dict[str, Any], None, None]:
    started = time.time()
    pid_raw = attrs.get("pid") or content.strip()

    pid: int | None = None
    if pid_raw:
        try:
            pid = int(pid_raw)
        except Exception:
            # Invalid PID — fall through to list all jobs instead of hard-failing
            pid_raw = ""

    if pid_raw and pid is not None:

        info = BG_JOBS.get(pid, {})
        log_path = Path(info.get("log", f"/tmp/ghost_bg_{pid}.log"))
        running = Path(f"/proc/{pid}").exists()
        tail = ""
        if log_path.exists():
            try:
                data = log_path.read_text(errors="replace")
                tail = data[-RESULT_PREVIEW_CHARS:]
            except Exception as exc:
                tail = f"Could not read log: {exc}"
        if not tail:
            tail = "(no log output found)"

        yield _output_event(tag_id, tail + "\n")
        yield _end_event(
            tag_id,
            name,
            True,
            started,
            {
                "pid": pid,
                "running": running,
                "log": str(log_path),
                "command": info.get("command"),
            },
        )
        return

    if not BG_JOBS:
        yield _output_event(tag_id, "No background jobs tracked in this server process.\n")
        yield _end_event(tag_id, name, True, started, {"jobs": []})
        return

    jobs = []
    for pid, info in BG_JOBS.items():
        running = Path(f"/proc/{pid}").exists()
        info["status"] = "running" if running else "exited"
        jobs.append(info)
        yield _output_event(
            tag_id,
            f"{pid} [{info['status']}] {info.get('command', '')} -> {info.get('log', '')}\n",
        )
    yield _end_event(tag_id, name, True, started, {"jobs": jobs})


def handle_get_file(
    tag_id: str, name: str, attrs: dict[str, str], content: str
) -> Generator[dict[str, Any], None, None]:
    started = time.time()
    raw = content.strip() or attrs.get("path", "")
    if not raw:
        yield _output_event(tag_id, "No path provided\n", "stderr")
        yield _end_event(tag_id, name, False, started, error="Empty path")
        return

    path = Path(raw).expanduser()
    if not path.exists():
        yield _output_event(tag_id, f"Path not found: {path}\n", "stderr")
        yield _end_event(tag_id, name, False, started, error="Path not found")
        return

    if path.is_dir():
        cmd = ["python3", str(EDITOR_TOOLS), "view", str(path)]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=DEFAULT_TIMEOUT,
                errors="replace",
            )
            output = proc.stdout or proc.stderr
            yield _output_event(tag_id, output[:RESULT_PREVIEW_CHARS] + "\n")
            yield _end_event(
                tag_id,
                name,
                proc.returncode == 0,
                started,
                {"path": str(path), "kind": "directory"},
                None if proc.returncode == 0 else f"exit code {proc.returncode}",
            )
        except Exception as exc:
            yield _output_event(tag_id, f"{type(exc).__name__}: {exc}\n", "stderr")
            yield _end_event(tag_id, name, False, started, error=str(exc))
        return

    size = path.stat().st_size
    mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    ext = path.suffix.lower()
    if mime.startswith("image/"):
        kind = "image"
    elif mime.startswith("text/") or ext in TEXT_EXTENSIONS or path.name.lower() in {"makefile", "dockerfile"}:
        kind = "text"
    else:
        kind = "binary"

    result: dict[str, Any] = {"path": str(path), "size": size, "mime": mime, "kind": kind}

    if kind == "image":
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        dest = UPLOAD_DIR / f"{uuid.uuid4().hex}{ext or '.img'}"
        shutil.copy2(path, dest)
        result["url"] = f"/uploads/{dest.name}"
        yield _output_event(tag_id, f"image {path} ({size} bytes) copied to {result['url']}\n")
    elif kind == "text":
        try:
            if size <= MAX_TEXT_BYTES:
                text = path.read_text(errors="replace")
            else:
                with path.open("r", errors="replace") as handle:
                    text = handle.read(PREVIEW_BYTES)
                result["truncated"] = True
            result["preview_chars"] = len(text)
            preview = text
            if len(preview) > RESULT_PREVIEW_CHARS:
                preview = preview[:RESULT_PREVIEW_CHARS] + f"\n... truncated ({len(text)} chars)"
            yield _output_event(tag_id, preview + "\n")
        except Exception as exc:
            yield _output_event(tag_id, f"{type(exc).__name__}: {exc}\n", "stderr")
            yield _end_event(tag_id, name, False, started, error=str(exc))
            return
    else:
        with path.open("rb") as handle:
            head = handle.read(256)
        result["preview_b64"] = base64.b64encode(head).decode()
        yield _output_event(tag_id, f"binary file {path} ({size} bytes, {mime})\n")

    yield _end_event(tag_id, name, True, started, result)


_SEARCH_SCRIPT = SKILLS_DIR / "data" / "search_online" / "web_search_batch.py"


def handle_search_online(
    tag_id: str, name: str, attrs: dict[str, str], content: str
) -> Generator[dict[str, Any], None, None]:
    started = time.time()
    query = content.strip() or attrs.get("query", "")
    if not query:
        yield _output_event(tag_id, "No search query provided\n", "stderr")
        yield _end_event(tag_id, name, False, started, error="Empty query")
        return

    yield _output_event(tag_id, f"Searching: {query}\n\n")

    try:
        proc = subprocess.run(
            ["python3", str(_SEARCH_SCRIPT), "--json", query],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode != 0:
            yield _output_event(tag_id, f"Search failed: {proc.stderr.strip()}\n", "stderr")
            yield _end_event(tag_id, name, False, started, error=proc.stderr.strip()[:500])
            return

        data = json.loads(proc.stdout)
        items = data.get("items", [])
        if not items:
            yield _output_event(tag_id, "No results found.\n")
            yield _end_event(tag_id, name, True, started, {"query": query, "results": []})
            return

        for item in items:
            context = item.get("context", "")
            if context:
                yield _output_event(tag_id, context + "\n")
            elif not item.get("ok"):
                yield _output_event(tag_id, f"Error: {item.get('error', 'unknown')}\n", "stderr")

        yield _end_event(tag_id, name, True, started, {"query": query, "results": items})

    except subprocess.TimeoutExpired:
        yield _output_event(tag_id, "Search timed out (60s)\n", "stderr")
        yield _end_event(tag_id, name, False, started, error="Search timed out")
    except json.JSONDecodeError as exc:
        yield _output_event(tag_id, f"Failed to parse search output: {exc}\n", "stderr")
        yield _end_event(tag_id, name, False, started, error=str(exc))
    except Exception as exc:
        yield _output_event(tag_id, f"Search error: {exc}\n", "stderr")
        yield _end_event(tag_id, name, False, started, error=str(exc))


def handle_openweb(
    tag_id: str, name: str, attrs: dict[str, str], content: str
) -> Generator[dict[str, Any], None, None]:
    started = time.time()
    site = attrs.get("site", "")
    op = attrs.get("op", "fetch").lower()
    params_raw = attrs.get("params", "")
    params: dict[str, Any] = {}
    if params_raw:
        try:
            loaded = json.loads(params_raw)
            if isinstance(loaded, dict):
                params = loaded
            else:
                params = {"query": str(loaded)}
        except Exception:
            params = {"query": params_raw}

    if op in {"search", "query"}:
        query = str(params.get("query") or content.strip()).strip()
        if site:
            query = f"site:{site} {query}"
        if not query:
            yield _output_event(tag_id, "No OpenWeb query provided\n", "stderr")
            yield _end_event(tag_id, name, False, started, error="Empty query")
            return
        yield _output_event(tag_id, f"OpenWeb search: {query}\n\n")
        try:
            proc = subprocess.run(
                ["python3", str(_SEARCH_SCRIPT), "--json", query],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if proc.returncode != 0:
                yield _output_event(tag_id, f"Search failed: {proc.stderr.strip()}\n", "stderr")
                yield _end_event(tag_id, name, False, started, error=proc.stderr.strip()[:500])
                return
            data = json.loads(proc.stdout)
            items = data.get("items", [])
            for item in items:
                context = item.get("context", "")
                if context:
                    yield _output_event(tag_id, context + "\n")
            yield _end_event(tag_id, name, True, started, {"site": site, "op": op, "results": items})
        except Exception as exc:
            yield _output_event(tag_id, f"Search error: {exc}\n", "stderr")
            yield _end_event(tag_id, name, False, started, error=str(exc))
        return

    url = str(params.get("url") or content.strip()).strip()
    if not url and site:
        url = f"https://{site}"
    if not url:
        yield _output_event(tag_id, "No URL provided\n", "stderr")
        yield _end_event(tag_id, name, False, started, error="Empty URL")
        return
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    try:
        res = httpx.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:153.0)"},
            timeout=15,
            follow_redirects=True,
        )
        ctype = res.headers.get("content-type", "")
        if "json" in ctype:
            try:
                text = json.dumps(res.json(), indent=2, ensure_ascii=False)
            except Exception:
                text = res.text
        else:
            text = _strip_html(res.text)
        preview = text[:RESULT_PREVIEW_CHARS]
        yield _output_event(tag_id, preview + "\n")
        yield _end_event(
            tag_id,
            name,
            True,
            started,
            {"url": url, "status": res.status_code, "content_type": ctype, "chars": len(text)},
        )
    except Exception as exc:
        yield _output_event(tag_id, f"{type(exc).__name__}: {exc}\n", "stderr")
        yield _end_event(tag_id, name, False, started, error=str(exc))


def handle_create_note(
    tag_id: str, name: str, attrs: dict[str, str], content: str
) -> Generator[dict[str, Any], None, None]:
    started = time.time()
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    raw = content.strip()
    note_name = ""
    note_content = ""

    try:
        payload = json.loads(raw)
        if isinstance(payload, dict) and ("content" in payload or "path" in payload or "title" in payload):
            note_name = str(payload.get("path") or payload.get("title") or "")
            note_content = str(payload.get("content", ""))
    except Exception:
        pass

    if not note_name:
        lines = raw.splitlines()
        if len(lines) > 1 and (lines[0].strip().endswith(".md") or "/" in lines[0]):
            note_name = lines[0].strip()
            note_content = "\n".join(lines[1:])
        else:
            note_name = f"note-{time.strftime('%Y%m%d-%H%M%S')}.md"
            note_content = raw

    if not note_name.endswith(".md"):
        note_name += ".md"

    try:
        path = _safe_under(NOTES_DIR, note_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(note_content, encoding="utf-8")
    except Exception as exc:
        yield _output_event(tag_id, f"{type(exc).__name__}: {exc}\n", "stderr")
        yield _end_event(tag_id, name, False, started, error=str(exc))
        return

    yield _output_event(tag_id, f"Created note {path} ({len(note_content)} chars)\n")
    yield _end_event(tag_id, name, True, started, {"path": str(path), "chars": len(note_content)})


def handle_save_svg(
    tag_id: str, name: str, attrs: dict[str, str], content: str
) -> Generator[dict[str, Any], None, None]:
    started = time.time()
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    raw = content.strip()
    svg_name = attrs.get("path", "")
    svg = ""

    try:
        payload = json.loads(raw)
        if isinstance(payload, dict) and "svg" in payload:
            svg_name = svg_name or str(payload.get("path", ""))
            svg = str(payload["svg"])
    except Exception:
        pass

    if not svg:
        svg = raw
    if not svg_name:
        svg_name = f"svg-{time.strftime('%Y%m%d-%H%M%S')}.svg"
    if not svg_name.endswith(".svg"):
        svg_name += ".svg"

    if not svg.lstrip().startswith("<svg"):
        yield _output_event(tag_id, "Content does not look like SVG\n", "stderr")
        yield _end_event(tag_id, name, False, started, error="Invalid SVG")
        return

    try:
        path = _safe_under(ASSETS_DIR, svg_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(svg, encoding="utf-8")
    except Exception as exc:
        yield _output_event(tag_id, f"{type(exc).__name__}: {exc}\n", "stderr")
        yield _end_event(tag_id, name, False, started, error=str(exc))
        return

    yield _output_event(tag_id, f"Saved SVG {path} ({len(svg)} chars)\n")
    yield _end_event(tag_id, name, True, started, {"path": str(path), "chars": len(svg)})


# --------------------------------------------------------------------------
# Native editor tag handlers — view_file, edit_file, create_file, insert_file
# These call editor_tools.py directly (no shell heredoc quoting needed).
# --------------------------------------------------------------------------

def _run_editor(args: list[str], stdin_data: str | None = None, timeout: int = DEFAULT_TIMEOUT) -> tuple[bool, str]:
    """Run editor_tools.py with the given args.  Returns (ok, output_text)."""
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


def handle_view_file(
    tag_id: str, name: str, attrs: dict[str, str], content: str
) -> Generator[dict[str, Any], None, None]:
    started = time.time()
    path = attrs.get("path", "").strip() or content.strip()
    if not path:
        yield _output_event(tag_id, "No path attribute provided\n", "stderr")
        yield _end_event(tag_id, name, False, started, error="Missing path")
        return

    path = os.path.expandvars(os.path.expanduser(path))

    args = ["view", path]
    start_line = attrs.get("start")
    end_line = attrs.get("end")
    full = attrs.get("full", "").lower() in ("true", "1", "yes")
    if start_line:
        args += ["--start", str(start_line)]
    if end_line:
        args += ["--end", str(end_line)]
    if full:
        args.append("--full")

    ok, output = _run_editor(args)
    output_trimmed = output[:RESULT_PREVIEW_CHARS]
    yield _output_event(tag_id, output_trimmed + "\n")
    yield _end_event(tag_id, name, ok, started, {"path": path}, None if ok else output_trimmed[:500])


def handle_edit_file(
    tag_id: str, name: str, attrs: dict[str, str], content: str
) -> Generator[dict[str, Any], None, None]:
    started = time.time()
    path = attrs.get("path", "").strip()
    if not path:
        yield _output_event(tag_id, "No path attribute provided\n", "stderr")
        yield _end_event(tag_id, name, False, started, error="Missing path")
        return

    path = os.path.expandvars(os.path.expanduser(path))

    if not content.strip():
        yield _output_event(tag_id, "No SEARCH/REPLACE blocks in edit_file body\n", "stderr")
        yield _end_event(tag_id, name, False, started, error="Empty edit body")
        return

    backup_path = _make_backup(path)
    ok, output = _run_editor(["edit", path], stdin_data=content)
    output_trimmed = output[:RESULT_PREVIEW_CHARS]
    yield _output_event(tag_id, output_trimmed + "\n")

    if ok:
        # Emit a file_edit event so the diff sidebar updates
        file_event = _build_file_edit_event(tag_id, "edit", path, output_trimmed, backup_path)
        if file_event is not None:
            yield file_event

    yield _end_event(tag_id, name, ok, started, {"path": path}, None if ok else output_trimmed[:500])


def handle_create_file(
    tag_id: str, name: str, attrs: dict[str, str], content: str
) -> Generator[dict[str, Any], None, None]:
    started = time.time()
    path = attrs.get("path", "").strip()
    if not path:
        yield _output_event(tag_id, "No path attribute provided\n", "stderr")
        yield _end_event(tag_id, name, False, started, error="Missing path")
        return

    path = os.path.expandvars(os.path.expanduser(path))
    overwrite = attrs.get("overwrite", "").lower() in ("true", "1", "yes")

    args = ["create", path]
    if overwrite:
        args.append("--overwrite")

    backup_path = _make_backup(path) if overwrite else None
    ok, output = _run_editor(args, stdin_data=content)
    output_trimmed = output[:RESULT_PREVIEW_CHARS]
    yield _output_event(tag_id, output_trimmed + "\n")

    if ok:
        file_event = _build_file_edit_event(tag_id, "create", path, output_trimmed, backup_path)
        if file_event is not None:
            yield file_event

    yield _end_event(tag_id, name, ok, started, {"path": path}, None if ok else output_trimmed[:500])


def handle_insert_file(
    tag_id: str, name: str, attrs: dict[str, str], content: str
) -> Generator[dict[str, Any], None, None]:
    started = time.time()
    path = attrs.get("path", "").strip()
    if not path:
        yield _output_event(tag_id, "No path attribute provided\n", "stderr")
        yield _end_event(tag_id, name, False, started, error="Missing path")
        return

    path = os.path.expandvars(os.path.expanduser(path))

    at_line = attrs.get("at_line") or attrs.get("at-line")
    after_str = attrs.get("after_str") or attrs.get("after-str")

    if not at_line and not after_str:
        yield _output_event(tag_id, "insert_file requires at_line or after_str attribute\n", "stderr")
        yield _end_event(tag_id, name, False, started, error="Missing at_line or after_str")
        return

    args = ["insert", path]
    backup_path = _make_backup(path)
    tmp_anchor: Path | None = None
    try:
        if at_line:
            args += ["--at-line", str(at_line)]
        elif after_str:
            # Write anchor to a temp file so multiline anchors survive intact
            # (passing multiline text as a CLI arg can be truncated by the shell)
            tmp_anchor = Path("/tmp") / f"sable_anchor_{uuid.uuid4().hex}.txt"
            tmp_anchor.write_text(after_str, encoding="utf-8")
            args += ["--after-file", str(tmp_anchor)]

        ok, output = _run_editor(args, stdin_data=content)
    finally:
        if tmp_anchor and tmp_anchor.exists():
            try:
                tmp_anchor.unlink()
            except Exception:
                pass

    output_trimmed = output[:RESULT_PREVIEW_CHARS]
    yield _output_event(tag_id, output_trimmed + "\n")

    if ok:
        file_event = _build_file_edit_event(tag_id, "insert", path, output_trimmed, backup_path)
        if file_event is not None:
            yield file_event

    yield _end_event(tag_id, name, ok, started, {"path": path}, None if ok else output_trimmed[:500])


HANDLERS = {
    "execute_command": handle_execute_command,
    "execute_background_command": handle_execute_background_command,
    "get_file": handle_get_file,
    "read_file": handle_get_file,
    "search-online": handle_search_online,
    "search_online": handle_search_online,
    "check_command": handle_check_command,
    "openweb": handle_openweb,
    "create_note": handle_create_note,
    "save_svg": handle_save_svg,
    # Native editor tags
    "view_file": handle_view_file,
    "edit_file": handle_edit_file,
    "create_file": handle_create_file,
    "insert_file": handle_insert_file,
}


def _parse_attrs(raw: str) -> dict[str, str]:
    raw = raw.strip()
    if raw.endswith("/"):
        raw = raw[:-1]
    attrs: dict[str, str] = {}
    for match in re.finditer(r'([\w-]+)\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s>]+))', raw):
        key = match.group(1).lower()
        value = match.group(2)
        if value is None:
            value = match.group(3)
        if value is None:
            value = match.group(4)
        attrs[key] = value or ""
    return attrs


def _run_tag(name: str, attrs_raw: str, content: str) -> Generator[dict[str, Any], None, None]:
    tag_id = uuid.uuid4().hex[:12]
    attrs = _parse_attrs(attrs_raw)
    started = time.time()
    yield {
        "type": "skill_start",
        "id": tag_id,
        "name": name,
        "data": {"attrs": attrs, "content": content[:2000]},
    }

    handler = HANDLERS.get(name.lower()) or HANDLERS.get(name.lower().replace("-", "_"))
    if handler is None:
        yield _end_event(tag_id, name, False, started, error=f"No handler for tag {name}")
        return

    saw_end = False
    try:
        for event in handler(tag_id, name, attrs, content):
            if event.get("type") == "skill_end":
                saw_end = True
            yield event
        if not saw_end:
            yield _end_event(tag_id, name, True, started)
    except Exception as exc:
        yield _end_event(tag_id, name, False, started, error=f"{type(exc).__name__}: {exc}")


TAG_ALTERNATION = "|".join(re.escape(tag) for tag in KNOWN_TAGS)


class SkillParser:
    """Extracts complete agentic tags from streamed answer text."""

    def __init__(self) -> None:
        self.buf = ""
        self.open_re = re.compile(r"<\s*(" + TAG_ALTERNATION + r")\b([^>]*)>", re.I)
        self._pending_tag: str | None = None  # tracks emitted tool_pending to avoid repeats
        self._last_progress: tuple[int, int] = (0, 0)  # (lines, bytes) of last tool_progress

    def feed(self, text: str) -> Generator[dict[str, Any], None, None]:
        self.buf += text
        while True:
            found = self._find_complete()
            if found:
                start, end, name, attrs, content = found
                before = self.buf[:start]
                if before:
                    yield {"type": "text", "text": before}
                self.buf = self.buf[end:]
                self._pending_tag = None  # tag completed, clear pending state
                self._last_progress = (0, 0)
                yield from _run_tag(name, attrs, content)
                continue

            hold = self._hold_start()
            if hold is None:
                if self.buf:
                    yield {"type": "text", "text": self.buf}
                    self.buf = ""
                break

            if hold > 0:
                yield {"type": "text", "text": self.buf[:hold]}
                self.buf = self.buf[hold:]

            # Emit a tool_pending event so the frontend can show an activity
            # card while the tag content is still streaming in.
            pending_match = self.open_re.search(self.buf)
            if pending_match:
                tag_name = pending_match.group(1).lower()
                if tag_name != self._pending_tag:
                    self._pending_tag = tag_name
                    self._last_progress = (0, 0)
                    attrs = _parse_attrs(pending_match.group(2) or "")
                    yield {
                        "type": "tool_pending",
                        "tag": tag_name,
                        "attrs": attrs,
                    }
                # Stream live progress (lines/bytes of the partial tag content)
                # so the activity card can show a growing counter while writing.
                partial = self.buf[pending_match.end():]
                p_lines = partial.count("\n") + (1 if partial else 0)
                p_bytes = len(partial.encode("utf-8"))
                last_lines, last_bytes = self._last_progress
                if p_lines != last_lines or p_bytes - last_bytes >= 96:
                    self._last_progress = (p_lines, p_bytes)
                    yield {
                        "type": "tool_progress",
                        "tag": tag_name,
                        "lines": p_lines,
                        "bytes": p_bytes,
                    }
            break

    def flush(self) -> Generator[dict[str, Any], None, None]:
        if self.buf:
            yield {"type": "text", "text": self.buf}
            self.buf = ""

    def _find_complete(self) -> tuple[int, int, str, str, str] | None:
        for match in self.open_re.finditer(self.buf):
            name = match.group(1).lower()
            attrs = match.group(2) or ""
            stripped_attrs = attrs.rstrip()
            if stripped_attrs.endswith("/") or stripped_attrs == "/":
                return match.start(), match.end(), name, attrs, ""
            close_re = re.compile(r"<\s*/\s*" + re.escape(name) + r"\s*>", re.I)
            close_match = close_re.search(self.buf, match.end())
            if close_match:
                return (
                    match.start(),
                    close_match.end(),
                    name,
                    attrs,
                    self.buf[match.end() : close_match.start()],
                )
        return None

    def _hold_start(self) -> int | None:
        match = self.open_re.search(self.buf)
        if match:
            return match.start()

        # Hold ANY trailing '<' that could be the start of a skill tag.
        # Only hold if the text after '<' is a valid prefix of a known tag
        # name (and does NOT already contain '>' which would mean it's a
        # closed HTML tag like <br> or <h2>, not an in-flight skill tag).
        idx = self.buf.rfind("<")
        if idx >= 0:
            tail = self.buf[idx:]
            # If the tail already has a '>' it's a complete HTML tag — release it
            if ">" not in tail:
                partial = tail.lstrip("<").strip().lower()
                if partial == "" or any(tag.startswith(partial) or partial.startswith(tag) for tag in KNOWN_TAGS):
                    return idx
        return None



def build_tool_feedback(
    skill_events: list[dict[str, Any]],
    max_output_per_skill: int = 12000,
    max_total: int = 32000,
) -> str | None:
    starts: dict[str, dict[str, Any]] = {}
    outputs: dict[str, list[str]] = {}
    ends: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    for event in skill_events:
        event_id = str(event.get("id") or "")
        if not event_id:
            continue
        event_type = event.get("type")
        if event_type == "skill_start":
            if event_id not in starts:
                order.append(event_id)
            starts[event_id] = event
            outputs.setdefault(event_id, [])
        elif event_type == "skill_output":
            outputs.setdefault(event_id, []).append(str(event.get("text", "")))
        elif event_type == "skill_end":
            ends[event_id] = event
            if event_id not in starts:
                order.append(event_id)
                starts[event_id] = {"name": event.get("name", "skill")}

    if not ends:
        return None

    parts: list[str] = []
    total = 0

    for event_id in order:
        end = ends.get(event_id)
        if end is None:
            continue

        start = starts.get(event_id, {})
        name = str(start.get("name") or end.get("name") or "skill")
        ok = bool(end.get("ok"))
        duration = end.get("duration_ms", 0)
        error = end.get("error")
        data = start.get("data") or {}
        content = str(data.get("content") or "")
        output = "".join(outputs.get(event_id, []))

        if len(output) > max_output_per_skill:
            half = max_output_per_skill // 2
            output = output[:half] + "\n... truncated ...\n" + output[-half:]

        result = end.get("result") or {}
        try:
            result_json = json.dumps(result, ensure_ascii=False)
        except Exception:
            result_json = "{}"

        if len(result_json) > 2000:
            result_json = result_json[:2000] + "...}"

        entry = f'<tool_result name="{name}" ok="{str(ok).lower()}" duration_ms="{duration}">\n'
        if content:
            entry += f'<input>\n{content[:2000]}{"... [truncated]" if len(content) > 2000 else ""}\n</input>\n'
        if output:
            entry += f'<output>\n{output}\n</output>\n'
        if error:
            entry += f'<error>\n{error}\n</error>\n'
        if result_json != "{}":
            entry += f'<result>\n{result_json}\n</result>\n'
        entry += '</tool_result>'

        if total + len(entry) > max_total:
            parts.append('<tool_result truncated="true" />')
            break

        parts.append(entry)
        total += len(entry)

    return (
        "<tool_results>\n" + "\n".join(parts) + "\n</tool_results>"
    )

