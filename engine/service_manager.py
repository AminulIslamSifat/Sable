"""Cross-platform service management for Sable.

On Linux: delegates to systemctl --user.
On Windows: uses direct process signaling (Sable runs as a foreground process,
not as a Windows service). Stop sends SIGTERM/Ctrl-C, restart re-executes.
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


def restart_service() -> None:
    """Restart the Sable server process."""
    if IS_WINDOWS:
        # On Windows, we re-exec ourselves. The old process exits,
        # and start.ps1 or the parent terminal keeps the new one alive.
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
