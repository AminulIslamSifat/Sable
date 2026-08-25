"""WSL2 <-> Windows Chrome bridge.

On WSL2, headed Playwright can't open a GUI (no display server).
Instead we launch Windows-side Chrome with --remote-debugging-port
and connect via CDP.  Linux is never affected -- every public function
returns None / passes through when not on WSL2.

If no Chrome/Edge is installed on Windows, automatically downloads
a portable Chromium for Windows from Playwright's CDN.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import NamedTuple

_BS = chr(92)  # backslash, avoids escape issues in string literals

# Where bundled Windows Chromium lives (inside WSL filesystem)
_BUNDLED_DIR = Path(__file__).resolve().parent.parent / "system" / "chromium-win"
_BUNDLED_EXE = _BUNDLED_DIR / "chrome-win64" / "chrome.exe"


class WSLChromeSession(NamedTuple):
    cdp_url: str
    process: subprocess.Popen | None  # None if reusing existing
    user_data_dir_win: str             # Windows-style path


# ---------------------------------------------------------------------------
# WSL2 detection
# ---------------------------------------------------------------------------

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
    r"""Convert /home/user/... -> C:\Users\user\... via wslpath."""
    try:
        result = subprocess.run(
            ["wslpath", "-w", posix_path],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    # Manual fallback for /mnt/c/... style paths
    p = posix_path
    if p.startswith("/mnt/"):
        drive = p[5].upper()
        rest = p[6:].replace("/", _BS)
        return f"{drive}:{rest}"
    return posix_path


# ---------------------------------------------------------------------------
# Portable Chromium download (Windows build from Playwright CDN)
# ---------------------------------------------------------------------------

def _get_playwright_chromium_info() -> tuple[str, int] | None:
    """Return (version, revision) that the installed Playwright expects."""
    try:
        from playwright._impl._driver import compute_driver_executable
        driver = compute_driver_executable()
        r = subprocess.run(
            [driver[0], driver[1], "install", "--dry-run"],
            capture_output=True, text=True, timeout=15,
        )
        # Parse first line: "Chrome for Testing 149.0.7827.55 (playwright chromium v1228)"
        for line in r.stdout.splitlines():
            if "Chrome for Testing" in line and "chromium v" in line:
                parts = line.split()
                version = parts[3]  # e.g. "149.0.7827.55"
                rev_str = line.split("chromium v")[-1].rstrip(")")
                revision = int(rev_str)
                return version, revision
    except Exception:
        pass
    return None


def _ensure_windows_chromium() -> Path | None:
    """Download + extract Windows Chromium if not already present.

    Returns path to chrome.exe or None on failure.
    Prints progress to stderr so the user sees what's happening.
    """
    if _BUNDLED_EXE.exists():
        return _BUNDLED_EXE

    info = _get_playwright_chromium_info()
    if not info:
        print("[wsl_browser] Could not determine Playwright Chromium version", file=sys.stderr)
        return None

    version, revision = info
    url = f"https://cdn.playwright.dev/builds/cft/{version}/win64/chrome-win64.zip"
    dest_zip = _BUNDLED_DIR / "chrome-win64.zip"

    _BUNDLED_DIR.mkdir(parents=True, exist_ok=True)

    print(f"[wsl_browser] No Windows Chrome found. Downloading Chromium {version} for Windows...", file=sys.stderr)
    print(f"[wsl_browser] URL: {url}", file=sys.stderr)

    # Download with curl (available on all WSL installs)
    dl = subprocess.run(
        ["curl", "-fSL", "--progress-bar", "-o", str(dest_zip), url],
        timeout=300,
    )
    if dl.returncode != 0:
        print(f"[wsl_browser] Download failed (exit {dl.returncode})", file=sys.stderr)
        return None

    print(f"[wsl_browser] Extracting...", file=sys.stderr)
    try:
        with zipfile.ZipFile(dest_zip, "r") as zf:
            zf.extractall(_BUNDLED_DIR)
    except Exception as exc:
        print(f"[wsl_browser] Extraction failed: {exc}", file=sys.stderr)
        return None
    finally:
        dest_zip.unlink(missing_ok=True)

    if _BUNDLED_EXE.exists():
        print(f"[wsl_browser] Chromium ready at {_BUNDLED_EXE}", file=sys.stderr)
        return _BUNDLED_EXE

    print("[wsl_browser] chrome.exe not found after extraction", file=sys.stderr)
    return None


# ---------------------------------------------------------------------------
# Chrome discovery
# ---------------------------------------------------------------------------

def _find_windows_chrome() -> str | None:
    """Locate Chrome/Edge on Windows, or fall back to bundled Chromium."""
    candidates = [
        f"C:{_BS}Program Files{_BS}Google{_BS}Chrome{_BS}Application{_BS}chrome.exe",
        f"C:{_BS}Program Files (x86){_BS}Google{_BS}Chrome{_BS}Application{_BS}chrome.exe",
        f"C:{_BS}Program Files (x86){_BS}Microsoft{_BS}Edge{_BS}Application{_BS}msedge.exe",
        f"C:{_BS}Program Files{_BS}Microsoft{_BS}Edge{_BS}Application{_BS}msedge.exe",
    ]
    # Also check LOCALAPPDATA via cmd.exe
    try:
        result = subprocess.run(
            ["cmd.exe", "/c", "echo %LOCALAPPDATA%"],
            capture_output=True, text=True, timeout=5,
        )
        local_appdata = result.stdout.strip()
        if local_appdata:
            candidates.insert(0, f"{local_appdata}{_BS}Google{_BS}Chrome{_BS}Application{_BS}chrome.exe")
            candidates.insert(1, f"{local_appdata}{_BS}Microsoft{_BS}Edge{_BS}Application{_BS}msedge.exe")
    except Exception:
        pass

    for win_path in candidates:
        try:
            r = subprocess.run(
                ["cmd.exe", "/c", f'if exist "{win_path}" echo YES'],
                capture_output=True, text=True, timeout=5,
            )
            if "YES" in r.stdout:
                return win_path
        except Exception:
            continue

    # Last resort: bundled portable Chromium
    bundled = _ensure_windows_chromium()
    if bundled:
        return wsl_to_win_path(str(bundled))

    return None


# ---------------------------------------------------------------------------
# CDP helpers
# ---------------------------------------------------------------------------

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
    return start


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def launch_windows_chrome(
    user_data_dir: str,
    port: int = 9222,
    *,
    headless: bool = False,
    extra_args: list[str] | None = None,
) -> WSLChromeSession | None:
    """Launch Windows Chrome with CDP on *port*.

    Returns ``None`` when not on WSL2 or no Windows Chrome could be obtained.
    The caller should then fall back to normal Playwright launch.
    """
    if not is_wsl2():
        return None

    chrome_exe = _find_windows_chrome()
    if not chrome_exe:
        print("[wsl_browser] No Windows Chrome/Edge/bundled Chromium available", file=sys.stderr)
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
        try:
            proc.kill()
        except Exception:
            pass
        return None

    return WSLChromeSession(
        cdp_url=f"http://127.0.0.1:{port}",
        process=proc,
        user_data_dir_win=win_data_dir,
    )
