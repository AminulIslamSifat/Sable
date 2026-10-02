"""DeepSeek ghost-tab session — in-page fetch with full device identity.

Why this exists
---------------
Curl sends 6 headers and 0 cookies on /chat/completion. The browser sends
20 headers + full cookies (including x-hif-leim + x-device-id, both minted
by the SPA's patched fetch wrapper). Same JWT walking in two costumes →
DeepSeek's anomaly detector flags the account and silently bans it.

Ghost-injected matches the browser byte-for-byte. Verified 20/20 header
match in deepseek_v9_test.py.

Streaming model (matches engine/inpage.py's Qwen ghost):
  The in-page fetch pushes every chunk to Python via the exposed
  `window.__ghostEvent` bridge. A per-request asyncio.Queue feeds the
  caller line-by-line, so tokens arrive as they are produced instead of
  being buffered into one blob.

Idle timeout:
  A 30s gap with no event — during connect or mid-stream — aborts the
  in-page fetch (AbortController) AND raises asyncio.TimeoutError from the
  queue drain. The caller's retry loop drops the connection and opens a
  fresh one.

Public API
----------
    session = await GhostSession.for_account("browser-data-acc5")
    resp = await session.open_stream(token, body, pow_header, idle_secs=30)
    async with resp as r:
        async for line in r.aiter_lines():
            ...
    await GhostSession.close_all()

Settings (system/settings.json)
-------------------------------
    deepseek_use_ghost: bool  (default True)
    helium_binary: str        (default /opt/helium-browser-bin/helium)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal

from engine import process_utils
from engine.platform_paths import pid_exists
import socket
import time
from pathlib import Path

logger = logging.getLogger("sable.deepseek_ghost")

_SYSTEM_DIR = Path(__file__).resolve().parent.parent.parent / "system"
IDENTITY_STORE = _SYSTEM_DIR / ".deepseek_identity.json"
DEFAULT_HELIUM = "/opt/helium-browser-bin/helium"
IDENTITY_TTL_SEC = 24 * 3600

# No "--ozone-platform=headless" here: this file's ghost browser runs HEADED
# (see GhostSession.start -> ProfileHub.acquire(force_headed=True)). The
# headless path is gone by request; _LAUNCH_ARGS must not re-impose it.
_LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-blink-features=AutomationControlled",
    "--disable-gpu",
]

# Headers we do NOT inject: set by browser automatically, or by us per-request.
_SKIP_INJECT = frozenset({
    "authorization", "x-ds-pow-response", "content-length", "host",
    "connection", "content-type", "cookie", "referer",
})


# ----------------------------------------------------------------------
# Identity cache (per-account header set sniffed from a real UI send)
# ----------------------------------------------------------------------

def _load_identity_store() -> dict:
    if IDENTITY_STORE.exists():
        try:
            raw = json.loads(IDENTITY_STORE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return raw
        except Exception:
            pass
    return {}


def _save_identity_store(store: dict) -> None:
    import tempfile
    IDENTITY_STORE.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=str(IDENTITY_STORE.parent),
        suffix=".tmp", delete=False,
    ) as tmp:
        json.dump(store, tmp, indent=2)
        tmp_path = tmp.name
    os.replace(tmp_path, str(IDENTITY_STORE))


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
# In-page streaming fetch
# ----------------------------------------------------------------------

JS_STREAM = r"""
async ({url, body, token, pow, extraHeaders, idleMs}) => {
  try {
    const h = Object.assign({
      'Content-Type': 'application/json',
      'Authorization': 'Bearer ' + token,
      'X-DS-PoW-Response': pow,
    }, extraHeaders || {});
    const ctrl = new AbortController();
    // Expose the controller so the server can abort this fetch out-of-band
    // (Stop button) without waiting for the 30s idle gate. Only one stream
    // runs at a time per session (enforced by _stream_lock), so a single
    // page-global slot is safe.
    window.__ghostAbort = ctrl;
    let idleTimer = setTimeout(() => ctrl.abort(), idleMs);
    const res = await fetch(url, {
      method: 'POST', credentials: 'include', headers: h,
      body: JSON.stringify(body), signal: ctrl.signal,
    });
    await window.__ghostEvent({t: 'status', status: res.status});
    if (!res.body) { await window.__ghostEvent({t: 'done'}); return 'nobody'; }
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    try {
      while (true) {
        clearTimeout(idleTimer);
        idleTimer = setTimeout(() => ctrl.abort(), idleMs);
        const {done, value} = await reader.read();
        if (done) break;
        await window.__ghostEvent({t: 'chunk', data: dec.decode(value, {stream: true})});
      }
    } finally {
      clearTimeout(idleTimer);
      if (window.__ghostAbort === ctrl) window.__ghostAbort = null;
    }
    await window.__ghostEvent({t: 'done'});
    return 'ok';
  } catch (e) {
    await window.__ghostEvent({t: 'error', err: String(e)});
    return 'err';
  }
}
"""


# ----------------------------------------------------------------------
# Streaming response shim
# ----------------------------------------------------------------------

class GhostStreamResponse:
    """Async context manager mimicking curl_cffi's streamed Response.

    Events are pulled from a per-request asyncio.Queue fed by the in-page
    `window.__ghostEvent` bridge. `aiter_lines()` yields SSE lines as they
    arrive; a gap longer than `idle_secs` raises asyncio.TimeoutError.
    """

    def __init__(self, page, eval_task: "asyncio.Task", queue: "asyncio.Queue",
                 idle_secs: float, on_close=None):
        self._page = page
        self._task = eval_task
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
        # Abort the in-page fetch first so the Playwright evaluate task can
        # actually settle — otherwise cancel()ing the Python task leaves the
        # browser fetch streaming and the task never finishes.
        await self._abort_in_page()
        if not self._task.done():
            self._task.cancel()
        try:
            # Bound this — a wedged evaluate task must not hold the lock open.
            await asyncio.wait_for(self._task, timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
            pass
        self._release_once()
        return False

    async def _abort_in_page(self) -> None:
        """Fire the in-page AbortController so the fetch stops server-side."""
        try:
            if self._page is not None and not self._page.is_closed():
                await self._page.evaluate(
                    "() => { if (window.__ghostAbort) { try { window.__ghostAbort.abort(); } catch (e) {} window.__ghostAbort = null; } }"
                )
        except Exception:
            pass

    def _release_once(self) -> None:
        """Release the session stream lock exactly once (idempotent)."""
        if not self._closed and self._on_close is not None:
            self._closed = True
            self._on_close()

    def force_close(self) -> None:
        """Synchronous out-of-band teardown for the Stop button.

        Called from /api/chat/stop. Aborts the in-page fetch (scheduled),
        cancels the evaluate task, unblocks any drainer waiting on the queue,
        and releases the stream lock immediately — so a new message can't
        wedge on a lock held by a stream the user already abandoned.
        """
        if self._closed:
            return
        # 1. Unblock aiter_lines() if it's parked on queue.get(). A "done"
        # event makes the drainer break cleanly instead of raising, so the
        # client's async-for ends gracefully and the chat generator's finally
        # block (tail-persist) still runs.
        try:
            self._q.put_nowait({"t": "done"})
        except Exception:
            pass
        self._done = True
        # 2. Abort the browser fetch + cancel the evaluate task (best effort).
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._abort_in_page())
        except Exception:
            pass
        if not self._task.done():
            self._task.cancel()
        # 3. Release the lock NOW — never wait on the task.
        self._release_once()

    @property
    async def content(self) -> bytes:
        # Drain any remaining events so non-200 branches can read the body.
        if not self._drained:
            async for _ in self.aiter_lines():
                pass
        return self._raw.encode("utf-8", errors="replace")

    async def aiter_lines(self):
        while not self._done:
            try:
                ev = await asyncio.wait_for(self._q.get(), timeout=self._idle)
            except asyncio.TimeoutError:
                # Idle gate — no event within the window. Surface as the same
                # TimeoutError the retry loop already handles for curl.
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

class GhostSession:
    _registry: dict[str, "GhostSession"] = {}
    _registry_lock: asyncio.Lock | None = None

    def __init__(self, account: str, helium_bin: str, profile_dir: Path):
        self.account = account
        self.helium_bin = helium_bin
        self.profile_dir = profile_dir
        self._playwright = None
        self._ctx = None
        self._page = None
        self._cdp_browser = None
        self._identity: dict[str, str] = {}
        self._started_at: float = 0.0
        self._exposed_pages: set = set()
        self._pipe_q: asyncio.Queue | None = None
        self._stream_lock = asyncio.Lock()
        # The response object owning the current stream (None when idle). Lets
        # /api/chat/stop reach in and force-close an in-flight stream.
        self._active_response: "GhostStreamResponse | None" = None
        self._marker = profile_dir / ".deepseek_ghost.json"
        self._hub = None

    # -- lifecycle -----------------------------------------------------

    @classmethod
    async def for_account(cls, account: str, helium_bin: str | None = None) -> "GhostSession":
        """Return a running session for `account`, respawning if dead."""
        if cls._registry_lock is None:
            cls._registry_lock = asyncio.Lock()
        async with cls._registry_lock:
            existing = cls._registry.get(account)
            if existing is not None and await existing.is_healthy():
                return existing
            if existing is not None:
                await existing.close()
            bin_path = helium_bin or _read_helium_from_settings() or DEFAULT_HELIUM
            profile = _SYSTEM_DIR / account
            # Kill-and-retry: a closed/externally-killed/stale browser context
            # makes start() blow up hard. On failure evict the hub, kill every
            # browser on this profile, and try again — max 3 attempts.
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
                        "[ghost:%s] start attempt %d/3 failed: %s: %s — "
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
                        logger.warning("[ghost:%s] profile kill failed: %s",
                                       account, kill_exc)
                    if attempt < 3:
                        await asyncio.sleep(min(0.5 * attempt, 2.0))
            raise RuntimeError(
                f"deepseek:{account}: session failed to start after 3 attempts"
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
        # Shared per-profile browser (ProfileHub): adopt the ONE browser that
        # owns the profile dir and open our own DeepSeek origin tab on it, so
        # co-located connectors don't kill-war over SingletonLock.
        # DeepSeek runs its ghost browser HEADED (visible window). In-page
        # fetch under a real windowed browser keeps a genuine device identity
        # and lets you eyeball the session; a headless holder is killed and
        # replaced by _ensure via force_headed.
        from connectors.common.profile_hub import ProfileHub
        self._hub = await ProfileHub.acquire(
            self.profile_dir, self.helium_bin, headless=False,
            force_headed=True)
        self._ctx = self._hub.context
        self._page = await self._hub.new_tab("https://chat.deepseek.com/")
        await self._after_browser_ready()

    async def _after_browser_ready(self) -> None:
        self._started_at = time.time()
        # ALWAYS sniff fresh. x-hif-leim rotates and x-hif-dliq is per-device
        # but was never captured by the old page.route sniffer. A stale cached
        # signature replayed against a fresh token is the frozen-fingerprint
        # mismatch that gets accounts silently banned. The sniff costs ONE UI
        # send at session start (not per message) — cheap insurance.
        try:
            await self._sniff_identity()
        except Exception as exc:
            cached = self._load_identity()
            if cached:
                self._identity = cached
                logger.warning(
                    "[ghost:%s] fresh sniff failed (%s) — falling back to "
                    "cached identity (%d headers)", self.account, exc, len(cached))
            else:
                raise

    async def _launch(self) -> None:
        from playwright.async_api import async_playwright

        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()

        self._playwright = await async_playwright().start()
        self._ctx = await self._playwright.chromium.launch_persistent_context(
            executable_path=self.helium_bin,
            user_data_dir=str(self.profile_dir),
            headless=True,
            args=_LAUNCH_ARGS + [f"--remote-debugging-port={port}"],
        )
        self._page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
        await self._page.goto("https://chat.deepseek.com/",
                              wait_until="domcontentloaded", timeout=60000)
        await self._page.wait_for_timeout(2000)
        try:
            self._marker.write_text(json.dumps({"port": port, "ts": time.time()}))
        except OSError:
            pass

    async def _adopt_existing(self) -> bool:
        """If our marker points at a live CDP endpoint, reuse that browser."""
        if not self._marker.exists():
            return False
        try:
            m = json.loads(self._marker.read_text())
            port = int(m["port"])
        except Exception:
            return False
        from playwright.async_api import async_playwright
        try:
            self._playwright = await async_playwright().start()
            self._cdp_browser = await self._playwright.chromium.connect_over_cdp(
                f"http://127.0.0.1:{port}", timeout=5000)
            ctxs = self._cdp_browser.contexts
            if not ctxs:
                raise RuntimeError("no contexts over CDP")
            self._ctx = ctxs[0]
            pages = self._ctx.pages
            self._page = pages[0] if pages else await self._ctx.new_page()
            logger.info("[ghost:%s] adopted existing browser over CDP (port %d)",
                        self.account, port)
            return True
        except Exception as exc:
            logger.info("[ghost:%s] CDP adopt failed (%s) — will relaunch",
                        self.account, exc)
            try:
                if self._playwright is not None:
                    await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
            self._cdp_browser = None
            self._marker.unlink(missing_ok=True)
            return False

    async def _ensure_profile_free(self) -> None:
        """Kill a live-but-unadoptable orphan on this profile, or clear a stale
        lock. A live browser we can't talk to is presumed to be ours (leftover
        from an interrupted launch) and is terminated."""
        pid = _lock_pid(self.profile_dir)
        if pid is not None and _pid_alive(pid):
            logger.warning("[ghost:%s] live orphan on profile (pid %d) — killing",
                           self.account, pid)
            process_utils.kill_process_tree(pid)
            for _ in range(10):
                await asyncio.sleep(0.3)
                if not _pid_alive(pid):
                    break
            if _pid_alive(pid):
                process_utils.kill_process_tree(pid, sig=process_utils.SIGKILL)
        _clear_singleton(self.profile_dir)
        try:
            self._marker.unlink(missing_ok=True)
        except OSError:
            pass

    async def close(self) -> None:
        # Only this connector's tab dies; the shared browser (owned by the
        # ProfileHub) stays up for other connectors on the same profile.
        if self._hub is not None and self._page is not None:
            try:
                await self._hub.close_tab(self._page)
            except Exception:
                pass
        self._page = None
        self._ctx = None
        self._playwright = None
        self._cdp_browser = None
        self._hub = None

    # -- health --------------------------------------------------------

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

    # -- identity ------------------------------------------------------

    def _load_identity(self) -> dict[str, str]:
        store = _load_identity_store()
        entry = store.get(self.account)
        if not isinstance(entry, dict):
            return {}
        if time.time() - entry.get("cached_at", 0) > IDENTITY_TTL_SEC:
            logger.info("[ghost:%s] cached identity expired", self.account)
            return {}
        headers = entry.get("headers")
        if not isinstance(headers, dict):
            return {}
        return headers

    def _save_identity(self, headers: dict[str, str]) -> None:
        store = _load_identity_store()
        store[self.account] = {"cached_at": time.time(), "headers": headers}
        _save_identity_store(store)

    async def _sniff_identity(self) -> None:
        """Drive a real UI send once and capture the FULL header set via CDP.

        CDP `Network.requestWillBeSent` is used instead of `page.route` because
        Playwright's route API does NOT surface `x-hif-dliq` — the SPA mints it
        through a path route interception never sees. The old route-based
        sniffer therefore cached an incomplete identity (leim but no dliq), and
        replaying a partial signature is exactly what flagged the account.
        """
        captured: list[dict] = []
        cdp = await self._ctx.new_cdp_session(self._page)
        try:
            await cdp.send("Network.enable")

            def _on_req(params: dict) -> None:
                req = params.get("request", {})
                if ("/api/v0/chat/completion" in req.get("url", "")
                        and req.get("method") == "POST"):
                    captured.append(
                        {(k or "").lower(): v for k, v in req.get("headers", {}).items()})

            cdp.on("Network.requestWillBeSent", _on_req)

            sel = None
            for s in ('textarea:visible', '#chat-input', '[contenteditable="true"]'):
                try:
                    if await self._page.locator(s).count():
                        sel = s
                        break
                except Exception:
                    pass
            if not sel:
                raise RuntimeError("composer not found for identity sniff")
            await self._page.locator(sel).first.click(timeout=5000)
            await self._page.keyboard.type("hi", delay=25)
            await self._page.wait_for_timeout(400)
            await self._page.keyboard.press("Enter")
            await self._page.wait_for_timeout(7000)
        finally:
            try:
                await cdp.detach()
            except Exception:
                pass

        if not captured:
            raise RuntimeError("identity sniff failed — no completion POST captured")
        injected = {k: v for k, v in captured[0].items() if k not in _SKIP_INJECT}
        missing = [h for h in ("x-hif-dliq", "x-hif-leim", "x-device-id")
                   if h not in injected]
        if missing:
            raise RuntimeError(f"identity sniff incomplete — missing {missing}")
        self._identity = injected
        self._save_identity(injected)
        logger.info(
            "[ghost:%s] sniffed %d identity headers (dliq=%d leim=%d dev=%s)",
            self.account, len(injected),
            len(injected.get("x-hif-dliq", "")),
            len(injected.get("x-hif-leim", "")),
            injected.get("x-device-id", "")[:8])

    # -- send ----------------------------------------------------------

    def _on_event(self, ev: dict) -> None:
        if self._pipe_q is not None:
            self._pipe_q.put_nowait(ev)

    async def _ensure_bridge(self) -> None:
        if self._page is None:
            raise RuntimeError("ghost session not started")
        if self._page.is_closed():
            raise RuntimeError("ghost page closed")
        if self._page not in self._exposed_pages:
            await self._page.expose_function("__ghostEvent", self._on_event)
            self._exposed_pages.add(self._page)

    async def open_stream(
        self, token: str, body: dict, pow_header: str, idle_secs: float = 30.0,
    ) -> GhostStreamResponse:
        """Open a streaming in-page fetch. Returns a GhostStreamResponse whose
        `aiter_lines()` yields SSE lines as they arrive. Serialized per session
        (one active stream at a time) so the shared event bridge stays coherent."""
        await self._ensure_bridge()
        # Bounded acquire: a lock left held by an abandoned stream (Stop before
        # __aexit__ ran) must NOT wedge every future message on this account.
        # If it times out, force-close the stale owner and take the lock.
        try:
            await asyncio.wait_for(self._stream_lock.acquire(), timeout=8.0)
        except asyncio.TimeoutError:
            logger.warning(
                "[ghost:%s] stream lock stuck >8s — force-closing stale stream",
                self.account,
            )
            stale = self._active_response
            if stale is not None:
                stale.force_close()
            # The force-close released it; grab it for real.
            try:
                await asyncio.wait_for(self._stream_lock.acquire(), timeout=2.0)
            except asyncio.TimeoutError:
                logger.error("[ghost:%s] could not reclaim stream lock — resetting", self.account)
                self._stream_lock = asyncio.Lock()
                await self._stream_lock.acquire()

        def _release() -> None:
            # Single release point, guarded so a double close can't over-release.
            if self._stream_lock.locked():
                self._stream_lock.release()
            self._pipe_q = None
            self._active_response = None

        try:
            queue: asyncio.Queue = asyncio.Queue()
            self._pipe_q = queue
            task = asyncio.create_task(self._page.evaluate(JS_STREAM, {
                "url": "/api/v0/chat/completion",
                "body": body,
                "token": token,
                "pow": pow_header,
                # Inject the COMPLETE fresh identity sniffed from a real UI send
                # this session (x-device-id, x-hif-dliq, x-hif-leim, x-client-*).
                # A raw fetch() cannot mint these itself — they come from the
                # SPA's request layer, which never runs for an out-of-band call.
                # Omitting them (or replaying a stale partial set) is the
                # fingerprint mismatch DeepSeek bans on.
                "extraHeaders": self._identity,
                "idleMs": int(idle_secs * 1000),
            }))
            resp = GhostStreamResponse(self._page, task, queue, idle_secs, on_close=_release)
            self._active_response = resp
            return resp
        except Exception:
            _release()
            raise

    def cancel_stream(self) -> bool:
        """Out-of-band cancel for /api/chat/stop. Returns True if a stream was
        active and got torn down."""
        resp = self._active_response
        if resp is None:
            return False
        resp.force_close()
        return True


# ----------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------

def ghost_enabled() -> bool:
    """Read `deepseek_use_ghost` from settings.json (default True)."""
    try:
        path = _SYSTEM_DIR / "settings.json"
        if not path.is_file():
            return True
        s = json.loads(path.read_text(encoding="utf-8"))
        val = s.get("deepseek_use_ghost")
        if val is None:
            return True
        return bool(val)
    except Exception:
        return True


def _read_helium_from_settings() -> str | None:
    try:
        path = _SYSTEM_DIR / "settings.json"
        if not path.is_file():
            return None
        s = json.loads(path.read_text(encoding="utf-8"))
        return s.get("helium_binary") or None
    except Exception:
        return None
