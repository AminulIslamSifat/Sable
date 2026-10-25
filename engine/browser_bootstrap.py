#!/usr/bin/env python3
"""browser_bootstrap — guarantee a Playwright-controllable browser exists.

Policy (in order):
  1. A real system Chrome / Chromium / Edge  -> use it, download nothing.
  2. A Chrome-based browser (Brave, Vivaldi, Thorium, Helium) -> use it.
  3. Playwright's own Chromium already on disk -> use it.
  4. None of the above -> download Playwright Chromium ONCE, on first run.

The download is `--no-shell`, which fetches only the full Chromium build
(379 MB) and not the separate headless shell (another 262 MB). Full Chromium
runs headed for browser-login and headless for scraping, so one download
covers both. Total saved versus plain `playwright install chromium`: 267 MB.

Exposed as `python server.py --ensure-browser` so the Electron splash can run
it before starting the real server and stream progress to the user.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _emit(stage: str, detail: str = "") -> None:
    """One machine-readable progress line on stdout, consumed by the shell."""
    print(f"SABLE_BROWSER {stage} {detail}".rstrip(), flush=True)


def usable_browser() -> str | None:
    """Return a path/command for a browser Playwright can drive, or None.

    System Chrome and Edge are checked first — they cost zero bytes to use.
    """
    try:
        from engine.platform_paths import find_best_chrome
    except Exception as exc:  # pragma: no cover - defensive
        _emit("error", f"cannot import platform_paths: {exc}")
        return None
    return find_best_chrome()


def _driver_cli() -> list[str] | None:
    """Return [node, cli.js] for Playwright's bundled driver, or None."""
    try:
        from playwright._impl._driver import compute_driver_executable

        node, cli = compute_driver_executable()
        return [str(node), str(cli)]
    except Exception:
        return None


def download_chromium() -> bool:
    """Download Playwright Chromium (full build only). True on success."""
    cli = _driver_cli()
    if not cli:
        _emit("error", "playwright driver not found in this build")
        return False

    # Never default PLAYWRIGHT_BROWSERS_PATH to "0" — that means "bundle-local",
    # which is read-only inside an AppImage. Leave it unset so Playwright uses
    # its normal per-user cache (~/.cache/ms-playwright on Linux).
    env = dict(os.environ)
    cmd = [*cli, "install", "--no-shell", "chromium"]
    _emit("download", "starting")

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            bufsize=1,
        )
    except Exception as exc:
        _emit("error", f"spawn failed: {exc}")
        return False

    last = ""
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        last = line
        # Playwright prints "|■■■■■■  | 45% of 130.2 MiB" style progress.
        if "%" in line or "Downloading" in line or "downloaded" in line:
            _emit("progress", line.replace(" ", "_")[:160])

    rc = proc.wait()
    if rc != 0:
        _emit("error", f"playwright install exited {rc}: {last[:120]}")
        return False

    _emit("download", "done")
    return True


def ensure_browser() -> int:
    """Check, download if needed. Returns process exit code."""
    found = usable_browser()
    if found:
        _emit("found", found)
        return 0

    _emit("missing", "no system Chrome/Chromium/Edge detected")
    if not download_chromium():
        return 1

    found = usable_browser()
    if found:
        _emit("ready", found)
        return 0

    _emit("error", "download finished but no browser resolved")
    return 1


if __name__ == "__main__":
    sys.exit(ensure_browser())
