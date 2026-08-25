
"""Command execution handlers: execute_command (with bg=true support), check_command."""

from __future__ import annotations

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
    from engine.config import SSD_TREE, HDD_TREE, USER_NAME
    if _re.search(r'systemctl\s+(--user\s+)?(restart|stop)\s+sable\.service', cmd):
        msg = f"[BLOCKED] Agent cannot restart/stop sable.service mid-session.\nAsk {USER_NAME} to run it manually in a terminal.\n"
        yield _output_event(tag_id, msg, "stderr")
        yield _end_event(tag_id, name, False, started, error="Blocked: sable.service restart not allowed via execute_command")
        return

    # Block git checkout universally — destroys uncommitted work
    if _re.search(r'\bgit\s+checkout\b', cmd):
        msg = f"[BLOCKED] git checkout is not allowed. It destroys uncommitted working changes.\nIf you need to revert, ask {USER_NAME} first or use targeted file restoration.\n"
        yield _output_event(tag_id, msg, "stderr")
        yield _end_event(tag_id, name, False, started, error="Blocked: git checkout not allowed")
        return

    # --- SSD tree write guard ---
    # Only allow reads and explicit cp from HDD tree (the sanctioned sync path).
    _ssd_escaped = _re.escape(SSD_TREE)
    _hdd_escaped = _re.escape(HDD_TREE)
    _ssd_pattern = _re.compile(
        rf'({_ssd_escaped}|~/Projects/Sable|\$PROJECT_ROOT)'
    )
    if _ssd_pattern.search(cmd):
        # Reads ALWAYS pass. Only block destructive/code-overwriting ops.
        # Sync (HDD->SSD copy) is always allowed — that's the sanctioned path.
        _has_hdd = bool(_re.search(rf'({_hdd_escaped}|~/hdd/projects/Sable)', cmd))
        _is_cp = bool(_re.search(r'(?:^|\s|;|&&|\|\||\|)\s*cp\b', cmd))
        is_sync = _is_cp and _has_hdd
        is_write = False
        # 1. shell redirection INTO an SSD path  (> or >> or 2>)
        if _re.search(rf'(>>?|2>)\s*["\']?({_ssd_escaped}|~/Projects/Sable)', cmd):
            is_write = True
        # 2. destructive / in-place-mutating verbs (mkdir excluded — safe, needed for sync)
        _mutate = r'(?:^|\s|;|&&|\|\||\|)\s*(rm|mv|touch|tee|chmod|chown|truncate|shred|sed\s+-i|perl\s+-i)\b'
        if _re.search(_mutate, cmd):
            is_write = True
        # 3. cp whose destination is SSD but source is NOT the HDD tree
        if _is_cp and not _has_hdd:
            is_write = True
        # 4. git write verbs (add/commit/push/etc.)
        if _re.search(r'\bgit\s+(add|commit|push|merge|rebase|reset|checkout|pull|clone|rm|mv)\b', cmd):
            is_write = True
        if is_write and not is_sync:
            msg = f"[BLOCKED] Direct write to {SSD_TREE} is not allowed.\nEdit in {HDD_TREE} first, dont touch ssd Sable.\n"
            yield _output_event(tag_id, msg, 'stderr')
            yield _end_event(tag_id, name, False, started, error='Blocked: SSD tree write guard')
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
        from engine.platform_paths import tmp_path, pid_exists
        log_path = Path(info.get("log", str(tmp_path(f"ghost_bg_{pid}.log"))))
        running = pid_exists(pid)
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
        from engine.platform_paths import pid_exists
        running = pid_exists(job_pid)
        info["status"] = "running" if running else "exited"
        jobs.append(info)
        yield _output_event(
            tag_id,
            f"{job_pid} [{info['status']}] {info.get('command', '')} -> {info.get('log', '')}\n",
        )
    yield _end_event(tag_id, name, True, started, {"jobs": jobs})
#
