"""Central logging configuration for Sable.

Single source of truth for every logger in the project.

Design
------
- ``sable`` namespace  → structured app logs (console + rotating file + SSE).
  Level controlled by ``SABLE_LOG_LEVEL`` (default ``INFO``).
- Third-party libraries → forced to ``WARNING`` so httpx/playwright/curl_cffi
  don't drown the log with per-request chatter.
- The in-memory ``_log_buffer`` feeds ``GET /api/logs`` (SSE) in the UI.

Every module should do::

    from server.logging_setup import logger

or, for a scoped child::

    logger = logging.getLogger("sable.my_module")

Never use bare ``print()`` in library/server code — route through ``logger``.
"""
from __future__ import annotations

import asyncio
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths & level
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parent.parent

# Frozen builds mount the app read-only — logs must live somewhere writable.
# SABLE_PERSISTENT_ROOT is set by the Electron shell; without it this is the
# original project-relative path.
_WRITABLE_ROOT = Path(os.getenv("SABLE_PERSISTENT_ROOT") or _ROOT)
_LOG_DIR = _WRITABLE_ROOT / "output" / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)

_LEVEL = getattr(
    logging,
    os.environ.get("SABLE_LOG_LEVEL", "INFO").upper(),
    logging.INFO,
)

_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_DATE_FMT = "%H:%M:%S"
_FORMATTER = logging.Formatter(_FORMAT, datefmt=_DATE_FMT)

# ---------------------------------------------------------------------------
# SSE buffer (consumed by GET /api/logs)
# ---------------------------------------------------------------------------
_log_buffer: asyncio.Queue[str] = asyncio.Queue(maxsize=500)


class SSELogHandler(logging.Handler):
    """Push formatted sable records into an in-memory queue for the UI."""

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        try:
            _log_buffer.put_nowait(self.format(record))
        except asyncio.QueueFull:
            pass  # UI is slow — drop rather than block the event loop
        except Exception:  # never let logging crash the app
            self.handleError(record)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
_console = logging.StreamHandler()
_console.setFormatter(_FORMATTER)
_console.setLevel(_LEVEL)

_file = RotatingFileHandler(
    _LOG_DIR / "sable.log",
    maxBytes=5_000_000,
    backupCount=3,
    encoding="utf-8",
)
_file.setFormatter(_FORMATTER)
_file.setLevel(_LEVEL)

_sse = SSELogHandler()
_sse.setFormatter(_FORMATTER)
_sse.setLevel(_LEVEL)

# ---------------------------------------------------------------------------
# Root logger — third-party capture only (WARNING+)
# ---------------------------------------------------------------------------
_root = logging.getLogger()
_root.handlers.clear()
_root.setLevel(logging.WARNING)
_root.addHandler(_console)

# ---------------------------------------------------------------------------
# sable logger — everything app-level
# ---------------------------------------------------------------------------
logger = logging.getLogger("sable")
logger.handlers.clear()
logger.setLevel(_LEVEL)
logger.propagate = False  # own handlers; no double-emit through root
logger.addHandler(_console)
logger.addHandler(_file)
logger.addHandler(_sse)

# ---------------------------------------------------------------------------
# Silence noisy libraries
# ---------------------------------------------------------------------------
for _noisy in (
    "httpx",
    "httpcore",
    "urllib3",
    "asyncio",
    "playwright",
    "curl_cffi",
    "websockets",
    "PIL",
    "multipart",
    "uvicorn.access",
    "telethon",
    "aiosqlite",
    "charset_normalizer",
    "filelock",
):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
