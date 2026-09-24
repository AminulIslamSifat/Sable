"""Cross-platform process group management.

On POSIX, uses os.setsid / os.killpg for true process-group semantics.
On Windows, uses CREATE_NEW_PROCESS_GROUP + taskkill /T for equivalent
tree-kill behaviour.

Public API:
    IS_WINDOWS          – bool
    popen_kwargs()      – dict to merge into subprocess.Popen() calls
    kill_process_tree() – kill a process and all its children
"""

from __future__ import annotations

import signal
import subprocess
import sys
import os

IS_WINDOWS = sys.platform == "win32"

# Windows process-creation flags
_CREATE_NEW_PROCESS_GROUP = 0x00000200  # independent group for tree-kill
_CREATE_NO_WINDOW = 0x08000000          # suppress the cmd.exe console popup


def popen_kwargs() -> dict:
    """Return extra keyword arguments for ``subprocess.Popen`` that create an
    independent process group.

    On POSIX this is ``{"start_new_session": True}`` (a thread-safe equivalent
    of ``preexec_fn=os.setsid`` — ``preexec_fn`` is documented as unsafe in the
    presence of threads, and this engine dispatches from a threaded server).
    On Windows this is ``{"creationflags": CREATE_NEW_PROCESS_GROUP}``.
    """
    if IS_WINDOWS:
        return {"creationflags": _CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW}
    return {"start_new_session": True}


def kill_process_tree(
    pid: int,
    *,
    sig: int | None = None,
    timeout: float = 5.0,
) -> None:
    """Kill *pid* and all its descendants.

    On POSIX sends *sig* (default SIGTERM) to the process group via
    ``os.killpg``.  Falls back to ``os.kill`` if the group lookup fails.

    On Windows runs ``taskkill /F /T /PID <pid>`` which kills the entire
    process tree.

    This function never raises — all errors are silently swallowed so it's
    safe to call in cleanup/finally blocks.
    """
    if pid <= 0:
        return

    if IS_WINDOWS:
        try:
            flag = "/F"  # force
            if sig is not None and sig == signal.SIGTERM:
                flag = ""  # graceful first on Windows
            cmd = ["taskkill"]
            if flag:
                cmd.append(flag)
            cmd.extend(["/T", "/PID", str(pid)])
            subprocess.run(cmd, capture_output=True, timeout=timeout)
        except Exception:
            pass
        return

    # --- POSIX path ---
    if sig is None:
        sig = signal.SIGTERM

    try:
        pgid = os.getpgid(pid)
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(pid, sig)
        except Exception:
            pass
