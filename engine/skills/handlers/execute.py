
"""Command execution handlers: execute_command (with bg=true support), check_command."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import time
import uuid
from collections.abc import Generator
from pathlib import Path
from typing import Any

from engine.platform_paths import home_dir, tmp_path, IS_WINDOWS
from engine.process_utils import kill_process_tree, popen_kwargs
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

# Command policy patterns — compiled once at import.
_SABLE_SERVICE_RE = re.compile(r"systemctl\s+(--user\s+)?(restart|stop)\s+sable\.service")
_GIT_CHECKOUT_RE = re.compile(r"\bgit\s+checkout\b")
_SUDO_TOKEN_RE = re.compile(r"(?<![\w-])sudo(?![\w-])")


def handle_execute_command(
    tag_id: str, name: str, attrs: dict[str, str], content: str
) -> Generator[dict[str, Any], None, None]:
    # Route bg="true" to background handler
    if attrs.get("bg", "").lower() in ("true", "1", "yes"):
        yield from handle_execute_background_command(tag_id, name, attrs, content)
        return

    started = time.time()
    cmd = content.strip()

    # Working directory: explicit cwd= wins, else the user's home.
    run_cwd = attrs.get("cwd", "").strip()
    if run_cwd:
        run_cwd = os.path.expandvars(os.path.expanduser(run_cwd))
    else:
        run_cwd = str(home_dir())

    # Extra environment variables (JSON object string, merged over inherited env).
    run_env: dict[str, str] | None = None
    _env_raw = attrs.get("env", "").strip()
    if _env_raw:
        try:
            _parsed = json.loads(_env_raw) if _env_raw.startswith("{") else None
            if isinstance(_parsed, dict):
                run_env = {**os.environ, **{str(k): str(v) for k, v in _parsed.items()}}
        except (json.JSONDecodeError, TypeError):
            pass

    from engine.config import USER_NAME

    # Block agent-issued restart/stop of sable.service (user can still do it manually)
    if _SABLE_SERVICE_RE.search(cmd):
        msg = f"[BLOCKED] Agent cannot restart/stop sable.service mid-session.\nAsk {USER_NAME} to run it manually in a terminal.\n"
        yield _output_event(tag_id, msg, "stderr")
        yield _end_event(tag_id, name, False, started, error="Blocked: sable.service restart not allowed via execute_command")
        return

    # Block git checkout universally — destroys uncommitted work
    if _GIT_CHECKOUT_RE.search(cmd):
        msg = f"[BLOCKED] git checkout is not allowed. It destroys uncommitted working changes.\nIf you need to revert, ask {USER_NAME} first or use targeted file restoration.\n"
        yield _output_event(tag_id, msg, "stderr")
        yield _end_event(tag_id, name, False, started, error="Blocked: git checkout not allowed")
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

    # sudo is POSIX-only; skip injection on Windows
    use_sudo = (not IS_WINDOWS) and cmd.lstrip().startswith("sudo ")
    if use_sudo and "sudo -S" not in cmd:
        # Replace only the first standalone `sudo` token (word-boundary safe)
        cmd = _SUDO_TOKEN_RE.sub("sudo -S -p ''", cmd, count=1)

    if not os.path.isdir(run_cwd):
        yield _output_event(tag_id, f"Working directory not found: {run_cwd}\n", "stderr")
        yield _end_event(tag_id, name, False, started, error=f"cwd not found: {run_cwd}")
        return

    proc = subprocess.Popen(
        cmd,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.PIPE if use_sudo else subprocess.DEVNULL,
        text=True,
        errors="replace",
        cwd=run_cwd,
        env=run_env,
        **popen_kwargs(),
    )

    # Capture the process-group id NOW, at spawn time.
    #
    # The group outlives its leader. Once the shell exits and is reaped by
    # proc.wait(), /proc/<pid> disappears and os.getpgid(pid) can no longer
    # resolve the group — so we would permanently lose our only handle on any
    # descendants the command left behind. This applies to BOTH failure paths:
    # the timeout timer (leader may already be reaped) and normal completion
    # (leader is always reaped before the finally block runs).
    _pgid: int | None = None
    if not IS_WINDOWS:
        try:
            _pgid = os.getpgid(proc.pid)
        except OSError:
            _pgid = None

    def _kill_group(sig: int) -> None:
        """Signal our spawned process group, falling back to the single pid."""
        if _pgid is not None:
            try:
                os.killpg(_pgid, sig)
                return
            except (ProcessLookupError, PermissionError, OSError):
                return
        kill_process_tree(proc.pid, sig=sig)

    if use_sudo and proc.stdin:
        try:
            proc.stdin.write(SUDO_PASSWORD + "\n")
            proc.stdin.flush()
            proc.stdin.close()
        except Exception:
            pass

    # Timer fires a polite SIGTERM, then escalates to SIGKILL after a short
    # grace period. The escalation matters: a child that traps SIGTERM and
    # produces no output would otherwise leave the `for line in proc.stdout`
    # read blocked forever (no output -> the in-loop deadline check never
    # runs), and the generator would never yield a final result.
    def _enforce_timeout() -> None:
        # Use the captured pgid, not the pid: by the time this fires the shell
        # leader may already be reaped, and kill_process_tree(pid) would find
        # no /proc entry and silently do nothing.
        _kill_group(signal.SIGTERM)   # graceful first
        time.sleep(2)                 # grace period
        _kill_group(signal.SIGKILL)   # force, unstoppable

    timer = threading.Timer(timeout, _enforce_timeout)
    timer.start()
    # Frontend/model protection: keep draining the subprocess pipe so the
    # child cannot block, but only emit a bounded amount of foreground output.
    # Lines are batched into ~4 KB chunks before yielding a single SSE event
    # so that verbose commands (zip, find, tar) don't produce thousands of
    # individual skill_output events that overwhelm the browser.
    # Total emitted text is capped at RESULT_PREVIEW_CHARS (20 K).
    emitted_chars = 0
    output_truncated = False
    output_cap = RESULT_PREVIEW_CHARS
    _BATCH_SIZE = 4096
    _batch_buf: list[str] = []
    _batch_len = 0

    def _flush_batch() -> Generator[dict[str, Any], None, None]:
        nonlocal _batch_buf, _batch_len
        if _batch_buf:
            yield _output_event(tag_id, "".join(_batch_buf))
            _batch_buf = []
            _batch_len = 0

    try:
        if proc.stdout is not None:
            for line in proc.stdout:
                # Hard deadline enforcement. The threading.Timer only *signals*
                # the process group (SIGTERM) — a child that traps/ignores it,
                # or holds the pipe open via a grandchild, would otherwise keep
                # streaming output to the model long past the timeout. So we
                # also check wall-clock here and SIGKILL, then stop reading.
                if time.time() - started > timeout:
                    kill_process_tree(proc.pid, sig=signal.SIGKILL)
                    break

                # Preserve editor command capture independently of UI output cap.
                if editor_target is not None and editor_chars < _EDITOR_OUTPUT_CAP:
                    remaining_editor = _EDITOR_OUTPUT_CAP - editor_chars
                    if remaining_editor > 0:
                        editor_chunks.append(line[:remaining_editor])
                        editor_chars += min(len(line), remaining_editor)

                if output_truncated:
                    continue

                # Cap total emitted characters
                remaining_cap = output_cap - emitted_chars
                if remaining_cap <= 0:
                    yield from _flush_batch()
                    _trunc_msg = (
                        f"\n[… output truncated after {output_cap:,} chars; "
                        "command is still running, further output is hidden …]\n"
                    )
                    yield _output_event(tag_id, _trunc_msg, "stderr")
                    output_truncated = True
                    continue

                # Truncate line if it would exceed the cap
                if len(line) > remaining_cap:
                    line = line[:remaining_cap]

                _batch_buf.append(line)
                _batch_len += len(line)
                emitted_chars += len(line)

                # Flush when batch buffer reaches target size
                if _batch_len >= _BATCH_SIZE:
                    yield from _flush_batch()

        # Flush any remaining buffered output
        if not output_truncated:
            yield from _flush_batch()
        proc.wait()
    finally:
        timer.cancel()

        # The timer is NOT the only thing that must stop here. This finally
        # runs on EVERY exit path — normal completion, an exception, and
        # critically GeneratorExit (the user pressing Stop, which closes the
        # SSE stream and throws GeneratorExit at the yield above). Previously
        # this block only cancelled the timer, i.e. it DISARMED the one thing
        # that would ever have killed the child, then walked away. Any command
        # still running at Stop time was orphaned deliberately.
        #
        # kill_process_tree is a no-op on an already-dead pid and never raises,
        # so this is safe to call unconditionally.
        try:
            if proc.stdout is not None:
                proc.stdout.close()
        except Exception:
            pass
        # Kill the captured GROUP, not merely the pid.
        #
        # The old `if proc.poll() is None:` guard was wrong in both directions:
        #   - Normal completion: the shell has already exited and been reaped
        #     by proc.wait(), so poll() is not None -> the kill was SKIPPED
        #     entirely. Every "nohup x &" inside a foreground command leaked
        #     its child. (Measured: 0.0s wall clock, child survived.)
        #   - Timeout/Stop: the leader is a zombie or already gone, and
        #     /proc/<pid> no longer resolves the group.
        # _kill_group uses the pgid captured at spawn, so it works on every
        # path. killpg on an empty group raises ESRCH and is swallowed.
        _kill_group(signal.SIGKILL)
        try:
            proc.poll()  # reap the leader if it has already exited
        except Exception:
            pass

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

    log_path = tmp_path(f"ghost_bg_{uuid.uuid4().hex}.log")
    log_file = log_path.open("w", encoding="utf-8")
    try:
        proc = subprocess.Popen(
            cmd,
            shell=True,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            cwd=home_dir(),
            **popen_kwargs(),
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

        # Optional tail line-count limit
        try:
            tail_lines = int(attrs.get("tail", "0") or 0)
        except (TypeError, ValueError):
            tail_lines = 0

        tail = ""
        if log_path.exists():
            try:
                data = log_path.read_text(errors="replace")
                if tail_lines > 0:
                    tail = "\n".join(data.splitlines()[-tail_lines:])
                else:
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


def handle_kill_command(
    tag_id: str, name: str, attrs: dict[str, str], content: str
) -> Generator[dict[str, Any], None, None]:
    """Terminate a background job started with bg=true, killing its process group."""
    started = time.time()
    pid_raw = attrs.get("pid") or content.strip()
    try:
        pid = int(pid_raw)
    except (TypeError, ValueError):
        yield _output_event(tag_id, "Missing or invalid 'pid'\n", "stderr")
        yield _end_event(tag_id, name, False, started, error="Missing pid")
        return

    if pid <= 0:
        yield _output_event(tag_id, "Invalid pid\n", "stderr")
        yield _end_event(tag_id, name, False, started, error="Invalid pid")
        return

    from engine.platform_paths import pid_exists

    if not pid_exists(pid):
        info = BG_JOBS.pop(pid, None)
        yield _output_event(tag_id, f"Process {pid} is not running" + (" (removed from job list)\n" if info else "\n"))
        yield _end_event(tag_id, name, True, started, {"pid": pid, "killed": False, "reason": "not running"})
        return

    sig_name = (attrs.get("signal") or "TERM").upper()
    import signal as _signal
    sig = _signal.SIGKILL if sig_name == "KILL" else _signal.SIGTERM

    from engine.process_utils import kill_process_tree

    try:
        kill_process_tree(pid, sig=sig)
    except Exception as exc:
        yield _output_event(tag_id, f"Failed to kill {pid}: {exc}\n", "stderr")
        yield _end_event(tag_id, name, False, started, error=str(exc))
        return

    info = BG_JOBS.get(pid)
    if info is not None:
        info["status"] = "killed"
    BG_JOBS.pop(pid, None)

    yield _output_event(tag_id, f"Sent SIG{sig_name} to process group {pid}\n")
    yield _end_event(tag_id, name, True, started, {"pid": pid, "killed": True, "signal": sig_name})
