
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
import subprocess
from collections.abc import AsyncGenerator, Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("sable.deepseek_api")

BASE_URL = "https://chat.deepseek.com"
SOLVER_PATH = Path(__file__).resolve().parent / "pow_solver" / "pow_solver"

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

# Legacy single-token cache (migrated on first access)
_LEGACY_TOKEN_CACHE = Path(__file__).resolve().parent / ".token_cache.json"
_LEGACY_MIGRATED = False  # guard so migration only runs once


# --------------------------------------------------------------------------
# Per-account token store helpers (list-based — tokens accumulate)
# --------------------------------------------------------------------------

def _resolve_active_account() -> str:
    """Get the active account name from the browser-data symlink target."""
    symlink = _SYSTEM_DIR / "browser-data"
    try:
        target = symlink.resolve()
        return target.name  # e.g. "browser-data-acc15"
    except OSError:
        return "browser-data"


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
            store[key] = [t for t in val if t and t != "None"]
        elif isinstance(val, str) and val and val != "None":
            # Migrate old flat format → list
            store[key] = [val]
        else:
            store[key] = []

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


def get_token_for_account(account: str | None = None) -> str | None:
    """Get the most recent token for an account, falling back to any available.

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


def save_token_for_account(token: str, account: str | None = None) -> None:
    """Append a token to the account's list (deduped, capped). Never replaces old tokens.

    Keeps at most MAX_TOKENS_PER_ACCOUNT entries per account (FIFO eviction of oldest).
    Uses atomic write to prevent corruption from concurrent saves.
    """
    if not token or token == "None":
        return
    acct = account or _resolve_active_account()
    store = _load_token_store()
    existing = store.get(acct, [])
    if token not in existing:
        existing.append(token)
    # Cap: keep only the most recent N tokens
    if len(existing) > MAX_TOKENS_PER_ACCOUNT:
        existing = existing[-MAX_TOKENS_PER_ACCOUNT:]
    store[acct] = existing
    _save_token_store(store)

CLIENT_HEADERS = {
    "x-client-version": "2.3.0",
    "x-client-platform": "web",
    "x-client-bundle-id": "com.deepseek.chat",
    "x-client-locale": "en_US",
}

# Client-side history cap (matches Gemini/Mistral sliding window)
_MAX_SESSION_CHARS = 100_000

from connectors.common.context_summarizer import (
    should_inject_hint, should_force_summarize, get_hint_text,
    extract_summarize_tag, strip_summarize_tag, build_summary_prompt,
    rewrite_history_with_summary, compute_force_cut_index,
)


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
_cached_version: int = -1


def _load_instructions(project_id: str | None = None) -> str:
    """Load instruction context for first-message injection. Project-aware."""
    global _instruction_cache, _cached_project_id, _cached_version
    from connectors.common.instruction_builder import get_instruction_version
    current_version = get_instruction_version()
    # Invalidate cache when project or instruction version changes
    if project_id != _cached_project_id or current_version != _cached_version:
        _instruction_cache = None
        _cached_project_id = project_id
        _cached_version = current_version
    if _instruction_cache is not None:
        return _instruction_cache
    from connectors.common.instruction_builder import build_instructions
    _instruction_cache = build_instructions(project_id=project_id, provider="deepseek")
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
        self._http: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()
        # Client-side conversation history: chat_id → [message dicts]
        self._sessions: dict[str, list[dict[str, Any]]] = {}
        # Token rotation state (always active — like Gemini/Mistral key rotation)
        self._rotate_tokens: list[str] = []
        self._rotate_idx: int = 0
        self._rotate_sessions: dict[str, str] = {}  # token → deepseek session_id

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
        # Resolve from per-account store (with fallback)
        self._token = get_token_for_account(self._account)
        return self._token

    @property
    def is_available(self) -> bool:
        """Whether the connector has valid credentials."""
        return self.token is not None

    def set_token(self, token: str, account: str | None = None) -> None:
        """Set and persist token under the given account."""
        self._token = token
        save_token_for_account(token, account or self._account)

    # ------------------------------------------------------------------
    # Token rotation (automatic round-robin + failover, like Gemini/Mistral)
    # ------------------------------------------------------------------

    def _init_rotation(self) -> None:
        """Load unique tokens from store for rotation."""
        self._rotate_tokens = get_unique_tokens()
        self._rotate_idx = 0
        logger.info("Token rotation initialized: %d unique tokens", len(self._rotate_tokens))

    @property
    def _current_rotate_token(self) -> str | None:
        if not self._rotate_tokens:
            return None
        return self._rotate_tokens[self._rotate_idx % len(self._rotate_tokens)]

    def _advance_rotation(self) -> None:
        if self._rotate_tokens:
            self._rotate_idx = (self._rotate_idx + 1) % len(self._rotate_tokens)

    def _auth_headers_for(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}", **CLIENT_HEADERS}

    def _auth_headers(self) -> dict[str, str]:
        """Auth headers using current rotation token (or fallback single token)."""
        if self._rotate_tokens:
            tok = self._current_rotate_token
        else:
            tok = self.token
        if not tok:
            raise DeepSeekAPIError("No DeepSeek token available.")
        return {"Authorization": f"Bearer {tok}", **CLIENT_HEADERS}

    async def _prepare_request_with_rotation(self) -> tuple[str, dict[str, str]] | None:
        """Pick a usable token, get/create its session, solve PoW.

        Returns (session_id, headers) or None if all tokens exhausted.
        Always uses parent_id=None (stateless) — context is in the prompt.
        Advances past tokens that fail; does NOT advance on success (caller does).
        """
        if not self._rotate_tokens:
            self._init_rotation()
        if not self._rotate_tokens:
            return None

        tried = 0
        total = len(self._rotate_tokens)

        while tried < total:
            token = self._current_rotate_token
            if not token:
                self._advance_rotation()
                tried += 1
                continue

            auth = self._auth_headers_for(token)

            # One persistent session per token (avoids ~460ms create overhead per turn)
            if token not in self._rotate_sessions:
                try:
                    session_id = await self._create_session(headers=auth)
                    self._rotate_sessions[token] = session_id
                except Exception as exc:
                    logger.warning("Rotation: session failed for %s...: %s", token[:10], exc)
                    self._advance_rotation()
                    tried += 1
                    continue
            session_id = self._rotate_sessions[token]

            try:
                challenge = await self._get_challenge(headers=auth)
                loop = asyncio.get_running_loop()
                nonce = await loop.run_in_executor(None, self._solve_pow, challenge)
                pow_header = self._build_pow_header(challenge, nonce)
            except Exception as exc:
                logger.warning("Rotation: PoW failed for %s...: %s", token[:10], exc)
                self._advance_rotation()
                tried += 1
                continue

            headers = {**auth, "X-DS-PoW-Response": pow_header, "Content-Type": "application/json"}
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

    async def _get_http(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                base_url=BASE_URL,
                timeout=httpx.Timeout(120.0, connect=10.0),
                follow_redirects=True,
            )
        return self._http

    async def close(self) -> None:
        if self._http and not self._http.is_closed:
            await self._http.aclose()
            self._http = None

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
            "/api/v0/chat/create_pow_challenge",
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
                "/api/v0/chat/create_pow_challenge",
                json={"target_path": "/api/v0/chat/completion"},
                headers=self._auth_headers(),
            )
            if resp.status_code == 401:
                raise DeepSeekAPIError("Token still invalid after browser refresh. Log in to chat.deepseek.com.")
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise DeepSeekAPIError(f"Challenge failed: {data.get('msg', 'unknown')}")
        return data["data"]["biz_data"]["challenge"]

    async def _create_session(self, headers: dict[str, str] | None = None) -> str:
        """Create a new chat session, return its UUID. Auto-refreshes token on 401.

        Pass explicit `headers` (rotation mode) to use a specific pooled token — on
        401 this raises instead of triggering a browser refresh.
        """
        http = await self._get_http()
        explicit = headers is not None
        hdrs = headers if explicit else self._auth_headers()
        resp = await http.post(
            "/api/v0/chat_session/create",
            json={},
            headers=hdrs,
        )
        if resp.status_code == 401:
            if explicit:
                raise DeepSeekAPIError("401 creating session with rotation token")
            logger.warning("401 on session create — refreshing token...")
            await self._refresh_token()
            resp = await http.post(
                "/api/v0/chat_session/create",
                json={},
                headers=self._auth_headers(),
            )
            if resp.status_code == 401:
                raise DeepSeekAPIError("Token still invalid after refresh. Log in to chat.deepseek.com.")
        resp.raise_for_status()
        data = resp.json()
        biz = data["data"]["biz_data"]
        # Response shape: biz_data.chat_session.id (or legacy biz_data.id)
        if "chat_session" in biz:
            return biz["chat_session"]["id"]
        return biz["id"]

    # ------------------------------------------------------------------
    # Client-side session history (mirrors Gemini/Mistral pattern)
    # ------------------------------------------------------------------

    async def _maybe_summarize(
        self, chat_id: str, history: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Check thresholds and summarize if needed. Returns updated history."""
        prefix_len = 1 if history and history[0].get("role") == "system" else 0
        total_chars = sum(_msg_chars(m) for m in history[prefix_len:])
        if should_force_summarize(total_chars, self._max_session_chars):
            cut_idx = compute_force_cut_index(history, prefix_len)
            msgs_to_summarize = history[prefix_len:cut_idx]
            if len(msgs_to_summarize) >= 2:
                prompt = build_summary_prompt(msgs_to_summarize, _msg_chars)
                summary = await self._call_self_summarize(prompt)
                if summary:
                    logger.info("Force-summarized %d messages for chat %s", len(msgs_to_summarize), chat_id)
                    history = rewrite_history_with_summary(history, summary, cut_idx, prefix_len, fmt="openai")
                    self._sessions[chat_id] = history
        return history

    async def _call_self_summarize(self, prompt: str) -> str | None:
        """Call the same DeepSeek model to generate a summary. Non-streaming."""
        # Use a simple non-streaming call via the same API
        prepared = await self._prepare_request_with_rotation()
        if prepared is None:
            return None
        session_id, headers = prepared
        body = {
            "chat_session_id": session_id,
            "parent_message_id": None,
            "model_type": self._model_id,
            "prompt": prompt,
            "ref_file_ids": [],
            "thinking_enabled": False,
            "search_enabled": False,
            "action": None,
            "preempt": False,
        }
        _log_raw("USER_MESSAGE [summarize]", prompt)
        try:
            http = await self._get_http()
            full_answer = ""
            async with http.stream(
                "POST", "/api/v0/chat/completion", json=body, headers=headers,
            ) as resp:
                if resp.status_code != 200:
                    return None
                async for event in self._iter_completion_events(resp):
                    if event.get("type") == "answer":
                        full_answer += event.get("text", "")
            _log_raw("RESPONSE [summarize] (raw)", full_answer)
            result = strip_summarize_tag(full_answer).strip()
            return result if result else None
        except Exception as e:
            logger.warning("DeepSeek summarizer error: %s", e)
            return None

    def _get_or_create_session(
        self, chat_id: str | None, inject_instructions: bool,
        system_instruction: str | None = None,
        project_id: str | None = None,
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
        instructions = system_instruction if system_instruction else (_load_instructions(project_id=project_id) if inject_instructions else None)
        if instructions:
            history.append({"role": "system", "content": instructions})

        if chat_id:
            self._sessions[chat_id] = history
        return history

    # Warning injected before the final user message to prevent DeepSeek from
    # emitting legacy XML tool-call tags (<invoke>, <parameter>) instead of
    # the expected format.  Keep this as a class-level constant so it's easy
    # to tweak or disable.
    _DEEPSEEK_TAG_WARNING = (
        "[SYSTEM WARNING: Do NOT use <invoke>, <parameter>, <tool_calls>, "
        "or ANY XML/custom tags for tool calls. Output tool calls as a "
        "plain JSON array at the end of your message. Example: "
        '[{"name": "grep", "arguments": {"pattern": "foo"}}] — '
        "no tags, no wrappers, just clean JSON. "
        "Any response containing XML tags will be rejected.]"
    )

    @classmethod
    def _serialize_history(cls, history: list[dict[str, Any]], current_message: str) -> str:
        """Serialize client-side history + current message into a single prompt string.

        A tag-format warning is prepended to the *last* user message so that
        DeepSeek sees it immediately before generating its response, reducing
        the chance of it falling back to legacy <invoke>/<parameter> XML.
        """
        parts: list[str] = []
        for msg in history:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "system":
                parts.append(f"[System Instructions]\n{content}")
            elif role == "user":
                parts.append(f"User: {content}")
            elif role == "assistant":
                parts.append(f"Assistant: {content}")
        # Prepend warning to the current (last) user message
        warned_message = f"{cls._DEEPSEEK_TAG_WARNING}\n\n{current_message}"
        parts.append(f"User: {warned_message}")
        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # SSE response parsing
    # ------------------------------------------------------------------

    async def _iter_completion_events(
        self,
        resp: httpx.Response,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Parse an open /chat/completion SSE stream into Sable events."""
        current_frag_type: str = "RESPONSE"

        async for line in resp.aiter_lines():
            if not line.startswith("data: "):
                continue
            payload = line[6:]
            if payload == "[DONE]":
                break
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue

            v = obj.get("v")
            p = obj.get("p")
            o = obj.get("o")

            if v is None and p is None:
                continue
            if p == "response" and o == "BATCH":
                continue
            if p == "response/status" and o == "SET":
                if v == "FINISHED":
                    yield {"type": "done", "parent_id": None}
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
                fragments = v["response"].get("fragments", [])
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

        Mirrors Gemini/Mistral: client-side history, full-context prompt, automatic
        token rotation with round-robin + failover.
        """
        thinking_enabled = str(thinking_mode or "").lower() in ("thinking", "deepthink")
        model_type = model
        file_ids = [str(fid) for fid in (ref_file_ids or []) if str(fid).strip()]
        project_id = kwargs.pop("project_id", None)
        db_history = kwargs.pop("db_history", None)

        # Build client-side history (instructions as first entry, sliding window)
        history = self._get_or_create_session(chat_id, inject_instructions, system_instruction=system_instruction, project_id=project_id)
        # Seed from DB when session is fresh (cross-provider switch)
        if db_history and chat_id and len(history) <= 1:
            for _m in db_history:
                history.append({"role": _m["role"], "content": _m["content"]})

        # Context summarization: check thresholds before sending
        if chat_id:
            history = await self._maybe_summarize(chat_id, history)
            prefix_len = 1 if history and history[0].get("role") == "system" else 0
            total_chars = sum(_msg_chars(m) for m in history[prefix_len:])
            if should_inject_hint(total_chars, self._max_session_chars):
                hint = get_hint_text(total_chars, self._max_session_chars)
                message = message + hint

        # Serialize history + current message into prompt
        prompt = self._serialize_history(history, message)

        # Try each token with round-robin rotation + failover
        attempts = max(1, len(self._rotate_tokens) or 1)
        last_err = "unknown"

        for _attempt in range(attempts):
            prepared = await self._prepare_request_with_rotation()
            if prepared is None:
                yield {"type": "error", "message": f"All DeepSeek tokens failed ({last_err})."}
                return
            session_id, headers = prepared

            body = {
                "chat_session_id": session_id,
                "parent_message_id": None,  # Always stateless — context is in the prompt
                "model_type": model_type,
                "prompt": prompt,
                "ref_file_ids": file_ids,
                "thinking_enabled": thinking_enabled,
                "search_enabled": False,
                "action": None,
                "preempt": False,
            }

            _log_raw("USER_MESSAGE (prompt sent)", prompt)

            try:
                http = await self._get_http()
                full_answer = ""
                full_thinking = ""

                async with http.stream(
                    "POST", "/api/v0/chat/completion", json=body, headers=headers,
                ) as resp:
                    # Auth/rate-limit errors → rotate to next token
                    if resp.status_code in (401, 403, 429):
                        await resp.aread()
                        last_err = f"HTTP {resp.status_code}"
                        logger.warning("DeepSeek token failed (%s), rotating...", last_err)
                        self._advance_rotation()
                        continue

                    if resp.status_code != 200:
                        error_body = await resp.aread()
                        yield {"type": "error", "message": f"HTTP {resp.status_code}: {error_body.decode()[:500]}"}
                        return

                    async for event in self._iter_completion_events(resp):
                        etype = event.get("type")
                        if etype == "answer":
                            full_answer += event.get("text", "")
                        elif etype == "thinking":
                            full_thinking += event.get("text", "")
                        yield event

                _log_raw("RESPONSE (raw)", (full_thinking + "\n---\n" + full_answer) if full_thinking else full_answer)

                # Success — save user message + assistant response to history
                _summarize_idx = extract_summarize_tag(full_answer)
                clean_answer = strip_summarize_tag(full_answer)
                history.append({"role": "user", "content": message})
                response_content = full_thinking + clean_answer if full_thinking else clean_answer
                if response_content:
                    history.append({"role": "assistant", "content": response_content})

                # Handle model-triggered summarization
                if _summarize_idx is not None and chat_id:
                    prefix_len = 1 if history and history[0].get("role") == "system" else 0
                    actual_cut = max(prefix_len, min(_summarize_idx, len(history) - 1))
                    msgs_to_summarize = history[prefix_len:actual_cut]
                    if len(msgs_to_summarize) >= 2:
                        prompt = build_summary_prompt(msgs_to_summarize, _msg_chars)
                        summary = await self._call_self_summarize(prompt)
                        if summary:
                            logger.info("Model-triggered summarization at index %d for chat %s", _summarize_idx, chat_id)
                            history = rewrite_history_with_summary(history, summary, actual_cut, prefix_len, fmt="openai")
                            self._sessions[chat_id] = history

                if chat_id:
                    prefix_len = 1 if history and history[0].get("role") == "system" else 0
                    total_chars = sum(_msg_chars(m) for m in history[prefix_len:])
                    if total_chars > self._max_session_chars:
                        self._sessions[chat_id] = _trim_history(history, prefix_len, self._max_session_chars)

                # Advance rotation for round-robin on next call
                self._advance_rotation()
                return

            except httpx.ReadTimeout:
                last_err = "timeout (120s)"
                logger.warning("DeepSeek token timed out, rotating...")
                self._advance_rotation()
                continue
            except Exception as exc:
                yield {"type": "error", "message": f"Stream error: {type(exc).__name__}: {exc}"}
                return

        yield {"type": "error", "message": f"All DeepSeek tokens failed ({last_err})."}

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
