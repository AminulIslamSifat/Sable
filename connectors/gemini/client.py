
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
from connectors.common.native_tools import (
    openai_to_gemini_tools,
    parse_gemini_function_call,
    format_gemini_tool_result,
    native_call_to_tag_event,
)
from connectors.common.context_summarizer import (
    should_inject_hint, should_force_summarize, get_hint_text,
    extract_summarize_tag, strip_summarize_tag, build_summary_prompt,
    rewrite_history_with_summary, compute_force_cut_index,
)

import httpx

logger = logging.getLogger("sable.gemini_api")

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
_SYSTEM_DIR = Path(__file__).resolve().parent.parent.parent / "system"
_KEYS_PATH = _SYSTEM_DIR / ".gemini_api_keys.json"
# Legacy single-key file (migrated on first load)
_LEGACY_KEY_PATH = _SYSTEM_DIR / ".gemini_api_key"



# Max chars for session history (sliding window by character count)
_MAX_SESSION_CHARS = 100_000


def _msg_chars(msg: dict[str, Any]) -> int:
    """Estimate character count of a Gemini-format message."""
    total = 0
    for part in msg.get("parts", []):
        text = part.get("text", "")
        if text:
            total += len(text)
    return total


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


_instruction_cache: str | None = None
_cached_project_id: str | None = "__none__"


def _load_instructions(project_id: str | None = None) -> str:
    """Load instruction context. Project-aware via shared builder."""
    global _instruction_cache, _cached_project_id
    if project_id != _cached_project_id:
        _instruction_cache = None
        _cached_project_id = project_id
    if _instruction_cache is not None:
        return _instruction_cache
    from connectors.common.instruction_builder import build_instructions
    _instruction_cache = build_instructions(project_id=project_id)
    return _instruction_cache



class GeminiClient:
    """Async Gemini API client with multi-key rotation and session history."""

    def __init__(self) -> None:
        self._keys: list[str] = _load_keys()
        self._key_index: int = 0
        # Session history: chat_id → list of {"role": ..., "parts": [...]}
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

    def _get_max_chars(self, chat_id: str | None) -> int:
        """Get effective max chars for a session."""
        if chat_id and chat_id in self._session_max_chars:
            return self._session_max_chars[chat_id]
        return _MAX_SESSION_CHARS

    async def _maybe_summarize(
        self, chat_id: str, history: list[dict[str, Any]],
        max_chars: int, model_id: str, thinking_mode: str | None,
    ) -> list[dict[str, Any]]:
        """Check thresholds and summarize if needed. Returns updated history."""
        prefix_len = 2 if history and history[0].get("parts", [{}])[0].get("text", "").startswith("[System Instructions]") else 0
        total_chars = sum(_msg_chars(m) for m in history[prefix_len:])

        if should_force_summarize(total_chars, max_chars):
            cut_idx = compute_force_cut_index(history, prefix_len)
            msgs_to_summarize = history[prefix_len:cut_idx]
            if len(msgs_to_summarize) >= 2:
                prompt = build_summary_prompt(msgs_to_summarize, _msg_chars)
                summary = await self._call_self_summarize(prompt, model_id, thinking_mode)
                if summary:
                    logger.info("Force-summarized %d messages for chat %s", len(msgs_to_summarize), chat_id)
                    history = rewrite_history_with_summary(history, summary, cut_idx, prefix_len, fmt="gemini")
                    self._sessions[chat_id] = history
        return history

    async def _call_self_summarize(self, prompt: str, model_id: str, thinking_mode: str | None) -> str | None:
        """Call the same model to generate a summary. Non-streaming, single shot."""
        url = f"{BASE_URL}/models/{model_id}:generateContent"
        body: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": 4096},
            "safetySettings": [
                {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
            ],
        }
        attempts = len(self._keys)
        for _ in range(attempts):
            key = self._current_key
            if not key:
                break
            try:
                http = await self._get_http()
                resp = await http.post(url, params={"key": key}, json=body)
                if resp.status_code in (401, 403, 429):
                    self._rotate_key()
                    continue
                if resp.status_code != 200:
                    logger.warning("Summarizer call failed: HTTP %d", resp.status_code)
                    self._rotate_key()
                    continue
                data = resp.json()
                candidates = data.get("candidates", [])
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    text_parts = [p.get("text", "") for p in parts if not p.get("thought")]
                    result = "".join(text_parts).strip()
                    if result:
                        return result
                return None
            except Exception as e:
                logger.warning("Summarizer call error: %s", e)
                self._rotate_key()
                continue
        return None

    def _get_or_create_session(
        self, chat_id: str | None, inject_instructions: bool,
        system_instruction: str | None = None,
        max_session_chars: int | None = None,
        project_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get existing session history or create a new one (sliding window)."""
        # Store per-session max chars if provided
        if chat_id and max_session_chars:
            self._session_max_chars[chat_id] = max_session_chars
        effective_max = self._get_max_chars(chat_id)
        if chat_id and chat_id in self._sessions:
            history = self._sessions[chat_id]
            prefix_len = 2 if history and history[0].get("parts", [{}])[0].get("text", "").startswith("[System Instructions]") else 0
            total_chars = sum(_msg_chars(m) for m in history[prefix_len:])
            if total_chars > effective_max:
                self._sessions[chat_id] = _trim_history(history, prefix_len, effective_max)
            return self._sessions[chat_id]

        history: list[dict[str, Any]] = []
        # Explicit system_instruction takes priority over default inject
        instructions = system_instruction if system_instruction else (_load_instructions(project_id=project_id) if inject_instructions else None)
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
        max_session_chars: int | None = None,
        tools: list[dict] | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Stream a chat completion, yielding Sable-standard events."""
        if not self._keys:
            yield {"type": "error", "message": "No Gemini API keys configured. Add one in Settings → Providers."}
            return

        model_id = model or "gemini-2.5-flash"
        url = f"{BASE_URL}/models/{model_id}:streamGenerateContent"

        system_instruction = kwargs.pop("system_instruction", None)
        project_id = kwargs.pop("project_id", None)
        db_history = kwargs.pop("db_history", None)
        history = self._get_or_create_session(chat_id, inject_instructions, system_instruction=system_instruction, max_session_chars=max_session_chars, project_id=project_id)
        # Seed from DB when session is fresh (cross-provider switch)
        if db_history and chat_id and len(history) <= 2:
            for _m in db_history:
                _role = "model" if _m["role"] == "assistant" else "user"
                history.append({"role": _role, "parts": [{"text": _m["content"]}]})

        # Context summarization: check thresholds before sending
        effective_max = self._get_max_chars(chat_id)
        if chat_id and max_session_chars:
            prefix_len = 2 if history and history[0].get("parts", [{}])[0].get("text", "").startswith("[System Instructions]") else 0
            total_chars = sum(_msg_chars(m) for m in history[prefix_len:])
            # Force summarize at 90%
            history = await self._maybe_summarize(chat_id, history, effective_max, model_id, thinking_mode)
            # Inject hint at 75% (after potential force-summarize re-check)
            total_chars = sum(_msg_chars(m) for m in history[prefix_len:])
            if should_inject_hint(total_chars, effective_max):
                hint = get_hint_text(total_chars, effective_max)
                message = message + hint
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

        # Disable all adjustable safety filters (BLOCK_NONE)
        body["safetySettings"] = [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
        ]

        # Native tool calling: convert and attach tool schemas
        if tools:
            gemini_tools = openai_to_gemini_tools(tools)
            if gemini_tools:
                body["tools"] = gemini_tools
                body["toolConfig"] = {"functionCallingConfig": {"mode": "AUTO"}}

        # Tool execution loop: re-enter on function calls
        _max_tool_rounds = 20
        _tool_round = 0

        while _tool_round < _max_tool_rounds:
            _tool_round += 1

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
                all_fc_parts: list[dict] = []  # Accumulated function call parts

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

                        # Collect all parts for this chunk
                        chunk_fc_parts: list[dict] = []
                        for part in parts:
                            if part.get("thought"):
                                text = part.get("text", "")
                                if text:
                                    full_thinking += text
                                    yield {"type": "thinking", "text": text}
                            elif "functionCall" in part:
                                chunk_fc_parts.append(part)
                            elif "text" in part:
                                text = part["text"]
                                if text:
                                    full_answer += text
                                    clean_text = strip_summarize_tag(text)
                                    if clean_text:
                                        yield {"type": "answer", "text": clean_text}

                        # Accumulate function call parts
                        if chunk_fc_parts:
                            all_fc_parts.extend(chunk_fc_parts)

                # --- Post-stream: log diagnostics ---
                logger.info(
                    "Gemini stream ended: model=%s finish_reason=%s "
                    "answer_chars=%d thinking_chars=%d fc_parts=%d got_response=%s",
                    model_id, finish_reason if got_response else "no_response",
                    len(full_answer), len(full_thinking), len(all_fc_parts), got_response,
                )
                if got_response and finish_reason and finish_reason not in ("STOP", ""):
                    logger.warning(
                        "Gemini stream finished with reason=%s (not STOP). "
                        "Response likely truncated. answer_chars=%d",
                        finish_reason, len(full_answer),
                    )

                # --- Post-stream: handle function calls or normal completion ---

                if got_response and all_fc_parts:
                    # Model returned function calls — execute and loop
                    fc_calls = parse_gemini_function_call(all_fc_parts)
                    if fc_calls:
                        # Save model's function call message to history
                        model_fc_parts: list[dict[str, Any]] = []
                        if full_thinking:
                            model_fc_parts.append({"text": full_thinking, "thought": True})
                        model_fc_parts.extend(all_fc_parts)
                        history.append({"role": "model", "parts": model_fc_parts})

                        # Execute each function call via Sable's handler pipeline
                        import asyncio as _asyncio
                        from engine.skills import get_skill_engine as _get_engine
                        engine = _get_engine()

                        for fc in fc_calls:
                            tag_event = native_call_to_tag_event(fc)

                            # Run handler pipeline in thread (it's sync)
                            try:
                                events = await _asyncio.to_thread(
                                    lambda: list(engine.process_tag(
                                        tag_event["name"], tag_event["attrs"],
                                        tag_event["content"], namespace="maria",
                                    ))
                                )
                            except Exception as exc:
                                events = [{"type": "skill_end", "name": tag_event["name"], "ok": False, "error": str(exc)}]

                            # Collect result text and yield events
                            result_text = ""
                            ok = True
                            for evt in events:
                                if evt.get("type") == "skill_output":
                                    result_text += evt.get("text", "")
                                elif evt.get("type") == "skill_end":
                                    ok = evt.get("ok", True)
                                yield evt  # Forward all skill events to frontend

                            # Format result for Gemini and add to history
                            result_msg = format_gemini_tool_result(tag_event["name"], result_text, ok)
                            history.append(result_msg)
                            logger.info("Native tool %s executed (ok=%s), continuing loop", tag_event["name"], ok)

                        # Update session and continue the while loop
                        if chat_id:
                            self._sessions[chat_id] = history
                        continue  # Re-enter API call with tool results

                # Normal completion (text response, no function calls)
                if got_response and (full_answer or full_thinking):
                    _summarize_idx = extract_summarize_tag(full_answer)
                    clean_answer = strip_summarize_tag(full_answer)
                    model_parts: list[dict[str, Any]] = []
                    if full_thinking:
                        model_parts.append({"text": full_thinking, "thought": True})
                    if clean_answer:
                        model_parts.append({"text": clean_answer})
                    history.append({"role": "model", "parts": model_parts})

                    if _summarize_idx is not None and chat_id:
                        prefix_len = 2 if history and history[0].get("parts", [{}])[0].get("text", "").startswith("[System Instructions]") else 0
                        actual_cut = max(prefix_len, min(_summarize_idx, len(history) - 1))
                        msgs_to_summarize = history[prefix_len:actual_cut]
                        if len(msgs_to_summarize) >= 2:
                            prompt = build_summary_prompt(msgs_to_summarize, _msg_chars)
                            summary = await self._call_self_summarize(prompt, model_id, thinking_mode)
                            if summary:
                                logger.info("Model-triggered summarization at index %d for chat %s", _summarize_idx, chat_id)
                                history = rewrite_history_with_summary(history, summary, actual_cut, prefix_len, fmt="gemini")
                                self._sessions[chat_id] = history

                    if chat_id:
                        prefix_len = 2 if history and history[0].get("parts", [{}])[0].get("text", "").startswith("[System Instructions]") else 0
                        total_chars = sum(_msg_chars(m) for m in history[prefix_len:])
                        eff_max = self._get_max_chars(chat_id)
                        if total_chars > eff_max:
                            self._sessions[chat_id] = _trim_history(history, prefix_len, eff_max)

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
