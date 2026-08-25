import sys
import time
import os
import asyncio
import subprocess
import shutil
import signal
import json
import re
import socket
import urllib.request
from datetime import datetime

from config import console, PLATFORM, PLATFORMS_CONFIG, OUTPUT_ROOT, ASSETS_DIR, PROJECT_ROOT, INSTRUCTIONS_DIR
from exceptions import ResponseCaptureError

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
from engine.platform_paths import find_playwright_chrome as _find_pw_chrome
_PLAYWRIGHT_CHROME_GLOB = None  # resolved lazily via find_playwright_chrome()

_INPUT_SELECTORS = [
    PLATFORM["selectors"]["input"],
    "textarea[formcontrolname='promptText']",
    "textarea[aria-label='Enter a prompt']",
    "textarea.textarea",
]

_STOP_ACTIVE_SELECTORS = [
    PLATFORM["selectors"].get("stop", "div.ds-icon-button:has(path[d^='M2 4.88'])"),
]

_RESPONSE_SEL     = PLATFORM["selectors"].get("content", "div.ds-assistant-message-main-content")

_INSTRUCTION_PATHS = [
    str(INSTRUCTIONS_DIR / "Maria.md"),
    str(INSTRUCTIONS_DIR / "output_format.md"),
    str(INSTRUCTIONS_DIR / "skills.md"),
]

_MEMORY_PATH = str(PROJECT_ROOT / "Brain" / "Memory.json")

def _get_relevant_memories(message: str) -> str:
    """Retrieve relevant memories for a user message via semantic search."""
    try:
        from engine.memory_search import get_searcher
        import json as _json
        settings_path = PROJECT_ROOT / "system/memory_search_settings.json"
        enabled = True
        top_k = 10
        if settings_path.exists():
            cfg = _json.loads(settings_path.read_text(encoding="utf-8"))
            enabled = cfg.get("enabled", True)
            top_k = cfg.get("top_k", 10)
        if not enabled:
            return ""
        searcher = get_searcher()
        results = searcher.search(message, top_k=top_k)
        return searcher.format_for_prompt(results)
    except Exception:
        return ""

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
    /* Ghost UI cleanup */
    .feedback-container, .help-button, .upgrade-card,
    .command-palette-button, button[aria-label="What's new"],
    .category-card, .logo-wrapper,
    button[aria-label="View related products"] { display: none !important; }
"""


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


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
    def __init__(self, port: int = 9222, viewer: bool = True, show_thoughts: bool = False) -> None:

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
        self.system_injected   = False
        self.clipboard_tool    = _detect_clipboard_tool()
        self._last_thinking    = ""
        self._last_response_text = ""
        self.current_model_type: str = "default"
        self.has_fresh_chat: bool = False

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

        from engine.platform_paths import system_chrome_candidates, find_playwright_chrome
        chrome_path = next((shutil.which(b) for b in _CHROME_BINARIES if shutil.which(b)), None)
        if not chrome_path:
            # Try platform-aware system Chrome candidates
            for cand in system_chrome_candidates():
                if os.path.isfile(cand):
                    chrome_path = cand
                    break
        if not chrome_path:
            # Fallback to Playwright-bundled Chromium (newest version)
            chrome_path = find_playwright_chrome()
        if not chrome_path:
            console.print("[bold red]❌ No Chrome found![/bold red]")
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
            "--ozone-platform=wayland",
            "--no-sandbox",
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
        from engine.process_utils import popen_kwargs
        self.chrome_process = subprocess.Popen(
            cmd, stdout=log_file, stderr=subprocess.STDOUT, **popen_kwargs()
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

        # --- DUMP ACTUAL BROWSER LOGS ON FAILURE ---
        console.print("[bold red]❌ Browser startup failed. Dumping browser stdout/stderr:[/bold red]")
        if getattr(self, "chrome_log_file", None) and os.path.exists(self.chrome_log_file):
            try:
                with open(self.chrome_log_file, "r", encoding="utf-8") as f:
                    logs = f.read().strip()
                    console.print(f"[dim yellow]{logs if logs else '(Browser output log was empty)'}[/dim yellow]")
            except Exception as read_err:
                console.print(f"[red]Could not read log file: {read_err}[/red]")
        else:
            console.print("[dim red]No browser log file found.[/dim red]")

        if last_error:
            console.print(f"[dim red]Last CDP connection error: {last_error}[/dim red]")

        # Raise RuntimeError instead of sys.exit(1) so the engine can catch it properly
        raise RuntimeError(f"Browser process exited or CDP failed to respond on port {self.port}")

    async def connect(self) -> None:
        from config import PLATFORMS_CONFIG
        cdp_port = self.port

        from playwright.async_api import async_playwright
        self.pw = await async_playwright().start()
        for attempt in range(5):
            try:
                self.browser = await self.pw.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
                self.context = self.browser.contexts[0]
                self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
                
                await self._setup_page(self.page)
                if PLATFORMS_CONFIG.get("stealth_mode", False):
                    asyncio.create_task(self._poll_mutations())
                else:
                    await self.page.expose_function("on_dom_mutation", lambda: self.mutation_event.set())
                
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
                "**/collect?*",
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


    # ------------------------------------------------------------------
    # Chat management
    # ------------------------------------------------------------------

    async def _click_model_button(self, model_type: str) -> bool:
        """Click a DeepSeek model-type button if it's visible. Returns True on success."""
        try:
            btn = self.page.locator(f"[data-model-type='{model_type}']").first
            if await btn.is_visible(timeout=3000):
                await btn.click()
                await asyncio.sleep(0.5)
                return True
        except Exception:
            pass
        return False

    async def new_chat(self, reapply_model: bool = True) -> None:
        console.print("[dim]🚀 Warping to New Chat...[/dim]")
        self.system_injected = False
        try:
            btn = self.page.locator('[aria-label="New chat"]').first
            if await btn.is_visible(timeout=3000):
                await btn.click()
                await asyncio.sleep(2)
                console.print("[dim]✨ New chat started in AI Studio[/dim]")
            else:
                raise Exception("New chat button not visible")
        except Exception as e:
            console.print(f"[dim yellow]Could not click New Chat ({e}). Refreshing...[/dim yellow]")
            await self.page.goto(PLATFORM["url"])
            await asyncio.sleep(3)

        # DeepSeek snaps back to Instant on every new chat / reload — restore
        # whichever model type was active so a refresh never loses the choice.
        if reapply_model and self.current_model_type != "default":
            if await self._click_model_button(self.current_model_type):
                label = {"expert": "Expert", "vision": "Vision"}.get(self.current_model_type, self.current_model_type)
                console.print(f"[dim]{label} mode restored after new chat[/dim] 🚀")

    async def stop_generation(self) -> bool:
        try:
            btn = self.page.locator(PLATFORM["selectors"]["stop"]).first
            await btn.wait_for(state="visible", timeout=2000)
            await btn.click(timeout=1000)
            console.print("[bold red]Generation stopped! 🛑[/bold red]")
            return True
        except Exception:
            return False

    async def upload_file(self, file_path: str, has_msg: bool = False) -> bool:
        file_path = os.path.abspath(file_path)
        try:
            for selector in ["input[type='file']", "input[accept*='image']", "#file-input"]:
                try:
                    fi = self.page.locator(selector).first
                    await fi.set_input_files(file_path, timeout=3000)
                    await asyncio.sleep(3)
                    await self.page.keyboard.press("Escape")
                    console.print("[bold green]✅ Upload complete![/bold green]")
                    return True
                except Exception:
                    continue

            add_btn = self.page.locator(
                "button[aria-label*='Add'], button:has-text('Add'), .add-button"
            ).filter(has_not=self.page.locator("[aria-label*='New chat']")).first
            async with self.page.expect_file_chooser(timeout=5000) as fc_info:
                await add_btn.click()
            await (await fc_info.value).set_files(file_path)
            await asyncio.sleep(3)
            await self.page.keyboard.press("Escape")
            console.print("[bold green]✅ Upload complete![/bold green]")
            return True

        except Exception as e:
            console.print(f"[bold red]❌ Upload failed: {e}[/bold red]")
            return False

    # ------------------------------------------------------------------
    # send_msg
    # ------------------------------------------------------------------

    def _strip_ds(self, text: str) -> str:
        DS_PREFIX = "markdown\nCopy\nDownload\n"
        if text.startswith(DS_PREFIX):
            text = text[len(DS_PREFIX):]
        # Strip leading code-fence language tag (e.g. "text\n") from markdown block
        text = re.sub(r"^text\n", "", text)
        return text

    async def send_msg(self, message: str, raw: bool = False) -> bool:
        for attempt in range(3):
            try:
                field = await self._find_input_field()
                if field is None:
                    raise Exception("No input textarea found")

                await asyncio.sleep(0.5)
                await field.click()

                if raw:
                    # Raw mode: send message as-is without any system prompt injection.
                    # Used for internal prompts (memory consolidation, etc.)
                    pass
                elif not self.system_injected:
                    instructions = self._load_instructions()
                    markdown_instruction = "MOST IMPORTANT OF ALL\n START YOUR RESPONSE WITH ``` AND ENDS WITH ```, WRAP YOUR WHOLE RESPONSE WITH IT. DON'T USE ``` IN ANYWHERE ELSE IN YOUR RESPONSE."
                    # Memory is injected centrally by server.py — do NOT duplicate here.
                    if instructions:
                        message = f"[SYSTEM INSTRUCTION]\n{instructions}\n\n{markdown_instruction}\n\n[USER MESSAGE]\n{message}"
                    self.system_injected = True
                else:
                    # Prepend a short quick reminder to every next user message
                    reminder = (
                        "[QUICK REMINDER]\n"
                        "1. start your response with ``` and ends with ```, only two use of ``` in whole response.\n"
                        "2. Use ~~~ for code blocks instead of ```, <execute_command> to run any command.\n"
                        "3. Always use approtiate tag to run command or use skills.\n\n"
                    )
                    # Memory is injected centrally by server.py — do NOT duplicate here.
                    message = f"{reminder}[USER MESSAGE]\n{message}"

                # Try clipboard paste first for all messages to bypass automation detection and keep Angular in sync!
                filled = await self._paste_large_message(field, message)
                if not filled:
                    await field.fill(message)

                # Force React/Angular/Vue input state synchronization
                try:
                    await field.focus()
                    await self.page.keyboard.press("End")
                    await self.page.keyboard.type(" ")
                    await self.page.keyboard.press("Backspace")
                except Exception:
                    pass

                await asyncio.sleep(0.4)
                
                # Wait for send button to be enabled (important for file parsing)
                is_ready = await self._wait_for_send_enabled()
                if not is_ready:
                    self._log_debug("send_button_timeout", timeout=10)
                
                # Mark all existing messages as old so get_response can cleanly identify the new one
                await self.page.evaluate("""() => {
                    document.querySelectorAll('div.ds-message, div.ds-assistant-message-main-content').forEach(el => {
                        el.dataset.ghostOld = 'true';
                    });
                }""")

                # Capture last thinking and response before sending the new message
                thinking_sel = PLATFORM["selectors"].get("thoughts", "div.ds-think-content")
                response_sel = PLATFORM["selectors"].get("content", "div.ds-assistant-message-main-content")
                
                try:
                    self._last_thinking = await self.page.locator(thinking_sel).last.inner_text() if await self.page.locator(thinking_sel).count() > 0 else ""
                except Exception:
                    self._last_thinking = ""
                    
                try:
                    raw_last_resp = await self.page.locator(response_sel).last.inner_text() if await self.page.locator(response_sel).count() > 0 else ""
                    self._last_response_text = self._strip_ds(raw_last_resp)
                except Exception:
                    self._last_response_text = ""

                # Click send button according to user's program logic
                send_sel = PLATFORM["selectors"].get("send_btn", "div[role='button']:has(path[d^='M8.3125 0.981587'])")
                send_button = self.page.locator(send_sel).first
                classes = await send_button.get_attribute("class") or ""
                
                if "ds-button--disabled" not in classes and "disabled" not in classes.lower():
                    await send_button.click()
                else:
                    await self.page.keyboard.press(PLATFORM["keys"]["send"])

                # Wait for stop button to be visible
                stop_sel = PLATFORM["selectors"].get("stop", "div[role='button']:has(path[d^='M2 4.88'])")
                try:
                    await self.page.wait_for_selector(stop_sel, state="visible", timeout=5000)
                except Exception:
                    pass

                return True

            except Exception as e:
                console.print(f"[dim yellow]Retrying send ({attempt+1}/3): {e}[/dim yellow]")
                if attempt < 2:
                    try:
                        await self.page.reload(timeout=15000)
                        await asyncio.sleep(5)
                        await self.setup_deepseek(force_update=False)
                    except Exception as err:
                        console.print(f"[dim red]Reload failed: {err}[/dim red]")
                        await asyncio.sleep(2)
        return False

    async def _find_input_field(self):
        for sel in _INPUT_SELECTORS:
            try:
                await self.page.wait_for_selector(sel, timeout=10_000)
                return self.page.locator(sel).last
            except Exception:
                continue
        return None

    async def _paste_large_message(self, field, message: str) -> bool:
        """Inject text via JS directly — never touches the OS clipboard."""
        try:
            escaped = message.replace("\\", "\\\\").replace("`", "\\`").replace("$", "\\$")
            await self.page.evaluate(f"""
                (msg) => {{
                    const el = document.activeElement;
                    if (el && (el.isContentEditable || el.tagName === 'TEXTAREA' || el.tagName === 'INPUT')) {{
                        if (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') {{
                            el.value = msg;
                            el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                            el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                        }} else {{
                            el.textContent = msg;
                            el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                        }}
                    }}
                }}
            """, escaped)
            await asyncio.sleep(0.2)
            await field.focus()
            await self.page.keyboard.press("End")
            await self.page.keyboard.type(" ")
            await self.page.keyboard.press("Backspace")
            
            val = await field.input_value()
            if val and len(val) >= len(message) * 0.8:
                await self._resume_observer()
                return True
        except Exception:
            pass

        # 2. Direct JS injection with React/Angular prototype value setter
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
            if val and len(val) >= len(message) * 0.8:
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
    # AI Studio setup
    # ------------------------------------------------------------------

    async def switch_model(self, model_type: str) -> bool:
        """Switch DeepSeek model type (default/expert/vision).

        Opens a new chat first, then clicks the corresponding model button.
        Returns True on success.
        """
        valid_types = {"default", "expert", "vision"}
        if model_type not in valid_types:
            console.print(f"[dim red]Unknown model type: {model_type}[/dim red]")
            return False

        try:
            # Fresh chat is opened WITHOUT re-applying the old model — we're
            # about to click the new one, and this chat counts as the fresh
            # chat for the next incoming message (no redundant second new_chat).
            await self.new_chat(reapply_model=False)
            await asyncio.sleep(1)
            if await self._click_model_button(model_type):
                self.current_model_type = model_type
                self.system_injected = False
                self.has_fresh_chat = True
                label = {"default": "Instant", "expert": "Expert", "vision": "Vision"}[model_type]
                console.print(f"[dim]Switched to {label} mode[/dim] 🚀")
                return True
            console.print(f"[dim yellow]Model button '{model_type}' not visible[/dim yellow]")
            return False
        except Exception as e:
            console.print(f"[dim red]Model switch failed: {e}[/dim red]")
            return False


    async def set_thinking_mode(self, mode: str) -> None:
        """Toggle DeepThink on/off before sending a message.

        mode: "deepthink" → ensure toggle is ON, "fast" → ensure toggle is OFF.
        Called by scraper.py right before send_msg.
        """
        sel = PLATFORM["selectors"].get("deepthink_toggle")
        if not sel:
            return
        try:
            toggle = self.page.locator(sel).first
            if not await toggle.is_visible(timeout=3000):
                console.print("[dim yellow]DeepThink toggle not visible[/dim yellow]")
                return

            classes = await toggle.get_attribute("class") or ""
            pressed = await toggle.get_attribute("aria-pressed")
            is_on = "ds-toggle-button--selected" in classes and pressed != "false"

            if mode == "deepthink" and not is_on:
                await toggle.click()
                await asyncio.sleep(0.4)
                console.print("[dim]DeepThink enabled[/dim] 🧠")
            elif mode == "fast" and is_on:
                await toggle.click()
                await asyncio.sleep(0.4)
                console.print("[dim]DeepThink disabled (fast mode)[/dim] ⚡")
        except Exception as e:
            console.print(f"[dim red]Thinking mode toggle failed: {e}[/dim red]")


    async def setup_deepseek(self, force_update: bool = False, include_diary: bool = False, model_type: str | None = None) -> None:
        """Setup for DeepSeek platform."""
        try:
            console.print(f"[bold purple]Syncing CEO configurations for {PLATFORM['name']}...[/bold purple] 🔐")

            # Click the requested model type button if visible (defaults to current)
            effective_type = model_type or self.current_model_type
            try:
                model_btn = self.page.locator(f"[data-model-type='{effective_type}']").first
                if await model_btn.is_visible(timeout=2000):
                    await model_btn.click()
                    label = {"default": "Instant", "expert": "Expert", "vision": "Vision"}.get(effective_type, effective_type)
                    console.print(f"[dim]{label} mode enabled[/dim] 🚀")
            except Exception:
                pass

            # Toggle DeepThink if supported
            if "deepthink_toggle" in PLATFORM["selectors"]:
                try:
                    toggle = self.page.locator(PLATFORM["selectors"]["deepthink_toggle"]).first
                    if await toggle.is_visible(timeout=2000):
                        classes = await toggle.get_attribute("class") or ""
                        pressed = await toggle.get_attribute("aria-pressed")
                        
                        if "ds-toggle-button--selected" not in classes or pressed == "false":
                            await toggle.click()
                            console.print("[dim]DeepThink enabled[/dim] 🧠")
                        else:
                            console.print("[dim]DeepThink already active[/dim] 🧠")
                except: pass

            await self._inject_mutation_observer()
            console.print(f"[bold green]✅ {PLATFORM['name']} ready! [/bold green]")
        except Exception as e:
            console.print(f"[dim red]Setup failed: {e}[/dim red]")

    def _load_instructions(self) -> str:
        parts: list[str] = []

        # Load Maria.md first
        maria_path = _INSTRUCTION_PATHS[0]
        if os.path.exists(maria_path):
            try:
                with open(maria_path) as f:
                    parts.append(f.read())
            except Exception:
                pass

        # Memory is now injected per-message via semantic search (see send_msg)

        # Load remaining instructions (output_format.md, skills.md)
        for path in _INSTRUCTION_PATHS[1:]:
            if os.path.exists(path):
                try:
                    with open(path) as f:
                        parts.append(f.read())
                except Exception:
                    pass

        return "\n\n".join(parts) + f"\n\nPROJECT_ROOT = {PROJECT_ROOT} \n OUTPUT_FOLDER = {PROJECT_ROOT / 'output'}"

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

    def _clean_thoughts_text(self, text: str) -> str:
        if not text:
            return ""
        lines = [
            line.strip() for line in text.splitlines()
            if line.strip()
            and line.strip().lower() not in _THOUGHT_IGNORED_EXACT
            and "expand to view model thoughts" not in line.lower()
            and not re.fullmatch(r"model\s+\d{1,2}:\d{2}(?:\s*[ap]m)?", line.strip().lower())
        ]
        cleaned = "\n\n".join(lines)
        cleaned = re.sub(r"(?:###\s*🧠\s*Model Thoughts\s*)+", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"(?:🧠\s*Model Tho(?:ughts?)?)+",   "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        return cleaned.strip()

    # ------------------------------------------------------------------
    # Thoughts panel
    # ------------------------------------------------------------------

    async def _expand_thoughts_panel_if_needed(self, initial_count: int = 0, force: bool = False) -> bool:
        now = time.time()
        if not force and (now - self.last_thought_expand_at) < 5.0:
            return False
        try:
            base_message_sel = PLATFORM["selectors"].get("response", "div.ds-message")
            message_sel = f"{base_message_sel}:not([data-ghost-old='true'])"
            turns = self.page.locator(message_sel)
            if await turns.count() == 0:
                return False
            last_turn = turns.last
            for sel in [
                "ms-thought-chunk mat-expansion-panel-header[aria-disabled='false']",
                "mat-expansion-panel-header.top-panel-header[aria-disabled='false']",
                "mat-expansion-panel-header:has-text('Thoughts')",
            ]:
                try:
                    header = last_turn.locator(sel).last
                    if not await header.is_visible(timeout=200):
                        continue
                    expanded = await header.get_attribute("aria-expanded", timeout=500)
                    if expanded == "true":
                        return True
                    if expanded == "false":
                        await header.click(timeout=1000, force=True)
                        self.last_thought_expand_at = now
                        self._log_debug("expanded_thoughts_panel")
                        await asyncio.sleep(0.5)
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    async def get_response_count(self) -> int:
        """Count assistant-only response elements on the page."""
        try:
            # Use the content selector to count only assistant responses,
            # NOT div.ds-message which includes user messages too.
            sel = PLATFORM["selectors"].get("content", "div.ds-assistant-message-main-content")
            return len(await self.page.query_selector_all(sel) or [])
        except Exception:
            return 0

    async def extract_thoughts(self, initial_count: int = 0, force_expand: bool = False) -> str | None:
        if not self.show_thoughts:
            return None
        try:
            base_message_sel = PLATFORM["selectors"].get("response", "div.ds-message")
            message_sel = f"{base_message_sel}:not([data-ghost-old='true'])"
            turns = self.page.locator(message_sel)
            if await turns.count() == 0:
                return None
            await self._expand_thoughts_panel_if_needed(initial_count, force=force_expand)
            thought_sel = PLATFORM["selectors"].get("thoughts", ".ds-think-content")
            js_code = """el => {
                let body = el.querySelector('THOUGHT_SEL');
                
                if (!body) return null;
                
                const clone = body.cloneNode(true);
                const garbage = clone.querySelectorAll('button, .ds-icon-button');
                garbage.forEach(g => g.remove());
                
                clone.querySelectorAll('br').forEach(br => {
                    br.replaceWith(document.createTextNode('\\n'));
                });
                clone.querySelectorAll('p, div, li, h1, h2, h3, h4, h5, h6').forEach(el => {
                    el.appendChild(document.createTextNode('\\n'));
                });
                
                return (clone.textContent || '').trim();
            }""".replace('THOUGHT_SEL', thought_sel)
            
            raw = await turns.last.evaluate(js_code)
            if raw:
                cleaned = self._clean_thoughts_text(raw)
                if cleaned:
                    self._log_debug("thoughts_extracted", preview=cleaned[:220])
                    return cleaned
        except Exception as e:
            self._log_debug("extract_thoughts_failed", error=str(e))
        return None

    # ------------------------------------------------------------------
    # Response capture
    # ------------------------------------------------------------------

    async def get_response(self, **kwargs) -> str:
        """Route to DeepSeek capture."""
        return await self.get_response_deepseek(**kwargs)




    async def get_response_deepseek(
        self,
        initial_count: int = 0,
        live_display=None,
        thoughts_callback=None,
        last_response: str = "",
        user_input: str = "",
    ) -> str:
        """Dedicated DeepSeek capture logic matching Playwright user program logic."""
        self._log_debug("deepseek_capture_started")
        
        stop_sel = PLATFORM["selectors"].get("stop", "div[role='button']:has(path[d^='M2 4.88'])")
        thinking_sel = PLATFORM["selectors"].get("thoughts", "div.ds-think-content")
        response_sel = PLATFORM["selectors"].get("content", "div.ds-assistant-message-main-content")

        last_thinking = getattr(self, "_last_thinking", "")
        last_response_val = getattr(self, "_last_response_text", "")

        cached_thinking = ""
        cached_response = ""

        # Wait for generation to actually start.
        # The stop button appearing means DeepSeek began generating.
        # If the server is slow, we poll here indefinitely instead of
        # accidentally capturing the previous message.
        stop_locator = self.page.locator(stop_sel)
        new_response_sel = f"{response_sel}:not([data-ghost-old='true'])"

        wait_deadline = time.time() + 60
        while True:
            if await stop_locator.is_visible():
                break
            # Fast-response edge case: stop already disappeared but new content exists
            if await self.page.locator(new_response_sel).count() > 0:
                break
            if time.time() > wait_deadline:
                console.print("[bold red]⏰ Timed out waiting for DeepSeek response (60s)[/bold red]")
                return "", ""
            await asyncio.sleep(0.3)

        # Loop while stop button is visible (streaming capture)
        while await stop_locator.is_visible():
            try:
                thinking_stream = await self.page.locator(thinking_sel).last.inner_text() if await self.page.locator(thinking_sel).count() > 0 else ""
            except Exception:
                thinking_stream = ""

            try:
                response_stream = await self.page.locator(response_sel).last.inner_text() if await self.page.locator(response_sel).count() > 0 else ""
            except Exception:
                response_stream = ""

            # Check thinking stream changes
            if thinking_stream != cached_thinking and thinking_stream != last_thinking:
                cached_thinking = thinking_stream
                if thoughts_callback and self.show_thoughts:
                    cleaned_thoughts = self._clean_thoughts_text(thinking_stream)
                    await thoughts_callback(cleaned_thoughts)

            # Check response stream changes
            response_stream = self._strip_ds(response_stream)
            if response_stream != cached_response and response_stream != last_response_val:
                cached_response = response_stream
                if live_display:
                    cleaned_response = self._clean_garbage(response_stream)
                    live_display(cleaned_response)

            await asyncio.sleep(0.3)

        # After stop button is no longer visible, capture the final content.
        # Only grab NEW content (not marked ghost-old by send_msg).
        new_thinking_sel = f"{thinking_sel}:not([data-ghost-old='true'])"
        try:
            loc = self.page.locator(new_thinking_sel)
            final_thinking = await loc.last.inner_text() if await loc.count() > 0 else ""
        except Exception:
            final_thinking = ""

        try:
            loc = self.page.locator(new_response_sel)
            final_response = await loc.last.inner_text() if await loc.count() > 0 else ""
        except Exception:
            final_response = ""

        final_response = self._strip_ds(final_response)

        # Trigger final callbacks
        if thoughts_callback and self.show_thoughts and final_thinking:
            await thoughts_callback(self._clean_thoughts_text(final_thinking))
        if live_display and final_response:
            live_display(self._clean_garbage(final_response))

        return self._clean_garbage(final_response)

    async def _wait_for_send_enabled(self, timeout: float = 10.0) -> bool:
        """Wait for the send button to be enabled (not busy parsing files)."""
        sel = PLATFORM["selectors"].get("send_btn")
        if not sel: return True
        
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                btn = self.page.locator(sel).first
                if await btn.count() > 0:
                    classes = await btn.get_attribute("class") or ""
                    disabled = await btn.get_attribute("aria-disabled")
                    # For DeepSeek: ds-icon-button--disabled
                    if "disabled" not in classes.lower() and disabled != "true":
                        return True
                else:
                    # If button isn't found, maybe it's still loading or the selector changed
                    pass
            except Exception:
                pass
            await asyncio.sleep(0.5)
        return False

    async def _get_element_text(self, element) -> str:
        try:
            content_sel = PLATFORM["selectors"].get("content")
            if content_sel:
                target = await element.query_selector(content_sel)
                if target:
                    return (await target.inner_text()).strip()

            return await element.evaluate("""el => {
                const garbage = [
                    'button','.ds-icon-button'
                ];
                const nodes = el.querySelectorAll(garbage.join(','));
                const styles = [];
                nodes.forEach(n => { styles.push(n.style.getPropertyValue('display')); n.style.setProperty('display','none','important'); });
                const txt = (el.innerText || el.textContent || '').trim();
                nodes.forEach((n, i) => n.style.setProperty('display', styles[i] || '', 'important'));
                return txt;
            }""")
        except Exception:
            return ""

    async def _get_element_text_direct(self, element) -> str:
        """Get text directly from a content element (already the right container)."""
        try:
            return (await element.inner_text()).strip()
        except Exception:
            return ""

    async def _is_stop_active(self) -> bool:
        """Check if DeepSeek's stop button is visible."""
        try:
            sel = PLATFORM["selectors"].get("stop", "div.ds-icon-button:has(path[d^='M2 4.88'])")
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
        try:
            sel = PLATFORM["selectors"].get("skip_vote")
            if not sel:
                return False
            btn = self.page.locator(sel).first
            if await btn.is_visible(timeout=200):
                await btn.click(timeout=500, force=True)
                self._log_debug("auto_skipped_preference_vote")
                console.print("[dim purple]Auto-skipped preference vote! [/dim purple]")
                return True
        except Exception:
            pass
        return False

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

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def cleanup(self) -> None:
        console.print("[bold purple]Closing... Bye baby! [/bold purple]")
        if self.browser:
            await self.browser.close()
        if self.pw:
            await self.pw.stop()
            
        if self.chrome_process:
            try:
                from engine.process_utils import kill_process_tree
                kill_process_tree(self.chrome_process.pid, sig=signal.SIGTERM)
            except Exception:
                pass

        self.final_tokens: str | None = None
#
