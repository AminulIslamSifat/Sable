import os
import sys
import signal
import threading

# ponytail: Windows cp1252 can't encode unicode in print() — force UTF-8
if sys.platform == "win32":
    # Detach from any parent console so no CMD window stays visible
    import ctypes
    try:
        ctypes.windll.kernel32.FreeConsole()
    except Exception:
        pass
    for _s in (sys.stdout, sys.stderr):
        if hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")

    # --- NUCLEAR POPUP KILL -------------------------------------------------
    # After FreeConsole() this process has NO console. Windows rule: any
    # console-subsystem child (cmd.exe, git.exe, uv.exe, python.exe ...) we
    # spawn will then ALLOCATE A BRAND-NEW VISIBLE CONSOLE unless we pass
    # CREATE_NO_WINDOW. That is exactly the "C:\...\Sable\engine" popup.
    #
    # Instead of hunting every subprocess.Popen site, monkeypatch Popen once
    # here so EVERY spawn in the entire process — execute_command, bg_jobs,
    # checkpoints, scraper, service_manager, future code, anything — always
    # gets CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP injected. Impossible
    # to bypass. One chokepoint. Zero popups, anywhere, ever.
    import subprocess as _sp
    _CREATE_NO_WINDOW = 0x08000000
    _CREATE_NEW_PROCESS_GROUP = 0x00000200
    _FORCE_FLAGS = _CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP
    _OrigPopen = _sp.Popen

    class _SilentPopen(_OrigPopen):
        def __init__(self, *args, **kwargs):
            # Only force flags when we are NOT going through a shell that
            # needs a visible console AND creationflags can be combined.
            # Always safe to OR in CREATE_NO_WINDOW for console children.
            kwargs["creationflags"] = int(kwargs.get("creationflags", 0)) | _FORCE_FLAGS
            super().__init__(*args, **kwargs)

    _sp.Popen = _SilentPopen
    # subprocess.run / .call / .check_* all build a Popen internally, so they
    # inherit the patch automatically. Nothing else to do.

from server import app
import uvicorn
from engine.config import HOST, PORT
from engine.service_manager import _write_pid


def _sigterm_deadman(signum, frame):
    """If graceful shutdown hasn't finished in 0.8s, force-exit."""
    def _kill():
        os._exit(1)
    t = threading.Timer(0.8, _kill)
    t.daemon = True
    t.start()


signal.signal(signal.SIGTERM, _sigterm_deadman)

if __name__ == "__main__":
    _write_pid()
    uvicorn.run(app, host=HOST, port=PORT, reload=False)
#
