"""Cross-platform desktop notifications.

Linux: notify-send
Windows: win10toast_click (toast) or PowerShell BurntToast fallback
macOS: osascript
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import sys

logger = logging.getLogger("sable")

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"

_NOTIFY_SEND = None if IS_WINDOWS else shutil.which("notify-send")
# Three-state flag: None = never tried, True = available, False = unavailable.
_BURNTOAST_AVAILABLE: bool | None = None


async def _ensure_burnttoast() -> bool:
    """Auto-install BurntToast module if missing. Returns True if available.

    The install is attempted at most once per process lifetime regardless of
    outcome — prevents spawning a 60-second PowerShell process on every
    notification when BurntToast is genuinely unavailable (e.g. corp policy).
    """
    global _BURNTOAST_AVAILABLE
    if _BURNTOAST_AVAILABLE is not None:
        return _BURNTOAST_AVAILABLE

    check_cmd = (
        "if (Get-Module -ListAvailable -Name BurntToast) { exit 0 } "
        "else { "
        "Install-Module BurntToast -Scope CurrentUser -Force -ErrorAction Stop; "
        "exit 0 "
        "}"
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            "powershell", "-NoProfile", "-Command", check_cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode == 0:
            _BURNTOAST_AVAILABLE = True
            logger.info("[desktop-notify] BurntToast ready")
            return True
        logger.warning(
            "[desktop-notify] BurntToast install failed: %s",
            stderr.decode(errors="replace").strip(),
        )
    except asyncio.TimeoutError:
        logger.warning("[desktop-notify] BurntToast install timed out")
    except FileNotFoundError:
        logger.warning("[desktop-notify] powershell not found")
    # Mark as unavailable so we never retry this session
    _BURNTOAST_AVAILABLE = False
    return False


async def notify_desktop(title: str, body: str, *, urgency: str = "normal") -> None:
    """Fire a desktop notification using the platform-native method.

    No-op if no notification backend is available.
    Runs in a thread executor so it never blocks the event loop.
    """
    try:
        if IS_WINDOWS:
            await _notify_windows(title, body)
        elif IS_MACOS:
            await _notify_macos(title, body)
        else:
            await _notify_linux(title, body, urgency)
    except Exception as exc:
        logger.warning("[desktop-notify] Failed: %s", exc)


async def _notify_linux(title: str, body: str, urgency: str) -> None:
    """Linux: notify-send."""
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


async def _notify_macos(title: str, body: str) -> None:
    """macOS: osascript display notification."""
    script = f'display notification "{body}" with title "{title}"'
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.communicate(), timeout=5)
    except asyncio.TimeoutError:
        logger.warning("[desktop-notify] osascript timed out")


async def _notify_windows(title: str, body: str) -> None:
    """Windows: PowerShell toast notification via BurntToast or fallback to MessageBox."""
    # Auto-install BurntToast if missing (runs once, cached after)
    await _ensure_burnttoast()

    # Escape single quotes for PowerShell
    safe_title = title.replace("'", "''")
    safe_body = body.replace("'", "''")

    # Try BurntToast module first (modern toast notifications)
    ps_toast = (
        f"try {{ "
        f"Import-Module BurntToast -ErrorAction Stop; "
        f"New-BurntToastNotification -Text '{safe_title}','{safe_body}' "
        f"}} catch {{ "
        f"Add-Type -AssemblyName System.Windows.Forms; "
        f"[System.Windows.Forms.MessageBox]::Show('{safe_body}','{safe_title}') "
        f"}}"
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            "powershell", "-NoProfile", "-Command", ps_toast,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode != 0:
            logger.warning(
                "[desktop-notify] PowerShell notification failed: %s",
                stderr.decode(errors="replace").strip(),
            )
    except asyncio.TimeoutError:
        logger.warning("[desktop-notify] PowerShell notification timed out")
    except FileNotFoundError:
        logger.warning("[desktop-notify] powershell not found")
