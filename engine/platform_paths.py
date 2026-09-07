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


# ─── Chrome-based browser discovery (Thorium, Brave, etc.) ──────────────────
def chrome_based_candidates() -> list[str]:
    """Return candidate paths/names for Chrome-based browsers (not Chrome itself)."""
    if IS_WINDOWS:
        program_files = os.environ.get("PROGRAMFILES", r"C:\Program Files")
        program_files_x86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
        local_appdata = os.environ.get("LOCALAPPDATA", "")
        candidates = []
        for base in (program_files, program_files_x86, local_appdata):
            for sub in (
                r"Thorium\thorium.exe",
                r"Thorium Browser\thorium.exe",
                r"Helium\helium.exe",
                r"Helium Browser\helium.exe",
                r"BraveSoftware\Brave-Browser\Application\brave.exe",
                r"Vivaldi\Application\vivaldi.exe",
                r"Microsoft\Edge\Application\msedge.exe",
            ):
                full = os.path.join(base, sub)
                if os.path.isfile(full):
                    candidates.append(full)
        return candidates
    else:
        return [
            "thorium-browser",
            "thorium",
            "/opt/chromium.org/thorium/thorium",
            "/usr/bin/thorium-browser",
            "helium-browser",
            "helium",
            "/opt/helium-browser/helium",
            "/usr/bin/helium-browser",
            "brave-browser",
            "brave",
            "/opt/brave.com/brave/brave-browser",
            "vivaldi",
            "vivaldi-stable",
            "/opt/vivaldi/vivaldi",
            "microsoft-edge",
            "microsoft-edge-stable",
            "/opt/microsoft/msedge/msedge",
        ]


def find_best_chrome() -> str | None:
    """Find the best available Chrome-compatible browser.

    Priority: real Chrome/Chromium → Chrome-based (Thorium, Brave, etc.) → Playwright bundled.
    Returns an absolute path or a command name resolvable via PATH, or None.
    """
    import shutil

    # 1. Real Chrome / Chromium
    for cand in system_chrome_candidates():
        resolved = shutil.which(cand) if not os.path.isabs(cand) else cand
        if resolved and os.path.isfile(resolved):
            return resolved

    # 2. Chrome-based browsers (Thorium, Brave, Vivaldi, Edge)
    for cand in chrome_based_candidates():
        resolved = shutil.which(cand) if not os.path.isabs(cand) else cand
        if resolved and os.path.isfile(resolved):
            return resolved

    # 3. Playwright-bundled Chromium
    return find_playwright_chrome()


# ─── Per-account browser resolution ─────────────────────────────────────────
def get_account_browser_path(profile_name: str) -> str | None:
    """Read the saved browser path for a profile from accounts.json.

    Returns None if no browser is configured (meaning 'use default/auto-detect').
    Returns the string 'default' if user explicitly chose system default browser.
    """
    import json
    accounts_json = PROJECT_ROOT / "system" / "accounts.json"
    try:
        cfg = json.loads(accounts_json.read_text())
        return cfg.get(profile_name, {}).get("browser_path") or None
    except Exception:
        return None


def resolve_browser_for_profile(profile_name: str) -> str | None:
    """Resolve which browser binary to use for a given account profile.

    Priority:
      1. Saved browser path in accounts.json
      2. Auto-detected best Chrome-compatible browser via find_best_chrome()

    Returns an absolute path, a command name, or None.
    If the saved path is 'default', returns None (caller should NOT set executable_path,
    letting Playwright use its bundled default).
    """
    saved = get_account_browser_path(profile_name)
    if saved == "default":
        return None  # Let Playwright use its own default
    if saved and os.path.isfile(saved):
        return saved
    # Fall back to auto-detect (saved path may have been deleted)
    return find_best_chrome()


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
