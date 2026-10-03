"""ChatGPT (chatgpt.com) connector for Sable.

Transport: ghost-tab in-page fetch HIJACK (see ``ghost.py``) — the only viable
path, because ``POST /backend-api/f/conversation`` is gated by a per-message
sentinel bundle (proof-of-work + Turnstile + chat-requirements) plus an
``x-conduit-token`` JWT, all single-use and bound to the SPA's own send.

We do not replay. The ghost lets the SPA mint + send, hijacks that request to
inject our prompt, and tees the SSE response back. This client decodes the
ChatGPT SSE delta/patch stream into Sable's stream events.

Yields events matching Sable's stream interface:
  {"type": "answer",   "text": "..."}
  {"type": "thinking", "text": "..."}
  {"type": "done",     "chat_id": "..."}
  {"type": "error",    "message": "..."}
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from pathlib import Path as _Path
from typing import Any

from connectors.common.instruction_builder import get_tool_format

logger = logging.getLogger("sable.chatgpt_api")

_IDLE_TIMEOUT_SECS = 45.0
_DEFAULT_MODEL = "gpt-5"

_instruction_cache: str | None = None
_cached_project_id: str | None = None
_cached_version: int | None = None
_cached_layout_mode: str | None = None


def _load_instructions(project_id: str | None = None, layout_mode: str | None = None) -> str:
    """Load instruction context for first-message injection. Project-aware."""
    global _instruction_cache, _cached_project_id, _cached_version, _cached_layout_mode
    from connectors.common.instruction_builder import get_instruction_version
    current_version = get_instruction_version()
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
        project_id=project_id, provider="chatgpt", layout_mode=layout_mode,
    )
    return _instruction_cache


def _resolve_active_account() -> str:
    """ChatGPT's OWN active browser account (independent of every other provider)."""
    from engine.config import get_active_account
    return get_active_account("chatgpt")


def _chatgpt_ghost_enabled() -> bool:
    try:
        from connectors.chatgpt import ghost as _g
        return _g.ghost_enabled()
    except Exception:
        return True


# ----------------------------------------------------------------------
# SSE decoding
# ----------------------------------------------------------------------

_ASSISTANT_ROLE = "assistant"


def _apply_patches(state: dict, patches: list[dict]) -> list[dict]:
    """Apply `{p, o, v}` JSON-patch ops to the tracked assistant message."""
    events: list[dict] = []
    for p in patches:
        if not isinstance(p, dict):
            continue
        path = str(p.get("p", ""))
        op = p.get("o", "")
        val = p.get("v")
        if path.startswith("/message/content/parts/"):
            if op in ("append", "add", "replace") and isinstance(val, str):
                ch = state.get("channel", "answer")
                events.append({"type": ch, "text": val})
        elif path == "/message/status" and val == "finished_successfully":
            state["finished"] = True
    return events


def _process_event(state: dict, obj: dict) -> list[dict]:
    """Fold one parsed ChatGPT delta object into Sable stream events."""
    events: list[dict] = []
    v = obj.get("v")
    msg = None
    if isinstance(v, dict) and isinstance(v.get("message"), dict):
        msg = v["message"]
    elif isinstance(v, dict) and v.get("author") and v.get("id"):
        msg = v
    if msg is not None:
        mid = msg.get("id")
        role = (msg.get("author") or {}).get("role")
        if mid:
            state.setdefault("messages", {})[mid] = {"role": role}
            if role == _ASSISTANT_ROLE:
                state["assistant_id"] = mid
                ch = (msg.get("metadata") or {}).get("channel")
                state["channel"] = "thinking" if ch == "commentary" else "answer"
                content = msg.get("content") or {}
                parts = content.get("parts") if isinstance(content, dict) else None
                if isinstance(parts, list):
                    for part in parts:
                        if isinstance(part, str) and part:
                            events.append({"type": state["channel"], "text": part})
        return events
    # Pathless delta append: {"v": "..."} with no p/o. ChatGPT's delta stream
    # uses this shorthand to continue the CURRENT content part — e.g. the
    # middle of a multi-chunk answer arrives as a bare {"v": "6\n7\n..."}.
    # Without this branch every pathless delta is silently dropped, which is
    # exactly how the middle of a reply goes missing (first + last kept only).
    if isinstance(v, str) and v and obj.get("p") is None and obj.get("o") is None:
        ch = state.get("channel", "answer")
        events.append({"type": "thinking" if ch == "thinking" else "answer",
                       "text": v})
        return events

    patches: list[dict] = []
    if isinstance(v, list):
        patches = [x for x in v if isinstance(x, dict)]
    elif isinstance(v, str) and obj.get("o") and obj.get("p"):
        patches = [obj]
    if patches:
        events.extend(_apply_patches(state, patches))
        return events
    t = obj.get("type")
    if t in ("message_stream_complete", "message_stream_complete_with_metadata"):
        state["stream_complete"] = True
    if obj.get("conversation_id") and not state.get("conversation_id"):
        state["conversation_id"] = obj["conversation_id"]
    return events


# ----------------------------------------------------------------------
# The client
# ----------------------------------------------------------------------

class ChatGPTClient:
    """Ghost-tab ChatGPT connector."""

    def __init__(self, account: str | None = None, model_id: str | None = None) -> None:
        from connectors.common.error_ctx import ErrorContext
        self.ctx = ErrorContext("chatgpt")
        self._account = account
        self._model_id = model_id or "chatgpt-gpt-5"
        self._conversation_ids: dict[str, str] = {}
        self._sessions: dict[str, list[dict[str, str]]] = {}
        self._live_session: Any = None

    @property
    def account(self) -> str:
        acct = self._account or _resolve_active_account()
        self.ctx.account = acct
        return acct

    @property
    def is_available(self) -> bool:
        if not _chatgpt_ghost_enabled():
            return False
        from connectors.chatgpt.ghost import _SYSTEM_DIR
        return (_SYSTEM_DIR / self.account).is_dir()

    async def _resolve_session(self):
        try:
            if not _chatgpt_ghost_enabled():
                return None
            from connectors.chatgpt.ghost import ChatGPTGhostSession
            return await ChatGPTGhostSession.for_account(self.account)
        except Exception as exc:
            logger.error("ChatGPT ghost unavailable for account '%s': %s",
                         self.account, exc, exc_info=True)
            return None

    async def stream_chat(
        self,
        message: str,
        *,
        model: str | None = None,
        thinking_mode: str | None = None,
        chat_id: str | None = None,
        ref_file_ids: list[str] | None = None,
        files: list[str] | None = None,
        inject_instructions: bool = True,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, Any], None]:
        prompt = message
        system_instruction = kwargs.pop("system_instruction", None)
        project_id = kwargs.pop("project_id", None)
        db_history = kwargs.pop("db_history", None)
        layout_mode = kwargs.pop("layout_mode", None)
        if inject_instructions:
            try:
                _reminder = get_tool_format("chatgpt") or get_tool_format()
            except Exception:
                _reminder = ""
            if _reminder:
                prompt = f"{_reminder}\n\n{message}"
        session = await self._resolve_session()
        if session is None:
            yield self.ctx.error("ghost unavailable — check server logs (stale lock / Helium crash)")
            return
        # DO NOT call session.has_token() here.
        #
        # has_token() fires an in-page fetch('/api/auth/session') right before
        # open_stream(). The SPA then runs /f/conversation/prepare but never
        # issues the actual POST /f/conversation — the send is silently
        # swallowed and the caller sits out the full idle timeout with zero
        # events. Reproduced deterministically: with has_token -> 0 lines,
        # without -> 54 lines in 5s. Login is already validated when the
        # session starts (for_account -> _install_hijack waits for the
        # composer, and raises "not logged in?" if it never appears).
        upstream = self._conversation_ids.get(chat_id) if chat_id else None
        is_fresh = upstream is None
        if is_fresh and inject_instructions:
            _instr: str | None = None
            if system_instruction:
                _instr = system_instruction
            else:
                try:
                    _instr = _load_instructions(project_id=project_id, layout_mode=layout_mode)
                except Exception as exc:
                    logger.warning("ChatGPT instruction build failed: %s", exc)
            if _instr:
                prompt = f"{_instr}\n\n{prompt}"
        history = self._sessions.get(chat_id, []) if chat_id else []
        if db_history and chat_id and not history:
            history = [
                {"role": m["role"], "content": m["content"]}
                for m in db_history
                if m.get("role") in ("user", "assistant") and m.get("content")
            ]
        if is_fresh and history:
            hist_txt = "\n".join(f"{h['role']}: {h['content']}" for h in history[-10:])
            prompt = f"{hist_txt}\n\nUser: {prompt}"
        got_answer = False
        # NO RETRY. A retry re-fires requestSubmit() and duplicates the user
        # turn in the SPA (we saw three copies of the prompt in the UI). One
        # open_stream per call — if it fails, it fails cleanly, no duplicate send.
        state: dict[str, Any] = {
            "assistant_id": None, "channel": "answer",
            "conversation_id": upstream, "messages": {}, "finished": False,
        }
        try:
            self._live_session = session
            # Fresh conversation: navigate to a blank chat FIRST. This must
            # precede uploads — the navigation reloads the page and would wipe
            # any attachments staged before it. Continuing chats skip this so
            # the page stays on the ongoing conversation.
            if is_fresh:
                try:
                    await session.prepare_new_chat()
                except Exception as _nav:
                    logger.warning("ChatGPT fresh-chat navigation failed: %s", _nav)
            # Stage any attached files in the composer BEFORE the send. The SPA
            # uploads each one through its own /backend-api/files path and
            # builds messages[0].metadata.attachments; the hijack preserves
            # that metadata while swapping the text. Uploads must happen on the
            # same page/tab that will submit, so do it here, not earlier.
            if files:
                for _f in files:
                    try:
                        _info = await session.upload_file(_f)
                    except Exception as _up:
                        logger.warning("ChatGPT file upload raised for %s: %s", _f, _up)
                        _info = {"name": _Path(_f).name, "ok": False, "reason": "upload_failed"}
                    if _info.get("ok"):
                        yield {
                            "type": "status",
                            "message": f"browser_file_attached:{_info.get('name', _f)}",
                        }
                    else:
                        # Upload failed (rate limit / too large / auth / …).
                        # Emit a classified status and KEEP GOING — the turn
                        # proceeds without the attachment rather than aborting.
                        yield {
                            "type": "status",
                            "message": (
                                f"browser_file_attach_failed:"
                                f"{_info.get('reason', 'upload_failed')}:"
                                f"{_info.get('name', _f)}"
                            ),
                        }
            resp = await session.open_stream(prompt, idle_secs=_IDLE_TIMEOUT_SECS)
            async with resp as r:
                if r.status_code != 200:
                    yield self.ctx.error(f"HTTP {r.status_code}", status_code=r.status_code)
                    return
                async for line in r.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    payload = line[6:].strip()
                    if not payload or payload == "[DONE]" or payload == '"v1"':
                        continue
                    try:
                        obj = json.loads(payload)
                    except Exception:
                        continue
                    if not isinstance(obj, dict):
                        continue
                    for ev in _process_event(state, obj):
                        if ev["type"] == "answer":
                            if not ev.get("text"):
                                continue
                            got_answer = True
                        yield ev
                    if state.get("conversation_id") and chat_id:
                        self._conversation_ids[chat_id] = state["conversation_id"]
                    # Modern ChatGPT streams emit `message_stream_complete`,
                    # NOT the legacy `/message/status == finished_successfully`.
                    # Only checking `finished` meant the completion event was
                    # silently ignored and the drain sat out the full idle
                    # timeout even though the answer had already ended.
                    if (state.get("finished")
                            or state.get("stream_complete")) and got_answer:
                        break
            if chat_id and state.get("conversation_id"):
                self._conversation_ids[chat_id] = state["conversation_id"]
            yield {"type": "done", "chat_id": state.get("conversation_id")}
            return
        except asyncio.TimeoutError:
            # Idle in the stream. Never re-send — just close with what we have.
            if got_answer:
                logger.warning("ChatGPT stream idle after partial answer — closing gracefully")
                yield {"type": "done", "chat_id": state.get("conversation_id")}
                return
            yield self.ctx.error("stream idle timeout")
            return
        except Exception as exc:
            yield self.ctx.error(f"stream failed: {exc}")
            return

    async def cancel(self, chat_id: str) -> bool:
        session = self._live_session
        if session is None:
            return False
        try:
            return session.cancel_stream()
        except Exception as exc:
            logger.warning("ChatGPT cancel failed (%s)", exc)
            return False

    async def chat(self, message: str, **kwargs: Any) -> dict[str, Any]:
        answer_parts: list[str] = []
        thinking_parts: list[str] = []
        chat_id = kwargs.get("chat_id")
        parent = None
        error = None
        async for ev in self.stream_chat(message, **kwargs):
            t = ev.get("type")
            if t == "answer":
                answer_parts.append(ev.get("text", ""))
            elif t == "thinking":
                thinking_parts.append(ev.get("text", ""))
            elif t == "done":
                parent = ev.get("chat_id")
            elif t == "error":
                error = ev.get("message")
        answer = "".join(answer_parts)
        if chat_id and answer:
            hist = self._sessions.setdefault(chat_id, [])
            hist.append({"role": "user", "content": message})
            hist.append({"role": "assistant", "content": answer})
        return {
            "answer": answer,
            "thinking": "".join(thinking_parts),
            "parent_id": parent,
            "error": error,
        }


_default_client: ChatGPTClient | None = None


def get_client(account: str | None = None) -> ChatGPTClient:
    global _default_client
    if account is None:
        if _default_client is None:
            _default_client = ChatGPTClient()
        return _default_client
    return ChatGPTClient(account=account)
