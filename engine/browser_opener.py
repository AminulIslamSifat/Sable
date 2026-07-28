"""Qwen Browser Opener — launch headful browser with persistent ./browser-data profile.

Use this tool to:
- Log into Qwen or switch accounts
- Solve manual CAPTCHAs / WAF checks if needed
- Change settings or manage chat history
"""

import sys
from playwright.sync_api import sync_playwright

USER_DATA_DIR = "./browser-data"
QWEN_URL = "https://chat.qwen.ai"


def main() -> None:
    print(f" Opening Qwen in browser profile ({USER_DATA_DIR})...")
    print("Log in, switch accounts, or complete any checks in the browser window.")
    print("When done, press ENTER in this terminal to save session & close.\n")

    try:
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=USER_DATA_DIR,
                headless=False,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"]
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(QWEN_URL)

            try:
                input(" [Press ENTER when finished to save & exit] ")
            except (KeyboardInterrupt, EOFError):
                print("\nClosing browser...")

            context.close()
            print(" Session & cookies saved to ./browser-data. You can now run python chat.py! ")
    except Exception as e:
        print(f"[ERROR] Failed to launch browser: {e}")


if __name__ == "__main__":
    main()
