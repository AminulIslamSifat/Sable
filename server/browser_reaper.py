"""server/browser_reaper.py — periodic cleanup of orphaned Sable browsers.

The governor caps *live* browsers at 2 and confines each to 1 GiB, but a Sable
process that dies mid-session (crash, SIGKILL, systemd restart) leaves its
browser children re-parented to init. Nothing owns them any more, they hold
their profile locks, and they accumulate — that is how hundreds of stale
profiles built up.

This task sweeps them on a timer. The governor's slot table is the source of
truth: any Sable browser whose pid is not claimed by a live slot is an orphan.
"""

from __future__ import annotations

import asyncio
import logging
import os

from engine import browser_governor

logger = logging.getLogger("sable.browser_reaper")

# How often to sweep. Cheap (a /proc scan), so a short interval is fine.
_INTERVAL_SECONDS = float(os.environ.get("SABLE_REAP_INTERVAL", "120"))

_task: asyncio.Task | None = None


async def _reaper_loop() -> None:
    logger.info("browser reaper started (interval=%ds)", int(_INTERVAL_SECONDS))
    # Small initial delay so startup's own browser launches have claimed their
    # slots before the first sweep runs — otherwise we would kill them.
    await asyncio.sleep(30)
    while True:
        try:
            killed = await asyncio.to_thread(browser_governor.reap_orphans)
            if killed:
                logger.warning("browser reaper reclaimed %d orphaned browser(s)", killed)
        except asyncio.CancelledError:
            logger.info("browser reaper stopped")
            raise
        except Exception as exc:
            logger.warning("browser reaper sweep failed: %s: %s", type(exc).__name__, exc)
        await asyncio.sleep(_INTERVAL_SECONDS)


def start_browser_reaper() -> None:
    """Start the sweep task. Safe to call multiple times."""
    global _task
    if _task is not None and not _task.done():
        return
    loop = asyncio.get_running_loop()
    _task = loop.create_task(_reaper_loop(), name="browser-reaper")
    logger.info("browser reaper task created")


def stop_browser_reaper() -> None:
    """Cancel the sweep task."""
    global _task
    if _task and not _task.done():
        _task.cancel()
        _task = None
        logger.info("browser reaper stopped")
