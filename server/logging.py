"""Logging configuration and SSE log handler."""

import asyncio
import logging

from server.utils import sse

logger = logging.getLogger("sable")

# Live log buffer for /api/logs SSE endpoint
_log_buffer: asyncio.Queue[str] = asyncio.Queue(maxsize=500)


class SSELogHandler(logging.Handler):
    """Non-blocking handler that pushes formatted log records into _log_buffer."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            _log_buffer.put_nowait(msg)
        except asyncio.QueueFull:
            pass  # drop oldest if buffer is full (fire-and-forget)


_sse_handler = SSELogHandler()
_sse_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S"))
logging.getLogger().addHandler(_sse_handler)
logging.getLogger().setLevel(logging.DEBUG)


def get_log_buffer() -> asyncio.Queue[str]:
    return _log_buffer