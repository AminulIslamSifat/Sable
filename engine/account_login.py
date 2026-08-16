
"""Minimal headed browser for account login — no terminal interaction needed.

Launches a persistent Chromium profile (creates it if missing).
Session saves automatically when the user closes the browser window.

Usage:
    uv run python engine/account_login.py browser-data-acc3
    uv run python engine/account_login.py 3
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SYSTEM = _ROOT / "system"


def resolve_profile(arg: str) -> Path:
    if arg.isdigit():
        return _SYSTEM / f"browser-data-acc{arg}"
    if arg.startswith("browser-data-acc"):
        return _SYSTEM / arg
    p = Path(arg)
    return p if p.is_absolute() else _SYSTEM / arg


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: account_login.py <profile>")
        raise SystemExit(1)

    profile_dir = resolve_profile(sys.argv[1])
    profile_dir.mkdir(parents=True, exist_ok=True)

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=False,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://chat.qwen.ai")

        # Block until user closes the browser window
        try:
            page.wait_for_event("close", timeout=0)
        except Exception:
            pass
        finally:
            context.close()

    print(f"Session saved to {profile_dir}")


if __name__ == "__main__":
    main()
#
