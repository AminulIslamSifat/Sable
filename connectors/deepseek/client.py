
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
import subprocess
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("sable.deepseek_api")

BASE_URL = "https://chat.deepseek.com"
SOLVER_PATH = Path(__file__).resolve().parent / "pow_solver" / "pow_solver"
TOKEN_CACHE = Path(__file__).resolve().parent / ".token_cache.json"

CLIENT_HEADERS = {
    "x-client-version": "2.3.0",
    "x-client-platform": "web",
    "x-client-bundle-id": "com.deepseek.chat",
    "x-client-locale": "en_US",
}

# Instruction files prepended to the first message of each session
_INSTRUCTION_DIR = Path(__file__).resolve().parent.parent.parent / "instruction"
_INSTRUCTION_FILES = ["Maria.md", "output_format.md", "skills.md"]
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
    _instruction_cache = "\n\n".join(parts)
    return _instruction_cache


class DeepSeekAPIError(Exception):
    """Raised on auth failure or unexpected API response."""


class DeepSeekClient:
    """Async DeepSeek chat client with PoW solving."""

    def __init__(self, token: str | None = None) -> None:
        self._token = token
        self._http: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()
        # Session continuity: sable_chat_id → (deepseek_session_id, next_parent_id)
        self._sessions: dict[str, tuple[str, int | None]] = {}

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    @property
    def token(self) -> str | None:
        if self._token:
            return self._token
        # Try cache
        if TOKEN_CACHE.exists():
            try:
                data = json.loads(TOKEN_CACHE.read_text(encoding="utf-8"))
                self._token = data.get("token")
            except Exception:
                pass
        return self._token

    def set_token(self, token: str) -> None:
        """Set and persist token."""
        self._token = token
        try:
            TOKEN_CACHE.write_text(
                json.dumps({"token": token}), encoding="utf-8"
            )
        except Exception as exc:
            logger.warning("Could not cache token: %s", exc)

    def _auth_headers(self) -> dict[str, str]:
        tok = self.token
        if not tok:
            raise DeepSeekAPIError("No token available. Extract from browser first.")
        return {"Authorization": f"Bearer {tok}", **CLIENT_HEADERS}

    # ------------------------------------------------------------------
    # Token auto-refresh via persistent browser profile
    # ------------------------------------------------------------------

    _BROWSER_PROFILE = Path.home() / ".local/share/ghostchat/chrome-data"

    async def _refresh_token_from_browser(self) -> str:
        """Open persistent browser profile, read DeepSeek token from localStorage."""
        from playwright.async_api import async_playwright

        logger.info("Refreshing DeepSeek token from browser profile...")
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

        # localStorage stores JSON: {"value":"<jwt>","__version":"0"}
        try:
            parsed = json.loads(token)
            token = parsed.get("value", token)
        except (json.JSONDecodeError, AttributeError):
            token = token.strip('"')

        self.set_token(token)
        logger.info("Token refreshed successfully from browser.")
        return token

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
            await self._refresh_token_from_browser()

        resp = await http.post(
            "/api/v0/chat/create_pow_challenge",
            json={"target_path": "/api/v0/chat/completion"},
            headers=self._auth_headers(),
        )
        if resp.status_code == 401:
            # Token expired — auto-refresh from persistent browser profile
            logger.warning("401 on challenge — refreshing token from browser...")
            await self._refresh_token_from_browser()
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
        """Create a new chat session, return its UUID."""
        http = await self._get_http()
        resp = await http.post(
            "/api/v0/chat_session/create",
            json={},
            headers=self._auth_headers(),
        )
        if resp.status_code == 401:
            raise DeepSeekAPIError("Token expired (401). Re-extract from browser.")
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
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Stream a chat completion. Yields Sable-compatible event dicts."""
        thinking_enabled = str(thinking_mode or "").lower() in ("thinking", "deepthink")
        model_type = model  # None is valid (Instant sends null)

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
            instructions = _load_instructions()
            if instructions:
                prompt = f"{instructions}\n\n{message}"

        body = {
            "chat_session_id": session_id,
            "parent_message_id": parent_id,
            "model_type": model_type,
            "prompt": prompt,
            "ref_file_ids": [],
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
    ) -> dict[str, Any]:
        """Non-streaming chat. Returns {answer, thinking, parent_id, error}."""
        answer_parts: list[str] = []
        thinking_parts: list[str] = []
        parent_id: str | None = None
        error: str | None = None

        async for event in self.stream_chat(
            message, model=model, thinking_mode=thinking_mode,
            chat_id=chat_id,
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


# Module-level singleton
_client: DeepSeekClient | None = None


def get_client() -> DeepSeekClient:
    """Get or create the module-level DeepSeek client singleton."""
    global _client
    if _client is None:
        _client = DeepSeekClient()
    return _client
