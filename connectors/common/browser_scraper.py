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
    # Selector whose matching elements get tagged `data-ghost-old="true"` right
    # before Send, so capture scopes to THIS turn. MUST be provider-specific:
    # the generic default used to be a hardcoded DeepSeek selector list, which
    # matched nothing on claude.ai/qwen — leaving every previous turn unmarked,
    # so `:not([data-ghost-old='true'])` matched stale panels and `.last`
    # returned the PREVIOUS answer.
    old_mark_selector: str | None = None
    # Rate-limit / soft-error banners rendered as TRANSCRIPT CHILDREN.
    #
    # DeepSeek does NOT use a toast or modal for "Messages too frequent.
    # Try again later." — it appends a bare <span> as a SIBLING of the
    # assistant ``.ds-message`` bubble. Consequences when undetected:
    #   * the assistant content element is empty  → net_content stays ""
    #   * no SSE frame is ever emitted           → nothing to parse
    #   * the turn is classified as an EMPTY RESPONSE and retried/rotated
    # That is the "Sable repeats the same reply forever" symptom: a rate
    # limit silently masquerades as an empty completion. Matching the banner
    # text here lets the engine raise a real rate_limited event instead.
    error_text_markers: tuple[str, ...] = ()

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


class SoftRateLimitError(RuntimeError):
    """Upstream rejected the turn with a soft rate-limit banner.

    Raised by :meth:`BaseBrowserEngine.get_response` when the provider renders
    a "too many messages / try again later" notice as a transcript child
    instead of returning an answer. Distinguished from a generic failure so
    the scraper stream can emit a ``rate_limited`` event (which triggers
    account auto-switch) rather than retrying an empty completion forever.
    """

    def __init__(self, message: str, provider: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.provider = provider

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
    # "[reminder]\n"
    # "tool calling format — every tool call MUST be a single JSON array "
    # "wrapped in an <action> block:\n"
    # '<action>[{"name": "<function-name>", "arguments": { ... }}]</action>\n'
    # "Multiple calls go in the SAME array. Never emit DSML. Keep prose before "
    # "it to one short sentence. Place it at the END of the response.\n"
    # "[user message]\n"
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


def _pid_is_running(pid: int) -> bool:
    """True only if `pid` is a live, non-zombie process.

    ``os.kill(pid, 0)`` returns success for zombies, so it is NOT a liveness
    test. We inspect /proc/<pid>/stat state instead and reject 'Z' (zombie)
    and 'X' (dead).
    """
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            data = fh.read()
        # state is the field right after the comm parenthesised name
        rparen = data.rfind(b")")
        if rparen == -1:
            return False
        state = data[rparen + 2:rparen + 3].decode("ascii", "ignore")
        return state not in ("Z", "X", "x")
    except (OSError, ValueError):
        return False


def _singleton_pid(profile_dir: str) -> int | None:
    """PID recorded in the profile's SingletonLock symlink, if any.

    Returns None when the recorded pid is a zombie or already gone — a stale
    symlink must never be allowed to seed a kill.
    """
    lock = Path(profile_dir) / "SingletonLock"
    if not (lock.is_symlink() or lock.exists()):
        return None
    try:
        _, _, pid_str = os.readlink(lock).rpartition("-")
        pid = int(pid_str)
    except Exception:
        return None
    if not _pid_is_running(pid):
        return None
    return pid


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
        # A zombie still answers os.kill(pid, 0) but getpgid() can return a
        # group that contains LIVE siblings — killpg would then nuke healthy
        # browsers. Verify the pid is actually running before touching it.
        if not _pid_is_running(pid):
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
        adopt_from: "BaseBrowserEngine | None" = None,
    ) -> None:
        self.spec = spec
        # When set, this engine does NOT launch its own browser. Instead it
        # attaches to `adopt_from`'s already-running CDP port and opens a NEW
        # tab (page) in that same browser/context. Used for critique/agent
        # sub-chats that must share the parent's logged-in profile without
        # fighting over the parent's page or spawning a second browser on the
        # same (lock-exclusive) user-data-dir.
        self._adopt_from = adopt_from
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
        self._slot: Any = None   # browser_governor.Slot, when admitted
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
        # Did the last turn stream ANY reasoning? A thought-only turn (the
        # model emits a chain of thought then stops with no answer text) is a
        # live, healthy browser — the connector reads this to retry on the
        # SAME browser instead of killing and relaunching it.
        self._last_reasoning_seen = False
        self._sse_chunks: list[str] = []
        self._sse_done = asyncio.Event()
        # Set by _on_sse_chunk, awaited by the capture loops. Without it the
        # loops can only discover new chunks on their 0.3s poll tick, which is
        # the single biggest source of perceived streaming lag versus a direct
        # socket read (curl_cffi has no such floor).
        self._sse_wake = asyncio.Event()
        self._sse_active_req_id: str | None = None
        # Set by a subclass parser when the stream carries an explicit terminal
        # marker (Qwen: {"status":"finished"}). Lets the capture loop end on
        # the network signal instead of relying solely on the stop button,
        # which some SPAs never paint for very fast turns.
        self._stream_finished = False

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
        """True if a live browser on our port is ours AND matches our launch mode.

        The mode check matters in both directions: a headed engine must not
        adopt a leftover headless browser (no window to drive), and a headless
        engine must not adopt a leftover headed one. Without the second half,
        headless mode killed and relaunched the browser on every single turn.
        """
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
            remote_headless = "--headless" in (data.get("BrowserCommandLine", "") or "")
            return remote_headless != bool(self.viewer)
        except Exception:
            return False

    # -- launch / connect ------------------------------------------------

    async def launch(self) -> None:
        # Adopted sub-engine: reuse the parent's live browser process + context,
        # open a fresh tab for this stream. No new browser, no profile lock war,
        # no governor slot.
        if self._adopt_from is not None:
            await self._connect_adopted()
            return

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

        from engine.platform_paths import (
            resolve_browser_for_profile, find_best_chrome, extra_browser_args,
            get_account_ua_fingerprint,
        )
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
            # Headless Chromium advertises 'HeadlessChrome/<ver>' in its UA, and
            # bot-aware CDNs (DeepSeek/CloudFront) 403 the request before the SPA
            # ever loads. Launch with a headed UA derived from the profile's
            # browser so both the request header AND navigator.userAgent look
            # like a normal desktop Chrome. Mirrors engine/scraper/core.py.
            try:
                _ua, _ = get_account_ua_fingerprint(self.profile_name)
                if _ua:
                    cmd.append(f"--user-agent={_ua}")
            except Exception as exc:
                logger.warning("[%s] UA override failed: %s", self.spec.provider, exc)

        from engine.process_utils import popen_kwargs
        from engine import browser_governor

        # Reserve a fleet-wide slot BEFORE spawning. At most MAX_BROWSERS exist
        # across every Sable process; if all are busy this waits instead of
        # piling on another Chromium.
        # CHAT priority: a scraper session is a live chat connection and is
        # never evicted to make room for a token-refresh browser.
        slot = await browser_governor.acquire(
            f"{self.spec.provider}:{self.profile_name}",
            priority=browser_governor.PRIORITY_CHAT,
        )

        self.chrome_log_file = os.path.join(self.user_data_dir, "browser.log")
        log_file = open(self.chrome_log_file, "a", encoding="utf-8")
        try:
            self.chrome_process = subprocess.Popen(
                cmd, stdout=log_file, stderr=subprocess.STDOUT, **popen_kwargs()
            )
        except BaseException:
            slot.release()
            raise

        # Bind the pid to the slot and move it into a hard memory cgroup.
        slot.attach(self.chrome_process.pid, user_data_dir=self.user_data_dir)
        self._slot = slot

        try:
            await self._wait_for_cdp_ready()
            await self.connect()
        except BaseException:
            # Never leave a slot held by a browser that failed to come up.
            slot.release(kill=True)
            self._slot = None
            raise

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

    async def _connect_adopted(self) -> None:
        """Attach to the parent engine's live browser and open a NEW tab.

        Shares the parent's Chromium process + context (same cookies, same
        login, same user-data-dir), but binds ``self.page`` to a freshly
        created tab so this stream never touches the parent's page. The
        parent keeps owning the browser process; this engine never launches
        or cleans up a browser (see :meth:`cleanup`).
        """
        parent = self._adopt_from
        if parent is None:
            raise RuntimeError("adopt_from engine is gone")
        # Reuse the parent's Playwright driver + browser if it is already up;
        # otherwise wait briefly for the parent to finish connecting.
        for _ in range(20):
            if getattr(parent, "browser", None) is not None and getattr(parent, "context", None) is not None:
                break
            await asyncio.sleep(0.5)
        self.pw = getattr(parent, "pw", None)
        self.browser = getattr(parent, "browser", None)
        self.context = getattr(parent, "context", None)
        if self.browser is None or self.context is None:
            raise RuntimeError("parent browser not ready for adoption")
        self.port = parent.port
        # New tab in the SAME context — inherits login/cookies.
        self.page = await self.context.new_page()
        await self._setup_page(self.page)
        if self.spec.stealth_mode:
            try:
                await self.page.expose_function("on_dom_mutation", lambda: self.mutation_event.set())
            except Exception:
                pass
        if self.spec.url not in (self.page.url or ""):
            await self.page.goto(self.spec.url, wait_until="domcontentloaded", timeout=60000)
        logger.info("[%s] adopted parent browser on port %d, opened new tab", self.spec.provider, self.port)

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
        # Wake the capture loop immediately — do not wait for its poll tick.
        self._sse_wake.set()

    def _on_sse_done(self, req_id: str) -> None:
        self._sse_done.set()
        self._sse_wake.set()

    async def _wait_for_sse_activity(self, timeout: float = 0.3) -> None:
        """Block until a chunk arrives, or *timeout* elapses.

        Replaces a blind sleep in the capture loops so a delta is rendered the
        moment the CDP binding fires instead of on the next poll tick. The
        timeout stays as a floor because the stop button and the DOM fallback
        still need periodic attention.
        """
        try:
            await asyncio.wait_for(self._sse_wake.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        self._sse_wake.clear()

    def _reset_sse_capture(self) -> None:
        # REBIND, never .clear(). A capture task from a previous (stopped) turn
        # may still hold a reference to the old list. Rebinding freezes that
        # old list, so the orphan can only ever see its own turn's chunks and
        # can never steal the NEXT turn's deltas.
        self._sse_chunks = []
        self._sse_done.clear()
        self._sse_wake.clear()
        self._sse_active_req_id = None
        self._stream_finished = False

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

    async def _wait_for_send_enabled(self, timeout: float = 3.0) -> bool:
        """Wait until the send button is genuinely actionable.

        Claude's composer is Tailwind-based: the class attribute permanently
        carries variant tokens like `disabled:opacity-40
        disabled:pointer-events-none`. A substring test on `class` therefore
        NEVER passes, and the old loop burned its entire 10s timeout on every
        send before falling through to the Enter-key path. Ask the DOM for the
        real enabled state instead — the `disabled` property, `aria-disabled`,
        and computed pointer-events are what actually gate the click.
        """
        sel = self.spec.sel("send_btn")
        if not sel:
            return True
        try:
            await self.page.locator(sel).first.wait_for(
                state="visible", timeout=timeout * 1000
            )
            await self.page.wait_for_function(
                """(sel) => {
                    const b = document.querySelector(sel);
                    return !!b && !b.disabled
                        && b.getAttribute('aria-disabled') !== 'true'
                        && getComputedStyle(b).pointerEvents !== 'none';
                }""",
                arg=sel,
                timeout=timeout * 1000,
            )
            return True
        except Exception:
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

                send_ready = await self._wait_for_send_enabled()
                await self._ensure_fetch_patched()
                self._reset_sse_capture()

                # Mark existing turns old so capture cleanly finds the new one.
                # The reasoning panels MUST be included: the capture reads them
                # through `:not([data-ghost-old='true'])`, so an unmarked panel
                # from a previous turn matches the filter, `.last` returns that
                # stale text, and it is emitted as this turn's thinking.
                _mark_parts: list[str] = []
                for _key in ("response", "content", "thoughts"):
                    _s = self.spec.sel(_key)
                    if _s and _s not in _mark_parts:
                        _mark_parts.append(_s)
                if self.spec.old_mark_selector:
                    _mark_parts.append(self.spec.old_mark_selector)
                if _mark_parts:
                    await self.page.evaluate(
                        "(sel) => document.querySelectorAll(sel)"
                        ".forEach(el => el.dataset.ghostOld = 'true')",
                        ", ".join(_mark_parts),
                    )

                send_sel = self.spec.sel("send_btn")
                sent = False
                if send_sel and send_ready:
                    try:
                        await self.page.locator(send_sel).first.click(timeout=2000)
                        sent = True
                    except Exception as exc:
                        self._log("send_click_failed", error=str(exc))
                if not sent:
                    # Button never became actionable (or the click was
                    # swallowed) — Enter is the composer's own submit path.
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
        # Pin the chunk list to THIS generation. send_msg() rebinds
        # self._sse_chunks right before clicking Send; anything appended by an
        # orphaned capture from a previous turn lands in the OLD list.
        chunk_list = self._sse_chunks

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
        new_think_sel = f"{thinking_sel}:not([data-ghost-old='true'])" if thinking_sel else ""
        while True:
            if stop_locator is not None and await stop_locator.is_visible():
                break
            if new_resp_sel and await self.page.locator(new_resp_sel).count() > 0:
                break
            # Reasoning can render before any answer text; without this the
            # loop sits on the 0.3s tick and the first thinking chunks are
            # missed until the answer element appears.
            if new_think_sel and await self.page.locator(new_think_sel).count() > 0:
                break
            if chunk_list:
                break
            if time.time() > deadline:
                return ""
            # A soft rate-limit banner can appear INSTEAD of any generation.
            # Without this probe the loop burns its full 60s deadline, then
            # returns "" — indistinguishable from a genuine empty completion.
            if await self.detect_soft_error():
                break
            await asyncio.sleep(0.3)

        # Phase 2 — stream loop. Completion is driven by the NETWORK first
        # (explicit terminal marker, stream-close signal, or idle), with the
        # stop button as a SECONDARY signal. Gating the loop purely on the stop
        # button silently skipped all live streaming whenever that selector did
        # not match — the answer then only appeared at the very end via the DOM
        # fallback, which is exactly the 'sluggish and broken' symptom.
        # `streamed_reasoning` tracks whether ANY reasoning reached the callback
        # this turn. It gates the DOM fallback + the final re-emit so the two
        # sources (network SSE and the rendered panel) never interleave.
        streamed_reasoning = False

        async def _drain() -> None:
            """Consume every unconsumed chunk and update the live views."""
            nonlocal net_content, net_reasoning, idx, streamed_reasoning
            while idx < len(chunk_list):
                content_delta, reasoning_delta = self.parse_sse_delta(chunk_list[idx])
                idx += 1
                if reasoning_delta:
                    net_reasoning += reasoning_delta
                    streamed_reasoning = True
                    if thoughts_callback and self.show_thoughts:
                        # RAW while streaming — never _clean_thoughts_text here.
                        # That helper rebuilds the whole line list and re-joins
                        # with blank lines, so its return value is NOT a
                        # prefix-extension of the previous tick's value. The
                        # connector converts each callback into a suffix delta by
                        # common-prefix diff; a non-monotonic value makes that
                        # diff return "" — chunks of thinking silently vanish.
                        await thoughts_callback(net_reasoning)
                if content_delta:
                    net_content += content_delta
                    if live_display:
                        # Cheap prefix strip only while streaming; the full
                        # clean runs once at the end (O(n^2) note above).
                        live_display(self._strip_ui(net_content))

        generation_active = False
        if stop_locator is not None:
            try:
                generation_active = await stop_locator.is_visible()
            except Exception:
                generation_active = False
        last_progress = time.time()
        idle_limit = 30.0
        hard_deadline = time.time() + 300.0
        prev_idx = -1
        while True:
            await _drain()

            if idx != prev_idx:
                last_progress = time.time()
                prev_idx = idx

            # Network completion signals.
            if self._stream_finished:
                break
            if self._sse_done.is_set() and idx >= len(chunk_list):
                break

            # Stop-button SECONDARY signal: once we have seen it active, its
            # disappearance means the model finished. If it never appeared we
            # must NOT treat that as completion (that was the original bug).
            if stop_locator is not None:
                try:
                    vis = await stop_locator.is_visible()
                except Exception:
                    vis = generation_active
                if vis:
                    generation_active = True
                elif generation_active:
                    break

            if (time.time() - last_progress) > idle_limit:
                self._log("capture_idle_timeout", secs=idle_limit)
                break
            if time.time() > hard_deadline:
                self._log("capture_hard_timeout")
                break

            # DOM reasoning fallback — ONLY while the network produced none.
            if thinking_sel and thoughts_callback and self.show_thoughts and not streamed_reasoning:
                stream = await self._read_thoughts_dom(thinking_sel)
                if stream and stream != cached_thinking and stream != last_thinking:
                    cached_thinking = stream
                    streamed_reasoning = True
                    await thoughts_callback(stream)

            # DOM answer fallback — ONLY while the network produced none.
            if not net_content and new_resp_sel:
                stream = await self._read_response_dom(response_sel)
                stream = self._strip_ui(stream)
                if stream and stream != cached_response and stream != last_response:
                    cached_response = stream
                    if live_display:
                        live_display(self._strip_ui(stream))

            await self._wait_for_sse_activity(0.3)

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
            while idx < len(chunk_list):
                content_delta, reasoning_delta = self.parse_sse_delta(chunk_list[idx])
                idx += 1
                net_content += content_delta
                net_reasoning += reasoning_delta
                if reasoning_delta and thoughts_callback and self.show_thoughts:
                    streamed_reasoning = True
                    await thoughts_callback(net_reasoning)
                if content_delta and live_display:
                    live_display(self._strip_ui(net_content))
            if idx >= len(chunk_list) or time.time() > settle_deadline:
                break
            await asyncio.sleep(0.15)

        # If the stop button vanished before ANY content landed, the send may
        # have been coalesced into the previous turn (or the SPA answered so
        # fast the button never painted). The network is authoritative, so give
        # it one more bounded window before concluding the turn is empty. This
        # is the difference between a spurious CAPTURE_EMPTY error and a real
        # answer, on fast models where the whole turn fits between two polls.
        if not net_content and not net_reasoning:
            extra_deadline = time.time() + 6.0
            while time.time() < extra_deadline:
                progressed = False
                while idx < len(chunk_list):
                    content_delta, reasoning_delta = self.parse_sse_delta(chunk_list[idx])
                    idx += 1
                    net_content += content_delta
                    net_reasoning += reasoning_delta
                    progressed = True
                    if reasoning_delta and thoughts_callback and self.show_thoughts:
                        streamed_reasoning = True
                        await thoughts_callback(net_reasoning)
                    if content_delta and live_display:
                        live_display(self._strip_ui(net_content))
                if net_content:
                    break
                if not progressed:
                    await self._wait_for_sse_activity(0.3)

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

        # Final reasoning: only emit when NOTHING streamed this turn. Re-emitting
        # the (reformatted) full text after a raw stream would arrive at the
        # connector as a non-prefix value and either vanish or duplicate.
        dom_thinking = ""
        if thoughts_callback and self.show_thoughts and not streamed_reasoning:
            try:
                dom_thinking = await self._read_thoughts_dom(thinking_sel) if thinking_sel else ""
                final_thinking = dom_thinking or net_reasoning
                if final_thinking and final_thinking != cached_thinking and final_thinking != last_thinking:
                    await thoughts_callback(self._clean_thoughts_text(final_thinking))
            except Exception:
                pass

        cleaned = self._clean_garbage(final)
        if live_display and cleaned:
            live_display(cleaned)

        # Empty capture → is it "said nothing" or "refused to say anything"?
        # Only the latter is retryable-by-account-switch; the former must stay
        # an empty response. Raising here (rather than returning "") is what
        # stops the 3x-empty-retry loop from silently hammering a locked
        # endpoint on every account.
        if not cleaned:
            soft = await self.detect_soft_error()
            if soft:
                self._log("soft_rate_limit", text=soft[:160])
                raise SoftRateLimitError(soft, provider=getattr(self.spec, "provider", ""))
        # Remember this turn's output so the NEXT turn's `!= last_thinking` /
        # `!= last_response` guards have a real baseline. `_last_thinking` was
        # initialised once and never updated, so a turn that produced no fresh
        # reasoning fell straight through to `last_thinking == ""`, matched the
        # previous turn's panel, and re-emitted it as this turn's thinking.
        self._last_response_text = cleaned
        _prev_think = cached_thinking or net_reasoning
        if _prev_think:
            self._last_thinking = _prev_think
        # A reasoning-only turn is NOT a dead browser: the page streamed a
        # chain of thought and simply produced no answer text. Flag it so the
        # connector retries on this same live browser instead of tearing it
        # down (kill + relaunch + re-navigate) for nothing.
        #
        # dom_thinking matters on a RE-CAPTURE: the SSE buffer was reset and
        # the network produced nothing, yet this turn's reasoning panel is
        # still in the DOM. Without folding it in, the flag flips back to False
        # on the retry and the connector tears down the very browser it just
        # decided to keep.
        self._last_reasoning_seen = bool(_prev_think or dom_thinking)
        return cleaned

    # -- text cleaning ---------------------------------------------------

    async def _read_thoughts_dom(self, thinking_sel: str) -> str:
        """Full reasoning text from THIS turn's panel.

        Reads ``textContent`` (not ``inner_text``) so a collapsed or
        height-clamped panel still yields the complete chain of thought instead
        of the truncated on-screen preview.
        """
        if not thinking_sel:
            return ""
        sel = f"{thinking_sel}:not([data-ghost-old='true'])"
        js = """(sel) => {
            const els = document.querySelectorAll(sel);
            if (!els.length) return '';
            const el = els[els.length - 1];
            const clone = el.cloneNode(true);
            clone.querySelectorAll('button, .ds-icon-button').forEach(n => n.remove());
            clone.querySelectorAll('br').forEach(br => br.replaceWith('\n'));
            return (clone.textContent || '').trim();
        }"""
        try:
            return await self.page.evaluate(js, sel) or ""
        except Exception:
            return ""

    async def detect_soft_error(self) -> str:
        """Return the rate-limit / soft-error banner text, or "".

        Providers that render refusals as transcript children (DeepSeek's
        "Messages too frequent. Try again later.") leave the assistant bubble
        empty and emit no SSE. Scanning the markers here is what separates
        "upstream said no" from "upstream said nothing" — the two used to be
        indistinguishable, so a rate limit was retried as an empty response.

        Scoped to the LAST few transcript entries so an old banner from a
        previous turn can never poison a healthy one.
        """
        markers = tuple(getattr(self.spec, "error_text_markers", ()) or ())
        if not markers:
            return ""
        js = """(markers) => {
            const bubbles = [...document.querySelectorAll('.ds-message')];
            if (!bubbles.length) return '';
            // Walk siblings AFTER the last assistant bubble only.
            const last = bubbles[bubbles.length - 1];
            const container = last.parentElement;
            if (!container) return '';
            const kids = [...container.children];
            const start = kids.indexOf(last);
            for (const node of kids.slice(start)) {
                const t = (node.innerText || node.textContent || '').trim();
                if (!t || t.length > 300) continue;
                for (const m of markers) {
                    if (t.toLowerCase().includes(m.toLowerCase())) return t;
                }
            }
            return '';
        }"""
        try:
            return (await self.page.evaluate(js, list(markers))) or ""
        except Exception as exc:
            self._log("soft_error_probe_failed", error=str(exc))
            return ""

    async def _read_response_dom(self, response_sel: str) -> str:
        """Answer text from THIS turn's message block, reasoning panel removed.

        The DeepSeek reasoning panel is nested inside the assistant message
        block, so a naive ``inner_text()`` on the content element returns the
        chain of thought glued to the answer — the 'thinking rendered as the
        model response' symptom whenever the network path is empty.
        """
        if not response_sel:
            return ""
        sel = f"{response_sel}:not([data-ghost-old='true'])"
        js = """(sel) => {
            const els = document.querySelectorAll(sel);
            if (!els.length) return '';
            const el = els[els.length - 1];
            const clone = el.cloneNode(true);
            clone.querySelectorAll(
                '.ds-think-content, [class*="ds-think"], [class*="think-content"]'
            ).forEach(n => n.remove());
            clone.querySelectorAll('button, .ds-icon-button').forEach(n => n.remove());
            return (clone.innerText || clone.textContent || '').trim();
        }"""
        try:
            return await self.page.evaluate(js, sel) or ""
        except Exception:
            return ""

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
        # A caller-supplied directive (study/agent) wins — it is the FULL
        # instruction for this stream and must be injected exactly once.
        override = getattr(self, "_override_instructions", None)
        if override:
            return override
        try:
            from connectors.common.instruction_builder import build_instructions
            return build_instructions(
                provider=self.spec.instruction_provider or self.spec.provider,
                layout_mode=getattr(self, "_layout_mode", None),
            )
        except Exception:
            return ""

    # -- teardown --------------------------------------------------------

    async def cleanup(self) -> None:
        # Adopted sub-engine: the browser + context belong to the parent, so we
        # only close OUR tab and detach. Closing the shared browser here would
        # tear down the parent chat's session.
        if self._adopt_from is not None:
            try:
                if self.page is not None and not self.page.is_closed():
                    await self.page.close()
            except Exception:
                pass
            self.page = None
            self.browser = None
            self.context = None
            self.pw = None
            return
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
        # Return the fleet slot so another browser can start, and drop the
        # per-browser cgroup. No-op when no slot was ever claimed.
        if self._slot is not None:
            try:
                self._slot.release(kill=False)
            except Exception:
                pass
            self._slot = None


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
