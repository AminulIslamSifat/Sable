
"""DeepSeek API connector for Sable.

Pure HTTP backend — no browser needed for chat (only for initial token extraction).
Solves PoW via compiled Go binary (~80ms), streams SSE responses.

Yields events matching Sable's stream interface:
  {"type": "answer", "text": "..."}
  {"type": "thinking", "text": "..."}
  {"type": "done", "parent_id": "..."}
  {"type": "error", "message": "..."}
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import random
import re
import subprocess
import sys
from collections.abc import AsyncGenerator, Awaitable, Callable
from pathlib import Path
from typing import Any

from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import ReadTimeout as CurlReadTimeout

from connectors.common.instruction_builder import get_tool_format

logger = logging.getLogger("sable.deepseek_api")

# Idle timeout: if no bytes/events arrive within this window — whether while
# connecting or mid-stream — the connection is dropped and reopened via the
# normal retry loop. Treated as a plain failure (retry, then rotate).
_IDLE_TIMEOUT_SECS = 30.0

# Per-message tool-format reminder is disabled — the format is already defined
# in the system prompt, and re-injecting it on every turn wasted tokens and
# nudged the model toward the very DSML tags we don't want. We now inject a
# guard only when a leak is actually detected (see _get_dsml_guard).
_FORMAT_REMINDER_ENABLED = False

# Detects DSML/pipe-tag tool syntax in a model response, e.g. <|DSML|tool_calls>,
# <｜DSML｜invoke>, or any "| DSML |" / "｜ DSML ｜" variant.
_DSML_LEAK_RE = re.compile(r"[|｜]\s*DSML|DSML\s*[|｜]", re.IGNORECASE)

# Regex to strip ALL DSML artifacts from text, including partial/truncated tags.
# Handles:
# 1. Full blocks: <｜DSML｜...> ... </｜DSML｜...>
# 2. Partial opens: <｜DSML｜call, <｜DSML｜invoke name="..." (missing >)
# 3. Partial closes: </｜DSML｜cal, </｜DSML｜> (with typos/incomplete chars)
# 4. Stray pipes: | DSML |, ｜ DSML ｜
_DSML_STRIP_RE = re.compile(
    r"(?:<｜DSML｜[^>]*?>.*?</｜DSML｜[^>]*?>)"   # Full paired blocks
    r"|(?:<｜DSML｜[a-zA-Z_0-9\s=\"']*)"         # Partial opening tags (no closing >)
    r"|(?:</｜DSML｜[a-zA-Z_0-9\s=\"']*>)?"      # Partial closing tags (optional >)
    r"|(?:[|｜]\s*DSML\s*[|｜])",               # Stray pipe-wrapped fragments
    re.DOTALL | re.IGNORECASE
)


def _strip_dsml(text: str) -> str:
    """Remove all DSML markup, including partial/truncated tags."""
    if not text:
        return text
    cleaned = _DSML_STRIP_RE.sub("", text)
    # Collapse excessive whitespace left behind by stripping
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = re.sub(r" {2,}", " ", cleaned)
    return cleaned.strip()

BASE_URL = "https://chat.deepseek.com"


def _is_session_invalid(biz_code: Any, biz_msg: str | None) -> bool:
    """True when the biz_error means the upstream chat_session_id is gone.

    DeepSeek returns HTTP 200 + biz_code=1 with biz_msg "invalid chat session id"
    when the session UUID is stale, deleted, or belongs to another account.
    This is a SESSION-level failure — token rotation will NOT fix it, and
    nulling the parent chain just causes amnesia. The only correct recovery is
    to create a fresh upstream session and re-seed it with the full local
    conversation history.
    """
    msg = (biz_msg or "").lower()
    if "session" not in msg:
        return False
    return any(
        k in msg
        for k in ("invalid", "not found", "not_found", "expired", "deleted", "does not exist")
    )
_SOLVER_DIR = Path(__file__).resolve().parent / "pow_solver"
_SOLVER_NAME = "pow_solver.exe" if sys.platform == "win32" else "pow_solver"
SOLVER_PATH = _SOLVER_DIR / _SOLVER_NAME

# Raw request/response logging
from engine.config import LOGS_DIR as _LOGS_DIR
_RAW_LOG_PATH = _LOGS_DIR / "deepseek_raw.txt"


def _log_raw(direction: str, payload: str) -> None:
    """Append a timestamped raw payload to the log file."""
    try:
        _RAW_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        from datetime import datetime
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(_RAW_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"\n{'='*80}\n[{ts}] {direction}\n{'='*80}\n{payload}\n")
    except Exception:
        pass

# Per-account token store: {"browser-data-acc1": ["jwt1", "jwt2", ...], ...}
# Tokens never expire, so we accumulate them in a list per account.
# Capped at MAX_TOKENS_PER_ACCOUNT to prevent unbounded file growth.
_SYSTEM_DIR = Path(__file__).resolve().parent.parent.parent / "system"
TOKEN_STORE_PATH = _SYSTEM_DIR / ".deepseek_tokens.json"
MAX_TOKENS_PER_ACCOUNT = 10


# Per-account device fingerprint store (browser identity header)
FP_STORE_PATH = _SYSTEM_DIR / ".deepseek_fp.json"


def _load_fp_store() -> dict[str, str]:
    if FP_STORE_PATH.exists():
        try:
            raw = json.loads(FP_STORE_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return raw
        except Exception:
            pass
    return {}


def _save_fp_store(store: dict[str, str]) -> None:
    import tempfile
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8",
        dir=str(FP_STORE_PATH.parent), suffix=".tmp",
        delete=False,
    ) as tmp:
        json.dump(store, tmp, indent=2)
        tmp_path = tmp.name
    os.replace(tmp_path, str(FP_STORE_PATH))


def get_fp_for_account(account: str | None = None) -> str | None:
    acct = account or _resolve_active_account()
    store = _load_fp_store()
    return store.get(acct)


def set_fp_for_account(value: str, account: str | None = None) -> None:
    acct = account or _resolve_active_account()
    store = _load_fp_store()
    store[acct] = value
    _save_fp_store(store)


def _auto_rotate_enabled(provider: str) -> bool:
    """Check if auto-rotation is enabled for a provider via settings.json."""
    try:
        path = _SYSTEM_DIR / "settings.json"
        if not path.is_file():
            return True
        settings = json.loads(path.read_text(encoding="utf-8"))
        per_provider = settings.get("account_auto_switch")
        if isinstance(per_provider, dict) and provider in per_provider:
            return bool(per_provider[provider])
        return bool(settings.get("account_auto_switch_enabled", True))
    except Exception:
        return True

# Legacy single-token cache (migrated on first access)
_LEGACY_TOKEN_CACHE = Path(__file__).resolve().parent / ".token_cache.json"
_LEGACY_MIGRATED = False  # guard so migration only runs once


# --------------------------------------------------------------------------
# Per-account token store helpers (list-based — tokens accumulate)
# --------------------------------------------------------------------------

def _resolve_active_account() -> str:
    """DeepSeek's OWN active browser account (independent of every provider)."""
    from engine.config import get_active_account
    return get_active_account("deepseek")


def _read_pinned_token() -> str | None:
    """The user's manually pinned DeepSeek token, read from settings.json.

    Single source of truth for the pin — both the store loader and the
    client's resolution path read it through here so they can never disagree
    about which token is selected.
    """
    try:
        path = _SYSTEM_DIR / "settings.json"
        if not path.is_file():
            return None
        tok = json.loads(path.read_text(encoding="utf-8")).get("deepseek_preferred_token")
        if tok and isinstance(tok, str) and tok != "None":
            return tok
    except Exception:
        pass
    return None


def _load_token_store() -> dict[str, list[str]]:
    """Load the per-account token store, migrating legacy formats if needed.

    Returns {account: [token, ...]}. Handles migration from:
      - Old flat format {account: "token"} → {account: ["token"]}
      - Legacy single-token .token_cache.json (runs once only)
    """
    global _LEGACY_MIGRATED
    raw: dict = {}
    if TOKEN_STORE_PATH.exists():
        try:
            raw = json.loads(TOKEN_STORE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass

    pinned = _read_pinned_token()

    store: dict[str, list[str]] = {}
    for key, val in raw.items():
        if isinstance(val, list):
            valid = [t for t in val if t and t != "None"]
        elif isinstance(val, str) and val and val != "None":
            # Migrate old flat format → list
            valid = [val]
        else:
            valid = []
        # Collapse to the CURRENT token (latest) — older entries are stale
        # duplicates left behind by a refresh. The manually PINNED token is the
        # one exception: it must survive a refresh, otherwise the user's
        # selection dangles out of the list, no key renders as active, and the
        # rotation start silently reroutes to another account. Kept first so
        # `valid[-1]` is still the newest token.
        keep = valid[-1:]
        if pinned and pinned in valid and pinned not in keep:
            keep.insert(0, pinned)
        store[key] = keep

    # One-time migration from legacy single-token file (guarded)
    if not store and not _LEGACY_MIGRATED and _LEGACY_TOKEN_CACHE.exists():
        _LEGACY_MIGRATED = True
        try:
            legacy = json.loads(_LEGACY_TOKEN_CACHE.read_text(encoding="utf-8"))
            tok = legacy.get("token")
            if tok and tok != "None":
                account = _resolve_active_account()
                store[account] = [tok]
                _save_token_store(store)
                _LEGACY_TOKEN_CACHE.unlink(missing_ok=True)
                logger.info("Migrated legacy token to per-account list store (%s)", account)
        except Exception:
            pass
    return store


def _save_token_store(store: dict[str, list[str]]) -> None:
    """Persist the per-account token store atomically (write-to-tmp + rename)."""
    import tempfile
    try:
        data = json.dumps(store, indent=2)
        fd, tmp_path = tempfile.mkstemp(
            dir=str(TOKEN_STORE_PATH.parent), suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(data)
            os.replace(tmp_path, str(TOKEN_STORE_PATH))
        except BaseException:
            # Clean up temp file on any failure
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except Exception as exc:
        logger.warning("Could not save token store: %s", exc)


def get_own_token_for_account(account: str) -> str | None:
    """Strict lookup — ONLY returns a token actually captured FOR this account.

    Unlike get_token_for_account(), this NEVER falls back to another account.
    Use this for capture/refresh paths so we never persist a foreign token
    under the wrong account key (the bug that smeared 'ds' badges everywhere).
    """
    store = _load_token_store()
    tokens = [t for t in store.get(account, []) if t and t != "None"]
    return tokens[-1] if tokens else None


def get_token_for_account(account: str | None = None) -> str | None:
    """Get the most recent token for an account, falling back to any available.

    READ-ONLY helper for chat/API calls. The cross-account fallback is
    intentional here: any live token beats none. Do NOT use this as the
    source value for save_token_for_account() — use get_own_token_for_account().

    Returns the last token in the list (most recently added) since tokens
    don't expire and newer ones are preferred.
    """
    store = _load_token_store()
    if not store:
        return None

    def _latest(tokens: list[str]) -> str | None:
        valid = [t for t in tokens if t and t != "None"]
        return valid[-1] if valid else None

    # Primary: requested account
    if account and account in store:
        tok = _latest(store[account])
        if tok:
            return tok
    # Fallback: active account
    active = _resolve_active_account()
    if active in store:
        tok = _latest(store[active])
        if tok:
            return tok
    # Last resort: any account with a valid token
    for tokens in store.values():
        tok = _latest(tokens)
        if tok:
            return tok
    return None


def get_unique_tokens() -> list[str]:
    """Load all tokens from the store, deduplicate, return unique valid tokens."""
    store = _load_token_store()
    seen: set[str] = set()
    unique: list[str] = []
    for tokens in store.values():
        for tok in tokens:
            if tok and tok != "None" and tok not in seen:
                seen.add(tok)
                unique.append(tok)
    return unique


def get_unique_tokens_with_accounts() -> list[dict[str, str]]:
    """Return unique tokens with their browser-data account association.

    Returns list of {"token": ..., "account": "browser-data-accN"} dicts.
    If a token appears under multiple accounts, first occurrence wins.
    """
    store = _load_token_store()
    seen: set[str] = set()
    result: list[dict[str, str]] = []
    for account, tokens in store.items():
        for tok in tokens:
            if tok and tok != "None" and tok not in seen:
                seen.add(tok)
                result.append({"token": tok, "account": account})
    return result


def save_token_for_account(token: str, account: str | None = None) -> None:
    """Store the CURRENT token for an account, overwriting any previous value.

    Refresh must be idempotent: the newest token replaces the old one so a
    single account never accumulates stale duplicates. A refresh means "this
    is now the live token for this account" — accumulating old ones only
    bloated the rotation pool with dead keys. Uses atomic write.
    """
    if not token or token == "None":
        return
    acct = account or _resolve_active_account()
    store = _load_token_store()
    # Append instead of replace: _load_token_store() trims the list back to
    # [pinned, newest], so refreshing an account adds its new token to the
    # rotation pool WITHOUT destroying the token the user pinned for it.
    store[acct] = [*store.get(acct, []), token]
    _save_token_store(store)

# Application-level headers only.
# TLS fingerprint, User-Agent, sec-ch-ua, Accept-Language are all injected
# automatically by curl_cffi's impersonate="chrome" — no need to set them here.
CLIENT_HEADERS = {
    "x-client-version": "2.5.0",
    "x-client-platform": "web",
    "x-client-bundle-id": "com.deepseek.chat",
    "x-client-locale": "en_US",
    "x-client-timezone-offset": "21600",
}

# Client-side history cap (matches Gemini/Mistral sliding window)
_MAX_SESSION_CHARS = 100_000

from connectors.common.context_summarizer import strip_summarize_tag


def _account_for_token(token: str | None) -> str | None:
    """Reverse-lookup: which account owns this JWT (from the token store).

    Ghost transport MUST impersonate the same browser profile the token
    belongs to — sending account A's JWT from account B's browser produces
    mismatched device headers (x-device-id, x-hif-leim, cookies), which is
    exactly the anomaly pattern that gets DeepSeek accounts silently banned.
    """
    if not token:
        return None
    for acct, toks in _load_token_store().items():
        if token in toks:
            return acct
    return None


def _msg_chars(msg: dict[str, Any]) -> int:
    """Character count of a DeepSeek-format history message."""
    return len(msg.get("content", ""))


def _trim_history(history: list[dict[str, Any]], prefix_len: int, max_chars: int) -> list[dict[str, Any]]:
    """Trim history to fit within max_chars, preserving prefix messages."""
    prefix = history[:prefix_len]
    msgs = history[prefix_len:]
    total = sum(_msg_chars(m) for m in msgs)
    while total > max_chars and len(msgs) > 1:
        total -= _msg_chars(msgs.pop(0))
    return prefix + msgs


# Instruction loading — uses shared builder with project-aware overrides
_instruction_cache: str | None = None
_cached_project_id: str | None = "__none__"
_cached_layout_mode: str | None = "__none__"
_cached_version: int = -1


def _load_instructions(project_id: str | None = None, layout_mode: str | None = None) -> str:
    """Load instruction context for first-message injection. Project-aware."""
    global _instruction_cache, _cached_project_id, _cached_version, _cached_layout_mode
    from connectors.common.instruction_builder import get_instruction_version
    current_version = get_instruction_version()
    # Invalidate cache when project, layout mode, or instruction version changes
    if (project_id != _cached_project_id
            or layout_mode != _cached_layout_mode
            or current_version != _cached_version):
        _instruction_cache = None
        _cached_project_id = project_id
        _cached_layout_mode = layout_mode
        _cached_version = current_version
    if _instruction_cache is not None:
        return _instruction_cache
    from connectors.common.instruction_builder import build_instructions
    _instruction_cache = build_instructions(
        project_id=project_id, provider="deepseek", layout_mode=layout_mode,
    )
    return _instruction_cache


class _EnterTimeoutCM:
    """Wrap a curl_cffi stream context manager so that *opening* it — which is
    what actually fires the HTTP request and blocks for response headers — is
    bounded by an idle timeout. The body stream is left untouched so long
    responses aren't capped; only a stalled connect/header phase is killed."""

    def __init__(self, cm, timeout: float):
        self._cm = cm
        self._timeout = timeout

    async def __aenter__(self):
        return await asyncio.wait_for(self._cm.__aenter__(), timeout=self._timeout)

    async def __aexit__(self, *exc):
        return await self._cm.__aexit__(*exc)


class DeepSeekAPIError(Exception):
    """Raised on auth failure or unexpected API response."""


class DeepSeekClient:
    """Async DeepSeek chat client with PoW solving and automatic token rotation.

    Mirrors the Gemini/Mistral connector pattern:
      - Client-side conversation history per chat_id (no server-side session)
      - Full context serialized into prompt each request (parent_id=None, stateless)
      - Automatic round-robin token rotation with failover across unique tokens
      - Sliding-window history trimming at _MAX_SESSION_CHARS
    """

    def __init__(
        self,
        token: str | None = None,
        token_refresher: Callable[[], Awaitable[str]] | None = None,
        account: str | None = None,
        model_id: str | None = None,
    ) -> None:
        self._token = token
        self._token_refresher = token_refresher
        self._account = account  # e.g. "browser-data-acc15"; None = active
        self._model_id = model_id or "deepseek-instant"
        self._http: AsyncSession | None = None
        # Consecutive failure counter (per-instance). When it hits
        # _POOL_FAILURE_THRESHOLD, _get_http() recycles the connection pool.
        # Fixes the "requests hang forever after network change" bug where
        # curl_cffi's pooled TCP sockets become half-open zombies.
        self._consecutive_failures: int = 0
        self._lock = asyncio.Lock()
        # Client-side conversation history: chat_id → [message dicts]
        self._sessions: dict[str, list[dict[str, Any]]] = {}
        # Server-side message chaining: chat_id → last assistant message_id
        # Used as parent_message_id for the next request so DeepSeek chains server-side.
        # DeepSeek message_id is an integer (not string).
        self._parent_ids: dict[str, int | None] = {}
        # Token rotation state (always active — like Gemini/Mistral key rotation)
        self._rotate_tokens: list[str] = []
        self._rotate_idx: int = 0
        # Per-chat DeepSeek sessions: (token, chat_id) → deepseek chat_session_id.
        # Each Sable chat gets its own upstream session so parent_message_id
        # chaining works correctly. Never shared across chats.
        self._chat_sessions: dict[tuple[str, str], str] = {}
        from connectors.common.error_ctx import ErrorContext
        self.ctx = ErrorContext("deepseek")
        self.ctx.model = self._model_id
        self._last_prepare_error: str | None = None  # surfaced when all tokens fail
        # Chats whose last response leaked DSML markup. Armed by the leak
        # detector; drives the hard no-DSML reminder on subsequent turns.
        self._dsml_warn_chats: set[str] = set()

        # Load per-model max session chars from config
        from engine.config import get_model_config
        config = get_model_config(self._model_id)
        self._max_session_chars = config.get("max_session_chars", 100_000)

    @property
    def account(self) -> str:
        """Resolved account name for this client instance."""
        return self._account or _resolve_active_account()

    def set_token_refresher(self, refresher: Callable[[], Awaitable[str]] | None) -> None:
        """Inject an external coroutine that returns a fresh DeepSeek token."""
        self._token_refresher = refresher

    # ------------------------------------------------------------------
    # Token management (per-account)
    # ------------------------------------------------------------------

    @property
    def token(self) -> str | None:
        """The effective token — the SAME one the send path will use.

        The pinned preference is checked FIRST, not last. Checking the
        in-memory `self._token` first let an automatic refresh (set_token) make
        this report the freshly scraped JWT while _init_rotation() still
        started on the pinned one — so the UI flagged the new key as selected
        and the request went out on a different one.
        """
        preferred = self._get_preferred_token_from_settings()
        if preferred:
            self._token = preferred
            return preferred
        if self._token:
            return self._token
        # Resolve from per-account store (with fallback)
        self._token = get_token_for_account(self._account)
        return self._token

    @property
    def is_available(self) -> bool:
        """Whether the connector has valid credentials."""
        return self.token is not None

    def set_token(
        self,
        token: str,
        account: str | None = None,
        *,
        persist: bool = True,
        pin: bool = False,
    ) -> None:
        """Set and (optionally) persist token under the given account.

        Pass persist=False when the token came from a cross-account fallback
        (e.g. rotation handoff) so it is used in-memory but NOT written under
        an account it doesn't belong to.

        Pass pin=True from the REFRESH path only. It promotes the fresh token
        to `deepseek_preferred_token` in settings.json, i.e. makes it the
        user's current selection. The UI (settings.py) and _init_rotation()
        both resolve the effective key through that pin, so a refresh that
        skipped this left the OLD token showing as selected while the fresh
        one sat unused. Manual switches/rotation handoffs must NOT pin —
        otherwise a handoff would silently steal the user's selection.
        """
        self._token = token
        # Invalidate rotation cache so next request picks up the new token
        self._rotate_tokens = []
        self._rotate_idx = 0
        # Account/token switch == new upstream chat. Drop stale parent chains
        # so the next turn doesn't reference a message_id that never existed on
        # the new account, and drop all cached sessions for this token so each
        # chat gets a fresh DeepSeek session on next request.
        self._parent_ids.clear()
        if token:
            keys_to_drop = [k for k in self._chat_sessions if k[0] == token]
            for k in keys_to_drop:
                del self._chat_sessions[k]
        if persist and token:
            save_token_for_account(token, account or self._account)
        if pin and token:
            self._pin_token(token, account or self.account)

    @staticmethod
    def _pin_token(token: str, account: str | None = None) -> None:
        """Write the effective DeepSeek selection into settings.json.

        `deepseek_preferred_token` is the single source of truth the settings
        UI (via .token) and _init_rotation() both read. Refresh must update it
        or the freshly scraped key never becomes the selected one.
        """
        try:
            path = _SYSTEM_DIR / "settings.json"
            settings: dict[str, Any] = {}
            if path.is_file():
                try:
                    settings = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    settings = {}
            settings["deepseek_preferred_token"] = token
            if account:
                settings["deepseek_preferred_account"] = account
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(settings, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except Exception as exc:
            logger.warning("Could not pin DeepSeek token: %s", exc)

    # ------------------------------------------------------------------
    # Token rotation (automatic round-robin + failover, like Gemini/Mistral)
    # ------------------------------------------------------------------

    @staticmethod
    def _get_preferred_token_from_settings() -> str | None:
        """Read the user's manually-selected DeepSeek token from settings.json."""
        try:
            path = _SYSTEM_DIR / "settings.json"
            if not path.is_file():
                return None
            settings = json.loads(path.read_text(encoding="utf-8"))
            tok = settings.get("deepseek_preferred_token")
            if tok and isinstance(tok, str) and tok != "None":
                return tok
        except Exception:
            pass
        return None

    def _init_rotation(self) -> None:
        """Load all unique tokens across accounts for round-robin rotation.

        DeepSeek tokens are browser-session-bound JWTs but they work like API
        keys — no browser needed to use them. Load ALL unique tokens from the
        store so auto-rotate can failover across accounts when one gets
        rate-limited or muted.
        """
        # Load all unique tokens across all accounts for rotation
        all_tokens = get_unique_tokens()

        # If a specific token was explicitly set (e.g. manual switch), ensure
        # it's in the pool and start from it — but DON'T restrict to just it
        if self._token and self._token not in all_tokens:
            all_tokens.insert(0, self._token)

        if all_tokens:
            self._rotate_tokens = all_tokens
            # Priority: persisted user preference > in-memory > active account.
            # User preference MUST win because application.py calls set_token()
            # on startup with a freshly-scraped browser token, which would
            # otherwise silently override the user's manual key selection.
            preferred = (
                self._get_preferred_token_from_settings()
                or self._token
                or get_own_token_for_account(self.account)
            )
            if preferred and preferred in self._rotate_tokens:
                self._rotate_idx = self._rotate_tokens.index(preferred)
            else:
                # A dangling pin must NOT silently reroute rotation to
                # all_tokens[0]: that sent the request on one account's token
                # and made the UI report a foreign key as selected. Fall back
                # to this client's OWN account before anything else.
                own = get_own_token_for_account(self.account)
                if own and own in self._rotate_tokens:
                    self._rotate_idx = self._rotate_tokens.index(own)
                else:
                    self._rotate_idx = 0
            # Keep in-memory token in sync with rotation starting point so
            # the .token property, is_available, etc. reflect the actual active key.
            start_token = self._current_rotate_token
            if start_token and start_token != self._token:
                self._token = start_token
        else:
            self._rotate_tokens = []
            self._rotate_idx = 0

        logger.info("[DeepSeek] Rotation pool loaded: %d tokens (account=%s, starting at idx=%d)",
                     len(self._rotate_tokens), self.account, self._rotate_idx)

    @property
    def _current_rotate_token(self) -> str | None:
        if not self._rotate_tokens:
            return None
        return self._rotate_tokens[self._rotate_idx % len(self._rotate_tokens)]

    @staticmethod
    def _mask_token(token: str | None) -> str:
        """Mask a JWT token for safe display: show first 8 + last 4 chars."""
        if not token or len(token) < 16:
            return "***"
        return f"{token[:8]}…{token[-4:]}"

    def _advance_rotation(self) -> None:
        if not self._rotate_tokens:
            return
        if not _auto_rotate_enabled("deepseek"):
            logger.info("[DeepSeek] Auto-rotate disabled — staying on current token")
            return
        self._rotate_idx = (self._rotate_idx + 1) % len(self._rotate_tokens)

    def _auth_headers_for(self, token: str) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {token}", **CLIENT_HEADERS}
        # Impersonate the profile the TOKEN belongs to, not the active account.
        # A pinned token from another account sent with the active account's
        # device header (x-hif-leim) is exactly the token/device mismatch
        # _account_for_token()'s docstring warns gets accounts muted.
        fp = get_fp_for_account(_account_for_token(token) or self._account)
        if fp:
            headers["x-hif-leim"] = fp
        return headers

    def _auth_headers(self) -> dict[str, str]:
        """Auth headers using current rotation token (or fallback single token)."""
        if self._rotate_tokens:
            tok = self._current_rotate_token
        else:
            tok = self.token
        if not tok:
            raise DeepSeekAPIError("No DeepSeek token available.")
        return {"Authorization": f"Bearer {tok}", **CLIENT_HEADERS}

    async def _prepare_request_with_rotation(
        self, chat_id: str | None = None,
    ) -> tuple[str, dict[str, str]] | None:
        """Pick a usable token, get/create a per-chat session, solve PoW.

        Returns (session_id, headers) or None if all tokens exhausted.
        Each (token, chat_id) pair gets its own DeepSeek session so
        parent_message_id chaining works correctly across concurrent chats.
        Advances past tokens that fail; does NOT advance on success (caller does).
        """
        if not self._rotate_tokens:
            self._init_rotation()
        if not self._rotate_tokens:
            return None

        auto_rotate = _auto_rotate_enabled("deepseek")
        max_tries = 1 if not auto_rotate else len(self._rotate_tokens)
        tried = 0

        # Fallback key when chat_id is None (ephemeral chats share one session per token)
        cid = chat_id or "__ephemeral__"

        while tried < max_tries:
            token = self._current_rotate_token
            if not token:
                if not auto_rotate:
                    break
                self._advance_rotation()
                tried += 1
                continue

            auth = self._auth_headers_for(token)
            cache_key = (token, cid)

            # One persistent session per (token, chat_id) pair.
            # After a server restart, _chat_sessions is empty but the DB may
            # hold the DeepSeek chat_session_id in upstream_session_id.
            # Restore it so we continue the same upstream conversation.
            if cache_key not in self._chat_sessions and chat_id:
                try:
                    from server.database import get_upstream_session_id as _get_usid
                    _saved_sid = _get_usid(chat_id)
                    if _saved_sid:
                        self._chat_sessions[cache_key] = _saved_sid
                        logger.info("DeepSeek: restored session %s for chat %s from DB", _saved_sid[:12], chat_id)
                except Exception:
                    pass

            if cache_key not in self._chat_sessions:
                try:
                    session_id = await self._create_session(headers=auth)
                    self._chat_sessions[cache_key] = session_id
                    # Persist so we can restore after restart
                    if chat_id:
                        try:
                            from server.database import set_upstream_session_id as _set_usid
                            _set_usid(chat_id, session_id)
                        except Exception:
                            pass
                except DeepSeekAPIError as exc:
                    logger.warning("Rotation: session failed for %s...: %s", token[:10], exc)
                    self._last_prepare_error = str(exc)
                    if not auto_rotate:
                        break
                    self._advance_rotation()
                    tried += 1
                    continue
                except Exception as exc:
                    logger.warning("Rotation: session failed for %s...: %s", token[:10], exc)
                    self._last_prepare_error = str(exc)
                    if not auto_rotate:
                        break
                    self._advance_rotation()
                    tried += 1
                    continue
            session_id = self._chat_sessions[cache_key]

            try:
                challenge = await self._get_challenge(headers=auth)
                loop = asyncio.get_running_loop()
                nonce = await loop.run_in_executor(None, self._solve_pow, challenge)
                pow_header = self._build_pow_header(challenge, nonce)
            except DeepSeekAPIError as exc:
                logger.warning("Rotation: PoW failed for %s...: %s", token[:10], exc)
                self._last_prepare_error = str(exc)
                if not auto_rotate:
                    break
                self._advance_rotation()
                tried += 1
                continue
            except Exception as exc:
                logger.warning("Rotation: PoW failed for %s...: %s", token[:10], exc)
                self._last_prepare_error = str(exc)
                if not auto_rotate:
                    break
                self._advance_rotation()
                tried += 1
                continue

            headers = {**auth, "X-DS-PoW-Response": pow_header, "Content-Type": "application/json"}
            headers["Referer"] = f"https://chat.deepseek.com/a/chat/s/{session_id}"
            return session_id, headers

        return None

    # ------------------------------------------------------------------
    # Token auto-refresh
    # ------------------------------------------------------------------

    @property
    def _BROWSER_PROFILE(self) -> Path:
        # DeepSeek's OWN active browser profile (not the primary/qwen slot).
        from engine.config import get_browser_data_dir
        return get_browser_data_dir("deepseek")

    async def refresh_token(self) -> str:
        """Public method — refresh token using injected refresher or fallback browser profile."""
        return await self._refresh_token()

    async def _refresh_token(self) -> str:
        """Refresh token via external provider when available, else standalone browser."""
        if self._token_refresher is not None:
            logger.info("Refreshing DeepSeek token via injected browser session...")
            token = await self._token_refresher()
            if not token:
                raise DeepSeekAPIError("Token refresher returned an empty token.")
            # pin=True: a refresh is a user-visible "use this token now" action,
            # so the fresh key must become the selected one in settings.
            self.set_token(token, pin=True)
            logger.info("Token refreshed successfully via injected browser session.")
            return token
        return await self._refresh_token_from_browser()

    async def _refresh_token_from_browser(self) -> str:
        """Fallback: open persistent browser profile, read DeepSeek token from localStorage."""
        from playwright.async_api import async_playwright

        logger.info("Refreshing DeepSeek token from standalone browser profile...")
        async with async_playwright() as pw:
            ctx = await pw.chromium.launch_persistent_context(
                user_data_dir=str(self._BROWSER_PROFILE),
                headless=True,
                args=["--disable-gpu", "--no-sandbox"],
            )
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto("https://chat.deepseek.com", wait_until="domcontentloaded", timeout=15000)
            await page.wait_for_timeout(2000)  # let JS hydrate localStorage
            token = await page.evaluate("() => localStorage.getItem('userToken')")
            await ctx.close()

        if not token:
            raise DeepSeekAPIError("No userToken found in browser profile. Log in to chat.deepseek.com first.")

        token = self._parse_localstorage_token(token)
        # pin=True: same rationale as the injected-refresher path above.
        self.set_token(token, pin=True)
        logger.info("Token refreshed successfully from standalone browser.")
        return token

    @staticmethod
    def _parse_localstorage_token(raw: str) -> str:
        # localStorage stores JSON: {"value":"<jwt>","__version":"0"}
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return str(parsed.get("value", raw))
        except (json.JSONDecodeError, AttributeError):
            pass
        return raw.strip('"')

    # ------------------------------------------------------------------
    # HTTP client lifecycle
    # ------------------------------------------------------------------

    # Pool failure threshold (constant, safe as class attr).
    _POOL_FAILURE_THRESHOLD: int = 2

    # Set True when the ghost transport fails mid-stream. _resolve_ghost()
    # then serves None (curl) for the rest of the current stream_chat() call.
    _ghost_broken: bool = False

    async def _get_http(self) -> AsyncSession:
        if self._http is None:
            # impersonate="chrome" replicates Chromium's exact TLS fingerprint
            # (cipher suites, extensions, ALPN, HTTP/2 SETTINGS frame).
            # This is what WAF checks — raw httpx/OpenSSL mismatches the Chrome UA.
            self._http = AsyncSession(impersonate="chrome", timeout=120)
        elif self._consecutive_failures >= self._POOL_FAILURE_THRESHOLD:
            logger.warning(
                "DeepSeek: %d consecutive failures — recycling connection pool",
                self._consecutive_failures,
            )
            try:
                await self._http.close()
            except Exception:
                pass
            self._http = AsyncSession(impersonate="chrome", timeout=120)
            self._consecutive_failures = 0
        return self._http

    def _mark_success(self) -> None:
        """Reset failure counter after a successful stream."""
        self._consecutive_failures = 0

    def _mark_failure(self) -> None:
        """Increment failure counter; triggers pool recycle at threshold."""
        self._consecutive_failures += 1

    async def close(self) -> None:
        if self._http is not None:
            await self._http.close()
            self._http = None
        # Tear down this account's ghost tab so Playwright's driver subprocess
        # exits cleanly (otherwise its transport is GC'd after the event loop
        # closes and spits a RuntimeError traceback).
        try:
            from connectors.deepseek import ghost as _ghost
            s = _ghost.GhostSession._registry.get(self.account)
            if s is not None:
                await s.close()
                _ghost.GhostSession._registry.pop(self.account, None)
        except Exception:
            pass

    async def cancel(self) -> bool:
        """Out-of-band stop for an in-flight ghost stream on this account.

        Called from /api/chat/stop. Aborts the in-page fetch, unblocks the
        queue drainer, and releases the ghost stream lock so the next message
        isn't wedged. Returns True if a stream was active.
        """
        try:
            from connectors.deepseek import ghost as _ghost
            s = _ghost.GhostSession._registry.get(self.account)
            if s is not None:
                return s.cancel_stream()
        except Exception as exc:
            logger.warning("DeepSeek cancel failed: %s", exc)
        return False

    # ------------------------------------------------------------------
    # Completion transport — ghost-tab (browser identity) or curl (fallback)
    # ------------------------------------------------------------------

    def _ghost_account(self) -> str:
        """Account whose profile the ghost must open.

        Ghost profile follows the ACTIVE TOKEN's account — NOT the global
        active account. The rotation pool may be serving acc2 while the global
        setting says acc3; opening acc3's browser with acc2's JWT is an identity
        mismatch (device headers ≠ token account).
        """
        _tok_acct = _account_for_token(self._current_rotate_token)
        if _tok_acct and _tok_acct != self.account:
            logger.info("DeepSeek: ghost opening profile %s (token's account) "
                        "instead of %s (global active)", _tok_acct, self.account)
        return _tok_acct or self.account

    async def _resolve_ghost(self, force_respawn: bool = False):
        """Return a live GhostSession for this account, or None if unavailable.

        When ``force_respawn`` is set, the cached session (if any) is evicted
        and a brand-new browser is acquired — this is how a deliberately-closed
        browser gets brought back instead of silently degrading to curl.
        Never raises — an unavailable ghost just means curl fallback.
        """
        try:
            from connectors.deepseek import ghost as _ghost
            if not _ghost.ghost_enabled():
                return None
            if self._ghost_broken and not force_respawn:
                logger.info("DeepSeek: ghost failed earlier this message — using curl")
                return None
            acct = self._ghost_account()
            if force_respawn:
                # Drop the stale session + kill its browser so for_account()
                # rebuilds from scratch (for_account already checks health, but
                # a closed-mid-life browser can still read as 'healthy').
                await self._evict_ghost_session(acct, kill=True)
                self._ghost_broken = False
                logger.info("DeepSeek: respawning ghost browser for %s", acct)
            return await _ghost.GhostSession.for_account(acct)
        except Exception as exc:
            logger.warning("Ghost unavailable (%s) — using curl", exc)
            return None

    async def _evict_ghost_session(self, account: str, kill: bool = False) -> None:
        """Tear down the cached GhostSession for ``account`` (and optionally
        kill every browser on its profile) so the next resolve relaunches."""
        try:
            from connectors.deepseek import ghost as _ghost
            s = _ghost.GhostSession._registry.pop(account, None)
            if s is not None:
                try:
                    await s.close()
                except Exception:
                    pass
            if kill:
                from connectors.common.profile_hub import (
                    ProfileHub as _PH, _kill_profile_holders as _kill)
                profile = _ghost._SYSTEM_DIR / account
                try:
                    await _PH.evict(profile)
                except Exception:
                    pass
                await _kill(profile)
        except Exception as exc:
            logger.warning("DeepSeek: ghost evict failed (%s)", exc)

    async def _completion_opener(self, headers: dict[str, str], body: dict[str, Any], ghost):
        """Return an async context manager yielding a response-like object.

        Prefers ghost (in-page fetch, browser identity). If the ghost stream
        fails to OPEN — page closed, bridge gone, browser killed — it is
        respawned once (kill + relaunch + retry) before falling back to curl.
        """
        token = headers.get("Authorization", "")
        if token.startswith("Bearer "):
            token = token[7:]
        pow_hdr = headers.get("X-DS-PoW-Response", "")

        for attempt in (1, 2):
            if ghost is None:
                break
            try:
                # Streaming in-page fetch: SSE lines arrive as they're produced,
                # with a 30s idle gate enforced both in-page and in the queue drain.
                return await ghost.open_stream(
                    token, body, pow_hdr, idle_secs=_IDLE_TIMEOUT_SECS)
            except Exception as exc:
                if attempt == 1:
                    logger.warning(
                        "Ghost stream open failed (%s) — respawning browser & retrying",
                        exc)
                    ghost = await self._resolve_ghost(force_respawn=True)
                    if ghost is None:
                        break
                    continue
                logger.warning(
                    "Ghost stream open failed again after respawn (%s) — "
                    "falling back to curl for this request", exc)
        http = await self._get_http()
        cm = http.stream("POST", f"{BASE_URL}/api/v0/chat/completion", json=body, headers=headers)
        # Bound the connect + response-header phase at 30s idle. Ghost path
        # enforces its own idle abort inside the page JS, so it's not wrapped.
        return _EnterTimeoutCM(cm, _IDLE_TIMEOUT_SECS)

    # ------------------------------------------------------------------
    # PoW solving
    # ------------------------------------------------------------------

    def _solve_pow(self, challenge: dict[str, Any]) -> int:
        """Solve PoW challenge via Go binary. Blocking — call from thread."""
        if not SOLVER_PATH.exists():
            raise DeepSeekAPIError(f"Go solver not found at {SOLVER_PATH}")
        result = subprocess.run(
            [str(SOLVER_PATH)],
            input=json.dumps(challenge),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise DeepSeekAPIError(f"PoW solver failed: {result.stderr.strip()}")
        nonce = int(result.stdout.strip())
        return nonce

    def _build_pow_header(self, challenge: dict[str, Any], nonce: int) -> str:
        """Build base64-encoded X-DS-PoW-Response header value."""
        payload = {
            "algorithm": challenge["algorithm"],
            "challenge": challenge["challenge"],
            "salt": challenge["salt"],
            "answer": nonce,
            "signature": challenge["signature"],
            "target_path": challenge["target_path"],
        }
        return base64.b64encode(
            json.dumps(payload, separators=(",", ":")).encode()
        ).decode()

    async def _refresh_pow_header(self, headers: dict[str, str]) -> dict[str, str]:
        """Solve a fresh PoW challenge and return headers with an updated header value.

        DeepSeek PoW nonces are single-use. Replaying the header from a previous
        request (including the hello seed) makes the server silently return an
        empty 200. Must be called before every retry or resend.
        """
        challenge = await self._get_challenge(headers=headers)
        loop = asyncio.get_running_loop()
        nonce = await loop.run_in_executor(None, self._solve_pow, challenge)
        return {**headers, "X-DS-PoW-Response": self._build_pow_header(challenge, nonce)}

    # ------------------------------------------------------------------
    # API calls
    # ------------------------------------------------------------------

    async def _get_challenge(self, headers: dict[str, str] | None = None) -> dict[str, Any]:
        """Request a fresh PoW challenge. Auto-refreshes token from browser on 401.

        Pass explicit `headers` (rotation mode) to use a specific pooled token — on
        401 this raises instead of triggering a browser refresh (rotation tokens are
        pre-collected JWTs; a dead one is skipped by the caller, not refreshed).
        """
        http = await self._get_http()
        explicit = headers is not None
        hdrs = headers if explicit else self._auth_headers()

        # No token at all — try browser refresh before anything else (non-rotation only)
        if not explicit and not self.token:
            await self._refresh_token()
            hdrs = self._auth_headers()

        resp = await http.post(
            f"{BASE_URL}/api/v0/chat/create_pow_challenge",
            json={"target_path": "/api/v0/chat/completion"},
            headers=hdrs,
        )
        if resp.status_code == 401:
            if explicit:
                raise DeepSeekAPIError("401 on challenge with rotation token")
            # Token expired — auto-refresh from persistent browser profile
            logger.warning("401 on challenge — refreshing token...")
            await self._refresh_token()
            resp = await http.post(
                f"{BASE_URL}/api/v0/chat/create_pow_challenge",
                json={"target_path": "/api/v0/chat/completion"},
                headers=self._auth_headers(),
            )
            if resp.status_code == 401:
                raise DeepSeekAPIError("Token still invalid after browser refresh. Log in to chat.deepseek.com.")
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict) or data.get("code") != 0:
            msg = data.get("msg", "unknown") if isinstance(data, dict) else "unknown"
            raise DeepSeekAPIError(f"Challenge failed: {msg}")
        inner = data.get("data")
        biz = inner.get("biz_data") if isinstance(inner, dict) else None
        challenge = biz.get("challenge") if isinstance(biz, dict) else None
        if not challenge:
            raise DeepSeekAPIError(f"Challenge response missing challenge field (data={inner})")
        return challenge

    async def _create_session(self, headers: dict[str, str] | None = None) -> str:
        """Create a new chat session, return its UUID. Auto-refreshes token on 401.

        Pass explicit `headers` (rotation mode) to use a specific pooled token — on
        401 this raises instead of triggering a browser refresh.
        """
        http = await self._get_http()
        explicit = headers is not None
        hdrs = headers if explicit else self._auth_headers()
        resp = await http.post(
            f"{BASE_URL}/api/v0/chat_session/create",
            json={},
            headers=hdrs,
        )
        if resp.status_code == 401:
            if explicit:
                raise DeepSeekAPIError("401 creating session with rotation token")
            logger.warning("401 on session create — refreshing token...")
            await self._refresh_token()
            resp = await http.post(
                f"{BASE_URL}/api/v0/chat_session/create",
                json={},
                headers=self._auth_headers(),
            )
            if resp.status_code == 401:
                raise DeepSeekAPIError("Token still invalid after refresh. Log in to chat.deepseek.com.")
        resp.raise_for_status()
        data = resp.json()
        # DeepSeek sometimes returns 200 with data=null for muted/rate-limited accounts
        inner = data.get("data") if isinstance(data, dict) else None
        biz = inner.get("biz_data") if isinstance(inner, dict) else None
        if not biz:
            msg = data.get("msg", "unknown") if isinstance(data, dict) else "unknown"
            code = data.get("code", "?") if isinstance(data, dict) else "?"
            raise DeepSeekAPIError(f"Session create returned no biz_data (code={code}, msg={msg})")
        # Response shape: biz_data.chat_session.id (or legacy biz_data.id)
        chat_session = biz.get("chat_session")
        if isinstance(chat_session, dict) and "id" in chat_session:
            return chat_session["id"]
        session_id = biz.get("id")
        if not session_id:
            raise DeepSeekAPIError(f"Session create response missing id: {biz}")
        return session_id

    async def _seed_hello_message(
        self, session_id: str, headers: dict[str, str],
    ) -> int | None:
        """Send a minimal 'hello' as the FIRST upstream message of a fresh chat.

        Opens the chat with a tiny greeting, captures the returned assistant
        message_id, and returns it for chaining the next message.
        Backend-only — callers MUST NOT forward any events to the frontend.
        """
        return await self._send_and_capture_id(
            session_id, headers, prompt="hello",
            parent_id=None, model_type="default", tag="hello-seed",
        )

    async def _send_and_capture_id(
        self, session_id: str, headers: dict[str, str],
        prompt: str, parent_id: int | None, model_type: str | None,
        tag: str = "send",
    ) -> int | None:
        """Send one non-frontend message, consume the stream, return its
        assistant response_message_id for parent chaining. None on failure."""
        body = {
            "chat_session_id": session_id,
            "parent_message_id": parent_id,
            "model_type": model_type,
            "prompt": prompt,
            "ref_file_ids": [],
            "thinking_enabled": False,
            "search_enabled": True,
            "action": None,
            "preempt": False,
        }
        _log_raw(f"USER_MESSAGE [{tag}]", prompt)
        try:
            http = await self._get_http()
            last_id: int | None = None
            async with http.stream(
                "POST", f"{BASE_URL}/api/v0/chat/completion", json=body, headers=headers,
            ) as resp:
                if resp.status_code != 200:
                    await resp.content
                    logger.warning("DeepSeek %s HTTP %s", tag, resp.status_code)
                    return None
                async for event in self._iter_with_idle_timeout(resp):
                    etype = event.get("type")
                    if etype == "done":
                        pid = event.get("parent_id")
                        if isinstance(pid, int):
                            last_id = pid
                    elif etype == "biz_error":
                        logger.warning("DeepSeek %s biz_error: %s", tag, event.get("biz_msg"))
                        return None
            _log_raw(f"RESPONSE [{tag}]", f"message_id={last_id}")
            return last_id
        except Exception as exc:
            logger.warning("DeepSeek %s failed: %s", tag, exc)
            return None

    def _get_or_create_session(
        self, chat_id: str | None, inject_instructions: bool,
        system_instruction: str | None = None,
        project_id: str | None = None,
        layout_mode: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get existing session history or create a new one (sliding window)."""
        if chat_id and chat_id in self._sessions:
            history = self._sessions[chat_id]
            prefix_len = 1 if history and history[0].get("role") == "system" else 0
            total_chars = sum(_msg_chars(m) for m in history[prefix_len:])
            if total_chars > self._max_session_chars:
                self._sessions[chat_id] = _trim_history(history, prefix_len, self._max_session_chars)
            return self._sessions[chat_id]

        history: list[dict[str, Any]] = []
        instructions = system_instruction if system_instruction else (_load_instructions(project_id=project_id, layout_mode=layout_mode) if inject_instructions else None)
        if instructions:
            history.append({"role": "system", "content": instructions})

        if chat_id:
            self._sessions[chat_id] = history
        return history

    # Tool-calling format reminder — prepended to EVERY user message (chained,
    # full-context, and rotation paths). Built dynamically from the active
    # tool_call_format setting so it always matches what the system prompt says.
    @classmethod
    def _get_reminder(cls) -> str:
        """Build a compact reminder showing the current tool call format.

        Uses placeholder tag names / non-parseable JSON so Sable's own
        response parser never fires on these examples.
        """
        if not _FORMAT_REMINDER_ENABLED:
            return ""

        fmt = get_tool_format("deepseek")
        if not fmt:
            return ""

        # Detect which format is active by checking for signature strings
        if "Hermes XML" in fmt or "invoke name" in fmt:
            # Hermes XML format
            _tc_o = "<" + "tool_call" + ">"
            _tc_c = "</" + "tool_call" + ">"
            return (
                "[reminder] Tool calling structure:\n"
                + _tc_o + "\n"
                + '<invoke name="TOOL_NAME">\n'
                + '<parameter name="arg">value</parameter>\n'
                + "</invoke>\n"
                + _tc_c + "\n"
                + "[user message]"
            )
        elif "DSML" in fmt or "|DSML|" in fmt:
            # DSML format
            return (
                "[reminder] Tool calling structure:\n"
                + "<|DSML|tool_calls>\n"
                + '  <|DSML|invoke name="TOOL_NAME">\n'
                + '    <|DSML|parameter name="arg" string="true">value</|DSML|parameter>\n'
                + "  </|DSML|invoke>\n"
                + "</|DSML|tool_calls>\n"
                + "[user message]"
            )
        elif "Native" in fmt or "[] wrapper" in fmt:
            # Native bracket format
            return (
                "[reminder] Tool calling structure:\n"
                + '[{"name": "TOOL_NAME", "arguments": {ARGS}}]\n'
                + "[user message]"
            )
        elif "None" in fmt or "native API" in fmt:
            return ""
        else:
            # Default: Qwen JSON (action tags)
            _ao = "<" + "action" + ">"
            _ac = "</" + "action" + ">"
            return (
                "[reminder] Tool calling structure:\n"
                + _ao + '[{"name": "TOOL_NAME", "arguments": {ARGS}}]' + _ac + "\n"
                + _ao + '[{"name": "TOOL_A", "arguments": {}}, {"name": "TOOL_B", "arguments": {}}]' + _ac + "\n"
                + "[user message]"
            )

    def _get_dsml_guard(self, chat_id: str | None) -> str:
        """Hard reminder injected ONLY when DeepSeek leaked DSML markup.

        Armed per-chat by the leak detector after a response contained DSML
        pipe tags. Persists for the life of the client, so every subsequent
        turn in that chat keeps getting corrected.
        """
        if not chat_id or chat_id not in self._dsml_warn_chats:
            return ""
        return (
            "[WARNING - POTENTIAL CRASH] Don't use dsml, Use the defined tool calling structure given to you. "
        )

    # ------------------------------------------------------------------
    # SSE response parsing
    # ------------------------------------------------------------------

    async def _iter_with_idle_timeout(
        self,
        resp,
        timeout_secs: float = _IDLE_TIMEOUT_SECS,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Wrap SSE parsing with an idle watchdog.

        If no event arrives within `timeout_secs` — connection open, first
        token, or a mid-stream stall — raise asyncio.TimeoutError so the
        caller's retry loop drops this connection and opens a fresh one.
        """
        agen = self._iter_completion_events(resp)
        while True:
            try:
                event = await asyncio.wait_for(agen.__anext__(), timeout=timeout_secs)
            except StopAsyncIteration:
                break
            yield event

    async def _iter_completion_events(
        self,
        resp,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Parse an open /chat/completion SSE stream into Sable events.

        Extracts message_id from response events for server-side chaining.
        """
        current_frag_type: str = "RESPONSE"
        last_message_id: int | None = None  # Track assistant message ID from stream

        async for raw_line in resp.aiter_lines():
            # curl_cffi yields bytes; httpx yielded str. Normalize.
            line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else raw_line
            line = line.strip()
            if not line:
                continue
            # Accept BOTH SSE-framed lines ("data: {...}") AND a raw JSON body.
            # DeepSeek returns errors as a plain application/json body with no
            # SSE framing. The old `data: ` guard silently dropped those, which
            # is exactly why an "invalid chat session id" error surfaced to the
            # user as an empty response (and then got retried/rotated into
            # amnesia instead of being handled as a session failure).
            if line.startswith("data: "):
                payload = line[6:].strip()
            elif line.startswith("{"):
                payload = line
            else:
                continue
            if payload == "[DONE]":
                break
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue

            # Check for business-layer errors (muted account, invalid session, etc.)
            # DeepSeek returns HTTP 200 with biz_code — sometimes top-level,
            # sometimes nested one level under `data` (plain JSON error body).
            biz_code = obj.get("biz_code")
            biz_msg = obj.get("biz_msg")
            biz_data = obj.get("biz_data")
            if biz_code in (None, 0):
                _inner = obj.get("data")
                if isinstance(_inner, dict):
                    _inner_bc = _inner.get("biz_code")
                    if _inner_bc not in (None, 0):
                        biz_code = _inner_bc
                        biz_msg = _inner.get("biz_msg", biz_msg)
                        if biz_data is None:
                            biz_data = _inner.get("biz_data")
            if biz_code not in (None, 0):
                if not biz_msg:
                    biz_msg = obj.get("msg") or "unknown"
                mute_until = None
                if isinstance(biz_data, dict):
                    mute_until = biz_data.get("mute_until")
                logger.warning("DeepSeek biz_code=%s: %s (mute_until=%s)", biz_code, biz_msg, mute_until)
                yield {
                    "type": "biz_error",
                    "biz_code": biz_code,
                    "biz_msg": biz_msg,
                    "mute_until": mute_until,
                }
                return  # Stop parsing — stream is dead

            # Extract response_message_id from initial handshake event
            # Format: {"request_message_id":1,"response_message_id":2,"model_type":"default"}
            if "response_message_id" in obj:
                rid = obj["response_message_id"]
                if isinstance(rid, int):
                    last_message_id = rid

            v = obj.get("v")
            p = obj.get("p")
            o = obj.get("o")

            if v is None and p is None:
                continue
            if p == "response" and o == "BATCH":
                # BATCH events contain token usage / status, not message IDs
                continue
            if p == "response/status" and o == "SET":
                if v == "FINISHED":
                    yield {"type": "done", "parent_id": last_message_id}
                continue
            if p and "elapsed_secs" in p:
                continue
            if p == "response/fragments" and o == "APPEND":
                if isinstance(v, list) and v:
                    new_type = v[0].get("type", "RESPONSE")
                    current_frag_type = new_type
                    content = v[0].get("content", "")
                    if content:
                        etype = "thinking" if new_type == "THINK" else "answer"
                        yield {"type": etype, "text": content}
                continue
            if p == "response/fragments/-1/content":
                # Handle both o="APPEND" and o=None — DeepSeek sometimes sends
                # content continuation events without the APPEND operation tag.
                # Without this, fragments like 'tool' in '<tool_call>' get dropped,
                # producing broken tags like '<tool_call>'.
                text = v if isinstance(v, str) else ""
                if text:
                    etype = "thinking" if current_frag_type == "THINK" else "answer"
                    yield {"type": etype, "text": text}
                continue
            if isinstance(v, dict) and "response" in v:
                # Extract message_id from response metadata
                resp_data = v["response"]
                if resp_data.get("message_id"):
                    last_message_id = resp_data["message_id"]
                fragments = resp_data.get("fragments", [])
                for frag in fragments:
                    ftype = frag.get("type", "RESPONSE")
                    current_frag_type = ftype
                    content = frag.get("content", "")
                    if content:
                        etype = "thinking" if ftype == "THINK" else "answer"
                        yield {"type": etype, "text": content}
                continue
            if isinstance(v, str) and p is None:
                etype = "thinking" if current_frag_type == "THINK" else "answer"
                yield {"type": etype, "text": v}
                continue

    # ------------------------------------------------------------------
    # Public interface — streaming
    # ------------------------------------------------------------------

    async def stream_chat(
        self,
        message: str,
        model: str | None = None,
        thinking_mode: str | None = None,
        chat_id: str | None = None,
        ref_file_ids: list[str] | None = None,
        inject_instructions: bool = True,
        system_instruction: str | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Stream a chat completion. Yields Sable-compatible event dicts.

        Every Sable chat gets its own DeepSeek session (chat_session_id).
        Flow for first message in a new chat:
          1. Create a new DeepSeek session via _create_session
          2. Send a tiny "hello" with model_type="default", parent_message_id=null
          3. Capture the response_message_id from the hello
          4. Send the real message (with instructions) chained to hello's message_id,
             model_type=null
        Flow for subsequent messages:
          - Send only the current message, chained via parent_message_id to the
            last assistant response_message_id. model_type=null always.
        No full-context serialization ever happens here. Fork history injection
        is handled at the route level by prepending to the message string.
        """
        # Fresh message — give ghost another chance (curl fallback is per-message).
        self._ghost_broken = False
        thinking_enabled = str(thinking_mode or "").lower() in ("thinking", "deepthink")
        file_ids = [str(fid) for fid in (ref_file_ids or []) if str(fid).strip()]
        project_id = kwargs.pop("project_id", None)
        db_history = kwargs.pop("db_history", None)
        layout_mode = kwargs.pop("layout_mode", None)

        # Build client-side history (instructions as first entry, sliding window)
        # Used ONLY for local tracking / trimming — never serialized to DeepSeek.
        history = self._get_or_create_session(
            chat_id, inject_instructions,
            system_instruction=system_instruction,
            project_id=project_id, layout_mode=layout_mode,
        )
        # Seed from DB when session is fresh (cross-provider switch)
        if db_history and chat_id and len(history) <= 1:
            for _m in db_history:
                history.append({"role": _m["role"], "content": _m["content"]})

        # Client-side history trimming (keeps memory bounded, doesn't affect upstream)
        if chat_id:
            prefix_len = 1 if history and history[0].get("role") == "system" else 0
            total_chars = sum(_msg_chars(m) for m in history[prefix_len:])
            if total_chars > self._max_session_chars:
                history = _trim_history(history, prefix_len, self._max_session_chars)
                self._sessions[chat_id] = history

        # Determine if this is a fresh chat (no parent chain yet).
        # After a server restart, self._parent_ids is empty but the DB still
        # holds the last DeepSeek response_message_id in chats.parent_id.
        # Restore it so we don't treat an existing conversation as fresh.
        _parent_id = self._parent_ids.get(chat_id) if chat_id else None
        if chat_id and _parent_id is None:
            try:
                from server.database import get_parent_id as _db_get_parent
                _db_parent = _db_get_parent(chat_id, None)
                if _db_parent is not None:
                    _parent_id = int(_db_parent)
                    self._parent_ids[chat_id] = _parent_id
                    logger.info("DeepSeek: restored parent_id=%s for chat %s from DB", _parent_id, chat_id)
            except Exception:
                pass
        _is_fresh_chat = _parent_id is None

        # The prompt is ALWAYS just the current message (+ optional reminder).
        # Never the full serialized history. DeepSeek chains server-side.
        _reminder = self._get_reminder() or self._get_dsml_guard(chat_id)
        prompt = f"{_reminder}\n\n{message}" if _reminder else message

        # Build the instruction payload for first-message hello-seed flow.
        # On a fresh chat, we send "hello" first, then chain the real message
        # with instructions as the second message.
        _instruction_payload: str | None = None
        if _is_fresh_chat and inject_instructions and history:
            sys_msgs = [m for m in history if m.get("role") == "system"]
            if sys_msgs:
                _instruction_payload = "\n\n".join(m.get("content", "") for m in sys_msgs)

        # Ensure rotation pool is loaded
        if not self._rotate_tokens:
            self._init_rotation()

        if not _auto_rotate_enabled("deepseek"):
            attempts = 1
        else:
            attempts = max(1, len(self._rotate_tokens))
        last_err = self._last_prepare_error or "unknown"
        _MAX_RETRIES_PER_TOKEN = 8
        _RETRY_DELAY_SECS = 5.0
        token_errors: list[str] = []

        def _all_failed_message() -> str:
            if not token_errors:
                return f"All DeepSeek tokens failed ({last_err})."
            lines = "\n".join(f"  • {e}" for e in token_errors)
            return f"All {len(token_errors)} DeepSeek token(s) failed:\n{lines}"

        # Anti-burst jitter
        try:
            from connectors.common.jitter import jitter_for as _jitter_for, sleep_jitter as _sleep_jitter
            _jdelay = _jitter_for("deepseek")
            if _jdelay:
                yield {"type": "status", "message": f"deepseek_delay_{_jdelay:.1f}s"}
                await _sleep_jitter(_jdelay)
        except Exception as _jexc:
            logger.debug("DeepSeek jitter skipped: %s", _jexc)

        _prev_token: str | None = None
        for _attempt in range(attempts):
            # SAME token + SAME chat => NEVER drop the cached session.
            # All retries stay in the same upstream conversation. A new
            # session is ONLY created for a real token change (different
            # account = different session namespace) or an explicit fork.
            if _attempt > 0 and chat_id:
                _tok = self._current_rotate_token
                if _tok and _prev_token and _tok != _prev_token:
                    self._chat_sessions.pop((_tok, chat_id), None)
                    self._chat_sessions.pop((_tok, "__ephemeral__"), None)
                    logger.info("DeepSeek rotation #%d: dropped session for chat %s, token %s",
                                _attempt, chat_id, self._mask_token(_tok))

            # Bound the whole prepare step (session create + PoW + rotation)
            # so a wedged upstream can't stall the stream silently.
            yield {"type": "status", "message": "deepseek_prepare"}
            try:
                prepared = await asyncio.wait_for(
                    self._prepare_request_with_rotation(chat_id=chat_id), timeout=90)
            except asyncio.TimeoutError:
                logger.warning("DeepSeek: prepare timed out after 90s (chat %s)", chat_id)
                prepared = None
            if prepared is None:
                yield self.ctx.error(_all_failed_message())
                return
            session_id, headers = prepared

            # --- Hello seed flow for fresh chats ---
            # Send "hello" first (model_type=default, parent=null), capture the
            # response_message_id, then chain the real message to it.
            current_parent_id = _parent_id
            current_model_type = None  # Subsequent messages always use null

            if _is_fresh_chat and current_parent_id is None:
                _seed_id = await self._seed_hello_message(session_id, headers)
                if _seed_id is not None:
                    current_parent_id = _seed_id
                    current_model_type = None  # Message #2+ must be null
                    logger.info("DeepSeek hello-seed ok: chaining to parent=%s for chat %s", _seed_id, chat_id)
                    # CRITICAL: The hello seed consumed the PoW nonce in `headers`.
                    # DeepSeek PoW challenges are single-use — reusing the same
                    # X-DS-PoW-Response on the real message causes silent failure
                    # (empty response or rejection). Solve a fresh challenge now.
                    try:
                        headers = await self._refresh_pow_header(headers)
                        logger.debug("DeepSeek: refreshed PoW after hello seed for chat %s", chat_id)
                    except Exception as _pow_exc:
                        logger.warning("DeepSeek: failed to refresh PoW after hello seed: %s", _pow_exc)
                else:
                    # Hello seed failed — send as first message with model_type=default
                    current_model_type = "default"
                    logger.warning("DeepSeek hello-seed failed for chat %s, sending as first message", chat_id)

            # Build the actual prompt. For fresh chats with instructions, prepend
            # the system instructions to the first real message so DeepSeek gets them.
            current_prompt = prompt
            if _is_fresh_chat and _instruction_payload:
                current_prompt = f"{_instruction_payload}\n\n{prompt}"

            body = {
                "chat_session_id": session_id,
                "parent_message_id": current_parent_id,
                "model_type": current_model_type,
                "prompt": current_prompt,
                "ref_file_ids": file_ids,
                "thinking_enabled": thinking_enabled,
                "search_enabled": True,
                "action": None,
                "preempt": False,
            }

            _log_raw("USER_MESSAGE (prompt sent)", current_prompt)

            # Resolve the transport once per token attempt.
            # Ghost (browser identity) is preferred; curl is the fallback.
            # Ghost resolution launches Playwright/CDP — bound it so a wedged
            # browser tab can't hang the stream. Falls back to curl on timeout.
            try:
                _ghost = await asyncio.wait_for(self._resolve_ghost(), timeout=20)
            except asyncio.TimeoutError:
                logger.warning("DeepSeek: ghost resolve timed out after 20s — using curl")
                _ghost = None
            if _ghost is not None:
                yield {"type": "status", "message": "deepseek_transport_ghost"}
            else:
                yield {"type": "status", "message": "deepseek_transport_curl"}

            # Session-invalid recovery state — a stale chat_session_id is a
            # SESSION failure, not a token failure. We recreate the upstream
            # session and re-seed it with local history exactly once per
            # stream_chat() call.
            _session_recreated = False

            # Per-token retry loop
            for _retry in range(_MAX_RETRIES_PER_TOKEN):
                # PoW nonces are single-use. The header solved in
                # _prepare_request_with_rotation (or after the hello seed) is only
                # valid for the FIRST send. Replaying it on a retry makes DeepSeek
                # return a silent empty 200 - which is why the retry chain never
                # succeeded. Solve a fresh challenge before each retry.
                if _retry > 0:
                    try:
                        headers = await self._refresh_pow_header(headers)
                    except Exception as _pow_exc:
                        last_err = f"pow_refresh_failed: {_pow_exc}"
                        logger.warning(
                            "DeepSeek: PoW refresh failed on retry %d (%s), rotating...",
                            _retry, _pow_exc,
                        )
                        break
                try:
                    full_answer = ""
                    full_thinking = ""

                    async with await self._completion_opener(headers, body, _ghost) as resp:
                        if resp.status_code in (401, 403, 429):
                            await resp.content
                            last_err = f"HTTP {resp.status_code}"
                            logger.warning("DeepSeek auth error (%s), rotating immediately...", last_err)
                            break

                        if resp.status_code != 200:
                            error_body = await resp.content
                            last_err = f"HTTP {resp.status_code}: {error_body.decode(errors='replace')[:200]}"
                            logger.warning("DeepSeek HTTP error (%s), rotating immediately...", last_err)
                            break

                        _response_parent_id: int | None = None
                        _got_biz_error = False
                        _session_invalid = False
                        async for event in self._iter_with_idle_timeout(resp):
                            etype = event.get("type")
                            if etype == "answer":
                                full_answer += event.get("text", "")
                            elif etype == "thinking":
                                full_thinking += event.get("text", "")
                            elif etype == "done":
                                _response_parent_id = event.get("parent_id")
                            elif etype == "biz_error":
                                _got_biz_error = True
                                _bc = event.get("biz_code")
                                _bm = event.get("biz_msg", "unknown")
                                last_err = f"biz_code:{_bc} ({_bm})"
                                logger.warning("DeepSeek biz_error: %s", last_err)
                                if _is_session_invalid(_bc, _bm):
                                    _session_invalid = True
                                break
                            yield event

                        if _got_biz_error:
                            # --- Session-invalid recovery ---
                            # The upstream chat_session_id is gone (stale UUID,
                            # deleted server-side, or bound to a different
                            # account). Token rotation will NOT fix this, and
                            # nulling the parent chain just causes amnesia. The
                            # correct recovery: create a fresh session and
                            # re-seed it with the ENTIRE local conversation so
                            # the model picks up where we left off.
                            if _session_invalid and chat_id and not _session_recreated:
                                _session_recreated = True
                                logger.warning(
                                    "DeepSeek: session invalid for chat %s — recreating session and re-seeding %d history msgs",
                                    chat_id, len(history),
                                )
                                yield {"type": "status", "message": "deepseek_session_recreate"}
                                _tok = self._current_rotate_token
                                if not _tok:
                                    last_err = "session_recreate_no_token"
                                    break
                                # Drop the poisoned session from every cache slot
                                self._chat_sessions.pop((_tok, chat_id), None)
                                self._chat_sessions.pop((_tok, "__ephemeral__"), None)
                                try:
                                    _auth = self._auth_headers_for(_tok)
                                    _new_sid = await self._create_session(headers=_auth)
                                except Exception as _recreate_exc:
                                    last_err = f"session_recreate_failed: {_recreate_exc}"
                                    logger.error("DeepSeek: failed to recreate session for %s: %s", chat_id, _recreate_exc)
                                    break
                                self._chat_sessions[(_tok, chat_id)] = _new_sid
                                try:
                                    from server.database import set_upstream_session_id as _set_usid
                                    _set_usid(chat_id, _new_sid)
                                except Exception:
                                    pass
                                # New session has no nodes — clear the stale parent.
                                self._parent_ids[chat_id] = None
                                # Point the Referer at the fresh session too.
                                headers["Referer"] = f"https://chat.deepseek.com/a/chat/s/{_new_sid}"

                                # Serialize the full local conversation into the
                                # first message of the new session. Skip the
                                # system prompt — it's re-injected separately.
                                _hist_lines: list[str] = []
                                for _m in history:
                                    _role = _m.get("role", "user")
                                    if _role == "system":
                                        continue
                                    _content = _m.get("content", "")
                                    if _content:
                                        # Strip all DSML artifacts (full/partial) before re-seeding
                                        _clean_content = _strip_dsml(_content)
                                        if _clean_content:
                                            _hist_lines.append(f"{_role}: {_clean_content}")
                                _hist_block = "\n\n".join(_hist_lines)
                                _recovered = (
                                    "The previous upstream session was lost (invalid chat session id). "
                                    "The full conversation so far is reproduced below — continue naturally, "
                                    "do not acknowledge this preamble, do not repeat the history back.\n\n"
                                    "=== CONVERSATION HISTORY ===\n"
                                    f"{_hist_block}\n"
                                    "=== END HISTORY ===\n\n"
                                    f"{message}"
                                )
                                _reminder_r = self._get_reminder() or self._get_dsml_guard(chat_id)
                                if _reminder_r:
                                    _recovered = f"{_reminder_r}\n\n{_recovered}"
                                body["chat_session_id"] = _new_sid
                                body["parent_message_id"] = None
                                body["model_type"] = "default"
                                body["prompt"] = _recovered
                                logger.info(
                                    "DeepSeek: re-seeded fresh session %s for chat %s with %d history msgs",
                                    _new_sid[:12], chat_id, len(_hist_lines),
                                )
                                # Let the next loop iteration refresh PoW and retry.
                                _got_biz_error = False
                                _session_invalid = False
                                continue
                            break

                    _log_raw("RESPONSE (raw)", (full_thinking + "\n---\n" + full_answer) if full_thinking else full_answer)

                    # Empty response detection — NO unchained fallback.
                    # An empty 200 is treated like any other transient failure:
                    # retry the SAME chained request (with a fresh PoW each time)
                    # up to the per-token cap, then rotate. Dropping the parent
                    # chain here silently re-rooted the conversation and orphaned
                    # all prior context — worse than a clean rotation.
                    if not full_answer and not full_thinking:
                        last_err = "empty_response"
                        self._mark_failure()
                        # If we already captured a response message_id from the
                        # 'done' event, DeepSeek DID create the node upstream —
                        # only delivery died (mid-stream disconnect). Retrying
                        # with the SAME parent_message_id would branch the tree
                        # (X→Y AND X→Z), which is exactly what poisons the chain
                        # on restart. So: commit whatever id we got and bail to
                        # the caller instead of re-sending.
                        if _response_parent_id is not None:
                            logger.warning(
                                "DeepSeek empty body but message_id=%s received for chat %s — "
                                "committing id, NOT retrying (upstream node exists)",
                                _response_parent_id, chat_id,
                            )
                            if chat_id:
                                self._parent_ids[chat_id] = _response_parent_id
                            yield {
                                "type": "error",
                                "message": "Response interrupted mid-stream; conversation state preserved.",
                            }
                            return
                        if _retry < _MAX_RETRIES_PER_TOKEN - 1:
                            logger.warning("DeepSeek empty response, retry %d/%d in %.0fs...",
                                           _retry + 1, _MAX_RETRIES_PER_TOKEN, _RETRY_DELAY_SECS)
                            yield {"type": "status", "message": f"deepseek_retry_{_retry + 1}"}
                            await asyncio.sleep(_RETRY_DELAY_SECS)
                            continue
                        else:
                            logger.warning("DeepSeek empty response after %d retries, rotating...",
                                           _MAX_RETRIES_PER_TOKEN)
                            break

                    # DSML leak detection — if the model emitted DSML/pipe tags,
                    # arm the hard reminder for this chat's next turn.
                    if chat_id and _DSML_LEAK_RE.search(full_thinking + "\n" + full_answer):
                        if chat_id not in self._dsml_warn_chats:
                            logger.warning(
                                "DeepSeek leaked DSML markup for chat %s — arming hard reminder",
                                chat_id,
                            )
                        self._dsml_warn_chats.add(chat_id)

                    # Connection is healthy again — reset failure counter.
                    self._mark_success()

                    # Success — update client-side history and parent chain
                    clean_answer = strip_summarize_tag(full_answer)
                    history.append({"role": "user", "content": message})
                    response_content = full_thinking + clean_answer if full_thinking else clean_answer
                    if response_content:
                        history.append({"role": "assistant", "content": response_content})

                    # Store the assistant message ID for next-turn chaining.
                    # SAME token + SAME chat => NEVER null the parent on a
                    # missing message_id. A missing id is a transient upstream
                    # glitch; nulling it would silently re-root the conversation
                    # (full amnesia on the next turn). Keep the previous parent
                    # so the chain survives.
                    if chat_id and _response_parent_id:
                        self._parent_ids[chat_id] = _response_parent_id
                    elif chat_id and not _response_parent_id:
                        logger.warning(
                            "DeepSeek: no message_id in response for chat %s — keeping previous parent=%s (chain preserved)",
                            chat_id, self._parent_ids.get(chat_id),
                        )

                    # Trim client-side history if needed
                    if chat_id:
                        prefix_len = 1 if history and history[0].get("role") == "system" else 0
                        total_chars = sum(_msg_chars(m) for m in history[prefix_len:])
                        if total_chars > self._max_session_chars:
                            self._sessions[chat_id] = _trim_history(history, prefix_len, self._max_session_chars)

                    return  # Success — done

                except (asyncio.TimeoutError, CurlReadTimeout) as _idle_exc:
                    # Idle gate fired — either the connect/header phase (bounded
                    # in _completion_opener) or a mid-stream stall (bounded in
                    # _iter_with_idle_timeout) produced no bytes within 30s.
                    # This is almost always a stale/zombie pooled socket after a
                    # network change. Don't burn 8×35s per token — fail fast so
                    # _get_http() recycles the pool on the next attempt.
                    if _ghost is not None:
                        # Ghost raised the idle gate — the browser may have been
                        # closed. Respawn it for the NEXT retry instead of
                        # permanently degrading to curl. Keep _ghost set so the
                        # retry loop re-resolves a fresh session.
                        logger.warning("DeepSeek ghost idle-failed (%s) — will respawn browser on retry", _idle_exc)
                        _ghost = await self._resolve_ghost(force_respawn=True)
                    last_err = f"idle_timeout ({_IDLE_TIMEOUT_SECS:.0f}s)"
                    self._mark_failure()
                    if _response_parent_id is not None and chat_id:
                        # Upstream node was created before the stall — preserve it.
                        self._parent_ids[chat_id] = _response_parent_id
                    logger.warning("DeepSeek idle timeout (%s) — breaking to recycle pool", last_err)
                    break
                except Exception as exc:
                    last_err = f"{type(exc).__name__}: {exc}"
                    self._mark_failure()
                    if isinstance(exc, RuntimeError) and "ghost" in str(exc).lower():
                        # In-page fetch died (page closed, bridge gone, fetch
                        # error). Respawn the browser so the next retry uses a
                        # fresh ghost, rather than silently dropping to curl.
                        logger.warning("DeepSeek ghost stream failed (%s) — respawning browser", exc)
                        _ghost = await self._resolve_ghost(force_respawn=True)
                    # Connection-class errors (reset/refused/DNS/TLS) mean the pool
                    # is poisoned — break immediately instead of hammering dead sockets.
                    _conn_cls = (
                        ConnectionError, OSError, TimeoutError,
                    )
                    try:
                        from curl_cffi.requests.exceptions import (
                            ConnectionError as CurlConnErr,
                            ConnectTimeout as CurlConnectTimeout,
                        )
                        _conn_cls = (*_conn_cls, CurlConnErr, CurlConnectTimeout)
                    except Exception:
                        pass
                    if isinstance(exc, _conn_cls):
                        logger.warning(
                            "DeepSeek connection error (%s) — breaking to recycle pool", last_err
                        )
                        break
                    if _retry < _MAX_RETRIES_PER_TOKEN - 1:
                        logger.warning("DeepSeek error (%s), retry %d/%d in %.0fs...",
                                       last_err, _retry + 1, _MAX_RETRIES_PER_TOKEN, _RETRY_DELAY_SECS)
                        yield {"type": "status", "message": f"deepseek_retry_{_retry + 1}"}
                        await asyncio.sleep(_RETRY_DELAY_SECS)
                        continue
                    else:
                        logger.warning("DeepSeek error after %d retries (%s), rotating...",
                                       _MAX_RETRIES_PER_TOKEN, last_err)
                        break

            # All retries exhausted for this token → record why, then rotate.
            _old_token = self._current_rotate_token
            token_errors.append(f"token {self._mask_token(_old_token)}: {last_err}")
            self._advance_rotation()
            _new_token = self._current_rotate_token
            _prev_token = _old_token

            # SAME token + SAME chat => NEVER touch the parent chain.
            # Every retry stays in the same upstream conversation. Only a
            # real token change (different account) may reset the chain,
            # because the new account cannot see the old session's messages.
            if chat_id and _new_token != _old_token:
                self._parent_ids[chat_id] = None

            if _new_token != _old_token:
                logger.warning("DeepSeek rotated token: %s → %s (reason: %s)",
                               self._mask_token(_old_token), self._mask_token(_new_token), last_err)
                yield {
                    "type": "token_rotation",
                    "reason": last_err,
                    "from_token": self._mask_token(_old_token),
                    "to_token": self._mask_token(_new_token),
                    "from_index": (_attempt) % len(self._rotate_tokens) if self._rotate_tokens else 0,
                    "to_index": self._rotate_idx,
                    "total_tokens": len(self._rotate_tokens),
                    "new_chat": True,
                }
            else:
                logger.warning("DeepSeek retries exhausted on token %s (reason: %s), no rotation available — chain preserved",
                               self._mask_token(_old_token), last_err)

        yield self.ctx.error(_all_failed_message())

    # ------------------------------------------------------------------
    # Public interface — non-streaming
    # ------------------------------------------------------------------

    async def chat(
        self,
        message: str,
        model: str | None = None,
        thinking_mode: str | None = None,
        chat_id: str | None = None,
        ref_file_ids: list[str] | None = None,
        inject_instructions: bool = True,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Non-streaming chat. Returns {answer, thinking, parent_id, error}."""
        answer_parts: list[str] = []
        thinking_parts: list[str] = []
        parent_id: str | None = None
        error: str | None = None

        async for event in self.stream_chat(
            message,
            model=model,
            thinking_mode=thinking_mode,
            chat_id=chat_id,
            ref_file_ids=ref_file_ids,
            inject_instructions=inject_instructions,
            **kwargs,
        ):
            etype = event.get("type")
            if etype == "answer":
                answer_parts.append(event.get("text", ""))
            elif etype == "thinking":
                thinking_parts.append(event.get("text", ""))
            elif etype == "done":
                parent_id = event.get("parent_id")
            elif etype == "error":
                error = event.get("message", "Unknown error")

        return {
            "answer": "".join(answer_parts),
            "thinking": "".join(thinking_parts),
            "parent_id": parent_id,
            "error": error,
        }


# Module-level client pool: account_name → DeepSeekClient
_clients: dict[str, DeepSeekClient] = {}
_default_client: DeepSeekClient | None = None


def get_client(account: str | None = None) -> DeepSeekClient:
    """Get or create a DeepSeek client for the given account.

    All clients now use automatic token rotation + client-side history (mirrors
    Gemini/Mistral pattern). No opt-in flag needed.

    account=None → default client (resolves active account dynamically).
    account="browser-data-acc7" → dedicated client pinned to that account.
    """
    if account is None:
        global _default_client
        if _default_client is None:
            _default_client = DeepSeekClient()
        return _default_client
    if account not in _clients:
        _clients[account] = DeepSeekClient(account=account)
    return _clients[account]


async def cancel_active_streams() -> bool:
    """Cancel any in-flight ghost stream across all DeepSeek clients.

    Used by /api/chat/stop. Iterates every cached client (each owns one
    account's ghost session) plus the default. Returns True if at least one
    stream was torn down.
    """
    cancelled = False
    clients: list[DeepSeekClient] = list(_clients.values())
    if _default_client is not None:
        clients.append(_default_client)
    for c in clients:
        try:
            if await c.cancel():
                cancelled = True
        except Exception:
            pass
    return cancelled
