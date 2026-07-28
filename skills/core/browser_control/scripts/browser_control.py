
#!/usr/bin/env python3
"""
Sable Browser Control — Persistent Playwright Daemon + CLI Client.

Architecture:
  - Daemon: asyncio server on Unix socket (/tmp/sable_browser.sock)
            Holds a persistent Chromium instance between calls.
  - Client: Thin CLI that connects, sends one JSON command, prints response.

Protocol: JSON-lines over Unix domain socket.
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
import signal
import sys
import time
import base64
from pathlib import Path

SOCKET_PATH = "/tmp/sable_browser.sock"
PID_FILE = "/tmp/sable_browser.pid"
SCREENSHOT_DIR = "/tmp/sable_browser_screenshots"


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
        from playwright.async_api import async_playwright
        self.pw = await async_playwright().start()

        # Select browser engine
        launcher = getattr(self.pw, self.browser_type, self.pw.chromium)
        launch_opts: dict = {
            "headless": self.headless,
            "args": [
                "--disable-blink-features=AutomationControlled",  # hide automation flag
                "--no-first-run",
                "--no-default-browser-check",
            ],
        }
        if self.executable_path:
            launch_opts["executable_path"] = self.executable_path

        if self.user_data_dir:
            # Clean stale Chromium lock files from previous hard kills
            for lock_name in ("SingletonLock", "SingletonSocket", "SingletonCookie", "DevToolsActivePort"):
                lock_path = Path(self.user_data_dir) / lock_name
                if lock_path.exists():
                    lock_path.unlink(missing_ok=True)

            # Persistent context — reuses cookies/sessions from profile dir
            self.context = await launcher.launch_persistent_context(
                self.user_data_dir,
                viewport={"width": 1280, "height": 800},
                user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                locale="en-US",
                timezone_id="Asia/Dhaka",
                **launch_opts,
            )
            self.browser = None  # persistent context has no separate browser obj
            self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        else:
            self.browser = await launcher.launch(**launch_opts)
            self.context = await self.browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                locale="en-US",
                timezone_id="Asia/Dhaka",
            )
            self.page = await self.context.new_page()

        # Inject stealth patches before any page JS runs
        await self.context.add_init_script(self._STEALTH_JS)

        self._attach_listeners(self.page)
        Path(SCREENSHOT_DIR).mkdir(exist_ok=True)

    def _attach_listeners(self, page):
        """Attach network + console listeners to a page."""
        page.on("request", self._on_request)
        page.on("response", self._on_response)
        page.on("console", self._on_console)

    def _on_request(self, request):
        if self._capturing_network:
            self._network_log.append({
                "type": "request",
                "method": request.method,
                "url": request.url[:300],
                "resource_type": request.resource_type,
                "headers": dict(request.headers) if request.headers else {},
                "post_data": (request.post_data or "")[:500],
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
        if self.browser:
            await self.browser.close()
        elif self.context:
            await self.context.close()
        if self.pw:
            await self.pw.stop()

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
        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)
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
        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)

        await self.start_browser()
        self.server = await asyncio.start_unix_server(
            self._handle_client, path=SOCKET_PATH
        )
        Path(PID_FILE).write_text(str(os.getpid()))
        print(f"Browser daemon started (pid={os.getpid()}, headless={self.headless})")
        print(f"Socket: {SOCKET_PATH}")
        sys.stdout.flush()

        async with self.server:
            await self.server.serve_forever()


# ─── CLI Client ───────────────────────────────────────────────────────────────

def send_command(msg: dict, timeout: float = 35.0) -> dict:
    """Connect to daemon socket, send command, return response."""
    import socket as sock_mod

    if not os.path.exists(SOCKET_PATH):
        return {"ok": False, "error": "Daemon not running. Start with: browser_control.py start"}

    s = sock_mod.socket(sock_mod.AF_UNIX, sock_mod.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(SOCKET_PATH)
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
        return {"ok": False, "error": "Daemon socket exists but refused connection. Try restarting."}
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
        _project_root = Path(__file__).resolve().parents[4]
        user_data_dir = str(_project_root / "automation-browser-data")

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
        daemon = BrowserDaemon(headless=headless, browser_type=browser_type,
                               executable_path=executable_path,
                               user_data_dir=user_data_dir)
        try:
            asyncio.run(daemon.run())
        except KeyboardInterrupt:
            pass
        return

    if action == "stop":
        if not daemon_alive():
            print("Daemon not running.")
            sys.exit(0)
        resp = send_command({"cmd": "stop"})
        print(json.dumps(resp, indent=2))
        time.sleep(0.5)
        # Cleanup stale files
        for f in [SOCKET_PATH, PID_FILE]:
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
