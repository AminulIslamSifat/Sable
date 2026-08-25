
#!/usr/bin/env python3
"""
Sable Browser Control — Persistent Playwright Daemon + CLI Client.

Architecture:
  - Daemon: asyncio server on Unix socket (POSIX) or TCP loopback (Windows).
            Holds a persistent Chromium instance between calls.
  - Client: Thin CLI that connects, sends one JSON command, prints response.

Protocol: JSON-lines over Unix domain socket (POSIX) or TCP 127.0.0.1 (Windows).
  Request:  {"cmd": "open", "args": ["https://example.com"], "kwargs": {}}
  Response: {"ok": true, "result": {...}} or {"ok": false, "error": "..."}

Usage:
  python3 browser_control.py start [--headless]   # launch daemon (bg)
  python3 browser_control.py stop                 # kill daemon
  python3 browser_control.py status               # health check
  python3 browser_control.py <cmd> [args...]      # send command to daemon
  python3 browser_control.py seq /tmp/seq.json    # run sequence file
"""

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import base64
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"

from engine.platform_paths import tmp_path as _tmp

# IPC transport: Unix socket on POSIX, TCP loopback on Windows
SOCKET_PATH = str(_tmp("sable_browser.sock"))   # POSIX only
PORT_FILE = str(_tmp("sable_browser.port"))     # Windows only
TCP_HOST = "127.0.0.1"
TCP_PORT = 0  # 0 = let OS assign; daemon writes actual port to PORT_FILE

PID_FILE = str(_tmp("sable_browser.pid"))
SCREENSHOT_DIR = str(_tmp("sable_browser_screenshots"))
CDP_PORT_FILE = str(_tmp("sable_browser_cdp_port"))


def _find_system_chrome() -> str | None:
    """Locate a real Chrome/Chromium binary on this system (cross-platform)."""
    from engine.platform_paths import system_chrome_candidates, find_playwright_chrome

    for c in system_chrome_candidates():
        has_sep = os.sep in c
        found = shutil.which(c) if not has_sep else (c if os.path.isfile(c) else None)
        if found:
            return found
    # Last resort: Playwright-bundled Chromium (newest first)
    return find_playwright_chrome()


# ─── Daemon ───────────────────────────────────────────────────────────────────

class BrowserDaemon:
    """Holds Playwright browser + page, serves commands over Unix socket."""

    def __init__(self, headless: bool = True, browser_type: str = "chromium",
                 executable_path: str = None, user_data_dir: str = None):
        self.headless = headless
        self.browser_type = browser_type
        self.executable_path = executable_path
        self.user_data_dir = user_data_dir
        self.pw = None
        self.browser = None
        self.context = None
        self.page = None  # active page
        self.server = None
        self._chrome_proc = None  # subprocess handle for system Chrome
        # DevTools capture buffers
        self._network_log: list[dict] = []
        self._console_log: list[dict] = []
        self._capturing_network = False
        self._capturing_console = False

    # Stealth init script — patches automation fingerprints
    _STEALTH_JS = """
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
    Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
    window.chrome = {runtime: {}, loadTimes: function(){}, csi: function(){}};
    Object.defineProperty(navigator, 'platform', {get: () => 'Linux x86_64'});
    """

    async def start_browser(self):
        """Launch Playwright's bundled browser directly (no subprocess, no CDP)."""
        from playwright.async_api import async_playwright

        self.pw = await async_playwright().start()

        # Use Playwright's native browser launch — clean, fast, no sandbox hammering
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
        ]
        if not self.headless and os.environ.get("WAYLAND_DISPLAY"):
            launch_args.append("--ozone-platform=wayland")

        launch_opts = {
            "headless": self.headless,
            "args": launch_args,
        }
        if self.executable_path:
            launch_opts["executable_path"] = self.executable_path

        # Select browser engine
        if self.browser_type == "firefox":
            self.browser = await self.pw.firefox.launch(**launch_opts)
        elif self.browser_type == "webkit":
            self.browser = await self.pw.webkit.launch(**launch_opts)
        else:
            self.browser = await self.pw.chromium.launch(chromium_sandbox=True, **launch_opts)

        # Persistent context if user_data_dir specified, else fresh context
        if self.user_data_dir:
            self.context = await self.pw.chromium.launch_persistent_context(
                self.user_data_dir,
                headless=self.headless,
                args=launch_args,
                executable_path=self.executable_path,
                chromium_sandbox=True,
            )
            self.browser = None  # persistent context owns the browser
            pages = self.context.pages
            self.page = pages[0] if pages else await self.context.new_page()
        else:
            self.context = await self.browser.new_context(
                user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            )
            self.page = await self.context.new_page()

        self._attach_listeners(self.page)
        self._attach_context_listeners()
        # Setup CDP on initial page for cross-origin iframe capture
        try:
            cdp = await self.context.new_cdp_session(self.page)
            await cdp.send("Network.enable")
            cdp.on("Network.requestWillBeSent", lambda params: self._on_cdp_request(params))
            cdp.on("Network.responseReceived", lambda params: self._on_cdp_response(params))
        except Exception as e:
            print(f"CDP setup warning: {e}")

    def _attach_listeners(self, page):
        """Attach network + console listeners to a page and all its frames."""
        page.on("request", self._on_request)
        page.on("response", self._on_response)
        page.on("console", self._on_console)
        # Also attach to all existing frames (cross-origin iframes)
        for frame in page.frames:
            if frame != page.main_frame:
                try:
                    frame.page.on("request", self._on_request)
                    frame.page.on("response", self._on_response)
                except Exception:
                    pass

    def _attach_context_listeners(self):
        """Attach listeners to context so new pages are auto-captured."""
        def _on_new_page(page):
            self._attach_listeners(page)
            # Also use CDP session for cross-origin iframe network capture
            try:
                import asyncio
                async def _setup_cdp():
                    try:
                        cdp = await page.context.new_cdp_session(page)
                        await cdp.send("Network.enable")
                        cdp.on("Network.requestWillBeSent", lambda params: self._on_cdp_request(params))
                        cdp.on("Network.responseReceived", lambda params: self._on_cdp_response(params))
                    except Exception:
                        pass
                asyncio.ensure_future(_setup_cdp())
            except Exception:
                pass
        self.context.on("page", _on_new_page)

    def _on_cdp_request(self, params):
        if self._capturing_network:
            req = params.get("request", {})
            self._network_log.append({
                "type": "request",
                "method": req.get("method", ""),
                "url": req.get("url", "")[:300],
                "resource_type": params.get("type", ""),
                "headers": req.get("headers", {}),
                "post_data": (req.get("postData") or "")[:5000],
                "ts": time.time(),
            })

    def _on_cdp_response(self, params):
        if self._capturing_network:
            resp = params.get("response", {})
            self._network_log.append({
                "type": "response",
                "status": resp.get("status", 0),
                "url": resp.get("url", "")[:300],
                "headers": resp.get("headers", {}),
                "ts": time.time(),
            })

    def _on_request(self, request):
        if self._capturing_network:
            self._network_log.append({
                "type": "request",
                "method": request.method,
                "url": request.url[:300],
                "resource_type": request.resource_type,
                "headers": dict(request.headers) if request.headers else {},
                "post_data": (request.post_data or "")[:5000],
                "ts": time.time(),
            })

    def _on_response(self, response):
        if self._capturing_network:
            self._network_log.append({
                "type": "response",
                "status": response.status,
                "url": response.url[:300],
                "headers": dict(response.headers) if response.headers else {},
                "ts": time.time(),
            })

    def _on_console(self, msg):
        if self._capturing_console:
            self._console_log.append({
                "level": msg.type,
                "text": msg.text[:500],
                "location": str(msg.location) if msg.location else None,
                "ts": time.time(),
            })

    async def stop_browser(self):
        try:
            if self.browser:
                await self.browser.close()
            elif self.context:
                await self.context.close()
        except Exception:
            pass
        if self.pw:
            try:
                await self.pw.stop()
            except Exception:
                pass
        # Clean up CDP port file if it exists from old runs
        if os.path.exists(CDP_PORT_FILE):
            os.unlink(CDP_PORT_FILE, missing_ok=True)

    @property
    def pages(self):
        return self.context.pages if self.context else []

    # ─── Command Handlers ─────────────────────────────────────────────────

    async def handle(self, msg: dict) -> dict:
        cmd = msg.get("cmd", "")
        args = msg.get("args", [])
        kwargs = msg.get("kwargs", {})
        handler = getattr(self, f"cmd_{cmd}", None)
        if not handler:
            return {"ok": False, "error": f"Unknown command: {cmd}"}
        try:
            result = await handler(*args, **kwargs)
            return {"ok": True, "result": result}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    async def cmd_status(self) -> dict:
        return {
            "alive": True,
            "url": self.page.url if self.page else None,
            "title": await self.page.title() if self.page else None,
            "tabs": len(self.pages),
            "headless": self.headless,
            "browser": self.browser_type,
            "executable": self.executable_path,
            "network_capturing": self._capturing_network,
            "console_capturing": self._capturing_console,
        }

    async def cmd_open(self, url: str, wait_until: str = "domcontentloaded") -> dict:
        resp = await self.page.goto(url, wait_until=wait_until, timeout=30000)
        return {
            "url": self.page.url,
            "title": await self.page.title(),
            "status": resp.status if resp else None,
        }

    async def cmd_back(self) -> dict:
        await self.page.go_back(timeout=15000)
        return {"url": self.page.url, "title": await self.page.title()}

    async def cmd_forward(self) -> dict:
        await self.page.go_forward(timeout=15000)
        return {"url": self.page.url, "title": await self.page.title()}

    async def cmd_reload(self) -> dict:
        await self.page.reload(timeout=15000)
        return {"url": self.page.url, "title": await self.page.title()}

    async def cmd_dump(self, selector: str = "body", depth: int = 6) -> dict:
        """Structured DOM dump for LLM reasoning."""
        return await self._dom_dump(selector, depth)

    def _prune_tree(self, node: dict, depth: int) -> dict:
        if depth <= 0:
            return {"role": node.get("role"), "name": node.get("name", "")[:80]}
        result = {
            "role": node.get("role"),
            "name": (node.get("name") or "")[:120],
        }
        if node.get("value"):
            result["value"] = str(node["value"])[:100]
        if node.get("description"):
            result["desc"] = node["description"][:80]
        children = node.get("children", [])
        if children:
            result["children"] = [
                self._prune_tree(c, depth - 1) for c in children[:30]
            ]
        return result

    async def _dom_dump(self, selector: str, depth: int) -> dict:
        """Fallback: simplified HTML structure."""
        js = """([sel, maxDepth]) => {
            function walk(el, d) {
                if (d <= 0 || !el) return null;
                const tag = el.tagName?.toLowerCase();
                if (!tag || ['script','style','svg','path'].includes(tag)) return null;
                const node = {tag};
                if (el.id) node.id = el.id;
                if (el.className && typeof el.className === 'string')
                    node.class = el.className.split(' ').slice(0,3).join(' ');
                const text = el.childNodes.length === 1 &&
                    el.childNodes[0].nodeType === 3
                    ? el.textContent.trim().slice(0, 80) : null;
                if (text) node.text = text;
                if (el.getAttribute('role')) node.role = el.getAttribute('role');
                if (el.getAttribute('aria-label')) node.label = el.getAttribute('aria-label');
                if (el.getAttribute('href')) node.href = el.getAttribute('href').slice(0,100);
                if (el.getAttribute('type')) node.type = el.getAttribute('type');
                if (el.getAttribute('placeholder')) node.placeholder = el.getAttribute('placeholder');
                const kids = [...el.children].map(c => walk(c, d-1)).filter(Boolean);
                if (kids.length) node.children = kids.slice(0, 25);
                return node;
            }
            const root = document.querySelector(sel);
            return walk(root, maxDepth);
        }"""
        tree = await self.page.evaluate(js, [selector, depth])
        return {"dom": tree}

    async def cmd_dump_html(self, selector: str = "body") -> dict:
        """Raw outer HTML of element (truncated)."""
        el = await self.page.query_selector(selector)
        if not el:
            return {"error": f"Selector not found: {selector}"}
        html = await el.evaluate("el => el.outerHTML")
        return {"html": html[:15000], "truncated": len(html) > 15000}

    async def cmd_click(self, selector: str, timeout: int = 10000,
                        force: bool = False) -> dict:
        await self.page.click(selector, timeout=timeout, force=force)
        await self.page.wait_for_load_state("domcontentloaded", timeout=5000)
        return {
            "clicked": selector,
            "url": self.page.url,
            "title": await self.page.title(),
        }

    async def cmd_type(self, selector: str, text: str, clear: bool = True,
                       force: bool = False) -> dict:
        if clear:
            await self.page.fill(selector, text, timeout=10000, force=force)
        else:
            await self.page.type(selector, text, timeout=10000)
        return {"typed_into": selector, "text": text}

    async def cmd_press(self, key: str) -> dict:
        await self.page.keyboard.press(key)
        return {"pressed": key}

    async def cmd_hover(self, selector: str) -> dict:
        await self.page.hover(selector, timeout=10000)
        return {"hovered": selector}

    async def cmd_select(self, selector: str, value: str) -> dict:
        await self.page.select_option(selector, value, timeout=10000)
        return {"selected": value, "in": selector}

    async def cmd_extract(self, selector: str, attribute: str = None) -> dict:
        els = await self.page.query_selector_all(selector)
        if not els:
            return {"found": 0, "values": []}
        values = []
        for el in els[:50]:
            if attribute:
                v = await el.get_attribute(attribute)
            else:
                v = await el.inner_text()
            values.append((v or "").strip()[:200])
        return {"found": len(els), "values": values}

    async def cmd_screenshot(self, full_page: bool = False, selector: str = None) -> dict:
        ts = int(time.time() * 1000)
        path = f"{SCREENSHOT_DIR}/shot_{ts}.png"
        if selector:
            el = await self.page.query_selector(selector)
            if not el:
                return {"error": f"Selector not found: {selector}"}
            await el.screenshot(path=path)
        else:
            await self.page.screenshot(path=path, full_page=full_page)
        size = os.path.getsize(path)
        return {"path": path, "size_kb": round(size / 1024, 1)}

    async def cmd_eval(self, js: str) -> dict:
        """Execute JS in page context. Supports raw JS, base64 (b64:...), or file (file:/path)."""
        # Decode base64 payload: "b64:dGVzdA==" -> "test"
        if js.startswith("b64:"):
            js = base64.b64decode(js[4:]).decode("utf-8")
        # Read from file: "file:/tmp/script.js"
        elif js.startswith("file:"):
            fpath = Path(js[5:])
            if not fpath.exists():
                return {"error": f"JS file not found: {fpath}"}
            js = fpath.read_text(encoding="utf-8")
        result = await self.page.evaluate(js)
        # Ensure JSON-serializable
        try:
            json.dumps(result)
        except (TypeError, ValueError):
            result = str(result)
        return {"result": result}

    async def cmd_eval_frame(self, url_pattern: str, js: str) -> dict:
        """Execute JS in an iframe matching url_pattern. Supports b64: and file: prefixes."""
        if js.startswith("b64:"):
            js = base64.b64decode(js[4:]).decode("utf-8")
        elif js.startswith("file:"):
            fpath = Path(js[5:])
            if not fpath.exists():
                return {"error": f"JS file not found: {fpath}"}
            js = fpath.read_text(encoding="utf-8")
        # Find matching frame
        for frame in self.page.frames:
            if url_pattern in (frame.url or ""):
                result = await frame.evaluate(js)
                try:
                    json.dumps(result)
                except (TypeError, ValueError):
                    result = str(result)
                return {"result": result, "frame_url": frame.url}
        urls = [f.url for f in self.page.frames]
        return {"error": f"No frame matching '{url_pattern}'. Available: {urls}"}

    async def cmd_wait(self, selector: str, timeout: int = 15000,
                       state: str = "visible") -> dict:
        await self.page.wait_for_selector(selector, timeout=timeout, state=state)
        return {"visible": selector}

    async def cmd_wait_url(self, substring: str, timeout: int = 15000) -> dict:
        await self.page.wait_for_url(f"**{substring}**", timeout=timeout)
        return {"url": self.page.url}

    async def cmd_wait_load(self, state: str = "networkidle",
                            timeout: int = 15000) -> dict:
        await self.page.wait_for_load_state(state, timeout=timeout)
        return {"state": state, "url": self.page.url}

    # ─── Tab Management ───────────────────────────────────────────────────

    async def cmd_tabs(self) -> dict:
        tabs = []
        for i, p in enumerate(self.pages):
            tabs.append({"index": i, "url": p.url, "title": await p.title()})
        return {"tabs": tabs, "active": self.pages.index(self.page)}

    async def cmd_tab_new(self, url: str = None) -> dict:
        self.page = await self.context.new_page()
        if url:
            await self.page.goto(url, timeout=30000)
        return {"index": len(self.pages) - 1, "url": self.page.url}

    async def cmd_tab_switch(self, index) -> dict:
        index = int(index)
        pages = self.pages
        if index < 0 or index >= len(pages):
            return {"error": f"Tab index {index} out of range (0-{len(pages)-1})"}
        self.page = pages[index]
        await self.page.bring_to_front()
        return {"active": index, "url": self.page.url}

    async def cmd_tab_close(self, index=None) -> dict:
        pages = self.pages
        if index is None:
            target = self.page
        else:
            index = int(index)
            if index < 0 or index >= len(pages):
                return {"error": f"Tab index {index} out of range"}
            target = pages[index]
        await target.close()
        if self.page == target or self.page.is_closed():
            self.page = self.pages[0] if self.pages else await self.context.new_page()
        return {"remaining": len(self.pages)}

    # ─── Cookies / Storage ────────────────────────────────────────────────

    async def cmd_cookies(self, url: str = None) -> dict:
        cookies = await self.context.cookies(url) if url else await self.context.cookies()
        return {"count": len(cookies), "cookies": cookies[:50]}

    async def cmd_storage(self, kind: str = "local") -> dict:
        """Read localStorage or sessionStorage."""
        js = f"""() => {{
            const store = window.{kind}Storage;
            const items = {{}};
            for (let i = 0; i < store.length; i++) {{
                const key = store.key(i);
                items[key] = store.getItem(key)?.slice(0, 200);
            }}
            return items;
        }}"""
        items = await self.page.evaluate(js)
        return {"type": kind, "count": len(items), "items": items}

    async def cmd_storage_set(self, kind: str = "local", key: str = "",
                              value: str = "") -> dict:
        js = f"() => window.{kind}Storage.setItem(arguments[0], arguments[1])"
        await self.page.evaluate(f"() => window.{kind}Storage.setItem('{key}', '{value}')")
        return {"set": key, "in": kind}

    # ─── DevTools: Network ────────────────────────────────────────────────

    async def cmd_network_start(self) -> dict:
        """Start capturing network traffic."""
        self._network_log.clear()
        self._capturing_network = True
        return {"capturing": True}

    async def cmd_network_stop(self) -> dict:
        """Stop capturing, return summary."""
        self._capturing_network = False
        return {"capturing": False, "entries_captured": len(self._network_log)}

    async def cmd_network_log(self, filter_type: str = None,
                              filter_url: str = None, limit: int = 50) -> dict:
        """Retrieve captured network entries."""
        entries = self._network_log
        if filter_type:
            entries = [e for e in entries if e.get("resource_type") == filter_type]
        if filter_url:
            entries = [e for e in entries if filter_url in e.get("url", "")]
        return {
            "total": len(self._network_log),
            "showing": len(entries[-limit:]),
            "entries": entries[-limit:],
        }

    async def cmd_network_clear(self) -> dict:
        self._network_log.clear()
        return {"cleared": True}

    # ─── DevTools: Console ────────────────────────────────────────────────

    async def cmd_console_start(self) -> dict:
        """Start capturing console messages."""
        self._console_log.clear()
        self._capturing_console = True
        return {"capturing": True}

    async def cmd_console_stop(self) -> dict:
        self._capturing_console = False
        return {"capturing": False, "entries_captured": len(self._console_log)}

    async def cmd_console_log(self, level: str = None, limit: int = 50) -> dict:
        """Retrieve captured console messages."""
        entries = self._console_log
        if level:
            entries = [e for e in entries if e.get("level") == level]
        return {
            "total": len(self._console_log),
            "showing": len(entries[-limit:]),
            "entries": entries[-limit:],
        }

    async def cmd_console_clear(self) -> dict:
        self._console_log.clear()
        return {"cleared": True}

    # ─── DevTools: Page Source & CSS ──────────────────────────────────────

    async def cmd_source(self) -> dict:
        """Full page HTML source."""
        html = await self.page.content()
        return {"html": html[:30000], "length": len(html), "truncated": len(html) > 30000}

    async def cmd_css(self, selector: str, properties: str = None) -> dict:
        """Get computed styles for an element."""
        js = """([sel, props]) => {
            const el = document.querySelector(sel);
            if (!el) return null;
            const computed = window.getComputedStyle(el);
            if (props) {
                const result = {};
                props.split(',').forEach(p => {
                    p = p.trim();
                    result[p] = computed.getPropertyValue(p);
                });
                return result;
            }
            // Return all non-default-looking properties
            const all = {};
            for (let i = 0; i < computed.length; i++) {
                const prop = computed[i];
                all[prop] = computed.getPropertyValue(prop);
            }
            return all;
        }"""
        result = await self.page.evaluate(js, [selector, properties])
        if result is None:
            return {"error": f"Selector not found: {selector}"}
        return {"selector": selector, "styles": result}

    async def cmd_performance(self) -> dict:
        """Page performance timing entries."""
        js = """() => {
            const nav = performance.getEntriesByType('navigation')[0];
            const resources = performance.getEntriesByType('resource');
            return {
                navigation: nav ? {
                    dns: Math.round(nav.domainLookupEnd - nav.domainLookupStart),
                    tcp: Math.round(nav.connectEnd - nav.connectStart),
                    ttfb: Math.round(nav.responseStart - nav.requestStart),
                    dom_ready: Math.round(nav.domContentLoadedEventEnd - nav.startTime),
                    load: Math.round(nav.loadEventEnd - nav.startTime),
                } : null,
                resource_count: resources.length,
                slowest: resources
                    .sort((a,b) => b.duration - a.duration)
                    .slice(0, 10)
                    .map(r => ({name: r.name.split('/').pop().slice(0,50), ms: Math.round(r.duration)})),
            };
        }"""
        return await self.page.evaluate(js)

    # ─── Sequence Execution ───────────────────────────────────────────────

    async def cmd_seq(self, json_path: str) -> dict:
        path = Path(json_path)
        if not path.exists():
            return {"error": f"Sequence file not found: {json_path}"}
        steps = json.loads(path.read_text())
        results = []
        for i, step in enumerate(steps):
            cmd = step.get("cmd", "")
            args = step.get("args", [])
            kwargs = step.get("kwargs", {})
            wait_ms = step.get("wait_ms", 300)

            resp = await self.handle({"cmd": cmd, "args": args, "kwargs": kwargs})
            results.append({"step": i, "cmd": cmd, **resp})

            if not resp.get("ok"):
                results.append({"step": i, "aborted": True, "reason": resp["error"]})
                break
            if wait_ms > 0:
                await asyncio.sleep(wait_ms / 1000)

        return {"steps_run": len(results), "results": results}

    async def cmd_stop(self) -> dict:
        # Schedule shutdown after responding
        asyncio.get_event_loop().call_soon(lambda: asyncio.ensure_future(self._shutdown()))
        return {"stopping": True}

    async def _shutdown(self):
        await self.stop_browser()
        if self.server:
            self.server.close()
        ipc_file = PORT_FILE if IS_WINDOWS else SOCKET_PATH
        if os.path.exists(ipc_file):
            os.unlink(ipc_file)
        if os.path.exists(PID_FILE):
            os.unlink(PID_FILE)

    # ─── Socket Server ────────────────────────────────────────────────────

    async def _handle_client(self, reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter):
        try:
            data = await reader.readline()
            if not data:
                return
            msg = json.loads(data.decode())
            response = await self.handle(msg)
            writer.write((json.dumps(response) + "\n").encode())
            await writer.drain()
        except Exception as e:
            err = json.dumps({"ok": False, "error": str(e)}) + "\n"
            writer.write(err.encode())
            await writer.drain()
        finally:
            writer.close()

    async def run(self):
        # Clean stale IPC endpoint
        if IS_WINDOWS:
            if os.path.exists(PORT_FILE):
                os.unlink(PORT_FILE)
        else:
            if os.path.exists(SOCKET_PATH):
                os.unlink(SOCKET_PATH)

        await self.start_browser()

        if IS_WINDOWS:
            self.server = await asyncio.start_server(
                self._handle_client, TCP_HOST, TCP_PORT
            )
            # Write actual port so client can discover it
            port = self.server.sockets[0].getsockname()[1]
            Path(PORT_FILE).write_text(str(port))
            ipc_label = f"TCP {TCP_HOST}:{port}"
        else:
            self.server = await asyncio.start_unix_server(
                self._handle_client, path=SOCKET_PATH
            )
            ipc_label = SOCKET_PATH

        Path(PID_FILE).write_text(str(os.getpid()))
        print(f"Browser daemon started (pid={os.getpid()}, headless={self.headless})")
        print(f"IPC: {ipc_label}")
        sys.stdout.flush()

        async with self.server:
            await self.server.serve_forever()


# ─── CLI Client ───────────────────────────────────────────────────────────────

def send_command(msg: dict, timeout: float = 35.0) -> dict:
    """Connect to daemon IPC endpoint, send command, return response."""
    import socket as sock_mod

    if IS_WINDOWS:
        if not os.path.exists(PORT_FILE):
            return {"ok": False, "error": "Daemon not running. Start with: browser_control.py start"}
        port = int(Path(PORT_FILE).read_text().strip())
        s = sock_mod.socket(sock_mod.AF_INET, sock_mod.SOCK_STREAM)
        connect_target = (TCP_HOST, port)
    else:
        if not os.path.exists(SOCKET_PATH):
            return {"ok": False, "error": "Daemon not running. Start with: browser_control.py start"}
        s = sock_mod.socket(sock_mod.AF_UNIX, sock_mod.SOCK_STREAM)
        connect_target = SOCKET_PATH

    s.settimeout(timeout)
    try:
        s.connect(connect_target)
        s.sendall((json.dumps(msg) + "\n").encode())
        data = b""
        while b"\n" not in data:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
        return json.loads(data.decode().strip())
    except sock_mod.timeout:
        return {"ok": False, "error": "Timeout waiting for daemon response"}
    except ConnectionRefusedError:
        return {"ok": False, "error": "Daemon IPC endpoint exists but refused connection. Try restarting."}
    finally:
        s.close()


def daemon_alive() -> bool:
    if not os.path.exists(PID_FILE):
        return False
    pid = int(Path(PID_FILE).read_text().strip())
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    action = sys.argv[1]

    # ─── Daemon lifecycle ─────────────────────────────────────────────────
    if action == "start":
        headless = "--headed" not in sys.argv
        browser_type = "chromium"
        executable_path = None
        # Default to project-local automation profile (persistent sessions)
        try:
            from engine.config import BROWSER_AUTOMATION_DATA_DIR
            user_data_dir = str(BROWSER_AUTOMATION_DATA_DIR)
        except ImportError:
            _project_root = Path(__file__).resolve().parents[4]
            user_data_dir = str(_project_root / "system" / "automation-browser-data")

        for i, arg in enumerate(sys.argv):
            if arg == "--browser" and i + 1 < len(sys.argv):
                browser_type = sys.argv[i + 1]
            elif arg.startswith("--browser="):
                browser_type = arg.split("=", 1)[1]
            elif arg == "--executable" and i + 1 < len(sys.argv):
                executable_path = sys.argv[i + 1]
            elif arg.startswith("--executable="):
                executable_path = arg.split("=", 1)[1]
            elif arg == "--user-data-dir" and i + 1 < len(sys.argv):
                user_data_dir = sys.argv[i + 1]
            elif arg.startswith("--user-data-dir="):
                user_data_dir = arg.split("=", 1)[1]

        if daemon_alive():
            print("Daemon already running.")
            sys.exit(0)

        # Launch daemon as background subprocess (reliable, no fork issues)
        import subprocess as _sp
        log_path = str(_tmp("browser_daemon.log"))
        cmd = [sys.executable, __file__, "_run_daemon",
               "--headless" if headless else "--headed",
               f"--browser={browser_type}"]
        if executable_path:
            cmd.append(f"--executable={executable_path}")
        if user_data_dir:
            cmd.append(f"--user-data-dir={user_data_dir}")
        with open(log_path, "a") as log_f:
            proc = _sp.Popen(cmd, stdout=log_f, stderr=log_f,
                             start_new_session=True)
        Path(PID_FILE).write_text(str(proc.pid))
        print(f"Browser daemon started (pid={proc.pid}, headless={headless})")
        ipc_label = PORT_FILE if IS_WINDOWS else SOCKET_PATH
        print(f"IPC: {ipc_label}")
        return

        # Grandchild: redirect stdio and run daemon
        sys.stdin = open(os.devnull, "r")
        log_path = str(_tmp("browser_daemon.log"))
        log_fd = open(log_path, "a")
        sys.stdout = log_fd
        sys.stderr = log_fd

        Path(PID_FILE).write_text(str(os.getpid()))

        daemon = BrowserDaemon(headless=headless, browser_type=browser_type,
                               executable_path=executable_path,
                               user_data_dir=user_data_dir)
        try:
            asyncio.run(daemon.run())
        except Exception as e:
            print(f"Daemon crashed: {e}", file=log_fd)
        finally:
            ipc_file = PORT_FILE if IS_WINDOWS else SOCKET_PATH
            for f in [ipc_file, PID_FILE]:
                if os.path.exists(f):
                    os.unlink(f)
        os._exit(0)

    if action == "stop":
        if not daemon_alive():
            print("Daemon not running.")
            sys.exit(0)
        resp = send_command({"cmd": "stop"})
        print(json.dumps(resp, indent=2))
        time.sleep(0.5)
        # Cleanup stale files
        ipc_file = PORT_FILE if IS_WINDOWS else SOCKET_PATH
        for f in [ipc_file, PID_FILE]:
            if os.path.exists(f):
                os.unlink(f)
        return

    if action == "status":
        if not daemon_alive():
            print(json.dumps({"ok": False, "error": "Daemon not running"}))
            sys.exit(1)
        resp = send_command({"cmd": "status"})
        print(json.dumps(resp, indent=2))
        return

    # ─── Internal: run daemon in foreground (called by start via subprocess) ──
    if action == "_run_daemon":
        headless = "--headed" not in sys.argv
        browser_type = "chromium"
        executable_path = None
        user_data_dir = None
        for i, arg in enumerate(sys.argv):
            if arg == "--browser" and i + 1 < len(sys.argv):
                browser_type = sys.argv[i + 1]
            elif arg.startswith("--browser="):
                browser_type = arg.split("=", 1)[1]
            elif arg == "--executable" and i + 1 < len(sys.argv):
                executable_path = sys.argv[i + 1]
            elif arg.startswith("--executable="):
                executable_path = arg.split("=", 1)[1]
            elif arg == "--user-data-dir" and i + 1 < len(sys.argv):
                user_data_dir = sys.argv[i + 1]
            elif arg.startswith("--user-data-dir="):
                user_data_dir = arg.split("=", 1)[1]
        if not user_data_dir:
            try:
                from engine.config import BROWSER_AUTOMATION_DATA_DIR
                user_data_dir = str(BROWSER_AUTOMATION_DATA_DIR)
            except ImportError:
                _project_root = Path(__file__).resolve().parents[4]
                user_data_dir = str(_project_root / "system" / "automation-browser-data")
        daemon = BrowserDaemon(headless=headless, browser_type=browser_type,
                               executable_path=executable_path,
                               user_data_dir=user_data_dir)
        try:
            asyncio.run(daemon.run())
        except Exception as e:
            print(f"Daemon crashed: {e}")
        finally:
            ipc_file = PORT_FILE if IS_WINDOWS else SOCKET_PATH
            for f in [ipc_file, PID_FILE]:
                if os.path.exists(f):
                    os.unlink(f)
        return

    # ─── Command passthrough ──────────────────────────────────────────────
    cmd = action
    args = sys.argv[2:]

    # Special handling for seq (file path)
    if cmd == "seq":
        if not args:
            print("Usage: browser_control.py seq /path/to/seq.json")
            sys.exit(1)
        resp = send_command({"cmd": "seq", "args": [args[0]]}, timeout=120)
        print(json.dumps(resp, indent=2))
        return

    # Parse --kwargs flags
    kwargs = {}
    positional = []
    i = 0
    while i < len(args):
        if args[i].startswith("--") and "=" in args[i]:
            k, v = args[i][2:].split("=", 1)
            # Try to parse as JSON for bools/ints
            try:
                kwargs[k] = json.loads(v)
            except (json.JSONDecodeError, ValueError):
                kwargs[k] = v
        else:
            positional.append(args[i])
        i += 1

    # eval convenience: --b64 <string> or --file <path> wraps the JS payload
    if cmd == "eval" and positional:
        raw = positional[0]
        if raw.startswith("b64:") or raw.startswith("file:"):
            pass  # already prefixed, cmd_eval handles decoding
        elif "--b64" in args:
            positional[0] = f"b64:{raw}"
        elif "--file" in args:
            positional[0] = f"file:{raw}"

    resp = send_command({"cmd": cmd, "args": positional, "kwargs": kwargs})
    print(json.dumps(resp, indent=2))


if __name__ == "__main__":
    main()
#
