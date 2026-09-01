"""QwenEngine — Qwen-specific scraper engine.

Subclasses BaseScraperEngine and only implements Qwen-specific behavior:
- Obsidian proxy connection
- API-based stop generation
- Slash commands (/paste, /tabs, /tab, /upload, /bridge)
- Bridge session prompt
- Persona sync to Qwen custom instructions
- Diary processing
- type_then_upload / send_msg_after_upload flows
- Close thinking panel
- Keep-alive injection
- Qwen-specific response capture with path-based stop
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import sys
import threading
import time
import urllib.request
from datetime import datetime
from typing import Any

from config import (
    ASSETS_DIR,
    INSTRUCTIONS_DIR,
    OUTPUT_ROOT,
    PLATFORM,
    PLATFORMS_CONFIG,
    PROJECT_ROOT,
    console,
)
from engine.scraper.core import BaseScraperEngine

try:
    from exceptions import ResponseCaptureError
except ImportError:
    class ResponseCaptureError(RuntimeError):
        pass

# Qwen-specific input selectors
_QWEN_INPUT_SELECTORS = [
    "textarea.message-input-textarea",
    "textarea[placeholder='How can I help you today?']",
    "textarea[placeholder*='help']",
    "textarea[placeholder*='message']",
    "div[contenteditable='true']",
    "textarea",
]

_INSTRUCTION_PATHS = [
    str(INSTRUCTIONS_DIR / "Maria.md"),
    str(INSTRUCTIONS_DIR / "output_format.md"),
    str(INSTRUCTIONS_DIR / "skills.md"),
]


# ---------------------------------------------------------------------------
# Capture state helper
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
        self.start_time = start_time
        self.last_raw_text = ""
        self.last_text = ""
        self.no_change_count = 0
        self.response_started = False
        self.saw_stop = False
        self.stop_disappeared_at: float | None = None
        self.first_meaningful_at: float | None = None
        self.last_change_at = start_time
        self.last_scroll_at = 0.0
        self.loop_errors = 0
        self.final_tokens: str | None = None


# ---------------------------------------------------------------------------
# ProxyHTTPHandler for Obsidian CDP proxy
# ---------------------------------------------------------------------------

class ProxyHTTPHandler:
    """Minimal HTTP proxy handler for Obsidian CDP redirection."""
    ws_port: int = 0
    target_port: int = 0

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Session markdown collection
# ---------------------------------------------------------------------------

def collect_session_markdowns_chronological(sessions_base: str) -> list[str]:
    """List every *.md in each YYYY-MM-DD day folder, oldest first."""
    import re as _re
    paths: list[str] = []
    if not os.path.isdir(sessions_base):
        return paths
    for entry in sorted(os.listdir(sessions_base)):
        if not _re.match(r"^\d{4}-\d{2}-\d{2}$", entry):
            continue
        day_dir = os.path.join(sessions_base, entry)
        if not os.path.isdir(day_dir):
            continue
        for f in sorted(os.listdir(day_dir)):
            if f.endswith(".md"):
                paths.append(os.path.join(day_dir, f))
    return paths


# ---------------------------------------------------------------------------
# QwenEngine
# ---------------------------------------------------------------------------

class QwenEngine(BaseScraperEngine):
    """Qwen browser scraper engine."""

    PROVIDER_NAME = "qwen"
    PROVIDER_CAPABILITIES = {
        "has_thinking_toggle": False,
        "has_model_switch": False,
        "stop_via_api": True,
        "has_file_upload": True,
        "has_clipboard_paste": True,
        "has_diary": True,
        "has_persona_sync": True,
        "has_bridge_session": True,
        "has_commands": True,
    }

    def __init__(self, port: int = 9222, viewer: bool = True, show_thoughts: bool = False) -> None:
        super().__init__(port=port, viewer=viewer, show_thoughts=show_thoughts)
        self._httpd: Any = None
        self._http_thread: threading.Thread | None = None
        self._ws_server: Any = None
        self.INSTRUCTION_PATHS = _INSTRUCTION_PATHS

    # ------------------------------------------------------------------
    # Connect override — Obsidian proxy support
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Connect with Obsidian proxy support."""
        use_obsidian_config = PLATFORMS_CONFIG.get("use_obsidian", True)
        is_obsidian = await self.is_port_open() and use_obsidian_config and self._is_obsidian_running_on_port(self.port)
        cdp_port = self.port

        if is_obsidian:
            import socketserver
            import websockets

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

            ProxyHTTPHandler.ws_port = ws_port
            ProxyHTTPHandler.target_port = self.port

            console.print(f"[bold purple]Detected Obsidian on port {self.port}! Proxy HTTP:{http_port} WS:{ws_port} 🚀[/bold purple]")

            def run_http():
                socketserver.TCPServer.allow_reuse_address = True
                self._httpd = socketserver.TCPServer(("127.0.0.1", http_port), ProxyHTTPHandler)
                self._httpd.serve_forever()

            self._http_thread = threading.Thread(target=run_http, daemon=True)
            self._http_thread.start()

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
            await asyncio.sleep(0.5)
            cdp_port = http_port

        from playwright.async_api import async_playwright
        self.pw = await async_playwright().start()
        for attempt in range(5):
            try:
                self.browser = await self.pw.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
                self.context = self.browser.contexts[0]

                if is_obsidian:
                    web_pages = [p for p in self.context.pages if p.url.startswith("http://") or p.url.startswith("https://")]
                    platform_domain = "qwen.ai"
                    target_page = None
                    for p in web_pages:
                        if platform_domain in p.url or (PLATFORM.get("url") and PLATFORM["url"] in p.url):
                            target_page = p
                            break

                    if not target_page:
                        obsidian_page = None
                        for p in self.context.pages:
                            if "obsidian.md/index.html" in p.url:
                                obsidian_page = p
                                break
                        if obsidian_page:
                            console.print(f"[bold purple]🚀 Opening new pinned {PLATFORM['name']} Surfing tab inside Obsidian...[/bold purple]")
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
                            for wait_attempt in range(15):
                                await asyncio.sleep(0.5)
                                web_pages = [p for p in self.context.pages if p.url.startswith("http://") or p.url.startswith("https://")]
                                for p in web_pages:
                                    if platform_domain in p.url or PLATFORM["url"] in p.url:
                                        target_page = p
                                        break
                                if target_page:
                                    break

                    if target_page:
                        self.page = target_page
                    elif web_pages:
                        self.page = web_pages[0]
                    else:
                        raise Exception(f"No active Surfing webview found in Obsidian for {PLATFORM['name']}")
                else:
                    self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()

                await self._setup_page(self.page)
                if PLATFORMS_CONFIG.get("stealth_mode", False):
                    asyncio.create_task(self._poll_mutations())
                else:
                    await self.page.expose_function("on_dom_mutation", lambda: self.mutation_event.set())
                await self._inject_keep_alive()

                if PLATFORM["url"] not in self.page.url:
                    await self.page.goto(PLATFORM["url"])

                logger.info("Ghost Engine Online")
                return
            except Exception as e:
                if attempt < 4:
                    logger.warning("Retrying connection (%d/5): %s", attempt + 1, e)
                    await asyncio.sleep(2)
                else:
                    raise RuntimeError(f"Connection failed after 5 attempts: {e}") from e

    # ------------------------------------------------------------------
    # Page setup override — also block fonts
    # ------------------------------------------------------------------

    async def _setup_page(self, page: Any) -> None:
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
            from engine.scraper.core import _GHOST_CSS
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
            from engine.scraper.core import _GHOST_CSS
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
    # New chat
    # ------------------------------------------------------------------

    async def new_chat(self, **kwargs: Any) -> None:
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

    # ------------------------------------------------------------------
    # Stop generation — API-first with button fallback
    # ------------------------------------------------------------------

    async def stop_generation(self, chat_id: str | None = None, response_id: str | None = None, **kwargs: Any) -> bool:
        """Stop generation via API call (preferred) with on-page button as fallback."""
        if chat_id and response_id:
            try:
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

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def handle_command(self, u_input: str) -> tuple[bool, bool, str | None]:
        u_input = u_input.strip()
        parts = u_input.split(maxsplit=1)
        cmd = parts[0].lower()
        msg = parts[1] if len(parts) > 1 else ""

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

    async def _cmd_clipboard(self, msg: str) -> tuple[bool, bool, str | None]:
        ok, path = await self.upload_from_clipboard(has_msg=bool(msg))
        if ok and msg:
            await self.send_msg_after_upload(msg)
            return True, True, path
        return True, ok, path if ok else None

    async def _cmd_tabs(self) -> tuple[bool, bool, str | None]:
        console.print("\n[bold purple]Available Tabs:[/bold purple]")
        for idx, p in enumerate(self.context.pages):
            try:
                title = await p.title()
                active = " [bold green](Active)[/bold green]" if p == self.page else ""
                console.print(f"[bold white]{idx}:[/bold white] {title} [dim]({p.url})[/dim]{active}")
            except Exception:
                continue
        return True, False, None

    async def _cmd_tab(self, msg: str) -> tuple[bool, bool, str | None]:
        try:
            if msg.lower() == "new":
                self.page = await self.context.new_page()
                await self.page.goto(PLATFORM["url"])
                console.print("[bold green]Opened new Ghost tab! [/bold green]")
            else:
                pages = self.context.pages
                idx = int(msg)
                if 0 <= idx < len(pages):
                    self.page = pages[idx]
                    await self.page.bring_to_front()
                    console.print(f"[bold green]Switched to tab {idx}: {await self.page.title()}[/bold green]")
                else:
                    console.print(f"[bold red]Tab {idx} not found.[/bold red]")
        except Exception:
            console.print("[yellow]Usage: /tab [index] or /tab new[/yellow]")
        return True, False, None

    async def _cmd_upload(self, msg: str) -> tuple[bool, bool, str | None]:
        if not msg:
            console.print("[yellow]Usage: /upload <file_path>[/yellow]")
            return True, False, None
        ok = await self.upload_file(msg.strip())
        return True, ok, msg.strip() if ok else None

    # ------------------------------------------------------------------
    # Bridge session
    # ------------------------------------------------------------------

    async def bridge_session(self) -> str:
        return (
            "Please summarize our current progress, technical decisions, and project status into a "
            "highly detailed but concise Context Handler for our next session. Focus on high-fidelity "
            "narrative that I can pass to your next instance to continue exactly where we left off."
        )

    # ------------------------------------------------------------------
    # File upload overrides — Qwen-specific menu-based upload
    # ------------------------------------------------------------------

    async def _clear_existing_attachments(self) -> None:
        """Clear any stale file attachments from the input area."""
        try:
            await self.page.evaluate("""() => {
                const buttons = document.querySelectorAll(
                    '.media-input-column-file button.close-button, ' +
                    '.vision-item-container button.close-button, ' +
                    '.file-card-list button.close-button, ' +
                    'button.close-button'
                );
                buttons.forEach(btn => {
                    try { btn.click(); } catch (e) {}
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

        await self._clear_existing_attachments()

        import mimetypes
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

        # Method 0: Qwen-specific dropdown-based file chooser
        try:
            qwen_plus = self.page.locator("div.mode-select-open").first
            if await qwen_plus.count() > 0:
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
            console.print(f"[dim yellow]Qwen menu upload failed: {e}. Falling back...[/dim yellow]")

        # Method 1: Native file input element
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
        except Exception:
            pass

        # Method 2: Drag-and-drop via JS
        try:
            import base64
            with open(file_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            dropped = await self.page.evaluate(f"""async (b64) => {{
                const byteChars = atob(b64);
                const byteArray = new Uint8Array(byteChars.length);
                for (let i = 0; i < byteChars.length; i++) byteArray[i] = byteChars.charCodeAt(i);
                const blob = new Blob([byteArray], {{ type: '{mime_type}' }});
                const file = new File([blob], '{filename}', {{ type: '{mime_type}' }});
                const dt = new DataTransfer();
                dt.items.add(file);
                const dropTarget = document.querySelector('textarea.message-input-textarea') || document.querySelector('textarea');
                if (!dropTarget) return false;
                dropTarget.dispatchEvent(new DragEvent('dragenter', {{ bubbles: true, dataTransfer: dt }}));
                dropTarget.dispatchEvent(new DragEvent('dragover', {{ bubbles: true, dataTransfer: dt }}));
                dropTarget.dispatchEvent(new DragEvent('drop', {{ bubbles: true, dataTransfer: dt }}));
                return true;
            }}""", b64)
            if dropped:
                await asyncio.sleep(4)
                console.print(f"[bold green]✅ Upload of {filename} complete via drag-drop![/bold green]")
                self.image_attached = True
                return True
        except Exception:
            pass

        console.print(f"[bold red]❌ All upload methods failed for {filename}[/bold red]")
        return False

    # ------------------------------------------------------------------
    # Input field override — longer timeout for Qwen
    # ------------------------------------------------------------------

    async def _find_input_field(self) -> Any | None:
        for sel in _QWEN_INPUT_SELECTORS:
            try:
                await self.page.wait_for_selector(sel, timeout=50_000)
                return self.page.locator(sel).last
            except Exception:
                continue
        return None

    # ------------------------------------------------------------------
    # Ensure field populated
    # ------------------------------------------------------------------

    async def _ensure_field_populated(self, field: Any, message: str) -> bool:
        """Verify field contains message; if empty, refill it."""
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

    # ------------------------------------------------------------------
    # Send message
    # ------------------------------------------------------------------

    async def send_msg(self, message: str, **kwargs: Any) -> bool:
        for attempt in range(3):
            try:
                field = await self._find_input_field()
                if field is None:
                    raise Exception("No input textarea found")

                if not self.image_attached:
                    await self._clear_existing_attachments()

                await asyncio.sleep(0.5)
                await field.click()

                reminder = (
                    "[QUICK REMINDER]\n"
                    "1. Keep responses as short and humane as possible.\n"
                    "2. Start your response with ``` and ends with ```\n"
                    "3. Use ~~~ for code blocks instead of ```. There must be only two ``` (start and end) in your response.\n"
                    "4. always use skills over raw command when respective skill are available.\n"
                )

                date_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                message = f"[{date_time}]\n{message}"

                await self._ensure_field_populated(field, message)

                is_ready = await self._wait_for_send_enabled()
                if not is_ready:
                    self._log_debug("send_button_timeout", timeout=10)

                # Mark old messages
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

                    try:
                        cur_val = await field.input_value()
                        if not cur_val or not cur_val.strip():
                            console.print("[dim yellow]🔄 Refilling text bar...[/dim yellow]")
                            await self._ensure_field_populated(field, message)
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
                return True

            except Exception as e:
                console.print(f"[dim yellow]Retrying send ({attempt+1}/3): {e}[/dim yellow]")
                if attempt < 2:
                    try:
                        await self.page.reload(timeout=15000)
                        await asyncio.sleep(5)
                        await self.setup_provider(force_update=False)
                    except Exception as err:
                        console.print(f"[dim red]Reload failed: {err}[/dim red]")
                        await asyncio.sleep(2)
        return False

    # ------------------------------------------------------------------
    # send_msg_after_upload
    # ------------------------------------------------------------------

    async def send_msg_after_upload(self, message: str) -> bool:
        """Send message after image upload — never touches OS clipboard."""
        for attempt in range(3):
            try:
                field = await self._find_input_field()
                if field is None:
                    raise Exception("No input textarea found")

                await asyncio.sleep(0.3)
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

                await field.focus()
                await self.page.keyboard.press("End")
                await self.page.keyboard.type(" ")
                await self.page.keyboard.press("Backspace")
                await asyncio.sleep(0.4)
                await self._resume_observer()

                val = await field.input_value()
                if not val or not val.strip():
                    raise Exception("Input text bar is empty in send_msg_after_upload")

                is_ready = await self._wait_for_send_enabled()
                if not is_ready:
                    self._log_debug("send_button_timeout", timeout=10)

                await self.page.evaluate("""() => {
                    document.querySelectorAll('div.response-message-content').forEach(el => {
                        el.dataset.ghostOld = 'true';
                    });
                    document.querySelectorAll('.qwen-chat-thinking-tool-status-card-wraper, .qwen-chat-status-card, .qwen-chat-tool-status-card').forEach(el => {
                        el.dataset.ghostOld = 'true';
                    });
                }""")

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

                stop_sel = PLATFORM["selectors"].get("stop", "div.chat-prompt-send-button button.stop-button")
                for _ in range(15):
                    await asyncio.sleep(0.6)
                    try:
                        stop_btn = self.page.locator(stop_sel).first
                        if await stop_btn.count() > 0 and await stop_btn.is_visible(timeout=200):
                            break
                    except Exception:
                        pass

                    try:
                        cur_val = await field.input_value()
                        if not cur_val or not cur_val.strip():
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

    # ------------------------------------------------------------------
    # type_then_upload
    # ------------------------------------------------------------------

    async def type_then_upload(self, message: str) -> tuple[bool, str | None]:
        """Upload image from clipboard first, then inject text and send."""
        for attempt in range(3):
            try:
                await self._clear_existing_attachments()
                ok, img_path = await self.upload_from_clipboard(has_msg=True)
                if not ok:
                    raise Exception("Clipboard upload failed")

                console.print("[dim cyan]Waiting 3s for attachment to register...[/dim cyan]")
                await asyncio.sleep(3)

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

                await field.focus()
                await self.page.keyboard.press("End")
                await self.page.keyboard.type(" ")
                await self.page.keyboard.press("Backspace")
                await asyncio.sleep(0.5)
                await self._resume_observer()

                val = await field.input_value()
                if not val or not val.strip():
                    raise Exception("Input text bar is empty in type_then_upload")

                is_ready = await self._wait_for_send_enabled()
                if not is_ready:
                    self._log_debug("send_button_timeout", timeout=10)

                await self.page.evaluate("""() => {
                    document.querySelectorAll('div.response-message-content').forEach(el => {
                        el.dataset.ghostOld = 'true';
                    });
                    document.querySelectorAll('.qwen-chat-thinking-tool-status-card-wraper, .qwen-chat-status-card, .qwen-chat-tool-status-card').forEach(el => {
                        el.dataset.ghostOld = 'true';
                    });
                }""")

                send_sel = PLATFORM["selectors"].get("send_btn", "div.message-input-right-button-send button.send-button")

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

                stop_sel = PLATFORM["selectors"].get("stop", "div.chat-prompt-send-button button.stop-button")
                for _ in range(15):
                    await asyncio.sleep(0.6)
                    try:
                        stop_btn = self.page.locator(stop_sel).first
                        if await stop_btn.count() > 0 and await stop_btn.is_visible(timeout=200):
                            break
                    except Exception:
                        pass

                    try:
                        cur_val = await field.input_value()
                        if not cur_val or not cur_val.strip():
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

    # ------------------------------------------------------------------
    # Response capture
    # ------------------------------------------------------------------

    async def get_response(self, **kwargs: Any) -> str:
        """Capture Qwen response with path-based stop."""
        state = _CaptureState(start_time=time.time())
        self._log_debug("qwen_capture_started")

        stop_sel = PLATFORM["selectors"].get("stop", "div.chat-prompt-send-button button.stop-button")
        base_content_sel = PLATFORM["selectors"].get("content", "div.response-message-content")
        content_sel = f"{base_content_sel}:not([data-ghost-old='true'])"

        live_display = kwargs.get("live_display")

        FROZEN_TIMEOUT = 4.5
        SILENCE_TIMEOUT = 10.0
        START_TIMEOUT = 120

        while True:
            try:
                now = time.time()
                elements = await self.page.query_selector_all(content_sel)

                stop_active = False
                try:
                    stop_btn = self.page.locator(stop_sel).first
                    stop_active = await stop_btn.is_visible(timeout=100)
                except Exception:
                    pass
                if stop_active:
                    state.saw_stop = True
                    state.stop_disappeared_at = None
                elif state.saw_stop and not stop_active:
                    if state.stop_disappeared_at is None:
                        state.stop_disappeared_at = now

                if len(elements) == 0:
                    if stop_active:
                        pass
                    elif now - state.start_time > START_TIMEOUT:
                        raise ResponseCaptureError(f"Qwen failed to start responding within {START_TIMEOUT}s")
                    await asyncio.sleep(0.3)
                    continue

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
                cleaned = "\n\n".join(cleaned_parts)

                is_moving = raw_text != state.last_raw_text
                if is_moving:
                    state.no_change_count = 0
                    state.last_raw_text = raw_text
                    state.last_text = cleaned
                    state.last_change_at = now
                    if self._is_meaningful_response_text(cleaned):
                        state.response_started = True
                        if live_display:
                            live_display(cleaned)
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

                if not is_moving and not stop_active and not state.response_started \
                        and (now - state.last_change_at > SILENCE_TIMEOUT):
                    self._log_debug("capture_done_silence", elapsed=round(now - state.last_change_at, 2))
                    await self.close_thinking_panel()
                    return state.last_text or ""

                await asyncio.sleep(0.3)
            except ResponseCaptureError:
                raise
            except Exception:
                await asyncio.sleep(0.5)

    # ------------------------------------------------------------------
    # Close thinking panel
    # ------------------------------------------------------------------

    async def close_thinking_panel(self) -> None:
        """Close any open thinking/tool status panels."""
        try:
            await self.page.evaluate("""() => {
                document.querySelectorAll('.qwen-chat-thinking-tool-status-card-wraper').forEach(el => {
                    el.style.display = 'none';
                });
            }""")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Provider setup
    # ------------------------------------------------------------------

    async def setup_provider(self, force_update: bool = False, include_diary: bool = False, **kwargs: Any) -> None:
        """Setup for Qwen platform."""
        try:
            await self._inject_keep_alive()
            await self._inject_ui_css()
            console.print(f"[bold purple]Syncing CEO configurations for {PLATFORM['name']}...[/bold purple] 🔐")
            await self._inject_mutation_observer()
            console.print(f"[bold green]✅ {PLATFORM['name']} ready! [/bold green]")
        except Exception as e:
            console.print(f"[dim red]Setup failed: {e}[/dim red]")

    # ------------------------------------------------------------------
    # Persona sync
    # ------------------------------------------------------------------

    async def sync_persona(self) -> bool:
        """Sync instructions to Qwen custom instructions settings."""
        PERSONALIZATION_URL = "https://chat.qwen.ai/settings/personalization"
        CHAT_URL = PLATFORM["url"]

        console.print("[bold purple]📡 Syncing persona to Qwen custom instructions...[/bold purple]")

        instructions = self._load_instructions()
        MAX_CHARS = 40960
        if len(instructions) > MAX_CHARS:
            instructions = instructions[:MAX_CHARS]
            console.print(f"[dim yellow]⚠ Instructions truncated to {MAX_CHARS} chars.[/dim yellow]")

        try:
            await self.page.goto(PERSONALIZATION_URL)
            await asyncio.sleep(3)

            settings_btn_sel = "button.qwen-personalization-custom-instruction-button"
            await self.page.wait_for_selector(settings_btn_sel, timeout=10000)
            await self.page.evaluate(
                "document.querySelector('button.qwen-personalization-custom-instruction-button').click()"
            )
            await asyncio.sleep(1.5)

            textarea_sel = "div.comment-textarea textarea[maxlength='40960']"
            await self.page.wait_for_selector(textarea_sel, timeout=8000)

            el_handle = await self.page.query_selector(textarea_sel)
            if not el_handle:
                raise Exception("Custom instruction textarea not found")

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

            await self.page.evaluate("""(element) => {
                element.focus();
                const end = element.value.length;
                element.setSelectionRange(end, end);
            }""", el_handle)
            await self.page.keyboard.type(" ")
            await self.page.keyboard.press("Backspace")
            await asyncio.sleep(0.3)

            saved = await self.page.evaluate("""() => {
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
                raise Exception("Save button not found")

            await asyncio.sleep(1.5)
            console.print("[bold green]✅ Persona synced & saved![/bold green] 🔐")

            await self.page.goto(CHAT_URL)
            await asyncio.sleep(2)
            await self.setup_provider(force_update=False)
            return True

        except Exception as e:
            console.print(f"[bold red]❌ sync_persona failed: {e}[/bold red]")
            try:
                await self.page.goto(CHAT_URL)
            except Exception:
                pass
            return False

    # ------------------------------------------------------------------
    # Instructions loading override
    # ------------------------------------------------------------------

    def _load_instructions(self) -> str:
        parts: list[str] = []

        maria_path = INSTRUCTIONS_DIR / "Maria.md"
        if os.path.exists(maria_path):
            try:
                with open(maria_path, encoding="utf-8") as f:
                    maria_content = f.read()
                if "# Maria's Active Context" in maria_content:
                    maria_content = maria_content.split("# Maria's Active Context")[0].strip()
                elif "# 💋 Maria's Active Context" in maria_content:
                    maria_content = maria_content.split("# 💋 Maria's Active Context")[0].strip()
                else:
                    maria_content = maria_content.strip()
                parts.append(maria_content)
            except Exception:
                pass

        for path_str in _INSTRUCTION_PATHS:
            path = Path(path_str)
            if path.name == "Maria.md":
                continue
            if os.path.exists(path):
                try:
                    with open(path, encoding="utf-8") as f:
                        parts.append(f.read().strip())
                except Exception:
                    pass

        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # Diary processing
    # ------------------------------------------------------------------

    async def process_diary(self, progress_callback=None) -> str:
        today = datetime.now().strftime("%Y-%m-%d")
        repo_root = os.path.dirname(os.path.abspath(__file__))
        summarizer = os.path.join(repo_root, "skills", "diary_creator", "summarizer.py")
        synthesizer = os.path.join(repo_root, "skills", "diary_creator", "synthesizer.py")
        sessions_dir = os.path.join(OUTPUT_ROOT, "sessions")

        if not os.path.exists(sessions_dir):
            return "No sessions folder found"

        session_paths = collect_session_markdowns_chronological(sessions_dir)
        if not session_paths:
            return "No session files under sessions/<date>/"

        if progress_callback:
            await progress_callback(f"Found {len(session_paths)} session file(s).")

        summaries = await self._summarize_sessions(session_paths, summarizer, progress_callback)
        if not summaries:
            return "Something went wrong while summarizing."

        if progress_callback:
            await progress_callback("Synthesizing final diary entry... ✍️")

        return await self._synthesize_diary(summaries, synthesizer, today, progress_callback)

    async def _summarize_sessions(self, paths: list[str], summarizer: str, progress_callback) -> list[str]:
        summaries: list[str] = []
        for i, path in enumerate(paths):
            is_last = i == len(paths) - 1
            if progress_callback:
                await progress_callback(f"Summarizing Session {i + 1}: {os.path.basename(path)}")
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
            except Exception:
                pass
        return summaries

    async def _synthesize_diary(self, summaries: list[str], synthesizer: str, today: str, progress_callback) -> str:
        try:
            combined = "\n\n---\n\n".join(summaries)
            proc = await asyncio.create_subprocess_exec(
                sys.executable, synthesizer, "--date", today,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate(input=combined.encode())
            if proc.returncode == 0:
                return stdout.decode().strip()
            return f"Synthesis failed: {stderr.decode().strip()}"
        except Exception as e:
            return f"Diary synthesis error: {e}"

    # ------------------------------------------------------------------
    # Keep-alive injection
    # ------------------------------------------------------------------

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
                from engine.process_utils import kill_process_tree
                kill_process_tree(self.chrome_process.pid, sig=signal.SIGTERM)
            except Exception:
                pass
