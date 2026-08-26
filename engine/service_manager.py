"""Cross-platform service management for Sable.

On Linux: delegates to systemctl --user.
On Windows: uses Task Scheduler ("Sable Server") for managed restart,
falling back to start.bat --background if the task doesn't exist.
Stop sends taskkill on Windows, systemctl stop on Linux.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
_SERVICE_NAME = "sable.service"
_PID_FILE = Path(__file__).resolve().parent.parent / ".sable_server.pid"


def _write_pid() -> None:
    """Write current PID so external tools can find us."""
    try:
        _PID_FILE.write_text(str(os.getpid()))
    except Exception:
        pass


def _read_pid() -> int | None:
    """Read PID from file, return None if missing/stale."""
    try:
        pid = int(_PID_FILE.read_text().strip())
        # Verify process exists
        if IS_WINDOWS:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x0400, False, pid)  # PROCESS_QUERY_INFORMATION
            if handle:
                kernel32.CloseHandle(handle)
                return pid
            return None
        else:
            os.kill(pid, 0)
            return pid
    except Exception:
        return None


def stop_service() -> None:
    """Stop the Sable server process."""
    if IS_WINDOWS:
        pid = _read_pid()
        if pid and pid != os.getpid():
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
            )
        else:
            # Self-stop: raise SIGINT equivalent
            os._exit(0)
    else:
        subprocess.Popen(
            ["systemctl", "--user", "stop", _SERVICE_NAME],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def _windows_task_exists(task_name: str) -> bool:
    """Check if a Windows scheduled task exists."""
    try:
        result = subprocess.run(
            ["schtasks", "/Query", "/TN", task_name],
            capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def restart_service() -> None:
    """Restart the Sable server process."""
    if IS_WINDOWS:
        # Kill current process first
        pid = os.getpid()
        project_root = Path(__file__).resolve().parent.parent
        start_bat = project_root / "start.bat"

        # Try Task Scheduler first (cleanest — hidden, managed)
        if _windows_task_exists("Sable Server"):
            # Stop existing task instance, then start fresh
            subprocess.run(
                ["schtasks", "/End", "/TN", "Sable Server"],
                capture_output=True, timeout=10,
            )
            subprocess.Popen(
                ["schtasks", "/Run", "/TN", "Sable Server"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            os._exit(0)

        # Fallback: launch via start.bat --background
        if start_bat.exists():
            subprocess.Popen(
                ["cmd", "/c", str(start_bat), "--background"],
                cwd=str(project_root),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=0x08000000,  # CREATE_NO_WINDOW
            )
            os._exit(0)

        # Last resort: raw re-exec (no window hiding)
        python = sys.executable
        args = [python] + sys.argv
        env = os.environ.copy()
        env["SABLE_RESTART"] = "1"
        subprocess.Popen(args, env=env, close_fds=True)
        os._exit(0)
    else:
        subprocess.Popen(
            ["systemctl", "--user", "restart", _SERVICE_NAME],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def is_systemd_available() -> bool:
    """Check if systemd user services are available."""
    if IS_WINDOWS:
        return False
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-system-running"],
            capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0 or "running" in result.stdout
    except Exception:
        return False
