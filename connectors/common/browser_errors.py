"""connectors/common/browser_errors.py — typed browser failure taxonomy.

Every browser-side failure a scraper connector can hit is classified into a
stable string code. The recovery layer (``BaseScraperConnector._recover_engine``)
uses that code to decide whether a restart can fix it, whether the turn must be
re-sent, and whether the user has to intervene.

Codes are stable identifiers — safe to surface to the UI and to log.
"""
from __future__ import annotations

# ── failure codes ──────────────────────────────────────────────────────────

SLOT_FULL = "slot_full"                # governor has no free browser slot
BROWSER_DEAD = "browser_dead"          # process exited / port refused
CDP_TIMEOUT = "cdp_timeout"            # browser never exposed the CDP endpoint
CDP_FAILED = "cdp_failed"              # CDP reachable but Playwright cannot attach
PROFILE_LOCKED = "profile_locked"      # SingletonLock held by another process
NO_BROWSER_BINARY = "no_browser_binary"  # no Chrome-compatible binary installed
CONTEXT_CLOSED = "context_closed"      # TargetClosedError on the shared context
PAGE_CLOSED = "page_closed"            # our tab was closed underneath us
SESSION_NOT_FOUND = "session_not_found"  # upstream chat URL no longer resolves
NOT_LOGGED_IN = "not_logged_in"        # redirected to the login wall
SEND_FAILED = "send_failed"            # composer never accepted the message
INPUT_MISSING = "input_missing"        # composer textarea not found
CAPTURE_TIMEOUT = "capture_timeout"    # no response started within the deadline
CAPTURE_EMPTY = "capture_empty"        # stream finished but yielded no text
ENGINE_DEAD = "engine_dead"            # engine object unusable
RATE_LIMITED = "rate_limited"          # upstream refused: too many messages
UNKNOWN = "unknown"

ALL_CODES = (
    SLOT_FULL, BROWSER_DEAD, CDP_TIMEOUT, CDP_FAILED, PROFILE_LOCKED,
    NO_BROWSER_BINARY, CONTEXT_CLOSED, PAGE_CLOSED, SESSION_NOT_FOUND,
    NOT_LOGGED_IN, SEND_FAILED, INPUT_MISSING, CAPTURE_TIMEOUT,
    CAPTURE_EMPTY, ENGINE_DEAD, RATE_LIMITED, UNKNOWN,
)

# A browser restart (kill + relaunch + re-navigate) can plausibly fix these.
RECOVERABLE = {
    SLOT_FULL, BROWSER_DEAD, CDP_TIMEOUT, CDP_FAILED, PROFILE_LOCKED,
    CONTEXT_CLOSED, PAGE_CLOSED, SESSION_NOT_FOUND, SEND_FAILED,
    INPUT_MISSING, CAPTURE_TIMEOUT, CAPTURE_EMPTY, ENGINE_DEAD, UNKNOWN,
}

# Retrying these without human action just burns time — the browser is fine,
# the account is not.
#
# RATE_LIMITED belongs here even though a *different account* could succeed:
# relaunching the SAME browser and re-sending the SAME message to a limiter that
# just said "Messages too frequent. Try again later." cannot possibly work, and
# each retry re-hits the locked endpoint. Account switching is the chat route's
# job — it needs this code surfaced as a `rate_limited` event, not swallowed by
# a browser-recovery retry loop.
NEEDS_USER = {NO_BROWSER_BINARY, NOT_LOGGED_IN}

# Codes that must NEVER be retried by the connector's recovery loop.
NO_RETRY = {RATE_LIMITED}

# Failures where the turn ALREADY reached upstream. Re-sending would duplicate
# the message, so recovery retries only the capture phase.
CAPTURE_ONLY = {CAPTURE_EMPTY, CAPTURE_TIMEOUT}

# Human-readable one-liners for the UI.
DESCRIPTIONS = {
    SLOT_FULL: "every browser slot is taken by a live chat session",
    BROWSER_DEAD: "the browser process is gone",
    CDP_TIMEOUT: "the browser never opened its debug port",
    CDP_FAILED: "could not attach to the browser",
    PROFILE_LOCKED: "another process holds this browser profile",
    NO_BROWSER_BINARY: "no Chrome-compatible browser is installed",
    CONTEXT_CLOSED: "the browser context was closed",
    PAGE_CLOSED: "the chat tab was closed",
    SESSION_NOT_FOUND: "the upstream chat session no longer exists",
    NOT_LOGGED_IN: "the browser profile is logged out",
    SEND_FAILED: "the browser could not send the message",
    INPUT_MISSING: "the composer box was not found",
    CAPTURE_TIMEOUT: "the model never started responding",
    CAPTURE_EMPTY: "the response stream produced no text",
    ENGINE_DEAD: "the browser engine is unusable",
    RATE_LIMITED: "the account is rate limited — too many messages, try again later",
    UNKNOWN: "an unrecognised browser error",
}


class BrowserError(RuntimeError):
    """A classified browser failure, carrying its stable code."""

    def __init__(self, code: str, message: str = "", *, cause: BaseException | None = None) -> None:
        self.code = code
        self.message = message or DESCRIPTIONS.get(code, code)
        super().__init__(self.message)
        if cause is not None:
            self.__cause__ = cause

    @property
    def recoverable(self) -> bool:
        return self.code in RECOVERABLE

    @property
    def needs_user(self) -> bool:
        return self.code in NEEDS_USER


def recoverable(code: str) -> bool:
    return code in RECOVERABLE


def needs_user(code: str) -> bool:
    return code in NEEDS_USER


def no_retry(code: str) -> bool:
    """True when the recovery loop must surface this code instead of retrying."""
    return code in NO_RETRY


def describe(code: str) -> str:
    return DESCRIPTIONS.get(code, code)


# Most-specific first: the first pattern that matches wins. Order matters —
# "browser has been closed" must resolve to CONTEXT_CLOSED, not BROWSER_DEAD.
_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    # Must precede CAPTURE_EMPTY/BROWSER_DEAD: a soft rate-limit banner leaves
    # the assistant bubble empty, so without this the failure classifies as a
    # retryable capture problem and gets hammered 3x.
    (RATE_LIMITED, (
        "messages too frequent", "too many requests", "try again later",
        "rate limit", "ratelimit", "rate_limit", "usage limit",
        "quota exceeded", "daily usage",
    )),
    (SLOT_FULL, (
        "noslotavailable", "no browser slot free", "slots are held by live chat",
        "browser slots are held", "all 2 browser slots",
    )),
    (NO_BROWSER_BINARY, (
        "no chrome-compatible browser", "no browser found",
        "executable doesn't exist", "no such file or directory: 'chrom",
    )),
    (PROFILE_LOCKED, (
        "singletonlock", "profile appears to be in use", "processsingleton",
        "cannot create a file when that file already exists",
    )),
    (CDP_TIMEOUT, ("did not expose cdp", "cdp ready")),
    (CDP_FAILED, (
        "cdp connect failed", "connect_over_cdp", "websocket url",
        "browser_type.launch", "failed to connect to browser",
    )),
    (CONTEXT_CLOSED, (
        "target page, context or browser has been closed",
        "context or browser has been closed", "target closed",
        "browser has been closed", "context has been closed",
    )),
    (PAGE_CLOSED, (
        "page has been closed", "page is closed", "target page has been closed",
        "tab has been closed",
    )),
    (SESSION_NOT_FOUND, (
        "session not found", "session_not_found", "conversation not found",
        "chat not found", "no such conversation",
    )),
    (NOT_LOGGED_IN, (
        "not logged in", "login required", "please sign in", "sign in to continue",
        "auth required",
    )),
    (INPUT_MISSING, (
        "no input textarea", "no input field", "input field not found",
        "no input selector",
    )),
    (SEND_FAILED, (
        "could not send", "send failed", "send_button_timeout",
        "composer never",
    )),
    (BROWSER_DEAD, (
        "browser has disconnected", "browser closed", "process exited",
        # Both spellings: Playwright raises "net::ERR_CONNECTION_REFUSED"
        # (underscored, Chromium's error code) while raw sockets raise the
        # human-readable "Connection refused".
        "connection refused", "connection_refused", "econnrefused",
        "connection reset", "connection_reset", "browser is closed",
        "websocket is closed", "browser not connected", "not connected to browser",
    )),
    (CAPTURE_TIMEOUT, (
        "timed out waiting for deepseek response", "capture timeout",
        "never started responding",
    )),
)


def classify(exc: BaseException) -> str:
    """Map any exception to a stable browser failure code."""
    if isinstance(exc, BrowserError):
        return exc.code

    # Soft rate limit: the engine raises a dedicated type. Check it
    # structurally (same trick as NoSlotAvailable) so this module never needs
    # to import browser_scraper at module scope.
    try:
        from connectors.common.browser_scraper import SoftRateLimitError
        if isinstance(exc, SoftRateLimitError):
            return RATE_LIMITED
    except Exception:
        pass

    # Governor exhaustion is a distinct type — check it structurally so this
    # module never needs to import the governor at module scope.
    try:
        from engine.browser_governor import NoSlotAvailable
        if isinstance(exc, NoSlotAvailable):
            return SLOT_FULL
    except Exception:
        pass

    name = type(exc).__name__
    text = f"{name}: {exc}".lower()
    for code, needles in _PATTERNS:
        if any(n in text for n in needles):
            return code
    return UNKNOWN