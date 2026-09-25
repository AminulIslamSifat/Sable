"""BaseScraperConnector - turn any BaseBrowserEngine into a connector.

Provides the ConnectorProtocol surface (stream_chat / chat / is_available) on
top of a headed-browser engine, with per-account engine caching so several
accounts (and several scraper connectors) each get their own browser without
fighting over a profile lock.
"""
from __future__ import annotations

import asyncio
import logging
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

    def _build_engine(self, account: str) -> BaseBrowserEngine:
        raise NotImplementedError

    @property
    def account(self) -> str:
        return self._account or get_active_account()

    def _lock_for(self, account: str) -> asyncio.Lock:
        lock = self._locks.get(account)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[account] = lock
        return lock

    async def _get_engine(self, account: str) -> BaseBrowserEngine:
        engine = self._engines.get(account)
        if engine is not None and await self._engine_alive(engine):
            return engine
        if engine is not None:
            await self._safe_cleanup(engine)
        engine = self._build_engine(account)
        await engine.launch()
        self._engines[account] = engine
        logger.info("[%s] engine up for account %s (port %s)",
                    self.backend_name, account, engine.port)
        return engine

    async def _engine_alive(self, engine: BaseBrowserEngine) -> bool:
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
            return get_browser_data_dir().is_dir()
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
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, Any], None]:
        account = self.account
        if system_instruction and "-agent-" in str(chat_id or ""):
            message = (
                "[SYSTEM INSTRUCTION]\n" + system_instruction
                + "\n\n[USER MESSAGE]\n" + message
            )

        try:
            async with self._lock_for(account):
                engine = await self._get_engine(account)

                if chat_id and chat_id != getattr(engine, "active_chat_id", None):
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

            yield {
                "type": "done",
                "chat_id": chat_id,
                "parent_id": "scraper-" + uuid.uuid4().hex,
                "chat_url": _chat_url_of(engine),
            }
        except asyncio.CancelledError:
            eng = self._engines.get(account)
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
        state = {"last_answer": "", "last_think": ""}

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
        engine = self._engines.get(self.account)
        if engine is None:
            return False
        try:
            return await engine.stop_generation()
        except Exception:
            return False

    async def shutdown(self) -> None:
        for engine in list(self._engines.values()):
            await self._safe_cleanup(engine)
        self._engines.clear()
