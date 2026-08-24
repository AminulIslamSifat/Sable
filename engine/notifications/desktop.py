"""OS-level desktop notifications via notify-send (Linux)."""
from __future__ import annotations

import asyncio
import logging
import shutil

logger = logging.getLogger("sable")

_NOTIFY_SEND = shutil.which("notify-send")


async def notify_desktop(title: str, body: str, *, urgency: str = "normal") -> None:
    """Fire a desktop notification via notify-send.

    No-op if notify-send is not installed or on non-Linux platforms.
    Runs in a thread executor so it never blocks the event loop.
    """
    if _NOTIFY_SEND is None:
        return

    cmd = ["notify-send", "-u", urgency, "--app-name=Sable", title, body]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
        if proc.returncode != 0:
            logger.warning(
                "[desktop-notify] notify-send exited %d: %s",
                proc.returncode,
                stderr.decode(errors="replace").strip(),
            )
    except asyncio.TimeoutError:
        logger.warning("[desktop-notify] notify-send timed out")
    except Exception as exc:
        logger.warning("[desktop-notify] Failed: %s", exc)
