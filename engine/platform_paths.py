"""Cross-platform path constants and helpers for Sable.

Centralizes all platform-dependent path logic so individual modules
don't need their own sys.platform checks scattered throughout.
"""

import os
import sys
import tempfile
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"

# ─── Home directory ──────────────────────────────────────────────────────────
def home_dir() -> str:
    """Return the user's home directory with OS-native path separators.

    Path.home() can return forward slashes on Windows when the HOME env var
    is set by Git Bash / MSYS2 / Cygwin.  os.path.normpath() guarantees
    native separators (backslash on Windows, forward slash on POSIX).
    """
    return os.path.normpath(str(Path.home()))


# ─── Temp directory ──────────────────────────────────────────────────────────
# On POSIX: /tmp  |  On Windows: %TEMP% (usually C:\Users\<user>\AppData\Local\Temp)
TMP_DIR = Path(tempfile.gettempdir())


def tmp_path(name: str) -> Path:
    """Return a platform-appropriate temp file path.

    Usage: tmp_path("dl_progress_abc123.json") → /tmp/dl_progress_abc123.json (POSIX)
                                                  %TEMP%/dl_progress_abc123.json (Windows)
    """
    return TMP_DIR / name


# ─── Shell discovery ─────────────────────────────────────────────────────────
def pick_shell() -> str:
    """Return the best available interactive shell for this platform."""
    if IS_WINDOWS:
        # Prefer pwsh > powershell > cmd
        import shutil
        for cand in ("pwsh", "powershell", "cmd"):
            if shutil.which(cand):
                return shutil.which(cand) or cand
        return "cmd.exe"
    else:
        for cand in ("/usr/bin/fish", "/bin/fish", "/bin/bash", "/bin/sh"):
            if os.path.isfile(cand):
                return cand
        return "/bin/sh"


# ─── Playwright Chrome discovery ─────────────────────────────────────────────
def playwright_chrome_globs() -> list[str]:
    """Return glob patterns for finding Playwright-bundled Chromium."""
    import glob as _glob

    if IS_WINDOWS:
        # Windows Playwright installs to %LOCALAPPDATA%/ms-playwright
        local_appdata = os.environ.get("LOCALAPPDATA", "")
        bases = []
        if local_appdata:
            bases.append(os.path.join(local_appdata, "ms-playwright"))
        bases.append(os.path.expanduser("~/AppData/Local/ms-playwright"))
        patterns = []
        for base in bases:
            patterns.extend([
                os.path.join(base, "chromium-*", "chrome-win", "chrome.exe"),
                os.path.join(base, "chromium-*", "chrome-win64", "chrome.exe"),
            ])
        return patterns
    else:
        return [
            os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux64/chrome"),
            os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux/chrome"),
        ]


def find_playwright_chrome() -> str | None:
    """Find Playwright-bundled Chromium binary, newest version first."""
    import glob as _glob

    for pattern in playwright_chrome_globs():
        matches = sorted(_glob.glob(pattern), reverse=True)
        if matches:
            return matches[0]
    return None


# ─── System Chrome discovery ─────────────────────────────────────────────────
def system_chrome_candidates() -> list[str]:
    """Return candidate paths/names for system-installed Chrome/Chromium."""
    if IS_WINDOWS:
        import winreg
        candidates = []
        # Check registry for Chrome install path
        for key_path in (
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",
            r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",
        ):
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
                    path, _ = winreg.QueryValueEx(key, "")
                    if os.path.isfile(path):
                        candidates.append(path)
            except (OSError, FileNotFoundError):
                pass
        # Common install locations
        program_files = os.environ.get("PROGRAMFILES", r"C:\Program Files")
        program_files_x86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
        local_appdata = os.environ.get("LOCALAPPDATA", "")
        for base in (program_files, program_files_x86, local_appdata):
            for sub in (
                r"Google\Chrome\Application\chrome.exe",
                r"Chromium\Application\chrome.exe",
                r"BraveSoftware\Brave-Browser\Application\brave.exe",
            ):
                full = os.path.join(base, sub)
                if os.path.isfile(full):
                    candidates.append(full)
        return candidates
    else:
        return [
            "google-chrome-stable",
            "google-chrome",
            "chromium-browser",
            "chromium",
            "/opt/google/chrome/chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/google-chrome",
            "/usr/bin/chromium",
            "/snap/bin/chromium",
        ]


# ─── Process liveness check ──────────────────────────────────────────────────
def pid_exists(pid: int) -> bool:
    """Check if a process with the given PID is alive (cross-platform)."""
    if IS_WINDOWS:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x0400, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False
    else:
        return Path(f"/proc/{pid}").exists()
