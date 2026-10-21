"""BaseScraperConnector - turn any BaseBrowserEngine into a connector.

Provides the ConnectorProtocol surface (stream_chat / chat / is_available) on
top of a headed-browser engine, with per-account engine caching so several
accounts (and several scraper connectors) each get their own browser without
fighting over a profile lock.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import uuid
from collections.abc import AsyncGenerator
from typing import Any

from connectors.common import browser_errors
from connectors.common.browser_errors import BrowserError
from connectors.common.browser_scraper import BaseBrowserEngine
from engine.config import get_active_account, get_browser_data_dir

logger = logging.getLogger("sable.scraper_connector")

# How many times a turn may restart the browser before giving up. Three covers
# the realistic transient cases (dead process, lost context, stalled send)
# without hanging the chat on a genuinely broken profile.
_RECOVERY_ATTEMPTS = 3


class _ThoughtOnlyRetry(Exception):
    """Internal signal: re-capture on the SAME browser, never tear it down.

    A thought-only turn (the model streamed a full chain of thought and then
    stopped with no answer text) is not a browser fault. The page is alive and
    mid-conversation, and killing + relaunching it cannot conjure an answer
    upstream never sent. This exception routes the retry to the no-teardown
    path in ``stream_chat``.

    Never escapes the module — ``stream_chat`` catches it before the generic
    ``except Exception``.
    """


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

    async def _recover_engine(self, scope: str, *, keep_chat_urls: bool = True) -> None:
        """Tear down a wedged engine so the next _get_engine() rebuilds it.

        This is the single restart primitive for every recoverable browser
        failure. It (1) drops the engine from the cache, (2) runs its cleanup
        which kills the browser process tree and releases the governor slot,
        (3) purges any leftover process still holding the profile, and (4)
        reclaims slots keyed to that profile.

        Adopted (``::shared``) sub-engines ride the parent's Chromium, so their
        cleanup only closes their own tab — the profile purge and slot reclaim
        are skipped, otherwise we would murder the parent chat's browser.

        ``_chat_urls`` is deliberately preserved so a capture-phase retry can
        navigate back to the SAME upstream conversation instead of starting a
        fresh chat and losing the in-flight answer.

        Never raises: if recovery itself fails the caller's next attempt simply
        hits the same error and the loop exhausts normally.
        """
        engine = self._engines.pop(scope, None)
        adopted = getattr(engine, "_adopt_from", None) is not None if engine else False
        profile_dir = getattr(engine, "user_data_dir", "") if engine else ""

        if engine is not None:
            try:
                await engine.cleanup()
            except Exception:
                pass

        if not adopted and profile_dir:
            # Sweep stragglers: a half-dead Chromium still holding the profile
            # would win the SingletonLock on relaunch and we would loop.
            try:
                from engine.process_utils import purge_profile_browsers
                purge_profile_browsers(profile_dir)
            except Exception:
                pass
            try:
                from engine.browser_governor import release_slots_for_profile
                release_slots_for_profile(profile_dir)
            except Exception:
                pass

        if not keep_chat_urls:
            for key in [k for k in self._chat_urls if k.startswith(scope + ":")]:
                self._chat_urls.pop(key, None)

        # Let the OS release the profile lock and the governor reap the slot
        # before we immediately try to claim a new one.
        await asyncio.sleep(1.5)
        logger.info("[%s] engine %s torn down for recovery", self.backend_name, scope)

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
            # A stateless call (humanizer) owns NO upstream session, so its
            # directive has to ride the message itself; raw=True skips the
            # once-only injection + per-turn reminder in the engine.
            if system_instruction:
                message = (
                    "[SYSTEM INSTRUCTION]\n" + system_instruction
                    + "\n\n[USER MESSAGE]\n" + message
                )
            inject_instructions = False
            chat_id = None
            chat_url = None
        # Everything else (main chat, study, agent sub-chats) hands the
        # directive to the engine below, which injects it EXACTLY ONCE via its
        # system_injected flag. Prepending here re-sent the whole instruction on
        # every turn — the study/agent bug this connector used to have.

        # Every recoverable browser failure (dead process, closed context,
        # lost session, stalled send, empty capture) restarts the browser and
        # retries. The turn is only ever SENT once — see _sent_upstream.
        #
        # _sent_upstream is the duplicate guard: once the message has reached
        # the SPA, a retry must NOT send it again (the model would answer
        # twice). Capture-phase failures therefore retry capture only, after
        # navigating back to the same upstream conversation.
        _sent_upstream = False
        # Set by the _ThoughtOnlyRetry handler: the turn already reached
        # upstream, so skip session-reset / re-navigation / file re-upload /
        # jitter and go straight back to capturing on the SAME live browser.
        # Re-navigating would RELOAD the page and wipe the completed turn's
        # DOM; re-uploading would attach the files twice.
        _capture_only = False
        # Bound before the try so the _ThoughtOnlyRetry handler can reference it
        # even if _get_engine() itself is what raised.
        engine: BaseBrowserEngine | None = None
        for _attempt in range(_RECOVERY_ATTEMPTS):
          try:
            async with self._lock_for(scope):
                engine = await self._get_engine(scope, account, browser_data_dir)
                # Instruction source for THIS stream. When the caller hands us a
                # built directive (study/agent), the engine injects it ONCE via
                # its system_injected flag instead of re-pasting it every turn.
                # layout_mode lets the engine build the right directive itself
                # when none was passed (study -> study tool set + directive).
                try:
                    engine._override_instructions = system_instruction or None
                    engine._layout_mode = kwargs.get("layout_mode")
                except Exception:
                    pass

                _active = getattr(engine, "active_chat_id", None)
                _cid = str(chat_id or "")
                _is_sub = "-critique-" in _cid or "-agent-" in _cid
                _active_is_sub = "-critique-" in str(_active or "") or "-agent-" in str(_active or "")
                # Safety net: a sub-chat riding a shared parent engine must NEVER
                # call new_chat() — that is precisely what produced the
                # hello -> instruction -> hard reset loop. Leave the parent's
                # active_chat_id untouched and just send.
                _shared_parent = _is_sub and not browser_data_dir and bool(_active) and not _active_is_sub

                if not _capture_only and chat_id and chat_id != _active:
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
                if not _capture_only and chat_id:
                    expected_url = chat_url or self._chat_urls.get(self._url_key(scope, chat_id))
                    if expected_url:
                        ensure = getattr(engine, "ensure_chat_url", None)
                        if ensure is not None:
                            ok = True
                            try:
                                ok = await ensure(expected_url)
                            except Exception as exc:
                                ok = False
                                logger.warning("[%s] ensure_chat_url failed: %s", self.backend_name, exc)
                            # The upstream chat is gone AND we landed on a login
                            # wall: no restart fixes this, the account needs to
                            # sign in again. Raise so the caller reports it as a
                            # needs-user error instead of retrying forever.
                            if ok is False:
                                check = getattr(engine, "looks_logged_out", None)
                                if callable(check) and check():
                                    raise BrowserError(
                                        browser_errors.NOT_LOGGED_IN,
                                        f"browser profile is logged out (wanted {expected_url})",
                                    )
                                raise BrowserError(
                                    browser_errors.SESSION_NOT_FOUND,
                                    f"upstream chat session unavailable: {expected_url}",
                                )

                if not _capture_only and thinking_mode in ("deepthink", "thinking", "fast"):
                    setter = getattr(engine, "set_thinking_mode", None)
                    if setter is not None:
                        try:
                            await setter(thinking_mode)
                        except Exception as exc:
                            logger.warning("[%s] thinking toggle failed: %s", self.backend_name, exc)

                if not _capture_only:
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
                    _jdelay = 0 if _capture_only else jitter_for(self.backend_name)
                    if _jdelay:
                        yield {"type": "status", "message": f"{self.backend_name}_delay_{_jdelay:.1f}s"}
                        await sleep_jitter(_jdelay)
                except Exception as exc:
                    logger.debug("[%s] jitter skipped: %s", self.backend_name, exc)

                if not _sent_upstream and not _capture_only:
                    sent = await engine.send_msg(message, raw=not inject_instructions)
                    if not sent:
                        # A dead page or a stale composer is recoverable — raise
                        # so the retry loop restarts the browser instead of
                        # ending the turn on the spot.
                        raise BrowserError(
                            browser_errors.SEND_FAILED,
                            "browser could not send the message",
                        )
                    _sent_upstream = True

            _cap: dict[str, Any] = {}
            async with self._response_lock:
                async for event in self._capture(engine, _cap):
                    yield event

            if _cap.get("empty"):
                # The turn DID reach upstream but produced no text. Re-sending
                # would duplicate the message, so _sent_upstream stays True and
                # the retry only re-captures from the resumed conversation.
                #
                # Thought-only turn: the model streamed a full chain of thought
                # and then stopped with no answer text. The browser is alive,
                # the page is exactly where we left it, and a relaunch cannot
                # conjure an answer that upstream never sent — so flag the
                # failure as NO-TEARDOWN and re-capture on this same engine.
                if _cap.get("reasoning_seen"):
                    # Do NOT tear the browser down — see _ThoughtOnlyRetry.
                    raise _ThoughtOnlyRetry(
                        "thought-only turn: reasoning streamed, no answer text",
                    )
                raise BrowserError(
                    browser_errors.CAPTURE_EMPTY,
                    "response stream produced no text",
                )

            _done_url = _chat_url_of(engine)
            if _done_url and chat_id:
                self._chat_urls[self._url_key(scope, chat_id)] = _done_url
            yield {
                "type": "done",
                "chat_id": chat_id,
                "parent_id": "scraper-" + uuid.uuid4().hex,
                "chat_url": _done_url,
            }
            return
          except _ThoughtOnlyRetry as exc:
            # Thought-only turn: reasoning streamed, no answer text. The
            # browser is healthy and holds the live upstream conversation, so
            # do NOT run _recover_engine (which kills the process tree and
            # relaunches). Reset the SSE buffer so the re-capture does not
            # replay this turn's stale chunks, then retry on the same engine.
            logger.info(
                "[%s] thought-only turn (attempt %d/%d) — re-capturing on the live browser: %s",
                self.backend_name, _attempt + 1, _RECOVERY_ATTEMPTS, exc,
            )
            # Prefer the engine this turn actually used (still bound from the
            # try block); fall back to the cache lookup.
            _eng = engine if engine is not None else self._engines.get(scope)
            if _eng is not None:
                _reset = getattr(_eng, "_reset_sse_capture", None)
                if callable(_reset):
                    try:
                        _reset()
                    except Exception as _re:
                        logger.debug("[%s] sse buffer reset failed: %s", self.backend_name, _re)
            # Preserve the upstream URL so the re-capture resumes the SAME
            # conversation instead of opening a fresh chat.
            _u = _chat_url_of(_eng) if _eng is not None else None
            if _u and chat_id:
                self._chat_urls[self._url_key(scope, chat_id)] = _u
            if _attempt + 1 >= _RECOVERY_ATTEMPTS:
                yield {
                    "type": "error",
                    "message": f"{browser_errors.CAPTURE_EMPTY}: {browser_errors.describe(browser_errors.CAPTURE_EMPTY)}",
                    "code": browser_errors.CAPTURE_EMPTY,
                    "needs_user": False,
                }
                return
            yield {
                "type": "status",
                "message": f"thought_only_recapture:attempt{_attempt + 2}",
            }
            # Next iteration must re-capture ONLY: skip session reset,
            # navigation, file upload and jitter (all guarded on _capture_only).
            _capture_only = True
            # Brief settle so the SPA finishes painting the completed turn
            # before we start reading the DOM again.
            await asyncio.sleep(0.5)
            continue
          except asyncio.CancelledError:
            eng = self._engines.get(scope)
            if eng is not None:
                try:
                    await eng.stop_generation()
                except Exception:
                    pass
            raise
          except Exception as exc:
            code = browser_errors.classify(exc)
            logger.warning(
                "[%s] turn failed (attempt %d/%d, code=%s): %s",
                self.backend_name, _attempt + 1, _RECOVERY_ATTEMPTS, code, exc,
            )
            # Rate limit is NOT a browser fault. Relaunching the browser and
            # re-sending the same message to a limiter that just said "too
            # frequent" cannot succeed, and every retry re-hits the locked
            # endpoint. Surface it as `rate_limited` so the chat route can
            # switch accounts (its existing auto-switch path) instead of this
            # loop burning 3 browser restarts and then reporting a bogus
            # capture_empty.
            if code == browser_errors.RATE_LIMITED:
                _rl_msg = str(getattr(exc, "message", "") or exc or "").strip()
                if not _rl_msg:
                    _rl_msg = browser_errors.describe(code)
                logger.warning(
                    "[%s] rate limited — NOT retrying, surfacing rate_limited: %s",
                    self.backend_name, _rl_msg[:160],
                )
                yield {
                    "type": "rate_limited",
                    "message": _rl_msg,
                    "hours": "?",
                    "provider": self.provider_name,
                }
                return
            last = _attempt + 1 >= _RECOVERY_ATTEMPTS
            if last or code in browser_errors.NEEDS_USER or code not in browser_errors.RECOVERABLE:
                if not (code in browser_errors.NEEDS_USER or code not in browser_errors.RECOVERABLE):
                    logger.exception("[%s] stream failed after %d attempts", self.backend_name, _RECOVERY_ATTEMPTS)
                yield {
                    "type": "error",
                    "message": f"{code}: {browser_errors.describe(code)}",
                    "code": code,
                    "needs_user": code in browser_errors.NEEDS_USER,
                }
                return
            yield {
                "type": "status",
                "message": f"browser_recovering:{code}:attempt{_attempt + 2}",
            }
            # Preserve where the browser actually ended up. The SPA navigates to
            # /a/chat/s/<id> the moment a response starts, so remembering it lets
            # a capture-phase retry resume the SAME upstream conversation
            # instead of opening a fresh chat and losing the answer.
            _eng = self._engines.get(scope)
            _u = _chat_url_of(_eng) if _eng is not None else None
            if _u and chat_id:
                self._chat_urls[self._url_key(scope, chat_id)] = _u
            await self._recover_engine(scope)
            continue

    async def _capture(
        self,
        engine: BaseBrowserEngine,
        capture_out: dict[str, Any] | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
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

        # The capture task MUST die with this generator. If the frontend aborts
        # the SSE stream (Stop button), this generator is closed at the `yield`
        # below and the `while not task.done()` loop never resumes — leaving the
        # task alive as an orphan. That orphan keeps polling the SPA and, worse,
        # keeps draining engine._sse_chunks with a stale index, so the NEXT
        # turn's chunks get split between the orphan and the real capture and
        # the new answer comes back truncated or empty.
        try:
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
        except (asyncio.CancelledError, GeneratorExit):
            # Abort path: kill the in-flight capture so it stops touching the
            # page and the chunk buffer. Best-effort — never mask the original
            # exception.
            if not task.done():
                task.cancel()
            with contextlib.suppress(BaseException):
                await task
            raise
        finally:
            if not task.done():
                task.cancel()

        tail = _delta(answer, state["last_answer"])
        if tail:
            yield {"type": "answer", "text": tail}
        elif answer and not state["last_answer"]:
            yield {"type": "answer", "text": answer}

        # Report the final text so stream_chat can distinguish "the model
        # replied nothing" from "we failed to capture what it said".
        if capture_out is not None:
            capture_out["answer"] = answer
            capture_out["empty"] = not str(answer or "").strip()
            # A thought-only turn produced reasoning but no answer text. The
            # browser is alive and mid-conversation — stream_chat must NOT kill
            # it; it retries the capture on the same browser instead.
            capture_out["reasoning_seen"] = bool(getattr(engine, "_last_reasoning_seen", False))

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

    async def get_ui_metadata(self) -> dict[str, Any]:
        """Live UI metadata for this backend (models, thinking modes, active).

        Unlike the legacy engine/scraper_engines path — where this lived on the
        engine and was invoked synchronously — connector engines are async and
        the model list must be read from the live page. So this is async and
        the route awaits it.

        Returns {"models": [{"id","label"}], "active_model": str|None,
                 "thinking_modes": [...]}
        Engines that don't support dynamic model listing get a safe default.
        """
        fallback = {
            "models": [{"id": "default", "label": self.provider_name.title()}],
            "active_model": None,
            "thinking_modes": [
                {"id": "fast", "label": "Fast"},
                {"id": "thinking", "label": "Thinking"},
            ],
        }
        try:
            account = self.account
            engine = await self._get_engine(account, account)
            lister = getattr(engine, "list_models", None)
            if lister is None:
                return fallback
            meta = await lister()
            models = meta.get("models") or []
            if not models:
                return fallback
            return {
                "models": models,
                "active_model": meta.get("active"),
                "thinking_modes": fallback["thinking_modes"],
            }
        except Exception as exc:
            logger.warning("[%s] get_ui_metadata failed: %s", self.backend_name, exc)
            return fallback

    async def switch_model(self, model_type: str) -> dict[str, Any]:
        """Switch the active model on the live browser engine.

        Returns {"status": "ok"|"error", ...} so the route can report cleanly
        instead of raising.
        """
        try:
            account = self.account
            engine = await self._get_engine(account, account)
            switcher = getattr(engine, "switch_model", None)
            if switcher is None:
                return {"status": "error",
                        "message": f"{self.backend_name} engine does not support model switching"}
            ok = await switcher(model_type)
            if ok:
                # Force a fresh upstream chat: the model is bound per-conversation,
                # so staying in the old chat would keep answering with the old one.
                try:
                    setattr(engine, "active_chat_id", None)
                except Exception:
                    pass
                return {"status": "ok", "model_type": model_type}
            return {"status": "error", "message": f"Could not switch to {model_type}"}
        except Exception as exc:
            logger.warning("[%s] switch_model failed: %s", self.backend_name, exc)
            return {"status": "error", "message": f"{type(exc).__name__}: {exc}"}

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
