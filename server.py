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
