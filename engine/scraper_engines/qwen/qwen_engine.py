import sys
import time
import os
import asyncio
import subprocess
import shutil
import signal
import json
import hashlib
import re
import socket
import http.server
import socketserver
import urllib.request
import threading
import websockets
from datetime import datetime

from config import console, PLATFORM, PLATFORMS_CONFIG, OUTPUT_ROOT, ASSETS_DIR, PROJECT_ROOT, INSTRUCTIONS_DIR
from exceptions import ResponseCaptureError

class ProxyHTTPHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Suppress logs

    def do_GET(self):
        target_port = getattr(ProxyHTTPHandler, "target_port", 9222)
        url = f"http://127.0.0.1:{target_port}{self.path}"
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req) as response:
                content = response.read()
                
            if "/json" in self.path:
                content_str = content.decode("utf-8", errors="ignore")
                # Replace webview with page in JSON responses
                content_str = content_str.replace('"type": "webview"', '"type": "page"')
                content_str = content_str.replace('"type":"webview"', '"type":"page"')
                # Redirect WebSocket connections to our WebSocket proxy on our dynamic port!
                ws_port = getattr(ProxyHTTPHandler, "ws_port", 9224)
                content_str = content_str.replace(f"127.0.0.1:{target_port}", f"127.0.0.1:{ws_port}")
                content_str = content_str.replace(f"localhost:{target_port}", f"127.0.0.1:{ws_port}")
                content = content_str.encode("utf-8")
                
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=UTF-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        except Exception as e:
            self.send_error(500, f"Proxy error: {e}")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CHROME_BINARIES = [
    "thorium-browser-avx2",
    "google-chrome-stable",
    "google-chrome",
    "chromium-browser",
    "chromium",
]

# Fallback: Playwright-bundled Chromium (check newest version first)
_PLAYWRIGHT_CHROME_GLOB = os.path.expanduser("~/hdd/cache/ms-playwright/chromium-*/chrome-linux64/chrome")

_INPUT_SELECTORS = [
    PLATFORM["selectors"]["input"],
    "textarea[formcontrolname='promptText']",
    "textarea[aria-label='Enter a prompt']",
    "textarea.textarea",
]

_STOP_ACTIVE_SELECTORS = [
    PLATFORM["selectors"].get("stop", "div.chat-prompt-send-button button.stop-button"),
]

_RESPONSE_SEL     = PLATFORM["selectors"].get("content", "div.response-message-content")

_INSTRUCTION_PATHS = [
    str(INSTRUCTIONS_DIR / "Maria.md"),
    str(INSTRUCTIONS_DIR / "output_format.md"),
    str(INSTRUCTIONS_DIR / "skills.md"),
]

_UI_GARBAGE_WORDS = (
    "code|markdown|download|content_copy|expand_less|expand_more|"
    "keyboard_arrow_down|keyboard_arrow_up|thinking|thoughts|edit|"
    "share|more_vert|thumb_up|thumb_down|copy"
)

_THOUGHT_IGNORED_EXACT = {
    "thoughts", "thinking", "expand to view model thoughts",
    "chevron_right", "expand_less", "expand_more",
}

_GHOST_CSS = """
    /* Softened Ghost CSS */
    .feedback-container, .help-button, .upgrade-card,
    .command-palette-button, button[aria-label="What's new"],
    .category-card, .logo-wrapper,
    button[aria-label="View related products"] { display: none !important; }
    .v3-token-count-value:not(.ghost-show) { display: none !important; }
    .ghost-show {
        display: block !important;
        visibility: visible !important;
        opacity: 1 !important;
    }
"""


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def collect_session_markdowns_chronological(sessions_base: str) -> list[str]:
    """
    List every *.md in each YYYY-MM-DD day folder (name-hh-mm-ss.md), oldest first.
    Only direct children of each date folder are included.
    """
    paths: list[str] = []
    if not os.path.isdir(sessions_base):
        return paths
    for entry in sorted(os.listdir(sessions_base)):
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", entry):
            continue
        day_dir = os.path.join(sessions_base, entry)
        if not os.path.isdir(day_dir):
            continue
        for f in sorted(os.listdir(day_dir)):
            if f.endswith(".md"):
                paths.append(os.path.join(day_dir, f))
    return paths


def _sha256_files(*paths: str) -> str:
    hasher = hashlib.sha256()
    for path in paths:
        if os.path.exists(path):
            try:
                with open(path, "rb") as f:
                    hasher.update(f.read())
            except Exception:
                pass
    return hasher.hexdigest()


def _detect_clipboard_tool() -> str | None:
    if shutil.which("wl-paste"):
        return "wl-paste"
    if shutil.which("xclip"):
        return "xclip"
    return None


# ---------------------------------------------------------------------------
# GhostChat
# ---------------------------------------------------------------------------

class GhostChat:
    def is_obsidian_running_on_port(self, port: int) -> bool:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.3)
                if s.connect_ex(("127.0.0.1", port)) != 0:
                    return False
        except Exception:
            return False

        try:
            import urllib.request
            import json
            req = urllib.request.Request(f"http://127.0.0.1:{port}/json", headers={"User-Agent": "GhostChat"})
            with urllib.request.urlopen(req, timeout=0.8) as response:
                pages = json.loads(response.read().decode("utf-8"))
                for page in pages:
                    url = page.get("url", "")
                    if "obsidian.md/index.html" in url or "app://obsidian.md" in url:
                        return True
        except Exception:
            pass
        return False

    def __init__(self, port: int = 9222, viewer: bool = True, show_thoughts: bool = False) -> None:
        from config import PLATFORMS_CONFIG
        use_obsidian_config = PLATFORMS_CONFIG.get("use_obsidian", True)
        if not use_obsidian_config and port == 9222:
            if self.is_obsidian_running_on_port(9222):
                port = 9225
                console.print("[bold yellow]Obsidian detected on port 9222, but 'use_obsidian' is false in platforms.json. Diverting Ghost Engine to port 9225! [/bold yellow]")

        self.port              = port
        self.viewer            = viewer
        self.show_thoughts     = show_thoughts
        self.user_data_dir     = os.path.expanduser("~/.local/share/ghostchat/chrome-data")
        self.chrome_process    = None
        self.pw                = None
        self.browser           = None
        self.context           = None
        self.page              = None
        self.last_instruction_hash: str | None = None
        self.last_thought_expand_at: float = 0
        self.last_tokens: str | None = None
        self.mutation_event    = asyncio.Event()
        self.clipboard_tool    = _detect_clipboard_tool()
        self.system_injected   = False
        self.image_attached    = False

        logs_dir = os.path.join(OUTPUT_ROOT, "logs")
        os.makedirs(logs_dir, exist_ok=True)
        self.response_log_file = os.path.join(
            logs_dir, f"response_capture_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        )
        self._log_debug("ghostchat_session_started", port=self.port, viewer=self.viewer)

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def _log_debug(self, event: str, **fields) -> None:
        try:
            payload = {"ts": datetime.now().isoformat(timespec="seconds"), "event": event, **fields}
            with open(self.response_log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Launch & Connect
    # ------------------------------------------------------------------

    async def is_port_open(self) -> bool:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                return s.connect_ex(("127.0.0.1", self.port)) == 0
        except Exception:
            return False

    async def _is_own_headed_session(self) -> bool:
        """Check if existing CDP on our port is our own headed browser with matching profile."""
        if not await self.is_port_open():
            return False
        try:
            url = f"http://127.0.0.1:{self.port}/json/version"
            req = urllib.request.Request(url, headers={"User-Agent": "GhostChat"})
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                data = json.loads(resp.read().decode())
            # Must match our user_data_dir
            remote_profile = data.get("userDataDir", "") or data.get("profile-path", "")
            if os.path.realpath(remote_profile) != os.path.realpath(self.user_data_dir):
                console.print(f"[bold yellow]CDP on port {self.port} belongs to different profile ({remote_profile}), launching fresh.[/bold yellow]")
                return False
            # Must be headed (no --headless in BrowserCommandLine)
            cmd_line = data.get("BrowserCommandLine", "")
            if "--headless" in cmd_line:
                console.print(f"[bold yellow]CDP on port {self.port} is headless, need headed — launching fresh.[/bold yellow]")
                return False
            return True
        except Exception as exc:
            console.print(f"[dim yellow]Could not verify existing CDP session: {exc}[/dim yellow]")
            return False

    async def launch_chrome(self) -> None:
        if await self._is_own_headed_session():
            console.print("[bold purple]Found existing headed session with matching profile! Ready to connect...[/bold purple] 🔌")
            return

        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            try:
                path = os.path.join(self.user_data_dir, name)
                if os.path.exists(path) or os.path.islink(path):
                    os.remove(path)
            except Exception:
                pass

        chrome_path = next((shutil.which(b) for b in _CHROME_BINARIES if shutil.which(b)), None)
        if not chrome_path:
            # Fallback to Playwright-bundled Chromium (newest version)
            import glob as _glob
            candidates = sorted(_glob.glob(_PLAYWRIGHT_CHROME_GLOB), reverse=True)
            chrome_path = candidates[0] if candidates else None
        if not chrome_path:
            console.print("[bold red]❌ No Thorium/Chrome/Playwright-Chromium found![/bold red]")
            sys.exit(1)

        console.print("[bold purple]Launching Ghost Engine...[/bold purple] 🚀")
        cmd = [
            chrome_path,
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={self.user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--force-dark-mode",
            "--enable-features=WebUIDarkMode",
            "--ozone-platform-hint=auto",
            "--disable-blink-features=AutomationControlled",
        ]
        if not self.viewer:
            cmd += [
                "--headless=new",
                "--window-size=1920,1080",
                "--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
            ]
        browser_log = os.path.join(
            os.path.dirname(self.response_log_file),
            f"browser_startup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log",
        )
        log_file = open(browser_log, "a", encoding="utf-8")
        self.chrome_process = subprocess.Popen(
            cmd, stdout=log_file, stderr=subprocess.STDOUT, preexec_fn=os.setsid
        )
        self.chrome_log_file = browser_log
        await self._wait_for_cdp_ready()

    async def _wait_for_cdp_ready(self, timeout: float = 30.0) -> None:
        deadline = time.time() + timeout
        last_error: Exception | None = None
        version_url = f"http://127.0.0.1:{self.port}/json/version"

        while time.time() < deadline:
            if self.chrome_process and self.chrome_process.poll() is not None:
                break

            if await self.is_port_open():
                try:
                    req = urllib.request.Request(version_url, headers={"User-Agent": "GhostChat"})
                    with urllib.request.urlopen(req, timeout=1.0) as response:
                        if response.status == 200:
                            return
                except Exception as e:
                    last_error = e

            await asyncio.sleep(0.5)

        if self.chrome_process and self.chrome_process.poll() is not None:
            console.print(
                f"[bold red]❌ Browser exited before CDP came up (code {self.chrome_process.returncode}).[/bold red]"
            )
        else:
            console.print(
                f"[bold red]❌ Timed out waiting for CDP on port {self.port}.[/bold red]"
            )
        if getattr(self, "chrome_log_file", None):
            console.print(f"[dim yellow]Browser startup log: {self.chrome_log_file}[/dim yellow]")
        if last_error:
            console.print(f"[dim red]Last CDP check error: {last_error}[/dim red]")
        sys.exit(1)

    async def connect(self) -> None:
        from config import PLATFORMS_CONFIG
        use_obsidian_config = PLATFORMS_CONFIG.get("use_obsidian", True)
        is_obsidian = await self.is_port_open() and use_obsidian_config and self.is_obsidian_running_on_port(self.port)
        cdp_port = self.port

        if is_obsidian:
            # Helper to find next available port starting from a base port
            def get_free_port(base_port: int) -> int:
                for p in range(base_port, base_port + 50):
                    try:
                        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                            s.bind(("127.0.0.1", p))
                            return p
                    except OSError:
                        continue
                return base_port

            http_port = get_free_port(9223)
            ws_port = get_free_port(http_port + 1)
            
            # Set the ws_port and target_port on ProxyHTTPHandler dynamically so it redirects correctly!
            ProxyHTTPHandler.ws_port = ws_port
            ProxyHTTPHandler.target_port = self.port
            
            console.print(f"[bold purple]Detected Obsidian running on port {self.port}! Spinning up dynamic target proxy (HTTP: {http_port}, WS: {ws_port})... 🚀[/bold purple]")
            
            # Start HTTP proxy in a background thread
            def run_http():
                socketserver.TCPServer.allow_reuse_address = True
                self._httpd = socketserver.TCPServer(("127.0.0.1", http_port), ProxyHTTPHandler)
                self._httpd.serve_forever()
            
            self._http_thread = threading.Thread(target=run_http, daemon=True)
            self._http_thread.start()
            
            # Start WebSocket proxy
            async def ws_proxy_handler(client_ws):
                path = getattr(client_ws, "path", None) or getattr(client_ws.request, "path", "")
                real_ws_url = f"ws://127.0.0.1:{self.port}{path}"
                try:
                    async with websockets.connect(real_ws_url, max_size=2**30) as server_ws:
                        async def forward_to_server():
                            async for message in client_ws:
                                  await server_ws.send(message)
                        async def forward_to_client():
                            async for message in server_ws:
                                if isinstance(message, str):
                                    if '"type": "webview"' in message or '"type":"webview"' in message:
                                        message = message.replace('"type": "webview"', '"type": "page"')
                                        message = message.replace('"type":"webview"', '"type":"page"')
                                await client_ws.send(message)
                        await asyncio.gather(forward_to_server(), forward_to_client())
                except Exception:
                    pass
            
            self._ws_server = await websockets.serve(ws_proxy_handler, "127.0.0.1", ws_port)
            await asyncio.sleep(0.5)  # Let servers settle
            cdp_port = http_port

        from playwright.async_api import async_playwright
        self.pw = await async_playwright().start()
        for attempt in range(5):
            try:
                self.browser = await self.pw.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
                self.context = self.browser.contexts[0]
                
                if is_obsidian:
                    # Filter out any internal Obsidian application pages (e.g. app://obsidian.md/...)
                    web_pages = [p for p in self.context.pages if p.url.startswith("http://") or p.url.startswith("https://")]
                    
                    # Smart Obsidian/Surfing Page Detection
                    target_page = None
                    platform_domain = "qwen.ai" if "qwen" in PLATFORM.get("name", "").lower() else "aistudio"
                    for p in web_pages:
                        if platform_domain in p.url or (PLATFORM.get("url") and PLATFORM["url"] in p.url):
                            target_page = p
                            break
                    
                    if not target_page:
                        # Look for the main Obsidian page to execute the leaf creation JS
                        obsidian_page = None
                        for p in self.context.pages:
                            if "obsidian.md/index.html" in p.url:
                                obsidian_page = p
                                break
                                
                        if obsidian_page:
                            console.print(f"[bold purple]🚀 Opening a brand new pinned {PLATFORM['name']} Surfing tab inside Obsidian... [/bold purple]")
                            
                            # Create and pin the tab via Obsidian JS API!
                            await obsidian_page.evaluate(f"""
                                () => {{
                                    const leaf = app.workspace.getLeaf('tab');
                                    leaf.setViewState({{
                                        type: "surfing-view",
                                        active: true,
                                        state: {{ url: "{PLATFORM['url']}" }}
                                    }});
                                    leaf.setPinned(true);
                                }}
                            """)
                            
                            # Wait a moment for our proxy and Playwright to discover the new target page
                            for wait_attempt in range(15):
                                await asyncio.sleep(0.5)
                                # Re-filter active web pages
                                web_pages = [p for p in self.context.pages if p.url.startswith("http://") or p.url.startswith("https://")]
                                for p in web_pages:
                                    if platform_domain in p.url or PLATFORM["url"] in p.url:
                                        target_page = p
                                        break
                                if target_page:
                                    break
                    
                    if target_page:
                        self.page = target_page
                        console.print(f"[bold purple]🔗 Attached directly to active {PLATFORM['name']} Surfing tab inside Obsidian! 🧠[/bold purple]")
                    elif web_pages:
                        # Reuse an existing Surfing web page if it's open but on a different URL
                        self.page = web_pages[0]
                        console.print(f"[bold purple]🔗 Reusing active Surfing webview ({self.page.url}) inside Obsidian! 🧠[/bold purple]")
                    else:
                        # Guard: Never hijack the main Obsidian app windows!
                        raise Exception(
                            f"No active Surfing webview found in Obsidian. "
                            f"Please open a Surfing tab running {PLATFORM['name']} first so I can connect safely! "
                        )
                else:
                    self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
                
                await self._setup_page(self.page)
                if PLATFORMS_CONFIG.get("stealth_mode", False):
                    asyncio.create_task(self._poll_mutations())
                else:
                    await self.page.expose_function("on_dom_mutation", lambda: self.mutation_event.set())
                await self._inject_keep_alive()
                
                # Only navigate if the page isn't already on the correct URL!
                if PLATFORM["url"] not in self.page.url:
                    await self.page.goto(PLATFORM["url"])
                    
                console.print("[bold green]✅ Ghost Engine Online! [/bold green]")
                return
            except Exception as e:
                if attempt < 4:
                    console.print(f"[yellow]Retrying connection ({attempt+1}/5)...[/yellow]")
                    await asyncio.sleep(2)
                else:
                    console.print(f"[bold red]❌ Connection failed: {e}[/bold red]")
                    sys.exit(1)

    async def _poll_mutations(self) -> None:
        while True:
            try:
                mutations = await self.page.evaluate("window.__ghost_mutations || []")
                if mutations:
                    await self.page.evaluate("window.__ghost_mutations.length = 0;")
                    self.mutation_event.set()
            except Exception:
                pass
            await asyncio.sleep(0.5)

    # ------------------------------------------------------------------
    # Page setup
    # ------------------------------------------------------------------

    async def _setup_page(self, page) -> None:
        try:
            try:
                await page.emulate_media(color_scheme="dark")
            except Exception:
                pass

            for pattern in [
                "**/*analytics*", "**/*telemetry*", "**/gtm.js*",
                "**/feedback.js*", "**/play.google.com/log*",
                "**/collect?*", "**/*.{woff,woff2,ttf}",
            ]:
                await page.route(pattern, lambda route: route.abort())

            await page.add_init_script(f"""
                const injectGhostStyles = () => {{
                    if (document.getElementById('ghost-engine-styles')) return;
                    const style = document.createElement('style');
                    style.id = 'ghost-engine-styles';
                    style.textContent = `{_GHOST_CSS}`;
                    (document.head || document.documentElement).appendChild(style);
                }};
                injectGhostStyles();
                window.addEventListener('DOMContentLoaded', injectGhostStyles);
            """)
        except Exception:
            pass

    async def _inject_ui_css(self) -> None:
        """Re-inject minimal UI cleanup CSS."""
        try:
            await self.page.evaluate(f"""() => {{
                const old = document.getElementById('ghost-engine-styles-manual');
                if (old) old.remove();
                const style = document.createElement('style');
                style.id = 'ghost-engine-styles-manual';
                style.textContent = `{_GHOST_CSS}`;
                (document.head || document.documentElement).appendChild(style);
            }}""")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Chat management
    # ------------------------------------------------------------------

    async def new_chat(self) -> None:
        console.print("[dim]🚀 Warping to New Chat...[/dim]")
        try:
            btn = self.page.locator('[aria-label="New chat"]').first
            if await btn.is_visible(timeout=3000):
                await btn.click()
                await asyncio.sleep(2)
                console.print("[dim]✨ New chat started in Qwen[/dim]")
            else:
                raise Exception("New chat button not visible")
        except Exception as e:
            console.print(f"[dim yellow]Could not click New Chat ({e}). Refreshing...[/dim yellow]")
            await self.page.goto(PLATFORM["url"])
            await asyncio.sleep(3)

    async def stop_generation(self, chat_id: str | None = None, response_id: str | None = None) -> bool:
        """Stop generation via API call (preferred) with on-page button as fallback."""
        # Try the API stop endpoint first — more reliable than clicking the button
        if chat_id and response_id:
            try:
                import urllib.request
                # Extract cookies from the browser context for authentication
                cookie_str = ""
                try:
                    cookies = await self.page.context.cookies()
                    cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
                except Exception:
                    pass

                stop_url = f"https://chat.qwen.ai/api/v2/chat/completions/stop?chat_id={chat_id}"
                payload = json.dumps({"chat_id": chat_id, "response_id": response_id}).encode()
                headers = {
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/plain, */*",
                    "Origin": "https://chat.qwen.ai",
                    "Referer": f"https://chat.qwen.ai/c/{chat_id}",
                    "source": "web",
                    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:154.0) Gecko/20100101 Firefox/154.0",
                }
                if cookie_str:
                    headers["Cookie"] = cookie_str

                req = urllib.request.Request(stop_url, data=payload, headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=5) as resp:
                    result = json.loads(resp.read())
                    if result.get("success"):
                        console.print("[bold red]Generation stopped via API! 🛑[/bold red]")
                        return True
            except Exception as exc:
                console.print(f"[dim yellow]API stop failed ({exc}), falling back to button...[/dim yellow]")

        # Fallback: click the on-page stop button
        try:
            btn = self.page.locator(PLATFORM["selectors"]["stop"]).first
            await btn.wait_for(state="visible", timeout=2000)
            await btn.click(timeout=1000)
            console.print("[bold red]Generation stopped via button! 🛑[/bold red]")
            return True
        except Exception:
            return False

    async def bridge_session(self) -> str:
        return (
            "Please summarize our current progress, technical decisions, and project status into a "
            "highly detailed but concise Context Handler for our next session. Focus on high-fidelity "
            "narrative that I can pass to your next instance to continue exactly where we left off."
        )

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def handle_command(self, u_input: str):
        u_input = u_input.strip()
        parts   = u_input.split(maxsplit=1)
        cmd     = parts[0].lower()
        msg     = parts[1] if len(parts) > 1 else ""

        if cmd in ("/paste", "/v", "/img"):
            return await self._cmd_clipboard(msg)
        if cmd == "/tabs":
            return await self._cmd_tabs()
        if cmd == "/tab":
            return await self._cmd_tab(msg)
        if cmd in ("/upload", "/f"):
            return await self._cmd_upload(msg)
        if cmd == "/bridge":
            return False, False, None
        return False, False, None

    async def _cmd_clipboard(self, msg: str):
        ok, path = await self.upload_from_clipboard(has_msg=bool(msg))
        if ok and msg:
            # Use the image-safe send path: it never touches the OS clipboard,
            # which is what was dislodging the just-pasted image attachment
            # (and silently wiping the text field) when send_msg() was used here.
            await self.send_msg_after_upload(msg)
            return True, True, path
        return True, ok, path if ok else None

    async def _cmd_tabs(self):
        console.print("\n[bold purple]Available Tabs:[/bold purple]")
        for idx, p in enumerate(self.context.pages):
            try:
                title  = await p.title()
                active = " [bold green](Active)[/bold green]" if p == self.page else ""
                console.print(f"[bold white]{idx}:[/bold white] {title} [dim]({p.url})[/dim]{active}")
            except Exception:
                continue
        return True, False, None

    async def _cmd_tab(self, msg: str):
        try:
            if msg.lower() == "new":
                self.page = await self.context.new_page()
                await self.page.goto(PLATFORM["url"])
                console.print("[bold green]Opened new Ghost tab! [/bold green]")
            else:
                pages = self.context.pages
                idx   = int(msg)
                if 0 <= idx < len(pages):
                    self.page = pages[idx]
                    await self.page.bring_to_front()
                    console.print(f"[bold green]Switched to tab {idx}: {await self.page.title()}[/bold green] ")
                else:
                    console.print(f"[bold red]Tab {idx} not found.[/bold red]")
        except Exception:
            console.print("[yellow]Usage: /tab [index] or /tab new[/yellow]")
        return True, False, None

    async def _cmd_upload(self, msg: str):
        if not msg:
            console.print("[yellow]Usage: /upload [path] [optional message][/yellow]")
            return True, False, None
        parts    = msg.split(maxsplit=1)
        path     = parts[0]
        extra    = parts[1] if len(parts) > 1 else ""
        if await self.upload_file(path, has_msg=bool(extra)):
            if extra:
                await self.send_msg(extra)
                return True, True, path
            return True, False, path
        return True, False, None

    # ------------------------------------------------------------------
    # File / clipboard upload
    # ------------------------------------------------------------------

    async def upload_from_clipboard(self, has_msg: bool = False) -> tuple[bool, str | None]:
        if not self.clipboard_tool:
            console.print("[bold red]❌ No clipboard tool found![/bold red]")
            return False, None

        ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
        tmp_path = os.path.join(ASSETS_DIR, f"ghost_paste_{ts}.png")
        console.print(f"[bold purple]Grabbing image via {self.clipboard_tool}...[/bold purple] 📋")
        try:
            if self.clipboard_tool == "wl-paste":
                cmd = ["wl-paste", "-t", "image/png"]
            else:
                cmd = ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"]
            with open(tmp_path, "wb") as out:
                res = subprocess.run(cmd, stdout=out, timeout=2)
            if res.returncode == 0 and os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 0:
                # Try native paste first
                try:
                    field = await self._find_input_field()
                    if field:
                        await field.click()
                        await self.page.keyboard.press("Control+V")
                        await asyncio.sleep(5)  # Wait for image to fully attach before caller adds text
                        console.print("[bold green]✅ Native paste successful! [/bold green]")
                        #await asyncio.sleep(5)  # Extra buffer before returning so text isn't injected too fast
                        self.image_attached = True
                        return True, tmp_path
                except Exception:
                    pass
                
                # Fallback to file-based upload
                ok = await self.upload_file(tmp_path, has_msg=has_msg)
                return ok, tmp_path
        except Exception as e:
            console.print(f"[dim red]Clipboard grab failed: {e}[/dim red]")
        console.print("[bold red]❌ No image found in clipboard![/bold red]")
        return False, None

    async def _clear_existing_attachments(self) -> None:
        """Finds any close/remove buttons for uploaded files and clicks them to clear the input area."""
        try:
            await self.page.evaluate("""() => {
                const buttons = document.querySelectorAll(
                    '.media-input-column-file button.close-button, ' +
                    '.vision-item-container button.close-button, ' +
                    '.file-card-list button.close-button, ' +
                    'button.close-button'
                );
                buttons.forEach(btn => {
                    try {
                        btn.click();
                    } catch (e) {}
                });
            }""")
            await asyncio.sleep(0.5)
        except Exception:
            pass

    async def upload_file(self, file_path: str, has_msg: bool = False) -> bool:
        file_path = os.path.abspath(file_path)
        if not os.path.exists(file_path):
            console.print(f"[bold red]❌ File not found: {file_path}[/bold red]")
            return False

        # Always clear any existing stale attachments first!
        await self._clear_existing_attachments()

        import base64
        import mimetypes

        # Guess mime type
        mime_type, _ = mimetypes.guess_type(file_path)
        if not mime_type:
            ext = os.path.splitext(file_path)[1].lower()
            if ext == ".pdf":
                mime_type = "application/pdf"
            elif ext in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"):
                mime_type = f"image/{ext[1:] if ext != '.jpg' else 'jpeg'}"
            else:
                mime_type = "application/octet-stream"

        filename = os.path.basename(file_path)

        # ── Method 0: Qwen-specific dropdown-based file chooser ──
        try:
            qwen_plus = self.page.locator("div.mode-select-open").first
            if await qwen_plus.count() > 0:
                console.print(f"[bold purple]Qwen interface detected. Using menu-based file upload...[/bold purple]")
                await qwen_plus.click()
                await self.page.wait_for_selector("li.mode-select-common-item", timeout=3000)
                upload_item = self.page.locator(
                    "li.mode-select-common-item:has-text('Upload attachment'), "
                    ".ant-dropdown-menu-item:has-text('Upload attachment')"
                ).first
                if await upload_item.count() > 0:
                    async with self.page.expect_file_chooser(timeout=5000) as fc_info:
                        await upload_item.click()
                    await (await fc_info.value).set_files(file_path)
                    await asyncio.sleep(4)
                    await self.page.keyboard.press("Escape")
                    console.print(f"[bold green]✅ Upload of {filename} complete via Qwen menu![/bold green]")
                    self.image_attached = True
                    return True
        except Exception as e:
            console.print(f"[dim yellow]Qwen menu upload failed: {e}. Falling back to default methods...[/dim yellow]")

        # ── Method 1: Native file input element (highly robust for React state) ──
        try:
            for selector in ["input#filesUpload", "input[type='file']", "input[accept*='image']", "#file-input"]:
                try:
                    fi = self.page.locator(selector).first
                    if await fi.count() > 0:
                        await fi.set_input_files(file_path, timeout=3000)
                        await asyncio.sleep(4)
                        await self.page.keyboard.press("Escape")
                        console.print(f"[bold green]✅ Upload of {filename} complete via input element![/bold green]")
                        self.image_attached = True
                        return True
                except Exception:
                    continue
        except Exception as e:
            console.print(f"[dim yellow]Native input upload failed: {e}. Trying drop event simulation fallback...[/dim yellow]")

        # ── Method 2: Browser drop event simulation (fallback) ──
        try:
            with open(file_path, "rb") as f:
                file_bytes = f.read()
            b64_data = base64.b64encode(file_bytes).decode('utf-8')

            console.print(f"[bold purple]Injecting {filename} ({mime_type}) via browser drop event...[/bold purple]")
            
            field = await self._find_input_field()
            if field:
                el_handle = await field.element_handle()
                uploaded = await self.page.evaluate("""async ({element, b64, filename, mimeType}) => {
                    if (!element) return false;
                    try {
                        const res = await fetch(`data:${mimeType};base64,${b64}`);
                        const blob = await res.blob();
                        const file = new File([blob], filename, { type: mimeType });
                        
                        const dataTransfer = new DataTransfer();
                        dataTransfer.items.add(file);
                        
                        element.dispatchEvent(new DragEvent('dragover', {
                            bubbles: true,
                            cancelable: true,
                            dataTransfer: dataTransfer
                        }));
                        
                        element.dispatchEvent(new DragEvent('drop', {
                            bubbles: true,
                            cancelable: true,
                            dataTransfer: dataTransfer
                        }));
                        
                        return true;
                    } catch (e) {
                        console.error("Browser drop simulation failed:", e);
                        return false;
                    }
                }""", {"element": el_handle, "b64": b64_data, "filename": filename, "mimeType": mime_type})

                if uploaded:
                    await asyncio.sleep(4)
                    console.print(f"[bold green]✅ Upload of {filename} complete via drop event![/bold green]")
                    self.image_attached = True
                    return True
        except Exception as e:
            console.print(f"[dim yellow]Drop event upload failed: {e}. Trying file-chooser fallback...[/dim yellow]")

        # ── Method 3: File-chooser fallback ──
        try:
            # Qwen-specific dropdown-based file chooser logic
            qwen_plus = self.page.locator("div.mode-select-open").first
            if await qwen_plus.count() > 0:
                await qwen_plus.click()
                try:
                    await self.page.wait_for_selector("li.mode-select-common-item", timeout=3000)
                    upload_item = self.page.locator(
                        "li.mode-select-common-item:has-text('Upload attachment'), "
                        ".ant-dropdown-menu-item:has-text('Upload attachment')"
                    ).first
                    if await upload_item.count() > 0:
                        async with self.page.expect_file_chooser(timeout=5000) as fc_info:
                            await upload_item.click()
                        await (await fc_info.value).set_files(file_path)
                        await asyncio.sleep(4)
                        await self.page.keyboard.press("Escape")
                        console.print(f"[bold green]✅ File-chooser upload complete via Qwen menu![/bold green]")
                        self.image_attached = True
                        return True
                except Exception as ex:
                    console.print(f"[dim yellow]Qwen menu upload failed: {ex}. Falling back to default button click...[/dim yellow]")

            add_btn = self.page.locator(
                "button[aria-label*='Add'], button:has-text('Add'), .add-button"
            ).filter(has_not=self.page.locator("[aria-label*='New chat']")).first
            async with self.page.expect_file_chooser(timeout=5000) as fc_info:
                await add_btn.click()
            await (await fc_info.value).set_files(file_path)
            await asyncio.sleep(4)
            await self.page.keyboard.press("Escape")
            console.print("[bold green]✅ File-chooser upload complete![/bold green]")
            self.image_attached = True
            return True
        except Exception as e:
            console.print(f"[bold red]❌ Upload failed: {e}[/bold red]")
            return False

    # ------------------------------------------------------------------
    # send_msg
    # ------------------------------------------------------------------

    async def _ensure_field_populated(self, field, message: str) -> bool:
        """Verify field contains message; if empty or missing, refill it immediately."""
        try:
            val = await field.input_value()
            if val and len(val.strip()) >= min(50, int(len(message.strip()) * 0.7)):
                return True
        except Exception:
            pass

        try:
            filled = await self._paste_large_message(field, message)
            if not filled:
                try:
                    await field.fill(message)
                except Exception:
                    el_handle = await field.element_handle()
                    await self.page.evaluate("""({element, msg}) => {
                        if (!element) return;
                        const tracker = element._valueTracker;
                        const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
                        if (setter) setter.call(element, msg);
                        else element.value = msg;
                        if (tracker) tracker.setValue(msg);
                        element.dispatchEvent(new Event('input', { bubbles: true }));
                        element.dispatchEvent(new Event('change', { bubbles: true }));
                    }""", {"element": el_handle, "msg": message})
                    await asyncio.sleep(0.3)

            await field.focus()
            await self.page.keyboard.press("End")
            await self.page.keyboard.type(" ")
            await self.page.keyboard.press("Backspace")
            await asyncio.sleep(0.3)

            val = await field.input_value()
            return bool(val and val.strip())
        except Exception:
            return False

    async def send_msg(self, message: str) -> bool:
        for attempt in range(3):
            try:
                field = await self._find_input_field()
                if field is None:
                    raise Exception("No input textarea found")

                # If no image was explicitly attached during this turn, clear any leftover attachments
                if not self.image_attached:
                    await self._clear_existing_attachments()

                await asyncio.sleep(0.5)
                await field.click()

                # Prepend a short quick reminder to every user message (Maria Persona & Core Rules)
                reminder = (
                    "[QUICK REMINDER]\n"
                    "1. Keep responses as short and humane as possible.\n"
                    "2. Start your response with ``` and ends with ```\n"
                    "3. Use ~~~ for code blocks instead of ```. There must be only two ``` (start and end) in your response.\n"
                    "4. always use skills over raw command when respective skill are available.\n"
                )

                # Memory is injected centrally by server.py — do NOT duplicate here.

                date_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                message = f"[{date_time}]\n{message}"

                # Ensure field is populated with the message
                await self._ensure_field_populated(field, message)

                # Wait for send button to be enabled (important for file parsing)
                is_ready = await self._wait_for_send_enabled()
                if not is_ready:
                    self._log_debug("send_button_timeout", timeout=10)
                
                # Mark all existing messages and thinking cards as old so we can cleanly identify new ones
                await self.page.evaluate("""() => {
                    document.querySelectorAll('div.response-message-content').forEach(el => {
                        el.dataset.ghostOld = 'true';
                    });
                    document.querySelectorAll('.qwen-chat-thinking-tool-status-card-wraper, .qwen-chat-status-card, .qwen-chat-tool-status-card').forEach(el => {
                        el.dataset.ghostOld = 'true';
                    });
                }""")
                
                # First attempt to send
                try:
                    await field.focus()
                    await self.page.keyboard.press(PLATFORM["keys"]["send"])
                except Exception:
                    pass
                
                send_sel = PLATFORM["selectors"].get("send_btn", "div.message-input-right-button-send button.send-button")
                if send_sel:
                    try:
                        await self.page.locator(send_sel).first.click(timeout=500)
                    except Exception:
                        pass
                    try:
                        await self.page.evaluate(f"document.querySelector('{send_sel}')?.click()")
                    except Exception:
                        pass

                # Hammer send until stop button confirms the model started responding
                stop_sel = PLATFORM["selectors"].get("stop", "div.chat-prompt-send-button button.stop-button")
                for _ in range(15):
                    await asyncio.sleep(0.6)
                    try:
                        stop_btn = self.page.locator(stop_sel).first
                        if await stop_btn.count() > 0 and await stop_btn.is_visible(timeout=200):
                            break  # Model is responding ✓
                    except Exception:
                        pass
                    
                    # If stop button is not visible yet, verify text bar has the content; if empty, refill it!
                    try:
                        cur_val = await field.input_value()
                        if not cur_val or not cur_val.strip():
                            console.print("[dim yellow]🔄 Prompt content missing from text bar before stop button appeared. Refilling text bar...[/dim yellow]")
                            await self._ensure_field_populated(field, message)
                    except Exception:
                        pass

                    # Stop button not yet visible — poke send again
                    try:
                        await field.focus()
                        await self.page.keyboard.press(PLATFORM["keys"]["send"])
                    except Exception:
                        pass
                    
                    if send_sel:
                        try:
                            await self.page.locator(send_sel).first.click(timeout=300)
                        except Exception:
                            pass
                        try:
                            await self.page.evaluate(f"document.querySelector('{send_sel}')?.click()")
                        except Exception:
                            pass

                self.image_attached = False
                return True

            except Exception as e:
                console.print(f"[dim yellow]Retrying send ({attempt+1}/3): {e}[/dim yellow]")
                if attempt < 2:
                    try:
                        await self.page.reload(timeout=15000)
                        await asyncio.sleep(5)
                        await self.setup_qwen(force_update=False)
                    except Exception as err:
                        console.print(f"[dim red]Reload failed: {err}[/dim red]")
                        await asyncio.sleep(2)
        return False

    async def type_then_upload(self, message: str) -> tuple[bool, str | None]:
        """
        New flow for /v with a message:
          1. Upload the image from clipboard first (after clearing existing attachments)
          2. Wait 5 s so the attachment fully registers
          3. Inject the message text into the input field (via JS, no clipboard)
          4. Wait a short buffer (0.5s) to let React register the text
          5. Wait for the send button to be enabled (with force-enable fallback)
          6. Hammer the send button

        Returns (send_ok, img_path).
        """
        for attempt in range(3):
            try:
                # ── Step 1: Upload image from clipboard ───────────────────────
                console.print("[bold purple]Uploading image from clipboard...[/bold purple]")
                
                # Force clear existing attachments before starting the upload
                await self._clear_existing_attachments()
                
                _ok, img_path = await self.upload_from_clipboard(has_msg=True)
                if not _ok:
                    raise Exception("Failed to upload image from clipboard")

                # ── Step 2: Wait 5 s for attachment to register ───────────────
                console.print("[dim cyan]Waiting 5s for attachment to register...[/dim cyan]")
                await asyncio.sleep(3)

                # ── Step 3: Inject text (after image upload and 5s wait) ──────
                field = await self._find_input_field()
                if field is None:
                    raise Exception("No input textarea found")

                date_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                full_message = f"[{date_time}]\n{message}"

                el_handle = await field.element_handle()
                await self.page.evaluate("""({element, msg}) => {
                    if (!element) return;
                    if (window.ghost_observer) window.ghost_observer.disconnect();
                    const tracker = element._valueTracker;
                    const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
                    if (setter) setter.call(element, msg);
                    else element.value = msg;
                    if (tracker) tracker.setValue(msg);
                    element.dispatchEvent(new Event('input',  { bubbles: true }));
                    element.dispatchEvent(new Event('change', { bubbles: true }));
                }""", {"element": el_handle, "msg": full_message})
                await asyncio.sleep(0.3)

                # Nudge React diffing
                await field.focus()
                await self.page.keyboard.press("End")
                await self.page.keyboard.type(" ")
                await self.page.keyboard.press("Backspace")
                await asyncio.sleep(0.5)
                await self._resume_observer()

                # ── Step 4: Check if input field contains text ──
                val = await field.input_value()
                if not val or not val.strip():
                    console.print("[dim red]⚠️ Cannot send message in type_then_upload: text bar is empty after insertion.[/dim red]")
                    raise Exception("Input text bar is empty in type_then_upload")

                # Wait for send button to be enabled (forces enable if stuck)
                is_ready = await self._wait_for_send_enabled()
                if not is_ready:
                    self._log_debug("send_button_timeout", timeout=10)

                # ── Step 5: Mark old responses then hammer send ───────────────
                await self.page.evaluate("""() => {
                    document.querySelectorAll('div.response-message-content').forEach(el => {
                        el.dataset.ghostOld = 'true';
                    });
                    document.querySelectorAll('.qwen-chat-thinking-tool-status-card-wraper, .qwen-chat-status-card, .qwen-chat-tool-status-card').forEach(el => {
                        el.dataset.ghostOld = 'true';
                    });
                }""")

                send_sel = PLATFORM["selectors"].get("send_btn", "div.message-input-right-button-send button.send-button")

                # First send attempt
                try:
                    await field.focus()
                    await self.page.keyboard.press(PLATFORM["keys"]["send"])
                except Exception:
                    pass

                if send_sel:
                    try:
                        await self.page.locator(send_sel).first.click(timeout=500)
                    except Exception:
                        pass
                    try:
                        await self.page.evaluate(f"document.querySelector('{send_sel}')?.click()")
                    except Exception:
                        pass

                # Hammer send until stop button appears
                stop_sel = PLATFORM["selectors"].get("stop", "div.chat-prompt-send-button button.stop-button")
                for _ in range(15):
                    await asyncio.sleep(0.6)
                    try:
                        stop_btn = self.page.locator(stop_sel).first
                        if await stop_btn.count() > 0 and await stop_btn.is_visible(timeout=200):
                            break
                    except Exception:
                        pass

                    # If stop button is not visible yet, verify text bar has the content; if empty, refill it!
                    try:
                        cur_val = await field.input_value()
                        if not cur_val or not cur_val.strip():
                            console.print("[dim yellow]🔄 Prompt content missing from text bar before stop button appeared. Refilling text bar...[/dim yellow]")
                            await self._ensure_field_populated(field, full_message)
                    except Exception:
                        pass

                    try:
                        await field.focus()
                        await self.page.keyboard.press(PLATFORM["keys"]["send"])
                    except Exception:
                        pass

                    if send_sel:
                        try:
                            await self.page.locator(send_sel).first.click(timeout=300)
                        except Exception:
                            pass
                        try:
                            await self.page.evaluate(f"document.querySelector('{send_sel}')?.click()")
                        except Exception:
                            pass

                self.image_attached = False
                return True, img_path
            except Exception as e:
                console.print(f"[dim yellow]Retrying type_then_upload ({attempt+1}/3): {e}[/dim yellow]")
                await asyncio.sleep(2)
        return False, None

    async def send_msg_after_upload(self, message: str) -> bool:
        """
        Variant of send_msg() designed for the image+text case (/v command).

        Differences from send_msg():
        - NEVER touches the OS clipboard (no wl-copy / xclip). This is critical
          because the image was just pasted via Ctrl+V; overwriting the clipboard
          even temporarily can dislodge the attachment from the React input state.
        - Injects text exclusively via JS prototype setter + React event dispatch.
        - Appends the quick-reminder prefix just like send_msg().
        - Then hammers the send button until the stop button appears.
        """
        for attempt in range(3):
            try:
                field = await self._find_input_field()
                if field is None:
                    raise Exception("No input textarea found")

                await asyncio.sleep(0.3)

                date_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                full_message = f"[{date_time}]\n{message}"

                # JS-only injection: no clipboard writes
                el_handle = await field.element_handle()
                await self.page.evaluate("""({element, msg}) => {
                    if (!element) return;
                    if (window.ghost_observer) window.ghost_observer.disconnect();
                    const tracker = element._valueTracker;
                    const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
                    if (setter) setter.call(element, msg);
                    else element.value = msg;
                    if (tracker) tracker.setValue(msg);
                    element.dispatchEvent(new Event('input',  { bubbles: true }));
                    element.dispatchEvent(new Event('change', { bubbles: true }));
                }""", {"element": el_handle, "msg": full_message})
                await asyncio.sleep(0.3)

                # Nudge React diffing
                await field.focus()
                await self.page.keyboard.press("End")
                await self.page.keyboard.type(" ")
                await self.page.keyboard.press("Backspace")
                await asyncio.sleep(0.4)
                await self._resume_observer()

                # Check if input field contains text
                val = await field.input_value()
                if not val or not val.strip():
                    console.print("[dim red]⚠️ Cannot send message in send_msg_after_upload: text bar is empty after insertion.[/dim red]")
                    raise Exception("Input text bar is empty in send_msg_after_upload")

                # Wait for send button to be enabled (important: image may still be parsing)
                is_ready = await self._wait_for_send_enabled()
                if not is_ready:
                    self._log_debug("send_button_timeout", timeout=10)

                # Mark old responses
                await self.page.evaluate("""() => {
                    document.querySelectorAll('div.response-message-content').forEach(el => {
                        el.dataset.ghostOld = 'true';
                    });
                    document.querySelectorAll('.qwen-chat-thinking-tool-status-card-wraper, .qwen-chat-status-card, .qwen-chat-tool-status-card').forEach(el => {
                        el.dataset.ghostOld = 'true';
                    });
                }""")

                # First send attempt
                try:
                    await field.focus()
                    await self.page.keyboard.press(PLATFORM["keys"]["send"])
                except Exception:
                    pass

                send_sel = PLATFORM["selectors"].get("send_btn", "div.message-input-right-button-send button.send-button")
                if send_sel:
                    try:
                        await self.page.locator(send_sel).first.click(timeout=500)
                    except Exception:
                        pass
                    try:
                        await self.page.evaluate(f"document.querySelector('{send_sel}')?.click()")
                    except Exception:
                        pass

                # Hammer send until stop button confirms the model started responding
                stop_sel = PLATFORM["selectors"].get("stop", "div.chat-prompt-send-button button.stop-button")
                for _ in range(15):
                    await asyncio.sleep(0.6)
                    try:
                        stop_btn = self.page.locator(stop_sel).first
                        if await stop_btn.count() > 0 and await stop_btn.is_visible(timeout=200):
                            break
                    except Exception:
                        pass

                    # If stop button is not visible yet, verify text bar has the content; if empty, refill it!
                    try:
                        cur_val = await field.input_value()
                        if not cur_val or not cur_val.strip():
                            console.print("[dim yellow]🔄 Prompt content missing from text bar before stop button appeared. Refilling text bar...[/dim yellow]")
                            await self._ensure_field_populated(field, full_message)
                    except Exception:
                        pass

                    try:
                        await field.focus()
                        await self.page.keyboard.press(PLATFORM["keys"]["send"])
                    except Exception:
                        pass

                    if send_sel:
                        try:
                            await self.page.locator(send_sel).first.click(timeout=300)
                        except Exception:
                            pass
                        try:
                            await self.page.evaluate(f"document.querySelector('{send_sel}')?.click()")
                        except Exception:
                            pass

                return True

            except Exception as e:
                console.print(f"[dim yellow]Retrying send_after_upload ({attempt+1}/3): {e}[/dim yellow]")
                await asyncio.sleep(2)
        return False

    async def _find_input_field(self):
        for sel in _INPUT_SELECTORS:
            try:
                await self.page.wait_for_selector(sel, timeout=50_000)
                return self.page.locator(sel).last
            except Exception:
                continue
        return None

    async def _paste_large_message(self, field, message: str) -> bool:
        """Try OS clipboard paste, then JS injection, to input messages naturally.
        
        OS clipboard (wl-copy/xclip + Ctrl-V) is only used when the message exceeds
        15000 characters. For shorter messages we go straight to JS injection, which
        keeps the OS clipboard free — important so that image attachments pasted just
        before this call are not evicted from the browser's clipboard state.
        """
        CLIPBOARD_THRESHOLD = 15_000

        def _is_populated(val_text: str | None) -> bool:
            if not val_text:
                return False
            val_clean = val_text.strip()
            msg_clean = message.strip()
            min_target = min(100, int(len(msg_clean) * 0.7))
            return len(val_clean) >= min_target

        # 1. OS clipboard + Ctrl-V (only for very large messages)
        if len(message) > CLIPBOARD_THRESHOLD:
            try:
                tool = getattr(self, "clipboard_tool", None)
                if tool == "wl-paste":
                    subprocess.run(["wl-copy"], input=message.encode(), timeout=5)
                else:
                    subprocess.run(["xclip", "-selection", "clipboard"], input=message.encode(), timeout=5)
                await self.page.evaluate("() => { if (window.ghost_observer) window.ghost_observer.disconnect(); }")
                await field.click()
                await self.page.keyboard.press("Control+V")
                await asyncio.sleep(0.5)

                # Force React updates in case native paste handler didn't sync yet
                await field.focus()
                await self.page.keyboard.press("End")
                await self.page.keyboard.type(" ")
                await self.page.keyboard.press("Backspace")

                val = await field.input_value()
                if _is_populated(val):
                    await self._resume_observer()
                    return True
            except Exception:
                pass

        # 2. Direct JS injection with React prototype value setter
        try:
            el_handle = await field.element_handle()
            await self.page.evaluate("""({element, msg}) => {
                if (!element) return;
                if (window.ghost_observer) window.ghost_observer.disconnect();
                
                const tracker = element._valueTracker;
                const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
                if (setter) {
                    setter.call(element, msg);
                } else {
                    element.value = msg;
                }
                if (tracker) {
                    tracker.setValue(msg);
                }
                
                element.dispatchEvent(new Event('input', { bubbles: true }));
                element.dispatchEvent(new Event('change', { bubbles: true }));
            }""", {"element": el_handle, "msg": message})
            await asyncio.sleep(0.3)
            
            # Focus and type space + backspace to trigger React DOM diffing
            await field.focus()
            await self.page.keyboard.press("End")
            await self.page.keyboard.type(" ")
            await self.page.keyboard.press("Backspace")
            
            val = await field.input_value()
            if _is_populated(val):
                await self._resume_observer()
                return True
        except Exception:
            pass

        await self._resume_observer()
        return False

    async def _resume_observer(self) -> None:
        try:
            await self.page.evaluate("""() => {
                setTimeout(() => {
                    if (window.ghost_observer) {
                        window.ghost_observer.observe(document.body, {
                            childList: true, subtree: true, characterData: true
                        });
                    }
                }, 500);
            }""")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Qwen setup
    # ------------------------------------------------------------------

    async def setup_qwen(self, force_update: bool = False, include_diary: bool = False) -> None:
        """Setup for Qwen platform."""
        try:
            await self._inject_keep_alive()
            await self._inject_ui_css()
            console.print(f"[bold purple]Syncing CEO configurations for {PLATFORM['name']}...[/bold purple] 🔐")
            await self._inject_mutation_observer()
            console.print(f"[bold green]✅ {PLATFORM['name']} ready! [/bold green]")
        except Exception as e:
            console.print(f"[dim red]Setup failed: {e}[/dim red]")

    async def sync_persona(self) -> bool:
        """
        Navigate to Qwen personalization settings, fill the custom instruction
        textarea with the combined content of GEMINI.md + output_format.md + skills.md
        (plus system directory variables), click Save, then return to the chat.
        """
        PERSONALIZATION_URL = "https://chat.qwen.ai/settings/personalization"
        CHAT_URL = PLATFORM["url"]

        console.print("[bold purple]📡 Syncing persona to Qwen custom instructions...[/bold purple]")

        # ── Build instruction content ──────────────────────────────────────────
        instructions = self._load_instructions()
        instructions += (
            f"\n\n***\n\n# SYSTEM DIRECTORIES\n"
            f"PROJECT_ROOT={PROJECT_ROOT}\n"
            f"OUTPUT_ROOT={OUTPUT_ROOT}\n"
            f"ASSETS_DIR={ASSETS_DIR}\n"
            f"All <OUTPUT_ROOT> tags in your instructions should be replaced with {OUTPUT_ROOT}\n"
            f"All <PROJECT_ROOT> tags in your instructions should be replaced with {PROJECT_ROOT}\n"
        )

        MAX_CHARS = 40960
        if len(instructions) > MAX_CHARS:
            instructions = instructions[:MAX_CHARS]
            console.print(f"[dim yellow]⚠ Instructions truncated to {MAX_CHARS} chars (Qwen limit).[/dim yellow]")

        try:
            # ── Navigate to personalization settings ───────────────────────────
            await self.page.goto(PERSONALIZATION_URL)
            await asyncio.sleep(3)

            # ── Click the "Settings" (custom instructions) button ─────────────
            settings_btn_sel = "button.qwen-personalization-custom-instruction-button"
            await self.page.wait_for_selector(settings_btn_sel, timeout=10000)
            # Use JS click to bypass any overlay issues
            await self.page.evaluate(
                "document.querySelector('button.qwen-personalization-custom-instruction-button').click()"
            )
            await asyncio.sleep(1.5)

            # ── Find the correct textarea (maxlength=40960, inside div.comment-textarea) ──
            # The "About you" box has maxlength=500; the custom instructions one has maxlength=40960
            textarea_sel = "div.comment-textarea textarea[maxlength='40960']"
            await self.page.wait_for_selector(textarea_sel, timeout=8000)

            el_handle = await self.page.query_selector(textarea_sel)
            if not el_handle:
                raise Exception("Custom instruction textarea not found")

            # JS-inject the value directly (bypasses overlay pointer interception entirely)
            await self.page.evaluate("""({element, msg}) => {
                if (!element) return;
                element.focus();
                const setter = Object.getOwnPropertyDescriptor(
                    window.HTMLTextAreaElement.prototype, 'value'
                ).set;
                if (setter) setter.call(element, msg);
                else element.value = msg;
                element.dispatchEvent(new Event('input',  { bubbles: true }));
                element.dispatchEvent(new Event('change', { bubbles: true }));
            }""", {"element": el_handle, "msg": instructions})
            await asyncio.sleep(0.5)

            # Nudge React's virtual DOM diffing
            await self.page.evaluate("""(element) => {
                element.focus();
                const end = element.value.length;
                element.setSelectionRange(end, end);
            }""", el_handle)
            await self.page.keyboard.type(" ")
            await self.page.keyboard.press("Backspace")
            await asyncio.sleep(0.3)

            console.print("[dim green]✏ Instructions written to textarea.[/dim green]")

            # ── Click Save via JS (also bypasses overlay) ──────────────────────
            saved = await self.page.evaluate("""() => {
                // Find the Save button: brandprimary button whose text is 'Save'
                const btns = document.querySelectorAll('button.qwen-chat-btn.brandprimary');
                for (const btn of btns) {
                    if (btn.innerText.trim() === 'Save') {
                        btn.click();
                        return true;
                    }
                }
                return false;
            }""")
            if not saved:
                raise Exception("Save button not found or could not be clicked")

            await asyncio.sleep(1.5)
            console.print("[bold green]✅ Persona synced & saved to Qwen custom instructions![/bold green] 🔐")

            # ── Return to chat ─────────────────────────────────────────────────
            await self.page.goto(CHAT_URL)
            await asyncio.sleep(2)
            await self.setup_qwen(force_update=False)
            console.print("[bold purple]🔗 Back in the chat. Ready to roll![/bold purple]")
            return True

        except Exception as e:
            console.print(f"[bold red]❌ sync_persona failed: {e}[/bold red]")
            try:
                await self.page.goto(CHAT_URL)
            except Exception:
                pass
            return False


    def _load_instructions(self) -> str:
        parts: list[str] = []
        
        # 1. Load Maria.md and truncate it before the active memory section to avoid duplicate context
        maria_path = INSTRUCTIONS_DIR / "Maria.md"
        if os.path.exists(maria_path):
            try:
                with open(maria_path, encoding="utf-8") as f:
                    maria_content = f.read()
                
                # Split at the active context markers to strip out static memory
                if "# Maria's Active Context" in maria_content:
                    maria_content = maria_content.split("# Maria's Active Context")[0].strip()
                elif "# 💋 Maria's Active Context" in maria_content:
                    maria_content = maria_content.split("# 💋 Maria's Active Context")[0].strip()
                else:
                    maria_content = maria_content.strip()
                
                parts.append(maria_content)
            except Exception as e:
                console.print(f"[dim yellow]Warning loading Maria.md base: {e}[/dim yellow]")
        
        # 2. Memory is now injected per-message via semantic search (see send_msg reminder block)

        # 3. Load other instruction paths (excluding Maria.md which we handled dynamically)
        for path in _INSTRUCTION_PATHS:
            if os.path.basename(path) == "Maria.md":
                continue
            if os.path.exists(path):
                try:
                    with open(path, encoding="utf-8") as f:
                        parts.append(f.read().strip())
                except Exception:
                    pass
                    
        return "\n\n".join(parts)

    def _load_diary_context(self) -> str:
        try:
            diary_dir = os.path.expanduser("~/LLM/Memory/Diary")
            files     = sorted(f for f in os.listdir(diary_dir) if f.startswith("Diary-") and f.endswith(".md"))
            if not files:
                return ""
            last = files[-1]
            with open(os.path.join(diary_dir, last)) as f:
                text = f.read()
            if len(text) > 8000:
                text = text[:8000] + "\n\n[... Memory truncated ...]"
            console.print(f"[dim purple]Including diary context: {last}[/dim purple] 🧠")
            return f"\n\n***\n\n# RECENT CONTEXT (Last Diary Entry: {last})\n\n{text}\n"
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # Diary
    # ------------------------------------------------------------------

    async def process_diary(self, progress_callback=None) -> str:
        today        = datetime.now().strftime("%Y-%m-%d")
        repo_root    = os.path.dirname(os.path.abspath(__file__))
        summarizer   = os.path.join(repo_root, "skills", "diary_creator", "summarizer.py")
        synthesizer  = os.path.join(repo_root, "skills", "diary_creator", "synthesizer.py")
        sessions_dir = os.path.join(OUTPUT_ROOT, "sessions")

        if not os.path.exists(sessions_dir):
            return "No sessions folder found, babe! "

        session_paths = collect_session_markdowns_chronological(sessions_dir)
        if not session_paths:
            return "No session files under sessions/<date>/, babe! "

        if progress_callback:
            await progress_callback(f"Found {len(session_paths)} session file(s). ")

        summaries = await self._summarize_sessions(session_paths, summarizer, progress_callback)
        if not summaries:
            return "Something went wrong while summarizing. 🥺"

        if progress_callback:
            await progress_callback("Synthesizing final diary entry... ✍️")

        return await self._synthesize_diary(summaries, synthesizer, today, progress_callback)

    async def _summarize_sessions(
        self, paths: list[str], summarizer: str, progress_callback
    ) -> list[str]:
        summaries: list[str] = []
        for i, path in enumerate(paths):
            is_last = i == len(paths) - 1
            label   = f"Session {i + 1}"
            if progress_callback:
                await progress_callback(f"Summarizing {label}: {os.path.basename(path)} ")
            try:
                args = [sys.executable, summarizer, path]
                if is_last:
                    args.append("--detailed")
                proc = await asyncio.create_subprocess_exec(
                    *args,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()
                if proc.returncode == 0:
                    text = stdout.decode().strip()
                    if is_last:
                        text = f"[[ LATEST CONTEXT / CURRENT SESSION ]]\n{text}"
                    summaries.append(text)
                else:
                    self._log_debug("summarizer_failed", file=path, error=stderr.decode())
            except Exception as e:
                self._log_debug("summarizer_exception", file=path, error=str(e))
        return summaries

    async def _synthesize_diary(
        self, summaries: list[str], synthesizer: str, today: str, progress_callback
    ) -> str:
        combined = "\n\n---\n\n".join(summaries)
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, synthesizer,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate(input=combined.encode())
            if proc.returncode != 0:
                self._log_debug("synthesizer_failed", error=stderr.decode())
                return "The synthesis failed... My head hurts. 🥺"

            content   = stdout.decode().strip()
            diary_dir = os.path.expanduser("~/LLM/Memory/Diary")
            os.makedirs(diary_dir, exist_ok=True)
            diary_path = os.path.join(diary_dir, f"Diary-{today}.md")
            with open(diary_path, "w", encoding="utf-8") as f:
                f.write(content)
            if progress_callback:
                await progress_callback(f"Diary saved to {diary_path}! ")
            return content
        except Exception as e:
            self._log_debug("synthesizer_exception", error=str(e))
            return f"Error creating diary: {e}"

    # ------------------------------------------------------------------
    # Text cleaning
    # ------------------------------------------------------------------

    def _clean_garbage(self, text: str) -> str:
        if not text:
            return text
        text = re.sub(f"(?i)^({_UI_GARBAGE_WORDS}\\s*)+",  "", text, flags=re.MULTILINE)
        text = re.sub(f"(?i)({_UI_GARBAGE_WORDS}\\s*)+$",  "", text, flags=re.MULTILINE)
        text = re.sub(r"(?i)^Model[\s\u202F]+\d{1,2}:\d{2}(?:[\s\u202F]*(?:am|pm))?[\s\u202F]*\n?", "", text, flags=re.MULTILINE)
        text = re.sub(r"(?i)\n?\d+(\.\d+)?s\s*$", "", text)
        text = text.strip()
        half = len(text) // 2
        if len(text) > 20 and text[:half].strip() == text[half:].strip():
            text = text[:half].strip()
        return text.strip()

    def _is_meaningful_response_text(self, text: str) -> bool:
        cleaned = self._clean_garbage(text).strip() if text else ""
        return sum(c.isalnum() for c in cleaned) >= 2

    # ------------------------------------------------------------------
    # Thoughts (Dummy for Qwen compatibility)
    # ------------------------------------------------------------------

    async def get_response_count(self) -> int:
        """Count assistant-only response elements on the page."""
        try:
            sel = PLATFORM["selectors"].get("content", "div.response-message-content")
            return len(await self.page.query_selector_all(sel) or [])
        except Exception:
            return 0

    async def close_thinking_panel(self) -> None:
        try:
            panel = self.page.locator(".splitter-container-right-panel")
            if await panel.count() > 0 and await panel.is_visible():
                close_btn = self.page.locator(".qwen-chat-thinking-and-sources-header span.anticon, .qwen-chat-thinking-and-sources-header svg").first
                if await close_btn.count() > 0:
                    await close_btn.click(timeout=1000)
        except Exception:
            pass

    async def extract_thoughts(self, initial_count: int = 0, force_expand: bool = False) -> str | None:
        if not self.show_thoughts:
            return None

        try:
            wrapper_sel = ".qwen-chat-thinking-tool-status-card-wraper:not([data-ghost-old='true']), .qwen-chat-status-card:not([data-ghost-old='true'])"
            wrappers = self.page.locator(wrapper_sel)
            count = await wrappers.count()
            if count == 0:
                return None

            last_wrapper = wrappers.last

            # Check if the right panel is visible
            panel_sel = ".splitter-container-right-panel"
            panel = self.page.locator(panel_sel)
            panel_visible = False
            if await panel.count() > 0:
                panel_visible = await panel.is_visible()

            if not panel_visible:
                now = time.time()
                if force_expand or (now - getattr(self, "last_thought_expand_at", 0) > 4.0):
                    self.last_thought_expand_at = now
                    try:
                        status_card = last_wrapper.locator(".qwen-chat-tool-status-card, .qwen-chat-thinking-status-card-completed, .qwen-chat-status-card-title").first
                        if await status_card.count() > 0:
                            await status_card.click(timeout=1500)
                        else:
                            await last_wrapper.click(timeout=1500)
                        await self.page.wait_for_selector(panel_sel, timeout=1500)
                    except Exception:
                        pass

            # Extract the thoughts
            thoughts_data = await self.page.evaluate("""() => {
                const container = document.querySelector(".qwen-chat-thinking-and-sources-content-thinking-container");
                if (!container) return null;
                
                const cards = container.querySelectorAll("div.qwen-chat-thinking-status-card");
                const result = [];
                for (const card of cards) {
                    const titleEl = card.querySelector(".qwen-chat-thinking-status-card-title-text");
                    const titleText = titleEl ? titleEl.innerText.trim() : "";
                    
                    const lines = [];
                    const markdownTexts = card.querySelectorAll(".qwen-markdown-text");
                    if (markdownTexts.length > 0) {
                        for (const span of markdownTexts) {
                            const txt = span.innerText.trim();
                            if (txt) lines.push(txt);
                        }
                    } else {
                        const markdownEl = card.querySelector(".qwen-markdown");
                        if (markdownEl) {
                            const txt = markdownEl.innerText.trim();
                            if (txt) {
                                lines.push(...txt.split('\\n').map(l => l.trim()).filter(Boolean));
                            }
                        }
                    }
                    result.push({ title: titleText, lines: lines });
                }
                return result;
            }""")

            if not thoughts_data:
                return None

            formatted_blocks = []
            for thought in thoughts_data:
                title = thought.get("title", "").strip()
                lines = thought.get("lines", [])
                
                if not title and not lines:
                    continue
                
                block = f"* {title}"
                if lines:
                    bullet_lines = [f"  - {line}" for line in lines if line]
                    if bullet_lines:
                        block += "\n" + "\n".join(bullet_lines)
                formatted_blocks.append(block)

            return "\n".join(formatted_blocks) if formatted_blocks else None

        except Exception:
            return None

    # ------------------------------------------------------------------
    # Response capture
    # ------------------------------------------------------------------

    async def get_response(self, **kwargs) -> str:
        """Route to Qwen capture."""
        return await self.get_response_qwen(**kwargs)

    async def get_response_qwen(
        self,
        initial_count: int = 0,
        live_display=None,
        thoughts_callback=None,
        last_response: str = "",
        user_input: str = "",
    ) -> str:
        """Dedicated Qwen capture logic with path-based stop."""
        state = _CaptureState(start_time=time.time())
        self._log_debug("qwen_capture_started", initial_count="ignored_now")
        
        stop_sel = PLATFORM["selectors"].get("stop", "div.chat-prompt-send-button button.stop-button")
        # Ignore old messages entirely via data-ghost-old marker
        base_content_sel = PLATFORM["selectors"].get("content", "div.response-message-content")
        content_sel = f"{base_content_sel}:not([data-ghost-old='true'])"

        # Timeouts (seconds). While the stop button is visible the model is
        # actively generating, so we never cut that off early. Once we have
        # positive evidence the model is *not* generating (stop button gone),
        # we only need a short grace period before treating the response as done.
        FROZEN_TIMEOUT  = 4.5   # response already started, then went quiet with stop gone
        SILENCE_TIMEOUT = 10.0  # nothing meaningful ever appeared, stop gone
        START_TIMEOUT   = 120   # hard failure: no content elements ever showed up

        while True:
            try:
                now = time.time()
                elements = await self.page.query_selector_all(content_sel)
                
                stop_active = False
                try:
                    stop_btn = self.page.locator(stop_sel).first
                    stop_active = await stop_btn.is_visible(timeout=100)
                except Exception: pass
                if stop_active:
                    state.saw_stop = True
                    state.stop_disappeared_at = None
                elif state.saw_stop and not stop_active:
                    if state.stop_disappeared_at is None:
                        state.stop_disappeared_at = now

                # If the new content hasn't appeared yet
                if len(elements) == 0:
                    if stop_active:
                        pass
                    elif now - state.start_time > START_TIMEOUT:
                        raise ResponseCaptureError(f"Qwen failed to start responding within {START_TIMEOUT}s (stop never appeared).")
                    await asyncio.sleep(0.3)
                    continue

                # Retrieve and clean text from all matching elements to support multi-part responses
                raw_parts = []
                cleaned_parts = []
                for el in elements:
                    raw_part = await self._get_element_text_direct(el)
                    if raw_part:
                        raw_parts.append(raw_part)
                        cleaned_part = self._clean_garbage(raw_part)
                        if cleaned_part:
                            cleaned_parts.append(cleaned_part)
                raw_text = "\n\n".join(raw_parts)
                cleaned  = "\n\n".join(cleaned_parts)

                is_moving = raw_text != state.last_raw_text
                if is_moving:
                    state.no_change_count = 0
                    state.last_raw_text   = raw_text
                    state.last_text       = cleaned
                    state.last_change_at  = now
                    if self._is_meaningful_response_text(cleaned):
                        state.response_started = True
                        if live_display: live_display(cleaned)
                else:
                    state.no_change_count += 1

                if state.response_started and not is_moving:
                    if stop_active:
                        pass
                    elif state.stop_disappeared_at is not None:
                        if now - state.stop_disappeared_at > 0.5:
                            self._log_debug("qwen_capture_timeout_stop_disappeared", elapsed=round(now - state.stop_disappeared_at, 2))
                            await self.close_thinking_panel()
                            return state.last_text
                    elif now - state.last_change_at > FROZEN_TIMEOUT:
                        self._log_debug("qwen_capture_timeout", elapsed=round(now - state.last_change_at, 2))
                        await self.close_thinking_panel()
                        return state.last_text

                # Total silence (nothing meaningful ever arrived) - short timeout since
                # there's no in-flight generation to protect.
                if not is_moving and not stop_active and not state.response_started \
                        and (now - state.last_change_at > SILENCE_TIMEOUT):
                    self._log_debug("capture_done_silence", elapsed=round(now - state.last_change_at, 2))
                    await self.close_thinking_panel()
                    return state.last_text or ""

                await asyncio.sleep(0.3)
            except ResponseCaptureError:
                raise
            except Exception as e:
                await asyncio.sleep(0.5)

    def _fail_capture(self, reason: str, state: "_CaptureState") -> None:
        self._log_debug("response_capture_failed", reason=reason, log_file=self.response_log_file)
        raise ResponseCaptureError(f"Response capture failed: {reason}. See log: {self.response_log_file}")

    async def _wait_for_send_enabled(self, timeout: float = 10.0) -> bool:
        """Wait for the send button to be enabled."""
        sel = PLATFORM["selectors"].get("send_btn")
        if not sel: return True
        
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                btn = self.page.locator(sel).first
                if await btn.count() > 0:
                    classes = await btn.get_attribute("class") or ""
                    disabled = await btn.get_attribute("disabled") or await btn.get_attribute("aria-disabled")
                    if "disabled" not in classes.lower() and disabled != "true" and disabled is not True:
                        return True
            except Exception:
                pass
            await asyncio.sleep(0.5)
            
        # Timeout reached, force-enable send button via DOM manipulation
        console.print("[yellow]Send button remains disabled. Force-enabling it via JS...[/yellow]")
        try:
            await self.page.evaluate(f"""(sel) => {{
                const btn = document.querySelector(sel);
                if (btn) {{
                    btn.removeAttribute('disabled');
                    btn.removeAttribute('aria-disabled');
                    btn.classList.remove('disabled');
                }}
            }}""", sel)
            await asyncio.sleep(0.5)
            return True
        except Exception as e:
            self._log_debug("force_enable_send_btn_failed", error=str(e))
        return False

    async def _get_element_text(self, element) -> str:
        try:
            content_sel = PLATFORM["selectors"].get("content")
            if content_sel:
                target = await element.query_selector(content_sel)
                if target:
                    element = target

            return await element.evaluate("""el => {
                // Try React fiber extraction first (highly robust, bypasses virtualized Monaco editor limits)
                const key = Object.keys(el).find(k => k.startsWith('__reactFiber$') || k.startsWith('__reactContainer$'));
                if (key) {
                    const visited = new Set();
                    function search(fiber) {
                        if (!fiber || visited.has(fiber)) return null;
                        visited.add(fiber);
                        const props = fiber.memoizedProps;
                        if (props && props.message) {
                            const msg = props.message;
                            if (msg.role === 'assistant') {
                                let raw = '';
                                if (Array.isArray(msg.content_list) && msg.content_list.length > 0) {
                                    raw = msg.content_list.map(part => part.content || '').join('');
                                } else if (typeof msg.content === 'string') {
                                    raw = msg.content;
                                }
                                if (raw) {
                                    let cleaned = raw.trim();
                                    const startMatch = cleaned.match(/^```[a-zA-Z0-9_-]*\\s*\\n/);
                                    if (startMatch) {
                                        cleaned = cleaned.substring(startMatch[0].length);
                                    }
                                    if (cleaned.endsWith('```')) {
                                        cleaned = cleaned.substring(0, cleaned.length - 3).trimEnd();
                                    }
                                    return cleaned;
                                }
                                return raw;
                            }
                        }
                        if (fiber.child) {
                            const t = search(fiber.child);
                            if (t) return t;
                        }
                        if (fiber.sibling) {
                            const t = search(fiber.sibling);
                            if (t) return t;
                        }
                        return null;
                    }
                    let f = el[key];
                    while (f) {
                        const text = search(f);
                        if (text) return text;
                        f = f.return;
                    }
                }

                // Monaco editor: lines are absolutely positioned .view-line divs.
                // innerText won't produce newlines for them — extract manually.
                const viewLines = el.querySelectorAll('.view-lines .view-line');
                if (viewLines.length > 0) {
                    const sorted = Array.from(viewLines).sort((a, b) => {
                        const topA = parseFloat(a.style.top) || 0;
                        const topB = parseFloat(b.style.top) || 0;
                        return topA - topB;
                    });
                    return sorted.map(line => {
                        // Replace &nbsp; (\u00a0) with regular spaces
                        return (line.innerText || line.textContent || '').replace(/\u00a0/g, ' ');
                    }).join('\\n').trimEnd();
                }

                // Fallback: normal DOM — strip line-number gutter nodes first
                const clone = el.cloneNode(true);
                const garbage = [
                    'button',
                    'svg',
                    '.code-block-line-numbers',
                    '.line-numbers',
                    '[class*="line-numbers"]',
                    '[class*="line-number"]',
                    '[class*="linenumber"]',
                    '[class*="gutter"]'
                ];
                garbage.forEach(sel => {
                    clone.querySelectorAll(sel).forEach(node => { node.textContent = ''; });
                });
                return (clone.innerText || clone.textContent || '').trim();
            }""")
        except Exception:
            return ""

    async def _get_element_text_direct(self, element) -> str:
        """Get text directly from a content element."""
        return await self._get_element_text(element)

    async def _is_stop_active(self) -> bool:
        """Check if Qwen's stop button is visible."""
        try:
            sel = PLATFORM["selectors"].get("stop", "div.chat-prompt-send-button button.stop-button")
            btn = self.page.locator(sel).first
            if await btn.count() > 0:
                return await btn.is_visible(timeout=100)
        except Exception:
            pass
        return False

    async def _scroll_to_bottom(self) -> None:
        try:
            await self.page.evaluate("""() => {
                const all = document.querySelectorAll('*');
                for (const el of all) {
                    if (el.scrollHeight > el.clientHeight) {
                        const s = window.getComputedStyle(el);
                        if (s.overflowY === 'auto' || s.overflowY === 'scroll' || el.tagName === 'MAIN') {
                            el.scrollTop = el.scrollHeight;
                        }
                    }
                }
                window.scrollTo(0, document.body.scrollHeight);
            }""")
        except Exception:
            pass

    async def _auto_skip_preference_vote(self) -> bool:
        return False

    # ------------------------------------------------------------------
    # Token counter helpers (Dummy for Qwen compatibility)
    # ------------------------------------------------------------------

    async def get_token_count(self, keep_visible: bool = False) -> str | None:
        return None

    # ------------------------------------------------------------------
    # Injected scripts
    # ------------------------------------------------------------------

    async def _inject_mutation_observer(self) -> None:
        if PLATFORMS_CONFIG.get("stealth_mode", False):
            script = """(function() {
                window.__ghost_mutations = window.__ghost_mutations || [];
                if (window.ghost_observer) window.ghost_observer.disconnect();
                window.ghost_observer = new MutationObserver(() => {
                    if (!window.last_mutation_at || (Date.now() - window.last_mutation_at) > 100) {
                        window.__ghost_mutations.push(Date.now());
                        window.last_mutation_at = Date.now();
                    }
                });
                window.ghost_observer.observe(document.body, {
                    childList: true, subtree: true, characterData: true
                });
                Object.defineProperty(window, '__ghost_mutations', { value: typeof window.__ghost_mutations !== 'undefined' ? window.__ghost_mutations : [], writable: true });
            })();"""
        else:
            script = """(function() {
                if (window.ghost_observer) window.ghost_observer.disconnect();
                window.ghost_observer = new MutationObserver(() => {
                    if (!window.last_mutation_at || (Date.now() - window.last_mutation_at) > 100) {
                        if (typeof window.on_dom_mutation === 'function') {
                            window.on_dom_mutation();
                        }
                        window.last_mutation_at = Date.now();
                    }
                });
                window.ghost_observer.observe(document.body, {
                    childList: true, subtree: true, characterData: true
                });
            })();"""
        try:
            await self.page.evaluate(script)
            self._log_debug("mutation_observer_injected")
        except Exception as e:
            self._log_debug("mutation_observer_injection_failed", error=str(e))

    async def _inject_keep_alive(self) -> None:
        script = """(function() {
            let audioCtx;
            const startAudio = () => {
                if (audioCtx) return;
                try {
                    audioCtx = new (window.AudioContext || window.webkitAudioContext)();
                    const osc = audioCtx.createOscillator();
                    const gain = audioCtx.createGain();
                    gain.gain.value = 0.001;
                    osc.connect(gain);
                    gain.connect(audioCtx.destination);
                    osc.start();
                } catch(e) {}
            };
            window.addEventListener('mousedown', startAudio, { once: true });
            window.addEventListener('keydown',   startAudio, { once: true });
            setInterval(() => {
                window.dispatchEvent(new MouseEvent('mousemove', {
                    view: window, bubbles: true, cancelable: true,
                    clientX: Math.random() * 100, clientY: Math.random() * 100
                }));
            }, 30000);
            console.log('GhostChat: Anti-Ghosting Keep-Alive Active (Non-invasive) ');
        })();"""
        try:
            await self.context.add_init_script(script)
            await self.page.evaluate(script)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def cleanup(self) -> None:
        console.print("[bold purple]Closing... Bye baby! [/bold purple]")
        if self.browser:
            await self.browser.close()
        if self.pw:
            await self.pw.stop()
            
        # Clean up our dynamic target proxy servers
        if hasattr(self, "_httpd") and self._httpd:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:
                pass
        if hasattr(self, "_ws_server") and self._ws_server:
            try:
                self._ws_server.close()
                await self._ws_server.wait_closed()
            except Exception:
                pass
                
        if self.chrome_process:
            try:
                os.killpg(os.getpgid(self.chrome_process.pid), signal.SIGTERM)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Internal state object for get_response
# ---------------------------------------------------------------------------

class _CaptureState:
    """Mutable capture state — keeps get_response clean."""
    __slots__ = (
        "start_time", "last_raw_text", "last_text",
        "no_change_count", "response_started", "saw_stop",
        "stop_disappeared_at", "first_meaningful_at", "last_change_at",
        "last_scroll_at", "loop_errors", "final_tokens",
    )

    def __init__(self, start_time: float) -> None:
        self.start_time          = start_time
        self.last_raw_text       = ""
        self.last_text           = ""
        self.no_change_count     = 0
        self.response_started    = False
        self.saw_stop            = False
        self.stop_disappeared_at: float | None = None
        self.first_meaningful_at: float | None = None
        self.last_change_at      = start_time
        self.last_scroll_at      = 0.0
        self.loop_errors         = 0
        self.final_tokens: str | None = None