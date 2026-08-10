
"""OpenAI API connector for Sable.

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
from connectors.common.context_summarizer import (
    should_inject_hint, should_force_summarize, get_hint_text,
    extract_summarize_tag, strip_summarize_tag, build_summary_prompt,
    rewrite_history_with_summary, compute_force_cut_index,
)

logger = logging.getLogger("sable.openai_api")

BASE_URL = "https://api.openai.com/v1"

_SYSTEM_DIR = Path(__file__).resolve().parent.parent.parent / "system"
_KEYS_PATH = _SYSTEM_DIR / ".openai_api_keys.json"

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


def _trim_history(history: list[dict[str, Any]], prefix_len: int, max_chars: int | None = None) -> list[dict[str, Any]]:
    """Trim history to fit within max_chars, preserving prefix messages."""
    limit = max_chars if max_chars is not None else _MAX_SESSION_CHARS
    prefix = history[:prefix_len]
    msgs = history[prefix_len:]
    total = sum(_msg_chars(m) for m in msgs)
    while total > limit and len(msgs) > 1:
        total -= _msg_chars(msgs.pop(0))
    return prefix + msgs

# Instruction files to prepend on first message
_INSTRUCTION_DIR = Path(__file__).resolve().parent.parent.parent / "instruction"
_INSTRUCTION_FILES: list[str] = []  # OpenAI: no system prompt injection (8k TPM limit)


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
    """Minimal agentic tag docs + distilled code_editor for OpenAI."""
    base = (
        "Every agentic tag must be wrapped in a single <action>...</action> block. "
        "The extractor only reads what is inside <action>; anything outside is prose.\n\n"
        "Tags: <get_file>/abs/path</get_file> \u00b7 <execute_command>cmd</execute_command>\n\n"
        "If you use <action>, the entire response is ONE short sentence + the block. "
        "<action> appears only in plain text, never inside a fenced code block."
    )
    editor = """# File I/O

    ## Read files
    <get_file>/abs/path</get_file> — read any file (text or binary)
    <view_file> path="/abs/path" </view_file> — read with line numbers, supports start/end range

    ## Write files  
    <edit_file> path="/abs/path">
    <<<<<< SEARCH
    exact old text from view_file
    =======
    new replacement text
    >>>>>>
    </edit_file> — replace text (must match exactly once)

    <create_file> path="/abs/path">
    file content here
    </create_file> — create new file (fails if exists)

    ## Rules
    - Always <view_file> before editing — never build old_str from memory
    - Wrap every tag in <action>...</action>
    - One short sentence + the <action> block, nothing else"""
    return base + "\n\n***\n\n" + editor


class OpenAIClient:
    """Async OpenAI API client with multi-key rotation and session history."""

    def __init__(self) -> None:
        self._keys: list[str] = _load_keys()
        self._key_index: int = 0
        # Session history: chat_id → list of {"role": ..., "content": ...}
        self._sessions: dict[str, list[dict[str, Any]]] = {}
        # Per-session max chars override (from model config)
        self._session_max_chars: dict[str, int] = {}
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

    def _get_max_chars(self, chat_id: str | None) -> int:
        """Get effective max chars for a session."""
        if chat_id and chat_id in self._session_max_chars:
            return self._session_max_chars[chat_id]
        return _MAX_SESSION_CHARS

    async def _maybe_summarize(
        self, chat_id: str, history: list[dict[str, Any]],
        max_chars: int, model_id: str,
    ) -> list[dict[str, Any]]:
        """Check thresholds and summarize if needed. Returns updated history."""
        prefix_len = 1 if history and history[0].get("role") == "system" else 0
        total_chars = sum(_msg_chars(m) for m in history[prefix_len:])
        if should_force_summarize(total_chars, max_chars):
            cut_idx = compute_force_cut_index(history, prefix_len)
            msgs_to_summarize = history[prefix_len:cut_idx]
            if len(msgs_to_summarize) >= 2:
                prompt = build_summary_prompt(msgs_to_summarize, _msg_chars)
                summary = await self._call_self_summarize(prompt, model_id)
                if summary:
                    logger.info("Force-summarized %d messages for chat %s", len(msgs_to_summarize), chat_id)
                    history = rewrite_history_with_summary(history, summary, cut_idx, prefix_len, fmt="openai")
                    self._sessions[chat_id] = history
        return history

    async def _call_self_summarize(self, prompt: str, model_id: str) -> str | None:
        """Call the same model to generate a summary. Non-streaming, single shot."""
        url = f"{BASE_URL}/chat/completions"
        body = {
            "model": model_id,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 4096,
            "stream": False,
        }
        attempts = len(self._keys)
        for _ in range(attempts):
            key = self._current_key
            if not key:
                break
            try:
                http = await self._get_http()
                resp = await http.post(url, headers={"Authorization": f"Bearer {key}"}, json=body)
                if resp.status_code in (401, 403, 429):
                    self._rotate_key()
                    continue
                if resp.status_code != 200:
                    logger.warning("OpenAI summarizer failed: HTTP %d", resp.status_code)
                    self._rotate_key()
                    continue
                data = resp.json()
                choices = data.get("choices", [])
                if choices:
                    result = choices[0].get("message", {}).get("content", "").strip()
                    if result:
                        return result
                return None
            except Exception as e:
                logger.warning("OpenAI summarizer error: %s", e)
                self._rotate_key()
                continue
        return None

    def _get_or_create_session(
        self, chat_id: str | None, inject_instructions: bool,
        system_instruction: str | None = None,
        max_session_chars: int | None = None,
    ) -> list[dict[str, Any]]:
        """Get existing session history or create a new one (sliding window)."""
        if chat_id and max_session_chars:
            self._session_max_chars[chat_id] = max_session_chars
        effective_max = self._get_max_chars(chat_id)
        if chat_id and chat_id in self._sessions:
            history = self._sessions[chat_id]
            prefix_len = 1 if history and history[0].get("role") == "system" else 0
            total_chars = sum(_msg_chars(m) for m in history[prefix_len:])
            if total_chars > effective_max:
                self._sessions[chat_id] = _trim_history(history, prefix_len, effective_max)
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
        max_session_chars: int | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Stream a chat completion, yielding Sable-standard events."""
        if not self._keys:
            yield {"type": "error", "message": "No OpenAI API keys configured. Add one in Settings → Providers."}
            return

        model_id = model or "gpt-4o-mini"
        url = f"{BASE_URL}/chat/completions"

        system_instruction = kwargs.pop("system_instruction", None)
        history = self._get_or_create_session(chat_id, inject_instructions, system_instruction=system_instruction, max_session_chars=max_session_chars)

        # Context summarization: check thresholds before sending
        effective_max = self._get_max_chars(chat_id)
        if chat_id and max_session_chars:
            prefix_len = 1 if history and history[0].get("role") == "system" else 0
            total_chars = sum(_msg_chars(m) for m in history[prefix_len:])
            history = await self._maybe_summarize(chat_id, history, effective_max, model_id)
            total_chars = sum(_msg_chars(m) for m in history[prefix_len:])
            if should_inject_hint(total_chars, effective_max):
                hint = get_hint_text(total_chars, effective_max)
                message = message + hint

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
                        yield {"type": "error", "message": f"OpenAI API error {response.status_code}: {body.decode()[:200]}"}
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
                            clean_text = strip_summarize_tag(content)
                            if clean_text:
                                yield {"type": "answer", "text": clean_text}

                # Success — save to history and finish
                _summarize_idx = extract_summarize_tag(full_answer)
                clean_answer = strip_summarize_tag(full_answer)
                history.append({"role": "assistant", "content": clean_answer})

                # Handle model-triggered summarization
                if _summarize_idx is not None and chat_id:
                    prefix_len = 1 if history and history[0].get("role") == "system" else 0
                    actual_cut = max(prefix_len, min(_summarize_idx, len(history) - 1))
                    msgs_to_summarize = history[prefix_len:actual_cut]
                    if len(msgs_to_summarize) >= 2:
                        prompt = build_summary_prompt(msgs_to_summarize, _msg_chars)
                        summary = await self._call_self_summarize(prompt, model_id)
                        if summary:
                            logger.info("Model-triggered summarization at index %d for chat %s", _summarize_idx, chat_id)
                            history = rewrite_history_with_summary(history, summary, actual_cut, prefix_len, fmt="openai")
                            self._sessions[chat_id] = history

                if chat_id:
                    prefix_len = 1 if history and history[0].get("role") == "system" else 0
                    total_chars = sum(_msg_chars(m) for m in history[prefix_len:])
                    eff_max = self._get_max_chars(chat_id)
                    if total_chars > eff_max:
                        self._sessions[chat_id] = _trim_history(history, prefix_len, eff_max)

                yield {"type": "done", "parent_id": chat_id or ""}
                return

            except httpx.TimeoutException:
                yield {"type": "error", "message": "OpenAI request timed out. Try again."}
                return
            except Exception as exc:
                logger.warning("OpenAI stream error (attempt %d): %s", attempt + 1, exc)
                self._rotate_key()
                continue

        yield {"type": "error", "message": "All OpenAI API keys exhausted or failed."}

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
_client: OpenAIClient | None = None


def get_client() -> OpenAIClient:
    """Return the global OpenAIClient singleton."""
    global _client
    if _client is None:
        _client = OpenAIClient()
    return _client
