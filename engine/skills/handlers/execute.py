
"""Command execution handlers: execute_command, execute_background_command, check_command."""

from __future__ import annotations

import os
import subprocess
import threading
import time
import uuid
from collections.abc import Generator
from pathlib import Path
from typing import Any

from engine.skills.handlers.common import (
    DEFAULT_TIMEOUT,
    MAX_TIMEOUT,
    RESULT_PREVIEW_CHARS,
    SUDO_PASSWORD,
    _EDITOR_OUTPUT_CAP,
    _end_event,
    _output_event,
    build_file_edit_event,
    kill_process_group,
    parse_editor_command,
)

# Module-level background job store (replaces old global BG_JOBS)
BG_JOBS: dict[int, dict[str, Any]] = {}


def handle_execute_command(
    tag_id: str, name: str, attrs: dict[str, str], content: str
) -> Generator[dict[str, Any], None, None]:
    # Route bg="true" to background handler
    if attrs.get("bg", "").lower() in ("true", "1", "yes"):
        yield from handle_execute_background_command(tag_id, name, attrs, content)
        return

    started = time.time()
    cmd = content.strip()

    # Block agent-issued restart/stop of sable.service (user can still do it manually)
    import re as _re
    if _re.search(r'systemctl\s+(--user\s+)?(restart|stop)\s+sable\.service', cmd):
        msg = "[BLOCKED] Agent cannot restart/stop sable.service mid-session.\nAsk the user to run it manually in a terminal.\n"
        yield _output_event(tag_id, msg, "stderr")
        yield _end_event(tag_id, name, False, started, error="Blocked: sable.service restart not allowed via execute_command")
        return

    # Block git checkout universally — destroys uncommitted work
    if _re.search(r'\bgit\s+checkout\b', cmd):
        msg = "[BLOCKED] git checkout is not allowed. It destroys uncommitted working changes.\nIf you need to revert, ask the user first or use targeted file restoration.\n"
        yield _output_event(tag_id, msg, "stderr")
        yield _end_event(tag_id, name, False, started, error="Blocked: git checkout not allowed")
        return

    # --- Protected tree write guard ---
    # Reads from SABLE_PROTECTED_TREE paths always pass; writes are blocked
    # unless the env var is unset (guard disabled).
    _protected = os.getenv("SABLE_PROTECTED_TREE", "")
    if _protected:
        _prot_paths = [p.strip() for p in _protected.split(",") if p.strip()]
        _prot_pattern = _re.compile("|".join(_re.escape(p) for p in _prot_paths))
        if _prot_pattern.search(cmd):
            _is_cp = bool(_re.search(r'(?:^|\s|;|&&|\|\||\|)\s*cp\b', cmd))
            is_write = False
            if _re.search(r'(>>?|2>)\s*["\']?' + "|".join(_re.escape(p) for p in _prot_paths), cmd):
                is_write = True
            _mutate = r'(?:^|\s|;|&&|\|\||\|)\s*(rm|mv|touch|tee|chmod|chown|truncate|shred|sed\s+-i|perl\s+-i)\b'
            if _re.search(_mutate, cmd):
                is_write = True
            if _is_cp:
                is_write = True
            if _re.search(r'\bgit\s+(?:-\w+\s+\S+\s+)*(add|commit|push|merge|rebase|reset|checkout|pull|clone|rm|mv)\b', cmd):
                is_write = True
            if is_write:
                msg = f"[BLOCKED] Direct write to protected tree ({_prot_paths[0]}) is not allowed.\n"
                yield _output_event(tag_id, msg, 'stderr')
                yield _end_event(tag_id, name, False, started, error='Blocked: protected tree write guard')
                return

    editor_target = parse_editor_command(cmd)
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

    timer = threading.Timer(timeout, kill_process_group, args=(proc,))
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
        file_event = build_file_edit_event(
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
    for job_pid, info in BG_JOBS.items():
        running = Path(f"/proc/{job_pid}").exists()
        info["status"] = "running" if running else "exited"
        jobs.append(info)
        yield _output_event(
            tag_id,
            f"{job_pid} [{info['status']}] {info.get('command', '')} -> {info.get('log', '')}\n",
        )
    yield _end_event(tag_id, name, True, started, {"jobs": jobs})
