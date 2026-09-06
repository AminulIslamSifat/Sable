"""ScraperEngine: streaming chat via browser automation."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from .lifecycle import ScraperLifecycle
from .loader import _accepts_arg
from .settings import _load_settings, DEFAULT_ENGINE_TYPE

logger = logging.getLogger("sable.scraper")

# Timeout for acquiring the setup lock — prevents silent hangs when a
# previous stream is stuck or leaked its lock.
_LOCK_ACQUIRE_TIMEOUT = 30.0


class ScraperEngine(ScraperLifecycle):
    """Singleton-ish adapter around a GhostChat-style browser scraper.

    Inherits lifecycle management (start/stop/probe/kill) from ScraperLifecycle
    and adds the streaming chat interface.
    """

    def __init__(self) -> None:
        super().__init__()
        # Serializes response capture so two concurrent streams don't
        # interleave DOM reads.  Setup lock (_lock) is released before
        # this phase so other chats can send while one response streams.
        self._response_lock = asyncio.Lock()

    async def _interrupt_generation(self, engine: Any, chat_id: str | None = None, response_id: str | None = None) -> None:
        """Stop generation via API (Qwen) or on-page stop button."""
        stop = getattr(engine, "stop_generation", None)
        if stop is None:
            return
        try:
            import inspect
            sig = inspect.signature(stop)
            if "chat_id" in sig.parameters:
                if await stop(chat_id=chat_id, response_id=response_id):
                    logger.info("Browser generation stopped via API/button")
            else:
                if await stop():
                    logger.info("Browser generation stopped via on-page stop button")
        except Exception as exc:
            logger.warning("Could not stop browser generation: %s", exc)

    async def _stream_get_response(
        self,
        engine: Any,
        response_kwargs: dict[str, Any],
        state: dict[str, Any],
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Yield live thought + answer deltas while the browser engine captures text."""
        if not _accepts_arg(engine.get_response, 'live_display'):
            answer = await engine.get_response(**response_kwargs)
            state['answer'] = str(answer or '')
            state['streamed'] = False
            return

        answer_queue: asyncio.Queue[str] = asyncio.Queue()
        thought_queue: asyncio.Queue[str] = asyncio.Queue()
        last_answer = ''
        last_thought = ''

        def _compute_delta(full: str, prev: str) -> str:
            """Compute new content delta between previous and current full text.

            Handles three cases:
            1. Normal growth: full starts with prev → emit tail
            2. Text shrunk (_clean_garbage dedup): full is a prefix of prev → no delta
            3. Mid-text reformat: use longest common prefix to find new tail
            """
            if full.startswith(prev):
                return full[len(prev):]
            # Text got shorter (e.g. _clean_garbage removed duplicate halves)
            # or was reformatted — don't re-emit old content
            if prev.startswith(full):
                return ''
            # Find longest common prefix
            min_len = min(len(prev), len(full))
            common = 0
            while common < min_len and prev[common] == full[common]:
                common += 1
            # Only emit genuinely new content beyond the shared prefix
            if common > len(prev) // 2 and len(full) > common:
                return full[common:]
            # Too little overlap — likely a full replacement, not incremental
            return ''

        def live_display(full_text: str) -> None:
            nonlocal last_answer
            full = str(full_text or '')
            if not full or full == last_answer:
                return
            delta = _compute_delta(full, last_answer)
            last_answer = full
            if delta:
                import logging as _logging
                _logging.getLogger("sable.scraper.stream").debug(
                    "live_display delta: len=%d full_len=%d prev_len=%d preview=%r",
                    len(delta), len(full), len(full) - len(delta), delta[:80]
                )
                answer_queue.put_nowait(delta)

        async def thoughts_callback(full_text: str) -> None:
            nonlocal last_thought
            full = str(full_text or '')
            if not full or full == last_thought:
                return
            delta = _compute_delta(full, last_thought)
            last_thought = full
            if delta:
                thought_queue.put_nowait(delta)

        kwargs = dict(response_kwargs)
        kwargs['live_display'] = live_display
        if _accepts_arg(engine.get_response, 'thoughts_callback'):
            kwargs['thoughts_callback'] = thoughts_callback
        task = asyncio.create_task(engine.get_response(**kwargs))

        while not task.done():
            yielded = False
            while not thought_queue.empty():
                delta = thought_queue.get_nowait()
                state['streamed_thoughts'] = True
                yield {'type': 'thinking', 'text': delta}
                yielded = True
            try:
                delta = await asyncio.wait_for(answer_queue.get(), timeout=0.15)
                state['streamed'] = True
                yield {'type': 'answer', 'text': delta}
                yielded = True
            except asyncio.TimeoutError:
                pass
            if not yielded:
                continue

        # Drain remaining thoughts
        while not thought_queue.empty():
            delta = thought_queue.get_nowait()
            state['streamed_thoughts'] = True
            yield {'type': 'thinking', 'text': delta}

        # Drain remaining answer
        while not answer_queue.empty():
            delta = answer_queue.get_nowait()
            state['streamed'] = True
            yield {'type': 'answer', 'text': delta}

        answer = await task
        answer_text = str(answer or '')
        state['answer'] = answer_text

        if answer_text:
            tail = _compute_delta(answer_text, last_answer)
            if not tail and not state.get('streamed'):
                # Nothing streamed yet — use full answer as fallback
                tail = answer_text
            if tail:
                import logging as _logging
                _logging.getLogger("sable.scraper.stream").debug(
                    "final_flush tail: len=%d answer_len=%d last_answer_len=%d streamed=%s preview=%r",
                    len(tail), len(answer_text), len(last_answer), state.get('streamed'), tail[:80]
                )
                state['streamed'] = True
                yield {'type': 'answer', 'text': tail}

    async def stream_events(
        self,
        message: str,
        chat_id: str | None = None,
        parent_id: str | None = None,
        files: list[dict[str, Any]] | None = None,
        model: str | None = None,
        thinking_mode: str | None = None,
        chat_url: str | None = None,
        raw: bool = False,
    ) -> AsyncGenerator[dict[str, Any], None]:
        settings = _load_settings()
        if not settings.get("enabled"):
            yield {"type": "error", "message": "Browser scraper is disabled"}
            return

        # ── Phase 1: Setup (locked) ──────────────────────────────────────
        # Engine init, browser liveness, new_chat, file upload, send_msg.
        # Lock is released BEFORE the long-running get_response so other
        # requests don't block silently.
        engine: Any = None
        initial_count = 0
        setup_ok = False

        try:
            async with asyncio.timeout(_LOCK_ACQUIRE_TIMEOUT):
                async with self._lock:
                    yield {"type": "status", "message": "browser_scraper_starting"}
                    engine = await self._ensure_engine(settings)
                    yield {"type": "status", "message": "browser_scraper_connected"}

                    if not await self._is_browser_alive(engine):
                        logger.warning("Browser process gone, restarting engine")
                        self.engine = None
                        self.loaded_path = None
                        engine = await self._ensure_engine(settings)
                        yield {"type": "status", "message": "browser_scraper_reconnected"}

                    if model in ("default", "expert", "vision") and hasattr(engine, "current_model_type"):
                        engine.current_model_type = model

                    if chat_id and chat_id != self.active_chat_id:
                        if getattr(engine, "has_fresh_chat", False):
                            engine.has_fresh_chat = False
                        elif chat_url:
                            page = getattr(engine, "page", None)
                            if page is not None:
                                try:
                                    current = page.url
                                    if current != chat_url:
                                        await page.goto(chat_url, wait_until="domcontentloaded", timeout=15000)
                                        await asyncio.sleep(2)
                                        yield {"type": "status", "message": "browser_resumed_chat"}
                                except Exception as exc:
                                    yield {
                                        "type": "status",
                                        "message": f"browser_resume_failed: {exc}",
                                    }
                        else:
                            new_chat = getattr(engine, "new_chat", None)
                            if new_chat is not None:
                                try:
                                    await new_chat()
                                except Exception as exc:
                                    yield {
                                        "type": "status",
                                        "message": f"browser_new_chat_failed: {exc}",
                                    }
                        self.active_chat_id = chat_id

                    get_response_count = getattr(engine, "get_response_count", None)
                    if get_response_count is not None:
                        try:
                            initial_count = int(await get_response_count())
                        except Exception:
                            initial_count = 0

                    if files:
                        upload_file = getattr(engine, "upload_file", None)
                        if upload_file is not None:
                            for file_entry in files:
                                fpath = file_entry.get("path") or file_entry.get("local_path")
                                if not fpath:
                                    continue
                                try:
                                    await upload_file(fpath, has_msg=False)
                                    yield {
                                        "type": "status",
                                        "message": f"browser_file_attached:{Path(fpath).name}",
                                    }
                                except Exception as exc:
                                    yield {
                                        "type": "status",
                                        "message": f"browser_file_attach_failed:{exc}",
                                    }

                    if thinking_mode in ("deepthink", "fast"):
                        set_thinking = getattr(engine, "set_thinking_mode", None)
                        if set_thinking is not None:
                            try:
                                await set_thinking(thinking_mode)
                            except Exception as exc:
                                yield {
                                    "type": "status",
                                    "message": f"browser_thinking_mode_failed: {exc}",
                                }

                    send_kwargs: dict[str, Any] = {}
                    if _accepts_arg(engine.send_msg, 'raw'):
                        send_kwargs['raw'] = raw
                    sent = await engine.send_msg(message, **send_kwargs)
                    if not sent:
                        yield {
                            "type": "error",
                            "message": "Browser scraper could not send the message",
                        }
                        return
                    setup_ok = True

                    # Diagnostics: heartbeat on successful send
                    try:
                        from .diagnostics import get_monitor
                        diag_sid = getattr(self, '_diag_session_id', None)
                        if diag_sid:
                            await get_monitor().heartbeat(diag_sid)
                    except Exception:
                        pass
        except TimeoutError:
            logger.error("Scraper setup lock timed out after %ss — previous stream may be stuck", _LOCK_ACQUIRE_TIMEOUT)
            yield {
                "type": "error",
                "message": f"Scraper busy: could not acquire lock within {_LOCK_ACQUIRE_TIMEOUT}s. Try stopping the current generation first.",
            }
            return
        except (asyncio.CancelledError, GeneratorExit):
            if engine is not None:
                await self._interrupt_generation(engine, chat_id=chat_id, response_id=parent_id)
            raise
        except SystemExit as exc:
            yield {"type": "error", "message": f"Browser engine exited unexpectedly: {exc}"}
            return
        except Exception as exc:
            logger.exception("Browser scraper setup failed")
            yield {"type": "error", "message": f"{type(exc).__name__}: {exc}"}
            return

        if not setup_ok:
            return

        # ── Phase 2: Response capture (unlocked) ─────────────────────────
        # The lock is released so other chats can queue up their send_msg
        # while this response streams. Only one response capture runs at a
        # time via _response_lock to avoid DOM read interleaving.
        yield {"type": "status", "message": "waiting_for_browser_response"}

        response_kwargs: dict[str, Any] = {}
        if _accepts_arg(engine.get_response, "initial_count"):
            response_kwargs["initial_count"] = initial_count

        try:
            async with self._response_lock:
                scraper_state: dict[str, Any] = {}
                async for event in self._stream_get_response(engine, response_kwargs, scraper_state):
                    yield event
                answer = str(scraper_state.get('answer', ''))
                streamed_answer = bool(scraper_state.get('streamed', False))

                answer_text = str(answer or "")
                if not streamed_answer:
                    if answer_text:
                        yield {"type": "answer", "text": answer_text}
                    else:
                        yield {
                            "type": "status",
                            "message": "browser_scraper_empty_response",
                        }

            new_parent = f"browser-{uuid.uuid4().hex}"
            chat_url_result = None
            try:
                page = getattr(engine, "page", None)
                if page is not None:
                    chat_url_result = page.url
            except Exception:
                pass
            yield {
                "type": "done",
                "chat_id": chat_id,
                "parent_id": new_parent,
                "chat_url": chat_url_result,
            }
        except (asyncio.CancelledError, GeneratorExit):
            if engine is not None:
                await self._interrupt_generation(engine, chat_id=chat_id, response_id=parent_id)
            raise
        except SystemExit as exc:
            yield {"type": "error", "message": f"Browser engine exited unexpectedly: {exc}"}
        except Exception as exc:
            logger.exception("Browser scraper response capture failed")
            yield {"type": "error", "message": f"{type(exc).__name__}: {exc}"}

    async def chat(
        self,
        message: str,
        chat_id: str | None = None,
        parent_id: str | None = None,
        files: list[dict[str, Any]] | None = None,
        model: str | None = None,
        thinking_mode: str | None = None,
        chat_url: str | None = None,
        raw: bool = False,
    ) -> dict[str, Any]:
        thinking_parts: list[str] = []
        answer_parts: list[str] = []
        tool_events: list[dict[str, Any]] = []
        final_chat_id = chat_id
        final_parent_id = parent_id
        final_chat_url: str | None = None
        error: str | None = None

        async for event in self.stream_events(
            message=message,
            chat_id=chat_id,
            parent_id=parent_id,
            files=files,
            model=model,
            thinking_mode=thinking_mode,
            chat_url=chat_url,
            raw=raw,
        ):
            event_type = event.get("type")
            if event_type == "thinking":
                thinking_parts.append(str(event.get("text", "")))
            elif event_type == "answer":
                answer_parts.append(str(event.get("text", "")))
            elif event_type in ("tool_call", "tool_result"):
                tool_events.append(event)
            elif event_type == "done":
                final_chat_id = event.get("chat_id") or final_chat_id
                final_parent_id = event.get("parent_id") or final_parent_id
                final_chat_url = event.get("chat_url") or final_chat_url
            elif event_type == "error":
                error = str(event.get("message", "Unknown scraper error"))

        return {
            "chat_id": final_chat_id,
            "parent_id": final_parent_id,
            "chat_url": final_chat_url,
            "thinking": "".join(thinking_parts),
            "answer": "".join(answer_parts),
            "tool_events": tool_events,
            "error": error,
        }


# Module-level singleton
scraper = ScraperEngine()
