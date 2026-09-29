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

# Instruction cache — rebuilt only when project, layout mode, or the global
# instruction version changes. Mirrors the DeepSeek connector.
_instruction_cache: str | None = None
_cached_project_id: str | None = None
_cached_version: int | None = None
_cached_layout_mode: str | None = None


def _load_instructions(project_id: str | None = None, layout_mode: str | None = None) -> str:
    """Load instruction context for first-message injection. Project-aware."""
    global _instruction_cache, _cached_project_id, _cached_version, _cached_layout_mode
    from connectors.common.instruction_builder import get_instruction_version
    current_version = get_instruction_version()
    # Invalidate cache when project, layout mode, or instruction version changes
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
        project_id=project_id, provider="kimi", layout_mode=layout_mode,
    )
    return _instruction_cache


# ----------------------------------------------------------------------
# Account / settings
# ----------------------------------------------------------------------

def _resolve_active_account() -> str:
    """Kimi's OWN active browser account (independent of every other provider)."""
    from engine.config import get_active_account
    return get_active_account("kimi")


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
    "none":   "REASONING_EFFORT_NONE",
    "low":    "REASONING_EFFORT_LOW",
    "medium": "REASONING_EFFORT_MEDIUM",
    "high":   "REASONING_EFFORT_HIGH",
}


def _build_body(
    prompt: str,
    *,
    model: str,
    thinking_enabled: bool,
    effort: str,
    chat_id: str | None,
    history: list[dict[str, str]] | None = None,
    file_ids: list[str] | None = None,
    parent_id: str | None = None,
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

    # Text block first, then one file block per attachment — matches the UI.
    blocks: list[dict[str, Any]] = [{"message_id": "", "text": {"content": content}}]
    for fid in (file_ids or []):
        if str(fid).strip():
            blocks.append({"file": {"id": str(fid), "status": "PROCESS_STATUS_SUCCESS"}})

    message: dict[str, Any] = {
        "role": "user",
        "blocks": blocks,
        "scenario": "SCENARIO_CHAT",
        "is_goal": False,
    }
    if parent_id:
        message["parent_id"] = parent_id

    body: dict[str, Any] = {
        "scenario": "SCENARIO_CHAT",
        "tools": [
            {"type": "TOOL_TYPE_SEARCH", "search": {}},
            {"type": "TOOL_TYPE_CRON_JOB"},
        ],
        "message": message,
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
        events.append({"type": "error", "message": _decode_error(obj.get("error") or {})})
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
        # Track the parent (the previous assistant message) so the NEXT turn can
        # chain with parent_id, exactly like the UI does.
        if role == "assistant" and mid:
            state["parent_id"] = mid
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


def _decode_error(err: dict[str, Any]) -> str:
    """Turn a Kimi error frame into a human message.

    The interesting text is base64 inside ``details[].value`` — e.g. the
    resource_exhausted gate ("Too many people are chatting with Kimi...").
    """
    code = err.get("code", "error")
    for det in err.get("details") or []:
        if not isinstance(det, dict):
            continue
        val = det.get("value")
        if not val:
            continue
        try:
            import base64 as _b64
            decoded = _b64.b64decode(val).decode("utf-8", "replace")
            # The blob is protobuf-ish; pull the longest printable run.
            runs = re.findall(r"[\x20-\x7e]{12,}", decoded)
            text = max(runs, key=len).strip() if runs else decoded.strip()
            # Protobuf string fields prefix the text with a single length byte.
            # When that byte is printable it glues onto the front of the run
            # ("VToo many..."). If the first char numerically equals the
            # remaining length, it's that prefix — drop it.
            if len(text) > 1 and ord(text[0]) == len(text) - 1:
                text = text[1:]
            if text:
                return f"kimi: {code} — {text}"
        except Exception:
            pass
    return f"kimi: {code}"


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
        from connectors.common.error_ctx import ErrorContext
        self.ctx = ErrorContext("kimi")
        self._account = account
        self._model_id = model_id or "kimi-k2.6"
        # Sable chat_id -> upstream Kimi chat id (for multi-turn continuation).
        self._chat_ids: dict[str, str] = {}
        # Sable chat_id -> last assistant message id, used as parent_id so Kimi
        # chains the turn server-side (matches the UI).
        self._parent_ids: dict[str, str] = {}
        # Sable chat_id -> the assistant message id currently GENERATING, so a
        # concurrent cancel() can target the live turn (not the previous one).
        self._generating: dict[str, str] = {}
        # Sable chat_id -> live ghost session, for cancel/upload during a stream.
        self._live_session: Any = None
        # Sable chat_id -> local [{role, content}] history (fallback context).
        self._sessions: dict[str, list[dict[str, str]]] = {}

    @property
    def account(self) -> str:
        acct = self._account or _provider_account() or _resolve_active_account()
        self.ctx.account = acct
        return acct

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
            # Log at ERROR with full traceback so restart issues (stale locks,
            # Helium crashes, CDP adopt failures) are immediately visible
            # instead of surfacing as a cryptic "no ghost available" in the UI.
            logger.error("Kimi ghost unavailable for account '%s': %s",
                         self.account, exc, exc_info=True)
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
        # Full system instruction is prepended ONLY on a fresh chat (no upstream
        # chat_id yet). Continuations ride on chat_id server-side, so re-sending
        # it would just waste context.
        system_instruction = kwargs.pop("system_instruction", None)
        project_id = kwargs.pop("project_id", None)
        db_history = kwargs.pop("db_history", None)
        layout_mode = kwargs.pop("layout_mode", None)

        if inject_instructions:
            try:
                _reminder = get_tool_format("kimi") or get_tool_format()
            except Exception:
                _reminder = ""
            if _reminder:
                prompt = f"{_reminder}\n\n{message}"

        session = await self._resolve_session()
        if session is None:
            yield self.ctx.error("ghost unavailable — check server logs (stale lock / Helium crash / CDP adopt failure)")
            return

        if not await session.has_token():
            yield self.ctx.error(f"not logged in on {self.account}")
            return

        upstream_chat = self._chat_ids.get(chat_id) if chat_id else None
        is_fresh = upstream_chat is None

        # First-message instruction injection — mirrors the DeepSeek connector.
        # On a fresh chat the full system instruction (persona + project + tools)
        # is prepended to the first user turn. A caller-supplied
        # ``system_instruction`` (agent sub-chats) wins over the built default.
        if is_fresh and inject_instructions:
            _instr: str | None = None
            if system_instruction:
                _instr = system_instruction
            else:
                try:
                    _instr = _load_instructions(project_id=project_id, layout_mode=layout_mode)
                except Exception as exc:
                    logger.warning("Kimi instruction build failed: %s", exc)
            if _instr:
                prompt = f"{_instr}\n\n{prompt}"

        history = self._sessions.get(chat_id, []) if chat_id else []
        # Seed local history from the DB on a fresh session (e.g. a cross-provider
        # switch) so context isn't lost. Never serialized when an upstream
        # chat_id exists — the server chains there instead.
        if db_history and chat_id and not history:
            history = [
                {"role": m["role"], "content": m["content"]}
                for m in db_history
                if m.get("role") in ("user", "assistant") and m.get("content")
            ]
            self._sessions[chat_id] = history
        # NOTE: we deliberately do NOT send message.parent_id. Empirically,
        # chat_id alone continues the conversation correctly (context preserved),
        # while chat_id + a reconstructed parent_id makes Kimi return an empty
        # stream. The UI tracks parent ids in a way we can't reproduce from the
        # event stream, so we rely on chat_id and let the server chain.
        file_ids = [str(fid) for fid in (ref_file_ids or []) if str(fid).strip()]

        yield {"type": "status", "message": "kimi_transport_ghost"}

        body = _build_body(
            prompt,
            model=api_model,
            thinking_enabled=thinking_enabled,
            effort=effort,
            chat_id=upstream_chat,
            history=history,
            file_ids=file_ids,
        )

        got_answer = False

        for attempt in range(2):
            # Fresh per-attempt parse state — a stale assistant_id from a
            # prior attempt would break the terminal-completion match.
            state: dict[str, Any] = {"chat_id": upstream_chat, "status": None}
            try:
                async with await session.open_stream(body, idle_secs=_IDLE_TIMEOUT_SECS) as resp:
                    if resp.status_code != 200:
                        yield self.ctx.error(f"HTTP {resp.status_code}", status_code=resp.status_code)
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
                            # Stash the live generating id so cancel() can target it.
                            if chat_id and state.get("assistant_id") and state.get("status") == "MESSAGE_STATUS_GENERATING":
                                self._generating[chat_id] = state["assistant_id"]
                            if ev["type"] == "done":
                                # Persist upstream chat id + parent id for continuation.
                                if chat_id and state.get("chat_id"):
                                    self._chat_ids[chat_id] = state["chat_id"]
                                if chat_id and state.get("parent_id"):
                                    self._parent_ids[chat_id] = state["parent_id"]
                                yield ev
                                return
                            if ev["type"] == "error":
                                yield ev
                                return
                            yield ev
                    # Stream closed without an explicit done. Kimi sometimes
                    # returns a COMPLETED-but-empty stream on continuation
                    # (~1 in 3), so an answerless close is retried once.
                    if not got_answer and attempt == 0:
                        logger.warning("Kimi empty stream — retrying once")
                        continue
                    if chat_id and state.get("chat_id"):
                        self._chat_ids[chat_id] = state["chat_id"]
                    yield {"type": "done", "chat_id": state.get("chat_id")}
                    return
            except asyncio.TimeoutError:
                if attempt == 0 and not got_answer:
                    logger.warning("Kimi stream idle — retrying once")
                    continue
                yield self.ctx.error("stream idle timeout")
                return
            except Exception as exc:
                yield self.ctx.error(f"stream failed: {exc}")
                return

    # -- files & cancel ------------------------------------------------

    async def upload_file(self, file_path: str, **_kw: Any) -> dict[str, Any]:
        """Upload one file through the ghost session. Returns file metadata."""
        from connectors.kimi.upload import upload_file as _up
        session = await self._resolve_session()
        if session is None:
            raise RuntimeError("kimi ghost transport unavailable")
        if not await session.has_token():
            raise RuntimeError(f"kimi not logged in on {self.account}")
        return await _up(session, file_path)

    async def cancel(self, chat_id: str) -> bool:
        """Stop generation for a Sable chat (maps to the UI stop button).

        Targets the LIVE generating assistant message id, stashed during the
        stream — cancelling the previous (completed) turn would be a no-op.
        """
        session = await self._resolve_session()
        if session is None:
            return False
        upstream = self._chat_ids.get(chat_id)
        msg_id = self._generating.get(chat_id) or self._parent_ids.get(chat_id)
        if not upstream or not msg_id:
            return False
        try:
            ok = await session.cancel_chat(upstream, msg_id)
            if ok:
                self._generating.pop(chat_id, None)
            return ok
        except Exception as exc:
            logger.warning("Kimi cancel failed (%s)", exc)
            return False

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
