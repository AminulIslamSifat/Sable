"""BaseScraperEngine — shared ABC for all browser-based scraper engines.

Every provider engine subclasses this and overrides only what differs.
Shared logic (browser launch, connect, input, paste, cleanup, text cleaning,
etc.) lives here so it's written once and maintained in one place.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("sable.scraper")

# ---------------------------------------------------------------------------
# Constants shared across all providers
# ---------------------------------------------------------------------------

_CHROME_BINARIES = ["thorium-browser", "google-chrome", "chromium", "chromium-browser"]

_GHOST_CSS = """
/* Ghost Engine UI cleanup */
::-webkit-scrollbar { width: 6px !important; }
::-webkit-scrollbar-thumb { background: #444 !important; border-radius: 3px !important; }
::-webkit-scrollbar-track { background: transparent !important; }
"""

_UI_GARBAGE_WORDS = (
    r"Copy|Download|Share|Regenerate|Good response\?|"
    r"Was this helpful\?|Rate|Feedback|Dislike|Like|"
    r"View more|Show less|Expand|Collapse|Read more"
)

_THOUGHT_IGNORED_EXACT = {
    "expand to view model thoughts",
    "model thoughts",
    "thoughts",
}

_INPUT_SELECTORS = [
    "textarea[name='search']",
    "textarea.message-input-textarea",
    "textarea[placeholder*='help']",
    "textarea[placeholder*='message']",
    "div[contenteditable='true']",
    "textarea",
]

_INSTRUCTION_PATHS_DEFAULT = [
    "Maria.md",
    "output_format.md",
    "skills.md",
]


# ---------------------------------------------------------------------------
# BaseScraperEngine
# ---------------------------------------------------------------------------

class BaseScraperEngine(ABC):
    """Abstract base class for browser scraper engines.

    Subclasses MUST implement:
        - send_msg()
        - get_response()
        - new_chat()
        - stop_generation()

    Subclasses CAN override any other method for provider-specific behavior.
    """

    # Override in subclass to declare capabilities
    PROVIDER_CAPABILITIES: dict[str, bool] = {
        "has_thinking_toggle": False,
        "has_model_switch": False,
        "stop_via_api": False,
        "has_file_upload": True,
        "has_clipboard_paste": True,
        "has_diary": False,
        "has_persona_sync": False,
        "has_bridge_session": False,
        "has_commands": False,
    }

    # Provider name — override in subclass
    PROVIDER_NAME: str = "base"

    def __init__(
        self,
        port: int = 9222,
        viewer: bool = True,
        show_thoughts: bool = False,
    ) -> None:
        from config import PLATFORMS_CONFIG

        use_obsidian_config = PLATFORMS_CONFIG.get("use_obsidian", True)
        if not use_obsidian_config and port == 9222:
            if self._is_obsidian_running_on_port(9222):
                port = 9225
                logger.info("Obsidian on 9222, diverting to 9225")

        self.port = port
        self.viewer = viewer
        self.show_thoughts = show_thoughts
        self.user_data_dir = os.path.expanduser("~/.local/share/ghostchat/chrome-data")
        self.chrome_process: subprocess.Popen | None = None
        self.pw: Any = None
        self.browser: Any = None
        self.context: Any = None
        self.page: Any = None
        self.last_instruction_hash: str | None = None
        self.last_thought_expand_at: float = 0
        self.last_tokens: str | None = None
        self.mutation_event = asyncio.Event()
        self.clipboard_tool = self._detect_clipboard_tool()
        self.system_injected = False
        self.image_attached = False

        # Resolve output dirs
        self._resolve_paths()

        logs_dir = os.path.join(self.output_root, "logs")
        os.makedirs(logs_dir, exist_ok=True)
        self.response_log_file = os.path.join(
            logs_dir, f"response_capture_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        )
        self._log_debug("session_started", port=self.port, viewer=self.viewer)

    # ------------------------------------------------------------------
    # Path resolution
    # ------------------------------------------------------------------

    def _resolve_paths(self) -> None:
        """Resolve output/instruction paths. Override for provider-specific paths."""
        from config import SABLE_ROOT, OUTPUT_ROOT, ASSETS_DIR, INSTRUCTIONS_DIR
        self.sable_root = SABLE_ROOT
        self.output_root = OUTPUT_ROOT
        self.assets_dir = ASSETS_DIR
        self.instructions_dir = INSTRUCTIONS_DIR

    # ------------------------------------------------------------------
    # Platform config access
    # ------------------------------------------------------------------

    @staticmethod
    def get_platform_config() -> dict[str, Any]:
        """Return the active platform config dict from platforms.json."""
        from config import PLATFORM, PLATFORMS_CONFIG
        return PLATFORM

    @staticmethod
    def get_platforms_config() -> dict[str, Any]:
        """Return the full platforms.json config."""
        from config import PLATFORMS_CONFIG
        return PLATFORMS_CONFIG

    @classmethod
    def get_selector(cls, key: str, default: str = "") -> str:
        """Get a selector from the active platform config."""
        platform = cls.get_platform_config()
        return platform.get("selectors", {}).get(key, default)

    @classmethod
    def get_key(cls, key: str, default: str = "Enter") -> str:
        """Get a key binding from the active platform config."""
        platform = cls.get_platform_config()
        return platform.get("keys", {}).get(key, default)

    @classmethod
    def get_ui_config(cls, key: str, default: Any = None) -> Any:
        """Get a UI config value. Providers can add ui_config to platforms.json."""
        platform = cls.get_platform_config()
        return platform.get("ui_config", {}).get(key, default)

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def _log_debug(self, event: str, **fields: Any) -> None:
        try:
            payload = {"ts": datetime.now().isoformat(timespec="seconds"), "event": event, **fields}
            with open(self.response_log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Clipboard detection
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_clipboard_tool() -> str | None:
        if shutil.which("wl-paste"):
            return "wl-paste"
        if shutil.which("xclip"):
            return "xclip"
        return None

    # ------------------------------------------------------------------
    # Obsidian detection
    # ------------------------------------------------------------------

    def _is_obsidian_running_on_port(self, port: int) -> bool:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.3)
                if s.connect_ex(("127.0.0.1", port)) != 0:
                    return False
        except Exception:
            return False
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/json",
                headers={"User-Agent": "GhostChat"},
            )
            with urllib.request.urlopen(req, timeout=0.8) as response:
                pages = json.loads(response.read().decode("utf-8"))
                for page in pages:
                    url = page.get("url", "")
                    if "obsidian.md/index.html" in url or "app://obsidian.md" in url:
                        return True
        except Exception:
            pass
        return False

    # ------------------------------------------------------------------
    # Port / CDP helpers
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
            remote_profile = data.get("userDataDir", "") or data.get("profile-path", "")
            if os.path.realpath(remote_profile) != os.path.realpath(self.user_data_dir):
                return False
            cmd_line = data.get("BrowserCommandLine", "")
            if "--headless" in cmd_line:
                return False
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Browser launch
    # ------------------------------------------------------------------

    async def launch_chrome(self) -> None:
        """Launch Chrome/Chromium with CDP. Override for provider-specific launch args."""
        if await self._is_own_headed_session():
            logger.info("Found existing headed session with matching profile")
            return

        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            try:
                path = os.path.join(self.user_data_dir, name)
                if os.path.exists(path) or os.path.islink(path):
                    os.remove(path)
            except Exception:
                pass

        # WSL2 support
        from engine.wsl_browser import is_wsl2, launch_windows_chrome
        if is_wsl2():
            wsl_session = launch_windows_chrome(
                self.user_data_dir, port=self.port, headless=not self.viewer,
                extra_args=["--force-dark-mode", "--enable-features=WebUIDarkMode"],
            )
            if wsl_session is not None:
                self.chrome_process = wsl_session.process
                await self._wait_for_cdp_ready()
                return

        from engine.platform_paths import system_chrome_candidates, find_playwright_chrome
        chrome_path = next((shutil.which(b) for b in _CHROME_BINARIES if shutil.which(b)), None)
        if not chrome_path:
            for cand in system_chrome_candidates():
                if os.path.isfile(cand):
                    chrome_path = cand
                    break
        if not chrome_path:
            chrome_path = find_playwright_chrome()
        if not chrome_path:
            raise RuntimeError("No Chrome/Chromium found")

        cmd = self._build_chrome_cmd(chrome_path)
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

    def _build_chrome_cmd(self, chrome_path: str) -> list[str]:
        """Build Chrome command line. Override to add provider-specific flags."""
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
        return cmd

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
            raise RuntimeError(f"Browser exited before CDP came up (code {self.chrome_process.returncode})")
        msg = f"Timed out waiting for CDP on port {self.port}"
        if last_error:
            msg += f" (last error: {last_error})"
        raise RuntimeError(msg)

    # ------------------------------------------------------------------
    # Connect
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Connect to browser via CDP. Override for provider-specific connection logic (e.g. Obsidian proxy)."""
        from config import PLATFORMS_CONFIG, PLATFORM
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
    # Mutation polling
    # ------------------------------------------------------------------

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

    async def _setup_page(self, page: Any) -> None:
        """Apply dark mode, block analytics, inject CSS. Override for provider-specific page setup."""
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
    # Input field finding
    # ------------------------------------------------------------------

    async def _find_input_field(self) -> Any | None:
        """Find the chat input field using known selectors. Override for provider-specific selectors."""
        # Try provider-specific selector first
        provider_sel = self.get_selector("input")
        if provider_selector:
            try:
                await self.page.wait_for_selector(provider_selector, timeout=10_000)
                return self.page.locator(provider_selector).last
            except Exception:
                pass

        # Fall back to generic selectors
        for sel in _INPUT_SELECTORS:
            try:
                await self.page.wait_for_selector(sel, timeout=10_000)
                return self.page.locator(sel).last
            except Exception:
                continue
        return None

    # ------------------------------------------------------------------
    # Message injection (paste)
    # ------------------------------------------------------------------

    async def _paste_large_message(self, field: Any, message: str) -> bool:
        """Inject text via JS directly — never touches the OS clipboard.
        Override for provider-specific injection strategies.
        """
        # Method 1: Direct JS injection with event dispatch
        try:
            # Pass message as evaluate argument to avoid JS escaping issues
            await self.page.evaluate("""
                (msg) => {
                    const el = document.activeElement;
                    if (el && (el.isContentEditable || el.tagName === 'TEXTAREA' || el.tagName === 'INPUT')) {
                        if (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') {
                            el.value = msg;
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                        } else {
                            el.textContent = msg;
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                        }
                    }
                }
            """, message)
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

        # Method 2: React/Angular prototype value setter
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
    # Mutation observer injection
    # ------------------------------------------------------------------

    async def _inject_mutation_observer(self) -> None:
        """Inject DOM mutation observer for stealth mode. Override for provider-specific observers."""
        try:
            await self.page.evaluate("""() => {
                if (window.ghost_observer) return;
                window.__ghost_mutations = [];
                window.ghost_observer = new MutationObserver((mutations) => {
                    window.__ghost_mutations.push(...mutations);
                    if (window.on_dom_mutation) window.on_dom_mutation();
                });
                window.ghost_observer.observe(document.body, {
                    childList: true, subtree: true, characterData: true
                });
            }""")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Text cleaning
    # ------------------------------------------------------------------

    def _clean_garbage(self, text: str) -> str:
        if not text:
            return text
        text = re.sub(rf"(?i)^({_UI_GARBAGE_WORDS}\s*)+", "", text, flags=re.MULTILINE)
        text = re.sub(rf"(?i)({_UI_GARBAGE_WORDS}\s*)+$", "", text, flags=re.MULTILINE)
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
        """Clean thinking panel text. Override for provider-specific cleaning."""
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
        cleaned = re.sub(r"(?:🧠\s*Model Tho(?:ughts?)?)+", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        return cleaned.strip()

    # ------------------------------------------------------------------
    # Element text extraction
    # ------------------------------------------------------------------

    async def _get_element_text(self, element: Any) -> str:
        """Extract text from a DOM element. Override for provider-specific extraction."""
        try:
            text = await element.inner_text(timeout=3000)
            return text.strip() if text else ""
        except Exception:
            return await self._get_element_text_direct(element)

    async def _get_element_text_direct(self, element: Any) -> str:
        try:
            text = await element.evaluate("el => (el.innerText || el.textContent || '').trim()")
            return text or ""
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # Stop button detection
    # ------------------------------------------------------------------

    async def _is_stop_active(self) -> bool:
        """Check if the stop button is visible (generation in progress)."""
        stop_sel = self.get_selector("stop")
        if not stop_sel:
            return False
        try:
            btn = self.page.locator(stop_sel).first
            return await btn.is_visible(timeout=1000)
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Scroll
    # ------------------------------------------------------------------

    async def _scroll_to_bottom(self) -> None:
        try:
            await self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(0.3)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Auto-skip preference vote
    # ------------------------------------------------------------------

    async def _auto_skip_preference_vote(self) -> bool:
        """Auto-dismiss thumbs up/down prompts. Override for provider-specific selectors."""
        return False

    # ------------------------------------------------------------------
    # Wait for send button enabled
    # ------------------------------------------------------------------

    async def _wait_for_send_enabled(self, timeout: float = 10.0) -> bool:
        """Wait until the send button is enabled. Override for provider-specific logic."""
        send_sel = self.get_selector("send_btn")
        if not send_sel:
            return True
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                btn = self.page.locator(send_sel).first
                classes = await btn.get_attribute("class") or ""
                if "disabled" not in classes.lower():
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.3)
        return False

    # ------------------------------------------------------------------
    # Instructions loading
    # ------------------------------------------------------------------

    def _load_instructions(self) -> str:
        """Load instruction files. Override for provider-specific instruction paths."""
        from config import INSTRUCTIONS_DIR, PROJECT_ROOT
        parts: list[str] = []

        instruction_paths = getattr(self, "INSTRUCTION_PATHS", _INSTRUCTION_PATHS_DEFAULT)
        for filename in instruction_paths:
            path = INSTRUCTIONS_DIR / filename
            if os.path.exists(path):
                try:
                    with open(path) as f:
                        parts.append(f.read())
                except Exception:
                    pass

        result = "\n\n".join(parts)
        if result:
            result += f"\n\nPROJECT_ROOT = {PROJECT_ROOT} \n OUTPUT_FOLDER = {PROJECT_ROOT / 'output'}"
        return result

    # ------------------------------------------------------------------
    # File upload
    # ------------------------------------------------------------------

    async def upload_file(self, file_path: str, has_msg: bool = False) -> bool:
        """Upload a file to the chat. Override for provider-specific upload methods."""
        file_path = os.path.abspath(file_path)
        if not os.path.exists(file_path):
            logger.error("File not found: %s", file_path)
            return False

        try:
            for selector in ["input[type='file']", "input[accept*='image']", "#file-input"]:
                try:
                    fi = self.page.locator(selector).first
                    await fi.set_input_files(file_path, timeout=3000)
                    await asyncio.sleep(3)
                    await self.page.keyboard.press("Escape")
                    logger.info("Upload complete via %s", selector)
                    self.image_attached = True
                    return True
                except Exception:
                    continue

            # Fallback: click add button and use file chooser
            add_btn = self.page.locator(
                "button[aria-label*='Add'], button:has-text('Add'), .add-button"
            ).filter(has_not=self.page.locator("[aria-label*='New chat']")).first
            async with self.page.expect_file_chooser(timeout=5000) as fc_info:
                await add_btn.click()
            await (await fc_info.value).set_files(file_path)
            await asyncio.sleep(3)
            await self.page.keyboard.press("Escape")
            self.image_attached = True
            return True

        except Exception as e:
            logger.error("Upload failed: %s", e)
            return False

    async def upload_from_clipboard(self, has_msg: bool = False) -> tuple[bool, str | None]:
        """Grab image from clipboard and upload. Shared across providers."""
        if not self.clipboard_tool:
            logger.error("No clipboard tool found")
            return False, None

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        tmp_path = os.path.join(self.assets_dir, f"ghost_paste_{ts}.png")
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
                        await asyncio.sleep(5)
                        self.image_attached = True
                        return True, tmp_path
                except Exception:
                    pass

                ok = await self.upload_file(tmp_path, has_msg=has_msg)
                return ok, tmp_path
        except Exception as e:
            logger.error("Clipboard grab failed: %s", e)
        return False, None

    # ------------------------------------------------------------------
    # Response count
    # ------------------------------------------------------------------

    async def get_response_count(self) -> int:
        """Count assistant response elements. Override for provider-specific selectors."""
        try:
            sel = self.get_selector("content")
            if sel:
                return len(await self.page.query_selector_all(sel) or [])
        except Exception:
            pass
        return 0

    # ------------------------------------------------------------------
    # Thoughts extraction
    # ------------------------------------------------------------------

    async def extract_thoughts(self, initial_count: int = 0, force_expand: bool = False) -> str | None:
        """Extract thinking panel content. Override for provider-specific thought extraction."""
        if not self.show_thoughts:
            return None
        thought_sel = self.get_selector("thoughts")
        if not thought_sel:
            return None
        try:
            response_sel = self.get_selector("response")
            if not response_sel:
                return None
            message_sel = f"{response_sel}:not([data-ghost-old='true'])"
            turns = self.page.locator(message_sel)
            if await turns.count() == 0:
                return None

            js_code = """el => {
                let body = el.querySelector('THOUGHT_SEL');
                if (!body) return null;
                const clone = body.cloneNode(true);
                const garbage = clone.querySelectorAll('button, .ds-icon-button');
                garbage.forEach(g => g.remove());
                clone.querySelectorAll('br').forEach(br => {
                    br.replaceWith(document.createTextNode('\n'));
                });
                clone.querySelectorAll('p, div, li, h1, h2, h3, h4, h5, h6').forEach(el => {
                    el.appendChild(document.createTextNode('\n'));
                });
                return (clone.textContent || '').trim();
            }""".replace('THOUGHT_SEL', thought_sel)

            raw = await turns.last.evaluate(js_code)
            if raw:
                cleaned = self._clean_thoughts_text(raw)
                if cleaned:
                    self._log_debug("thoughts_extracted", preview=cleaned[:220])
                    return cleaned
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # Virtual methods — override in subclass as needed
    # ------------------------------------------------------------------

    async def switch_model(self, model_type: str) -> bool:
        """Switch model type. Default: not supported."""
        logger.warning("%s does not support model switching", self.PROVIDER_NAME)
        return False

    async def set_thinking_mode(self, mode: str) -> None:
        """Toggle thinking mode. Default: no-op."""
        pass

    async def setup_provider(self, **kwargs: Any) -> None:
        """Provider-specific setup after connect. Override as needed."""
        pass

    async def handle_command(self, u_input: str) -> tuple[bool, bool, str | None]:
        """Handle slash commands. Default: not handled."""
        return False, False, None

    async def bridge_session(self) -> str:
        """Generate bridge prompt for session continuity. Override as needed."""
        return ""

    # ------------------------------------------------------------------
    # Abstract methods — MUST be implemented by subclass
    # ------------------------------------------------------------------

    @abstractmethod
    async def send_msg(self, message: str, **kwargs: Any) -> bool:
        """Send a message to the chat. Must be implemented by provider."""
        ...

    @abstractmethod
    async def get_response(self, **kwargs: Any) -> str:
        """Capture the AI response. Must be implemented by provider."""
        ...

    @abstractmethod
    async def new_chat(self, **kwargs: Any) -> None:
        """Start a new chat session. Must be implemented by provider."""
        ...

    @abstractmethod
    async def stop_generation(self, **kwargs: Any) -> bool:
        """Stop ongoing generation. Must be implemented by provider."""
        ...

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def cleanup(self) -> None:
        """Clean up browser resources. Override for provider-specific cleanup."""
        try:
            if self.pw:
                await self.pw.stop()
        except Exception:
            pass
        try:
            if self.chrome_process and self.chrome_process.poll() is None:
                self.chrome_process.terminate()
                try:
                    await asyncio.wait_for(
                        asyncio.get_event_loop().run_in_executor(None, self.chrome_process.wait),
                        timeout=5.0,
                    )
                except asyncio.TimeoutError:
                    self.chrome_process.kill()
        except Exception:
            pass
        self.pw = None
        self.browser = None
        self.context = None
        self.page = None
