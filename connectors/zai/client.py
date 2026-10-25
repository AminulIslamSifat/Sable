"""z.ai (GLM) connector for Sable — browser-assisted.

z.ai's POST /api/v2/chat/completions requires three gates:
  1. X-Signature         — HMAC-SHA256 (two-level, hardcoded key)
  2. captcha_verify_param — Aliyun TRACELESS token (auto-passes, no puzzle)
  3. Bearer JWT          — from localStorage

The Aliyun captcha SDK (initAliyunCaptcha) only runs in a real browser page, so
this connector runs the whole flow in-page:

    create chat -> mint captcha -> sign -> POST completions -> parse SSE

Deltas stream back to Python via an exposed binding, so Sable gets a real
token-by-token stream.

Profile sharing: Chromium allows only ONE process per user_data_dir. Sable's
ChatService already owns the active account profile, so this client accepts an
injected `page_provider` and runs inside that same context. A standalone
browser is used only as a fallback when no provider is wired.

Yields Sable-compatible events:
  {"type": "answer",   "text": "..."}
  {"type": "thinking", "text": "..."}
  {"type": "done",     "parent_id": "..."}
  {"type": "error",    "message": "..."}
"""

from __future__ import annotations
from engine.config import PERSISTENT_ROOT  # frozen-build writable root

import asyncio
import json
import logging
import os
import tempfile
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger("sable.zai_api")

BASE_URL = "https://chat.z.ai"
_SYSTEM_DIR = PERSISTENT_ROOT / "system"
TOKEN_STORE_PATH = _SYSTEM_DIR / ".zai_tokens.json"
MAX_TOKENS_PER_ACCOUNT = 10

_BINDING = "__sableZaiChunk"


# ---------------------------------------------------------------------------
# Instruction context (system prompt) — mirrors deepseek/client.py
# ---------------------------------------------------------------------------
_instruction_cache: str | None = None
_cached_project_id: str | None = "__none__"
_cached_layout_mode: str | None = "__none__"
_cached_version: int = -1


def _load_instructions(project_id: str | None = None, layout_mode: str | None = None) -> str:
    """Load instruction context for first-message injection. Project-aware.

    Cached and invalidated on project / layout / version change, exactly like
    the DeepSeek connector so both providers ship identical system prompts.
    """
    global _instruction_cache, _cached_project_id, _cached_version, _cached_layout_mode
    try:
        from connectors.common.instruction_builder import build_instructions, get_instruction_version
    except Exception:
        return ""
    current_version = get_instruction_version()
    if (project_id != _cached_project_id
            or layout_mode != _cached_layout_mode
            or current_version != _cached_version):
        _instruction_cache = None
        _cached_project_id = project_id
        _cached_layout_mode = layout_mode
        _cached_version = current_version
    if _instruction_cache is not None:
        return _instruction_cache
    _instruction_cache = build_instructions(
        project_id=project_id, provider="zai", layout_mode=layout_mode,
    )
    return _instruction_cache


# ---------------------------------------------------------------------------
# Per-account token store (list-based — mirrors deepseek/client.py)
# ---------------------------------------------------------------------------

def _resolve_active_account() -> str:
    """z.ai's OWN active browser account (independent of every other provider)."""
    from engine.config import get_active_account
    return get_active_account("zai")


def _load_token_store() -> dict[str, list[str]]:
    raw: dict = {}
    if TOKEN_STORE_PATH.exists():
        try:
            raw = json.loads(TOKEN_STORE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    store: dict[str, list[str]] = {}
    for k, v in raw.items():
        if isinstance(v, list):
            store[k] = [t for t in v if t and t != "None"]
        elif isinstance(v, str) and v and v != "None":
            store[k] = [v]
        else:
            store[k] = []
    return store


def _save_token_store(store: dict[str, list[str]]) -> None:
    try:
        data = json.dumps(store, indent=2)
        fd, tmp_path = tempfile.mkstemp(dir=str(TOKEN_STORE_PATH.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(data)
            os.replace(tmp_path, str(TOKEN_STORE_PATH))
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except Exception as exc:
        logger.warning("Could not save z.ai token store: %s", exc)


def get_own_token_for_account(account: str) -> str | None:
    store = _load_token_store()
    tokens = [t for t in store.get(account, []) if t and t != "None"]
    return tokens[-1] if tokens else None


def get_token_for_account(account: str | None = None) -> str | None:
    acct = account or _resolve_active_account()
    store = _load_token_store()
    own = [t for t in store.get(acct, []) if t and t != "None"]
    if own:
        return own[-1]
    for _k, v in store.items():
        live = [t for t in v if t and t != "None"]
        if live:
            return live[-1]
    return None


def save_token_for_account(token: str, account: str | None = None) -> None:
    if not token:
        return
    acct = account or _resolve_active_account()
    store = _load_token_store()
    lst = store.setdefault(acct, [])
    if token in lst:
        lst.remove(token)
    lst.append(token)
    if len(lst) > MAX_TOKENS_PER_ACCOUNT:
        lst[:] = lst[-MAX_TOKENS_PER_ACCOUNT:]
    _save_token_store(store)


# ---------------------------------------------------------------------------
# In-page JS — verified flow (create chat -> captcha -> sign -> SSE)
# ---------------------------------------------------------------------------

_ZAI_JS = r"""
async (args) => {
  const { prompt, model, enableThinking, reasoningEffort, history, bindName,
          chatId: existingChatId, parentId, requestId, userMessageId, files } = args;
  const report = (kind, payload) => {
    try { window[bindName](kind, payload); } catch (e) {}
  };
  try {
    const token = localStorage.getItem('token');
    if (!token) { report('__error__', 'no token in localStorage'); return; }
    const userId = JSON.parse(atob(token.split('.')[1])).id;

    // ---- 1. create chat ONLY on the first turn ----
    // Chained turns reuse the existing chat_id and send nothing but the
    // current message; the server walks the parent chain itself (verified
    // against a live 3-turn capture: turn N+1's current_user_message_parent_id
    // is turn N's request `id`). Full history is embedded here only when the
    // caller has no chain state (first turn, or after a server restart).
    let chatId = existingChatId || null;
    if (!chatId) {
      const msgs = {};
      let lastId = null;
      for (let i = 0; i < history.length; i++) {
        const m = history[i];
        // The current user turn is the last entry; use the caller-supplied id
        // so `current_user_message_id` below matches what's stored in the chat.
        const id = (i === history.length - 1) ? userMessageId : crypto.randomUUID();
        msgs[id] = {
          id, parentId: lastId, childrenIds: [], role: m.role,
          content: m.content, timestamp: Math.floor(Date.now()/1000), models: [model],
        };
        if (lastId && msgs[lastId]) msgs[lastId].childrenIds.push(id);
        lastId = id;
      }
      const nc = await fetch('/api/v1/chats/new', {
        method: 'POST',
        headers: { 'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json',
                   'X-FE-Version': 'prod-fe-1.1.95', 'x-region': 'overseas' },
        body: JSON.stringify({ chat: {
          id: '', title: 'New Chat', models: [model], params: {},
          history: { messages: msgs, currentId: lastId },
          tags: [], flags: [],
          features: [{ server: 'tool_selector_h', status: 'hidden', type: 'tool_selector' }],
          mcp_servers: [], enable_thinking: !!enableThinking,
          reasoning_effort: reasoningEffort || 'high', auto_web_search: false,
          message_version: 1, extra: {}, timestamp: Date.now(), type: 'default',
        }})
      });
      const cd = await nc.json();
      chatId = cd.id;
      if (!chatId) { report('__error__', 'chat create failed: ' + JSON.stringify(cd).slice(0,200)); return; }
    }

    // ---- 2. ensure the Aliyun captcha SDK is loaded, then mint ----
    // The z.ai SPA lazy-loads AliyunCaptcha.js only on first user interaction,
    // so a freshly-opened page has window.initAliyunCaptcha === undefined.
    // Config must be set BEFORE the SDK script loads (it reads it on init).
    window.AliyunCaptchaConfig = { region: 'sgp', prefix: 'no8xfe' };
    if (typeof window.initAliyunCaptcha !== 'function') {
      await new Promise((resolve) => {
        const existing = Array.from(document.scripts).find(
          (s) => s.src && s.src.indexOf('AliyunCaptcha.js') >= 0
        );
        if (existing) {
          existing.addEventListener('load', resolve);
          setTimeout(resolve, 3000);
          return;
        }
        const sc = document.createElement('script');
        sc.src = 'https://o.alicdn.com/captcha-frontend/aliyunCaptcha/AliyunCaptcha.js';
        sc.onload = resolve;
        sc.onerror = resolve;
        document.head.appendChild(sc);
        setTimeout(resolve, 8000);
      });
    }
    if (typeof window.initAliyunCaptcha !== 'function') {
      report('__error__', 'Aliyun captcha SDK failed to load');
      return;
    }
    ['mm-el','mm-btn'].forEach(id => {
      if (!document.getElementById(id)) {
        const d = document.createElement(id === 'mm-el' ? 'div' : 'button');
        d.id = id;
        d.style.cssText = 'position:absolute;left:-99999px;top:-99999px;width:1px;height:1px;opacity:0;';
        document.body.appendChild(d);
      }
    });
    const cap = await new Promise((resolve) => {
      let done = false;
      const fin = (o) => { if (done) return; done = true; resolve(o); };
      try {
        window.initAliyunCaptcha({
          SceneId: 'didk33e0', mode: 'popup', element: '#mm-el', button: '#mm-btn',
          timeout: 10000, delayBeforeSuccess: false, language: 'en',
          success: (e) => fin(typeof e === 'string' ? e : null),
          fail: () => fin(null), onError: () => fin(null), onClose: () => fin(null),
          getInstance: () => setTimeout(() => { try { document.getElementById('mm-btn').click(); } catch (e) {} }, 200),
        });
      } catch (e) { fin(null); }
      setTimeout(() => fin(null), 12000);
    });
    if (!cap) { report('__error__', 'captcha mint failed'); return; }

    // ---- 3. sign ----
    const b64 = (s) => btoa(unescape(encodeURIComponent(s)));
    const H = async (k, m) => {
      const key = await crypto.subtle.importKey('raw', new TextEncoder().encode(k), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
      const sig = await crypto.subtle.sign('HMAC', key, new TextEncoder().encode(m));
      return Array.from(new Uint8Array(sig)).map(b => b.toString(16).padStart(2, '0')).join('');
    };
    const ts = Date.now();
    const rid = crypto.randomUUID();
    const s1 = await H('key-@@@@)))()((9))-xxxx&&&%%%%%', String(Math.floor(ts / (5 * 60 * 1000))));
    const sig = await H(s1, 'requestId,' + rid + ',timestamp,' + ts + ',user_id,' + userId + '|' + b64(prompt) + '|' + ts);

    // ---- 4. send + stream ----
    const now = new Date();
    const body = {
      stream: true, model, params: {}, extra: {},
      messages: [{ role: 'user', content: prompt }],
      signature_prompt: prompt,
      files: (files && files.length) ? files : undefined,
      features: {
        image_generation: false, web_search: false, auto_web_search: false,
        preview_mode: true, flags: [], vlm_tools_enable: false,
        vlm_web_search_enable: false, vlm_website_mode: false,
        enable_thinking: !!enableThinking,
        reasoning_effort: reasoningEffort || 'high',
      },
      variables: {
        '{{USER_NAME}}': 'sable', '{{USER_LOCATION}}': 'Unknown',
        '{{CURRENT_DATETIME}}': now.toISOString().slice(0,19).replace('T',' '),
        '{{CURRENT_DATE}}': now.toISOString().slice(0,10),
        '{{CURRENT_TIME}}': now.toTimeString().slice(0,8),
        '{{CURRENT_WEEKDAY}}': now.toLocaleDateString('en-US',{weekday:'long'}),
        '{{CURRENT_TIMEZONE}}': Intl.DateTimeFormat().resolvedOptions().timeZone,
        '{{USER_LANGUAGE}}': 'en-US',
      },
      chat_id: chatId, id: requestId,
      current_user_message_id: userMessageId, current_user_message_parent_id: parentId,
      background_tasks: { title_generation: true, tags_generation: true },
      captcha_verify_param: cap,
    };
    const fp = {
      timestamp: ts, requestId: rid, user_id: userId, version: '0.0.1', platform: 'web',
      token, signature_timestamp: ts,
      user_agent: navigator.userAgent, language: navigator.language,
      languages: (navigator.languages || []).join(','),
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone, cookie_enabled: 'true',
      screen_width: String(screen.width), screen_height: String(screen.height),
      screen_resolution: screen.width + 'x' + screen.height,
      viewport_height: String(innerHeight), viewport_width: String(innerWidth),
      viewport_size: innerWidth + 'x' + innerHeight,
      color_depth: String(screen.colorDepth), pixel_ratio: String(devicePixelRatio),
      current_url: location.href, pathname: location.pathname, host: location.host,
      hostname: location.hostname, protocol: location.protocol, title: document.title,
      timezone_offset: String(new Date().getTimezoneOffset()),
      local_time: now.toISOString(), utc_time: now.toUTCString(),
      is_mobile: 'false', is_touch: 'false',
      max_touch_points: String(navigator.maxTouchPoints || 0),
      browser_name: 'Chrome', os_name: 'Linux',
    };
    // Stop support: an AbortController whose abort() is exposed on window,
    // so Python can cancel the in-flight completion (matches the SPA's own
    // stop button, which fires an AbortController.abort()).
    const ac = new AbortController();
    window.__zaiAbort = () => { try { ac.abort(); } catch (e) {} };
    let resp;
    try {
      resp = await fetch('/api/v2/chat/completions?' + new URLSearchParams(fp).toString(), {
        method: 'POST',
        headers: {
          'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json',
          'X-FE-Version': 'prod-fe-1.1.95', 'X-Signature': sig,
          'Accept-Language': 'en-US', 'x-region': 'overseas',
        },
        body: JSON.stringify(body),
        signal: ac.signal,
      });
    } catch (e) {
      window.__zaiAbort = null;
      if (e && e.name === 'AbortError') { report('__aborted__', ''); return; }
      report('__error__', String(e));
      return;
    }
    if (!resp.ok) {
      const t = await resp.text();
      window.__zaiAbort = null;
      report('__error__', 'HTTP ' + resp.status + ': ' + t.slice(0, 300));
      return;
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) break;
      buf += decoder.decode(chunk.value, { stream: true });
      let idx;
      while ((idx = buf.indexOf(String.fromCharCode(10))) >= 0) {
        const line = buf.slice(0, idx).trim();
        buf = buf.slice(idx + 1);
        if (!line.startsWith('data:')) continue;
        const payload = line.slice(5).trim();
        if (!payload || payload === '[DONE]') continue;
        try {
          const j = JSON.parse(payload);
          const dd = (j.data && j.data.data) ? j.data.data : (j.data || {});
          if (dd.delta_content) {
            report(dd.phase === 'answer' ? 'answer' : 'thinking', dd.delta_content);
          }
          if (dd.usage) report('__usage__', JSON.stringify(dd.usage));
          if (dd.error) report('__error__', JSON.stringify(dd.error).slice(0, 300));
        } catch (e) {}
      }
    }
    window.__zaiAbort = null;
    report('__done__', JSON.stringify({ chatId: chatId, requestId: requestId }));
  } catch (e) {
    try { window.__zaiAbort = null; } catch (_) {}
    report('__error__', String(e));
  }
}
"""


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class ZaiClient:
    """Persistent browser-assisted client for chat.z.ai."""

    def __init__(self) -> None:
        self._token: str | None = None
        self._account: str | None = None
        self._token_refresher: Callable[[], Awaitable[str]] | None = None
        self._page_provider: Callable[[], Awaitable[Any]] | None = None
        self._playwright = None
        self._context = None
        self._page = None
        self._lock = asyncio.Lock()
        self._stream_lock = asyncio.Lock()
        # Per Sable-chat chain state: {chat_id: {"zai_chat_id": str, "parent_id": str}}.
        # Mirrors DeepSeek's `_parent_ids` — the parent of turn N+1 is the
        # request `id` we generated for turn N (verified against a live capture).
        self._zai_state: dict[str, dict[str, str]] = {}
        # Stable per-page chunk dispatcher target; swapped each turn.
        self._chunk_handler: Callable[[str, str], None] | None = None
        self._binding_exposed = False
        from connectors.common.error_ctx import ErrorContext
        self.ctx = ErrorContext("zai")
        self.ctx.account = self._account

    # ---- token management ----

    @property
    def token(self) -> str | None:
        if self._token:
            return self._token
        self._token = get_token_for_account(self._account)
        return self._token

    @property
    def is_available(self) -> bool:
        return self.token is not None

    def set_token(self, token: str, account: str | None = None, *, persist: bool = True) -> None:
        self._token = token
        if persist and token:
            save_token_for_account(token, account or self._account)

    def set_token_refresher(self, fn: Callable[[], Awaitable[str]]) -> None:
        self._token_refresher = fn

    def set_page_provider(self, fn: Callable[[], Awaitable[Any]]) -> None:
        """Inject a callable returning a Playwright page on chat.z.ai.

        Sable's ChatService owns the active account profile; sharing its page
        avoids the Chromium one-process-per-user_data_dir lock conflict.
        """
        self._page_provider = fn

    async def refresh_token(self) -> str:
        if self._token_refresher is not None:
            token = await self._token_refresher()
            if not token:
                raise RuntimeError("z.ai token refresher returned empty")
            self.set_token(token)
            return token
        token = await self._extract_token_from_page()
        self.set_token(token)
        return token

    # ---- browser lifecycle ----

    def _account_dir(self) -> str:
        from engine.config import get_browser_data_dir
        return str(get_browser_data_dir("zai"))

    async def _ensure_page(self):
        # 1) Prefer the shared service browser (no profile-lock conflict)
        if self._page_provider is not None:
            try:
                page = await self._page_provider()
                if page is not None and not page.is_closed():
                    # Track page identity: a swapped page loses our binding.
                    if page is not getattr(self, "_bound_page", None):
                        self._binding_exposed = False
                        self._bound_page = page
                    self._page = page
                    return page
            except Exception as exc:
                logger.warning("z.ai page provider failed (%s); using standalone browser", exc)

        # 2) Reuse our own warm page
        if self._page is not None:
            try:
                if not self._page.is_closed():
                    return self._page
            except Exception:
                pass

        # 3) Fallback: launch a standalone persistent context
        async with self._lock:
            if self._page is not None:
                try:
                    if not self._page.is_closed():
                        return self._page
                except Exception:
                    pass
            from playwright.async_api import async_playwright
            from engine.platform_paths import resolve_browser_for_profile, extra_browser_args
            user_data_dir = self._account_dir()
            self._playwright = await async_playwright().start()
            profile_name = Path(user_data_dir).name
            exe_path = resolve_browser_for_profile(profile_name)
            kwargs: dict[str, Any] = dict(
                user_data_dir=user_data_dir,
                # Aliyun captcha rejects headless — this MUST be headed.
                headless=False,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled",
                      "--disable-infobars", "--disable-gpu"] + extra_browser_args(exe_path),
            )
            if exe_path:
                kwargs["executable_path"] = exe_path
            # Headed launch needs a display; borrow/start one (Xvfb) if absent.
            try:
                from engine.session import _browser_env_with_display
                _env = _browser_env_with_display()
                if _env:
                    kwargs["env"] = _env
            except Exception:
                pass
            self._context = await self._playwright.chromium.launch_persistent_context(**kwargs)
            self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
            # New page -> old binding is gone; re-expose on next stream.
            self._binding_exposed = False
            await self._page.goto(BASE_URL, wait_until="domcontentloaded", timeout=20000)
            for _ in range(12):
                if await self._page.evaluate("() => !!localStorage.getItem('token')"):
                    break
                await self._page.wait_for_timeout(500)
            for _ in range(10):
                if await self._page.evaluate("() => !!window.initAliyunCaptcha"):
                    break
                await self._page.wait_for_timeout(500)
            return self._page

    async def _extract_token_from_page(self) -> str:
        page = await self._ensure_page()
        raw = await page.evaluate("() => localStorage.getItem('token')")
        if not raw:
            raise RuntimeError("No z.ai token in browser profile. Log in to chat.z.ai first.")
        return raw

    # ---- attachments ----

    async def upload_file(self, path: str | Path) -> dict[str, Any]:
        """Upload a file to z.ai and return the file object for the `files` field.

        Verified against a live capture: `POST /api/v1/files/` is plain
        multipart with Bearer auth and needs NO X-Signature (only the
        completions endpoint is signature-gated). The response object is what
        the SPA later embeds in the completion body's `files` array.
        """
        token = self.token or await self.refresh_token()
        p = Path(path)
        if not p.is_file():
            raise FileNotFoundError(f"z.ai upload: no such file: {p}")

        import mimetypes
        content_type = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        data = p.read_bytes()

        try:
            import httpx
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(
                    BASE_URL + "/api/v1/files/",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/json",
                        "x-region": "overseas",
                        "X-FE-Version": "prod-fe-1.1.95",
                    },
                    files={"file": (p.name, data, content_type)},
                )
            status, text = resp.status_code, resp.text
        except ImportError:
            import urllib.request
            boundary = uuid.uuid4().hex
            body = b"".join([
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="file"; filename="{p.name}"\r\n'.encode(),
                f"Content-Type: {content_type}\r\n\r\n".encode(),
                data,
                f"\r\n--{boundary}--\r\n".encode(),
            ])
            req = urllib.request.Request(
                BASE_URL + "/api/v1/files/", data=body, method="POST",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                    "Accept": "application/json", "x-region": "overseas",
                },
            )
            loop = asyncio.get_running_loop()
            def _do():
                try:
                    with urllib.request.urlopen(req, timeout=120) as r:
                        return r.status, r.read().decode("utf-8", "replace")
                except urllib.error.HTTPError as e:
                    return e.code, e.read().decode("utf-8", "replace")
            status, text = await loop.run_in_executor(None, _do)

        if status != 200:
            raise RuntimeError(f"z.ai upload failed (HTTP {status}): {text[:300]}")
        up = json.loads(text)
        fid = up.get("id")
        if not fid:
            raise RuntimeError(f"z.ai upload returned no id: {text[:300]}")

        # Build the exact object shape the SPA embeds in the completion body.
        meta = up.get("meta", {}) or {}
        return {
            "type": "file",
            "id": fid,
            "url": f"/api/v1/files/{fid}",
            "name": up.get("filename") or meta.get("name") or p.name,
            "status": "uploaded",
            "size": meta.get("size", len(data)),
            "error": "",
            "media": "file",
            "file": {"id": fid, "meta": meta},
        }

    # ---- stop ----

    async def cancel(self) -> bool:
        """Abort the in-flight z.ai completion, like the SPA's stop button.

        The SPA aborts via an AbortController; we expose the same hook on
        `window.__zaiAbort` inside `_ZAI_JS`. Returns True if an abort was
        signalled, False if there was no live request.
        """
        page = self._page
        if page is None:
            return False
        try:
            if page.is_closed():
                return False
            return bool(await page.evaluate(
                "() => { if (window.__zaiAbort) { window.__zaiAbort(); return true; } return false; }"
            ))
        except Exception as exc:
            logger.debug("z.ai cancel failed: %s", exc)
            return False

    async def close(self) -> None:
        try:
            if self._context is not None:
                await self._context.close()
        except Exception:
            pass
        try:
            if self._playwright is not None:
                await self._playwright.stop()
        except Exception:
            pass
        self._playwright = None
        self._context = None
        self._page = None

    # ---- thinking mode mapping ----

    @staticmethod
    def _resolve_thinking(thinking_mode: str | None) -> tuple[bool, str]:
        mode = (thinking_mode or "").lower()
        if mode in ("fast", "off", ""):
            return False, "high"
        if mode in ("low", "medium", "high", "max"):
            return True, mode
        return True, "high"

    async def _rebind_chunk_handler(self, page, handler) -> None:
        """Point the in-page chunk callback at the current turn's handler.

        Playwright's expose_function raises if the name already exists, so we
        expose exactly ONCE per page with a stable dispatcher, then just swap
        the Python-side target each turn. The page always calls the same
        binding; the dispatcher routes to whichever turn is active.
        """
        # Always update the Python-side target first.
        self._chunk_handler = handler

        if getattr(self, "_binding_exposed", False):
            return

        def _dispatch(kind: str, payload: str) -> None:
            # Route to whatever handler is current when the chunk arrives.
            h = getattr(self, "_chunk_handler", None)
            if h is not None:
                try:
                    h(kind, payload)
                except Exception as exc:
                    logger.warning("z.ai chunk dispatch failed: %s", exc)

        await page.expose_function(_BINDING, _dispatch)
        self._binding_exposed = True
        self._bound_page = page

    # ---- streaming ----

    async def stream_chat(
        self,
        message: str,
        model: str | None = None,
        thinking_mode: str | None = None,
        chat_id: str | None = None,
        ref_file_ids: list[str] | None = None,
        ref_files: list[dict[str, Any]] | None = None,
        inject_instructions: bool = True,
        system_instruction: str | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, Any], None]:
        # Attachments: prefer fully-built file objects; otherwise wrap bare ids
        # into the minimal shape z.ai's completion endpoint accepts.
        files_payload: list[dict[str, Any]] | None = ref_files or None
        if files_payload is None and ref_file_ids:
            files_payload = [
                {"type": "file", "id": fid, "file": {"id": fid}}
                for fid in ref_file_ids
                if fid
            ] or None
        # Anti-burst jitter: one gap per send (each tool-call round / user turn).
        try:
            from connectors.common.jitter import jitter_for as _jitter_for, sleep_jitter as _sleep_jitter
            _jdelay = _jitter_for("zai")
            if _jdelay:
                # Emit status BEFORE sleeping so the UI card updates first
                yield {"type": "status", "message": f"zai_delay_{_jdelay:.1f}s"}
                await _sleep_jitter(_jdelay)
        except Exception as _jexc:
            logger.debug("z.ai jitter skipped: %s", _jexc)

        page = await self._ensure_page()
        model_id = model or "x-preview-l"
        self.ctx.model = model_id
        enable_thinking, reasoning_effort = self._resolve_thinking(thinking_mode)

        # History / instruction seeding.
        #
        # Two rules, mirroring the DeepSeek connector's stateless model:
        #   1. The persona/system instruction is delivered ONCE, on a fresh
        #      session, by prefixing the first user turn. z.ai's chat server
        #      builds its own system prompt server-side and IGNORES a
        #      `system`-role entry in an embedded history (the web client
        #      never sends one), so a system message silently does nothing.
        #   2. The user turn is committed to the session history BEFORE the
        #      request, so the next turn carries the full
        #      [user1, assistant1, user2, ...] chain. Previously only the
        #      assistant reply was stored, so turn 2 sent an answer with no
        #      preceding question — the model had no memory.
        db_history = kwargs.pop("db_history", None)
        project_id = kwargs.pop("project_id", None)
        layout_mode = kwargs.pop("layout_mode", None)

        # --- server-side chaining, exactly like DeepSeek's parent_message_id ---
        #
        # Verified against a live 3-turn capture (2026-09-16):
        #   /api/v1/chats/new is called ONCE. Every later turn reuses the same
        #   chat_id, sends ONLY the current message, and sets
        #       current_user_message_parent_id = <previous turn's request `id`>
        #   The server walks that chain itself — history is NOT re-embedded.
        #
        # So `parent_id is None` is what marks "first message", not any local
        # turn counter. When our in-memory chain is gone (fresh chat, provider
        # switch, server restart) we fall back to seeding the whole history
        # into /chats/new — the stateless path DeepSeek also has.
        state = self._zai_state.get(chat_id) if chat_id else None
        zai_chat_id: str | None = state.get("zai_chat_id") if state else None
        parent_id: str | None = state.get("last_request_id") if state else None
        create_chat = not zai_chat_id

        instructions: str | None = None
        if create_chat:
            instructions = system_instruction
            if not instructions and inject_instructions:
                try:
                    instructions = _load_instructions(
                        project_id=project_id, layout_mode=layout_mode,
                    )
                except Exception as exc:
                    logger.warning("z.ai instruction load failed: %s", exc)
                    instructions = None
        elif system_instruction:
            # Mid-session override (agent sub-chat): prefix this single turn.
            instructions = system_instruction

        prompt = message
        if create_chat:
            # Build the history embedded into /chats/new. db_history is loaded
            # AFTER chat.py commits the current user message, so its trailing
            # entry is this turn — drop it or the message duplicates.
            embed: list[dict[str, str]] = []
            if db_history:
                prior = [
                    {"role": m.get("role", "user"), "content": m.get("content", "")}
                    for m in db_history
                    if m.get("content")
                ]
                if prior and prior[-1].get("role") == "user":
                    prior.pop()
                # Bound the embedded history — a deep agentic chat would
                # otherwise ship the entire message table into /chats/new.
                from connectors.common.history_budget import trim_history_messages
                prior = trim_history_messages(prior)
                embed.extend(prior)

            if embed:
                # Fork / stateless path: z.ai's completions API ignores the
                # embedded chat tree when current_user_message_parent_id is
                # null (always the case for a fresh/forked chat). Sending a
                # multi-node history tree with no parent pointer means the
                # server treats the message as root — model sees nothing prior.
                #
                # Fix: serialize the entire conversation into a single flat
                # prompt string. This is exactly what DeepSeek does in its
                # _serialize_history() fallback and what Qwen does via
                # fork_history injection — when there's no parent chain,
                # put all context inline so the model sees it as text.
                parts: list[str] = []
                if instructions:
                    parts.append(f"[System Instructions]\n{instructions}")
                for m in embed:
                    role = m.get("role", "user")
                    content = m.get("content", "")
                    if role == "user":
                        parts.append(f"User: {content}")
                    elif role == "assistant":
                        parts.append(f"Assistant: {content}")
                parts.append(f"User: {message}")
                prompt = "\n\n".join(parts)
                # Single-message embed: no tree needed, just one fat prompt.
                embed = [{"role": "user", "content": prompt}]
            else:
                # No prior history (brand new chat, not a fork): simple path.
                if instructions:
                    prompt = instructions + "\n\n---\n\n" + message
                embed.append({"role": "user", "content": prompt})
        else:
            if instructions:
                prompt = instructions + "\n\n---\n\n" + message
            # Chained turn: the server already holds the tree; nothing to embed.
            embed = [{"role": "user", "content": prompt}]

        request_id = str(uuid.uuid4())
        user_message_id = str(uuid.uuid4())

        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def _on_chunk(kind: str, payload: str) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, (kind, payload))

        args = {
            "prompt": prompt,
            "model": model_id,
            "enableThinking": enable_thinking,
            "reasoningEffort": reasoning_effort,
            "history": embed,
            "bindName": _BINDING,
            "chatId": zai_chat_id,
            "parentId": parent_id,
            "requestId": request_id,
            "userMessageId": user_message_id,
            "files": files_payload,
        }

        # One z.ai stream at a time — the binding name is shared.
        async with self._stream_lock:
            # Rebind every turn: the binding closes over THIS turn's queue.
            # Playwright rejects duplicate expose_function for the same name,
            # and a silent failure would leave the page pushing chunks into the
            # PREVIOUS turn's dead queue -> queue.get() hangs forever.
            await self._rebind_chunk_handler(page, _on_chunk)

            eval_task = asyncio.create_task(page.evaluate(_ZAI_JS, args))
            full_answer = ""
            full_thinking = ""
            done_payload: dict[str, Any] | None = None
            try:
                while True:
                    kind, payload = await queue.get()
                    if kind == "__done__":
                        try:
                            done_payload = json.loads(payload)
                        except Exception:
                            done_payload = None
                        break
                    if kind == "__aborted__":
                        yield {"type": "status", "message": "zai_aborted"}
                        return
                    if kind == "__error__":
                        yield self.ctx.error(payload)
                        return
                    if kind == "__usage__":
                        continue
                    if kind == "answer":
                        full_answer += payload
                        yield {"type": "answer", "text": payload}
                    elif kind == "thinking":
                        full_thinking += payload
                        yield {"type": "thinking", "text": payload}
            finally:
                try:
                    await asyncio.wait_for(eval_task, timeout=5)
                except Exception:
                    eval_task.cancel()

        # Advance the chain only after a completed turn. `last_request_id` is
        # this turn's request id — the capture showed turn N+1's
        # current_user_message_parent_id is exactly turn N's request id.
        if full_answer and chat_id and done_payload:
            self._zai_state[chat_id] = {
                "zai_chat_id": done_payload.get("chatId") or zai_chat_id or "",
                "last_request_id": done_payload.get("requestId") or request_id,
            }

        yield {"type": "done", "parent_id": None}

    async def chat(
        self,
        message: str,
        model: str | None = None,
        thinking_mode: str | None = None,
        chat_id: str | None = None,
        ref_file_ids: list[str] | None = None,
        ref_files: list[dict[str, Any]] | None = None,
        inject_instructions: bool = True,
        system_instruction: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        answer = ""
        thinking = ""
        error = None
        async for ev in self.stream_chat(
            message, model=model, thinking_mode=thinking_mode, chat_id=chat_id,
            ref_file_ids=ref_file_ids, ref_files=ref_files,
            inject_instructions=inject_instructions,
            system_instruction=system_instruction, **kwargs,
        ):
            t = ev.get("type")
            if t == "answer":
                answer += ev.get("text", "")
            elif t == "thinking":
                thinking += ev.get("text", "")
            elif t == "error":
                error = ev.get("message")
        return {"answer": answer, "thinking": thinking, "parent_id": None, "error": error}


_client: ZaiClient | None = None


def get_client() -> ZaiClient:
    global _client
    if _client is None:
        _client = ZaiClient()
    return _client
