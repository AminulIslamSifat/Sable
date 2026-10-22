"""Persistent in-process Playwright session for Sable.

NO SUBPROCESS, no CLI spawn, no unix socket daemon. Playwright is imported
into the Sable process and launches the browser itself.

Why a background thread: Playwright's async API binds to one event loop, but
Sable's handlers are synchronous generators. We run ONE dedicated event loop on
a background thread and marshal each coroutine onto it via
``run_coroutine_threadsafe``. The handler calls ``session.run(coro)`` and blocks
for the result. Browser state (cookies, tabs, network log) therefore survives
across tool calls, while every public method stays synchronous.

All Playwright objects are touched ONLY from the loop thread. Every public
method funnels through ``run()``, so that invariant always holds.
"""

from __future__ import annotations

import asyncio
import atexit
import base64
import json
import logging
import os
import platform
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from playwright.async_api import TimeoutError as PlaywrightTimeout

logger = logging.getLogger(__name__)

_MAX_LOG = 500


# Walks the DOM and tags every interesting node with data-sable-ref="eN".
# We build our own tree instead of using Playwright's aria_snapshot() because
# we need the ref -> element mapping to survive in the page: tagging the node
# means a ref resolves with a plain CSS selector later, with no handle juggling.
SNAPSHOT_JS = r"""
(opts) => {
  const INTERACTIVE = new Set(['button','link','textbox','searchbox','checkbox','radio','combobox','listbox','menuitem','menuitemcheckbox','menuitemradio','option','slider','spinbutton','switch','tab','treeitem']);
  const STRUCTURAL = new Set(['heading','img','form','navigation','main','table','list','listitem','dialog','alert','region']);
  document.querySelectorAll('[data-sable-ref]').forEach(function(e){ e.removeAttribute('data-sable-ref'); });
  const scope = opts.scope ? document.querySelector(opts.scope) : document.body;
  if (!scope) return { error: 'scope not found: ' + opts.scope, items: [] };
  const out = [];
  let n = 0;
  function vis(el) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return false;
    const cs = getComputedStyle(el);
    return cs.visibility !== 'hidden' && cs.display !== 'none' && cs.opacity !== '0';
  }
  function roleOf(el) {
    const ex = el.getAttribute('role');
    if (ex) return ex.trim().toLowerCase();
    const t = el.tagName.toLowerCase();
    if (t === 'a') return el.hasAttribute('href') ? 'link' : null;
    if (t === 'button') return 'button';
    if (t === 'select') return 'combobox';
    if (t === 'textarea') return 'textbox';
    if (t === 'img') return 'img';
    if (t === 'form') return 'form';
    if (t === 'nav') return 'navigation';
    if (t === 'main') return 'main';
    if (t === 'table') return 'table';
    if (t === 'ul' || t === 'ol') return 'list';
    if (t === 'li') return 'listitem';
    if (t === 'dialog') return 'dialog';
    if (/^h[1-6]$/.test(t)) return 'heading';
    if (t === 'input') {
      const ty = (el.type || 'text').toLowerCase();
      if (ty === 'checkbox') return 'checkbox';
      if (ty === 'radio') return 'radio';
      if (ty === 'submit' || ty === 'button' || ty === 'reset' || ty === 'image') return 'button';
      if (ty === 'search') return 'searchbox';
      if (ty === 'range') return 'slider';
      if (ty === 'number') return 'spinbutton';
      return 'textbox';
    }
    return null;
  }
  function nameOf(el) {
    const al = el.getAttribute('aria-label');
    if (al && al.trim()) return al.trim();
    const lb = el.getAttribute('aria-labelledby');
    if (lb) {
      const p = lb.split(/\s+/).map(function(id){ const e2 = document.getElementById(id); return e2 ? e2.textContent : ''; }).join(' ').trim();
      if (p) return p;
    }
    const t = el.tagName.toLowerCase();
    if (t === 'img') return (el.getAttribute('alt') || '').trim();
    if (t === 'input' || t === 'textarea') {
      const ph = el.getAttribute('placeholder');
      if (ph) return ph.trim();
      const v = el.value;
      if (v) return String(v).slice(0, 80);
    }
    const txt = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
    if (txt) return txt.slice(0, 100);
    // Unnamed links are usually image tiles or icon buttons. An empty name made
    // them indistinguishable and forced callers into raw eval() clicks.
    const img = el.querySelector ? el.querySelector('img') : null;
    if (img) {
      const alt = (img.getAttribute('alt') || '').trim();
      if (alt) return alt.slice(0, 100);
      const src = img.getAttribute('src') || img.getAttribute('data-src') || '';
      const base = src.split('?')[0].split('/').pop();
      if (base) return 'img:' + base.slice(0, 80);
    }
    const title = el.getAttribute('title');
    if (title) return title.slice(0, 100);
    return '';
  }
  function walk(el, depth) {
    if (!vis(el)) return;
    const role = roleOf(el);
    const interactive = role ? INTERACTIVE.has(role) : false;
    // Depth caps STRUCTURE, never interactivity. A deep button is still the
    // button the agent needs; hiding it made whole pages look actionless.
    const tooDeep = depth > opts.maxDepth;
    if (tooDeep && !interactive) {
      // Still descend: an interactive descendant may sit below this node.
      for (let i = 0; i < el.children.length; i++) walk(el.children[i], depth + 1);
      return;
    }
    const interesting = role && (interactive || STRUCTURAL.has(role));
    const keep = interesting && (!opts.interactiveOnly || interactive);
    if (keep) {
      n += 1;
      const ref = 'e' + (n + (opts.offset || 0));
      el.setAttribute('data-sable-ref', ref);
      const item = { ref: ref, role: role, tag: el.tagName.toLowerCase(), name: nameOf(el), depth: depth };
      if (role === 'heading') item.level = parseInt(el.tagName.slice(1), 10);
      if (role === 'link' && el.href) item.href = String(el.href).slice(0, 120);
      if (el.disabled) item.disabled = true;
      if (el.tagName.toLowerCase() === 'input' && el.type) item.type = el.type;
      out.push(item);
    }
    for (let i = 0; i < el.children.length; i++) walk(el.children[i], depth + 1);
  }
  walk(scope, 0);

  // How many interactive elements sit below the fold and therefore may not be
  // in `out` yet (lazy render / virtualised lists). Reported, not acted on.
  let belowFold = 0;
  const vh = window.innerHeight || 0;
  scope.querySelectorAll('a[href],button,input,select,textarea,[role=button],[role=link]').forEach(function(el){
    const r = el.getBoundingClientRect();
    if (r.height > 0 && r.top > vh) belowFold += 1;
  });

  return { items: out, url: location.href, title: document.title, count: n, belowFold: belowFold };
}
"""

ANNOTATE_JS = r"""
() => {
  document.querySelectorAll('.__sable_annot').forEach(function(e){ e.remove(); });
  let n = 0;
  document.querySelectorAll('[data-sable-ref]').forEach(function(el){
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return;
    const tag = document.createElement('div');
    tag.className = '__sable_annot';
    tag.textContent = el.getAttribute('data-sable-ref').replace('e', '');
    tag.style.cssText = 'position:fixed;left:' + Math.max(0, r.left - 2) + 'px;top:' + Math.max(0, r.top - 2) +
      'px;background:#7c5cbf;color:#fff;font:bold 13px/1.2 ui-monospace,monospace;padding:2px 6px;' +
      'border-radius:4px;z-index:2147483647;pointer-events:none;box-shadow:0 0 0 2px #fff,0 1px 4px rgba(0,0,0,.5);';
    document.body.appendChild(tag);
    n += 1;
  });
  return n;
}
"""

UNANNOTATE_JS = "() => { document.querySelectorAll('.__sable_annot').forEach(function(e){ e.remove(); }); return true; }"





def system_chrome_candidates() -> list[str]:
    """Known Chrome/Chromium locations across Linux, macOS and Windows."""
    if platform.system() == "Windows":
        return [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        ]
    if platform.system() == "Darwin":
        return [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ]
    return [
        "google-chrome-stable",
        "google-chrome",
        "chromium",
        "chromium-browser",
        "brave-browser",
        "microsoft-edge-stable",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/brave-browser",
    ]


def find_browser_binary(name: str = "chrome") -> str | None:
    """Resolve a browser binary by path, bare name, or shortcut.

    ``name`` may be a concrete path, an executable name to search on PATH, or
    one of 'chrome' / 'chromium' / 'edge' / 'brave'. Returns None when nothing
    matches, so the caller falls back to Playwright's bundled build.
    """
    if not name:
        return None
    if os.sep in name or (os.altsep and os.altsep in name):
        p = Path(os.path.expanduser(name))
        return str(p) if p.is_file() else None

    wanted = name.lower()
    for cand in system_chrome_candidates():
        base = os.path.basename(cand).lower()
        if wanted in ("chrome", "chromium"):
            match = "chrom" in base or "edge" in base or "brave" in base
        else:
            match = wanted in base
        if not match:
            continue
        found = shutil.which(cand) if os.sep not in cand else (cand if os.path.isfile(cand) else None)
        if found:
            return found

    return shutil.which(name)



def format_snapshot(items: list[dict], url: str = "", title: str = "") -> str:
    """Render snapshot items as compact text, one line per node.

    Format follows agent-browser's, which agents already read well:
        @e5 [button] "Continue"
        @e7 [input type=email] placeholder="Email"
    Tag name leads (not ARIA role) because it is what people and models both
    picture, with the input type inline. Elements inside an iframe are indented
    under their frame so it is obvious where the ref lives.
    """
    lines: list[str] = []
    if title:
        lines.append(f"Page: {title}")
    if url:
        lines.append(f"URL: {url}")
    if lines:
        lines.append("")

    current_frame: str | None = None
    for it in items:
        frame = it.get("frame")
        if frame != current_frame:
            current_frame = frame
            if frame:
                lines.append(f"  [frame] {frame}")

        tag = it.get("tag") or it.get("role") or "?"
        if it.get("type") and tag in ("input", "button"):
            tag = f"{tag} type={it['type']}"

        bits = [f"@{it['ref']}", f"[{tag}]"]
        name = (it.get("name") or "").strip()
        if name:
            bits.append('"' + name.replace('"', "'") + '"')
        if it.get("href"):
            bits.append(f"href={it['href']}")
        if it.get("disabled"):
            bits.append("disabled")

        indent = "  " * min(int(it.get("depth") or 0), 6)
        lines.append(indent + " ".join(bits))

    if not items:
        lines.append("(no interactive elements found)")
    return chr(10).join(lines)


class BrowserSession:
    """Owns one Playwright browser on a private event loop thread."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

        self.pw = None
        self.browser = None
        self.context = None
        self.page = None

        self.config: dict[str, Any] = {}
        self.started_at: float | None = None

        self._network_log: list[dict] = []
        self._console_log: list[dict] = []
        self._capturing_network = False
        self._capturing_console = False

    # ── event loop plumbing ──────────────────────────────────────────────

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop and self._loop.is_running():
                return self._loop

            loop = asyncio.new_event_loop()

            def _runner() -> None:
                asyncio.set_event_loop(loop)
                loop.run_forever()

            t = threading.Thread(target=_runner, name="sable-browser-loop", daemon=True)
            t.start()

            for _ in range(300):
                if loop.is_running():
                    break
                time.sleep(0.01)

            self._loop = loop
            self._thread = t
            atexit.register(self._atexit_stop)
            return loop

    def run(self, coro, timeout: float = 60.0):
        """Run a coroutine on the browser loop and block for its result."""
        loop = self._ensure_loop()
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        return fut.result(timeout)

    def _atexit_stop(self) -> None:
        try:
            if self.page is not None or self.browser is not None:
                self.run(self._shutdown(), timeout=15)
        except Exception:
            pass
        try:
            if self._loop and self._loop.is_running():
                self._loop.call_soon_threadsafe(self._loop.stop)
        except Exception:
            pass

    # ── lifecycle ────────────────────────────────────────────────────────

    @property
    def alive(self) -> bool:
        try:
            return self.page is not None and not self.page.is_closed()
        except Exception:
            return False

    def ensure_started(self, **opts: Any) -> None:
        """Launch the browser if it is not already up."""
        if self.alive:
            return
        self.run(self._start(**opts), timeout=120)

    async def _start(
        self,
        browser: str = "chromium",
        executable_path: str | None = None,
        user_data_dir: str | None = None,
        headless: bool = True,
        args: list[str] | None = None,
        proxy: dict | None = None,
        viewport: dict | None = None,
        user_agent: str | None = None,
        timeout_ms: int = 30000,
        har_path: str | None = None,
        har_content: str = "embed",
    ) -> None:
        from playwright.async_api import async_playwright

        if self.pw is None:
            self.pw = await async_playwright().start()

        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
        ]
        if not headless and os.environ.get("WAYLAND_DISPLAY"):
            launch_args.append("--ozone-platform=wayland")
        if headless:
            # Headless Chrome otherwise falls back to SwiftShader (CPU rasterizer)
            # and any animated canvas pegs a core. Keep it on the real GL path.
            launch_args += [
                "--use-angle=gl",
                "--enable-webgl",
                "--ignore-gpu-blocklist",
                "--enable-gpu-rasterization",
                "--enable-zero-copy",
                "--disable-software-rasterizer",
            ]
        if args:
            launch_args += [a for a in args if a]

        use_sandbox = platform.system() != "Windows"
        btype = (browser or "chromium").lower()

        vp = viewport if isinstance(viewport, dict) else {"width": 1440, "height": 900}
        ua = user_agent or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            if platform.system() == "Windows"
            else "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        )

        # Persistent context only exists on chromium. firefox/webkit get a normal
        # launch + fresh context; user_data_dir is ignored for them.
        if user_data_dir and btype == "chromium":
            udd = str(Path(user_data_dir).expanduser())
            Path(udd).mkdir(parents=True, exist_ok=True)
            ctx_kwargs: dict[str, Any] = {
                "headless": headless,
                "args": launch_args,
                "chromium_sandbox": use_sandbox,
                "viewport": vp,
                "user_agent": ua,
                "accept_downloads": True,
            }
            if executable_path:
                ctx_kwargs["executable_path"] = executable_path
            if proxy:
                ctx_kwargs["proxy"] = proxy

            self.context = await self.pw.chromium.launch_persistent_context(udd, **ctx_kwargs)
            self.browser = None
            pages = self.context.pages
            self.page = pages[0] if pages else await self.context.new_page()
        else:
            launch_opts: dict[str, Any] = {"headless": headless, "args": launch_args}
            if executable_path and btype == "chromium":
                launch_opts["executable_path"] = executable_path
            if proxy:
                launch_opts["proxy"] = proxy

            if btype == "firefox":
                self.browser = await self.pw.firefox.launch(**launch_opts)
            elif btype == "webkit":
                self.browser = await self.pw.webkit.launch(**launch_opts)
            else:
                self.browser = await self.pw.chromium.launch(
                    chromium_sandbox=use_sandbox, **launch_opts
                )
            ctx_extra: dict[str, Any] = {}
            if har_path:
                Path(har_path).parent.mkdir(parents=True, exist_ok=True)
                ctx_extra["record_har_path"] = har_path
                ctx_extra["record_har_content"] = har_content
            self.context = await self.browser.new_context(
                viewport=vp, user_agent=ua, accept_downloads=True, **ctx_extra
            )
            self.page = await self.context.new_page()

        self.page.set_default_timeout(timeout_ms)
        self._attach_listeners(self.page)

        self.config = {
            "browser": btype,
            "executable_path": executable_path,
            "user_data_dir": user_data_dir,
            "headless": headless,
            "args": launch_args,
            "viewport": vp,
        }
        self.started_at = time.time()
        logger.info("browser started: %s headless=%s exec=%s", btype, headless, executable_path)

    async def _shutdown(self) -> None:
        for obj in (self.context, self.browser, self.pw):
            if obj is None:
                continue
            try:
                await (obj.close() if obj is not self.pw else obj.stop())
            except Exception:
                pass
        self.page = None
        self.context = None
        self.browser = None
        self.pw = None
        self.started_at = None

    def stop(self) -> None:
        if self._loop is None:
            return
        try:
            self.run(self._shutdown(), timeout=20)
        except Exception as exc:
            logger.warning("browser stop: %s", exc)

    # ── listeners ────────────────────────────────────────────────────────

    def _attach_listeners(self, page) -> None:
        page.on("request", self._on_request)
        page.on("response", self._on_response)
        page.on("console", self._on_console)

    def _on_request(self, req) -> None:
        if not self._capturing_network:
            return
        try:
            self._network_log.append({
                "type": "request",
                "method": req.method,
                "url": req.url[:400],
                "resource_type": req.resource_type,
                "ts": time.time(),
            })
            self._trim()
        except Exception:
            pass

    def _on_response(self, resp) -> None:
        if not self._capturing_network:
            return
        try:
            self._network_log.append({
                "type": "response",
                "status": resp.status,
                "url": resp.url[:400],
                "ts": time.time(),
            })
            self._trim()
        except Exception:
            pass

    def _on_console(self, msg) -> None:
        if not self._capturing_console:
            return
        try:
            self._console_log.append({
                "level": msg.type,
                "text": msg.text[:1000],
                "ts": time.time(),
            })
            if len(self._console_log) > _MAX_LOG:
                del self._console_log[:-_MAX_LOG]
        except Exception:
            pass

    def _trim(self) -> None:
        if len(self._network_log) > _MAX_LOG:
            del self._network_log[:-_MAX_LOG]

    # ── navigation ───────────────────────────────────────────────────────

    def open(self, url: str, wait_until: str = "domcontentloaded", timeout_ms: int | None = None) -> dict:
        return self.run(self._open(url, wait_until, timeout_ms))

    async def _open(self, url: str, wait_until: str, timeout_ms: int | None) -> dict:
        if not url.startswith(("http://", "https://", "file://", "about:", "data:")):
            p = Path(os.path.expanduser(url))
            url = p.resolve().as_uri() if p.exists() else "https://" + url
        kw: dict[str, Any] = {"wait_until": wait_until, "timeout": 30000}
        if timeout_ms:
            kw["timeout"] = timeout_ms
        resp = await self.page.goto(url, **kw)
        return {
            "url": self.page.url,
            "title": await self.page.title(),
            "status": resp.status if resp else None,
        }

    def back(self) -> dict:
        return self.run(self._back())

    async def _back(self) -> dict:
        await self.page.go_back(timeout=3000)
        return {"url": self.page.url, "title": await self.page.title()}

    def forward(self) -> dict:
        return self.run(self._forward())

    async def _forward(self) -> dict:
        await self.page.go_forward(timeout=3000)
        return {"url": self.page.url, "title": await self.page.title()}

    def reload(self) -> dict:
        return self.run(self._reload())

    async def _reload(self) -> dict:
        await self.page.reload(timeout=3000)
        return {"url": self.page.url, "title": await self.page.title()}

    def wait_url(self, pattern: str, timeout_ms: int = 30000) -> dict:
        return self.run(self._wait_url(pattern, timeout_ms))

    async def _wait_url(self, pattern: str, timeout_ms: int) -> dict:
        await self.page.wait_for_url(pattern, timeout=timeout_ms)
        return {"url": self.page.url}

    def wait_selector(self, selector: str, state: str = "visible", timeout_ms: int = 30000) -> dict:
        return self.run(self._wait_selector(selector, state, timeout_ms))

    async def _wait_selector(self, selector: str, state: str, timeout_ms: int) -> dict:
        await self.page.wait_for_selector(selector, state=state, timeout=timeout_ms)
        return {"found": selector, "state": state}

    def wait(self, seconds: float) -> dict:
        time.sleep(max(0.0, min(120.0, float(seconds))))
        return {"waited": seconds}

    # ── inspection ───────────────────────────────────────────────────────

    def screenshot(
        self,
        path: str,
        full_page: bool = False,
        selector: str | None = None,
        clip: dict | None = None,
    ) -> dict:
        return self.run(self._screenshot(path, full_page, selector, clip))

    async def _screenshot(self, path: str, full_page: bool, selector: str | None, clip: dict | None) -> dict:
        """Capture a screenshot, waiting for first paint and retrying once.

        Right after navigation Chromium has a layout but no composited frame yet,
        and Page.captureScreenshot fails with a bare "Unable to capture
        screenshot". The very next attempt always succeeds, so: give the
        compositor two animation frames, then retry once on that protocol error
        rather than surfacing a confusing failure on the first capture.
        """
        Path(path).parent.mkdir(parents=True, exist_ok=True)

        try:
            await self.page.evaluate(
                "() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))"
            )
        except Exception:
            pass

        async def _capture() -> None:
            if selector:
                await self._locator(selector).screenshot(path=path, timeout=3000)
            else:
                kw: dict[str, Any] = {"path": path, "full_page": full_page}
                if clip:
                    kw["clip"] = clip
                await self.page.screenshot(**kw)

        try:
            await _capture()
        except Exception as exc:
            if "captureScreenshot" not in str(exc):
                raise
            await asyncio.sleep(0.25)
            await _capture()

        size = os.path.getsize(path)
        return {"path": path, "bytes": size, "url": self.page.url}

    def dump_html(self, selector: str = "body") -> dict:
        return self.run(self._dump_html(selector))

    async def _dump_html(self, selector: str) -> dict:
        """Inner HTML of a selector. Never silently falls back to the full page.

        Returning the whole document when the selector missed was actively
        misleading: the caller asked for <h1>, got <html>, and had no signal the
        element did not exist. Raise instead so the failure is visible.
        """
        return {"selector": selector, "html": await self._locator(selector).inner_html(timeout=3000)}

    def extract(self, selector: str = "body") -> dict:
        return self.run(self._extract(selector))

    async def _extract(self, selector: str) -> dict:
        text = await self._locator(selector).inner_text(timeout=3000)
        return {"selector": selector, "text": text, "length": len(text)}

    def source(self) -> dict:
        return self.run(self._source())

    async def _source(self) -> dict:
        html = await self.page.content()
        return {"url": self.page.url, "length": len(html), "html": html}

    def eval(self, js: str) -> dict:
        return self.run(self._eval(js))

    async def _eval(self, js: str) -> dict:
        if js.startswith("b64:"):
            js = base64.b64decode(js[4:]).decode("utf-8")
        elif js.startswith("file:"):
            js = Path(js[5:]).read_text(encoding="utf-8")
        result = await self.page.evaluate(js)
        try:
            json.dumps(result)
        except (TypeError, ValueError):
            result = str(result)
        return {"result": result}


    # ── snapshot / refs ──────────────────────────────────────────────────

    def snapshot(
        self,
        interactive_only: bool = True,
        scope: str | None = None,
        max_depth: int = 12,
        include_frames: bool = True,
    ) -> dict:
        """Accessibility snapshot with stable @eN refs, iframes included.

        Every ref is tagged into its own frame's DOM as data-sable-ref, and the
        ref -> (frame, selector) mapping is recorded in self._ref_map so a later
        action can (a) reject an unknown ref instantly instead of polling for a
        selector that will never appear, and (b) act inside the right frame.

        Cross-origin frames are skipped: their accessibility tree is not
        readable, and silently pretending otherwise would be worse than omitting
        them. Only one level of nesting is walked.
        """
        return self.run(self._snapshot(interactive_only, scope, max_depth, include_frames))

    async def _snapshot(
        self,
        interactive_only: bool,
        scope: str | None,
        max_depth: int,
        include_frames: bool = True,
    ) -> dict:
        self._ref_map = {}
        all_items: list[dict] = []
        offset = 0
        below_fold = 0

        frames = [self.page.main_frame] if not include_frames else self.page.frames
        first = True
        for frame in frames:
            try:
                raw = await frame.evaluate(
                    SNAPSHOT_JS,
                    {
                        "interactiveOnly": interactive_only,
                        "scope": scope if first else None,
                        "maxDepth": max_depth,
                        "offset": offset,
                    },
                )
            except Exception:
                # Cross-origin or detached frame - skip, do not fail the whole
                # snapshot over one unreadable frame.
                continue
            first = False
            if raw.get("error"):
                continue

            items = raw.get("items", [])
            if frame is not self.page.main_frame:
                frame_url = (frame.url or "")[:80]
                for it in items:
                    it["frame"] = frame_url
                    # Depth +1 so frame children read as nested under the frame.
                    it["depth"] = int(it.get("depth") or 0) + 1
            for it in items:
                self._ref_map[it["ref"]] = {
                    "frame": None if frame is self.page.main_frame else frame,
                    "selector": '[data-sable-ref="%s"]' % it["ref"],
                }
            all_items.extend(items)
            below_fold += int(raw.get("belowFold") or 0)
            offset += int(raw.get("count") or 0)

        raw = {
            "items": all_items,
            "count": len(all_items),
            "url": self.page.url,
            "title": await self.page.title(),
            "frames": len(frames),
            "below_fold": below_fold,
        }
        raw["text"] = format_snapshot(all_items, raw["url"], raw["title"])
        return raw

    def _locator(self, sel: str):
        """Resolve '@eN' to a frame-aware Locator; pass selectors straight through.

        Unknown refs raise immediately. That is the whole point: a stale ref used
        to become a CSS selector Playwright would poll for 10 seconds, turning
        every wrong guess into a 10s stall in the agent loop.
        """
        s = (sel or "").strip()
        if not s.startswith("@"):
            return self.page.locator(s)
        ref = s[1:]
        info = getattr(self, "_ref_map", {}).get(ref)
        if info is None:
            known = ", ".join(sorted(getattr(self, "_ref_map", {}))[:12]) or "none"
            raise ValueError(
                f"unknown ref '{s}' - it is not in the latest snapshot. "
                f"Known refs: {known}. Re-run action=snapshot after the page changes."
            )
        target = info["frame"] if info["frame"] is not None else self.page
        return target.locator(info["selector"])

    def annotate(self) -> dict:
        """Overlay numbered labels on elements, tagging them first if needed.

        Labels map to @eN refs, so annotating a page that was never snapshotted
        used to draw nothing. Now it self-tags: if no element carries a ref, run
        a snapshot first so the numbers actually mean something.
        """
        return self.run(self._annotate())

    async def _annotate(self) -> dict:
        has_refs = await self.page.evaluate(
            "() => document.querySelectorAll('[data-sable-ref]').length"
        )
        auto_snapshot = False
        if not has_refs:
            await self._snapshot(True, None, 12)
            auto_snapshot = True
        n = await self.page.evaluate(ANNOTATE_JS)
        return {"annotated": n, "auto_snapshot": auto_snapshot}

    def unannotate(self) -> dict:
        return self.run(self.page.evaluate(UNANNOTATE_JS))

    # ── interaction ──────────────────────────────────────────────────────

    def click(self, selector: str, timeout_ms: int = 3000) -> dict:
        return self.run(self._click(selector, timeout_ms))

    async def _click(self, selector: str, timeout_ms: int) -> dict:
        before = self.page.url
        # Playwright 1.61 deprecated no_wait_after ("This option has no effect"),
        # so the click still blocks on pending navigations and can time out
        # AFTER the click already fired. Swallow the actionability timeout and
        # let _settle() decide whether a navigation actually happened - it polls
        # the URL itself and never raises.
        try:
            await self._locator(selector).click(timeout=timeout_ms)
        except PlaywrightTimeout:
            logger.debug("click on %s timed out post-dispatch; deferring to _settle", selector)
        moved = await self._settle(before)
        return {
            "clicked": selector,
            "navigated": moved,
            "url": self.page.url,
            "title": await self.page.title(),
        }

    def type_text(self, selector: str, text: str, clear: bool = True) -> dict:
        return self.run(self._type(selector, text, clear))

    async def _type(self, selector: str, text: str, clear: bool) -> dict:
        if clear:
            await self._locator(selector).fill(text, timeout=3000)
        else:
            await self._locator(selector).press_sequentially(text, timeout=3000)
        return {"typed": selector, "chars": len(text)}

    def press(self, key: str, selector: str | None = None) -> dict:
        return self.run(self._press(key, selector))

    async def _press(self, key: str, selector: str | None) -> dict:
        before = self.page.url
        if selector:
            await self._locator(selector).press(key, timeout=3000, no_wait_after=True)
        else:
            await self.page.keyboard.press(key)
        moved = await self._settle(before)
        return {"pressed": key, "navigated": moved, "url": self.page.url}

    def hover(self, selector: str) -> dict:
        return self.run(self._hover(selector))

    async def _hover(self, selector: str) -> dict:
        await self._locator(selector).hover(timeout=3000)
        return {"hovered": selector}

    def select(self, selector: str, value: str) -> dict:
        return self.run(self._select(selector, value))

    async def _select(self, selector: str, value: str) -> dict:
        picked = await self._locator(selector).select_option(value, timeout=3000)
        return {"selector": selector, "selected": picked}

    # ── tabs ─────────────────────────────────────────────────────────────

    def tabs(self) -> dict:
        return self.run(self._tabs())

    async def _tabs(self) -> dict:
        pages = self.context.pages
        out = []
        for i, p in enumerate(pages):
            try:
                out.append({"index": i, "url": p.url, "title": await p.title(), "active": p is self.page})
            except Exception:
                out.append({"index": i, "url": p.url, "active": p is self.page})
        return {"count": len(pages), "tabs": out}

    def tab_new(self, url: str | None = None) -> dict:
        return self.run(self._tab_new(url))

    async def _tab_new(self, url: str | None) -> dict:
        self.page = await self.context.new_page()
        self._attach_listeners(self.page)
        if url:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
        return {"url": self.page.url, "tabs": len(self.context.pages)}

    def tab_switch(self, index: int) -> dict:
        return self.run(self._tab_switch(index))

    async def _tab_switch(self, index: int) -> dict:
        pages = self.context.pages
        if index < 0 or index >= len(pages):
            raise IndexError(f"tab index {index} out of range (0..{len(pages) - 1})")
        self.page = pages[index]
        await self.page.bring_to_front()
        return {"index": index, "url": self.page.url, "title": await self.page.title()}

    def tab_close(self, index: int) -> dict:
        return self.run(self._tab_close(index))

    async def _tab_close(self, index: int) -> dict:
        pages = self.context.pages
        if index < 0 or index >= len(pages):
            raise IndexError(f"tab index {index} out of range")
        await pages[index].close()
        remaining = self.context.pages
        self.page = remaining[0] if remaining else await self.context.new_page()
        return {"closed": index, "remaining": len(remaining)}

    # ── cookies / storage ────────────────────────────────────────────────

    def cookies(self) -> dict:
        return self.run(self._cookies())

    async def _cookies(self) -> dict:
        return {"cookies": await self.context.cookies()}

    def set_cookies(self, cookies: list[dict]) -> dict:
        return self.run(self._set_cookies(cookies))

    async def _set_cookies(self, cookies: list[dict]) -> dict:
        await self.context.add_cookies(cookies)
        return {"added": len(cookies)}

    def storage(self) -> dict:
        return self.run(self._storage())

    async def _storage(self) -> dict:
        local = await self.page.evaluate("() => Object.fromEntries(Object.entries(localStorage))")
        sess = await self.page.evaluate("() => Object.fromEntries(Object.entries(sessionStorage))")
        return {"localStorage": local, "sessionStorage": sess}

    # ── devtools capture ─────────────────────────────────────────────────

    def network_start(self) -> dict:
        self._capturing_network = True
        return {"capturing": True}

    def network_stop(self) -> dict:
        self._capturing_network = False
        return {"capturing": False, "entries": len(self._network_log)}

    def network_log(self, limit: int = 100, only_errors: bool = False) -> dict:
        entries = self._network_log
        if only_errors:
            entries = [e for e in entries if e.get("status", 200) >= 400]
        return {"entries": entries[-limit:], "total": len(self._network_log)}

    def network_clear(self) -> dict:
        n = len(self._network_log)
        self._network_log.clear()
        return {"cleared": n}

    def console_start(self) -> dict:
        self._capturing_console = True
        return {"capturing": True}

    def console_stop(self) -> dict:
        self._capturing_console = False
        return {"capturing": False, "entries": len(self._console_log)}

    def console_log(self, limit: int = 100, level: str | None = None) -> dict:
        entries = self._console_log
        if level:
            entries = [e for e in entries if e.get("level") == level]
        return {"entries": entries[-limit:], "total": len(self._console_log)}

    def console_clear(self) -> dict:
        n = len(self._console_log)
        self._console_log.clear()
        return {"cleared": n}

    # ── status ───────────────────────────────────────────────────────────

    def status(self) -> dict:
        if not self.alive:
            return {"alive": False, "config": self.config}
        return self.run(self._status())

    async def _status(self) -> dict:
        try:
            title = await self.page.title()
        except Exception:
            title = None
        return {
            "alive": True,
            "url": self.page.url,
            "title": title,
            "tabs": len(self.context.pages),
            "uptime_s": round(time.time() - (self.started_at or time.time()), 1),
            "config": self.config,
            "network_capturing": self._capturing_network,
            "console_capturing": self._capturing_console,
            "network_entries": len(self._network_log),
            "console_entries": len(self._console_log),
        }



    async def _settle(self, before_url: str, wait_ms: int = 5000) -> bool:
        """Give a navigation a moment to start, then to finish. Never raises.

        wait_for_load_state() is useless right after a click: the CURRENT page is
        already loaded, so it returns instantly and the navigation that fires a
        beat later gets missed (press reported navigated=False for a submit that
        did navigate). Poll for the URL to change instead, then wait for the new
        document to be ready.

        Returns True if the URL changed. Timeouts are swallowed on purpose: the
        action already happened, so a slow load is information, not failure.
        """
        deadline = time.time() + (wait_ms / 1000.0)
        moved = False
        # Phase 1: short window for the navigation to COMMIT.
        while time.time() < deadline:
            if self.page.url != before_url:
                moved = True
                break
            try:
                await self.page.wait_for_timeout(120)
            except Exception:
                break
        if not moved:
            return False
        # Phase 2: let the new document reach domcontentloaded, best effort.
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=wait_ms)
        except Exception:
            pass
        return True

    # ── semantic locators (no snapshot needed) ───────────────────────────

    def find(
        self,
        by: str,
        value: str = "",
        name: str | None = None,
        do: str = "count",
        text: str | None = None,
        exact: bool = False,
        nth: int | None = None,
    ) -> dict:
        """Address elements by role/text/label/testid instead of CSS.

        Playwright's locator API is often faster than snapshot-and-ref: when you
        already know it's a button named "Submit", there is no reason to read the
        whole tree first. Refs remain the better choice for exploration.
        """
        return self.run(self._find(by, value, name, do, text, exact, nth))

    async def _find(self, by, value, name, do, text, exact, nth) -> dict:
        page = self.page
        b = (by or "").lower()
        if b == "role":
            loc = page.get_by_role(value, name=name, exact=exact) if name else page.get_by_role(value, exact=exact)
        elif b == "text":
            loc = page.get_by_text(value, exact=exact)
        elif b == "label":
            loc = page.get_by_label(value, exact=exact)
        elif b == "placeholder":
            loc = page.get_by_placeholder(value, exact=exact)
        elif b == "testid":
            loc = page.get_by_test_id(value)
        elif b == "alt":
            loc = page.get_by_alt_text(value, exact=exact)
        elif b == "title":
            loc = page.get_by_title(value, exact=exact)
        else:
            raise ValueError(f"unknown 'by': {by}. Use role|text|label|placeholder|testid|alt|title")

        total = await loc.count()
        if nth is not None:
            loc = loc.nth(nth)
        elif total > 1:
            # Act on the first match instead of raising strict-mode. Ambiguity is
            # reported in the result so the caller can narrow it, but a common
            # "this text appears 4 times" case should not be a hard failure.
            loc = loc.first

        d = (do or "count").lower()
        if d == "count":
            return {"count": total, "by": by, "value": value, "names": (await loc.all_inner_texts())[:5] if total else []}
        if d == "click":
            # Same post-dispatch timeout trap as _click. Swallow the timeout,
            # but do NOT claim success blindly: settle on the URL so a real
            # navigation is observed, and surface `timed_out` so a genuine miss
            # is still visible instead of being reported as a clean click.
            before = page.url
            timed_out = False
            try:
                await loc.click(timeout=3000)
            except PlaywrightTimeout:
                timed_out = True
                logger.debug("find do=click on %r timed out post-dispatch", value)
            moved = await self._settle(before, wait_ms=8000)
            return {
                "clicked": True,
                "matches": total,
                "navigated": moved,
                "timed_out": timed_out,
                "url": page.url,
                "title": await page.title(),
            }
        if d == "fill":
            if not text:
                raise ValueError(
                    "fill needs 'text' with the value to type. Did you put it in 'name' "
                    "(the accessible-name filter) instead?"
                )
            await loc.fill(text, timeout=3000)
            # fill() is a silent no-op on non-input elements: filling an <h1>
            # returned success and did nothing. Verify the target type so a
            # caller can tell a real fill from a miss.
            tag = await loc.evaluate("e => e.tagName.toLowerCase()")
            if tag not in ("input", "textarea", "select"):
                raise ValueError(f"cannot fill <{tag}>: not a form control")
            return {"filled": True, "chars": len(text or ""), "matches": total}
        if d == "type":
            await loc.type(text or "", timeout=3000)
            return {"typed": True}
        if d == "text":
            return {"text": (await loc.inner_text(timeout=3000))[:20000]}
        if d == "html":
            return {"html": (await loc.inner_html(timeout=3000))[:20000]}
        if d == "value":
            return {"value": await loc.input_value(timeout=3000)}
        if d == "attr":
            return {"value": await loc.get_attribute(name or "href", timeout=3000)}
        if d == "check":
            await loc.check(timeout=3000)
            return {"checked": True}
        if d == "uncheck":
            await loc.uncheck(timeout=3000)
            return {"unchecked": True}
        if d == "hover":
            await loc.hover(timeout=3000)
            return {"hovered": True}
        if d == "exists":
            return {"exists": await loc.count() > 0}
        raise ValueError(f"unknown 'do': {do}")

    # ── scrolling / drag / upload ────────────────────────────────────────

    def scroll(self, direction: str = "down", amount: int = 500, selector: str | None = None) -> dict:
        return self.run(self._scroll(direction, amount, selector))

    async def _scroll(self, direction: str, amount: int, selector: str | None) -> dict:
        if selector:
            await self._locator(selector).scroll_into_view_if_needed(timeout=3000)
            return {"scrolled_into_view": selector}
        dx = dy = 0
        a = int(amount)
        if direction == "down":
            dy = a
        elif direction == "up":
            dy = -a
        elif direction == "right":
            dx = a
        elif direction == "left":
            dx = -a
        await self.page.mouse.wheel(dx, dy)
        # mouse.wheel() resolves before the scroll is committed, so reading
        # scrollY immediately returns the OLD position (looks like the scroll
        # silently failed). Wait for the scroll to actually land.
        pos = await self.page.evaluate(
            """() => new Promise(resolve => {
                requestAnimationFrame(() => requestAnimationFrame(() => {
                    resolve({x: scrollX, y: scrollY});
                }));
            })"""
        )
        return {"scrollX": pos["x"], "scrollY": pos["y"]}

    def drag(self, source: str, target: str) -> dict:
        return self.run(self._drag(source, target))

    async def _drag(self, source: str, target: str) -> dict:
        await self._locator(source).drag_to(self._locator(target), timeout=3000)
        return {"dragged": source, "to": target}

    def upload(self, selector: str, files: list[str]) -> dict:
        return self.run(self._upload(selector, files))

    async def _upload(self, selector: str, files: list[str]) -> dict:
        await self._locator(selector).set_input_files(files, timeout=3000)
        return {"uploaded": len(files), "selector": selector}

    def check(self, selector: str, checked: bool = True) -> dict:
        return self.run(self._check(selector, checked))

    async def _check(self, selector: str, checked: bool) -> dict:
        loc = self._locator(selector)
        await (loc.check(timeout=3000) if checked else loc.uncheck(timeout=3000))
        return {"selector": selector, "checked": checked}

    def focus(self, selector: str) -> dict:
        return self.run(self._focus(selector))

    async def _focus(self, selector: str) -> dict:
        # Playwright's focus() resolves to None - wrap so the handler always
        # gets a dict and never trips over a None result.
        await self._locator(selector).focus(timeout=3000)
        return {"focused": selector}

    def dblclick(self, selector: str) -> dict:
        return self.run(self._dblclick(selector))

    async def _dblclick(self, selector: str) -> dict:
        await self._locator(selector).dblclick(timeout=3000)
        return {"double_clicked": selector}

    # ── browser settings ─────────────────────────────────────────────────

    def set_viewport(self, width: int, height: int, scale: float = 1.0) -> dict:
        return self.run(self._set_viewport(width, height, scale))

    async def _set_viewport(self, width: int, height: int, scale: float) -> dict:
        # set_viewport_size() resolves to None - wrap so callers always get a dict.
        await self.page.set_viewport_size({"width": int(width), "height": int(height)})
        return {"viewport": {"width": int(width), "height": int(height)}}

    def set_media(self, color_scheme: str | None = None, reduced_motion: str | None = None) -> dict:
        return self.run(self._set_media(color_scheme, reduced_motion))

    async def _set_media(self, color_scheme, reduced_motion) -> dict:
        kw: dict[str, Any] = {}
        if color_scheme:
            kw["color_scheme"] = color_scheme
        if reduced_motion:
            kw["reduced_motion"] = reduced_motion
        await self.page.emulate_media(**kw)
        return {"color_scheme": color_scheme, "reduced_motion": reduced_motion}

    def set_headers(self, headers: dict) -> dict:
        return self.run(self._set_headers(headers))

    async def _set_headers(self, headers: dict) -> dict:
        await self.context.set_extra_http_headers(headers)
        return {"headers": headers}

    def set_offline(self, offline: bool = True) -> dict:
        return self.run(self._set_offline(offline))

    async def _set_offline(self, offline: bool) -> dict:
        await self.context.set_offline(bool(offline))
        return {"offline": bool(offline)}

    def set_geolocation(self, latitude: float, longitude: float) -> dict:
        return self.run(self._set_geo(latitude, longitude))

    async def _set_geo(self, lat: float, lon: float) -> dict:
        await self.context.set_geolocation({"latitude": float(lat), "longitude": float(lon)})
        await self.context.grant_permissions(["geolocation"])
        return {"latitude": lat, "longitude": lon}

    # ── network interception ─────────────────────────────────────────────

    def route(self, pattern: str, action: str = "abort", body: str | None = None,
              status: int = 200, content_type: str = "application/json") -> dict:
        """Intercept matching requests: abort, mock a body, or continue.

        Mocking an API response without touching the backend is the single most
        useful thing here - it lets a page be tested in states the real server
        will not produce on demand (empty lists, 500s, slow payloads).
        """
        return self.run(self._route(pattern, action, body, status, content_type))

    async def _route(self, pattern, action, body, status, content_type) -> dict:
        if not hasattr(self, "_routes"):
            self._routes = {}

        async def _handler(route_obj):
            if action == "abort":
                await route_obj.abort()
            elif action == "mock":
                await route_obj.fulfill(
                    status=int(status),
                    body=(body or ""),
                    content_type=content_type,
                )
            else:
                await route_obj.continue_()

        await self.context.route(pattern, _handler)
        self._routes[pattern] = action
        return {"routed": pattern, "action": action}

    def unroute(self, pattern: str | None = None) -> dict:
        return self.run(self._unroute(pattern))

    async def _unroute(self, pattern) -> dict:
        if pattern:
            await self.context.unroute(pattern)
            return {"unrouted": pattern}
        await self.context.unroute_all()
        return {"unrouted": "all"}

    # ── HAR replay ───────────────────────────────────────────────────────

    def har_replay(self, path: str, not_found: str = "abort") -> dict:
        """Serve requests from a recorded HAR instead of the network.

        Recording is NOT possible mid-session: Playwright only writes a HAR when
        the context is CREATED, and our context already exists. To record, pass
        har_path to the call that launches the browser (see _start). Replay,
        however, can be switched on at any time, which is the more useful half
        anyway - it makes a capture replayable without a server.
        """
        return self.run(self._har_replay(path, not_found))

    async def _har_replay(self, path: str, not_found: str) -> dict:
        p = Path(os.path.expanduser(path))
        if not p.is_file():
            raise ValueError(f"HAR not found: {p}")
        await self.context.route_from_har(str(p), not_found=not_found)
        return {"replaying": str(p), "not_found": not_found}

_session: BrowserSession | None = None
_session_lock = threading.Lock()


def get_session() -> BrowserSession:
    """Return the process-wide browser session (created on first use)."""
    global _session
    with _session_lock:
        if _session is None:
            _session = BrowserSession()
        return _session
