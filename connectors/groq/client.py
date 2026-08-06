
"""Groq API connector for Sable.

OpenAI-compatible HTTP backend using Bearer token auth.
Streams SSE responses from /v1/chat/completions endpoint.
Maintains in-memory session history for multi-turn conversations.

Yields events matching Sable's stream interface:
  {"type": "answer", "text": "..."}
  {"type": "thinking", "text": "..."}
  {"type": "done", "parent_id": "..."}
  {"type": "error", "message": "..."}
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import httpx

from connectors.common.media import prepare_inline_file, to_openai_image

logger = logging.getLogger("sable.groq_api")

BASE_URL = "https://api.groq.com/openai/v1"
_SYSTEM_DIR = Path(__file__).resolve().parent.parent.parent / "system"
_KEYS_PATH = _SYSTEM_DIR / ".groq_api_keys.json"

# Max chars for session history (sliding window by character count)
_MAX_SESSION_CHARS = 100_000


def _msg_chars(msg: dict[str, Any]) -> int:
    """Estimate character count of an OpenAI-format message."""
    content = msg.get('content', '')
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(len(p.get('text', '')) for p in content if isinstance(p, dict))
    return 0


def _trim_history(history: list[dict[str, Any]], prefix_len: int) -> list[dict[str, Any]]:
    """Trim history to fit within _MAX_SESSION_CHARS, preserving prefix messages."""
    prefix = history[:prefix_len]
    msgs = history[prefix_len:]
    total = sum(_msg_chars(m) for m in msgs)
    while total > _MAX_SESSION_CHARS and len(msgs) > 1:
        total -= _msg_chars(msgs.pop(0))
    return prefix + msgs

# Instruction files to prepend on first message
_INSTRUCTION_DIR = Path(__file__).resolve().parent.parent.parent / "instruction"
_INSTRUCTION_FILES: list[str] = []  # Groq: no system prompt injection (8k TPM limit)


def _load_keys() -> list[str]:
    """Load API keys from JSON file."""
    if _KEYS_PATH.exists():
        try:
            data = json.loads(_KEYS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [k for k in data if isinstance(k, str) and k.strip()]
        except (json.JSONDecodeError, OSError):
            pass
    return []


def _save_keys(keys: list[str]) -> None:
    """Persist keys list to disk."""
    _KEYS_PATH.write_text(json.dumps(keys, indent=2), encoding="utf-8")


def _load_instructions() -> str:
    """Load and concatenate instruction files + dynamic skill registry for system prompt injection."""
    parts: list[str] = []
    for fname in _INSTRUCTION_FILES:
        fpath = _INSTRUCTION_DIR / fname
        if fpath.exists():
            parts.append(fpath.read_text(encoding="utf-8").strip())

    # Groq: zero system prompt bloat. 8k TPM can't handle it.
    return ""


class GroqClient:
    """Async Groq API client with multi-key rotation and session history."""

    def __init__(self) -> None:
        self._keys: list[str] = _load_keys()
        self._key_index: int = 0
        # Session history: chat_id → list of {"role": ..., "content": ...}
        self._sessions: dict[str, list[dict[str, Any]]] = {}
        self._http: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------
    # Key management
    # ------------------------------------------------------------------

    @property
    def is_available(self) -> bool:
        return len(self._keys) > 0

    @property
    def _current_key(self) -> str | None:
        if not self._keys:
            return None
        return self._keys[self._key_index % len(self._keys)]

    def add_key(self, key: str) -> None:
        """Add a new API key. Persists to disk."""
        key = key.strip()
        if key and key not in self._keys:
            self._keys.append(key)
            _save_keys(self._keys)

    def remove_key(self, index: int) -> bool:
        """Remove key by index. Returns True if removed."""
        if 0 <= index < len(self._keys):
            self._keys.pop(index)
            if self._key_index >= len(self._keys) and self._keys:
                self._key_index = 0
            _save_keys(self._keys)
            return True
        return False

    def list_keys(self) -> list[dict[str, Any]]:
        """Return masked key list for UI display."""
        result = []
        for i, key in enumerate(self._keys):
            masked = key[:8] + "..." + key[-4:] if len(key) > 12 else "***"
            result.append({
                "index": i,
                "masked": masked,
                "active": i == self._key_index % len(self._keys),
            })
        return result

    def _rotate_key(self) -> str | None:
        """Rotate to next key. Returns the new current key or None if exhausted."""
        if len(self._keys) <= 1:
            return self._current_key
        self._key_index = (self._key_index + 1) % len(self._keys)
        return self._keys[self._key_index]

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    async def _get_http(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(120.0, connect=15.0),
            )
        return self._http

    def _get_or_create_session(
        self, chat_id: str | None, inject_instructions: bool,
        system_instruction: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get existing session history or create a new one (sliding window)."""
        if chat_id and chat_id in self._sessions:
            history = self._sessions[chat_id]
            prefix_len = 1 if history and history[0].get("role") == "system" else 0
            total_chars = sum(_msg_chars(m) for m in history[prefix_len:])
            if total_chars > _MAX_SESSION_CHARS:
                self._sessions[chat_id] = _trim_history(history, prefix_len)
            return self._sessions[chat_id]

        history: list[dict[str, Any]] = []
        # Explicit system_instruction takes priority over default inject
        instructions = system_instruction if system_instruction else (_load_instructions() if inject_instructions else None)
        if instructions:
            history.append({"role": "system", "content": instructions})

        if chat_id:
            self._sessions[chat_id] = history
        return history

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    async def stream_chat(
        self,
        message: str,
        *,
        model: str | None = None,
        thinking_mode: str | None = None,
        chat_id: str | None = None,
        inject_instructions: bool = True,
        files: list[str] | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Stream a chat completion, yielding Sable-standard events."""
        if not self._keys:
            yield {"type": "error", "message": "No Groq API keys configured. Add one in Settings → Providers."}
            return

        model_id = model or "llama-3.3-70b-versatile"
        url = f"{BASE_URL}/chat/completions"

        system_instruction = kwargs.pop("system_instruction", None)
        history = self._get_or_create_session(chat_id, inject_instructions, system_instruction=system_instruction)

        # Build multimodal content when files are attached
        if files:
            content: list[dict[str, Any]] = [{"type": "text", "text": message}]
            for fpath in files:
                pf = prepare_inline_file(fpath)
                if pf and pf.category == "image":
                    content.append(to_openai_image(pf))
                elif pf:
                    # Non-image files: embed as text note
                    content[0]["text"] += f"\n\n[Attached file: {Path(fpath).name} ({pf.mime_type}, {pf.size_bytes} bytes)]"
            history.append({"role": "user", "content": content})
        else:
            history.append({"role": "user", "content": message})

        payload: dict[str, Any] = {
            "model": model_id,
            "messages": history,
            "stream": True,
        }

        # Try each key with rotation on failure
        attempts = len(self._keys)
        for attempt in range(attempts):
            key = self._current_key
            if not key:
                break

            try:
                http = await self._get_http()
                full_answer = ""

                async with http.stream(
                    "POST",
                    url,
                    headers={"Authorization": f"Bearer {key}"},
                    json=payload,
                ) as response:
                    if response.status_code in (401, 403, 429):
                        await response.aread()
                        self._rotate_key()
                        continue
                    if response.status_code != 200:
                        body = await response.aread()
                        yield {"type": "error", "message": f"Groq API error {response.status_code}: {body.decode()[:200]}"}
                        return

                    async for line in response.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        data_str = line[6:]
                        if data_str.strip() == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue
                        delta = (chunk.get("choices") or [{}])[0].get("delta", {})
                        content = delta.get("content") or ""
                        if content:
                            full_answer += content
                            yield {"type": "answer", "text": content}

                # Success — save to history and finish
                history.append({"role": "assistant", "content": full_answer})
                yield {"type": "done", "parent_id": chat_id or ""}
                return

            except httpx.TimeoutException:
                yield {"type": "error", "message": "Groq request timed out. Try again."}
                return
            except Exception as exc:
                logger.warning("Groq stream error (attempt %d): %s", attempt + 1, exc)
                self._rotate_key()
                continue

        yield {"type": "error", "message": "All Groq API keys exhausted or failed."}

    async def chat(
        self,
        message: str,
        *,
        model: str | None = None,
        thinking_mode: str | None = None,
        chat_id: str | None = None,
        ref_file_ids: list[str] | None = None,
        inject_instructions: bool = True,
        files: list[str] | None = None,
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
            inject_instructions=inject_instructions,
            files=files,
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


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------
_client: GroqClient | None = None


def get_client() -> GroqClient:
    """Return the global GroqClient singleton."""
    global _client
    if _client is None:
        _client = GroqClient()
    return _client
