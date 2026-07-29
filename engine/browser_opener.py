
"""Qwen Browser Opener — launch headful browser with a persistent profile.

Use this tool to:
- Log into Qwen or switch accounts
- Solve manual CAPTCHAs / WAF checks if needed
- Change settings or manage chat history

Examples:
    # Open the default configured profile
    uv run python engine/browser_opener.py

    # Open system/browser-data-acc12 by full profile name
    uv run python engine/browser_opener.py browser-data-acc12

    # Open system/browser-data-acc12 by account number
    uv run python engine/browser_opener.py 12

    # Open an absolute profile path
    uv run python engine/browser_opener.py /home/sifat/hdd/projects/Sable/system/browser-data-acc12

    # Open a different starting URL
    uv run python engine/browser_opener.py 12 --url https://chat.deepseek.com
"""

from __future__ import annotations

import argparse
from pathlib import Path

from playwright.sync_api import sync_playwright

from engine.config import BROWSER_DATA_DIR

_ROOT = Path(__file__).resolve().parent.parent
_SYSTEM = _ROOT / "system"
DEFAULT_URL = "https://chat.qwen.ai"


def resolve_profile(profile: str | None) -> Path:
    """Resolve a CLI profile argument to an absolute user-data directory.

    Accepted forms:
    - None                  → configured BROWSER_DATA_DIR
    - "12"                  → system/browser-data-acc12
    - "browser-data-acc12"  → system/browser-data-acc12
    - "system/browser-data-acc12" → resolved relative to CWD or project root
    - "/abs/path/profile"   → used as-is
    """
    if not profile:
        return Path(str(BROWSER_DATA_DIR))

    if profile.isdigit():
        return _SYSTEM / f"browser-data-acc{profile}"

    p = Path(profile).expanduser()
    if p.is_absolute():
        return p

    # Relative path from current working directory
    if p.exists():
        return p.resolve()

    # Relative to project system/ directory
    system_candidate = _SYSTEM / profile
    if system_candidate.exists() or "/" not in profile:
        return system_candidate

    # Fall back to CWD-relative path
    return p.resolve()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Open a persistent Playwright Chromium profile for login/account setup."
    )
    parser.add_argument(
        "profile",
        nargs="?",
        default=None,
        help=(
            "Profile to open. Can be an account number (12), a profile name "
            "(browser-data-acc12), a relative path, or an absolute path. "
            "Defaults to the configured BROWSER_DATA_DIR."
        ),
    )
    parser.add_argument(
        "--url",
        default=DEFAULT_URL,
        help=f"Starting URL. Default: {DEFAULT_URL}",
    )
    args = parser.parse_args()

    user_data_dir = resolve_profile(args.profile)
    url = args.url

    print(f"🚀 Opening browser profile: {user_data_dir}")
    print(f"🌐 Starting URL: {url}")
    print("Log in, switch accounts, or complete any checks in the browser window.")
    print("When done, press ENTER in this terminal to save session & close.\n")

    try:
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(user_data_dir),
                headless=False,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(url)

            try:
                input("👉 [Press ENTER when finished to save & exit] ")
            except (KeyboardInterrupt, EOFError):
                print("\nClosing browser...")

            context.close()
            print(f"✨ Session & cookies saved to {user_data_dir}.")
    except Exception as e:
        print(f"[ERROR] Failed to launch browser: {e}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
