"""BaseScraperConnector - turn any BaseBrowserEngine into a connector.

Provides the ConnectorProtocol surface (stream_chat / chat / is_available) on
top of a headed-browser engine, with per-account engine caching so several
accounts (and several scraper connectors) each get their own browser without
fighting over a profile lock.
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections.abc import AsyncGenerator
from typing import Any

from connectors.common.browser_scraper import BaseBrowserEngine
from engine.config import get_active_account, get_browser_data_dir

logger = logging.getLogger("sable.scraper_connector")


def _chat_url_of(engine: BaseBrowserEngine) -> str | None:
    """Best-effort current page URL for a browser engine."""
    page = getattr(engine, "page", None)
    if page is None:
        return None
    try:
        return page.url
    except Exception:
        return None


class BaseScraperConnector:
    """Common connector wrapper around a browser engine.

    Subclasses implement _build_engine() to return a configured engine for a
    given account. Everything else is shared.
    """

    backend_name: str = "scraper"
    provider_name: str = "scraper"

    def __init__(self, account: str | None = None) -> None:
        self._account = account
        self._engines: dict[str, BaseBrowserEngine] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._response_lock = asyncio.Lock()
        # account:chat_id -> last known upstream chat URL, so every round can
        # re-verify the session (chat_url is only passed on round 0).
        self._chat_urls: dict[str, str] = {}

    def _build_engine(
        self, account: str, browser_data_dir: str | None = None,
        *, adopt_from: "BaseBrowserEngine | None" = None,
    ) -> BaseBrowserEngine:
        raise NotImplementedError

    @property
    def account(self) -> str:
        # Each scraper connector tracks its own provider's active browser slot.
        return self._account or get_active_account(self.backend_name)

    def _engine_key(
        self, account: str, browser_data_dir: str | None
    ) -> tuple[str, str | None]:
        """Resolve (scope, effective_browser_data_dir) for an engine lookup.

        A stream that pins its own ``browser_data_dir`` (critique and agent
        sub-chats) gets a wholly separate engine: separate browser profile,
        separate page, and — critically — its own ``active_chat_id``. Without
        this the sub-chat and the parent chat take turns tripping the
        ``chat_id != active_chat_id`` check, each call firing ``new_chat()``
        which resets ``system_injected`` and replays the hello warmup forever.

        Safety: if the pinned dir resolves to the SAME profile the account
        already owns, isolation is impossible — engines launch with
        ``kill_existing=True``, so a second browser would kill the first. In
        that case we deliberately fall back to the shared engine; the
        ``_shared_parent`` guard in stream_chat() keeps it loop-free.
        """
        if not browser_data_dir:
            return account, None
        scope = os.path.basename(os.path.normpath(browser_data_dir)) or "scoped"
        if scope == account:
            # The pinned dir IS this account's own profile (e.g. critique wants
            # the deepseek_scraper account's live profile). We can't launch a
            # second browser on it (kill_existing + SingletonLock would kill the
            # parent), and we can't blindly share the parent's page either (the
            # two chats would fight over active_chat_id). So: attach a scoped
            # sub-engine to the parent's live browser and give it its OWN tab.
            # The sub-scope key keeps its page + active_chat_id isolated.
            logger.info(
                "[%s] pinned browser_data_dir %s matches account %s — "
                "sub-engine will share the parent browser on a new tab",
                self.backend_name, browser_data_dir, account,
            )
            return f"{account}::shared", browser_data_dir
        return f"{account}::{scope}", browser_data_dir

    def _lock_for(self, scope: str) -> asyncio.Lock:
        lock = self._locks.get(scope)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[scope] = lock
        return lock

    def _url_key(self, scope: str, chat_id: str | None) -> str:
        return scope + ":" + (chat_id or "")

    async def _get_engine(
        self, scope: str, account: str, browser_data_dir: str | None = None
    ) -> BaseBrowserEngine:
        engine = self._engines.get(scope)
        if engine is not None and await self._engine_alive(engine):
            return engine
        if engine is not None:
            await self._safe_cleanup(engine)
        # A `::shared` scope means: adopt the parent (base-account) engine's
        # live browser and open a new tab, instead of launching a second
        # browser on the same profile.
        if scope.endswith("::shared"):
            parent_scope = account
            parent = self._engines.get(parent_scope)
            if parent is None or not await self._engine_alive(parent):
                # Parent not up yet (sub-chat raced ahead). Bring it up first.
                if parent is not None:
                    await self._safe_cleanup(parent)
                parent = self._build_engine(account, None)
                await parent.launch()
                self._engines[parent_scope] = parent
                logger.info("[%s] parent engine up for scope %s (account %s, port %s)",
                            self.backend_name, parent_scope, account, parent.port)
            engine = self._build_engine(account, None, adopt_from=parent)
            await engine.launch()
            self._engines[scope] = engine
            logger.info("[%s] shared sub-engine up for scope %s on parent port %s",
                        self.backend_name, scope, engine.port)
            return engine
        engine = self._build_engine(account, browser_data_dir)
        await engine.launch()
        self._engines[scope] = engine
        logger.info("[%s] engine up for scope %s (account %s, port %s)",
                    self.backend_name, scope, account, engine.port)
        return engine

    async def _engine_alive(self, engine: BaseBrowserEngine) -> bool:
        # Adopted (shared) sub-engines ride the parent's port, so a live port
        # does NOT mean our tab is alive. Check the tab too.
        if getattr(engine, "_adopt_from", None) is not None:
            try:
                page = getattr(engine, "page", None)
                return page is not None and not page.is_closed()
            except Exception:
                return False
        try:
            return bool(await engine.is_port_open())
        except Exception:
            return False

    async def _safe_cleanup(self, engine: BaseBrowserEngine) -> None:
        try:
            await engine.cleanup()
        except Exception:
            pass

    @property
    def is_available(self) -> bool:
        try:
            return get_browser_data_dir(self.backend_name).is_dir()
        except Exception:
            return False

    async def stream_chat(
        self,
        message: str,
        *,
        model: str | None = None,
        thinking_mode: str | None = None,
        chat_id: str | None = None,
        ref_file_ids: list[str] | None = None,
        inject_instructions: bool = True,
        system_instruction: str | None = None,
        files: list[str] | None = None,
        chat_url: str | None = None,
        browser_data_dir: str | None = None,
        stateless: bool = False,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, Any], None]:
        account = self.account
        # A pinned browser_data_dir means this stream owns a private browser
        # profile (critique / agent sub-chats). Keying the engine by it gives
        # the sub-chat its own page + active_chat_id, so it can never
        # hard-reset the parent chat's upstream session.
        scope, browser_data_dir = self._engine_key(account, browser_data_dir)
        # Stateless call (e.g. the humanizer): a throwaway chat whose ONLY
        # instruction is the supplied system_instruction. No Maria/Sable
        # persona, no format primer, no shared session history.
        if stateless:
            if system_instruction:
                message = (
                    "[SYSTEM INSTRUCTION]\n" + system_instruction
                    + "\n\n[USER MESSAGE]\n" + message
                )
            # raw=True skips both the first-message instruction dump and the
            # per-turn quick reminder (engine.send_msg honours it below).
            inject_instructions = False
            chat_id = None
            chat_url = None
        elif system_instruction and "-agent-" in str(chat_id or ""):
            message = (
                "[SYSTEM INSTRUCTION]\n" + system_instruction
                + "\n\n[USER MESSAGE]\n" + message
            )

        try:
            async with self._lock_for(scope):
                engine = await self._get_engine(scope, account, browser_data_dir)

                _active = getattr(engine, "active_chat_id", None)
                _cid = str(chat_id or "")
                _is_sub = "-critique-" in _cid or "-agent-" in _cid
                _active_is_sub = "-critique-" in str(_active or "") or "-agent-" in str(_active or "")
                # Safety net: a sub-chat riding a shared parent engine must NEVER
                # call new_chat() — that is precisely what produced the
                # hello -> instruction -> hard reset loop. Leave the parent's
                # active_chat_id untouched and just send.
                _shared_parent = _is_sub and not browser_data_dir and bool(_active) and not _active_is_sub

                if chat_id and chat_id != _active:
                    if _shared_parent:
                        logger.warning(
                            "[%s] sub-chat %s on unscoped parent engine (active=%s) — "
                            "skipping session reset to avoid the new_chat loop",
                            self.backend_name, chat_id, _active,
                        )
                    else:
                        # Resume the stored upstream chat when we have its URL;
                        # otherwise start a fresh one. Resume keeps the upstream
                        # conversation (and its system prompt) intact.
                        resumed = False
                        if chat_url:
                            resume = getattr(engine, "resume_chat", None)
                            if resume is not None:
                                try:
                                    resumed = await resume(chat_url)
                                except Exception as exc:
                                    logger.warning("[%s] resume_chat failed: %s", self.backend_name, exc)
                        if not resumed:
                            new_chat = getattr(engine, "new_chat", None)
                            if new_chat is not None:
                                try:
                                    await new_chat()
                                except Exception as exc:
                                    logger.warning("[%s] new_chat failed: %s", self.backend_name, exc)
                        setattr(engine, "active_chat_id", chat_id)

                # Per-message session guard — runs on EVERY message (user turns
                # AND tool-loop rounds). chat_url is only passed on round 0, so
                # later rounds fall back to the URL remembered from the prior
                # round's done event. On any session mismatch the engine
                # navigates back to the expected chat BEFORE the send.
                if chat_id:
                    expected_url = chat_url or self._chat_urls.get(self._url_key(scope, chat_id))
                    if expected_url:
                        ensure = getattr(engine, "ensure_chat_url", None)
                        if ensure is not None:
                            try:
                                await ensure(expected_url)
                            except Exception as exc:
                                logger.warning("[%s] ensure_chat_url failed: %s", self.backend_name, exc)

                if thinking_mode in ("deepthink", "thinking", "fast"):
                    setter = getattr(engine, "set_thinking_mode", None)
                    if setter is not None:
                        try:
                            await setter(thinking_mode)
                        except Exception as exc:
                            logger.warning("[%s] thinking toggle failed: %s", self.backend_name, exc)

                for path in (files or []):
                    try:
                        ok = await engine.upload_file(path, has_msg=False)
                        msg = "browser_file_attached:" + path if ok else "browser_file_attach_failed:" + path
                        yield {"type": "status", "message": msg}
                    except Exception as exc:
                        yield {"type": "status", "message": "browser_file_attach_failed:" + str(exc)}

                # Anti-burst jitter (per-backend, configurable in Settings).
                # Keyed by backend_name (e.g. deepseek_scraper) so the scraper is
                # throttled independently of the API connector. Emit the status
                # BEFORE sleeping so the UI shows it first.
                try:
                    from connectors.common.jitter import jitter_for, sleep_jitter
                    _jdelay = jitter_for(self.backend_name)
                    if _jdelay:
                        yield {"type": "status", "message": f"{self.backend_name}_delay_{_jdelay:.1f}s"}
                        await sleep_jitter(_jdelay)
                except Exception as exc:
                    logger.debug("[%s] jitter skipped: %s", self.backend_name, exc)

                sent = await engine.send_msg(message, raw=not inject_instructions)
                if not sent:
                    yield {"type": "error", "message": "Browser scraper could not send the message"}
                    return

            async with self._response_lock:
                async for event in self._capture(engine):
                    yield event

            _done_url = _chat_url_of(engine)
            if _done_url and chat_id:
                self._chat_urls[self._url_key(scope, chat_id)] = _done_url
            yield {
                "type": "done",
                "chat_id": chat_id,
                "parent_id": "scraper-" + uuid.uuid4().hex,
                "chat_url": _done_url,
            }
        except asyncio.CancelledError:
            eng = self._engines.get(scope)
            if eng is not None:
                try:
                    await eng.stop_generation()
                except Exception:
                    pass
            raise
        except Exception as exc:
            logger.exception("[%s] stream failed", self.backend_name)
            yield {"type": "error", "message": type(exc).__name__ + ": " + str(exc)}

    async def _capture(self, engine: BaseBrowserEngine) -> AsyncGenerator[dict[str, Any], None]:
        answer_q: asyncio.Queue[str] = asyncio.Queue()
        think_q: asyncio.Queue[str] = asyncio.Queue()
        # Seed from the engine's PREVIOUS turn capture, not empty strings.
        # This generator is recreated every turn; if state starts empty the
        # first stale DOM read of the new turn passes the `!= state[...]` test
        # and gets streamed as fresh thinking/answer (cross-turn bleed).
        state = {
            "last_answer": getattr(engine, "_last_response_text", "") or "",
            "last_think": getattr(engine, "_last_thinking", "") or "",
        }

        def _delta(full: str, prev: str) -> str:
            if full.startswith(prev):
                return full[len(prev):]
            if prev.startswith(full):
                return ""
            common = 0
            m = min(len(prev), len(full))
            while common < m and prev[common] == full[common]:
                common += 1
            return full[common:] if common > len(prev) // 2 else ""

        def live_display(full_text: str) -> None:
            full = str(full_text or "")
            if not full or full == state["last_answer"]:
                return
            delta = _delta(full, state["last_answer"])
            state["last_answer"] = full
            if delta:
                answer_q.put_nowait(delta)

        async def thoughts_callback(full_text: str) -> None:
            full = str(full_text or "")
            if not full or full == state["last_think"]:
                return
            delta = _delta(full, state["last_think"])
            state["last_think"] = full
            if delta:
                think_q.put_nowait(delta)

        yield {"type": "status", "message": "waiting_for_browser_response"}
        task = asyncio.create_task(
            engine.get_response(live_display=live_display, thoughts_callback=thoughts_callback)
        )

        while not task.done():
            yielded = False
            while not think_q.empty():
                yield {"type": "thinking", "text": think_q.get_nowait()}
                yielded = True
            try:
                yield {"type": "answer", "text": await asyncio.wait_for(answer_q.get(), timeout=0.15)}
                yielded = True
            except asyncio.TimeoutError:
                pass
            if not yielded:
                await asyncio.sleep(0.05)

        while not think_q.empty():
            yield {"type": "thinking", "text": think_q.get_nowait()}
        while not answer_q.empty():
            yield {"type": "answer", "text": answer_q.get_nowait()}

        answer = str(await task or "")
        tail = _delta(answer, state["last_answer"])
        if tail:
            yield {"type": "answer", "text": tail}
        elif answer and not state["last_answer"]:
            yield {"type": "answer", "text": answer}

    async def chat(self, message: str, **kwargs: Any) -> dict[str, Any]:
        thinking: list[str] = []
        answer: list[str] = []
        chat_id = kwargs.get("chat_id")
        parent_id: str | None = None
        chat_url: str | None = None
        error: str | None = None

        async for event in self.stream_chat(message, **kwargs):
            etype = event.get("type")
            if etype == "thinking":
                thinking.append(str(event.get("text", "")))
            elif etype == "answer":
                answer.append(str(event.get("text", "")))
            elif etype == "done":
                parent_id = event.get("parent_id") or parent_id
                chat_url = event.get("chat_url") or chat_url
            elif etype == "error":
                error = str(event.get("message", ""))

        return {
            "chat_id": chat_id,
            "parent_id": parent_id,
            "chat_url": chat_url,
            "thinking": "".join(thinking),
            "answer": "".join(answer),
            "error": error,
        }

    async def cancel(self) -> bool:
        # Cancels every engine scoped to this account — the parent chat's engine
        # AND any isolated sub-chat engines (critique/agent) sharing the prefix.
        account = self.account
        engines = [
            e for k, e in self._engines.items()
            if k == account or k.startswith(account + "::")
        ]
        if not engines:
            return False
        cancelled = False
        for engine in engines:
            try:
                if await engine.stop_generation():
                    cancelled = True
            except Exception:
                pass
        return cancelled

    async def shutdown(self) -> None:
        for engine in list(self._engines.values()):
            await self._safe_cleanup(engine)
        self._engines.clear()
