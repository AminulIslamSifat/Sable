"""Kimi (kimi.ai) connector for Sable.

Transport: ghost-tab in-page fetch (see ``ghost.py``) — the only viable path,
because the endpoint needs a per-request ``x-msh-shield-data`` anti-bot token
and a localStorage bearer JWT, both of which the SPA mints/holds itself.

The backend is gRPC-Web over the Connect protocol. Each response frame is
decoded and mapped to Sable's stream events:

    mask="block.think"        op=set     -> thinking block created
    mask="block.think.content" op=append -> thinking delta
    mask="block.text"         op=set     -> answer block created (content=first delta)
    mask="block.text.content" op=append  -> answer delta
    message.status=COMPLETED             -> done

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
import re
from collections.abc import AsyncGenerator
from typing import Any

from connectors.common.instruction_builder import get_tool_format

logger = logging.getLogger("sable.kimi_api")

_IDLE_TIMEOUT_SECS = 30.0
_DEFAULT_MODEL = "k2d6-chat"


# ----------------------------------------------------------------------
# Account / settings
# ----------------------------------------------------------------------

def _resolve_active_account() -> str:
    from engine.config import get_active_account
    return get_active_account()


def _provider_account() -> str | None:
    """Optional per-provider account override from settings.provider_accounts."""
    try:
        from engine.config import _SYSTEM
        s = json.loads((_SYSTEM / "settings.json").read_text(encoding="utf-8"))
        return (s.get("provider_accounts") or {}).get("kimi")
    except Exception:
        return None


def _kimi_ghost_enabled() -> bool:
    try:
        from connectors.kimi import ghost as _g
        return _g.kimi_ghost_enabled()
    except Exception:
        return True


# ----------------------------------------------------------------------
# Request body / options
# ----------------------------------------------------------------------

# Sable thinking-mode labels -> Kimi reasoning effort enum.
_EFFORT = {
    "low": "REASONING_EFFORT_LOW",
    "medium": "REASONING_EFFORT_MEDIUM",
    "high": "REASONING_EFFORT_HIGH",
}


def _build_body(
    prompt: str,
    *,
    model: str,
    thinking_enabled: bool,
    effort: str,
    chat_id: str | None,
    history: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Compose the Connect JSON body.

    Kimi's request carries a single ``message``. For continuing an existing
    upstream chat we pass ``chat_id``; when it's absent (first turn) the server
    creates a fresh chat and returns its id in the stream.

    ``history`` is a local fallback: if we have no upstream chat_id but do have
    prior turns, fold them into the prompt so context isn't lost.
    """
    content = prompt
    if history and not chat_id:
        lines = []
        for turn in history:
            role = "User" if turn.get("role") == "user" else "Assistant"
            lines.append(f"{role}: {turn.get('content', '')}")
        lines.append(f"User: {prompt}")
        content = "\n\n".join(lines)

    body: dict[str, Any] = {
        "scenario": "SCENARIO_CHAT",
        "tools": [
            {"type": "TOOL_TYPE_SEARCH", "search": {}},
            {"type": "TOOL_TYPE_CRON_JOB"},
        ],
        "message": {
            "role": "user",
            "blocks": [{"message_id": "", "text": {"content": content}}],
            "scenario": "SCENARIO_CHAT",
            "is_goal": False,
        },
        "options": {
            "thinking": bool(thinking_enabled),
            "enable_plugin": True,
            "reasoning_effort": effort,
            "model": model or _DEFAULT_MODEL,
        },
        "project_id": "",
    }
    if chat_id:
        body["chat_id"] = chat_id
    return body


# ----------------------------------------------------------------------
# Frame -> event parsing
# ----------------------------------------------------------------------

def _parse_frame(text: str, state: dict[str, Any]) -> list[dict[str, Any]]:
    """Map one Connect frame payload to zero or more Sable stream events.

    ``state`` carries cross-frame context: ``chat_id``, whether we've emitted
    the first answer/thinking delta, and the last seen message status.
    """
    events: list[dict[str, Any]] = []
    try:
        obj = json.loads(text)
    except Exception:
        return events

    # Top-level transport error frame: {"error": {"code": ...}}
    if isinstance(obj, dict) and "error" in obj and "op" not in obj:
        err = obj.get("error") or {}
        events.append({"type": "error", "message": f"kimi: {err.get('code', 'error')}"})
        return events

    # Capture the upstream chat id the first time we see it.
    chat = obj.get("chat")
    if isinstance(chat, dict) and chat.get("id") and not state.get("chat_id"):
        state["chat_id"] = chat["id"]

    # Explicit end-of-stream marker: {"done": {}} — the cleanest terminal signal.
    if isinstance(obj.get("done"), dict):
        events.append({"type": "done", "chat_id": state.get("chat_id")})
        return events

    msg = obj.get("message")
    if isinstance(msg, dict) and msg.get("status"):
        role = msg.get("role")
        mid = msg.get("id")
        status = msg["status"]
        # On a continuation, Kimi REPLAYS the existing conversation before the
        # new turn — including the previous assistant message, already marked
        # COMPLETED. Firing on that would end the stream before turn 2 starts.
        # So: remember the id of the *current* assistant message when it enters
        # GENERATING, and only treat a completion for THAT id as terminal.
        if role == "assistant" and status == "MESSAGE_STATUS_GENERATING":
            state["assistant_id"] = mid
        elif status in ("MESSAGE_STATUS_COMPLETED", "MESSAGE_STATUS_FAILED"):
            aid = state.get("assistant_id")
            # Terminal only if it matches the current turn (or, defensively, if
            # no id was tracked AND the frame carries no role — the final
            # mask="message.status" completion).
            is_current = (aid is not None and mid == aid) or (aid is None and role not in ("system", "user") and role is not None)
            if is_current:
                state["status"] = status
                events.append({
                    "type": "done" if status == "MESSAGE_STATUS_COMPLETED" else "error",
                    "chat_id": state.get("chat_id"),
                    **({"message": "kimi message failed"} if status != "MESSAGE_STATUS_COMPLETED" else {}),
                })

    block = obj.get("block")
    if not isinstance(block, dict):
        return events

    mask = obj.get("mask", "")
    think = block.get("think")
    text_blk = block.get("text")

    if mask == "block.think" and isinstance(think, dict):
        c = think.get("content")
        if c:
            events.append({"type": "thinking", "text": c})
    elif mask == "block.think.content" and isinstance(think, dict):
        c = think.get("content")
        if c:
            events.append({"type": "thinking", "text": c})
    elif mask == "block.text" and isinstance(text_blk, dict):
        c = text_blk.get("content")
        if c:
            events.append({"type": "answer", "text": c})
    elif mask == "block.text.content" and isinstance(text_blk, dict):
        c = text_blk.get("content")
        if c:
            events.append({"type": "answer", "text": c})

    return events


# Kimi injects internal memory/status tags into the visible text stream, e.g.
# "<REF>status✦added memory</REF>". They are Kimi UI chrome, not model output,
# so strip them (and any trailing whitespace they leave) before yielding.
_REF_TAG_RE = re.compile(r"<REF>.*?</REF>", re.DOTALL)


def _clean_answer(text: str) -> str:
    if "<REF>" not in text:
        return text
    return _REF_TAG_RE.sub("", text)


# ----------------------------------------------------------------------
# The client
# ----------------------------------------------------------------------

class KimiClient:
    """Ghost-tab Kimi connector."""

    def __init__(self, account: str | None = None, model_id: str | None = None) -> None:
        self._account = account
        self._model_id = model_id or "kimi-k2"
        # Sable chat_id -> upstream Kimi chat id (for multi-turn continuation).
        self._chat_ids: dict[str, str] = {}
        # Sable chat_id -> local [{role, content}] history (fallback context).
        self._sessions: dict[str, list[dict[str, str]]] = {}

    @property
    def account(self) -> str:
        return self._account or _provider_account() or _resolve_active_account()

    @property
    def is_available(self) -> bool:
        """Ready when ghost transport is enabled and the profile exists."""
        if not _kimi_ghost_enabled():
            return False
        from connectors.kimi.ghost import _SYSTEM_DIR
        return (_SYSTEM_DIR / self.account).is_dir()

    async def _resolve_session(self):
        """Return a live ghost session, or None if unavailable. Never raises."""
        try:
            if not _kimi_ghost_enabled():
                return None
            from connectors.kimi.ghost import KimiGhostSession
            return await KimiGhostSession.for_account(self.account)
        except Exception as exc:
            logger.warning("Kimi ghost unavailable (%s)", exc)
            return None

    # -- streaming -----------------------------------------------------

    async def stream_chat(
        self,
        message: str,
        *,
        model: str | None = None,
        thinking_mode: str | None = None,
        chat_id: str | None = None,
        ref_file_ids: list[str] | None = None,
        inject_instructions: bool = True,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, Any], None]:
        from engine.config import get_model_config, get_thinking_mode_config

        cfg = get_model_config(self._model_id)
        tcfg = get_thinking_mode_config(self._model_id, thinking_mode)
        thinking_enabled = bool(tcfg.get("thinking_enabled", False))
        mode_label = (tcfg.get("thinking_mode") or "low").lower()
        effort = _EFFORT.get(mode_label, "REASONING_EFFORT_LOW")
        api_model = cfg.get("api_model_type") or _DEFAULT_MODEL

        prompt = message
        if inject_instructions:
            try:
                prompt = f"{get_tool_format()}\n\n{message}"
            except Exception:
                pass

        session = await self._resolve_session()
        if session is None:
            yield {"type": "error", "message": "kimi ghost transport unavailable"}
            return

        if not await session.has_token():
            yield {"type": "error", "message": f"kimi not logged in on {self.account}"}
            return

        upstream_chat = self._chat_ids.get(chat_id) if chat_id else None
        history = self._sessions.get(chat_id, []) if chat_id else []

        yield {"type": "status", "message": "kimi_transport_ghost"}

        body = _build_body(
            prompt,
            model=api_model,
            thinking_enabled=thinking_enabled,
            effort=effort,
            chat_id=upstream_chat,
            history=history,
        )

        state: dict[str, Any] = {"chat_id": upstream_chat, "status": None}
        got_answer = False

        for attempt in range(2):
            try:
                async with await session.open_stream(body, idle_secs=_IDLE_TIMEOUT_SECS) as resp:
                    if resp.status_code != 200:
                        yield {"type": "error", "message": f"kimi HTTP {resp.status_code}"}
                        return
                    async for flag, text in resp.aiter_frames():
                        if flag == 2:
                            break
                        for ev in _parse_frame(text, state):
                            if ev["type"] == "answer":
                                ev["text"] = _clean_answer(ev["text"])
                                if not ev["text"]:
                                    continue
                                got_answer = True
                            if ev["type"] == "done":
                                # Persist upstream chat id for continuation.
                                if chat_id and state.get("chat_id"):
                                    self._chat_ids[chat_id] = state["chat_id"]
                                yield ev
                                return
                            if ev["type"] == "error":
                                yield ev
                                return
                            yield ev
                    # Stream closed without an explicit done.
                    if chat_id and state.get("chat_id"):
                        self._chat_ids[chat_id] = state["chat_id"]
                    yield {"type": "done", "chat_id": state.get("chat_id")}
                    return
            except asyncio.TimeoutError:
                if attempt == 0 and not got_answer:
                    logger.warning("Kimi stream idle — retrying once")
                    continue
                yield {"type": "error", "message": "kimi stream idle timeout"}
                return
            except Exception as exc:
                yield {"type": "error", "message": f"kimi stream failed: {exc}"}
                return

    async def chat(self, message: str, **kwargs: Any) -> dict[str, Any]:
        """Non-streaming convenience wrapper."""
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


# ----------------------------------------------------------------------
# Registry factory
# ----------------------------------------------------------------------

_default_client: KimiClient | None = None


def get_client(account: str | None = None) -> KimiClient:
    global _default_client
    if account is None:
        if _default_client is None:
            _default_client = KimiClient()
        return _default_client
    return KimiClient(account=account)
