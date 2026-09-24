"""Plain browser launcher for Sable's account / login flows.

These flows only need a real window the user can log in through, so the launch
command is deliberately minimal:

    <browser> --user-data-dir=<profile> <url> [<url> ...]

No Playwright, no automation/stealth flags — the same command a user would
type in a terminal. Playwright stays in use everywhere else (scraping,
control, session attach); this module exists so login windows stay clean.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)


def _resolve_binary(browser_path: str | None) -> str | None:
    """Turn a saved browser choice into an executable path.

    Falls back to the auto-detected Chrome-compatible browser when the saved
    choice is empty, 'default', or points at a binary that no longer exists.
    """
    from engine.platform_paths import find_best_chrome

    cand = (browser_path or "").strip()
    if cand and cand != "default":
        if os.path.isabs(cand):
            if os.path.isfile(cand):
                return cand
        else:
            resolved = shutil.which(cand)
            if resolved:
                return resolved
    return find_best_chrome()


async def launch_browser(
    browser_path: str | None,
    profile_dir: Path | str,
    urls: list[str],
    label: str = "",
) -> None:
    """Launch `browser_path` with only its profile dir and the given URLs.

    One tab per URL (Chromium opens each positional URL in its own tab).
    Runs in the calling task and returns when the browser window closes, so
    callers wrap it in create_task() to keep it off the request path.
    """
    binary = _resolve_binary(browser_path)
    tag = label or str(profile_dir)
    if not binary:
        logger.warning("Browser for %s: no browser binary found", tag)
        return

    cmd = [binary, f"--user-data-dir={profile_dir}", *urls]
    logger.info("Browser for %s: %s", tag, " ".join(cmd))
    try:
        proc = await asyncio.create_subprocess_exec(*cmd)
        code = await proc.wait()
        logger.info("Browser for %s exited (code=%s)", tag, code)
    except Exception as exc:
        logger.warning("Browser for %s failed to launch: %s", tag, exc)