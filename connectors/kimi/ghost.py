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

The browser binary is resolved per account from system/accounts.json
(browser_path). If the profile has no saved path, Playwright's bundled
Chromium is used.
"""
from __future__ import annotations
from engine.config import PERSISTENT_ROOT  # frozen-build writable root

import asyncio
import base64
import json
import logging
import os
import signal
import socket
import time
from pathlib import Path

from engine import process_utils
from engine.platform_paths import pid_exists

logger = logging.getLogger("sable.kimi_ghost")

_SYSTEM_DIR = PERSISTENT_ROOT / "system"
TOKEN_STORE_PATH = _SYSTEM_DIR / ".kimi_tokens.json"
KIMI_URL = "https://www.kimi.ai/"
CHAT_PATH = "/apiv2/kimi.gateway.chat.v1.ChatService/Chat"


# ----------------------------------------------------------------------
# Per-account token persistence (mirrors DeepSeek's .deepseek_tokens.json)
# ----------------------------------------------------------------------

def _load_token_store() -> dict[str, str]:
    """Load {account: jwt} map from disk."""
    try:
        if TOKEN_STORE_PATH.exists():
            return json.loads(TOKEN_STORE_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Could not load kimi token store: %s", exc)
    return {}


def _save_token_store(store: dict[str, str]) -> None:
    try:
        TOKEN_STORE_PATH.write_text(
            json.dumps(store, indent=2), encoding="utf-8"
        )
    except Exception as exc:
        logger.warning("Could not save kimi token store: %s", exc)


def save_token_for_account(token: str, account: str) -> None:
    """Persist a fresh JWT for ``account`` so cold starts don't need the SPA."""
    store = _load_token_store()
    store[account] = token
    _save_token_store(store)


def get_saved_token(account: str) -> str | None:
    """Return the last-saved JWT for ``account``, or None."""
    store = _load_token_store()
    token = store.get(account)
    if token and _jwt_not_expired(token):
        return token
    return None

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
async ({url, body, extraHeaders, idleMs, rid}) => {
  try {
    const token = localStorage.getItem('access_token');
    if (!token) { await window.__ghostEvent({t:'error', err:'no_access_token', rid: rid}); return 'err'; }

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
    await window.__ghostEvent({t: 'status', status: res.status, rid: rid});
    if (!res.body) { await window.__ghostEvent({t: 'done', rid: rid}); return 'nobody'; }

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
          await window.__ghostEvent({t:'frame', flag: flag, data: dec.decode(buf.slice(5, 5+len)), rid: rid});
          buf = buf.slice(5 + len);
        }
      }
    } finally {
      clearTimeout(idleTimer);
    }
    await window.__ghostEvent({t: 'done', rid: rid});
    return 'ok';
  } catch (e) {
    await window.__ghostEvent({t: 'error', err: String(e), rid: rid});
    return 'err';
  }
}
"""


# ----------------------------------------------------------------------
# Lock helpers — distinguish "our orphan" from a live foreign browser
# ----------------------------------------------------------------------

def _jwt_not_expired(token: str, skew: int = 30) -> bool:
    """True if ``token`` is a JWT whose ``exp`` is still in the future.

    Returns False for a malformed token or one already expired (with a small
    clock-skew margin). Kimi's bearer is a standard JWT, so the ``exp`` claim
    is the authoritative "is this still usable" signal — unlike mere presence.
    """
    try:
        parts = token.split('.')
        if len(parts) < 2:
            return False
        payload = parts[1]
        payload += '=' * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload).decode('utf-8', 'replace'))
        exp = claims.get('exp')
        if not isinstance(exp, (int, float)):
            # No exp claim — can't prove it's live; treat as usable.
            return True
        return exp > (time.time() + skew)
    except Exception:
        return False


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


def _pid_is_our_helium(pid: int, profile_dir: Path, helium_bin: str) -> bool:
    """True if ``pid`` is a Helium browser launched on *our* profile dir.

    We identify our own orphan by process cmdline rather than by the marker
    file, because the marker can be lost (crash before it was written, or an
    adopt-failure unlinking it while the browser stays alive). Requires BOTH
    the binary basename AND the exact ``--user-data-dir`` to match, so a
    foreign connector's browser sharing the profile is never touched.
    """
    # No binary to match against (e.g. a profile set to "default" -> Playwright
    # bundled Chromium with no explicit path). Never claim a pid is ours.
    if not helium_bin:
        return False
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    parts = [p for p in raw.split(b"\0") if p]
    if not parts:
        return False
    try:
        exe = parts[0].decode()
    except Exception:
        return False
    if Path(exe).name != Path(helium_bin).name:
        return False
    want = {
        f"--user-data-dir={profile_dir}".encode(),
        f"--user-data-dir={profile_dir.resolve()}".encode(),
    }
    return any(p in want for p in parts)


def _profile_helium_pids(profile_dir: Path, helium_bin: str) -> list[int]:
    """Every Helium pid bound to ``profile_dir`` (main + forked children)."""
    out: list[int] = []
    proc = Path("/proc")
    if not proc.is_dir():
        return out
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            pid = int(entry.name)
            if _pid_is_our_helium(pid, profile_dir, helium_bin):
                out.append(pid)
        except Exception:
            continue
    return out


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

    def __init__(self, account: str, helium_bin: str, profile_dir: Path,
                 headless: bool = False):
        self.account = account
        self.helium_bin = helium_bin
        self.profile_dir = profile_dir
        # Headed (CHAT — needs a real window for shield minting) vs headless
        # (TOKEN REFRESH only — never spawns a visible browser).
        self.headless = headless
        self._playwright = None
        self._ctx = None
        self._page = None
        self._cdp_browser = None
        self._exposed_pages: set = set()
        self._pipe_q: asyncio.Queue | None = None
        self._active_rid: str | None = None
        self._stream_lock = asyncio.Lock()
        self._marker = profile_dir / ".kimi_ghost.json"
        self._hub = None

    # -- lifecycle -----------------------------------------------------

    @classmethod
    async def for_account(cls, account: str,
                          headless: bool = False) -> "KimiGhostSession":
        """Return a running session for ``account``, respawning if dead.

        ``headless=True`` is the TOKEN-REFRESH path: never spawns a visible
        window. ``headless=False`` (default) is the CHAT path, which needs a
        real headed Helium for shield minting.

        A live session is reused as-is, with ONE exception: a caller asking for
        HEADED (chat) when the cached session is HEADLESS upgrades it, because
        chat cannot run headless. A headless refresh happily reuses an already
        -headed chat browser.
        """
        if cls._registry_lock is None:
            cls._registry_lock = asyncio.Lock()
        async with cls._registry_lock:
            existing = cls._registry.get(account)
            if existing is not None and await existing.is_healthy():
                if headless or not existing.headless:
                    return existing
                # Chat wants headed but the live session is headless — replace.
                await existing.close()
            elif existing is not None:
                await existing.close()
            from engine.platform_paths import resolve_browser_for_profile
            # Honor the per-account browser set in accounts.json (Chrome/Helium/
            # any Chromium). No saved path -> Playwright bundled Chromium.
            bin_path = resolve_browser_for_profile(account)
            profile = _SYSTEM_DIR / account
            # Kill-and-retry: a closed/externally-killed/stale browser context
            # makes start() blow up hard. On failure evict the hub, kill every
            # browser on this profile, and try again — max 3 attempts.
            from connectors.common.profile_hub import (
                ProfileHub as _PH, _kill_profile_holders as _kill,
            )
            last_exc: Exception | None = None
            for attempt in range(1, 4):
                session = cls(account, bin_path, profile, headless=headless)
                try:
                    await session.start()
                    cls._registry[account] = session
                    return session
                except Exception as exc:
                    last_exc = exc
                    logger.warning(
                        "[kimi:%s] start attempt %d/3 failed: %s: %s — "
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
                        logger.warning("[kimi:%s] profile kill failed: %s",
                                       account, kill_exc)
                    if attempt < 3:
                        await asyncio.sleep(min(0.5 * attempt, 2.0))
            raise RuntimeError(
                f"kimi:{account}: session failed to start after 3 attempts"
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
        # Shared per-profile browser (ProfileHub): multiplex this connector's
        # origin tab onto the ONE browser that owns the profile dir, so Kimi +
        # DeepSeek + Gemini-web coexist on the same profile without the
        # SingletonLock kill-war. See connectors/common/profile_hub.py.
        from connectors.common.profile_hub import ProfileHub
        # CHAT (headless=False) needs a real visible Helium window for shield
        # minting, so force_headed=True promotes/replaces a headless hub and
        # clears stale holders. TOKEN REFRESH (headless=True) must NOT kill
        # anything or spawn a visible window: force_headed=False reuses a live
        # hub if present, else launches a headless browser.
        self._hub = await ProfileHub.acquire(
            self.profile_dir, self.helium_bin,
            headless=self.headless,
            force_headed=not self.headless)
        self._ctx = self._hub.context
        self._page = await self._hub.new_tab(KIMI_URL)
        logger.info("[kimi:%s] tab ready on shared browser (%s)",
                    self.account, self.profile_dir.name)

    async def _launch(self) -> None:
        from playwright.async_api import async_playwright

        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()

        # Write the marker BEFORE launch. If we crash (or get killed) mid-launch,
        # the marker still records which port we own, so the next start can
        # adopt over CDP instead of seeing a live lock with no way to identify
        # it. The post-launch state is identical, so this is strictly safer.
        try:
            self._marker.write_text(json.dumps({"port": port, "ts": time.time()}))
        except OSError:
            pass

        try:
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
        except Exception:
            # If the browser context never came up, drop the marker so the next
            # start doesn't try to adopt a port that was never bound. If it DID
            # come up (failure was later, e.g. goto), keep the marker so the
            # next start can adopt the live browser instead of orphaning it.
            if self._ctx is None:
                try:
                    self._marker.unlink(missing_ok=True)
                except OSError:
                    pass
            raise
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
            # Is the live browser OUR OWN orphan? We can tell from the process
            # cmdline (our helium binary + our exact profile dir). That happens
            # when the marker was lost — crash before it was written, or an
            # adopt-failure that unlinked it while the browser stayed alive.
            # Reclaim it instead of refusing.
            if _pid_is_our_helium(pid, self.profile_dir, self.helium_bin):
                logger.info("[kimi:%s] reclaiming our orphaned browser pid %d",
                            self.account, pid)
                await self._reclaim_orphans()
                return
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

    async def _reclaim_orphans(self) -> None:
        """Kill every Helium process bound to our profile dir, then clear the lock.

        Sweeps the whole process tree (Chromium forks renderers/GPU procs), not
        just the SingletonLock pid, so nothing is left holding the dir.
        """
        victims = _profile_helium_pids(self.profile_dir, self.helium_bin)
        for vp in victims:
            process_utils.kill_process_tree(vp)
        for _ in range(30):
            if not any(_pid_alive(vp) for vp in victims):
                break
            await asyncio.sleep(0.1)
        for vp in victims:
            if _pid_alive(vp):
                process_utils.kill_process_tree(vp, sig=process_utils.SIGKILL)
        await asyncio.sleep(0.2)
        _clear_singleton(self.profile_dir)
        try:
            self._marker.unlink(missing_ok=True)
        except OSError:
            pass
        logger.info("[kimi:%s] reclaimed %d orphaned process(es)",
                    self.account, len(victims))

    async def close(self) -> None:
        # Only this connector's tab dies; the shared browser (owned by the
        # ProfileHub) stays up for other connectors on the same profile.
        if self._hub is not None and self._page is not None:
            try:
                await self._hub.close_tab(self._page)
            except Exception:
                pass
        self._page = self._ctx = self._playwright = self._cdp_browser = None
        self._hub = None
        self._exposed_pages.clear()

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
        # Drop events from a previous stream that are still draining through the
        # bridge — otherwise a stale 'done' poisons the next request's queue.
        if self._pipe_q is None:
            return
        if self._active_rid is not None and ev.get("rid") != self._active_rid:
            return
        self._pipe_q.put_nowait(ev)

    async def _ensure_bridge(self) -> None:
        if self._page is None:
            raise RuntimeError("kimi ghost session not started")
        if self._page.is_closed():
            raise RuntimeError("kimi ghost page closed")
        if self._page not in self._exposed_pages:
            await self._page.expose_function("__ghostEvent", self._on_event)
            self._exposed_pages.add(self._page)

    async def inject_saved_token(self) -> bool:
        """Inject a previously-saved JWT into the page's localStorage.

        Called on cold start BEFORE the SPA hydrates, so the ghost session
        immediately has a usable token without waiting for the SPA's silent
        refresh flow. Returns True if injection succeeded.
        """
        saved = get_saved_token(self.account)
        if not saved:
            return False
        try:
            await self._ensure_bridge()
            await self._page.evaluate(
                "(tok) => localStorage.setItem('access_token', tok)", saved
            )
            logger.info("[kimi:%s] injected saved token from disk", self.account)
            return True
        except Exception as exc:
            logger.warning("[kimi:%s] failed to inject saved token: %s",
                           self.account, exc)
            return False

    async def has_token(self, wait_secs: float = 8.0, poll: float = 1.0) -> bool:
        """True if the page holds a *non-expired* login token in localStorage.

        Kimi's bearer JWT expires ~900s after issue; the SPA silently refreshes
        it while the page is alive. A bare presence check reports "logged in"
        for an expired token — which is exactly what made the refresh endpoint
        lie. We decode the JWT ``exp`` claim and reject anything expired.

        On server restart the SPA needs time to hydrate its auth state (silent
        refresh via cookies). ``wait_secs`` gives the page up to that many
        seconds to produce a valid token before we give up.

        When a valid token IS found, it is automatically persisted to disk
        so future cold starts can inject it without waiting for the SPA.
        """
        try:
            await self._ensure_bridge()
        except Exception:
            return False

        # Fast path: try injecting a saved token first
        await self.inject_saved_token()

        deadline = time.monotonic() + wait_secs
        while True:
            try:
                token = await self._page.evaluate(
                    "() => localStorage.getItem('access_token')"
                )
                if token and _jwt_not_expired(token):
                    # Persist to disk whenever we find a live token
                    save_token_for_account(token, self.account)
                    return True
            except Exception:
                pass

            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(poll)

    async def open_stream(self, body: dict, idle_secs: float = 30.0) -> KimiStreamResponse:
        """Open a streaming in-page Connect fetch. Serialized per session so the
        shared event bridge stays coherent (one active stream at a time)."""
        await self._ensure_bridge()
        await self._stream_lock.acquire()

        def _release() -> None:
            if self._stream_lock.locked():
                self._stream_lock.release()
            self._pipe_q = None
            self._active_rid = None

        try:
            import uuid as _uuid
            rid = _uuid.uuid4().hex
            queue: asyncio.Queue = asyncio.Queue()
            self._pipe_q = queue
            self._active_rid = rid
            task = asyncio.create_task(self._page.evaluate(JS_STREAM, {
                "url": CHAT_PATH,
                "body": body,
                "extraHeaders": {},
                "idleMs": int(idle_secs * 1000),
                "rid": rid,
            }))
            return KimiStreamResponse(self._page, task, queue, idle_secs, on_close=_release)
        except Exception:
            _release()
            raise

    # -- file upload ---------------------------------------------------

    async def upload_file(self, name: str, mime: str, data: bytes) -> dict:
        """Upload a file via in-page multipart POST (browser identity).

        The file bytes are base64'd into the page, rebuilt as a Blob, and
        posted with FormData so the SPA's own auth/shield headers apply.
        Returns the parsed JSON ``{"file": {id, meta, blob}}`` response.
        """
        import base64 as _b64

        await self._ensure_bridge()
        if self._page is None:
            raise RuntimeError("kimi ghost session not started")
        b64 = _b64.b64encode(data).decode("ascii")
        result = await self._page.evaluate(JS_UPLOAD, {
            "url": "/apiv2-files/file/upload",
            "name": name,
            "mime": mime,
            "b64": b64,
        })
        if not isinstance(result, dict) or not result.get("ok"):
            raise RuntimeError(f"kimi upload failed: {result}")
        try:
            return json.loads(result.get("body") or "{}")
        except Exception as exc:
            raise RuntimeError(f"kimi upload bad JSON: {exc}")

    async def connect_json(self, path: str, body: dict) -> list[dict]:
        """Send a Connect-framed JSON request in-page and return decoded frames.

        Used for small control calls (GetFileParseProgress, CancelChat) that
        ride the same protocol as Chat but aren't streaming.
        """
        await self._ensure_bridge()
        if self._page is None:
            raise RuntimeError("kimi ghost session not started")
        result = await self._page.evaluate(JS_CONNECT, {"url": path, "body": body})
        if not isinstance(result, dict) or not result.get("ok"):
            raise RuntimeError(f"kimi connect call failed: {result}")
        out: list[dict] = []
        for fr in result.get("frames", []):
            if int(fr.get("flag", 0)) == 2:
                continue
            try:
                out.append(json.loads(fr.get("text") or "{}"))
            except Exception:
                pass
        return out

    async def cancel_chat(self, chat_id: str, message_id: str) -> bool:
        """Stop generation server-side (the UI 'stop' button)."""
        frames = await self.connect_json(
            "/apiv2/kimi.gateway.chat.v1.ChatService/CancelChat",
            {"chat_id": chat_id, "message_id": message_id},
        )
        # A successful cancel returns an empty/ack body; treat clean frames as OK.
        return True


# ----------------------------------------------------------------------
# In-page fetch snippets for control calls
# ----------------------------------------------------------------------

JS_UPLOAD = r"""
async ({url, name, mime, b64}) => {
  try {
    const token = localStorage.getItem('access_token');
    if (!token) return {ok:false, err:'no_access_token'};
    const bin = atob(b64);
    const arr = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
    const blob = new Blob([arr], {type: mime});
    const fd = new FormData();
    fd.append('file', blob, name);
    const h = {
      'Authorization': 'Bearer ' + token,
      'x-msh-device-id': localStorage.getItem('__snaker__id') || '',
      'x-msh-platform': 'web',
      'x-msh-version': '2.3.0',
    };
    const res = await fetch(url, {method:'POST', credentials:'include', headers:h, body: fd});
    const txt = await res.text();
    return {ok:true, status: res.status, body: txt};
  } catch(e) { return {ok:false, err:String(e)}; }
}
"""


JS_CONNECT = r"""
async ({url, body}) => {
  try {
    const token = localStorage.getItem('access_token');
    if (!token) return {ok:false, err:'no_access_token'};
    const enc = new TextEncoder();
    const payload = enc.encode(JSON.stringify(body));
    const framed = new Uint8Array(5 + payload.length);
    framed[0] = 0;
    framed[1] = (payload.length >>> 24) & 0xff;
    framed[2] = (payload.length >>> 16) & 0xff;
    framed[3] = (payload.length >>> 8) & 0xff;
    framed[4] = payload.length & 0xff;
    framed.set(payload, 5);
    const h = {
      'Content-Type': 'application/connect+json',
      'connect-protocol-version': '1',
      'Authorization': 'Bearer ' + token,
      'x-msh-device-id': localStorage.getItem('__snaker__id') || '',
      'x-msh-platform': 'web',
      'x-msh-version': '2.3.0',
      'x-language': 'en-US',
      'r-timezone': 'Asia/Dhaka',
    };
    const res = await fetch(url, {method:'POST', credentials:'include', headers:h, body: framed});
    const buf = new Uint8Array(await res.arrayBuffer());
    const dec = new TextDecoder();
    const out = [];
    let off = 0;
    while (off + 5 <= buf.length) {
      const flag = buf[off];
      const len = (buf[off+1]<<24)|(buf[off+2]<<16)|(buf[off+3]<<8)|buf[off+4];
      if (off + 5 + len > buf.length) break;
      out.push({flag: flag, text: dec.decode(buf.slice(off+5, off+5+len))});
      off += 5 + len;
    }
    return {ok:true, status: res.status, frames: out};
  } catch(e) { return {ok:false, err:String(e)}; }
}
"""


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



