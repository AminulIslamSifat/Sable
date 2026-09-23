"""Kimi ghost-tab session — in-page gRPC-Web/Connect fetch with browser identity.

Why this exists
---------------
kimi.ai exposes its chat backend as **gRPC-Web over the Connect protocol**:

    POST https://www.kimi.ai/apiv2/kimi.gateway.chat.v1.ChatService/Chat
    Content-Type: application/connect+json

Two things block a plain curl/httpx client:

  1. ``x-msh-shield-data`` — a per-request anti-bot token minted by the SPA's
     obfuscated shield SDK. We don't reverse it; the page mints it for us.
  2. The bearer JWT lives in ``localStorage['access_token']`` (NOT a cookie) and
     expires in 900s. The SPA refreshes it silently; a curl client would need
     its own refresh loop.

Ghost-injected fetch runs same-origin inside a real headless Helium tab, so
both problems vanish: the page supplies the token AND the shield, per request.

Wire format (Connect streaming)
-------------------------------
Every message — request and response alike — is length-prefixed:

    [flag:1][length:4 big-endian][payload:length]

  * request: a single frame, flag=0
  * response: many frames. flag 0 = data, flag 2 = end-of-stream.

Payloads are JSON. The response is an event stream of ``{op, mask, block|message}``
ops (see ``client.py`` for the op→event mapping).

Streaming model (mirrors connectors/deepseek/ghost.py)
------------------------------------------------------
Every decoded frame is pushed to Python through the exposed ``window.__ghostEvent``
bridge, feeding a per-request asyncio.Queue. A 30s gap with no event — connect or
mid-stream — aborts the in-page fetch AND raises asyncio.TimeoutError from the
drain, so the caller can drop and reopen.

Public API
----------
    session = await KimiGhostSession.for_account("browser-data-acc5")
    resp = await session.open_stream(body, idle_secs=30)
    async with resp as r:
        async for flag, text in r.aiter_frames():
            ...
    await KimiGhostSession.close_all()

Settings (system/settings.json)
-------------------------------
    kimi_use_ghost: bool  (default True)
    helium_binary:  str   (default /opt/helium-browser-bin/helium)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import socket
import time
from pathlib import Path

logger = logging.getLogger("sable.kimi_ghost")

_SYSTEM_DIR = Path(__file__).resolve().parent.parent.parent / "system"
DEFAULT_HELIUM = "/opt/helium-browser-bin/helium"
KIMI_URL = "https://www.kimi.ai/"
CHAT_PATH = "/apiv2/kimi.gateway.chat.v1.ChatService/Chat"

_LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-blink-features=AutomationControlled",
    "--disable-gpu",
    "--ozone-platform=headless",
]

# Headers the page sets itself, or that vary per request — never injected.
_SKIP_INJECT = frozenset({
    "authorization", "content-length", "host", "connection",
    "content-type", "cookie", "referer",
})


# ----------------------------------------------------------------------
# In-page streaming fetch (Connect framing handled in JS)
# ----------------------------------------------------------------------

JS_STREAM = r"""
async ({url, body, extraHeaders, idleMs}) => {
  try {
    const token = localStorage.getItem('access_token');
    if (!token) { await window.__ghostEvent({t:'error', err:'no_access_token'}); return 'err'; }

    // Frame the request: [flag=0][len:4 BE][json]
    const enc = new TextEncoder();
    const payload = enc.encode(JSON.stringify(body));
    const framed = new Uint8Array(5 + payload.length);
    framed[0] = 0;
    framed[1] = (payload.length >>> 24) & 0xff;
    framed[2] = (payload.length >>> 16) & 0xff;
    framed[3] = (payload.length >>> 8) & 0xff;
    framed[4] = payload.length & 0xff;
    framed.set(payload, 5);

    const h = Object.assign({
      'Content-Type': 'application/connect+json',
      'connect-protocol-version': '1',
      'Authorization': 'Bearer ' + token,
      'x-msh-device-id': localStorage.getItem('__snaker__id') || '',
      'x-msh-platform': 'web',
      'x-msh-version': '2.3.0',
      'x-language': 'en-US',
      'r-timezone': 'Asia/Dhaka',
    }, extraHeaders || {});

    const ctrl = new AbortController();
    let idleTimer = setTimeout(() => ctrl.abort(), idleMs);
    const res = await fetch(url, {
      method: 'POST', credentials: 'include', headers: h,
      body: framed, signal: ctrl.signal,
    });
    await window.__ghostEvent({t: 'status', status: res.status});
    if (!res.body) { await window.__ghostEvent({t: 'done'}); return 'nobody'; }

    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = new Uint8Array(0);
    try {
      while (true) {
        clearTimeout(idleTimer);
        idleTimer = setTimeout(() => ctrl.abort(), idleMs);
        const {done, value} = await reader.read();
        if (done) break;
        const merged = new Uint8Array(buf.length + value.length);
        merged.set(buf); merged.set(value, buf.length); buf = merged;
        while (buf.length >= 5) {
          const flag = buf[0];
          const len = (buf[1]<<24)|(buf[2]<<16)|(buf[3]<<8)|buf[4];
          if (buf.length < 5 + len) break;
          await window.__ghostEvent({t:'frame', flag: flag, data: dec.decode(buf.slice(5, 5+len))});
          buf = buf.slice(5 + len);
        }
      }
    } finally {
      clearTimeout(idleTimer);
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
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _clear_singleton(profile: Path) -> None:
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            (profile / name).unlink(missing_ok=True)
        except OSError:
            pass


# ----------------------------------------------------------------------
# Streaming response shim
# ----------------------------------------------------------------------

class KimiStreamResponse:
    """Async context manager yielding decoded Connect frames.

    Frames arrive from a per-request asyncio.Queue fed by the in-page
    ``window.__ghostEvent`` bridge. ``aiter_frames()`` yields ``(flag, text)``
    tuples as they arrive; a gap longer than ``idle_secs`` raises
    asyncio.TimeoutError.
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
        self._done = False
        self._drained = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        if not self._task.done():
            self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass
        if not self._closed and self._on_close is not None:
            self._closed = True
            self._on_close()
        return False

    @property
    async def content(self) -> bytes:
        if not self._drained:
            async for _ in self.aiter_frames():
                pass
        return b""

    async def aiter_frames(self):
        """Yield (flag, text) for every response frame. flag 2 = stream end."""
        while not self._done:
            try:
                ev = await asyncio.wait_for(self._q.get(), timeout=self._idle)
            except asyncio.TimeoutError:
                raise asyncio.TimeoutError(
                    f"kimi ghost idle >{self._idle:.0f}s (connect or mid-stream stall)")
            t = ev.get("t")
            if t == "status":
                try:
                    self.status_code = int(ev.get("status", 200))
                except Exception:
                    self.status_code = 200
                continue
            if t == "frame":
                flag = int(ev.get("flag", 0))
                text = ev.get("data", "")
                if flag == 2:
                    self._done = True
                    yield (flag, text)
                    break
                yield (flag, text)
                continue
            if t == "error":
                self._done = True
                raise RuntimeError(f"kimi ghost stream error: {ev.get('err')}")
            if t == "done":
                self._done = True
                break
        self._drained = True


# ----------------------------------------------------------------------
# The session
# ----------------------------------------------------------------------

class KimiGhostSession:
    _registry: dict[str, "KimiGhostSession"] = {}
    _registry_lock: asyncio.Lock | None = None

    def __init__(self, account: str, helium_bin: str, profile_dir: Path):
        self.account = account
        self.helium_bin = helium_bin
        self.profile_dir = profile_dir
        self._playwright = None
        self._ctx = None
        self._page = None
        self._cdp_browser = None
        self._exposed_pages: set = set()
        self._pipe_q: asyncio.Queue | None = None
        self._stream_lock = asyncio.Lock()
        self._marker = profile_dir / ".kimi_ghost.json"

    # -- lifecycle -----------------------------------------------------

    @classmethod
    async def for_account(cls, account: str, helium_bin: str | None = None) -> "KimiGhostSession":
        """Return a running session for ``account``, respawning if dead."""
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
            session = cls(account, bin_path, profile)
            await session.start()
            cls._registry[account] = session
            return session

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
        if await self._adopt_existing():
            return
        await self._ensure_profile_free()
        await self._launch()

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
        await self._page.goto(KIMI_URL, wait_until="domcontentloaded", timeout=60000)
        await self._page.wait_for_timeout(2000)
        try:
            self._marker.write_text(json.dumps({"port": port, "ts": time.time()}))
        except OSError:
            pass
        logger.info("[kimi:%s] launched headless Helium (port %d)", self.account, port)

    async def _adopt_existing(self) -> bool:
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
            logger.info("[kimi:%s] adopted existing browser over CDP (port %d)", self.account, port)
            return True
        except Exception as exc:
            logger.info("[kimi:%s] CDP adopt failed (%s) — relaunching", self.account, exc)
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
        """Free the profile for a fresh launch.

        A live browser on the profile that we could NOT adopt over CDP is
        presumed to belong to ANOTHER connector (e.g. the DeepSeek ghost, or a
        real user session) sharing this profile dir. Killing it would start a
        kill-war between the two ghosts — each only adopts its own marker file,
        so each sees the other as a foreign orphan. We therefore REFUSE to
        touch a live foreign browser: clear only a stale lock, or kill a pid
        that is already dead. If a live browser is truly ours-but-unadoptable,
        the launch will fail loudly and the operator can clear it.
        """
        pid = _lock_pid(self.profile_dir)
        if pid is not None and _pid_alive(pid):
            raise RuntimeError(
                f"profile {self.profile_dir.name} is held by live browser pid {pid} "
                f"that we could not adopt over CDP — refusing to kill a foreign "
                f"browser. Use a dedicated profile per connector.")
        # No live owner — the lock is stale. Safe to clear.
        _clear_singleton(self.profile_dir)
        try:
            self._marker.unlink(missing_ok=True)
        except OSError:
            pass

    async def close(self) -> None:
        for closer in (self._cdp_browser, self._ctx, self._playwright):
            try:
                if closer is not None:
                    await closer.close() if closer is not self._playwright else await closer.stop()
            except Exception:
                pass
        self._page = self._ctx = self._playwright = self._cdp_browser = None

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

    # -- send ----------------------------------------------------------

    def _on_event(self, ev: dict) -> None:
        if self._pipe_q is not None:
            self._pipe_q.put_nowait(ev)

    async def _ensure_bridge(self) -> None:
        if self._page is None:
            raise RuntimeError("kimi ghost session not started")
        if self._page.is_closed():
            raise RuntimeError("kimi ghost page closed")
        if self._page not in self._exposed_pages:
            await self._page.expose_function("__ghostEvent", self._on_event)
            self._exposed_pages.add(self._page)

    async def has_token(self) -> bool:
        """True if the page currently holds a login token in localStorage."""
        try:
            await self._ensure_bridge()
            return bool(await self._page.evaluate("() => !!localStorage.getItem('access_token')"))
        except Exception:
            return False

    async def open_stream(self, body: dict, idle_secs: float = 30.0) -> KimiStreamResponse:
        """Open a streaming in-page Connect fetch. Serialized per session so the
        shared event bridge stays coherent (one active stream at a time)."""
        await self._ensure_bridge()
        await self._stream_lock.acquire()

        def _release() -> None:
            if self._stream_lock.locked():
                self._stream_lock.release()
            self._pipe_q = None

        try:
            queue: asyncio.Queue = asyncio.Queue()
            self._pipe_q = queue
            task = asyncio.create_task(self._page.evaluate(JS_STREAM, {
                "url": CHAT_PATH,
                "body": body,
                "extraHeaders": {},
                "idleMs": int(idle_secs * 1000),
            }))
            return KimiStreamResponse(self._page, task, queue, idle_secs, on_close=_release)
        except Exception:
            _release()
            raise


# ----------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------

def kimi_ghost_enabled() -> bool:
    """Read ``kimi_use_ghost`` from settings.json (default True)."""
    try:
        path = _SYSTEM_DIR / "settings.json"
        if not path.is_file():
            return True
        s = json.loads(path.read_text(encoding="utf-8"))
        val = s.get("kimi_use_ghost")
        return True if val is None else bool(val)
    except Exception:
        return True


def _read_helium_from_settings() -> str | None:
    try:
        path = _SYSTEM_DIR / "settings.json"
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8")).get("helium_binary")
    except Exception:
        pass
    return None
