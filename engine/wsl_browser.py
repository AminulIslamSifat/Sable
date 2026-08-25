"""WSL2 ↔ Windows Chrome bridge.

On WSL2, headed Playwright can't open a GUI (no display server).
Instead we launch Windows-side Chrome with --remote-debugging-port
and connect via CDP.  Linux is never affected — every public function
returns None / passes through when not on WSL2.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import NamedTuple


class WSLChromeSession(NamedTuple):
    cdp_url: str
    process: subprocess.Popen | None  # None if reusing existing
    user_data_dir_win: str             # Windows-style path


def is_wsl2() -> bool:
    """Return True only inside WSL2."""
    try:
        version = Path("/proc/version").read_text().lower()
        return "microsoft" in version and (
            "wsl2" in version or "microsoft-standard" in version
        )
    except Exception:
        return False


def wsl_to_win_path(posix_path: str) -> str:
    r"""Convert /home/sifat/... -> C:\Users\sifat\... via wslpath."""
    try:
        result = subprocess.run(
            ["wslpath", "-w", posix_path],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    # Manual fallback for /mnt/c/… style paths
    p = posix_path
    if p.startswith("/mnt/"):
        drive = p[5].upper()
        rest = p[6:].replace("/", chr(92))
        return f"{drive}:{rest}"
    return posix_path


def _find_windows_chrome() -> str | None:
    """Locate Chrome/Edge on the Windows side."""
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    # Also check LOCALAPPDATA via cmd.exe
    try:
        result = subprocess.run(
            ["cmd.exe", "/c", "echo %LOCALAPPDATA%"],
            capture_output=True, text=True, timeout=5,
        )
        local_appdata = result.stdout.strip()
        if local_appdata:
            candidates.insert(0, local_appdata + chr(92) + "Google" + chr(92) + "Chrome" + chr(92) + "Application" + chr(92) + "chrome.exe")
            candidates.insert(1, local_appdata + chr(92) + "Microsoft" + chr(92) + "Edge" + chr(92) + "Application" + chr(92) + "msedge.exe")
    except Exception:
        pass

    for win_path in candidates:
        # Check existence via cmd.exe (Windows paths aren't directly stat-able)
        try:
            r = subprocess.run(
                ["cmd.exe", "/c", f'if exist "{win_path}" echo YES'],
                capture_output=True, text=True, timeout=5,
            )
            if "YES" in r.stdout:
                return win_path
        except Exception:
            continue
    return None


def _is_cdp_alive(port: int) -> bool:
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            return s.connect_ex(("127.0.0.1", port)) == 0
    except Exception:
        return False


def _find_free_port(start: int = 9301, end: int = 9399) -> int:
    """Return first available TCP port in [start, end]."""
    import socket
    for port in range(start, end):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", port))
                return port
        except OSError:
            continue
    return start  # fallback


def launch_windows_chrome(
    user_data_dir: str,
    port: int = 9222,
    *,
    headless: bool = False,
    extra_args: list[str] | None = None,
) -> WSLChromeSession | None:
    """Launch Windows Chrome with CDP on *port*.

    Returns ``None`` when not on WSL2 or no Windows Chrome found.
    The caller should then fall back to normal Playwright launch.
    """
    if not is_wsl2():
        return None

    chrome_exe = _find_windows_chrome()
    if not chrome_exe:
        return None

    # Reuse if CDP already alive on this port (same profile assumed)
    if _is_cdp_alive(port):
        win_dir = wsl_to_win_path(user_data_dir)
        return WSLChromeSession(
            cdp_url=f"http://127.0.0.1:{port}",
            process=None,
            user_data_dir_win=win_dir,
        )

    # If port is taken by something else, find a free one
    import socket as _sock
    try:
        with _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", port))
    except OSError:
        port = _find_free_port()

    win_data_dir = wsl_to_win_path(user_data_dir)

    cmd = [
        chrome_exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={win_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
    ]
    if headless:
        cmd.append("--headless=new")
    if extra_args:
        cmd.extend(extra_args)

    # Launch via cmd.exe /c start so the Windows process detaches from WSL.
    # Direct .exe execution from WSL works but ties the process to this Python
    # session; `start /b` makes it independent.
    win_cmd = " ".join(f'"{c}"' for c in cmd)
    proc = subprocess.Popen(
        ["cmd.exe", "/c", "start", "/b", win_cmd],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Wait for CDP to become available
    for _ in range(30):  # up to 6 seconds
        if _is_cdp_alive(port):
            break
        time.sleep(0.2)
    else:
        # CDP never came up
        proc.kill()
        return None

    return WSLChromeSession(
        cdp_url=f"http://127.0.0.1:{port}",
        process=proc,
        user_data_dir_win=win_data_dir,
    )
