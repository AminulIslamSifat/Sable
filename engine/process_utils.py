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

# SIGKILL has no Windows equivalent. Callers that want a hard kill pass this;
# on Windows it degrades to SIGTERM, which taskkill escalates to /F anyway
# (see kill_process_tree).
SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)

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
        # taskkill /F /T is the only reliable way to kill a Windows process
        # tree: without /F it merely posts WM_CLOSE, which browsers, daemons
        # and console apps routinely ignore. Windows has no signal semantics,
        # so the POSIX graceful-vs-force distinction does not apply here.
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True, timeout=timeout,
            )
        except Exception:
            pass
        return

    # --- POSIX path ---
    if sig is None:
        sig = signal.SIGTERM

    # Resolve the process group BEFORE any liveness check.
    #
    # Why: when a shell does `cmd & exit` under start_new_session=True, the
    # shell IS the group leader (pgid == pid). It exits, becomes a ZOMBIE, and
    # is never reaped by us — yet its group is still valid and still holds the
    # live descendants we actually need to kill. A zombie-only guard here would
    # skip the signal entirely and leak the whole tree.
    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, PermissionError, OSError):
        pgid = None

    # killpg is only safe when WE own the group. If pgid == pid then we were
    # the setsid/start_new_session leader and the group is ours to destroy —
    # even if the leader is a zombie. If pgid != pid we did not create a
    # session, so the group belongs to our parent; killing it would take out
    # unrelated processes.
    if pgid is not None and pgid == pid:
        try:
            os.killpg(pgid, sig)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass

    # Fallback: single-pid kill. Here the zombie check IS correct — signalling
    # a dead-but-unreaped pid does nothing useful, and getpgid() may have
    # handed us a group we don't own.
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            data = fh.read()
        rparen = data.rfind(b")")
        state = data[rparen + 2:rparen + 3].decode("ascii", "ignore") if rparen != -1 else ""
    except (OSError, ValueError):
        return
    if state in ("Z", "X", "x"):
        return

    try:
        os.kill(pid, sig)
    except Exception:
        pass
