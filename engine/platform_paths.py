"""Cross-platform path constants and helpers for Sable.

Centralizes all platform-dependent path logic so individual modules
don't need their own sys.platform checks scattered throughout.
"""

import os
import sys
import tempfile
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"

# Project root (parent of engine/ directory)
PROJECT_ROOT = Path(__file__).resolve().parent.parent

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

    Priority: real Chrome/Chromium → Chrome-based (Thorium, Helium, Brave, etc.) → Playwright bundled.
    Returns an absolute path or a command name resolvable via PATH, or None.
    """
    import shutil

    # 1. Real Chrome / Chromium
    for cand in system_chrome_candidates():
        resolved = shutil.which(cand) if not os.path.isabs(cand) else cand
        if resolved and os.path.isfile(resolved):
            return resolved

    # 2. Chrome-based browsers (Thorium, Helium, Brave, Vivaldi, Edge)
    for cand in chrome_based_candidates():
        resolved = shutil.which(cand) if not os.path.isabs(cand) else cand
        if resolved and os.path.isfile(resolved):
            return resolved

    # 3. Playwright-bundled Chromium
    return find_playwright_chrome()


def list_available_browsers() -> list[dict[str, str]]:
    """Return all detected browsers with metadata for the frontend picker.

    Returns a list of dicts: [{"path": ..., "name": ..., "type": ...}, ...]
    Always includes a 'default' entry at the top.
    """
    import shutil

    results: list[dict[str, str]] = [
        {"path": "default", "name": "Default (Auto-detect)", "type": "default"},
    ]
    seen: set[str] = set()

    def _add(cand: str, btype: str) -> None:
        resolved = shutil.which(cand) if not os.path.isabs(cand) else cand
        if resolved and os.path.isfile(resolved) and resolved not in seen:
            seen.add(resolved)
            name = Path(resolved).stem.replace("-", " ").title()
            results.append({"path": resolved, "name": name, "type": btype})

    for c in system_chrome_candidates():
        _add(c, "chrome")
    for c in chrome_based_candidates():
        _add(c, "chrome-based")

    pw = find_playwright_chrome()
    if pw and pw not in seen:
        results.append({"path": pw, "name": "Playwright Bundled", "type": "playwright"})

    return results


def extra_browser_args(browser_path: str | None) -> list[str]:
    """Return extra Chrome flags needed for specific browser binaries."""
    if not browser_path:
        return []
    lower = browser_path.lower()
    args: list[str] = []
    if "msedge" in lower or "edge" in lower:
        args.append("--disable-features=msEdgeNewProfileOnboarding")
    return args


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
    if not saved:
        # No saved browser — use Playwright bundled Chromium directly
        return find_playwright_chrome()
    if saved == "default":
        return None  # Let Playwright use its own default
    if os.path.isfile(saved):
        return saved
    # Saved path was deleted — fall back to Playwright
    return find_playwright_chrome()


def is_browser_available(browser_path: str | None) -> bool:
    """Check if a saved browser path still exists on disk."""
    if not browser_path or browser_path == "default":
        return True  # default always "available"
    import shutil
    if os.path.isabs(browser_path):
        return os.path.isfile(browser_path)
    return shutil.which(browser_path) is not None


# ─── Browser version → UA fingerprint derivation ────────────────────────────
# Chrome 110+ ships a *reduced* UA: the version is collapsed to
# `<major>.0.0.0`, and the same major drives the sec-ch-ua brand list.
# So the only thing we actually need from the binary is its Chromium major.
_UA_VERSION_CACHE: dict[str, str | None] = {}


def get_browser_chromium_version(browser_path: str | None) -> str | None:
    """Return the Chromium version string for a browser binary (cached).

    Accepts a path, a command name, or None (→ Playwright bundled Chromium).
    Returns e.g. '153.0.8010.36', or None if it could not be determined.
    """
    if browser_path in _UA_VERSION_CACHE:
        return _UA_VERSION_CACHE[browser_path]

    import re
    import shutil
    import subprocess

    resolved: str | None
    if not browser_path or browser_path == "default":
        resolved = find_playwright_chrome()
    else:
        resolved = shutil.which(browser_path) if not os.path.isabs(browser_path) else browser_path

    version: str | None = None
    if resolved and (os.path.isfile(resolved) or shutil.which(resolved)):
        try:
            out = subprocess.run(
                [resolved, "--version"],
                capture_output=True, text=True, timeout=15,
            )
            text = f"{out.stdout}\n{out.stderr}"
            # Prefer the version that follows the Chromium/Chrome keyword, since
            # vendor strings precede it — e.g. Helium prints
            # "Helium 0.17.0.1 (Chromium 153.0.8010.36)" and we want 153.x,
            # and Chrome-for-Testing prints
            # "Google Chrome for Testing 149.0.7827.55".
            m = re.search(
                r"(?:Chromium|Chrome)[^\d\n]{0,25}(\d+(?:\.\d+){1,3})",
                text,
            )
            if not m:
                # Last resort: first 3–4 part version anywhere in the output.
                m = re.search(r"(\d+\.\d+\.\d+(?:\.\d+)?)", text)
            if m:
                version = m.group(1)
        except Exception:
            version = None

    _UA_VERSION_CACHE[browser_path] = version
    return version


def get_account_browser_version(profile_name: str) -> str | None:
    """Chromium version of the browser that *created* the given account."""
    return get_browser_chromium_version(get_account_browser_path(profile_name))


def derive_ua_fingerprint(chromium_version: str | None) -> tuple[str, str]:
    """Derive (user_agent, sec-ch-ua) from a Chromium version string.

    Falls back to a safe modern default when the version is unknown.
    """
    major = "149"
    if chromium_version:
        parts = chromium_version.split(".")
        if parts and parts[0].isdigit():
            major = parts[0]

    ua = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
    )
    sec_ch_ua = f'"Chromium";v="{major}", "Not)A;Brand";v="24"'
    return ua, sec_ch_ua


def get_account_ua_fingerprint(profile_name: str | None) -> tuple[str, str]:
    """(user_agent, sec-ch-ua) for the browser that created this account."""
    version = None
    if profile_name:
        version = get_account_browser_version(profile_name)
    else:
        version = get_browser_chromium_version(None)
    return derive_ua_fingerprint(version)


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
