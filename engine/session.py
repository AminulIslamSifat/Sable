"""Qwen Session & Authentication Manager — handles headers, WAF tokens, and server session creation."""

import asyncio
import logging
import os
import time
import uuid
import base64
import json
from datetime import datetime
from pathlib import Path
from typing import Any


from engine import process_utils
from engine.config import COOKIES, BX_UA, BX_UMIDTOKEN, NEW_CHAT_URL, get_model_config
from engine.platform_paths import pid_exists

logger = logging.getLogger("sable.session")


def _get_user_name() -> str:
    """Get display name from config (env-overridable)."""
    from engine.config import USER_NAME
    return USER_NAME


# Persistent Playwright launch counter — survives restarts.
_LAUNCH_COUNTER_PATH = Path(__file__).resolve().parent.parent / "system" / ".playwright_launches"


def _increment_playwright_counter() -> int:
    """Atomically increment and return the Playwright launch counter."""
    try:
        count = int(_LAUNCH_COUNTER_PATH.read_text().strip()) if _LAUNCH_COUNTER_PATH.exists() else 0
    except (ValueError, OSError):
        count = 0
    count += 1
    try:
        _LAUNCH_COUNTER_PATH.write_text(str(count))
    except OSError:
        pass
    return count

def _pid_is_headless(pid: int) -> bool | None:
    '''True = headless, False = headed, None = cmdline unreadable.'''
    try:
        raw = Path('/proc/%d/cmdline' % pid).read_bytes()
    except OSError:
        return None
    toks = raw.replace(bytes([0]), b' ').decode('utf-8', 'replace').split()
    if not toks:
        return None
    for tok in toks:
        if tok == '--headless' or tok.startswith('--headless='):
            return True
        if tok.startswith('--ozone-platform=headless'):
            return True
    return False


def _display_available(display: str | None = None) -> bool:
    """True if an X display socket exists for the given DISPLAY value."""
    disp = display or os.environ.get("DISPLAY")
    if not disp:
        return False
    # DISPLAY forms: ":1" or "host:1" -> socket /tmp/.X11-unix/X1
    num = disp.rsplit(":", 1)[-1].split(".", 1)[0]
    if not num.isdigit():
        return False
    return Path(f"/tmp/.X11-unix/X{num}").exists()


# Xvfb process started by this module (kept alive for the headed browser).
_XVFB_PROC: Any = None
_XVFB_DISPLAY: str | None = None


def _ensure_virtual_display() -> str | None:
    """Return a DISPLAY that a headed Chromium can use, starting Xvfb if needed.

    The server often runs with no DISPLAY (systemd/ssh). Headed Chromium aborts
    without one, so we spawn a private Xvfb once and reuse it.
    """
    global _XVFB_PROC, _XVFB_DISPLAY
    if _display_available():
        return os.environ.get("DISPLAY")
    if _XVFB_DISPLAY and _display_available(_XVFB_DISPLAY):
        return _XVFB_DISPLAY
    import shutil as _shutil
    import subprocess as _subprocess
    xvfb = _shutil.which("Xvfb")
    if not xvfb:
        return None
    # Pick a free display number by probing sockets 99..110
    for num in range(99, 111):
        if Path(f"/tmp/.X11-unix/X{num}").exists():
            continue
        try:
            proc = _subprocess.Popen(
                [xvfb, f":{num}", "-screen", "0", "1280x1024x24", "-nolisten", "tcp"],
                stdout=_subprocess.DEVNULL,
                stderr=_subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception:
            return None
        time.sleep(0.8)
        if _display_available(f":{num}"):
            _XVFB_PROC = proc
            _XVFB_DISPLAY = f":{num}"
            logger.debug(f"Started Xvfb on DISPLAY=:{num} for headed browser")
            return _XVFB_DISPLAY
        try:
            proc.terminate()
        except Exception:
            pass
    return None


def _browser_env_with_display() -> dict[str, str] | None:
    """Return an env dict with a usable DISPLAY for a headed launch.

    Returns None when the current env already has a valid DISPLAY (inherit),
    otherwise a copy of os.environ with DISPLAY pointing at an Xvfb we manage.
    """
    if _display_available():
        return None
    disp = _ensure_virtual_display()
    if not disp:
        return None
    env = dict(os.environ)
    env["DISPLAY"] = disp
    return env


def _account_ua(account: str | None) -> tuple[str, str]:
    """(user_agent, sec-ch-ua) for the browser that created `account`.

    Falls back to the Playwright-bundled Chromium when the account is unknown
    or has no saved browser. Never raises — a broken accounts.json must not
    break outbound requests.
    """
    try:
        from engine.platform_paths import get_account_ua_fingerprint
        return get_account_ua_fingerprint(account)
    except Exception:
        from engine.platform_paths import derive_ua_fingerprint
        return derive_ua_fingerprint(None)


def _current_account_ua() -> tuple[str, str]:
    """(user_agent, sec-ch-ua) for the browser that created the active account."""
    try:
        from engine.config import get_active_account
        return _account_ua(get_active_account())
    except Exception:
        return _account_ua(None)


def build_headers(
    cookies: str | None = None,
    bx_ua: str | None = None,
    bx_umidtoken: str | None = None,
    referer: str | None = None,
    account: str | None = None,
    authorization: str | None = None,
) -> dict[str, str]:
    """Construct HTTP headers with given or fallback cookies and security tokens.

    Mirrors the real chat.qwen.ai Chromium web client so the WAF fingerprint
    (UA + sec-ch-ua + bx-ua token) stays internally consistent. The UA and
    sec-ch-ua brands are derived from the Chromium version of the browser that
    created the account (see accounts.json → browser_path), so a Helium-created
    account announces Chromium 153, not a hardcoded guess.

    `referer` should be the full chat URL when known
    (https://chat.qwen.ai/c/<chat_id>), falling back to the site root.
    """
    _tz = datetime.now().astimezone().strftime("%a %b %d %Y %H:%M:%S GMT%z")

    user_agent, sec_ch_ua = _account_ua(account) if account else _current_account_ua()

    headers = {
        "User-Agent": user_agent,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/json",
        "Referer": referer or "https://chat.qwen.ai/",
        "Timezone": _tz,
        "X-Accel-Buffering": "no",
        "X-Request-Id": str(uuid.uuid4()),
        "Version": "0.3.20",
        "source": "web",
        "sec-ch-ua": sec_ch_ua,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Linux"',
        # ── Sec-Fetch-* headers ──────────────────────────────────────────
        # curl_cffi's impersonate="chrome" auto-injects these as if the
        # request were a page navigation (Dest: document, Mode: navigate,
        # Site: none). But Qwen's API is called via JS fetch(), which sends
        # completely different values. Alibaba's WAF flags the mismatch
        # instantly — a POST to /completions with Sec-Fetch-Mode: navigate
        # is impossible for a real browser.
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        # ── Strip navigation-only headers ────────────────────────────────
        # curl_cffi also leaks Sec-Fetch-User and Upgrade-Insecure-Requests
        # which only exist on top-level navigations, never on fetch() calls.
        # Setting them to empty string prevents curl_cffi from injecting
        # its defaults. We handle actual removal in the caller.
        "Cookie": cookies or COOKIES,
        "bx-ua": bx_ua or BX_UA,
        "bx-umidtoken": bx_umidtoken or BX_UMIDTOKEN,
        "bx-v": "2.5.37",
    }
    # Qwen authenticates API calls with a Bearer JWT, NOT with cookies. The web
    # client sends `Authorization: Bearer <localStorage.token>` on every
    # /api/v2 call and — confirmed by CDP capture — sends NO Cookie header at
    # all. Without this, /chat/completions returns HTTP 200 with
    # {"success":false,"code":"CHAT_NOT_FOUND"} and the WAF eventually tarpits
    # the session with an x5sec punish page.
    #
    # Fall back to the bearer cached on disk: most callers (service.py's
    # cached-token paths, agents, research) build headers from cached WAF
    # tokens without passing authorization. Look it up here so every caller
    # gets the header, not just the two that pass it explicitly.
    bearer = authorization
    if bearer is None:
        try:
            from engine.config import get_qwen_tokens_for_account
            cached = get_qwen_tokens_for_account(account)
            if cached:
                tok = cached.get("bearer")
                if tok:
                    bearer = f"Bearer {tok}"
        except Exception:
            bearer = None
    if bearer:
        headers["Authorization"] = bearer
    return headers


# Headers that curl_cffi's impersonate="chrome" auto-injects for page navigations
# but that a real browser NEVER sends on fetch()/XHR API calls. If these leak
# through, Alibaba's WAF sees an impossible combination (JSON POST + navigation
# headers) and flags it as bot traffic.
_NAVIGATION_ONLY_HEADERS = frozenset({
    "sec-fetch-user",
    "upgrade-insecure-requests",
})


def sanitize_fetch_headers(headers: dict[str, str]) -> dict[str, str]:
    """Remove navigation-only headers that curl_cffi leaks into API requests.

    Call this right before passing headers to curl_cffi for any Qwen API call.
    Returns a new dict — doesn't mutate the original.
    """
    return {
        k: v for k, v in headers.items()
        if k.lower() not in _NAVIGATION_ONLY_HEADERS
    }


class BrowserManager:
    """Manages a single persistent Chromium instance to upload images & sniff headers."""

    def __init__(self, user_data_dir: str | None = None, headless: bool = True):
        # HEADLESS by default. This manager is the TOKEN / HEADER / COOKIE
        # refresh path for Qwen (WAF), MiMo and Gemini — those only need the
        # browser to mint credentials, never a visible window, so a headed
        # launch here means a new on-screen Chromium on every refresh. The
        # actual ghost-tab CHAT sessions (Kimi, DeepSeek) run headed through
        # ProfileHub instead; z.ai promotes itself headed via ensure_headed().
        if user_data_dir is None:
            from engine.config import get_browser_data_dir
            user_data_dir = str(get_browser_data_dir())
        self.user_data_dir = user_data_dir
        self.headless = headless
        self.playwright = None
        self.context = None
        self.page = None
        # Set when we attached to a browser we did not launch: teardown must
        # drop the CDP link, never close someone else browser.
        self._adopted = False
        self._cdp_browser = None
        # Set when we borrowed a live ProfileHub's shared browser (same profile
        # dir). We own only our tab; the hub owns the browser.
        self._hub = None
        self._hub_page = None
        # Governor slot for a browser WE launched (ephemeral priority — the
        # header/token path). Released in close().
        self._slot = None
        # Job-and-die bookkeeping. This manager exists to mint headers, not to
        # live forever: once the last header op finishes it schedules its own
        # teardown so it does not squat a slot the chat side may need.
        self._hold_count = 0
        self._close_timer: asyncio.Task | None = None
        self._auto_close_ttl = float(os.environ.get("SABLE_HEADER_BROWSER_TTL", "20"))

    @property
    def browser_headless(self) -> bool:
        return self.headless

    @property
    def is_running(self) -> bool:
        """True if the persistent browser context is currently up."""
        return self.playwright is not None

    @staticmethod
    def _pid_is_browser(pid: int) -> bool:
        """True only if `pid` is alive AND looks like a Chromium-family process.

        Guards against PID recycling: a live *unrelated* process must not count
        as holding the profile lock. This is what caused the phantom
        "profile locked for no reason" warnings.
        """
        # os.kill(pid, 0) is POSIX-only; on Windows it terminates the target.
        # pid_exists() is the cross-platform liveness probe.
        if not pid_exists(pid):
            return False

        # Linux: inspect /proc/<pid>/cmdline for a browser-ish executable.
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\x00", b" ").lower()
        except OSError:
            # Non-Linux (or unreadable): fall back to "alive == locked".
            return True
        return any(tok in cmdline for tok in (
            b"chrom", b"chrome", b"chromium", b"brave", b"msedge", b"browser",
        ))

    def _check_profile_lock(self) -> bool:
        """Check if the profile is locked by another Chromium instance.

        The lock is a symlink to "<hostname>-<pid>". `os.kill(pid, 0)` alone is
        NOT enough to call it live: PIDs get recycled, so a dead Chromium's PID
        can be owned by some unrelated process, making a stale lock look real
        ("profile locked for no reason"). We additionally verify the hostname
        matches and that the PID actually belongs to a browser process.
        """
        lock_file = Path(self.user_data_dir) / "SingletonLock"
        # Use is_symlink() | exists() because SingletonLock is a symlink to
        # "hostname-PID". If the target process is dead, exists() returns False
        # but the dangling symlink still blocks Chromium from launching.
        if not (lock_file.is_symlink() or lock_file.exists()):
            return True

        stale = True
        if lock_file.is_symlink():
            try:
                target = lock_file.readlink().name  # e.g. "Archie-24367"
                host, _, pid_str = target.rpartition("-")
                pid = int(pid_str)

                import socket
                same_host = (not host) or host == socket.gethostname()
                if same_host and self._pid_is_browser(pid):
                    stale = False  # genuinely alive browser on this host
            except (ValueError, OSError, ProcessLookupError):
                stale = True

        if stale:
            logger.debug("Removing stale SingletonLock...")
            try:
                lock_file.unlink()
            except OSError as e:
                logger.error(f"Could not remove lock: {e}")
                return False
        else:
            logger.warning("Profile locked by active Chromium process")
            return False
        return True


    async def _adopt_hub(self) -> bool:
        """Borrow a live ProfileHub's shared browser for this profile dir.

        Kimi/DeepSeek multiplex their tabs onto ONE Helium per profile via
        ProfileHub. When this manager targets the SAME dir, reuse that hub's
        context instead of launching a rival browser (SingletonLock kill-war).
        We open our own page inside it; the hub keeps ownership, so teardown
        only closes OUR tab.
        """
        try:
            from connectors.common.profile_hub import ProfileHub
        except Exception:
            return False
        try:
            hub = ProfileHub.get_live(Path(self.user_data_dir))
        except Exception:
            hub = None
        if hub is None:
            return False
        try:
            ctx = hub.context
            if ctx is None:
                return False
            # Verify the context is genuinely usable before adopting it.
            _ = ctx.pages
        except Exception as exc:
            logger.debug("hub for %s not usable (%s)", self.user_data_dir, exc)
            return False
        # We do NOT take ownership: the hub owns the browser, we just ride it.
        self._hub = hub
        self.context = ctx
        self.playwright = getattr(hub, "_pw", None)
        self._adopted = True
        self.headless = bool(getattr(hub, "headless", self.headless))
        try:
            self.page = await hub.new_tab(self._hub_start_url())
            self._hub_page = self.page
        except Exception as exc:
            logger.debug("hub tab open failed (%s); will relaunch", exc)
            self._hub = None
            self._hub_page = None
            self.context = None
            self.page = None
            self._adopted = False
            return False
        logger.debug("BrowserManager riding live ProfileHub for %s",
                     self.user_data_dir)
        return True

    def _hub_start_url(self) -> str:
        """Origin this manager needs a tab on (Qwen by default)."""
        return "https://chat.qwen.ai"

    async def _adopt_existing(self) -> bool:
        '''Attach to a live HEADED browser already holding this profile.

        z.ai's Aliyun captcha rejects headless Chromium, so only a headed
        holder is worth sharing. Ports come from the holders'
        --remote-debugging-port flag, DevToolsActivePort, and listening fds.
        '''
        from connectors.common import profile_hub as _ph
        profile = Path(self.user_data_dir)
        holders = _ph._profile_holder_pids(profile)
        if not holders:
            return False
        if any(_pid_is_headless(p) is True for p in holders):
            logger.debug('live browser on profile is headless - not usable for z.ai')
            return False

        ports = _ph._cmdline_cdp_ports(holders)
        dp = _ph._devtools_port(profile)
        if dp is not None and dp not in ports:
            ports.append(dp)
        for lp in sorted(_ph._listening_ports(holders)):
            if lp not in ports:
                ports.append(lp)
        if not ports:
            logger.debug('live headed browser exposes no CDP port - cannot adopt')
            return False

        from playwright.async_api import async_playwright
        for port in ports:
            try:
                self.playwright = await async_playwright().start()
                self._cdp_browser = await self.playwright.chromium.connect_over_cdp(
                    'http://127.0.0.1:%d' % port, timeout=8000)
                ctxs = self._cdp_browser.contexts
                if not ctxs:
                    raise RuntimeError('no contexts over CDP')
                self.context = ctxs[0]
                self.page = ctxs[0].pages[0] if ctxs[0].pages else await ctxs[0].new_page()
                self._adopted = True
                self.headless = False
                logger.debug("adopted live headed browser over CDP (port %d)", port)
                return True
            except Exception as exc:
                logger.debug("CDP adopt failed on port %d: %s", port, exc)
                try:
                    if self.playwright is not None:
                        await self.playwright.stop()
                except Exception:
                    pass
                self.playwright = None
                self._cdp_browser = None
        return False

    async def _reclaim_profile(self) -> None:
        '''Kill every process holding this profile so we can launch our own.'''
        from connectors.common import profile_hub as _ph
        profile = Path(self.user_data_dir)
        victims = _ph._profile_holder_pids(profile)
        for pid in victims:
            process_utils.kill_process_tree(pid)
        for _ in range(30):
            if not any(_ph._pid_alive(p) for p in victims):
                break
            await asyncio.sleep(0.1)
        for pid in victims:
            if _ph._pid_alive(pid):
                process_utils.kill_process_tree(pid, sig=process_utils.SIGKILL)
        await asyncio.sleep(0.2)
        _ph._clear_singleton(profile)
        logger.debug("reclaimed %d process(es) on profile", len(victims))

    _start_failed: bool = False

    def _context_alive(self) -> bool:
        """True only if our browser link is genuinely usable.

        ``self.context`` can be non-None yet dead — the browser window was
        closed, the process crashed, or the CDP socket dropped. Touching
        ``.pages`` raises in that case, which is exactly the condition that
        used to make header refresh fail hard with no retry.
        """
        if self.context is None:
            return False
        try:
            _ = self.context.pages
            return True
        except Exception:
            return False

    async def start(self, max_retries: int = 3):
        """Lazy-start the browser, self-healing a dead context.

        On any launch failure every browser bound to this profile is killed
        and the launch is retried up to ``max_retries`` times.
        """
        last_exc: Exception | None = None
        for attempt in range(1, max_retries + 1):
            try:
                await self._start_once()
                return
            except Exception as exc:
                last_exc = exc
                # A full pool is not a broken profile. Retrying (and especially
                # killing profile holders) would tear down a live chat browser
                # to make room for a mere token refresh — exactly backwards.
                # Bubble it untouched so the caller renders the kill card.
                from engine.browser_governor import NoSlotAvailable
                if isinstance(exc, NoSlotAvailable):
                    logger.info(
                        "Browser start skipped for %s — no slot available "
                        "(all held by chat)", self.user_data_dir)
                    self._start_failed = False
                    raise
                logger.warning(
                    "Browser start attempt %d/%d failed for %s: %s: %s — "
                    "killing profile browsers & retrying",
                    attempt, max_retries, self.user_data_dir,
                    type(exc).__name__, exc)
                try:
                    await self.close()
                except Exception:
                    pass
                try:
                    from connectors.common.profile_hub import (
                        _kill_profile_holders as _kill)
                    await _kill(Path(self.user_data_dir))
                except Exception as kill_exc:
                    logger.warning("profile kill failed for %s: %s",
                                   self.user_data_dir, kill_exc)
                # Clear the poison flag so the next attempt is allowed.
                self._start_failed = False
                if attempt < max_retries:
                    await asyncio.sleep(min(0.5 * attempt, 2.0))
        self._start_failed = True
        raise RuntimeError(
            f"Browser start failed after {max_retries} attempts for "
            f"{self.user_data_dir}") from last_exc

    async def _start_once(self):
        """Single launch attempt (the old ``start`` body)."""
        if self.playwright and self._context_alive():
            return
        if self.playwright and not self._context_alive():
            # Stale link from a prior dead browser — drop it before relaunch.
            await self.close()

        # 0) CO-EXIST with a live ProfileHub on the SAME profile dir. Kimi and
        #    DeepSeek own the shared browser through ProfileHub; Qwen/z.ai/
        #    Gemini/MiMo ride this BrowserManager. Without this step the manager
        #    would launch a rival Helium on the same user-data-dir and the two
        #    would kill-war over SingletonLock. Riding the hub's context means
        #    ONE browser per profile, multiplexed by tab.
        if await self._adopt_hub():
            return

        # 1) Attach to a live HEADED browser already holding this profile.
        if await self._adopt_existing():
            return

        # 2) A live holder we could not adopt: a headless one is useless for
        #    z.ai, so reclaim it; a headed one with no CDP port is a real
        #    conflict, so fail loudly instead of double-launching.
        from connectors.common import profile_hub as _ph
        _profile = Path(self.user_data_dir)
        if _ph._lock_is_stale(_profile):
            _ph._clear_singleton(_profile)
        else:
            _holders = _ph._profile_holder_pids(_profile)
            if any(_pid_is_headless(p) is True for p in _holders):
                logger.debug('headless browser holds the profile - reclaiming for headed z.ai')
                await self._reclaim_profile()
            else:
                raise RuntimeError(
                    'Browser profile locked by a live headed process with no '
                    'CDP port to adopt: %s' % self.user_data_dir
                )
        import traceback as _tb
        launch_num = _increment_playwright_counter()
        logger.debug(f"Launching persistent browser context #{launch_num} (headless={self.headless})...")
        _caller = _tb.extract_stack()[-2]
        logger.debug(f"start() #{launch_num} from {_caller.filename}:{_caller.lineno} in {_caller.name}")
        from playwright.async_api import async_playwright
        from engine.platform_paths import (
            resolve_browser_for_profile,
            shared_browser_args,
            BROWSER_IGNORE_DEFAULT_ARGS,
        )
        # EPHEMERAL priority: this browser mints headers/tokens and then dies.
        # At the cap it waits for a chat browser rather than evicting one; if
        # every slot is chat-held it raises NoSlotAvailable, which the caller
        # surfaces as the "no browser slot" card.
        from engine import browser_governor
        self._slot = await browser_governor.acquire(
            f"headers:{Path(self.user_data_dir).name}",
            priority=browser_governor.PRIORITY_EPHEMERAL,
        )
        try:
            self.playwright = await async_playwright().start()
            profile_name = getattr(self, "profile_name", None) or Path(self.user_data_dir).name
            exe_path = resolve_browser_for_profile(profile_name)
            launch_kwargs: dict[str, Any] = dict(
                user_data_dir=self.user_data_dir,
                headless=self.headless,
                args=shared_browser_args(exe_path),
                # Use the REAL OS keyring, not Playwright's forced mock
                # keychain. A profile logged in via a raw `chrome` launch has
                # v10 cookies encrypted with the keyring key; with the default
                # `--use-mock-keychain` they are undecryptable and every API
                # call 401s.
                ignore_default_args=list(BROWSER_IGNORE_DEFAULT_ARGS),
            )
            if not self.headless:
                # Headed launch needs a display; provide one (Xvfb) if the
                # server has none, otherwise Chromium aborts immediately.
                _env = _browser_env_with_display()
                if _env:
                    launch_kwargs["env"] = _env
                else:
                    logger.warning("No DISPLAY available for headed launch")
            if exe_path:
                launch_kwargs["executable_path"] = exe_path
            self.context = await self.playwright.chromium.launch_persistent_context(**launch_kwargs)
            # Bind the launched browser's pid to our governor slot. Playwright
            # spawns the process for us, so the pid is resolved from the
            # profile dir rather than a Popen handle.
            try:
                _roots = browser_governor.find_browser_pids(self.user_data_dir)
                if _roots:
                    self._slot.attach(_roots[0], user_data_dir=self.user_data_dir)
            except Exception as _att_exc:
                logger.debug("could not attach browser pid to slot: %s", _att_exc)
            self.page = await self.context.new_page()
            await self.page.goto("https://chat.qwen.ai", wait_until="domcontentloaded", timeout=15000)

            # Poll localStorage for ANY known auth marker. This BrowserManager is
            # shared across Qwen, DeepSeek and z.ai — Qwen/z.ai park their JWT at
            # localStorage 'token', DeepSeek at 'userToken'. Checking only
            # 'token' made every DeepSeek refresh (which reuses this same
            # manager) log a bogus "no JWT" warning even though login was fine.
            # We only warn when NONE of the known markers appear.
            _AUTH_KEYS = ("token", "userToken")
            has_token = False
            for _ in range(12):  # up to 6s, checking every 500ms
                has_token = await self.page.evaluate(
                    "(keys) => keys.some(k => !!localStorage.getItem(k))",
                    list(_AUTH_KEYS),
                )
                if has_token:
                    break
                await self.page.wait_for_timeout(500)

            if not has_token:
                logger.warning("⚠️  No auth token in localStorage after 6s (checked: %s)!", ", ".join(_AUTH_KEYS))
                logger.warning(f"Profile may be stale/corrupted. Try: rm -rf {self.user_data_dir} && re-login via browser_opener.py")
            else:
                logger.debug("✅ Auth token hydrated successfully.")

            # Await the SDK load directly — the old fixed 1s sleep added dead
            # time to every cold launch with no guarantee it had loaded.
            try:
                await self.page.add_script_tag(url="https://gosspublic.alicdn.com/aliyun-oss-sdk-6.18.1.min.js")
            except Exception as exc:
                logger.warning(f"Aliyun OSS SDK load failed (non-fatal): {exc}")
        except Exception:
            # Mark as permanently failed so callers can't loop on this profile.
            # One failure = done. Re-login or delete the profile to recover.
            self._start_failed = True
            # A slot held by a browser that never came up would wedge the cap —
            # drop it before closing.
            try:
                if self._slot is not None:
                    self._slot.release(kill=True)
                    self._slot = None
            except Exception:
                pass
            await self.close()
            raise

    # -- job-and-die lifecycle ------------------------------------------

    def hold(self) -> None:
        """Pin the browser open for the duration of a real chat session.

        Header refreshes are fire-and-forget: the browser mints tokens and
        dies. But some callers (MiMo, Gemini web) keep riding the live context
        to talk to the model. They call ``hold()`` before the session and
        ``unhold()`` after, which cancels the auto-close timer so the browser
        is not torn down mid-conversation.
        """
        self._hold_count += 1
        self._cancel_auto_close()

    def unhold(self) -> None:
        """Release a hold taken with :meth:`hold`; may schedule auto-close."""
        if self._hold_count > 0:
            self._hold_count -= 1
        if self._hold_count == 0:
            self._schedule_auto_close()

    def _cancel_auto_close(self) -> None:
        if self._close_timer is not None and not self._close_timer.done():
            self._close_timer.cancel()
        self._close_timer = None

    def _schedule_auto_close(self) -> None:
        """Close ourselves once the TTL lapses with nobody holding us.

        TTL>0 lets a burst of header ops in one turn reuse the same warm
        browser (each op re-arms the timer) while still guaranteeing the
        browser is gone before the next turn. TTL=0 closes immediately.
        Adopted/hub browsers are never auto-closed — we don't own them.
        """
        if self._hold_count > 0:
            return
        if self._adopted or self._hub is not None:
            return
        if self.playwright is None:
            return
        self._cancel_auto_close()
        ttl = self._auto_close_ttl

        async def _closer() -> None:
            try:
                if ttl > 0:
                    await asyncio.sleep(ttl)
                if self._hold_count > 0:
                    return
                logger.debug("header browser idle past TTL — closing %s",
                             self.user_data_dir)
                await self.close()
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.debug("auto-close failed: %s", exc)

        try:
            self._close_timer = asyncio.create_task(_closer())
        except RuntimeError:
            # No running loop (sync caller) — close synchronously instead.
            self._close_timer = None

    async def restart(self, headless: bool | None = None) -> None:
        """Close and relaunch the browser with an optional new headless flag."""
        if headless is not None:
            self.headless = headless
        self._start_failed = False
        await self.close()
        await self.start()

    async def ensure_headed(self) -> None:
        """Ensure the browser runs headed — required for Aliyun captcha minting.

        z.ai's TRACELESS captcha SDK rejects headless Chromium outright (headless
        UA + --ozone-platform=headless flags are trivially fingerprinted). If we
        are currently headless, tear down and relaunch headed on a virtual
        display (Xvfb) so the mint can succeed.
        """
        if not self.headless:
            return
        logger.debug("z.ai requires a headed browser — restarting headed on a virtual display...")
        await self.restart(headless=False)

    async def get_fresh_headers(self) -> dict[str, str]:
        """Sniff fresh WAF tokens and cookies using the running browser tab context."""
        await self.start()
        if not self.page or not self.context:
            raise RuntimeError("Browser session is not available")

        captured: dict[str, str] = {}

        def on_request(req) -> None:
            if "api/v2" in req.url:
                h = dict(req.headers)
                if "bx-ua" in h and "bx-umidtoken" in h:
                    captured["bx-ua"] = h["bx-ua"]
                    captured["bx-umidtoken"] = h["bx-umidtoken"]

        self.page.on("request", on_request)
        try:
            probe_urls = [
                "https://chat.qwen.ai/api/v2/users/status",
                "https://chat.qwen.ai/api/v2/chats?page_number=1&page_size=1",
            ]
            status = None
            for probe_url in probe_urls:
                if captured.get("bx-ua") and captured.get("bx-umidtoken"):
                    break
                status = await self.page.evaluate(
                    """async (url) => {
                        try {
                            const res = await fetch(url, { credentials: 'include' });
                            const body = await res.text();
                            return { ok: res.ok, status: res.status, body: body.slice(0, 300) };
                        } catch (err) {
                            return { ok: false, status: 0, body: String((err && err.message) || err) };
                        }
                    }""", probe_url
                )
                await self.page.wait_for_timeout(1500)
        finally:
            self.page.remove_listener("request", on_request)

        # Wait for critical Alibaba WAF cookies to be set by page JS.
        # acw_tc (WAF session), isg (bot detection token), and tfstk (fingerprint)
        # are generated asynchronously by Alibaba's anti-bot SDK. Without them,
        # httpx requests get captcha-challenged even with perfect headers.
        for _ in range(12):  # up to 6s, checking every 500ms
            _check_cookies = await self.context.cookies()
            _cookie_names = {c["name"] for c in _check_cookies}
            if "acw_tc" in _cookie_names and "isg" in _cookie_names:
                logger.debug("WAF cookies (acw_tc, isg) baked successfully")
                break
            await self.page.wait_for_timeout(500)
        else:
            logger.warning("WAF cookies (acw_tc/isg) not detected after 6s — captcha likely")

        all_cookies = await self.context.cookies()
        # Only include cookies that a real browser would send to chat.qwen.ai.
        # Playwright's context stores cross-domain cookies (DeepSeek, mmstat, etc.)
        # that a browser would never leak to Qwen. Sending them is a fingerprint red flag.
        _qwen_domains = (".qwen.ai", "chat.qwen.ai", ".alibaba.com")
        filtered_cookies = [
            c for c in all_cookies
            if any(c["domain"] == d or c["domain"].endswith(d) for d in _qwen_domains)
        ]
        cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in filtered_cookies]) if filtered_cookies else None

        missing = [key for key in ("bx-ua", "bx-umidtoken") if not captured.get(key)]
        if missing:
            status_info = str(status)[:500]
            logger.warning(
                "header capture incomplete: missing %s; cookies=%s; status=%s; "
                "falling back to configured session tokens",
                ", ".join(missing), "yes" if cookie_str else "no", status_info,
            )

        fresh = "fresh" if not missing else "fallback"
        logger.debug(f"Using {fresh} WAF headers (bx-ua={'yes' if captured.get('bx-ua') else 'no'}, bx-umidtoken={'yes' if captured.get('bx-umidtoken') else 'no'})")

        # Use the live page URL as Referer when we're on a chat page, so the
        # request carries the same https://chat.qwen.ai/c/<id> referer the real
        # client sends.
        referer = None
        try:
            cur = self.page.url if self.page else ""
            if cur.startswith("https://chat.qwen.ai/c/"):
                referer = cur
        except Exception:
            pass

        # Job done — re-arm the auto-close timer. Another header op in this
        # same turn re-arms it again; the browser dies once the turn ends.
        self._schedule_auto_close()
        bearer = await self.extract_qwen_token()
        return build_headers(
            cookies=cookie_str,
            bx_ua=captured.get("bx-ua"),
            bx_umidtoken=captured.get("bx-umidtoken"),
            referer=referer,
            authorization=f"Bearer {bearer}" if bearer else None,
        )

    async def get_live_headers(self) -> dict[str, str]:
        """Instantly read current headers from the live browser context.

        No HTTP probes, no waiting. Reads cookies directly from the Playwright
        cookie jar and extracts bx-ua/bx-umidtoken from the Baxia SDK's global
        state in the page JS context. Takes <100ms instead of 3-6s.

        Call this before every request to always send the freshest tokens.
        Requires the browser to already be running (via start() or get_fresh_headers()).
        """
        # Don't try to launch — just reuse whatever is already running.
        # If the browser isn't up yet, fall back to get_fresh_headers().
        if not self.page or not self.context:
            if not self.playwright:
                logger.debug("Browser not running, doing full fresh header setup...")
                return await self.get_fresh_headers()
            raise RuntimeError("Browser session is not available")

        # Read cookies directly — instant, no network needed
        all_cookies = await self.context.cookies()
        _qwen_domains = (".qwen.ai", "chat.qwen.ai", ".alibaba.com")
        filtered_cookies = [
            c for c in all_cookies
            if any(c["domain"] == d or c["domain"].endswith(d) for d in _qwen_domains)
        ]
        cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in filtered_cookies]) if filtered_cookies else None

        # Extract bx-ua and bx-umidtoken from the Baxia SDK's JS globals.
        # The SDK stores its state on window.__baxia__ or similar objects.
        # We try multiple known access patterns.
        bx_ua: str | None = None
        bx_umidtoken: str | None = None
        try:
            baxia_state = await self.page.evaluate("""() => {
                const result = { bx_ua: null, bx_umidtoken: null };
                try {
                    // Pattern 1: Baxia global object
                    if (window.__baxia__) {
                        const b = window.__baxia__;
                        if (typeof b.getUA === 'function') result.bx_ua = b.getUA();
                        if (b.umidToken) result.bx_umidtoken = b.umidToken;
                        if (b.token) result.bx_umidtoken = b.token;
                    }
                    // Pattern 2: AWSC namespace
                    if (!result.bx_ua && window.AWSC) {
                        const a = window.AWSC;
                        if (typeof a.getUA === 'function') result.bx_ua = a.getUA();
                    }
                    // Pattern 3: __umid_getinfo
                    if (!result.bx_umidtoken && typeof window.__umid_getinfo === 'function') {
                        const info = window.__umid_getinfo();
                        if (info && info.token) result.bx_umidtoken = info.token;
                    }
                    // Pattern 4: scan for umid token in meta tags
                    if (!result.bx_umidtoken) {
                        const meta = document.querySelector('meta[name="umid-token"]');
                        if (meta) result.bx_umidtoken = meta.getAttribute('content');
                    }
                } catch(e) {}
                return result;
            }""")
            bx_ua = baxia_state.get("bx_ua") if isinstance(baxia_state, dict) else None
            bx_umidtoken = baxia_state.get("bx_umidtoken") if isinstance(baxia_state, dict) else None
        except Exception:
            pass

        # Fallback: if JS extraction failed, do ONE fast fetch probe
        if not bx_ua or not bx_umidtoken:
            captured: dict[str, str] = {}
            def on_req(req) -> None:
                if "api/v2" in req.url:
                    h = dict(req.headers)
                    if "bx-ua" in h:
                        captured["bx-ua"] = h["bx-ua"]
                    if "bx-umidtoken" in h:
                        captured["bx-umidtoken"] = h["bx-umidtoken"]
            self.page.on("request", on_req)
            try:
                await self.page.evaluate("""async () => {
                    try { await fetch('/api/v2/users/status', { credentials: 'include' }); } catch(e) {}
                }""")
                await self.page.wait_for_timeout(800)
            finally:
                self.page.remove_listener("request", on_req)
            bx_ua = bx_ua or captured.get("bx-ua")
            bx_umidtoken = bx_umidtoken or captured.get("bx-umidtoken")

        referer = None
        try:
            cur = self.page.url if self.page else ""
            if cur.startswith("https://chat.qwen.ai/c/"):
                referer = cur
        except Exception:
            pass

        self._schedule_auto_close()
        bearer = await self.extract_qwen_token()
        return build_headers(
            cookies=cookie_str,
            bx_ua=bx_ua,
            bx_umidtoken=bx_umidtoken,
            referer=referer,
            authorization=f"Bearer {bearer}" if bearer else None,
        )

    async def extract_qwen_token(self) -> str | None:
        """Read the Qwen Bearer JWT from chat.qwen.ai localStorage.

        Qwen's web client authenticates every /api/v2 call with
        ``Authorization: Bearer <localStorage.token>`` and sends no Cookie.
        The stored value is the bare JWT string (confirmed: 281 chars, starts
        ``eyJhbGciOiJIUzI1NiIsImtpZCI6ImxlZ2FjeSJ9``).

        Returns None when the profile has no token or the page is elsewhere.
        """
        if not self.page:
            return None
        try:
            raw = await self.page.evaluate("() => localStorage.getItem('token')")
        except Exception:
            return None
        if not raw:
            return None
        raw = str(raw).strip()
        if not raw:
            return None
        # Tolerate both the bare JWT and a JSON envelope {"value": "<jwt>"}.
        if raw.startswith("{"):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    raw = str(parsed.get("value") or parsed.get("access_token") or "")
            except (json.JSONDecodeError, TypeError):
                pass
        raw = raw.strip().strip('"')
        return raw or None

    async def extract_deepseek_token(self) -> str:
        """Read DeepSeek bearer token from the shared persistent browser profile."""
        await self.start()
        if not self.context:
            raise RuntimeError("Browser session is not available")

        page = await self.context.new_page()
        try:
            await page.goto("https://chat.deepseek.com", wait_until="domcontentloaded", timeout=15000)
            # Poll for the token instead of a fixed 2s sleep — returns the
            # moment localStorage hydrates (usually a few hundred ms).
            raw = None
            for _ in range(10):  # up to ~3s
                raw = await page.evaluate("() => localStorage.getItem('userToken')")
                if raw:
                    break
                await page.wait_for_timeout(300)
        finally:
            await page.close()

        if not raw:
            raise RuntimeError("No DeepSeek userToken found in browser profile. Log in to chat.deepseek.com first.")

        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                token = parsed.get("value", raw)
            else:
                token = raw
        except (json.JSONDecodeError, AttributeError):
            token = raw.strip('"')

        token = str(token).strip()
        if not token:
            raise RuntimeError("DeepSeek userToken was empty after parsing.")
        return token

    async def extract_zai_token(self) -> str:
        """Read the z.ai bearer JWT from the shared persistent browser profile."""
        await self.start()
        if not self.context:
            raise RuntimeError("Browser session is not available")

        page = await self.context.new_page()
        try:
            await page.goto("https://chat.z.ai", wait_until="domcontentloaded", timeout=15000)
            # Poll for the token instead of a fixed 2s sleep — returns the
            # moment localStorage hydrates (usually a few hundred ms).
            raw = None
            for _ in range(10):  # up to ~3s
                raw = await page.evaluate("() => localStorage.getItem('token')")
                if raw:
                    break
                await page.wait_for_timeout(300)
        finally:
            await page.close()

        if not raw:
            raise RuntimeError("No z.ai token found in browser profile. Log in to chat.z.ai first.")

        token = str(raw).strip().strip('"')
        if not token:
            raise RuntimeError("z.ai token was empty after parsing.")
        return token

    async def upload_file(self, file_path: str, cookies: str | None = None, bx_ua: str | None = None, bx_umidtoken: str | None = None) -> dict | None:
        """Upload any file (image or document) via direct HTTP (STS token + Aliyun OSS PUT).

        Documents (code, PDFs, text) use Qwen's `document` class — parsed
        server-side — while images use the `vision` class. No Playwright JS needed.
        """
        if not os.path.exists(file_path):
            logger.error(f"File not found: {file_path}")
            return None

        filesize = os.path.getsize(file_path)
        filename = os.path.basename(file_path)

        # Real MIME detection (shared helper) — Qwen keys parsing off this.
        from connectors.common.media import detect_mime, categorize
        mime_type = detect_mime(Path(file_path))
        category = categorize(mime_type)

        is_image = category == "image"
        # STS/filetype vocabulary: images -> "image", everything else -> "file".
        sts_filetype = "image" if is_image else "file"
        # Display class in the Qwen UI payload.
        file_class = "vision" if is_image else "document"
        show_type = "image" if is_image else "file"

        # Prefer live browser tokens — the disk-cached config constants go
        # stale and STS rejects them with "Token has expired" (2026-09-21).
        if not (cookies and bx_ua and bx_umidtoken):
            try:
                live = await self.get_live_headers()

                def _hdr(name: str, _live: dict = live) -> str | None:
                    for k, v in (_live or {}).items():
                        if k.lower() == name.lower():
                            return v
                    return None

                cookies = cookies or _hdr("cookie")
                bx_ua = bx_ua or _hdr("bx-ua")
                bx_umidtoken = bx_umidtoken or _hdr("bx-umidtoken")
            except Exception as e:
                logger.warning(f"upload_file live-header harvest failed ({e}) — trying disk tokens")

        # Fallback to config constants as last resort
        cookies = cookies or COOKIES
        bx_ua = bx_ua or BX_UA
        bx_umidtoken = bx_umidtoken or BX_UMIDTOKEN

        logger.debug(f"Uploading '{filename}' ({filesize} bytes) via direct HTTP...")

        # Fingerprint derived from the browser that created the active account.
        _ua, _sec_ch_ua = _current_account_ua()

        # Step 1: Get STS token
        sts_headers = {
            "User-Agent": _ua,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Content-Type": "application/json",
            "Timezone": datetime.now().astimezone().strftime("%a %b %d %Y %H:%M:%S GMT%z"),
            "source": "web",
            "version": "0.2.91",
            "x-request-id": str(uuid.uuid4()),
            "Cookie": cookies,
            "bx-ua": bx_ua or "",
            "bx-umidtoken": bx_umidtoken or "",
            "bx-v": "2.5.37",
            "sec-ch-ua": _sec_ch_ua,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Linux"',
            "Referer": "https://chat.qwen.ai/",
        }
        sts_payload = {"filename": filename, "filesize": str(filesize), "filetype": sts_filetype}

        try:
            from curl_cffi.requests import AsyncSession as _CffiSession
            async with _CffiSession(impersonate="chrome", timeout=15) as client:
                sts_resp = await client.post(
                    "https://chat.qwen.ai/api/v2/files/getstsToken",
                    headers=sts_headers,
                    json=sts_payload,
                )
            if sts_resp.status_code != 200:
                logger.error(f"STS token request failed: HTTP {sts_resp.status_code} — {sts_resp.content.decode(errors='replace')[:300]}")
                return None
            sts_data = json.loads(sts_resp.content)
            if not sts_data.get("success"):
                logger.error(f"STS token rejected: {json.dumps(sts_data)[:300]}")
                return None
            sts = sts_data["data"]
        except Exception as e:
            logger.error(f"STS token request exception: {e}")
            return None

        # Step 2: Upload to Aliyun OSS
        try:
            import oss2
            auth = oss2.StsAuth(sts["access_key_id"], sts["access_key_secret"], sts["security_token"])
            bucket = oss2.Bucket(auth, f"https://{sts['endpoint']}", sts["bucketname"])

            with open(file_path, "rb") as f:
                put_result = bucket.put_object(sts["file_path"], f)

            if put_result.status not in (200, 204):
                logger.error(f"OSS PUT failed with status {put_result.status}")
                return None
        except Exception as e:
            logger.error(f"OSS upload failed: {e}")
            return None

        # Step 3: Build file object
        now_ms = int(time.time() * 1000)
        file_id = sts["file_id"]
        file_url = sts.get("file_url", "")

        meta: dict = {"name": filename, "size": filesize, "content_type": mime_type}
        if not is_image:
            # Qwen's web client reports server-side parse state for documents.
            meta["parse_meta"] = {"parse_status": "success"}

        file_obj = {
            "type": "file" if not is_image else "image",
            "file": {
                "created_at": now_ms,
                "data": {},
                "filename": filename,
                "hash": None,
                "id": file_id,
                "user_id": sts["file_path"].split("/")[0],
                "meta": meta,
                "update_at": now_ms,
                "lastModified": now_ms,
                "name": filename,
                "webkitRelativePath": "",
                "size": filesize,
                "type": mime_type,
            },
            "id": file_id,
            "url": file_url,
            "name": filename,
            "collection_name": "",
            "progress": 0,
            "status": "uploaded",
            "greenNet": "success",
            "size": filesize,
            "error": "",
            "itemId": str(uuid.uuid4()),
            "file_type": mime_type,
            "showType": show_type,
            "file_class": file_class,
            "uploadTaskId": str(uuid.uuid4()),
        }
        logger.debug(f"File uploaded successfully! ({file_class}) File ID: {file_id}")
        return file_obj

    # Backward-compatible alias — existing callers pass images.
    async def upload_image(self, image_path: str, cookies: str | None = None, bx_ua: str | None = None, bx_umidtoken: str | None = None) -> dict | None:
        return await self.upload_file(image_path, cookies=cookies, bx_ua=bx_ua, bx_umidtoken=bx_umidtoken)

    async def sync_context(self, headers: dict[str, str] | None = None, project_id: str | None = None, custom_instructions: str | None = None, layout_mode: str | None = None) -> bool:
        """Sync persona instructions to Qwen via settings/update API (no Playwright DOM).

        Args:
            custom_instructions: If provided, use this string directly instead of
                building instructions via build_instructions(). Used by subagents
                to push their own system prompt into Qwen's personalization slot.
            layout_mode: "chat" strips tools/skills/MCP except web search + chat_title.
        """
        # Try disk-cached tokens first — this is a pure API call, no browser needed.
        # Only fall back to launching Playwright if no cached tokens exist.
        if headers is None:
            from engine.config import get_qwen_tokens_for_account
            cached = get_qwen_tokens_for_account()
            if cached and cached.get("cookies"):
                headers = build_headers(
                    cookies=cached["cookies"],
                    bx_ua=cached.get("bx_ua"),
                    bx_umidtoken=cached.get("bx_umidtoken"),
                )
                logger.debug("sync_context: using disk-cached tokens")
            else:
                await self.start()

        SETTINGS_URL = "https://chat.qwen.ai/api/v2/users/user/settings/update"

        if custom_instructions is not None:
            instructions = custom_instructions
        else:
            # Build instructions using shared builder.
            # provider="qwen" injects <action> tag format instructions (Qwen's native wrapper).
            from connectors.common.instruction_builder import build_instructions
            instructions = build_instructions(project_id=project_id, provider="qwen", layout_mode=layout_mode)

        MAX_CHARS = 40960
        if len(instructions) > MAX_CHARS:
            instructions = instructions[:MAX_CHARS]

        # Payloads are built once and reused by both the first attempt and the
        # self-heal retry below.
        tools_payload = {
            "tools_enabled": {
                "web_extractor": False,
                "web_search_image": False,
                "web_search": False,
                "image_gen_tool": False,
                "code_interpreter": False,
                "history_retriever": False,
                "image_edit_tool": False,
                "bio": False,
                "image_zoom_in_tool": False,
            }
        }
        instr_payload = {
            "personalization": {
                "name": _get_user_name(),
                "description": "",
                "style": "Default",
                "instruction": instructions,
            }
        }

        async def _attempt(base_headers: dict[str, str]) -> tuple[bool, bool]:
            """Run both settings POSTs. Returns (ok, unauthorized).

            Each step uses its own session because curl_cffi auto-stores
            Set-Cookie responses in its jar and appends them to subsequent
            requests, creating duplicate cookies that Qwen's WAF rejects.
            """
            from curl_cffi.requests import AsyncSession as _CffiSession
            h = dict(base_headers)  # copy — never mutate the cached dict
            h.update({
                "Content-Type": "application/json",
                "Version": "0.3.20",
                "source": "web",
                "Referer": "https://chat.qwen.ai/settings/personalization",
                "X-Request-Id": str(uuid.uuid4()),
            })
            # Step 1: Disable default Qwen tools that conflict with Sable skills.
            async with _CffiSession(impersonate="chrome", timeout=15) as s1:
                r1 = await s1.post(SETTINGS_URL, json=tools_payload, headers=h)
                d1 = json.loads(r1.content)
                if r1.status_code == 401 or d1.get("data", {}).get("code") == "Unauthorized":
                    logger.warning(f"sync_context: {r1.status_code} Unauthorized — response: {str(d1)[:300]}")
                    return False, True
                if not d1.get("success"):
                    raise Exception(f"Disable tools failed: {d1}")
                logger.debug("Qwen default tools disabled")
            # Step 2: Update personalization instruction (separate session).
            h["X-Request-Id"] = str(uuid.uuid4())
            async with _CffiSession(impersonate="chrome", timeout=15) as s2:
                r2 = await s2.post(SETTINGS_URL, json=instr_payload, headers=h)
                d2 = json.loads(r2.content)
                if not d2.get("success"):
                    raise Exception(f"Update instruction failed: {d2}")
            return True, False

        # First attempt with whatever headers we already hold (disk-cached or
        # freshly minted).
        if headers is None:
            headers = await self.get_fresh_headers()
        try:
            ok, unauthorized = await _attempt(headers)
        except Exception as e:
            logger.error("sync_context failed: %s", e, exc_info=True)
            return False
        if ok:
            logger.debug("sync_context: instruction synced (%d chars)", len(instructions))
            return True

        # Unauthorized → the JWT login cookie is still valid but the Alibaba WAF
        # fingerprint (acw_tc/isg/tfstk) is stale or duplicated, so the settings
        # API returns 200 + {"code":"Unauthorized"}. Re-mint live headers from
        # the browser (deduped on save) and retry EXACTLY once.
        if unauthorized:
            logger.info("sync_context: re-minting WAF headers from live browser and retrying once")
            try:
                fresh = await self.get_fresh_headers()
                from engine.config import (
                    save_qwen_tokens_for_account,
                    _resolve_active_account,
                )
                save_qwen_tokens_for_account(
                    cookies=fresh.get("Cookie", ""),
                    bx_ua=fresh.get("bx-ua", ""),
                    bx_umidtoken=fresh.get("bx-umidtoken", ""),
                    account=_resolve_active_account(),
                    bearer=(fresh.get("Authorization", "") or "").replace("Bearer ", ""),
                )
                ok2, _ = await _attempt(fresh)
                if ok2:
                    logger.info("sync_context: recovered after live WAF header refresh")
                    return True
            except Exception as e:
                logger.error("sync_context self-heal failed: %s", e, exc_info=True)
            return False
        return False

    async def close(self):
        """Cleanly closes context, saving browser profile state."""
        # Stop keepalive first
        # FIX: context.close() and playwright.stop() are coroutines in the async
        # API — the old sync `def close` never awaited them, so the browser
        # process was never actually torn down (silent leak + RuntimeWarning).
        # Riding a live ProfileHub: close ONLY our tab. The hub owns the shared
        # browser (and its playwright), so stopping it here would kill Kimi's /
        # DeepSeek's session too.
        if self._hub is not None:
            try:
                if self._hub_page is not None:
                    await self._hub.close_tab(self._hub_page)
            except Exception:
                pass
            self._hub = None
            self._hub_page = None
            self.playwright = None
            self.context = None
            self.page = None
            self._adopted = False
            return
        # Never close a browser we adopted - it belongs to another launcher.
        if self._adopted:
            if self._cdp_browser is not None:
                try:
                    await self._cdp_browser.close()
                except Exception:
                    pass
            self._cdp_browser = None
            if self.playwright:
                try:
                    await self.playwright.stop()
                except Exception:
                    pass
            self.playwright = None
            self.context = None
            self.page = None
            self._adopted = False
            return
        self._cancel_auto_close()
        # Harden teardown: every step is guarded, and if the browser survives a
        # graceful close we SIGKILL it from the governor slot. Before this, a
        # single raise/hang in context.close() skipped slot.release() entirely
        # and leaked a live headless browser (and its slot) forever — the
        # "ephemeral header browser never dies" leak.
        browser_pid = None
        if self._slot is not None:
            browser_pid = getattr(self._slot, "browser_pid", None)
        try:
            if self.context:
                await self.context.close()
        except Exception as exc:
            logger.warning("context.close() failed for %s: %s",
                           self.user_data_dir, exc)
        finally:
            self.context = None
        try:
            if self.playwright:
                await self.playwright.stop()
        except Exception as exc:
            logger.warning("playwright.stop() failed for %s: %s",
                           self.user_data_dir, exc)
        finally:
            self.playwright = None
        self.page = None
        # Free the governor slot the moment the browser is gone, so the next
        # chat message can claim the capacity immediately. If the process is
        # still breathing after the graceful close, kill it hard first.
        if self._slot is not None:
            try:
                pid = browser_pid or getattr(self._slot, "browser_pid", None)
                if pid and pid_exists(int(pid)):
                    logger.warning(
                        "browser pid %s survived graceful close — killing",
                        pid)
                    from engine import process_utils
                    process_utils.kill_process_tree(int(pid),
                                                    sig=process_utils.SIGKILL)
            except Exception:
                pass
            try:
                self._slot.release(kill=True)
            except Exception:
                pass
            self._slot = None


async def create_new_chat(headers: dict[str, str], model: str | None = None) -> str | None:
    """Create a new upstream Qwen session and return its session ID.

    This returns the **upstream** Qwen session ID, NOT a local Sable chat_id.
    Callers should store it via ``set_upstream_session_id(local_chat_id, result)``.

    `model`, if given, selects which entry from config.MODELS this session is
    created for (falls back to the default MODEL).
    """
    model_id = get_model_config(model)["id"]
    body = {
        "chatId": "",
        "models": [model_id],
        "project_id": "",
        "timestamp": int(time.time() * 1000),
        "chat_type": "t2t",
        "chat_mode": "normal",
    }
    # curl_cffi with impersonate="chrome" — same TLS fix as stream_chat.
    # httpx uses Python/OpenSSL which Alibaba's WAF fingerprints and blocks.
    try:
        from curl_cffi.requests import AsyncSession
        async with AsyncSession(impersonate="chrome", timeout=15) as client:
            res = await client.post(NEW_CHAT_URL, headers=sanitize_fetch_headers(headers), json=body)
        if res.status_code == 200:
            import json as _json
            raw = res.content.decode(errors="replace")
            # Guard against WAF returning HTML captcha page with HTTP 200
            if raw.lstrip().startswith("<"):
                logger.error("WAF captcha challenge on chats/new — tokens likely stale")
                return None
            data = _json.loads(raw)
            if data.get("success"):
                chat_id = data.get("data", {}).get("id")
                logger.debug(f"Server created chat session ID: {chat_id}")
                return chat_id
            logger.error(f"Server refused chat creation: {data}")
        else:
            logger.error(f"HTTP {res.status_code} on chats/new: {res.content.decode(errors='replace')[:300]}")
    except Exception as e:
        logger.error(f"create_new_chat failed: {e}")
    return None