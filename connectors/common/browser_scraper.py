"""Generic headed browser scraper engine — shared by every scraper connector.

One real (headed) Chrome per account, driven over CDP. Everything provider
specific (selectors, the SSE endpoint to tap, the delta parser, warmup quirks)
arrives through a ``ScraperSpec`` plus a couple of overridable hooks, so the
same engine serves DeepSeek, Kimi, Gemini-web, or any other chat SPA without
copy-pasting the browser plumbing into each connector.

Event contract (consumed by ``BaseScraperConnector``):
    {"type": "thinking", "text": "..."}
    {"type": "answer",   "text": "..."}
    {"type": "error",    "message": "..."}
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import socket
import subprocess
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("sable.browser_scraper")


# ---------------------------------------------------------------------------
# Spec
# ---------------------------------------------------------------------------

@dataclass
class ScraperSpec:
    """Provider-specific configuration for :class:`BaseBrowserEngine`."""

    provider: str
    url: str
    selectors: dict[str, str]
    keys: dict[str, str] = field(default_factory=lambda: {"send": "Enter"})
    sse_target: str | None = None            # URL substring whose SSE stream to tap
    stealth_mode: bool = True
    instruction_provider: str | None = None  # provider key for instruction_builder

    def sel(self, key: str, default: str = "") -> str:
        return self.selectors.get(key, default)


_GENERIC_INPUT_SELECTORS = [
    "textarea[name='search']",
    "textarea[placeholder*='message']",
    "textarea[placeholder*='help']",
    "textarea[placeholder*='ask']",
    "div[contenteditable='true']",
    "textarea",
]

_UI_GARBAGE_WORDS = (
    r"Copy|Download|Share|Regenerate|Good response\?|Was this helpful\?|"
    r"Rate|Feedback|Dislike|Like|View more|Show less|Expand|Collapse|Read more"
)

_THOUGHT_IGNORED_EXACT = {
    "thoughts", "thinking", "expand to view model thoughts",
    "chevron_right", "expand_less", "expand_more",
}

_GHOST_CSS = """
::-webkit-scrollbar { width: 6px !important; }
::-webkit-scrollbar-thumb { background: #444 !important; border-radius: 3px !important; }
::-webkit-scrollbar-track { background: transparent !important; }
.feedback-container, .help-button, .upgrade-card { display: none !important; }
"""

_TOOL_REMINDER = (
    "[reminder]\n"
    "tool calling format — every tool call MUST be a single JSON array "
    "wrapped in an <action> block:\n"
    '<action>[{"name": "<function-name>", "arguments": { ... }}]</action>\n'
    "Multiple calls go in the SAME array. Never emit DSML. Keep prose before "
    "it to one short sentence. Place it at the END of the response.\n"
    "[user message]\n"
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _pids_using_profile(profile_dir: str) -> set[int]:
    """Return PIDs whose command line references `profile_dir` as --user-data-dir.

    Linux-only (reads /proc). Returns an empty set elsewhere or on any error.
    """
    import glob
    target = "--user-data-dir=" + os.path.realpath(profile_dir)
    target_alt = "--user-data-dir=" + os.path.abspath(profile_dir)
    pids: set[int] = set()
    for cmdline_path in glob.glob("/proc/[0-9]*/cmdline"):
        try:
            with open(cmdline_path, "rb") as fh:
                raw = fh.read()
            if not raw:
                continue
            args = raw.split(b"\x00")
            for arg in args:
                try:
                    text = arg.decode("utf-8", "ignore")
                except Exception:
                    continue
                if text == target or text == target_alt:
                    pid = int(cmdline_path.split("/")[2])
                    pids.add(pid)
                    break
        except (OSError, ValueError):
            continue
    return pids


def _singleton_pid(profile_dir: str) -> int | None:
    """PID recorded in the profile's SingletonLock symlink, if any."""
    lock = Path(profile_dir) / "SingletonLock"
    if not (lock.is_symlink() or lock.exists()):
        return None
    try:
        _, _, pid_str = os.readlink(lock).rpartition("-")
        return int(pid_str)
    except Exception:
        return None


def purge_profile_browsers(profile_dir: str, *, exclude_pid: int | None = None) -> list[int]:
    """Kill every browser holding `profile_dir` so a fresh launch can proceed.

    Chromium allows only ONE process per profile dir (SingletonLock). This finds
    all PIDs pointing at the profile (via /proc cmdline + the SingletonLock
    symlink), kills their process trees, then clears the stale lock files.

    Returns the list of PIDs that were targeted.
    """
    from engine.process_utils import kill_process_tree

    pids = _pids_using_profile(profile_dir)
    lock_pid = _singleton_pid(profile_dir)
    if lock_pid is not None:
        pids.add(lock_pid)

    me = os.getpid()
    victims = sorted(p for p in pids if p and p != me and p != exclude_pid)

    for pid in victims:
        try:
            os.kill(pid, 0)  # liveness probe
        except (ProcessLookupError, PermissionError, OSError):
            continue
        try:
            kill_process_tree(pid)
        except Exception:
            pass

    # Clear stale locks left behind after the kills.
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            (Path(profile_dir) / name).unlink(missing_ok=True)
        except OSError:
            pass

    return victims


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class BaseBrowserEngine:
    """Headed-browser chat driver.

    Subclasses override only the bits that differ — most commonly
    :meth:`parse_sse_delta`, and optionally :meth:`send_hello_warmup`.
    """

    def __init__(
        self,
        spec: ScraperSpec,
        *,
        port: int | None = None,
        viewer: bool = True,
        show_thoughts: bool = False,
        user_data_dir: str | Path | None = None,
        profile_name: str | None = None,
        kill_existing: bool = True,
    ) -> None:
        self.spec = spec
        # When True, launch() kills any other browser holding this profile
        # before starting, so switching into scraper mode never hits a
        # SingletonLock war with a previously-open profile browser.
        self.kill_existing = kill_existing
        self.port = int(port or _free_port())
        self.viewer = viewer
        self.show_thoughts = show_thoughts
        self.user_data_dir = str(user_data_dir or (Path.home() / ".local/share/sable/scraper"))
        self.profile_name = profile_name or Path(self.user_data_dir).name

        self.chrome_process: subprocess.Popen | None = None
        self.chrome_log_file: str | None = None
        self.pw: Any = None
        self.browser: Any = None
        self.context: Any = None
        self.page: Any = None

        self.mutation_event = asyncio.Event()
        self.system_injected = False
        self.current_model_type = "default"
        self.has_fresh_chat = False

        self._last_thinking = ""
        self._last_response_text = ""
        self._sse_chunks: list[str] = []
        self._sse_done = asyncio.Event()
        self._sse_active_req_id: str | None = None

        self._prefix = re.sub(r"[^a-z0-9]", "", spec.provider.lower())[:6] or "scr"
        os.makedirs(Path(self.user_data_dir).parent, exist_ok=True)

    # -- logging ---------------------------------------------------------

    def _log(self, event: str, **fields: Any) -> None:
        logger.debug("[%s] %s %s", self.spec.provider, event, fields)

    # -- port / session probing -----------------------------------------

    async def is_port_open(self) -> bool:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                return s.connect_ex(("127.0.0.1", self.port)) == 0
        except Exception:
            return False

    async def _is_own_headed_session(self) -> bool:
        if not await self.is_port_open():
            return False
        try:
            url = f"http://127.0.0.1:{self.port}/json/version"
            req = urllib.request.Request(url, headers={"User-Agent": "Sable"})
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                data = json.loads(resp.read().decode())
            remote_profile = data.get("userDataDir", "") or data.get("profile-path", "")
            if remote_profile and os.path.realpath(remote_profile) != os.path.realpath(self.user_data_dir):
                return False
            if "--headless" in (data.get("BrowserCommandLine", "") or ""):
                return False
            return True
        except Exception:
            return False

    # -- launch / connect ------------------------------------------------

    async def launch(self) -> None:
        if await self._is_own_headed_session():
            logger.info("[%s] adopting existing headed session on port %d", self.spec.provider, self.port)
            await self.connect()
            return

        # Kill any other browser already holding this profile so a fresh launch
        # can proceed. Chromium permits only one process per profile dir; a
        # previously-open profile browser (another connector, or a leftover
        # headed session) would otherwise win the SingletonLock and our launch
        # would silently fail. Only done when no own session was adopted above.
        if self.kill_existing:
            try:
                killed = purge_profile_browsers(
                    self.user_data_dir,
                    exclude_pid=self.chrome_process.pid if self.chrome_process else None,
                )
                if killed:
                    logger.info(
                        "[%s] killed %d browser(s) holding profile %s before launch: %s",
                        self.spec.provider, len(killed), self.profile_name, killed,
                    )
                    # Give the OS a moment to release the profile lock.
                    await asyncio.sleep(1.0)
            except Exception as exc:
                logger.warning("[%s] profile purge failed: %s", self.spec.provider, exc)

        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            try:
                p = os.path.join(self.user_data_dir, name)
                if os.path.exists(p) or os.path.islink(p):
                    os.remove(p)
            except Exception:
                pass

        from engine.platform_paths import resolve_browser_for_profile, find_best_chrome, extra_browser_args
        chrome_path = resolve_browser_for_profile(self.profile_name) or find_best_chrome()
        if not chrome_path:
            raise RuntimeError("No Chrome-compatible browser found")

        cmd = [
            chrome_path,
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={self.user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--ozone-platform=wayland",
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            *extra_browser_args(chrome_path),
        ]
        if not self.viewer:
            cmd += ["--headless=new", "--window-size=1920,1080"]

        from engine.process_utils import popen_kwargs
        self.chrome_log_file = os.path.join(self.user_data_dir, "browser.log")
        log_file = open(self.chrome_log_file, "a", encoding="utf-8")
        self.chrome_process = subprocess.Popen(
            cmd, stdout=log_file, stderr=subprocess.STDOUT, **popen_kwargs()
        )
        await self._wait_for_cdp_ready()
        await self.connect()

    async def _wait_for_cdp_ready(self, timeout: float = 30.0) -> None:
        deadline = time.time() + timeout
        version_url = f"http://127.0.0.1:{self.port}/json/version"
        while time.time() < deadline:
            if self.chrome_process and self.chrome_process.poll() is not None:
                break
            if await self.is_port_open():
                try:
                    req = urllib.request.Request(version_url, headers={"User-Agent": "Sable"})
                    with urllib.request.urlopen(req, timeout=1.0) as resp:
                        if resp.status == 200:
                            return
                except Exception:
                    pass
            await asyncio.sleep(0.5)
        raise RuntimeError(f"Browser did not expose CDP on port {self.port}")

    async def connect(self) -> None:
        from playwright.async_api import async_playwright
        self.pw = await async_playwright().start()
        last_err: Exception | None = None
        for _ in range(5):
            try:
                self.browser = await self.pw.chromium.connect_over_cdp(f"http://127.0.0.1:{self.port}")
                self.context = self.browser.contexts[0]
                self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
                await self._setup_page(self.page)
                if self.spec.stealth_mode:
                    asyncio.create_task(self._poll_mutations())
                else:
                    await self.page.expose_function("on_dom_mutation", lambda: self.mutation_event.set())
                if self.spec.url not in (self.page.url or ""):
                    await self.page.goto(self.spec.url, wait_until="domcontentloaded", timeout=60000)
                return
            except Exception as exc:
                last_err = exc
                await asyncio.sleep(2)
        raise RuntimeError(f"CDP connect failed after 5 attempts: {last_err}")

    async def _poll_mutations(self) -> None:
        while True:
            try:
                muts = await self.page.evaluate("window.__ghost_mutations || []")
                if muts:
                    await self.page.evaluate("window.__ghost_mutations.length = 0;")
                    self.mutation_event.set()
            except Exception:
                pass
            await asyncio.sleep(0.5)

    # -- page setup ------------------------------------------------------

    async def _setup_page(self, page: Any) -> None:
        try:
            try:
                await page.emulate_media(color_scheme="dark")
            except Exception:
                pass
            for pattern in [
                "**/*analytics*", "**/*telemetry*", "**/gtm.js*",
                "**/feedback.js*", "**/play.google.com/log*", "**/collect?*",
            ]:
                await page.route(pattern, lambda route: route.abort())

            if self.spec.sse_target:
                await self._install_sse_interceptor(page)

            await page.add_init_script(
                "const injectGhostStyles=()=>{if(document.getElementById('ghost-engine-styles'))"
                "return;const s=document.createElement('style');s.id='ghost-engine-styles';"
                "s.textContent=`" + _GHOST_CSS + "`;(document.head||document.documentElement).appendChild(s);};"
                "injectGhostStyles();window.addEventListener('DOMContentLoaded',injectGhostStyles);"
            )
        except Exception as exc:
            self._log("setup_page_failed", error=str(exc))

    async def _install_sse_interceptor(self, page: Any) -> None:
        """Patch window.fetch to tee SSE chunks from the target endpoint into Python."""
        chunk_fn = f"__{self._prefix}_on_sse_chunk"
        done_fn = f"__{self._prefix}_on_sse_done"
        await page.expose_function(chunk_fn, lambda chunk, req_id: self._on_sse_chunk(chunk, req_id))
        await page.expose_function(done_fn, lambda req_id: self._on_sse_done(req_id))

        js = _FETCH_PATCH_JS.replace("__TARGET__", self.spec.sse_target or "") \
                            .replace("__CHUNK_FN__", chunk_fn) \
                            .replace("__DONE_FN__", done_fn)
        await page.add_init_script(js)
        try:
            result = await page.evaluate(js)
            self._log("sse_patch", result=result)
        except Exception as exc:
            self._log("sse_patch_failed", error=str(exc))

    async def _ensure_fetch_patched(self) -> None:
        if not self.spec.sse_target:
            return
        try:
            chunk_fn = f"__{self._prefix}_on_sse_chunk"
            done_fn = f"__{self._prefix}_on_sse_done"
            js = _FETCH_PATCH_JS.replace("__TARGET__", self.spec.sse_target) \
                                .replace("__CHUNK_FN__", chunk_fn) \
                                .replace("__DONE_FN__", done_fn)
            await self.page.evaluate(js)
        except Exception as exc:
            self._log("sse_repatch_failed", error=str(exc))

    def _on_sse_chunk(self, chunk: str, req_id: str) -> None:
        self._sse_active_req_id = req_id
        self._sse_chunks.append(chunk)

    def _on_sse_done(self, req_id: str) -> None:
        self._sse_done.set()

    def _reset_sse_capture(self) -> None:
        self._sse_chunks.clear()
        self._sse_done.clear()
        self._sse_active_req_id = None

    def parse_sse_delta(self, raw_text: str) -> tuple[str, str]:
        """Parse one SSE chunk into (content_delta, reasoning_delta).

        Default returns nothing — providers that carry reasoning override this.
        """
        return "", ""

    # -- input -----------------------------------------------------------

    async def _find_input_field(self) -> Any | None:
        provider_sel = self.spec.sel("input")
        candidates = ([provider_sel] if provider_sel else []) + _GENERIC_INPUT_SELECTORS
        for sel in candidates:
            try:
                await self.page.wait_for_selector(sel, timeout=5000)
                return self.page.locator(sel).last
            except Exception:
                continue
        return None

    async def _paste_large_message(self, field: Any, message: str) -> bool:
        try:
            handle = await field.element_handle()
            await self.page.evaluate(
                """({element, msg}) => {
                    if (!element) return;
                    if (element.isContentEditable) {
                        element.textContent = msg;
                    } else {
                        const proto = element.tagName === 'INPUT'
                            ? window.HTMLInputElement.prototype
                            : window.HTMLTextAreaElement.prototype;
                        const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
                        if (setter) setter.call(element, msg); else element.value = msg;
                    }
                    element.dispatchEvent(new Event('input', { bubbles: true }));
                    element.dispatchEvent(new Event('change', { bubbles: true }));
                }""",
                {"element": handle, "msg": message},
            )
            await asyncio.sleep(0.3)
            await field.focus()
            await self.page.keyboard.press("End")
            await self.page.keyboard.type(" ")
            await self.page.keyboard.press("Backspace")
            try:
                val = await field.input_value()
                return bool(val) and len(val) >= len(message) * 0.8
            except Exception:
                return True
        except Exception:
            return False

    async def _wait_for_send_enabled(self, timeout: float = 10.0) -> bool:
        sel = self.spec.sel("send_btn")
        if not sel:
            return True
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                btn = self.page.locator(sel).first
                if await btn.count() > 0:
                    classes = (await btn.get_attribute("class") or "").lower()
                    disabled = await btn.get_attribute("aria-disabled")
                    if "disabled" not in classes and disabled != "true":
                        return True
            except Exception:
                pass
            await asyncio.sleep(0.4)
        return False

    # -- chat lifecycle --------------------------------------------------

    async def new_chat(self, reapply_model: bool = True) -> None:
        self.system_injected = False
        try:
            btn = self.page.locator('[aria-label="New chat"]').first
            if await btn.is_visible(timeout=3000):
                await btn.click()
                await asyncio.sleep(2)
                self.has_fresh_chat = True
                return
        except Exception:
            pass
        try:
            await self.page.goto(self.spec.url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(2)
        except Exception as exc:
            self._log("new_chat_failed", error=str(exc))
        self.has_fresh_chat = True

    async def resume_chat(self, url: str) -> bool:
        """Navigate to a stored chat URL so the upstream conversation continues.

        Returns True when the page ends up on (or already on) that URL. The
        system prompt is NOT re-injected on a resumed chat — the upstream
        session already carries it.
        """
        if not url:
            return False
        try:
            current = getattr(self.page, "url", "") or ""
            if current == url:
                self.system_injected = True
                self._log("resume_chat_already_there", url=url)
                return True
            await self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(2)
            self.system_injected = True   # instructions already in the upstream session
            self.has_fresh_chat = False
            self._log("resume_chat_ok", url=url)
            return True
        except Exception as exc:
            self._log("resume_chat_failed", url=url, error=str(exc))
            return False

    def session_id_from_url(self, url: str) -> str | None:
        """Extract the upstream session id from a chat URL.

        Providers override this. Returning None means "this URL carries no
        comparable session id", so ensure_chat_url() falls back to an exact
        URL comparison.
        """
        return None

    async def ensure_chat_url(self, url: str) -> bool:
        """Guarantee the page is on `url` (or the same session) before a send.

        Compares by upstream session id when the provider exposes one, else by
        exact URL. On mismatch it navigates via resume_chat() — which also sets
        system_injected=True so instructions are NOT re-injected on a resumed
        upstream conversation. Returns True when the page is (now) on target.
        """
        if not url:
            return False
        try:
            current = getattr(self.page, "url", "") or ""
        except Exception:
            current = ""
        if current == url:
            return True
        cur_sid = self.session_id_from_url(current)
        want_sid = self.session_id_from_url(url)
        if cur_sid and want_sid and cur_sid == want_sid:
            return True
        return await self.resume_chat(url)

    async def stop_generation(self) -> bool:
        sel = self.spec.sel("stop")
        if not sel:
            return False
        try:
            btn = self.page.locator(sel).first
            await btn.wait_for(state="visible", timeout=2000)
            await btn.click(timeout=1000)
            return True
        except Exception:
            return False

    async def set_thinking_mode(self, mode: str) -> None:
        """Toggle provider thinking. Override where a toggle exists."""
        return None

    async def upload_file(self, file_path: str, has_msg: bool = False) -> bool:
        file_path = os.path.abspath(file_path)
        # 1) Hidden <input type=file> — the common, fastest path.
        for selector in ["input[type='file']", "input[accept*='image']", "#file-input"]:
            try:
                await self.page.locator(selector).first.set_input_files(file_path, timeout=3000)
                await asyncio.sleep(3)
                await self.page.keyboard.press("Escape")
                return True
            except Exception:
                continue

        # 2) Provider attach button -> native file chooser.
        attach_sel = self.spec.sel("attach_btn")
        if attach_sel:
            try:
                async with self.page.expect_file_chooser(timeout=5000) as fc_info:
                    await self.page.locator(attach_sel).first.click()
                chooser = await fc_info.value
                await chooser.set_files(file_path)
                await asyncio.sleep(3)
                await self.page.keyboard.press("Escape")
                return True
            except Exception as exc:
                self._log("upload_via_attach_failed", error=str(exc))
        return False

    async def _send_hello_warmup(self) -> None:
        """Optional: seed a bare 'hello' so the first real turn chains cleanly.

        No-op by default; providers with instruction-heavy first turns override.
        """
        return None

    async def send_msg(self, message: str, raw: bool = False) -> bool:
        for attempt in range(3):
            try:
                if not raw and not self.system_injected:
                    await self._send_hello_warmup()

                field = await self._find_input_field()
                if field is None:
                    raise RuntimeError("No input field found")
                await asyncio.sleep(0.4)
                await field.click()

                if raw:
                    pass
                elif not self.system_injected:
                    instructions = self._load_instructions()
                    if instructions:
                        message = f"[SYSTEM INSTRUCTION]\n{instructions}\n\n{_TOOL_REMINDER}{message}"
                    else:
                        message = f"{_TOOL_REMINDER}{message}"
                    self.system_injected = True
                else:
                    message = f"{_TOOL_REMINDER}{message}"

                filled = await self._paste_large_message(field, message)
                if not filled:
                    await field.fill(message)

                await self._wait_for_send_enabled()
                await self._ensure_fetch_patched()
                self._reset_sse_capture()

                # Mark existing turns old so capture cleanly finds the new one.
                await self.page.evaluate(
                    "() => document.querySelectorAll('div.ds-message, div.ds-assistant-message-main-content')"
                    ".forEach(el => el.dataset.ghostOld = 'true')"
                )

                send_sel = self.spec.sel("send_btn")
                sent = False
                if send_sel:
                    try:
                        btn = self.page.locator(send_sel).first
                        classes = (await btn.get_attribute("class") or "").lower()
                        if "disabled" not in classes:
                            await btn.click()
                            sent = True
                    except Exception:
                        pass
                if not sent:
                    await self.page.keyboard.press(self.spec.keys.get("send", "Enter"))

                stop_sel = self.spec.sel("stop")
                if stop_sel:
                    try:
                        await self.page.wait_for_selector(stop_sel, state="visible", timeout=5000)
                    except Exception:
                        pass
                return True
            except Exception as exc:
                self._log("send_retry", attempt=attempt + 1, error=str(exc))
                if attempt < 2:
                    await asyncio.sleep(2)
        return False

    # -- response capture ------------------------------------------------

    async def get_response_count(self) -> int:
        sel = self.spec.sel("content")
        if not sel:
            return 0
        try:
            return len(await self.page.query_selector_all(sel) or [])
        except Exception:
            return 0

    async def get_response(
        self,
        initial_count: int = 0,
        live_display: Callable[[str], None] | None = None,
        thoughts_callback: Callable[[str], Any] | None = None,
        **kwargs: Any,
    ) -> str:
        """Network-first SSE capture with DOM fallback. Returns the final text.

        IMPORTANT: this method deliberately does NOT reset the SSE capture
        state. send_msg() already resets it immediately before clicking Send,
        and the gap between that click and this call is non-zero (the connector
        invokes this via asyncio.create_task). Resetting here would:

          1. wipe chunks that already arrived in that gap (lost beginning), and
          2. reset the fragment-type state mid-stream — so deltas belonging to a
             THINK fragment get routed as answer text.

        That second effect is exactly the 'thinking mixed into the real message'
        symptom, most visible on the first turn where the gap is largest.
        """
        stop_sel = self.spec.sel("stop")
        thinking_sel = self.spec.sel("thoughts")
        response_sel = self.spec.sel("content")
        last_thinking = self._last_thinking
        last_response = self._last_response_text

        net_content = ""
        net_reasoning = ""
        idx = 0
        cached_thinking = ""
        cached_response = ""

        stop_locator = self.page.locator(stop_sel) if stop_sel else None
        new_resp_sel = f"{response_sel}:not([data-ghost-old='true'])" if response_sel else ""

        # Phase 1 — wait for generation to start
        deadline = time.time() + 60
        while True:
            if stop_locator is not None and await stop_locator.is_visible():
                break
            if new_resp_sel and await self.page.locator(new_resp_sel).count() > 0:
                break
            if self._sse_chunks:
                break
            if time.time() > deadline:
                return ""
            await asyncio.sleep(0.3)

        # Phase 2 — stream loop
        while stop_locator is not None and await stop_locator.is_visible():
            while idx < len(self._sse_chunks):
                content_delta, reasoning_delta = self.parse_sse_delta(self._sse_chunks[idx])
                idx += 1
                if reasoning_delta:
                    net_reasoning += reasoning_delta
                    if thoughts_callback and self.show_thoughts:
                        await thoughts_callback(self._clean_thoughts_text(net_reasoning))
                if content_delta:
                    net_content += content_delta
                    if live_display:
                        live_display(self._clean_garbage(net_content))

            if thinking_sel and thoughts_callback and self.show_thoughts:
                try:
                    stream = await self.page.locator(thinking_sel).last.inner_text() \
                        if await self.page.locator(thinking_sel).count() > 0 else ""
                except Exception:
                    stream = ""
                if stream and stream != cached_thinking and stream != last_thinking:
                    cached_thinking = stream
                    await thoughts_callback(self._clean_thoughts_text(stream))

            if not net_content and new_resp_sel:
                try:
                    stream = await self.page.locator(new_resp_sel).last.inner_text() \
                        if await self.page.locator(new_resp_sel).count() > 0 else ""
                except Exception:
                    stream = ""
                stream = self._strip_ui(stream)
                if stream and stream != cached_response and stream != last_response:
                    cached_response = stream
                    if live_display:
                        live_display(self._clean_garbage(stream))

            await asyncio.sleep(0.3)

        # The stop button vanishing means the model finished *rendering*, but
        # the final XHR readyState=4 delta + done signal may still be in flight
        # across the JS->Python bridge. Wait briefly for it, then drain.
        try:
            await asyncio.wait_for(self._sse_done.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            pass
        await asyncio.sleep(0.4)  # let late bridge callbacks land

        # Drain trailing chunks (loop, not once — late chunks may still arrive).
        settle_deadline = time.time() + 1.5
        while True:
            while idx < len(self._sse_chunks):
                content_delta, reasoning_delta = self.parse_sse_delta(self._sse_chunks[idx])
                idx += 1
                net_content += content_delta
                net_reasoning += reasoning_delta
            if idx >= len(self._sse_chunks) or time.time() > settle_deadline:
                break
            await asyncio.sleep(0.15)

        # Flush any buffered partial final line. The SSE stream frequently ends
        # WITHOUT a trailing newline, so the last line sits in _sse_line_buf and
        # would otherwise be dropped — this is the usual cause of missing tail words.
        if self._sse_line_buf:
            content_delta, reasoning_delta = self.parse_sse_delta("\n")
            net_content += content_delta
            net_reasoning += reasoning_delta

        # Phase 3 — final capture
        if net_content:
            final = net_content
        else:
            try:
                loc = self.page.locator(new_resp_sel) if new_resp_sel else None
                final = await loc.last.inner_text() if loc and await loc.count() > 0 else ""
            except Exception:
                final = ""
            final = self._strip_ui(final)

        if thoughts_callback and self.show_thoughts:
            try:
                tsel = f"{thinking_sel}:not([data-ghost-old='true'])" if thinking_sel else ""
                tloc = self.page.locator(tsel) if tsel else None
                final_thinking = await tloc.last.inner_text() if tloc and await tloc.count() > 0 else ""
                if final_thinking:
                    await thoughts_callback(self._clean_thoughts_text(final_thinking))
            except Exception:
                pass

        cleaned = self._clean_garbage(final)
        if live_display and cleaned:
            live_display(cleaned)
        self._last_response_text = cleaned
        return cleaned

    # -- text cleaning ---------------------------------------------------

    def _strip_ui(self, text: str) -> str:
        text = re.sub(r"^text\n", "", text or "")
        prefix = "markdown\nCopy\nDownload\n"
        if text.startswith(prefix):
            text = text[len(prefix):]
        return text

    def _clean_garbage(self, text: str) -> str:
        if not text:
            return text
        text = re.sub(rf"(?i)^({_UI_GARBAGE_WORDS}\s*)+", "", text, flags=re.MULTILINE)
        text = re.sub(rf"(?i)({_UI_GARBAGE_WORDS}\s*)+$", "", text, flags=re.MULTILINE)
        text = re.sub(r"(?i)\n?\d+(\.\d+)?s\s*$", "", text)
        text = text.strip()
        half = len(text) // 2
        if len(text) > 20 and text[:half].strip() == text[half:].strip():
            text = text[:half].strip()
        return text.strip()

    def _clean_thoughts_text(self, text: str) -> str:
        if not text:
            return ""
        lines = [
            line.strip() for line in text.splitlines()
            if line.strip() and line.strip().lower() not in _THOUGHT_IGNORED_EXACT
        ]
        cleaned = "\n\n".join(lines)
        cleaned = re.sub(r"(?:🧠\s*Model Tho(?:ughts?)?)+", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        return cleaned.strip()

    def _load_instructions(self) -> str:
        try:
            from connectors.common.instruction_builder import build_instructions
            return build_instructions(provider=self.spec.instruction_provider or self.spec.provider)
        except Exception:
            return ""

    # -- teardown --------------------------------------------------------

    async def cleanup(self) -> None:
        try:
            if self.browser:
                await self.browser.close()
        except Exception:
            pass
        try:
            if self.pw:
                await self.pw.stop()
        except Exception:
            pass
        if self.chrome_process:
            try:
                from engine.process_utils import kill_process_tree
                kill_process_tree(self.chrome_process.pid)
            except Exception:
                pass


_FETCH_PATCH_JS = r"""
(() => {
    const TARGET = '__TARGET__';
    const CHUNK = '__CHUNK_FN__';
    const DONE = '__DONE_FN__';
    let did = false;

    // ---- 1. fetch patch (SPAs that stream via fetch) --------------------
    if (!(window.__ghost_fetch_patched && window.fetch.toString().includes('originalFetch'))) {
        window.__ghost_fetch_patched = true;
        const originalFetch = window.__fetch_original || window.fetch;
        window.__fetch_original = originalFetch;
        let chunkIndex = 0;
        window.fetch = async function(...args) {
            const url = typeof args[0] === 'string' ? args[0] : (args[0] && args[0].url) || '';
            const response = await originalFetch.apply(this, args);
            if (!url.includes(TARGET)) return response;
            try {
                const clone = response.clone();
                const reader = clone.body.getReader();
                const decoder = new TextDecoder();
                const reqId = 'sse_' + Date.now() + '_' + (++chunkIndex);
                (async () => {
                    try {
                        while (true) {
                            const { done, value } = await reader.read();
                            if (done) { window[DONE](reqId); break; }
                            window[CHUNK](decoder.decode(value, { stream: true }), reqId);
                        }
                    } catch (e) {
                        window[DONE](reqId);
                    }
                })();
            } catch (e) {}
            return response;
        };
        did = true;
    }

    // ---- 2. XHR patch (DeepSeek streams via XMLHttpRequest) -------------
    if (!window.__ghost_xhr_patched) {
        window.__ghost_xhr_patched = true;
        const origOpen = XMLHttpRequest.prototype.open;
        const origSend = XMLHttpRequest.prototype.send;
        XMLHttpRequest.prototype.open = function(method, url, ...rest) {
            this.__ghost_url = url || '';
            this.__ghost_idx = 0;
            this.__ghost_reqId = 'xhr_' + Date.now() + '_' + Math.random().toString(36).slice(2, 8);
            return origOpen.call(this, method, url, ...rest);
        };
        XMLHttpRequest.prototype.send = function(...args) {
            const xhr = this;
            if (xhr.__ghost_url && xhr.__ghost_url.includes(TARGET)) {
                xhr.addEventListener('readystatechange', function() {
                    // readyState 3 = LOADING (incremental), 4 = DONE
                    if (xhr.readyState !== 3 && xhr.readyState !== 4) return;
                    // responseText throws if responseType is not ''/'text'.
                    const rt = xhr.responseType;
                    if (rt && rt !== 'text' && rt !== '') return;
                    try {
                        const full = xhr.responseText || '';
                        const delta = full.slice(xhr.__ghost_idx);
                        if (delta) {
                            xhr.__ghost_idx = full.length;
                            window[CHUNK](delta, xhr.__ghost_reqId);
                        }
                        if (xhr.readyState === 4) {
                            window[DONE](xhr.__ghost_reqId);
                        }
                    } catch (e) {}
                });
            }
            return origSend.apply(this, args);
        };
        did = true;
    }

    return did ? 'patched' : 'already_active';
})()
"""
