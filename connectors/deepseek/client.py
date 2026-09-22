
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
import subprocess
import sys
from collections.abc import AsyncGenerator, Awaitable, Callable
from pathlib import Path
from typing import Any

from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import ReadTimeout as CurlReadTimeout

from connectors.common.instruction_builder import get_tool_format

logger = logging.getLogger("sable.deepseek_api")

BASE_URL = "https://chat.deepseek.com"
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
    """Get the active account name. Delegates to engine.config.get_active_account()."""
    from engine.config import get_active_account
    return get_active_account()


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

    store: dict[str, list[str]] = {}
    for key, val in raw.items():
        if isinstance(val, list):
            valid = [t for t in val if t and t != "None"]
        elif isinstance(val, str) and val and val != "None":
            # Migrate old flat format → list
            valid = [val]
        else:
            valid = []
        # Collapse to the single CURRENT token. An account has exactly one
        # live auth token; anything older is a stale duplicate left behind by
        # a refresh. Keeping only the latest also cleans the file on next save.
        store[key] = valid[-1:]

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
    store[acct] = [token]
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
        self._last_prepare_error: str | None = None  # surfaced when all tokens fail

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
        if self._token:
            return self._token
        # Check persisted user preference before generic fallback
        preferred = self._get_preferred_token_from_settings()
        if preferred:
            self._token = preferred
            return self._token
        # Resolve from per-account store (with fallback)
        self._token = get_token_for_account(self._account)
        return self._token

    @property
    def is_available(self) -> bool:
        """Whether the connector has valid credentials."""
        return self.token is not None

    def set_token(self, token: str, account: str | None = None, *, persist: bool = True) -> None:
        """Set and (optionally) persist token under the given account.

        Pass persist=False when the token came from a cross-account fallback
        (e.g. rotation handoff) so it is used in-memory but NOT written under
        an account it doesn't belong to.
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
        fp = get_fp_for_account(self._account)
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
        from engine.config import BROWSER_DATA_DIR
        return BROWSER_DATA_DIR

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
            self.set_token(token)
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
        self.set_token(token)
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

    async def _get_http(self) -> AsyncSession:
        if self._http is None:
            # impersonate="chrome" replicates Chromium's exact TLS fingerprint
            # (cipher suites, extensions, ALPN, HTTP/2 SETTINGS frame).
            # This is what WAF checks — raw httpx/OpenSSL mismatches the Chrome UA.
            self._http = AsyncSession(impersonate="chrome", timeout=120)
        return self._http

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

    # ------------------------------------------------------------------
    # Completion transport — ghost-tab (browser identity) or curl (fallback)
    # ------------------------------------------------------------------

    async def _resolve_ghost(self):
        """Return a live GhostSession for this account, or None if unavailable.
        Never raises — a dead/missing ghost just means we fall back to curl."""
        try:
            from connectors.deepseek import ghost as _ghost
            if not _ghost.ghost_enabled():
                return None
            return await _ghost.GhostSession.for_account(self.account)
        except Exception as exc:
            logger.warning("Ghost unavailable (%s) — using curl", exc)
            return None

    async def _completion_opener(self, headers: dict[str, str], body: dict[str, Any], ghost):
        """Return an async context manager yielding a response-like object.
        Prefers ghost (in-page fetch, browser identity). Falls back to curl.
        """
        if ghost is not None:
            token = headers.get("Authorization", "")
            if token.startswith("Bearer "):
                token = token[7:]
            pow_hdr = headers.get("X-DS-PoW-Response", "")
            try:
                r = await ghost.fetch_completion(token, body, pow_hdr)
                from connectors.deepseek.ghost import GhostResponse
                return GhostResponse(int(r.get("status", 0)), r.get("raw", ""))
            except Exception as exc:
                logger.warning("Ghost fetch failed (%s) — falling back to curl for this request", exc)
        http = await self._get_http()
        return http.stream("POST", f"{BASE_URL}/api/v0/chat/completion", json=body, headers=headers)

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
                async for event in self._iter_completion_events(resp):
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

    # ------------------------------------------------------------------
    # SSE response parsing
    # ------------------------------------------------------------------

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
            if not line.startswith("data: "):
                continue
            payload = line[6:]
            if payload == "[DONE]":
                break
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue

            # Check for business-layer errors (muted account, etc.)
            # DeepSeek returns HTTP 200 with biz_code for account-level issues
            biz_code = obj.get("biz_code")
            if biz_code is not None and biz_code != 0:
                biz_msg = obj.get("biz_msg", "unknown")
                mute_until = None
                biz_data = obj.get("biz_data")
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
        _reminder = self._get_reminder()
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

        for _attempt in range(attempts):
            # On rotation, drop this chat's cached session so we get a fresh one
            if _attempt > 0 and chat_id:
                _tok = self._current_rotate_token
                if _tok:
                    self._chat_sessions.pop((_tok, chat_id), None)
                    self._chat_sessions.pop((_tok, "__ephemeral__"), None)
                    logger.info("DeepSeek rotation #%d: dropped session for chat %s, token %s",
                                _attempt, chat_id, self._mask_token(_tok))

            prepared = await self._prepare_request_with_rotation(chat_id=chat_id)
            if prepared is None:
                yield {"type": "error", "message": _all_failed_message()}
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
                        _fresh_challenge = await self._get_challenge(headers=headers)
                        loop = asyncio.get_running_loop()
                        _fresh_nonce = await loop.run_in_executor(None, self._solve_pow, _fresh_challenge)
                        _fresh_pow = self._build_pow_header(_fresh_challenge, _fresh_nonce)
                        headers = {**headers, "X-DS-PoW-Response": _fresh_pow}
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
            _ghost = await self._resolve_ghost()
            if _ghost is not None:
                yield {"type": "status", "message": "deepseek_transport_ghost"}
            else:
                yield {"type": "status", "message": "deepseek_transport_curl"}

            # Per-token retry loop
            for _retry in range(_MAX_RETRIES_PER_TOKEN):
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
                        async for event in self._iter_completion_events(resp):
                            etype = event.get("type")
                            if etype == "answer":
                                full_answer += event.get("text", "")
                            elif etype == "thinking":
                                full_thinking += event.get("text", "")
                            elif etype == "done":
                                _response_parent_id = event.get("parent_id")
                            elif etype == "biz_error":
                                _got_biz_error = True
                                last_err = f"biz_code:{event.get('biz_code')} ({event.get('biz_msg', 'unknown')})"
                                logger.warning("DeepSeek biz_error: %s", last_err)
                                break
                            yield event

                        if _got_biz_error:
                            break

                    _log_raw("RESPONSE (raw)", (full_thinking + "\n---\n" + full_answer) if full_thinking else full_answer)

                    # Empty response detection
                    if not full_answer and not full_thinking:
                        last_err = "empty_response"
                        # Self-heal: DeepSeek returns an empty 200 when a large
                        # chained message follows the tiny hello seed. Drop the
                        # parent chain and resend UNCHAINED with model_type=default
                        # (the known-good first-message path) instead of burning
                        # retries on a request that will never answer.
                        if body.get("parent_message_id") is not None:
                            logger.warning("DeepSeek empty response on chained message — dropping parent chain, retrying unchained")
                            body["parent_message_id"] = None
                            body["model_type"] = "default"
                            yield {"type": "status", "message": "deepseek_retry_unchained"}
                            await asyncio.sleep(_RETRY_DELAY_SECS)
                            continue
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

                    # Success — update client-side history and parent chain
                    clean_answer = strip_summarize_tag(full_answer)
                    history.append({"role": "user", "content": message})
                    response_content = full_thinking + clean_answer if full_thinking else clean_answer
                    if response_content:
                        history.append({"role": "assistant", "content": response_content})

                    # Store the assistant message ID for next-turn chaining
                    if chat_id and _response_parent_id:
                        self._parent_ids[chat_id] = _response_parent_id
                    elif chat_id and not _response_parent_id:
                        self._parent_ids[chat_id] = None
                        logger.warning("DeepSeek: no message_id in response for chat %s, chain reset", chat_id)

                    # Trim client-side history if needed
                    if chat_id:
                        prefix_len = 1 if history and history[0].get("role") == "system" else 0
                        total_chars = sum(_msg_chars(m) for m in history[prefix_len:])
                        if total_chars > self._max_session_chars:
                            self._sessions[chat_id] = _trim_history(history, prefix_len, self._max_session_chars)

                    return  # Success — done

                except CurlReadTimeout:
                    last_err = "timeout (120s)"
                    if _retry < _MAX_RETRIES_PER_TOKEN - 1:
                        logger.warning("DeepSeek timeout, retry %d/%d in %.0fs...",
                                       _retry + 1, _MAX_RETRIES_PER_TOKEN, _RETRY_DELAY_SECS)
                        yield {"type": "status", "message": f"deepseek_retry_{_retry + 1}"}
                        await asyncio.sleep(_RETRY_DELAY_SECS)
                        continue
                    else:
                        logger.warning("DeepSeek timeout after %d retries, rotating...",
                                       _MAX_RETRIES_PER_TOKEN)
                        break
                except Exception as exc:
                    last_err = f"{type(exc).__name__}: {exc}"
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

            # All retries exhausted for this token → record why, then rotate
            if chat_id:
                self._parent_ids[chat_id] = None
            _old_token = self._current_rotate_token
            token_errors.append(f"token {self._mask_token(_old_token)}: {last_err}")
            self._advance_rotation()
            _new_token = self._current_rotate_token
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

        yield {"type": "error", "message": _all_failed_message()}

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
