"""Native in-process browser automation.

No subprocess, no daemon, no unix socket, no CLI program. Playwright runs
inside the Sable process on a dedicated background event loop, so the browser
persists between tool calls while every call stays a plain synchronous method.
"""

from engine.browser.session import BrowserSession, get_session

__all__ = ["BrowserSession", "get_session"]