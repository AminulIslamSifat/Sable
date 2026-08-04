
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

# Instruction files prepended to the first message of each session
_INSTRUCTION_DIR = Path(__file__).resolve().parent.parent.parent / "instruction"
_INSTRUCTION_FILES = ["Maria.md", "personal.md", "output_format.md"]
_instruction_cache: str | None = None


def _load_instructions() -> str:
    """Load and cache instruction context for first-message injection."""
    global _instruction_cache
    if _instruction_cache is not None:
        return _instruction_cache
    parts: list[str] = []
    for fname in _INSTRUCTION_FILES:
        fpath = _INSTRUCTION_DIR / fname
        if fpath.exists():
            parts.append(fpath.read_text(encoding="utf-8"))
    # Append auto-generated skill registry
    from engine.skills import SkillEngine
    from engine.skills.handlers import HANDLER_MAP
    _engine = SkillEngine(
        skills_dir=Path(__file__).resolve().parent.parent.parent / "skills",
        handlers=HANDLER_MAP,
        agent_id="maria",
    )
    parts.append(_engine.get_registry_prompt())

    PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
    OUTPUT_ROOT = PROJECT_ROOT / "output"
    ASSETS_DIR = OUTPUT_ROOT / "assets"
    parts.append(
        f"\n\n***\n\n# SYSTEM DIRECTORIES\n"
        f"PROJECT_ROOT={PROJECT_ROOT}\n"
        f"OUTPUT_ROOT={OUTPUT_ROOT}\n"
        f"ASSETS_DIR={ASSETS_DIR}\n"
        f"All <OUTPUT_ROOT> tags in your instructions should be replaced with {OUTPUT_ROOT}\n"
        f"All <PROJECT_ROOT> tags in your instructions should be replaced with {PROJECT_ROOT}\n"
    )

    _instruction_cache = "\n\n".join(parts)
    return _instruction_cache


class DeepSeekAPIError(Exception):
    """Raised on auth failure or unexpected API response."""


class DeepSeekClient:
    """Async DeepSeek chat client with PoW solving.

    Account-aware: resolves tokens from the per-account store.
    Falls back to any available account token if the primary has none.
    """

    def __init__(
        self,
        token: str | None = None,
        token_refresher: Callable[[], Awaitable[str]] | None = None,
        account: str | None = None,
    ) -> None:
        self._token = token
        self._token_refresher = token_refresher
        self._account = account  # e.g. "browser-data-acc15"; None = active
        self._http: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()
        # Session continuity: sable_chat_id → (deepseek_session_id, next_parent_id)
        self._sessions: dict[str, tuple[str, int | None]] = {}

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
        """Set and persist token under the given account. Clears stale sessions."""
        if token != self._token:
            self._sessions.clear()
        self._token = token
        save_token_for_account(token, account or self._account)

    def _auth_headers(self) -> dict[str, str]:
        tok = self.token
        if not tok:
            raise DeepSeekAPIError("No token available. Extract from browser first.")
        return {"Authorization": f"Bearer {tok}", **CLIENT_HEADERS}

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

    async def _get_challenge(self) -> dict[str, Any]:
        """Request a fresh PoW challenge. Auto-refreshes token from browser on 401."""
        http = await self._get_http()

        # No token at all — try browser refresh before anything else
        if not self.token:
            await self._refresh_token()

        resp = await http.post(
            "/api/v0/chat/create_pow_challenge",
            json={"target_path": "/api/v0/chat/completion"},
            headers=self._auth_headers(),
        )
        if resp.status_code == 401:
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

    async def _create_session(self) -> str:
        """Create a new chat session, return its UUID. Auto-refreshes token on 401."""
        http = await self._get_http()
        resp = await http.post(
            "/api/v0/chat_session/create",
            json={},
            headers=self._auth_headers(),
        )
        if resp.status_code == 401:
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

    async def _prepare_request(
        self, chat_id: str | None = None
    ) -> tuple[str, int | None, dict[str, str]]:
        """Solve PoW, reuse or create session. Returns (session_id, parent_message_id, headers)."""
        challenge = await self._get_challenge()

        # Solve in thread pool to avoid blocking event loop
        loop = asyncio.get_running_loop()
        nonce = await loop.run_in_executor(None, self._solve_pow, challenge)
        logger.debug("PoW solved: nonce=%d", nonce)

        pow_header = self._build_pow_header(challenge, nonce)

        # Reuse session for continuity, or create new
        parent_id: int | None = None
        if chat_id and chat_id in self._sessions:
            session_id, parent_id = self._sessions[chat_id]
        else:
            session_id = await self._create_session()
            parent_id = None

        headers = {
            **self._auth_headers(),
            "X-DS-PoW-Response": pow_header,
            "Content-Type": "application/json",
        }
        return session_id, parent_id, headers

    def _advance_session(self, chat_id: str | None, session_id: str, parent_id: int | None) -> None:
        """Track session state after a successful message. parent_id increments by 2 each turn."""
        if not chat_id:
            return
        next_parent = (parent_id or 0) + 2
        self._sessions[chat_id] = (session_id, next_parent)

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
        """Stream a chat completion. Yields Sable-compatible event dicts."""
        thinking_enabled = str(thinking_mode or "").lower() in ("thinking", "deepthink")
        model_type = model  # None is valid (Instant sends null)
        file_ids = [str(fid) for fid in (ref_file_ids or []) if str(fid).strip()]

        try:
            session_id, parent_id, headers = await self._prepare_request(chat_id=chat_id)
        except DeepSeekAPIError as exc:
            yield {"type": "error", "message": str(exc)}
            return
        except Exception as exc:
            yield {"type": "error", "message": f"PoW/session prep failed: {exc}"}
            return

        # First message in session → prepend instruction context
        prompt = message
        if parent_id is None:
            if system_instruction:
                prompt = f"[System Instructions]\n{system_instruction}\n\n{message}"
            elif inject_instructions:
                instructions = _load_instructions()
                if instructions:
                    prompt = f"{instructions}\n\n{message}"

        # Append compact reminders only for non-agent sessions (agents have their own format)
        if not system_instruction:
            _REMINDERS = (
                "\n\n[REMINDERS: Do NOT break character. Follow skills strictly, "
                "step by step. Never alter tag format. Keep responses concise. "
                "No generic/AI-speak — stay in Maria persona.]"
            )
            prompt += _REMINDERS

        body = {
            "chat_session_id": session_id,
            "parent_message_id": parent_id,
            "model_type": model_type,
            "prompt": prompt,
            "ref_file_ids": file_ids,
            "thinking_enabled": thinking_enabled,
            "search_enabled": False,
            "action": None,
            "preempt": False,
        }

        http = await self._get_http()
        try:
            async with http.stream(
                "POST",
                "/api/v0/chat/completion",
                json=body,
                headers=headers,
            ) as resp:
                if resp.status_code == 401:
                    yield {"type": "error", "message": "Token expired mid-stream (401)."}
                    return
                if resp.status_code != 200:
                    yield {"type": "error", "message": f"HTTP {resp.status_code}"}
                    return

                # Track current fragment type to route appends correctly.
                # DeepSeek SSE: first fragment is THINK, then a new RESPONSE
                # fragment is appended. Shorthand {"v":"..."} appends go to
                # whichever fragment is currently active.
                current_frag_type: str = "RESPONSE"  # THINK | RESPONSE

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

                    # Batch status updates (FINISHED, token usage)
                    if p == "response" and o == "BATCH":
                        continue

                    # Status SET → FINISHED
                    if p == "response/status" and o == "SET":
                        if v == "FINISHED":
                            self._advance_session(chat_id, session_id, parent_id)
                            yield {"type": "done", "parent_id": session_id}
                        continue

                    # elapsed_secs on fragment — ignore
                    if p and "elapsed_secs" in p:
                        continue

                    # New fragment appended (thinking→response transition)
                    if p == "response/fragments" and o == "APPEND":
                        if isinstance(v, list) and v:
                            new_type = v[0].get("type", "RESPONSE")
                            current_frag_type = new_type
                            content = v[0].get("content", "")
                            if content:
                                etype = "thinking" if new_type == "THINK" else "answer"
                                yield {"type": etype, "text": content}
                        continue

                    # Explicit content append with path
                    if p == "response/fragments/-1/content" and o == "APPEND":
                        text = v if isinstance(v, str) else ""
                        if text:
                            etype = "thinking" if current_frag_type == "THINK" else "answer"
                            yield {"type": etype, "text": text}
                        continue

                    # Full response object (first event — sets initial fragment)
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

                    # Shorthand string append {"v": " text"} — no path
                    if isinstance(v, str) and p is None:
                        etype = "thinking" if current_frag_type == "THINK" else "answer"
                        yield {"type": etype, "text": v}
                        continue

        except httpx.ReadTimeout:
            yield {"type": "error", "message": "Stream timed out (120s)"}
        except Exception as exc:
            yield {"type": "error", "message": f"Stream error: {type(exc).__name__}: {exc}"}

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
