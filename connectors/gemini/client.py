
"""Google Gemini API connector for Sable.

Pure HTTP backend using API key authentication with multi-key rotation.
Streams SSE responses from streamGenerateContent endpoint.
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

from connectors.common.media import prepare_inline_file, to_gemini_inline

import httpx

logger = logging.getLogger("sable.gemini_api")

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
_SYSTEM_DIR = Path(__file__).resolve().parent.parent.parent / "system"
_KEYS_PATH = _SYSTEM_DIR / ".gemini_api_keys.json"
# Legacy single-key file (migrated on first load)
_LEGACY_KEY_PATH = _SYSTEM_DIR / ".gemini_api_key"



# Max messages to keep in session history (sliding window)
_MAX_SESSION_MESSAGES = 60

# Instruction files to prepend on first message
_INSTRUCTION_DIR = Path(__file__).resolve().parent.parent.parent / "instruction"
_INSTRUCTION_FILES = ["Maria.md", "personal.md", "output_format.md"]


def _load_keys() -> list[str]:
    """Load API keys from JSON file, migrating legacy single-key file if needed."""
    if _KEYS_PATH.exists():
        try:
            data = json.loads(_KEYS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [k for k in data if isinstance(k, str) and k.strip()]
        except (json.JSONDecodeError, OSError):
            pass
    # Migrate legacy single-key file
    if _LEGACY_KEY_PATH.exists():
        key = _LEGACY_KEY_PATH.read_text(encoding="utf-8").strip()
        if key:
            keys = [key]
            _save_keys(keys)
            _LEGACY_KEY_PATH.unlink(missing_ok=True)
            return keys
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
    return "\n\n".join(parts)



class GeminiClient:
    """Async Gemini API client with multi-key rotation and session history."""

    def __init__(self) -> None:
        self._keys: list[str] = _load_keys()
        self._key_index: int = 0
        # Session history: chat_id → list of {"role": ..., "parts": [...]}
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

    def _build_thinking_config(self, thinking_mode: str | None) -> dict[str, Any] | None:
        """Map Sable thinking mode to Gemini thinkingConfig.

        Gemini 3.x/2.5 models use thinking_level (minimal/low/medium/high).
        'fast'/'minimal' → omit config (model uses its default minimum).
        Explicit levels → send thinkingConfig with thinkingLevel.
        """
        if not thinking_mode:
            return None
        mode = thinking_mode.lower()
        if mode in ("thinking", "deepthink", "fast", "minimal", "auto"):
            return None  # Omit — model uses default dynamic thinking
        if mode in ("low", "medium", "high"):
            return {"thinkingConfig": {"thinkingLevel": mode}}
        return None

    def _get_or_create_session(
        self, chat_id: str | None, inject_instructions: bool,
        system_instruction: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get existing session history or create a new one (sliding window)."""
        if chat_id and chat_id in self._sessions:
            history = self._sessions[chat_id]
            if len(history) > _MAX_SESSION_MESSAGES:
                prefix_len = 2 if history and history[0].get("parts", [{}])[0].get("text", "").startswith("[System Instructions]") else 0
                keep = history[prefix_len:][_MAX_SESSION_MESSAGES - prefix_len:]
                self._sessions[chat_id] = history[:prefix_len] + keep
            return self._sessions[chat_id]

        history: list[dict[str, Any]] = []
        # Explicit system_instruction takes priority over default inject
        instructions = system_instruction if system_instruction else (_load_instructions() if inject_instructions else None)
        if instructions:
            history.append({
                    "role": "user",
                    "parts": [{"text": f"[System Instructions]\n{instructions}"}],
                })
            history.append({
                    "role": "model",
                    "parts": [{"text": "Understood. I'll follow these instructions."}],
                })

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
        ref_file_ids: list[str] | None = None,
        inject_instructions: bool = True,
        files: list[str] | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Stream a chat completion, yielding Sable-standard events."""
        if not self._keys:
            yield {"type": "error", "message": "No Gemini API keys configured. Add one in Settings → Providers."}
            return

        model_id = model or "gemini-2.5-flash"
        url = f"{BASE_URL}/models/{model_id}:streamGenerateContent"

        system_instruction = kwargs.pop("system_instruction", None)
        history = self._get_or_create_session(chat_id, inject_instructions, system_instruction=system_instruction)
        parts: list[dict[str, Any]] = [{"text": message}]
        if files:
            for fpath in files:
                pf = prepare_inline_file(fpath)
                if pf:
                    parts.append(to_gemini_inline(pf))
        history.append({"role": "user", "parts": parts})

        body: dict[str, Any] = {"contents": history}
        thinking_config = self._build_thinking_config(thinking_mode)
        if thinking_config:
            body["generationConfig"] = thinking_config

        # Try each key with rotation on failure
        attempts = len(self._keys)
        for attempt in range(attempts):
            key = self._current_key
            if not key:
                break

            try:
                http = await self._get_http()
                full_answer = ""
                full_thinking = ""
                got_response = False

                async with http.stream(
                    "POST",
                    url,
                    params={"alt": "sse", "key": key},
                    json=body,
                ) as response:
                    # Auth/rate-limit errors → rotate key
                    if response.status_code in (401, 403, 429):
                        await response.aread()
                        logger.warning("Gemini key %d failed (HTTP %d), rotating...", self._key_index, response.status_code)
                        self._rotate_key()
                        continue

                    if response.status_code != 200:
                        error_body = await response.aread()
                        try:
                            error_data = json.loads(error_body)
                            error_msg = error_data.get("error", {}).get("message", str(error_body))
                        except (json.JSONDecodeError, AttributeError):
                            error_msg = f"HTTP {response.status_code}: {error_body.decode()[:500]}"
                        yield {"type": "error", "message": error_msg}
                        history.pop()
                        return

                    got_response = True
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

                        candidates = chunk.get("candidates", [])
                        if not candidates:
                            prompt_feedback = chunk.get("promptFeedback", {})
                            if prompt_feedback.get("blockReason"):
                                yield {"type": "error", "message": f"Blocked: {prompt_feedback['blockReason']}"}
                                history.pop()
                                return
                            continue

                        candidate = candidates[0]
                        finish_reason = candidate.get("finishReason", "")
                        if finish_reason in ("SAFETY", "RECITATION", "PROHIBITED_CONTENT"):
                            yield {"type": "error", "message": f"Response blocked: {finish_reason}"}
                            history.pop()
                            return

                        content = candidate.get("content", {})
                        parts = content.get("parts", [])

                        for part in parts:
                            if part.get("thought"):
                                text = part.get("text", "")
                                if text:
                                    full_thinking += text
                                    yield {"type": "thinking", "text": text}
                            elif "text" in part:
                                text = part["text"]
                                if text:
                                    full_answer += text
                                    yield {"type": "answer", "text": text}

                # Success — save to history
                if got_response and (full_answer or full_thinking):
                    model_parts: list[dict[str, Any]] = []
                    if full_thinking:
                        model_parts.append({"text": full_thinking, "thought": True})
                    if full_answer:
                        model_parts.append({"text": full_answer})
                    history.append({"role": "model", "parts": model_parts})
                    if chat_id and len(history) > _MAX_SESSION_MESSAGES:
                        prefix_len = 2 if history and history[0].get("parts", [{}])[0].get("text", "").startswith("[System Instructions]") else 0
                        self._sessions[chat_id] = history[:prefix_len] + history[prefix_len:][-(_MAX_SESSION_MESSAGES - prefix_len):]

                yield {"type": "done", "parent_id": None}
                return  # Success — exit

            except httpx.TimeoutException:
                logger.warning("Gemini key %d timed out, rotating...", self._key_index)
                self._rotate_key()
                continue
            except httpx.HTTPError as e:
                logger.warning("Gemini HTTP error on key %d: %s, rotating...", self._key_index, e)
                self._rotate_key()
                continue
            except Exception as e:
                logger.exception("Gemini stream_chat unexpected error")
                yield {"type": "error", "message": f"Gemini error: {e}"}
                history.pop()
                return

        # All keys exhausted
        yield {"type": "error", "message": "All Gemini API keys failed (rate limited or invalid)."}
        history.pop()

    async def chat(
        self,
        message: str,
        *,
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

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._http and not self._http.is_closed:
            await self._http.aclose()


# Module-level singleton
_client: GeminiClient | None = None


def get_client() -> GeminiClient:
    """Get or create the singleton Gemini client."""
    global _client
    if _client is None:
        _client = GeminiClient()
    return _client
