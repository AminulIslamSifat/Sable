"""ChatGPT ghost-tab session — in-page fetch hijack with full device identity.

Why this exists
---------------
ChatGPT's ``POST /backend-api/f/conversation`` is gated by a per-message
sentinel bundle (proof-of-work + Cloudflare Turnstile + chat-requirements)
plus an ``x-conduit-token`` ES256 JWT. Unlike DeepSeek's ``x-hif-leim``
(24h cache) or Kimi's ``x-msh-shield-data`` (page-minted on demand), this
bundle is **single-use and bound to the SPA's own send path** — a hand-rolled
replay is rejected with 403 "Unusual activity".

So we do NOT replay. We HIJACK:

  1. The SPA mints prepare -> finalize -> the sentinel bundle itself.
  2. We intercept the outgoing ``/f/conversation`` fetch, **rewrite only the
     ``messages[]`` body** to our prompt, and let every header pass through
     untouched (the SPA already minted them correctly).
  3. We ``tee()`` the SSE response back to Python via ``window.__ghostEvent``.

No UI clicking. The send is triggered with ``form.requestSubmit()`` — one
programmatic call on the SPA's own composer form, which fires the SPA's real
submit handler (and therefore its token minting) exactly as a click would.

Verified end to end: body rewritten from a decoy UI string to a custom prompt,
HTTP 200, assistant replied with the rewritten prompt's answer.

Streaming model (mirrors connectors/deepseek/ghost.py):
  The teed SSE stream pushes every chunk to Python via the exposed
  ``window.__ghostEvent`` bridge. A per-request asyncio.Queue feeds the
  caller line-by-line. A 30s idle gap aborts the in-page reader AND raises
  asyncio.TimeoutError from the drain, so the caller can retry.

Public API
----------
    session = await ChatGPTGhostSession.for_account("browser-data-acc5")
    resp = await session.open_stream("hello", idle_secs=30)
    async with resp as r:
        async for line in r.aiter_lines():
            ...
    await ChatGPTGhostSession.close_all()

Settings (system/settings.json)
-------------------------------
    chatgpt_use_ghost: bool  (default True)

The browser binary is resolved per account from system/accounts.json
(browser_path). If the profile has no saved path, Playwright's bundled
Chromium is used.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from pathlib import Path

from engine.platform_paths import pid_exists

logger = logging.getLogger("sable.chatgpt_ghost")

_SYSTEM_DIR = Path(__file__).resolve().parent.parent.parent / "system"
CHATGPT_URL = "https://chatgpt.com/"

# This ghost browser runs HEADED (ProfileHub.acquire(force_headed=True)).
_LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-blink-features=AutomationControlled",
    "--disable-gpu",
]


# ----------------------------------------------------------------------
# Lock helpers — distinguish "our orphan" from a live foreign browser
# ----------------------------------------------------------------------

def _lock_pid(profile: Path) -> int | None:
    lock = profile / "SingletonLock"
    if not (lock.is_symlink() or lock.exists()):
        return None
    try:
        target = os.readlink(lock)
        _, _, pid_str = target.rpartition("-")
        return int(pid_str)
    except Exception:
        return None


def _pid_alive(pid: int) -> bool:
    # os.kill(pid, 0) is POSIX-only and *terminates* the target on Windows.
    # pid_exists() is the cross-platform liveness probe.
    return pid_exists(pid)


def _clear_singleton(profile: Path) -> None:
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            (profile / name).unlink(missing_ok=True)
        except OSError:
            pass


# ----------------------------------------------------------------------
# In-page hijack + stream
# ----------------------------------------------------------------------

# Installs a fetch interceptor that:
#   * rewrites the outgoing /f/conversation body with OUR prompt,
#   * tees the SSE response into window.__ghostEvent,
#   * exposes window.__ghostAbort for the Stop button.
# The interceptor is installed once per page; each open_stream() sets
# window.__ghostPrompt and triggers the SPA submit.
JS_HIJACK_INSTALL = r"""
() => {
  if (window.__ghostInstalled) return 'already';
  window.__ghostInstalled = true;
  window.__ghostPrompt = null;
  window.__ghostAbort = null;
  window.__ghostSendId = 0;

  const of = window.fetch;
  window.fetch = async function(...args) {
    const u = typeof args[0] === 'string' ? args[0] : (args[0] && args[0].url) || '';
    const opts = args[1] || {};
    const isSend = u.includes('/backend-api/f/conversation')
                   && !u.includes('prepare') && !u.includes('resume')
                   && (opts.method || 'GET') === 'POST';
    if (!isSend) return of.apply(this, args);
    window.__ghostSentAt = Date.now();

    // The SPA has now READ the composer (it issued the send), so clearing it
    // here can never race the submit handler. Clearing inside JS_SEND did
    // race it: whenever sentinel prepare was slow, the composer was emptied
    // before the SPA read it, the send was aborted, and /f/conversation/prepare
    // fired with NO POST /f/conversation following. Intermittent by design.
    try {
      const _el = document.querySelector('#prompt-textarea');
      if (_el) {
        _el.focus();
        document.execCommand('selectAll', false, null);
        document.execCommand('delete', false, null);
      }
    } catch (e) {}

    // Rewrite ONLY the message text; keep every SPA-minted header/cookie AND
    // preserve the SPA-built metadata (attachments live in
    // messages[0].metadata.attachments — replacing messages[] wholesale
    // silently dropped every attached file).
    let newOpts = opts;
    try {
      if (window.__ghostPrompt != null) {
        const b = JSON.parse(opts.body);
        const msgs = Array.isArray(b.messages) ? b.messages : [];
        if (msgs.length && msgs[0].content) {
          // Keep the SPA's message object (id, metadata.attachments, …) and
          // swap ONLY the text parts to our prompt.
          msgs[0].content.content_type = 'text';
          msgs[0].content.parts = [window.__ghostPrompt];
          b.messages = [msgs[0]];
        } else {
          // No SPA message to preserve — synthesize a minimal one (no attach).
          b.messages = [{
            id: crypto.randomUUID(),
            author: { role: 'user' },
            create_time: Date.now() / 1000,
            content: { content_type: 'text', parts: [window.__ghostPrompt] },
            metadata: {
              serialization_metadata: { custom_symbol_offsets: [] },
              submission_mode: 'manual_send',
            },
          }];
        }
        newOpts = Object.assign({}, opts, { body: JSON.stringify(b) });
      }
    } catch (e) { /* fall through with original body */ }

    const sid = ++window.__ghostSendId;
    const ctrl = new AbortController();
    window.__ghostAbort = ctrl;
    newOpts = Object.assign({}, newOpts, { signal: ctrl.signal });

    let res;
    try {
      res = await of.call(this, args[0], newOpts);
    } catch (e) {
      await window.__ghostEvent({ t: 'error', err: 'fetch: ' + String(e) });
      return new Response('', { status: 0 });
    }
    await window.__ghostEvent({ t: 'status', status: res.status });
    if (!res.body) { await window.__ghostEvent({ t: 'done' }); return res; }

    const [a, b] = res.body.tee();
    (async () => {
      const rd = b.getReader();
      const dec = new TextDecoder();
      // Batch chunks before crossing the JS->Python bridge. A per-chunk
      // __ghostEvent() call is one CDP roundtrip each; a 40ms / 8KB flush
      // collapses hundreds of roundtrips into a handful.
      let buf = '';
      let flushing = false;
      const flush = async () => {
        if (flushing || !buf) return;
        flushing = true;
        const chunk = buf; buf = '';
        try { await window.__ghostEvent({ t: 'chunk', data: chunk }); }
        finally { flushing = false; }
      };
      const timer = setInterval(() => { flush().catch(() => {}); }, 40);
      try {
        while (true) {
          const { done, value } = await rd.read();
          if (done) break;
          buf += dec.decode(value, { stream: true });
          if (buf.length >= 8192) await flush();
        }
      } catch (e) {
        await window.__ghostEvent({ t: 'error', err: 'stream: ' + String(e) });
      } finally {
        clearInterval(timer);
        await flush();
        if (window.__ghostAbort === ctrl) window.__ghostAbort = null;
        await window.__ghostEvent({ t: 'done' });
      }
    })();
    return new Response(a, { status: res.status, headers: res.headers });
  };
  return 'installed';
}
"""

# Sets the prompt and triggers the SPA's own submit. requestSubmit() fires the
# real submit handler (token minting included) without a synthetic click.
JS_SEND = r"""
async ({ prompt }) => {
  window.__ghostPrompt = prompt;
  window.__ghostSentAt = null;
  const el = document.querySelector('#prompt-textarea');
  if (!el) throw new Error('composer not found');
  // Type the REAL prompt so the SPA renders the correct user bubble — the old
  // 'sent' decoy leaked into the UI as a corrupted message. The fetch hook
  // still rewrites messages[] to the identical value, so the sent body stays
  // exact even if the composer mangles mentions/markdown.
  el.focus();
  document.execCommand('selectAll', false, null);
  // Type a SHORT placeholder, NOT the real prompt. The real prompt can be
  // tens of KB (instruction preamble), and React commits composer text into
  // component state asynchronously — submitting before that commit made the
  // SPA read an empty composer and silently drop the send (prepare fired, no
  // POST). The fetch interceptor below rewrites the outgoing messages[] to
  // window.__ghostPrompt, so the composer only needs to be non-empty. This
  // removes React's async commit from the send path entirely.
  document.execCommand('insertText', false, 'hi');
  const form = el.closest('form');
  if (!form) throw new Error('composer form not found');
  await new Promise(r => setTimeout(r, 80));
  form.requestSubmit();
  // Do NOT clear the composer here; the fetch interceptor clears it once the
  // POST is actually on the wire.
  return 'submitted';
}
"""

JS_ABORT = """
() => {
  if (window.__ghostAbort) { try { window.__ghostAbort.abort(); } catch (e) {} window.__ghostAbort = null; }
  return 'aborted';
}
"""


# ----------------------------------------------------------------------
# Streaming response shim (mirrors deepseek GhostStreamResponse)
# ----------------------------------------------------------------------

class ChatGPTStreamResponse:
    """Async context manager yielding SSE lines from the hijacked send."""

    def __init__(self, page, trigger_task: "asyncio.Task", queue: "asyncio.Queue",
                 idle_secs: float, on_close=None):
        self._page = page
        self._task = trigger_task
        self._q = queue
        self._idle = idle_secs
        self._on_close = on_close
        self._closed = False
        self.status_code: int = 200
        self._status_seen = False
        self._buf = ""
        self._raw = ""
        self._done = False
        self._drained = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self._abort_in_page()
        if not self._task.done():
            self._task.cancel()
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
            pass
        self._release_once()
        return False

    async def _abort_in_page(self) -> None:
        try:
            if self._page is not None and not self._page.is_closed():
                await self._page.evaluate(JS_ABORT)
        except Exception:
            pass

    def _release_once(self) -> None:
        if not self._closed and self._on_close is not None:
            self._closed = True
            self._on_close()

    def force_close(self) -> None:
        """Out-of-band teardown for Stop. Aborts the in-page fetch, unblocks
        the drainer, cancels the trigger task, releases the lock now."""
        if self._closed:
            return
        try:
            self._q.put_nowait({"t": "done"})
        except Exception:
            pass
        self._done = True
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._abort_in_page())
        except Exception:
            pass
        if not self._task.done():
            self._task.cancel()
        self._release_once()

    @property
    async def content(self) -> bytes:
        if not self._drained:
            async for _ in self.aiter_lines():
                pass
        return self._raw.encode("utf-8", errors="replace")

    async def aiter_lines(self):
        while not self._done:
            try:
                ev = await asyncio.wait_for(self._q.get(), timeout=self._idle)
            except asyncio.TimeoutError:
                raise asyncio.TimeoutError(
                    f"ghost idle >{self._idle:.0f}s (connect or mid-stream stall)")
            t = ev.get("t")
            if t == "status":
                try:
                    self.status_code = int(ev.get("status", 200))
                except Exception:
                    self.status_code = 200
                self._status_seen = True
                continue
            if t == "chunk":
                data = ev.get("data", "")
                self._raw += data
                self._buf += data
                while "\n" in self._buf:
                    line, self._buf = self._buf.split("\n", 1)
                    yield line
                continue
            if t == "error":
                self._done = True
                raise RuntimeError(f"ghost stream error: {ev.get('err')}")
            if t == "done":
                self._done = True
                if self._buf:
                    yield self._buf
                    self._buf = ""
                break
        self._drained = True


# ----------------------------------------------------------------------
# The session
# ----------------------------------------------------------------------

class ChatGPTGhostSession:
    _registry: dict[str, "ChatGPTGhostSession"] = {}
    _registry_lock: asyncio.Lock | None = None

    def __init__(self, account: str, helium_bin: str, profile_dir: Path):
        self.account = account
        self.helium_bin = helium_bin
        self.profile_dir = profile_dir
        self._ctx = None
        self._page = None
        self._hub = None
        self._pipe_q: asyncio.Queue | None = None
        self._stream_lock = asyncio.Lock()
        self._active_response: "ChatGPTStreamResponse | None" = None
        self._installed = False

    @classmethod
    async def for_account(cls, account: str) -> "ChatGPTGhostSession":
        """Return a running session for `account`, respawning if dead."""
        if cls._registry_lock is None:
            cls._registry_lock = asyncio.Lock()
        async with cls._registry_lock:
            existing = cls._registry.get(account)
            if existing is not None and await existing.is_healthy():
                return existing
            if existing is not None:
                await existing.close()
            from engine.platform_paths import resolve_browser_for_profile
            # Honor the per-account browser set in accounts.json (Chrome/Helium/
            # any Chromium). No saved path -> Playwright bundled Chromium.
            bin_path = resolve_browser_for_profile(account)
            profile = _SYSTEM_DIR / account
            from connectors.common.profile_hub import (
                ProfileHub as _PH, _kill_profile_holders as _kill,
            )
            last_exc: Exception | None = None
            for attempt in range(1, 4):
                session = cls(account, bin_path, profile)
                try:
                    await session.start()
                    cls._registry[account] = session
                    return session
                except Exception as exc:
                    last_exc = exc
                    logger.warning(
                        "[chatgpt:%s] start attempt %d/3 failed: %s: %s — "
                        "killing profile browsers & retrying",
                        account, attempt, type(exc).__name__, exc)
                    try:
                        await session.close()
                    except Exception:
                        pass
                    try:
                        await _PH.evict(profile)
                        await _kill(profile)
                    except Exception as kill_exc:
                        logger.warning("[chatgpt:%s] profile kill failed: %s",
                                       account, kill_exc)
                    if attempt < 3:
                        await asyncio.sleep(min(0.5 * attempt, 2.0))
            raise RuntimeError(
                f"chatgpt:{account}: session failed to start after 3 attempts"
            ) from last_exc

    @classmethod
    async def close_all(cls) -> None:
        for s in list(cls._registry.values()):
            try:
                await s.close()
            except Exception:
                pass
        cls._registry.clear()

    async def start(self) -> None:
        if not self.profile_dir.exists():
            raise RuntimeError(f"profile dir missing: {self.profile_dir}")
        from connectors.common.profile_hub import ProfileHub
        # Launch mode is user-controlled from Settings → Account → ChatGPT
        # (headless on/off). Default headed so a first run is visible. When
        # headless, we launch WITHOUT force_headed and with headless=True.
        _headless = False
        try:
            from engine.scraper import get_settings as _get_scraper_settings
            _headless = bool(_get_scraper_settings().get("chatgpt_headless", False))
        except Exception:
            _headless = False
        _t0 = time.monotonic()
        # kill_existing=True: kill EVERY browser already on this profile before
        # launching, so two Chromium writers never share the same
        # --user-data-dir (which corrupts Cookies / Local Storage / LevelDB).
        # This is independent of the head/headless choice — even a headless
        # ghost must own the profile exclusively.
        self._hub = await ProfileHub.acquire(
            self.profile_dir, self.helium_bin, headless=_headless,
            force_headed=not _headless, kill_existing=True)
        _t1 = time.monotonic()
        self._ctx = self._hub.context
        self._page = await self._hub.new_tab(CHATGPT_URL)
        _t2 = time.monotonic()
        await self._install_hijack()
        _t3 = time.monotonic()
        logger.info(
            "[chatgpt:%s] START TIMINGS  acquire=%.1fs  new_tab=%.1fs  "
            "hijack=%.1fs  total=%.1fs",
            self.account, _t1 - _t0, _t2 - _t1, _t3 - _t2, _t3 - _t0)

    async def _install_hijack(self) -> None:
        if self._page is None:
            raise RuntimeError("chatgpt ghost not started")
        try:
            await self._page.wait_for_selector("#prompt-textarea", timeout=20000)
        except Exception:
            raise RuntimeError("chatgpt composer never appeared — not logged in?")
        try:
            await self._page.expose_function("__ghostEvent", self._on_event)
        except Exception:
            pass
        res = await self._page.evaluate(JS_HIJACK_INSTALL)
        self._installed = res in ("installed", "already")
        logger.info("[chatgpt:%s] hijack %s", self.account, res)

    async def close(self) -> None:
        if self._hub is not None and self._page is not None:
            try:
                await self._hub.close_tab(self._page)
            except Exception:
                pass
        self._page = None
        self._ctx = None
        self._hub = None
        self._installed = False

    async def prepare_new_chat(self) -> None:
        """Navigate the ghost page to a fresh, blank ChatGPT conversation.

        MUST be called BEFORE upload_file(): the navigation reloads the page
        and would wipe any composer attachments staged earlier. The page is
        created once in start() and never navigated, so without this the SPA
        keeps appending to whatever thread is open. Navigation also wipes the
        in-page fetch hook, so we force a reinstall afterwards.
        """
        if self._page is None or self._page.is_closed():
            raise RuntimeError("chatgpt ghost page closed")
        try:
            await self._page.goto(CHATGPT_URL, wait_until="domcontentloaded", timeout=20000)
            await self._page.wait_for_selector("#prompt-textarea", timeout=20000)
            await self._install_hijack()
        except Exception as exc:
            logger.warning("[chatgpt:%s] fresh-chat navigation failed: %s",
                           self.account, exc)

    # Records the HTTP status of the SPA's /backend-api/files upload so a
    # failure can be classified (rate limit vs too-large vs auth vs generic)
    # and surfaced to the UI instead of vanishing. Installed once per page.
    JS_UPLOAD_WATCH = r"""
    () => {
      if (window.__upWatch) return 'already';
      window.__upWatch = true;
      window.__upStatus = null;
      const of = window.fetch;
      window.fetch = async function(...args) {
        const u = typeof args[0] === 'string' ? args[0] : (args[0] && args[0].url) || '';
        if (u.includes('/backend-api/files') && !u.includes('process_upload')) {
          try {
            const res = await of.apply(this, args);
            let body = '';
            try { body = (await res.clone().text()).slice(0, 500); } catch (e) {}
            window.__upStatus = { status: res.status, body };
            return res;
          } catch (e) {
            window.__upStatus = { status: 0, body: 'network: ' + String(e) };
            throw e;
          }
        }
        return of.apply(this, args);
      };
      return 'installed';
    }
    """

    @staticmethod
    def _classify_upload_failure(status: int, body: str) -> str:
        """Map an upload HTTP status/body to a short UI-friendly reason code."""
        b = (body or "").lower()
        if status == 429 or "rate limit" in b or "too many" in b:
            return "rate_limited"
        if status == 413 or "too large" in b or "maximum" in b:
            return "file_too_large"
        if status in (401, 403) or "unauthorized" in b or "forbidden" in b:
            return "not_authorized"
        if status == 0:
            return "network_error"
        if status and status >= 500:
            return f"server_error_{status}"
        if status and status >= 400:
            return f"upload_rejected_{status}"
        return "upload_failed"

    async def upload_file(self, path: str, timeout_ms: int = 30000) -> dict[str, Any]:
        """Stage a file in the ChatGPT composer via the hidden #upload-files input.

        The SPA uploads it and, on the next submit, builds
        ``messages[0].metadata.attachments[]`` itself. We therefore do NOT
        hand-roll the /backend-api/files dance — we set the input and let the
        SPA run its own upload path (prepare token, Azure PUT, process), which
        is the only flow the server accepts. The hijack's message rewrite is
        adjusted to PRESERVE metadata.attachments so the SPA-built attachment
        survives our prompt swap.

        Returns {path, name, ok, reason}. On failure `ok` is False and `reason`
        is a short code (rate_limited / file_too_large / not_authorized /
        network_error / server_error_<n> / upload_rejected_<n> / upload_failed /
        timeout). The caller surfaces `reason` to the UI and CONTINUES the send
        without the attachment — a failed upload must never abort the turn.
        """
        from pathlib import Path as _Path
        p = _Path(path)
        if not p.is_file():
            raise FileNotFoundError(f"chatgpt upload: no such file: {p}")
        if self._page is None or self._page.is_closed():
            raise RuntimeError("chatgpt ghost page closed")
        # Install the upload-status watcher (idempotent) and reset it.
        try:
            await self._page.evaluate(self.JS_UPLOAD_WATCH)
            await self._page.evaluate("() => { window.__upStatus = null; }")
        except Exception:
            pass
        locator = self._page.locator("input#upload-files").first
        await locator.set_input_files(str(p), timeout=timeout_ms)
        # Wait for the SPA to finish its upload and render the attachment chip.
        # Poll for the file name text; fall back to a fixed settle time.
        attached = False
        deadline = time.time() + (timeout_ms / 1000.0)
        while time.time() < deadline:
            try:
                found = await self._page.evaluate(
                    "(name) => document.body.innerText.includes(name)", p.name
                )
                if found:
                    attached = True
                    break
            except Exception:
                pass
            await asyncio.sleep(0.5)
        # Give the SPA a moment to finalize the attachment record server-side.
        await asyncio.sleep(1.5)

        if attached:
            logger.info("[chatgpt:%s] uploaded %s (ok)", self.account, p.name)
            return {"path": str(p), "name": p.name, "ok": True, "reason": None}

        # Not attached — read the captured upload HTTP status to classify why.
        reason = "upload_failed"
        try:
            st = await self._page.evaluate("() => window.__upStatus")
            if st and isinstance(st, dict):
                reason = self._classify_upload_failure(
                    int(st.get("status") or 0), str(st.get("body") or "")
                )
            else:
                reason = "timeout"
        except Exception:
            reason = "timeout"
        logger.warning(
            "[chatgpt:%s] upload FAILED for %s (reason=%s)", self.account, p.name, reason
        )
        return {"path": str(p), "name": p.name, "ok": False, "reason": reason}

    async def is_healthy(self) -> bool:
        if self._page is None or self._ctx is None:
            return False
        try:
            if self._page.is_closed():
                return False
            await asyncio.wait_for(self._page.evaluate("() => 1"), timeout=5)
            return True
        except Exception:
            return False

    async def has_token(self, wait_secs: float = 8.0, poll: float = 1.0) -> bool:
        """True when the page carries a logged-in ChatGPT session token.

        Polls ``/api/auth/session`` inside the page — the same endpoint the SPA
        uses. Cookies alone can be stale, so we check for a real accessToken.
        """
        if self._page is None or self._page.is_closed():
            return False
        deadline = time.time() + wait_secs
        while True:
            try:
                ok = await self._page.evaluate(
                    "async () => { try { const r = await fetch('/api/auth/session',"
                    "{credentials:'include'}); if (!r.ok) return false;"
                    " const j = await r.json(); return !!(j && j.accessToken); }"
                    " catch (e) { return false; } }"
                )
                if ok:
                    return True
            except Exception:
                pass
            if time.time() >= deadline:
                return False
            await asyncio.sleep(poll)

    def _on_event(self, ev: dict) -> None:
        # Debug capture: dump every bridge event to disk so we can diff what
        # the page actually streamed vs what our decoder consumed. Enabled
        # always for now — cheap (one append per event) and essential while
        # the decoder is still losing mid-stream patches.
        try:
            _cap = Path.home() / "sable_output" / "logs"
            _cap.mkdir(parents=True, exist_ok=True)
            _f = _cap / f"chatgpt_sse_{self.account}.jsonl"
            import json as _j
            with _f.open("a", encoding="utf-8") as _fh:
                _fh.write(_j.dumps({"t": time.time(), "ev": ev},
                                   ensure_ascii=False) + "\n")
        except Exception:
            pass
        if self._pipe_q is not None:
            self._pipe_q.put_nowait(ev)

    async def open_stream(self, prompt: str,
                          idle_secs: float = 30.0) -> ChatGPTStreamResponse:
        """Trigger one ChatGPT send with `prompt` via the hijacked SPA submit.
        Returns a stream whose aiter_lines() yields SSE lines as they arrive.
        Serialized per session (one active stream at a time)."""
        _ts = time.monotonic()
        if self._page is None or self._page.is_closed():
            raise RuntimeError("chatgpt ghost page closed")
        if not self._installed:
            await self._install_hijack()
        try:
            await asyncio.wait_for(self._stream_lock.acquire(), timeout=8.0)
            logger.info("[chatgpt:%s] open_stream: lock acquired in %.2fs",
                        self.account, time.monotonic() - _ts)
        except asyncio.TimeoutError:
            logger.warning("[chatgpt:%s] stream lock stuck >8s — force-closing",
                           self.account)
            stale = self._active_response
            if stale is not None:
                stale.force_close()
            try:
                await asyncio.wait_for(self._stream_lock.acquire(), timeout=2.0)
            except asyncio.TimeoutError:
                self._stream_lock = asyncio.Lock()
                await self._stream_lock.acquire()

        def _release() -> None:
            if self._stream_lock.locked():
                self._stream_lock.release()
            self._pipe_q = None
            self._active_response = None

        try:
            queue: asyncio.Queue = asyncio.Queue()
            self._pipe_q = queue
            task = asyncio.create_task(
                self._page.evaluate(JS_SEND, {"prompt": prompt}))

            def _on_send_done(t: "asyncio.Task") -> None:
                # A throw inside JS_SEND (composer not found, form missing)
                # otherwise vanishes and the drain waits out the full idle
                # timeout. Surface it into the stream immediately.
                if t.cancelled():
                    return
                exc = t.exception()
                if exc is not None:
                    logger.error("[chatgpt:%s] JS_SEND failed: %s",
                                 self.account, exc)
                    try:
                        queue.put_nowait({"t": "error",
                                          "err": f"send failed: {exc}"})
                    except Exception:
                        pass

            task.add_done_callback(_on_send_done)
            resp = ChatGPTStreamResponse(self._page, task, queue, idle_secs,
                                         on_close=_release)
            self._active_response = resp
            asyncio.create_task(self._resend_watchdog(prompt, resp))
            return resp
        except Exception:
            _release()
            raise

    async def _resend_watchdog(self, prompt: str,
                               resp: "ChatGPTStreamResponse") -> None:
        """Re-trigger the send when the SPA silently swallows it.

        Observed intermittently: ``/f/conversation/prepare`` fires but the
        POST ``/f/conversation`` never does — the SPA's submit handler aborts
        before sending. Nothing was sent, so re-firing ``requestSubmit()`` is
        safe (no duplicate message). Guarded on the in-page POST marker so a
        request that actually went out is never re-sent.
        """
        for attempt in range(1, 3):
            await asyncio.sleep(6.0 * attempt)
            if resp._done or resp._closed:
                return
            try:
                sent_at = await self._page.evaluate(
                    "() => window.__ghostSentAt || null")
            except Exception:
                return
            if sent_at:
                return
            logger.warning(
                "[chatgpt:%s] POST never fired after %.0fs — re-triggering "
                "submit (attempt %d/2)", self.account, 6.0 * attempt, attempt)
            try:
                await self._page.evaluate(JS_SEND, {"prompt": prompt})
            except Exception as exc:
                logger.warning("[chatgpt:%s] re-trigger failed: %s",
                               self.account, exc)
                return

    def cancel_stream(self) -> bool:
        """Out-of-band cancel for the Stop button."""
        resp = self._active_response
        if resp is None:
            return False
        resp.force_close()
        return True


# ----------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------

def ghost_enabled() -> bool:
    """Read `chatgpt_use_ghost` from settings.json (default True)."""
    try:
        path = _SYSTEM_DIR / "settings.json"
        if not path.is_file():
            return True
        s = json.loads(path.read_text(encoding="utf-8"))
        val = s.get("chatgpt_use_ghost")
        return True if val is None else bool(val)
    except Exception:
        return True


