import os
import signal
import threading

from server import app
import uvicorn
from engine.config import HOST, PORT


def _sigterm_deadman(signum, frame):
    """If graceful shutdown hasn't finished in 0.8s, force-exit."""
    def _kill():
        os._exit(1)
    t = threading.Timer(0.8, _kill)
    t.daemon = True
    t.start()


signal.signal(signal.SIGTERM, _sigterm_deadman)

if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, reload=False)