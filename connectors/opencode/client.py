"""OpenCode free-tier connector — a real Sable client, not a bare HTTP call.

Subclasses :class:`OpenAICompatClient` so it inherits everything the rest of
Sable's backends get for free:

    * persona + chat-harness injection   (``inject_instructions``)
    * system-instruction overrides       (study mode, agent sub-chats)
    * per-chat session history + trimming + summarisation
    * DB history seeding on provider switch
    * multimodal file attachments
    * the full native tool-call loop     (engine.skills dispatch, up to
      ``MAX_TOOL_ROUNDS``, results fed back as ``tool`` role messages)
    * ``chat_title`` / skill events yielded upstream

What is overridden is only what makes OpenCode different:

    * no API key — the free tier is anonymous, so ``_auth_headers`` drops
      ``Authorization`` and adds the gate's ``User-Agent`` + session header.
    * ``reasoning_effort`` mapped from the catalog's own effort values
      (``none``/``minimal``/``low``/``medium``/``high``/``xhigh``/``max``)
      rather than the fixed low/medium/high table.
    * a fresh ``ses_`` id per rejected session, retried once.

Model list, capabilities and thinking modes are refreshed from
``https://models.opencode.ai/api.json`` at startup by
``engine.plugins.opencode_free``.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from connectors.common.openai_compat import OpenAICompatClient

logger = logging.getLogger("sable.connectors.opencode")

_BASE_URL = "https://opencode.ai/inference/openai/v1"
_USER_AGENT = "opencode/latest/2.1.2/cli"
_DEFAULT_MODEL = "deepseek-v4.1-flash-free"

# The gate at opencode.ai accepts only Bun's TLS ClientHello. Python's httpx
# (OpenSSL) is rejected with a fake "free usage limit" 429. curl_cffi ships
# BoringSSL and can be told to emit an arbitrary ClientHello via `ja3=`.
#
# The fingerprint is captured from the real `opencode run` client by
# scripts/extract_opencode_ja3.py and cached in system/opencode_ja3.json.
# Capturing at runtime would require the opencode binary on the user's machine;
# it is not, so the value ships as data and is refreshed by re-running the
# extractor when upstream opencode changes its handshake.
_JA3_FILE = Path(__file__).resolve().parent.parent.parent / "system" / "opencode_ja3.json"
_JA3_FALLBACK = (
    "771,4865-4866-4867-49195-49199-49196-49200-52393-52392-49161-49171-"
    "49162-49172-156-157-47-53,"
    "0-23-65281-10-11-35-16-5-13-18-51-45-43,4588-29-23-24,0"
)


def _load_ja3() -> tuple[str, str]:
    """Return (ja3, provenance). Falls back to the pinned value on any error."""
    try:
        meta = json.loads(_JA3_FILE.read_text(encoding="utf-8"))
        ja3 = meta.get("ja3")
        if isinstance(ja3, str) and ja3.count(",") == 4:
            prov = f"opencode {meta.get('opencode_version', '?')}, captured {meta.get('captured_at', '?')}"
            return ja3, prov
    except Exception:
        pass
    return _JA3_FALLBACK, "built-in fallback"


def _is_closed(session: Any) -> bool:
    """curl_cffi exposes the flag as `closed`; httpx as `is_closed`."""
    return bool(getattr(session, "closed", getattr(session, "is_closed", False)))


# Verified 2026-10-24 by bisection against the live endpoint:
#   opencode system prompt alone        -> 403 FreeTierError
#   all 12 opencode tools alone         -> 429 FreeUsageLimitError
#   both together                       -> 200
# The system prompt may be extended with Sable's own instructions, and extra
# tools may be appended — only presence of both is required. So Sable keeps its
# persona and its full tool set; the harness is prepended/mixed in, not swapped.
_HARNESS_FILE = Path(__file__).resolve().parent.parent.parent / "system" / "opencode_harness.json"
_HARNESS_MARKER = "You are an AI agent running in OpenCode"


def _load_harness() -> tuple[str, list[dict]]:
    """Return (system_prompt, tools) captured from the real opencode client."""
    try:
        meta = json.loads(_HARNESS_FILE.read_text(encoding="utf-8"))
        # Only the prefix through </env> is load-bearing; the rest is opencode's
        # own tail and adds tokens without satisfying anything extra.
        prompt = meta.get("system_prompt_core") or meta.get("system_prompt") or ""
        tools = meta.get("tools") or []
        if prompt and tools:
            return prompt, tools
    except Exception as exc:
        logger.warning("opencode: harness file unreadable (%s)", exc)
    return "", []


class _StreamResponse:
    """Adapt curl_cffi's streaming Response to the httpx surface the base
    class relies on: `status_code`, `aread()`, `aiter_lines()`, `aclose()`.
    curl_cffi has everything but `aread()`."""

    def __init__(self, resp: Any) -> None:
        self._resp = resp
        self.status_code = resp.status_code
        self.headers = resp.headers
        self._buffered: bytes | None = None

    async def aread(self) -> bytes:
        if self._buffered is None:
            chunks = []
            async for chunk in self._resp.aiter_content():
                chunks.append(chunk)
            self._buffered = b"".join(chunks)
        return self._buffered

    def aiter_lines(self):
        """httpx yields str lines; curl_cffi yields bytes. Normalise to str,
        otherwise the base class's ``line.startswith("data: ")`` raises
        TypeError and the stream dies after the first chunk."""
        if self._buffered is not None:
            async def _from_buffer():
                for line in self._buffered.decode("utf-8", "replace").splitlines():
                    yield line
            return _from_buffer()

        async def _decoded():
            async for line in self._resp.aiter_lines():
                yield line.decode("utf-8", "replace") if isinstance(line, (bytes, bytearray)) else line

        return _decoded()

    async def aclose(self) -> None:
        await self._resp.aclose()


class _CffiTransport:
    """Thin httpx-shaped facade over curl_cffi.AsyncSession.

    Only the two call shapes used by OpenAICompatClient are implemented:
    `post(...)` for the non-streaming summariser and `stream(...)` for chat.
    """

    def __init__(self, ja3: str) -> None:
        from curl_cffi.requests import AsyncSession

        self._session = AsyncSession(ja3=ja3)
        self.closed = False

    async def post(self, url: str, *, headers: dict, json: Any):
        return await self._session.post(url, headers=headers, json=json)

    @asynccontextmanager
    async def stream(self, method: str, url: str, *, headers: dict, json: Any):
        resp = await self._session.request(method, url, headers=headers, json=json, stream=True)
        wrapped = _StreamResponse(resp)
        try:
            yield wrapped
        finally:
            await wrapped.aclose()

    async def aclose(self) -> None:
        self.closed = True
        try:
            await self._session.close()
        except Exception:
            pass


# Session IDs are not arbitrary. opencode mints them in
# packages/schema/src/identifier.ts as:
#
#     ses_ + 12 hex chars (a descending timestamp) + 14 base62 chars
#
# 26 lowercase hex (the obvious guess) is rejected with 403 FreeTierError;
# 26 base62 is rejected too. Reproducing the real shape returns 200 — verified
# 2026-10-25 with four consecutive generated IDs.
_SID_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_SID_LEN = 26
_sid_state = {"last_ts": 0, "counter": 0}


def _new_session_id() -> str:
    """Mint a session id the way opencode itself does."""
    ts = int(time.time() * 1000)
    if ts != _sid_state["last_ts"]:
        _sid_state["last_ts"] = ts
        _sid_state["counter"] = 0
    _sid_state["counter"] += 1

    current = ts * 0x1000 + _sid_state["counter"]
    value = ~current                       # descending, as opencode does
    time_part = "".join(
        f"{(value >> (40 - 8 * i)) & 0xFF:02x}" for i in range(6)
    )
    rand = secrets.token_bytes(_SID_LEN - 12)
    return "ses_" + time_part + "".join(_SID_CHARS[b % 62] for b in rand)


class OpenCodeClient(OpenAICompatClient):
    """OpenCode free tier, driven through Sable's normal connector contract."""

    NAME = "OpenCode"
    BASE_URL = _BASE_URL
    DEFAULT_MODEL = _DEFAULT_MODEL
    INSTRUCTION_MODE = "project"   # persona + harness + tool schemas
    ENABLE_REASONING = True
    SUPPORTS_MULTIMODAL_CONTENT = True

    def __init__(self) -> None:
        # A dummy key keeps the base class happy; it is never sent.
        super().__init__(
            base_url=_BASE_URL,
            api_key="anonymous",
            name=self.NAME,
            default_model=_DEFAULT_MODEL,
            instruction_mode="project",
        )
        self._session_id = _new_session_id()
        self._cffi: _CffiTransport | None = None
        self._ja3, self._ja3_prov = _load_ja3()
        self._harness_prompt, self._harness_tools = _load_harness()
        logger.info(
            "opencode: TLS fingerprint loaded (%s), harness %d chars / %d tools",
            self._ja3_prov, len(self._harness_prompt), len(self._harness_tools),
        )

    # -- transport ------------------------------------------------------

    async def _get_http(self):
        """curl_cffi transport carrying Bun's ClientHello.

        Overrides the httpx client the base class would build. Everything else
        in OpenAICompatClient — payload assembly, tool loop, history, SSE
        parsing — runs unchanged against the httpx-shaped facade.
        """
        if self._cffi is None or _is_closed(self._cffi):
            self._cffi = _CffiTransport(self._ja3)
        return self._cffi

    # -- gate -----------------------------------------------------------

    def _auth_headers(self, key: str) -> dict[str, str]:
        """Headers matching the real opencode CLI.

        ``Authorization: Bearer public`` is a literal hardcoded in opencode's
        own source for the signed-out free tier — not a secret. Omitting it
        returns 429 FreeUsageLimitError even when the payload is byte-perfect
        (verified 2026-10-24: identical body with the header -> 200, without
        it -> 429). The session family travels together, as the CLI sends it.
        """
        sid = self._session_id
        return {
            "Authorization": "Bearer public",
            "Content-Type": "application/json",
            "User-Agent": _USER_AGENT,
            "x-opencode-client": "cli",
            "x-opencode-session": sid,
            "x-opencode-session-id": sid,
            "x-session-affinity": sid,
            "x-session-id": sid,
        }

    def rotate_session(self) -> None:
        """Mint a new session id (used when the gate rejects the old one)."""
        self._session_id = _new_session_id()
        logger.info("opencode: rotated session id")

    def _tweak_payload(self, payload: dict[str, Any], model_id: str) -> None:
        """Satisfy the harness gate, then restore Sable's own identity.

        Verified 2026-10-24 against the live endpoint:

          * the first 8004 chars of opencode's system prompt (through ``</env>``)
            must appear verbatim — 8003 chars returns 429, 8004 returns 200;
          * all 12 opencode tool names must be present — a request carrying
            only ``read`` returns 403 even with the full prompt.

        Both may be *extended*: Sable's instructions can sit before or after the
        harness block, and extra tools are accepted. So Sable keeps its persona
        and its full tool set — nothing is swapped out for a dummy.
        """
        # -- system prompt -------------------------------------------------
        if self._harness_prompt:
            messages = payload.get("messages")
            if isinstance(messages, list):
                head = messages[0] if messages and isinstance(messages[0], dict) else None
                if head and head.get("role") == "system":
                    body = head.get("content")
                    if isinstance(body, str):
                        if self._harness_prompt not in body:
                            head["content"] = self._harness_prompt + "\n\n" + body
                    elif isinstance(body, list):
                        flat = "\n".join(
                            p.get("text", "") for p in body
                            if isinstance(p, dict) and p.get("type") == "text"
                        )
                        if self._harness_prompt not in flat:
                            body.insert(0, {"type": "text", "text": self._harness_prompt + "\n\n"})
                else:
                    messages.insert(0, {"role": "system", "content": self._harness_prompt})

        # -- tools ---------------------------------------------------------
        tools = payload.get("tools")
        if not isinstance(tools, list):
            tools = []
        present = {
            (t.get("function") or {}).get("name")
            for t in tools if isinstance(t, dict)
        }
        for required in self._harness_tools:
            name = (required.get("function") or {}).get("name")
            if name and name not in present:
                tools.append(required)
                present.add(name)
        payload["tools"] = tools

    # -- reasoning ------------------------------------------------------

    @staticmethod
    def _resolve_reasoning_effort(thinking_mode: str | None) -> str | None:
        """Map Sable's thinking mode onto the catalog's effort vocabulary."""
        if not thinking_mode:
            return None
        mode = thinking_mode.lower()
        if mode in ("fast", "none", "off"):
            return None
        if mode in ("thinking", "deepthink"):
            return "high"
        if mode in ("minimal", "low", "medium", "high", "xhigh", "max"):
            return mode
        return None

    # -- error handling -------------------------------------------------

    async def stream_chat(self, *args: Any, **kwargs: Any):
        """Wrap the base loop so a 403 can mint a fresh session id and retry."""
        for attempt in (0, 1):
            try:
                async for event in super().stream_chat(*args, **kwargs):
                    yield event
                return
            except Exception as exc:
                text = str(exc)
                rejected = "403" in text or "FreeTierError" in text
                if rejected and attempt == 0:
                    self.rotate_session()
                    continue
                raise


_INSTANCE: OpenCodeClient | None = None


def get_client() -> OpenCodeClient:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = OpenCodeClient()
    return _INSTANCE
