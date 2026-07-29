from __future__ import annotations

import asyncio
import logging

_log_buffer: asyncio.Queue[str] = asyncio.Queue(maxsize=500)

class SSELogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            _log_buffer.put_nowait(msg)
        except asyncio.QueueFull:
            pass

_sse_handler = SSELogHandler()
_sse_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S"))
logging.getLogger().addHandler(_sse_handler)
logging.getLogger().setLevel(logging.DEBUG)