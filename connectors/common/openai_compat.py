"""Unified OpenAI-compatible connector for Sable.

A single, config-driven implementation that powers every OpenAI-compatible
chat backend: OpenAI, Groq, Mistral, and any custom endpoint (Cloudflare,
etc.). Replaces the three near-identical per-provider clients.

Provider behaviour is selected via class attributes (subclasses) or via
constructor overrides (ad-hoc custom endpoints):

    NAME                    display name (logger + error strings)
    BASE_URL                OpenAI-compatible base URL (no trailing slash)
    KEYS_PATH               Path to JSON key pool (None => single-key mode)
    DEFAULT_MODEL           fallback model id
    INSTRUCTION_MODE        "minimal" | "project" | None
    ENABLE_REASONING        send reasoning_effort for thinking modes

Streams SSE from ``/chat/completions`` and yields Sable-standard events:
    {"type": "answer",   "text": "..."}
    {"type": "thinking", "text": "..."}
    {"type": "done",     "parent_id": "..."}
    {"type": "error",    "message": "..."}
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import httpx

from connectors.common.media import prepare_inline_file, to_openai_image
from connectors.common.native_tools import (
    openai_to_openai_tools,
    format_openai_tool_result,
    native_call_to_tag_event,
)
from connectors.common.context_summarizer import (
    should_inject_hint, should_force_summarize, get_hint_text,
    extract_summarize_tag, strip_summarize_tag, build_summary_prompt,
    rewrite_history_with_summary, compute_force_cut_index,
)

_SYSTEM_DIR = Path(__file__).resolve().parent.parent.parent / "system"

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


def _trim_history(
    history: list[dict[str, Any]], prefix_len: int, max_chars: int | None = None,
) -> list[dict[str, Any]]:
    """Trim history to fit within max_chars, preserving prefix messages."""
    limit = max_chars if max_chars is not None else _MAX_SESSION_CHARS
    prefix = history[:prefix_len]
    msgs = history[prefix_len:]
    total = sum(_msg_chars(m) for m in msgs)
    while total > limit and len(msgs) > 1:
        total -= _msg_chars(msgs.pop(0))
    return prefix + msgs


def _minimal_instructions() -> str:
    """Compact agentic tool docs for providers with tight prompt budgets.

    Built via concatenation so this module never contains a literal
    ``<tool_call>`` closing tag (which would break agent transport).
    """
    open_tag = "<" + "tool_call>"
    close_tag = "</" + "tool_call>"
    base = (
        f"CRITICAL RULE: Every response may contain exactly ONE {open_tag} opening tag and ONE {close_tag} closing tag.\n"
        f"The extractor only reads what is inside {open_tag}; anything outside is prose.\n\n"
        f'Single call: {open_tag}{{"name": "grep", ...}}{close_tag}\n'
        f'Multiple calls: {open_tag}[{{"name": "grep", ...}}, {{"name": "view_file", ...}}]{close_tag}\n'
        f"NEVER output multiple separate {open_tag} blocks. Always wrap ALL calls in ONE array inside ONE wrapper.\n\n"
        f"If you use {open_tag}, keep prose to ONE short sentence before the block. "
        f"{open_tag} appears only in plain text, never inside a fenced code block."
    )
    editor = (
        "# File I/O\n\n"
        "## Read files\n"
        f' {open_tag}{{"name": "get_file", "arguments": {{"path": "/abs/path"}}}}{close_tag} — read any file (text or binary)\n'
        f' {open_tag}{{"name": "view_file", "arguments": {{"path": "/abs/path"}}}}{close_tag} — read with line numbers, supports start/end range\n\n'
        "## Write files\n"
        f' {open_tag}{{"name": "edit_file", "arguments": {{"path": "/abs/path", "old_str": "...", "new_str": "..."}}}}{close_tag}\n'
        "    <<<<<< SEARCH\n    exact old text from view_file\n    =======\n    new replacement text\n    >>>>>>\n"
        "     — replace text (must match exactly once)\n\n"
        f' {open_tag}{{"name": "create_file", "arguments": {{"path": "/abs/path", "content": "..."}}}}{close_tag}\n'
        "    file content here\n     — create new file (fails if exists)\n\n"
        "## Rules\n"
        "- Always view_file before editing — never build old_str from memory\n"
        f"- Wrap every tag in {open_tag}...{close_tag}\n"
        f"- One short sentence + the {open_tag} block, nothing else"
    )
    from engine.config import OUTPUT_ROOT as _OUT
    output_dir = (
        f"# Output Directory (MANDATORY)\n"
        f"ALL generated content MUST be saved under `{_OUT}/`. NEVER save to CWD or project root.\n"
        f"Default to `{_OUT}/notes/` for text/docs when no path specified."
    )
    return base + "\n\n***\n\n" + editor + "\n\n***\n\n" + output_dir


class OpenAICompatClient:
    """Async OpenAI-compatible client with key rotation + session history.

    Subclasses override the class attributes below to specialise. Instances
    may also be constructed ad-hoc with explicit ``base_url``/``api_key`` for
    custom endpoints.
    """

    # ---- provider config (override in subclasses) ---------------------
    NAME: str = "OpenAI-Compatible"
    BASE_URL: str = ""
    KEYS_PATH: Path | None = None
    DEFAULT_MODEL: str = ""
    INSTRUCTION_MODE: str | None = None   # "minimal" | "project" | None
    ENABLE_REASONING: bool = False
    SUPPORTS_MULTIMODAL_CONTENT: bool = True  # False → flatten array content to strings

    MAX_TOOL_ROUNDS: int = 20

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        name: str | None = None,
        default_model: str | None = None,
        keys_path: Path | str | None = None,
        instruction_mode: str | None | object = "__unset__",
    ) -> None:
        if name:
            self.NAME = name
        if base_url:
            self.BASE_URL = base_url
        if default_model:
            self.DEFAULT_MODEL = default_model
        if keys_path is not None:
            self.KEYS_PATH = Path(keys_path)
        if instruction_mode != "__unset__":
            self.INSTRUCTION_MODE = instruction_mode  # type: ignore[assignment]


        self.BASE_URL = self.BASE_URL.rstrip("/")
        self.logger = logging.getLogger(f"sable.{self.NAME.lower().replace(' ', '_')}_api")

        self._single_key = bool(api_key)
        if api_key:
            self._keys: list[str] = [api_key]
        else:
            self._keys = self._load_keys()
        self._key_index: int = 0
        # Session history: chat_id -> list of OpenAI-format messages
        self._sessions: dict[str, list[dict[str, Any]]] = {}
        self._session_max_chars: dict[str, int] = {}
        self._http: httpx.AsyncClient | None = None
        self._instruction_cache: str | None = None
        self._cached_project_id: str | None = "__none__"
        self._cached_version: int = -1

    # ------------------------------------------------------------------
    # Key management
    # ------------------------------------------------------------------

    def _load_keys(self) -> list[str]:
        if self.KEYS_PATH and self.KEYS_PATH.exists():
            try:
                data = json.loads(self.KEYS_PATH.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return [k for k in data if isinstance(k, str) and k.strip()]
            except (json.JSONDecodeError, OSError):
                pass
        return []

    def _save_keys(self, keys: list[str]) -> None:
        if self._single_key or not self.KEYS_PATH:
            return
        self.KEYS_PATH.write_text(json.dumps(keys, indent=2), encoding="utf-8")

    @property
    def is_available(self) -> bool:
        return len(self._keys) > 0

    @property
    def _current_key(self) -> str | None:
        if not self._keys:
            return None
        return self._keys[self._key_index % len(self._keys)]

    def add_key(self, key: str) -> None:
        key = key.strip()
        if key and key not in self._keys:
            self._keys.append(key)
            self._save_keys(self._keys)

    def remove_key(self, index: int) -> bool:
        if 0 <= index < len(self._keys):
            self._keys.pop(index)
            if self._key_index >= len(self._keys) and self._keys:
                self._key_index = 0
            self._save_keys(self._keys)
            return True
        return False

    def list_keys(self) -> list[dict[str, Any]]:
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
        if len(self._keys) <= 1:
            return self._current_key
        self._key_index = (self._key_index + 1) % len(self._keys)
        return self._keys[self._key_index]

    # ------------------------------------------------------------------
    # Instructions
    # ------------------------------------------------------------------

    def _load_instructions(self, project_id: str | None = None) -> str:
        mode = self.INSTRUCTION_MODE
        if mode == "project":
            from connectors.common.instruction_builder import get_instruction_version
            current_version = get_instruction_version()
            if project_id != self._cached_project_id or current_version != self._cached_version:
                self._instruction_cache = None
                self._cached_project_id = project_id
                self._cached_version = current_version
            if self._instruction_cache is not None:
                return self._instruction_cache
            from connectors.common.instruction_builder import build_instructions
            self._instruction_cache = build_instructions(project_id=project_id)
            return self._instruction_cache
        if mode == "minimal":
            return _minimal_instructions()
        return ""

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    async def _get_http(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(120.0, connect=15.0),
            )
        return self._http

    # ------------------------------------------------------------------
    # Thinking / reasoning helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _model_supports_thinking(model_id: str) -> bool:
        try:
            from engine.config import get_model_config
            cfg = get_model_config(model_id)
            modes = cfg.get("thinking_modes", [])
            return any(m.get("thinking_enabled", False) for m in modes)
        except Exception:
            return False

    @staticmethod
    def _resolve_reasoning_effort(thinking_mode: str | None) -> str | None:
        if not thinking_mode:
            return None
        mode = thinking_mode.lower()
        if mode in ("fast", "none"):
            return None
        if mode in ("thinking", "deepthink", "high"):
            return "high"
        if mode == "medium":
            return "medium"
        if mode == "low":
            return "low"
        return None

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def _get_max_chars(self, chat_id: str | None) -> int:
        if chat_id and chat_id in self._session_max_chars:
            return self._session_max_chars[chat_id]
        return _MAX_SESSION_CHARS

    def _get_or_create_session(
        self,
        chat_id: str | None,
        inject_instructions: bool,
        system_instruction: str | None = None,
        max_session_chars: int | None = None,
        project_id: str | None = None,
    ) -> list[dict[str, Any]]:
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
        instructions = (
            system_instruction
            if system_instruction
            else (self._load_instructions(project_id) if inject_instructions else None)
        )
        if instructions:
            history.append({"role": "system", "content": instructions})

        if chat_id:
            self._sessions[chat_id] = history
        return history

    async def _maybe_summarize(
        self, chat_id: str, history: list[dict[str, Any]],
        max_chars: int, model_id: str,
    ) -> list[dict[str, Any]]:
        prefix_len = 1 if history and history[0].get("role") == "system" else 0
        total_chars = sum(_msg_chars(m) for m in history[prefix_len:])
        if should_force_summarize(total_chars, max_chars):
            cut_idx = compute_force_cut_index(history, prefix_len)
            msgs_to_summarize = history[prefix_len:cut_idx]
            if len(msgs_to_summarize) >= 2:
                prompt = build_summary_prompt(msgs_to_summarize, _msg_chars)
                summary = await self._call_self_summarize(prompt, model_id)
                if summary:
                    self.logger.info(
                        "Force-summarized %d messages for chat %s",
                        len(msgs_to_summarize), chat_id,
                    )
                    history = rewrite_history_with_summary(
                        history, summary, cut_idx, prefix_len, fmt="openai",
                    )
                    self._sessions[chat_id] = history
        return history

    async def _call_self_summarize(self, prompt: str, model_id: str) -> str | None:
        url = f"{self.BASE_URL}/chat/completions"
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
                resp = await http.post(
                    url, headers={"Authorization": f"Bearer {key}"}, json=body,
                )
                if resp.status_code in (401, 403, 429):
                    self._rotate_key()
                    continue
                if resp.status_code != 200:
                    self.logger.warning("%s summarizer failed: HTTP %d", self.NAME, resp.status_code)
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
                self.logger.warning("%s summarizer error: %s", self.NAME, e)
                self._rotate_key()
                continue
        return None

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
        tools: list[dict] | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Stream a chat completion, yielding Sable-standard events."""
        # Pop kwargs used only by other connectors / callers
        kwargs.pop("model_id", None)
        kwargs.pop("ref_file_ids", None)

        if not self._keys:
            yield {"type": "error", "message": f"No {self.NAME} API keys configured. Add one in Settings → Providers."}
            return

        model_id = model or self.DEFAULT_MODEL
        url = f"{self.BASE_URL}/chat/completions"

        system_instruction = kwargs.pop("system_instruction", None)
        project_id = kwargs.pop("project_id", None)
        db_history = kwargs.pop("db_history", None)
        history = self._get_or_create_session(
            chat_id, inject_instructions,
            system_instruction=system_instruction,
            max_session_chars=max_session_chars,
            project_id=project_id,
        )
        # Seed from DB when session is fresh (cross-provider switch)
        if db_history and chat_id and len(history) <= 1:
            for _m in db_history:
                history.append({"role": _m["role"], "content": _m["content"]})

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
                    content[0]["text"] += (
                        f"\n\n[Attached file: {Path(fpath).name} "
                        f"({pf.mime_type}, {pf.size_bytes} bytes)]"
                    )
            history.append({"role": "user", "content": content})
        else:
            history.append({"role": "user", "content": message})

        # Sanitize messages for providers with limited format support
        # (e.g. Cloudflare Workers AI: no array content, no null content,
        #  no native tool_calls/tool role)
        if not self.SUPPORTS_MULTIMODAL_CONTENT:
            self.logger.warning("SANITIZE: running for %s, %d messages", self.NAME, len(history))
            # Dump raw history before sanitization
            import json as _raw_json
            try:
                from engine.config import LOGS_DIR as _LD
                _LD.mkdir(parents=True, exist_ok=True)
                with open(_LD / "cloudflare_raw_history.jsonl", "a") as _rf:
                    _rf.write(_raw_json.dumps({"ts": __import__("time").time(), "history": history}, default=str) + "\n")
            except Exception:
                pass
            sanitized: list[dict[str, Any]] = []
            for msg in history:
                m = dict(msg)  # shallow copy
                c = m.get("content")

                # Flatten array content to string
                if isinstance(c, list):
                    parts = []
                    for item in c:
                        if isinstance(item, dict):
                            if item.get("type") == "text":
                                parts.append(item.get("text", ""))
                            elif item.get("type") == "image_url":
                                parts.append("[image attached]")
                            else:
                                parts.append(str(item))
                        else:
                            parts.append(str(item))
                    m["content"] = "\n".join(parts) if parts else ""

                # Fix null content (assistant msgs with tool_calls)
                if m.get("content") is None:
                    m["content"] = ""

                sanitized.append(m)
            history = sanitized

        payload: dict[str, Any] = {
            "model": model_id,
            "messages": history,
            "stream": True,
        }

        # DEBUG: dump full payload to file for inspection (append mode)
        import json as _dbg_json, time as _dbg_time
        from engine.config import LOGS_DIR as _LD2
        _dbg_path = str(_LD2 / "cloudflare_payload.jsonl")
        try:
            from pathlib import Path as _P
            _P(_dbg_path).parent.mkdir(parents=True, exist_ok=True)
            with open(_dbg_path, "a") as _f:
                _f.write(_dbg_json.dumps({"ts": _dbg_time.time(), "payload": payload}, default=str) + "\n")
        except Exception:
            pass

        if self.ENABLE_REASONING and self._model_supports_thinking(model_id):
            reasoning = self._resolve_reasoning_effort(thinking_mode)
            if reasoning:
                payload["reasoning_effort"] = reasoning

        if tools:
            oai_tools = openai_to_openai_tools(tools)
            if oai_tools:
                payload["tools"] = oai_tools

        _tool_round = 0
        while _tool_round < self.MAX_TOOL_ROUNDS:
            _tool_round += 1

            attempts = len(self._keys)
            for attempt in range(attempts):
                key = self._current_key
                if not key:
                    break

                try:
                    http = await self._get_http()
                    full_answer = ""
                    full_thinking = ""

                    # Re-sanitize before each API call (tool rounds add new msgs)
                    if not self.SUPPORTS_MULTIMODAL_CONTENT:
                        for msg in payload["messages"]:
                            if msg.get("content") is None:
                                msg["content"] = ""
                            elif isinstance(msg.get("content"), list):
                                parts = []
                                for item in msg["content"]:
                                    if isinstance(item, dict):
                                        if item.get("type") == "text":
                                            parts.append(item.get("text", ""))
                                        elif item.get("type") == "image_url":
                                            parts.append("[image attached]")
                                        else:
                                            parts.append(str(item))
                                    else:
                                        parts.append(str(item))
                                msg["content"] = "\n".join(parts) if parts else ""

                    # DEBUG: save exact payload sent to Cloudflare
                    import json as _pj
                    try:
                        from engine.config import LOGS_DIR as _LD3
                        with open(_LD3 / "cloudflare_sent_payload.json", "w") as _pf:
                            _pj.dump(payload, _pf, indent=2, default=str)
                    except Exception:
                        pass

                    async with http.stream(
                        "POST", url,
                        headers={"Authorization": f"Bearer {key}"},
                        json=payload,
                    ) as response:
                        if response.status_code in (401, 403, 429):
                            await response.aread()
                            self._rotate_key()
                            continue
                        if response.status_code != 200:
                            body = await response.aread()
                            yield {"type": "error", "message": f"{self.NAME} API error {response.status_code}: {body.decode()[:200]}"}
                            return

                        in_thinking = False
                        _tc_buffers: dict[int, dict] = {}

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

                            tc = delta.get("content")
                            if tc:
                                if isinstance(tc, str):
                                    if in_thinking:
                                        in_thinking = False
                                    full_answer += tc
                                    clean_text = strip_summarize_tag(tc)
                                    if clean_text:
                                        yield {"type": "answer", "text": clean_text}
                                elif isinstance(tc, list):
                                    for part in tc:
                                        ptype = part.get("type", "")
                                        if ptype == "thinking":
                                            in_thinking = True
                                            for inner in part.get("thinking", []):
                                                text = inner.get("text", "") if isinstance(inner, dict) else str(inner)
                                                if text:
                                                    full_thinking += text
                                                    yield {"type": "thinking", "text": text}
                                        elif ptype == "text":
                                            if in_thinking:
                                                in_thinking = False
                                            text = part.get("text", "")
                                            if text:
                                                full_answer += text
                                                clean_text = strip_summarize_tag(text)
                                                if clean_text:
                                                    yield {"type": "answer", "text": clean_text}

                            tc_deltas = delta.get("tool_calls")
                            if tc_deltas:
                                for tcd in tc_deltas:
                                    idx = tcd.get("index", 0)
                                    if idx not in _tc_buffers:
                                        _tc_buffers[idx] = {"id": tcd.get("id", ""), "name": "", "args_str": ""}
                                    buf = _tc_buffers[idx]
                                    if tcd.get("id"):
                                        buf["id"] = tcd["id"]
                                    fn = tcd.get("function", {})
                                    if fn.get("name"):
                                        buf["name"] = fn["name"]
                                    if fn.get("arguments"):
                                        buf["args_str"] += fn["arguments"]

                        # Handle tool calls
                        if _tc_buffers:
                            fc_calls = []
                            for idx in sorted(_tc_buffers.keys()):
                                buf = _tc_buffers[idx]
                                try:
                                    args = json.loads(buf["args_str"]) if buf["args_str"] else {}
                                except json.JSONDecodeError:
                                    args = {}
                                fc_calls.append({"name": buf["name"], "args": args, "id": buf["id"]})

                            if fc_calls:
                                assistant_msg: dict[str, Any] = {"role": "assistant", "content": full_answer or None}
                                assistant_msg["tool_calls"] = [
                                    {"id": fc["id"], "type": "function",
                                     "function": {"name": fc["name"], "arguments": json.dumps(fc["args"])}}
                                    for fc in fc_calls
                                ]
                                history.append(assistant_msg)

                                import asyncio as _asyncio
                                from engine.skills import get_skill_engine as _get_engine
                                engine = _get_engine()

                                for fc in fc_calls:
                                    # chat_title is handled by the chat route via stream parsing,
                                    # not by the skill engine. Acknowledge it and move on.
                                    if fc["name"] == "chat_title":
                                        title_val = fc.get("args", {}).get("title", "")
                                        yield {"type": "chat_title", "title": title_val[:80]}
                                        tool_result = format_openai_tool_result(
                                            "chat_title", f"Title set to: {title_val[:80]}", True, fc.get("id", ""),
                                        )
                                        history.append(tool_result)
                                        self.logger.info("Native tool chat_title acknowledged (title=%s)", title_val[:80])
                                        continue

                                    tag_event = native_call_to_tag_event(fc)
                                    try:
                                        events = await _asyncio.to_thread(
                                            lambda: list(engine.process_tag(
                                                tag_event["name"], tag_event["attrs"],
                                                tag_event["content"], namespace="maria",
                                            ))
                                        )
                                    except Exception as exc:
                                        events = [{"type": "skill_end", "name": tag_event["name"], "ok": False, "error": str(exc)}]

                                    result_text = ""
                                    ok = True
                                    for evt in events:
                                        if evt.get("type") == "skill_output":
                                            result_text += evt.get("text", "")
                                        elif evt.get("type") == "skill_end":
                                            ok = evt.get("ok", True)
                                        yield evt

                                    tool_result = format_openai_tool_result(
                                        tag_event["name"], result_text, ok, fc.get("id", ""),
                                    )
                                    history.append(tool_result)
                                    self.logger.info(
                                        "Native tool %s executed (ok=%s), continuing loop",
                                        tag_event["name"], ok,
                                    )

                                if chat_id:
                                    self._sessions[chat_id] = history
                                continue  # re-enter API call with tool results

                        # Normal completion
                        _summarize_idx = extract_summarize_tag(full_answer)
                        clean_answer = strip_summarize_tag(full_answer)
                        history.append({"role": "assistant", "content": clean_answer})

                        if _summarize_idx is not None and chat_id:
                            prefix_len = 1 if history and history[0].get("role") == "system" else 0
                            actual_cut = max(prefix_len, min(_summarize_idx, len(history) - 1))
                            msgs_to_summarize = history[prefix_len:actual_cut]
                            if len(msgs_to_summarize) >= 2:
                                prompt = build_summary_prompt(msgs_to_summarize, _msg_chars)
                                summary = await self._call_self_summarize(prompt, model_id)
                                if summary:
                                    self.logger.info(
                                        "Model-triggered summarization at index %d for chat %s",
                                        _summarize_idx, chat_id,
                                    )
                                    history = rewrite_history_with_summary(
                                        history, summary, actual_cut, prefix_len, fmt="openai",
                                    )
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
                    yield {"type": "error", "message": f"{self.NAME} request timed out. Try again."}
                    return
                except Exception as exc:
                    self.logger.warning("%s stream error (attempt %d): %s", self.NAME, attempt + 1, exc)
                    self._rotate_key()
                    continue

        yield {"type": "error", "message": f"All {self.NAME} API keys exhausted or failed."}

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
