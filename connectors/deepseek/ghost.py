"""DeepSeek ghost-tab session — in-page fetch with full device identity.

Why this exists
---------------
Curl sends 6 headers and 0 cookies on /chat/completion. The browser sends
20 headers + full cookies (including x-hif-leim + x-device-id, both minted
by the SPA's patched fetch wrapper). Same JWT walking in two costumes →
DeepSeek's anomaly detector flags the account and silently bans it.

Ghost-injected matches the browser byte-for-byte. Verified 20/20 header
match in deepseek_v9_test.py.

Public API
----------
    session = await GhostSession.for_account("browser-data-acc5")
    result = await session.fetch_completion(token, body, pow_header)
    # result = {"status": int, "raw": str, "ms": int}
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
import time
from pathlib import Path

logger = logging.getLogger("sable.deepseek_ghost")

_SYSTEM_DIR = Path(__file__).resolve().parent.parent.parent / "system"
IDENTITY_STORE = _SYSTEM_DIR / ".deepseek_identity.json"
DEFAULT_HELIUM = "/opt/helium-browser-bin/helium"
IDENTITY_TTL_SEC = 24 * 3600

_LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-blink-features=AutomationControlled",
    "--disable-gpu",
    "--ozone-platform=headless",
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
# Stale lock cleanup (avoid ProcessSingleton failure on relaunch)
# ----------------------------------------------------------------------

def _clean_stale_lock(profile: Path) -> bool:
    """Return True if we're clear to launch. Remove Singleton* only if the
    process the lock points to is dead. If a live process owns it, refuse."""
    lock = profile / "SingletonLock"
    if not lock.exists() and not lock.is_symlink():
        return True
    try:
        target = os.readlink(lock)
        _, _, pid_str = target.rpartition("-")
        pid = int(pid_str)
        try:
            os.kill(pid, 0)
            return False  # alive — do not touch
        except ProcessLookupError:
            pass
        except PermissionError:
            return False
    except Exception:
        pass
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        p = profile / name
        try:
            p.unlink()
        except Exception:
            pass
    return True


# ----------------------------------------------------------------------
# In-page fetch (runs inside the helium page's JS context, credentials
# included automatically → cookies + native TLS + native referer)
# ----------------------------------------------------------------------

JS_FETCH = """
async ({url, body, token, pow, extraHeaders}) => {
  const h = Object.assign({
    'Content-Type': 'application/json',
    'Authorization': 'Bearer ' + token,
    'X-DS-PoW-Response': pow,
  }, extraHeaders || {});
  const t0 = performance.now();
  const res = await fetch(url, {
    method: 'POST', credentials: 'include', headers: h,
    body: JSON.stringify(body),
  });
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let raw = '';
  while (true) {
    const {done, value} = await reader.read();
    if (done) break;
    raw += dec.decode(value, {stream: true});
  }
  return {status: res.status, raw, ms: Math.round(performance.now() - t0)};
}
"""


# ----------------------------------------------------------------------
# Response shim — satisfies the interface _iter_completion_events expects
# ----------------------------------------------------------------------

class GhostResponse:
    """Async context manager mimicking curl_cffi's streamed Response.
    Supports: __aenter__/__aexit__, .status_code, await .content, .aiter_lines()."""

    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self._text = text

    @property
    async def content(self) -> bytes:
        return self._text.encode("utf-8")

    async def aiter_lines(self):
        for line in self._text.split("\n"):
            yield line

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


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
        self._identity: dict[str, str] = {}
        self._started_at: float = 0.0

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
        from playwright.async_api import async_playwright

        if not self.profile_dir.exists():
            raise RuntimeError(f"profile dir missing: {self.profile_dir}")
        if not _clean_stale_lock(self.profile_dir):
            raise RuntimeError(f"profile locked by live process: {self.profile_dir}")

        self._playwright = await async_playwright().start()
        self._ctx = await self._playwright.chromium.launch_persistent_context(
            executable_path=self.helium_bin,
            user_data_dir=str(self.profile_dir),
            headless=True,
            args=_LAUNCH_ARGS,
        )
        self._page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
        await self._page.goto("https://chat.deepseek.com/",
                              wait_until="domcontentloaded", timeout=60000)
        await self._page.wait_for_timeout(3000)
        self._started_at = time.time()

        cached = self._load_identity()
        if cached:
            self._identity = cached
            logger.info("[ghost:%s] identity loaded from cache (%d headers, hif len %d)",
                        self.account, len(cached), len(cached.get("x-hif-leim", "")))
        else:
            logger.info("[ghost:%s] no cached identity — sniffing via one UI send...",
                        self.account)
            await self._sniff_identity()

    async def close(self) -> None:
        try:
            if self._ctx is not None:
                await self._ctx.close()
        except Exception:
            pass
        try:
            if self._playwright is not None:
                await self._playwright.stop()
        except Exception:
            pass
        self._page = None
        self._ctx = None
        self._playwright = None

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
        """Drive a real UI send once, capture the full UI-only header set."""
        captured: list[dict] = []

        async def on_route(route):
            req = route.request
            if "/api/v0/chat/completion" in req.url and req.method == "POST":
                try:
                    captured.append(dict(req.headers))
                except Exception:
                    pass
            await route.continue_()

        await self._page.route("**/api/v0/chat/completion", on_route)
        try:
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
                await self._page.unroute("**/api/v0/chat/completion", on_route)
            except Exception:
                pass

        if not captured:
            raise RuntimeError("identity sniff failed — no completion POST captured")
        ui_headers = {(k or "").lower(): v for k, v in captured[0].items()}
        injected = {k: v for k, v in ui_headers.items() if k not in _SKIP_INJECT}
        if "x-hif-leim" not in injected or "x-device-id" not in injected:
            raise RuntimeError(
                f"identity sniff incomplete — hif={'x-hif-leim' in injected} "
                f"dev={'x-device-id' in injected}"
            )
        self._identity = injected
        self._save_identity(injected)
        logger.info("[ghost:%s] sniffed %d identity headers (hif len %d)",
                    self.account, len(injected), len(injected.get("x-hif-leim", "")))

    # -- send ----------------------------------------------------------

    async def fetch_completion(
        self, token: str, body: dict, pow_header: str,
    ) -> dict:
        """Fire a ghost fetch of /chat/completion. Returns {status, raw, ms}."""
        if self._page is None:
            raise RuntimeError("ghost session not started")
        if self._page.is_closed():
            raise RuntimeError("ghost page closed")
        return await asyncio.wait_for(
            self._page.evaluate(JS_FETCH, {
                "url": "/api/v0/chat/completion",
                "body": body,
                "token": token,
                "pow": pow_header,
                "extraHeaders": self._identity,
            }),
            timeout=300,
        )


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
