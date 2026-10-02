"""Shared per-profile browser hub for ghost connectors.

Problem
-------
Kimi, DeepSeek, Gemini-web, etc. each need a real browser page so the SPA mints
per-request auth/shield tokens (localStorage bearer JWT, WAF cookies). The naive
design gives each connector its own ``--user-data-dir``, but Chromium allows
only ONE browser process per profile dir (SingletonLock). When Sable's
``provider_accounts`` points several connectors at the SAME profile:

  * each ghost launches its own Helium on that dir -> the loser dies on the lock;
  * each ghost only adopts its OWN marker (``.kimi_ghost.json`` vs
    ``.deepseek_ghost.json``), so neither can see the other's live browser;
  * ``_pid_is_our_helium()`` matches BOTH (same binary + same profile dir), so
    each connector treats the other's browser as its own orphan and kills it.

That is the kill-war the README warns about.

Fix
---
One shared browser per profile dir, multiplexed by TAB. Each connector opens its
own tab on its own origin (localStorage is per-origin, so tokens never clash),
while the browser process + CDP endpoint are shared. A profile-level marker
(``.profile_hub.json``) records the CDP port so ANY process can adopt the live
browser instead of launching a rival.

Usage::

    hub  = await ProfileHub.acquire(profile_dir, helium_bin)
    page = await hub.new_tab("https://www.kimi.ai/")
    ...  # expose_function / evaluate on `page`
    await hub.close_tab(page)   # browser stays up for other connectors
"""
from __future__ import annotations

import asyncio
import json
import logging
import os

from engine import process_utils
from engine.platform_paths import IS_WINDOWS, pid_exists
import socket
import time
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger("sable.profile_hub")

MARKER_NAME = ".profile_hub.json"

_LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-blink-features=AutomationControlled",
    "--disable-infobars",
    "--disable-gpu",
    # A ghost tab is never the focused tab, so Chromium throttles its timers
    # to 1s (then 1min once hidden >5min). That stretched the SPA's sentinel
    # proof-of-work + fetch dispatch to 60-120s BEFORE the request even left
    # the page — the "5 minute send" was almost entirely client-side throttling.
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-features=CalculateNativeWinOcclusion,IntensiveWakeUpThrottling",
]


async def _focus(page) -> None:
    """Bring a ghost tab to the foreground.

    Chromium throttles timer callbacks and fetch dispatch in backgrounded
    tabs. Without this the hijacked ``requestSubmit()`` does not reach the
    network for a minute or more. Best-effort — never fatal.
    """
    try:
        await page.bring_to_front()
    except Exception:
        pass


# ----------------------------------------------------------------------
# Process / lock helpers
# ----------------------------------------------------------------------

def _lock_pid(profile: Path) -> int | None:
    lock = profile / "SingletonLock"
    if not (lock.is_symlink() or lock.exists()):
        return None
    try:
        target = os.readlink(lock)
        _, _, pid_str = target.rpartition("-")
        return int(pid_str)
    except (OSError, ValueError) as exc:
        logger.debug("[hub:%s] unreadable SingletonLock: %s", profile.name, exc)
        return None


def _pid_alive(pid: int) -> bool:
    # os.kill(pid, 0) is a POSIX-only probe; on Windows any os.kill except
    # CTRL_C/CTRL_BREAK *terminates* the target. pid_exists() is cross-platform.
    return pid_exists(pid)


def _cmdline_tokens(pid: int) -> list[str]:
    """Tokenize a process's cmdline.

    ``/proc/<pid>/cmdline`` is *supposed* to be NUL-separated, but some
    launchers (and Helium's own wrapper) rewrite it as a single
    space-separated string with no NUL bytes. Splitting only on ``\0`` then
    yields one giant token and every arg check fails — a silent false
    negative. Normalize NULs to spaces and split on whitespace so both
    layouts parse identically.
    """
    if IS_WINDOWS:
        return []  # no /proc to read; cmdline-based ownership checks don't apply
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    text = raw.replace(b"\0", b" ").decode("utf-8", "replace")
    return text.split()


def _pid_is_our_helium(pid: int, profile_dir: Path, helium_bin: str) -> bool:
    """True if ``pid`` is a Helium browser launched on *this* profile dir.

    Requires BOTH the binary basename AND the exact ``--user-data-dir`` to
    match, so a foreign connector's browser sharing the profile is never
    mistaken for ours.
    """
    toks = _cmdline_tokens(pid)
    if not toks:
        return False
    if Path(toks[0]).name != Path(helium_bin).name:
        return False
    want = {
        f"--user-data-dir={profile_dir}",
        f"--user-data-dir={profile_dir.resolve()}",
    }
    return any(t in want for t in toks)


def _profile_helium_pids(profile_dir: Path, helium_bin: str) -> list[int]:
    """Every Helium pid bound to ``profile_dir`` (main + forked children)."""
    out: list[int] = []
    proc = Path("/proc")
    if not proc.is_dir():
        return out
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            pid = int(entry.name)
            if _pid_is_our_helium(pid, profile_dir, helium_bin):
                out.append(pid)
        except Exception:
            continue
    return out


def _lock_hostname(profile: Path) -> str | None:
    """Host half of the ``hostname-pid`` SingletonLock symlink target."""
    lock = profile / "SingletonLock"
    if not (lock.is_symlink() or lock.exists()):
        return None
    try:
        host, _, _pid = os.readlink(lock).rpartition("-")
        return host or None
    except Exception:
        return None


def _profile_holder_pids(profile_dir: Path) -> list[int]:
    """Every live process bound to ``profile_dir``, whatever its binary.

    Distinguishes a *real* foreign browser (a process genuinely holding the
    profile) from a **recycled PID** — the lock names a pid the kernel later
    handed to an unrelated process, which is how a long-dead browser could
    wedge the profile behind a bogus "foreign browser" error.
    """
    want = {
        f"--user-data-dir={profile_dir}",
        f"--user-data-dir={profile_dir.resolve()}",
    }
    out: list[int] = []
    proc = Path("/proc")
    if not proc.is_dir():
        return out
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            pid = int(entry.name)
            if any(t in want for t in _cmdline_tokens(pid)):
                out.append(pid)
        except Exception:
            continue
    return out


def _lock_is_stale(profile: Path) -> bool:
    """True when SingletonLock is junk and safe to delete.

    Junk means: unparsable pid, dead pid, lock minted on another host, or —
    the case that used to wedge us — **no process actually holds the
    profile**, i.e. the pid was recycled onto something unrelated.
    """
    pid = _lock_pid(profile)
    if pid is None or not _pid_alive(pid):
        return True
    host = _lock_hostname(profile)
    if host and host != socket.gethostname():
        return True
    return not _profile_holder_pids(profile)


def _devtools_port(profile: Path) -> int | None:
    """Port from Chromium's ``DevToolsActivePort`` file, if present.

    Standard chromium writes this whenever remote debugging is on. Helium
    does **not** (verified: its profile dir only ever holds ``Singleton*``),
    so this is a bonus source, not the primary one — ``_cmdline_cdp_ports``
    covers Helium.
    """
    try:
        first = (profile / "DevToolsActivePort").read_text(
            encoding="utf-8", errors="replace").splitlines()[0]
        port = int(first.strip())
    except Exception:
        return None
    return port if port > 0 else None


def _cmdline_cdp_ports(pids: list[int]) -> list[int]:
    """Explicit ``--remote-debugging-port`` values from the holders' cmdlines.

    The reliable source for Helium, which writes no port file. A flag of
    ``=0`` means "pick a free port", which the listening-port sweep resolves.
    """
    ports: list[int] = []
    for pid in pids:
        for tok in _cmdline_tokens(pid):
            if not tok.startswith("--remote-debugging-port="):
                continue
            try:
                port = int(tok.split("=", 1)[1])
            except ValueError:
                continue
            if port > 0 and port not in ports:
                ports.append(port)
    return ports


def _listening_ports(pids: list[int]) -> set[int]:
    """TCP ports ``pids`` are LISTENing on, via /proc/net/tcp{,6} + fd inodes."""
    inodes: set[str] = set()
    for pid in pids:
        try:
            for fd in Path(f"/proc/{pid}/fd").iterdir():
                try:
                    link = os.readlink(fd)
                except OSError:
                    continue
                if link.startswith("socket:["):
                    inodes.add(link[8:-1])
        except OSError:
            continue
    if not inodes:
        return set()
    ports: set[int] = set()
    for proc_file in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(proc_file).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            parts = line.split()
            if len(parts) < 10 or parts[3] != "0A" or parts[9] not in inodes:
                continue
            try:
                ports.add(int(parts[1].split(":")[1], 16))
            except Exception:
                continue
    return ports


def _pid_is_headless(pid: int) -> bool | None:
    """True = headless, False = headed, None = cmdline unreadable."""
    toks = _cmdline_tokens(pid)
    if not toks:
        return None
    for tok in toks:
        if tok == "--headless" or tok.startswith("--headless="):
            return True
        if tok.startswith("--ozone-platform=headless"):
            return True
    return False


def _profile_is_headless(profile_dir: Path) -> bool:
    """True if any live holder of ``profile_dir`` is a headless browser."""
    return any(_pid_is_headless(p) is True
               for p in _profile_holder_pids(profile_dir))


def _clear_singleton(profile: Path) -> None:
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            (profile / name).unlink(missing_ok=True)
        except OSError:
            pass


async def _kill_profile_holders(profile_dir: Path,
                                grace: float = 0.6) -> int:
    """SIGTERM then SIGKILL every process bound to ``profile_dir``.

    Module-level (no hub instance needed) so both ``ProfileHub.acquire``
    retries and connector code can force a clean slate. Any binary, headed
    or headless — the caller has already decided this profile must be freed.
    Returns the number of victims found.
    """
    victims = _profile_holder_pids(profile_dir)
    for vp in victims:
        process_utils.kill_process_tree(vp)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not any(_pid_alive(vp) for vp in victims):
            break
        await asyncio.sleep(0.1)
    for vp in victims:
        if _pid_alive(vp):
            process_utils.kill_process_tree(vp, sig=process_utils.SIGKILL)
    await asyncio.sleep(0.2)
    _clear_singleton(profile_dir)
    # Free this profile's governor slots too. A killed browser whose slot was
    # never attached (or never released) would otherwise keep counting against
    # MAX_BROWSERS, blocking the relaunch until the outer acquire times out.
    try:
        from engine import browser_governor as _gov
        _gov.release_slots_for_profile(profile_dir)
    except Exception:
        pass
    return len(victims)


# ----------------------------------------------------------------------
# The hub
# ----------------------------------------------------------------------

class ProfileHub:
    """Owns (or adopts) the ONE browser for a profile dir."""

    _hubs: dict[str, "ProfileHub"] = {}
    _registry_lock: asyncio.Lock | None = None

    # -- factory -------------------------------------------------------

    @classmethod
    async def acquire(cls, profile_dir: Path, helium_bin: str,
                      headless: bool = True,
                      force_headed: bool = False,
                      max_retries: int = 3) -> "ProfileHub":
        """Return the live hub for ``profile_dir``, launching it if needed.

        ``force_headed=True`` requests a *visible* browser: any headless
        browser currently holding the profile is killed first, and the
        replacement is launched with ``headless=False``.

        On any failure the cached hub is evicted, **every browser bound to
        the profile is killed**, and acquisition is retried up to
        ``max_retries`` times. This is what makes a closed/stale/already-used
        context self-heal instead of failing hard.
        """
        if cls._registry_lock is None:
            cls._registry_lock = asyncio.Lock()
        key = str(Path(profile_dir).resolve())
        async with cls._registry_lock:
            hub = cls._hubs.get(key)
            if hub is not None and hub.is_alive:
                # A cached headless hub cannot satisfy a headed request —
                # tear it down and build a fresh headed one.
                if not (force_headed and hub.headless):
                    return hub
                await hub._teardown()
            elif hub is not None:
                # Cached hub is dead (context closed / browser gone) — drop it.
                await hub._teardown()
            cls._hubs.pop(key, None)

            last_exc: Exception | None = None
            for attempt in range(1, max_retries + 1):
                hub = cls(Path(profile_dir), helium_bin, headless,
                          force_headed=force_headed)
                try:
                    await hub._ensure()
                    cls._hubs[key] = hub
                    return hub
                except Exception as exc:
                    last_exc = exc
                    logger.warning(
                        "[hub:%s] acquire attempt %d/%d failed: %s: %s — "
                        "killing all browsers on profile & retrying",
                        Path(profile_dir).name, attempt, max_retries,
                        type(exc).__name__, exc)
                    try:
                        await hub._teardown()
                    except Exception:
                        pass
                    try:
                        killed = await _kill_profile_holders(Path(profile_dir))
                        logger.info("[hub:%s] killed %d holder(s) on retry",
                                    Path(profile_dir).name, killed)
                    except Exception as kill_exc:
                        logger.warning("[hub:%s] kill-holders failed: %s",
                                       Path(profile_dir).name, kill_exc)
                    if attempt < max_retries:
                        await asyncio.sleep(min(0.5 * attempt, 2.0))
            raise RuntimeError(
                f"profile {Path(profile_dir).name}: could not acquire a live "
                f"browser after {max_retries} attempts") from last_exc

    @classmethod
    def get_live(cls, profile_dir: Path) -> "ProfileHub | None":
        """Return the cached hub for ``profile_dir`` IF it is still alive.

        Non-mutating and non-launching — lets a co-tenant (the shared
        BrowserManager) reuse an already-running browser on the same profile
        instead of spawning a rival. Returns None when nothing is cached or the
        cached hub's context has died.
        """
        key = str(Path(profile_dir).resolve())
        hub = cls._hubs.get(key)
        if hub is None:
            return None
        try:
            return hub if hub.is_alive else None
        except Exception:
            return None

    @classmethod
    async def evict(cls, profile_dir: Path) -> None:
        """Drop the cached hub for ``profile_dir`` and tear its state down.

        Safe when nothing is cached. Tears down only *our* link — it does not
        kill the browser (use ``_kill_profile_holders`` for that).
        """
        if cls._registry_lock is None:
            cls._registry_lock = asyncio.Lock()
        key = str(Path(profile_dir).resolve())
        async with cls._registry_lock:
            hub = cls._hubs.pop(key, None)
        if hub is not None:
            try:
                await hub._teardown()
            except Exception:
                pass

    def __init__(self, profile_dir: Path, helium_bin: str, headless: bool,
                 force_headed: bool = False):
        self.profile_dir = profile_dir
        self.helium_bin = helium_bin
        # force_headed wins over the caller's headless flag: it means "I need
        # a visible window", so the effective launch mode is headed.
        self.force_headed = force_headed
        self.headless = False if force_headed else headless
        self._pw = None
        self._context = None
        self._cdp_browser = None
        self._tabs: set = set()
        self._marker = profile_dir / MARKER_NAME
        # False when we adopted someone else's browser: teardown must drop the
        # CDP link, never close a browser we don't own.
        self._owns_browser = False
        # browser_governor.Slot held while this hub owns a browser.
        self._slot = None

    # -- state ---------------------------------------------------------

    @property
    def context(self):
        return self._context

    @property
    def is_alive(self) -> bool:
        if self._context is None:
            return False
        try:
            # Touching .pages raises if the underlying browser/context died —
            # closed externally, crashed, or the CDP link dropped. A non-None
            # but dead context is the #1 cause of "ghost tab chat fails hard".
            _ = self._context.pages
            return True
        except Exception:
            return False

    # -- lifecycle -----------------------------------------------------

    async def _ensure(self) -> None:
        if not self.profile_dir.exists():
            raise RuntimeError(f"profile dir missing: {self.profile_dir}")
        # force_headed: kill EVERY browser currently on this profile — any
        # binary, headed or headless — then launch a fresh HEADED one. No
        # adoption: the caller wants the old browsers gone, not reused.
        if self.force_headed:
            killed = await self._kill_all_holders()
            logger.info("[hub:%s] force-headed: killed %d holder(s) — launching "
                        "a fresh headed browser", self.profile_dir.name, killed)
            try:
                await self._launch()
            except Exception:
                if await self._adopt():
                    return
                raise
            return
        if await self._adopt():
            return
        await self._free_profile()
        try:
            await self._launch()
        except Exception:
            # Lost a cross-process launch race — the winner's browser should be
            # live now; adopt it instead of failing.
            if await self._adopt():
                return
            raise

    async def _adopt(self) -> bool:
        """Attach to a live CDP browser on this profile, whoever launched it.

        Candidate ports, in order:
          1. our own marker (``.profile_hub.json``) — fastest, no probing;
          2. the holders' ``--remote-debugging-port`` flag — the only source
             that works for Helium, which writes no ``DevToolsActivePort``;
          3. ``DevToolsActivePort`` — standard chromium, kept as a bonus;
          4. a listening-port sweep of the holders' fds — resolves ``=0``
             (random port) launches and anything the flags miss.

        A dead pid in the lock means there is nothing to adopt, so bail early;
        a *stale* lock (recycled pid) falls through to ``_free_profile`` once
        every candidate port refuses the connection.
        """
        pid = _lock_pid(self.profile_dir)
        if pid is not None and not _pid_alive(pid):
            self._marker.unlink(missing_ok=True)
            return False

        holders = _profile_holder_pids(self.profile_dir)

        ports: list[int] = []
        if self._marker.exists():
            try:
                ports.append(int(json.loads(self._marker.read_text())["port"]))
            except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
                logger.debug("[hub:%s] marker unreadable, ignoring: %s",
                             self.profile_dir.name, exc)
        for cp in _cmdline_cdp_ports(holders):
            if cp not in ports:
                ports.append(cp)
        dp = _devtools_port(self.profile_dir)
        if dp is not None and dp not in ports:
            ports.append(dp)
        for lp in sorted(_listening_ports(holders)):
            if lp not in ports:
                ports.append(lp)

        if not ports:
            self._marker.unlink(missing_ok=True)
            return False

        from playwright.async_api import async_playwright
        last_exc: Exception | None = None
        for port in ports:
            try:
                self._pw = await async_playwright().start()
                self._cdp_browser = await self._pw.chromium.connect_over_cdp(
                    f"http://127.0.0.1:{port}", timeout=8000)
                ctxs = self._cdp_browser.contexts
                if not ctxs:
                    raise RuntimeError("no contexts over CDP")
                self._context = ctxs[0]
                self._owns_browser = False
                # Sync the mode flag with reality so a later force_headed
                # acquire can spot an adopted headless browser via hub.headless.
                self.headless = _profile_is_headless(self.profile_dir)
                logger.info("[hub:%s] adopted live browser over CDP (port %d)",
                            self.profile_dir.name, port)
                return True
            except Exception as exc:
                last_exc = exc
                try:
                    if self._pw is not None:
                        await self._pw.stop()
                except Exception:
                    pass
                self._pw = None
                self._cdp_browser = None
                continue

        logger.info("[hub:%s] CDP adopt failed on ports %s (%s) — relaunching",
                    self.profile_dir.name, ports, last_exc)
        self._marker.unlink(missing_ok=True)
        return False

    async def _free_profile(self) -> None:
        """Clear a stale lock, or reclaim our own orphan (same helium binary on
        this profile). A genuinely foreign browser is left alone — the hub is
        the single owner of the profile, so it cannot proceed past one."""
        pid = _lock_pid(self.profile_dir)
        if pid is not None and not _lock_is_stale(self.profile_dir):
            if _pid_is_our_helium(pid, self.profile_dir, self.helium_bin):
                await self._reclaim()
                return
            raise RuntimeError(
                f"profile {self.profile_dir.name} is held by foreign browser "
                f"pid {pid} that isn't our helium binary — refusing to kill it")
        # Dead pid, foreign host, or a recycled pid with no real holder — the
        # lock is junk, not a live browser. Clear it and move on.
        _clear_singleton(self.profile_dir)
        self._marker.unlink(missing_ok=True)

    async def _kill_all_holders(self) -> int:
        """Kill every process bound to this profile dir — any binary, headed
        or headless — then clear the lock/marker so a fresh launch owns it.

        Used by ``force_headed``, where the caller explicitly wants every
        browser already on the profile terminated before we open ours.
        """
        victims = _profile_holder_pids(self.profile_dir)
        for vp in victims:
            process_utils.kill_process_tree(vp)
        for _ in range(50):
            if not any(_pid_alive(vp) for vp in victims):
                break
            await asyncio.sleep(0.1)
        for vp in victims:
            if _pid_alive(vp):
                process_utils.kill_process_tree(vp, sig=process_utils.SIGKILL)
        await asyncio.sleep(0.2)
        _clear_singleton(self.profile_dir)
        self._marker.unlink(missing_ok=True)
        # Drop governor slots bound to this profile so the fresh headed launch
        # isn't blocked by a slot whose browser we just killed.
        try:
            from engine import browser_governor as _gov
            _gov.release_slots_for_profile(self.profile_dir)
        except Exception:
            pass
        return len(victims)

    async def _reclaim(self) -> None:
        victims = _profile_helium_pids(self.profile_dir, self.helium_bin)
        for vp in victims:
            process_utils.kill_process_tree(vp)
        for _ in range(30):
            if not any(_pid_alive(vp) for vp in victims):
                break
            await asyncio.sleep(0.1)
        for vp in victims:
            if _pid_alive(vp):
                process_utils.kill_process_tree(vp, sig=process_utils.SIGKILL)
        await asyncio.sleep(0.2)
        _clear_singleton(self.profile_dir)
        self._marker.unlink(missing_ok=True)
        logger.info("[hub:%s] reclaimed %d orphaned process(es)",
                    self.profile_dir.name, len(victims))

    async def _launch(self) -> None:
        from playwright.async_api import async_playwright
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        # Write the marker BEFORE launch so a crash mid-launch still records
        # our port and the next process can adopt instead of orphaning.
        try:
            self._marker.write_text(json.dumps({"port": port, "ts": time.time()}))
        except OSError as exc:
            # Non-fatal, but a failed marker write means the next process
            # can't adopt this browser — worth knowing about.
            logger.warning("[hub:%s] could not write adopt marker: %s",
                           self.profile_dir.name, exc)
        # Fleet-wide admission control, shared with every other Sable browser
        # path. Playwright starts the process for us here, so the pid is
        # resolved afterwards from the profile dir rather than from Popen.
        from engine import browser_governor
        # CHAT priority: a hub browser is the user's live ghost-tab session.
        # It outranks ephemeral header-refresh browsers, so a token mint can
        # never evict it.
        slot = await browser_governor.acquire(
            f"hub:{self.profile_dir.name}",
            priority=browser_governor.PRIORITY_CHAT,
        )

        self._pw = await async_playwright().start()
        # A headed launch needs a display. On a host with none (server / WSL),
        # borrow or start an Xvfb via the shared helper — same path the shared
        # BrowserManager uses — so headed ghosts work everywhere.
        launch_kwargs: dict = dict(
            executable_path=self.helium_bin,
            user_data_dir=str(self.profile_dir),
            headless=self.headless,
            args=_LAUNCH_ARGS + [f"--remote-debugging-port={port}"],
        )
        if not self.headless:
            try:
                from engine.session import _browser_env_with_display
                _env = _browser_env_with_display()
                if _env:
                    launch_kwargs["env"] = _env
                elif not (os.environ.get("DISPLAY")
                          or os.environ.get("WAYLAND_DISPLAY")):
                    # Only a genuine absence of a display is worth warning about.
                    # _browser_env_with_display() also returns None when the
                    # current env already has a usable DISPLAY (inherit case) —
                    # that is the happy path, not a problem.
                    logger.warning("[hub:%s] no DISPLAY for headed launch",
                                   self.profile_dir.name)
            except Exception as exc:
                logger.warning("[hub:%s] display setup failed: %s",
                               self.profile_dir.name, exc)
        try:
            self._context = await self._pw.chromium.launch_persistent_context(
                **launch_kwargs)
        except BaseException:
            slot.release()
            raise

        self._owns_browser = True

        # Bind whichever root process owns this profile, then confine it.
        roots = browser_governor.find_browser_pids(str(self.profile_dir))
        if roots:
            slot.attach(roots[0], user_data_dir=str(self.profile_dir))
        else:
            logger.warning("[hub:%s] launched but no browser pid found to confine",
                           self.profile_dir.name)
        self._slot = slot

        logger.info("[hub:%s] launched shared browser (port %d)",
                    self.profile_dir.name, port)

    def _touch(self) -> None:
        """Mark this hub's browser as recently used (governor idle-eviction
        guard). Cheap and best-effort — never let it break a tab operation."""
        try:
            pid = getattr(self._slot, "browser_pid", None) if self._slot else None
            if pid:
                from engine import browser_governor as _gov
                _gov.touch(int(pid))
        except Exception:
            pass

    # -- tabs ----------------------------------------------------------

    async def new_tab(self, url: str):
        """Open (or reuse) a tab on ``url``'s origin. Reuses an existing tab on
        the same host so re-acquires don't pile up duplicate tabs."""
        if self._context is None:
            raise RuntimeError("hub has no browser context")
        self._touch()
        want_host = urlparse(url).netloc
        for p in self._context.pages:
            try:
                if not p.is_closed() and urlparse(p.url).netloc == want_host:
                    self._tabs.add(p)
                    await _focus(p)
                    return p
            except Exception:
                continue
        page = await self._context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            # Give the SPA time to hydrate auth state (silent token refresh via
            # cookies). Too short and has_token() polls against an empty
            # localStorage; too long wastes startup. 3s is a safe middle ground.
            await page.wait_for_timeout(3000)
            await _focus(page)
        except Exception:
            try:
                await page.close()
            except Exception:
                pass
            raise
        self._tabs.add(page)
        return page

    async def close_tab(self, page) -> None:
        self._tabs.discard(page)
        try:
            if page is not None and not page.is_closed():
                await page.close()
        except Exception:
            pass

    async def _teardown(self) -> None:
        for page in list(self._tabs):
            try:
                if not page.is_closed():
                    await page.close()
            except Exception:
                pass
        self._tabs.clear()
        # Only close the browser if WE launched it. An adopted browser belongs
        # to another launcher — closing it here would kill their session.
        if self._owns_browser:
            try:
                if self._cdp_browser is not None:
                    await self._cdp_browser.close()
            except Exception:
                pass
            try:
                if self._context is not None:
                    await self._context.close()
            except Exception:
                pass
        try:
            if self._pw is not None:
                await self._pw.stop()
        except Exception:
            pass
        # Don't delete a marker we didn't write — it may be another Sable
        # process's adopt handle.
        if self._owns_browser:
            self._marker.unlink(missing_ok=True)
        self._owns_browser = False
        self._context = None
        self._pw = None
        self._cdp_browser = None
        # Hand the fleet slot back so another browser can start, and drop the
        # per-browser cgroup. No-op when no slot was ever claimed.
        if self._slot is not None:
            try:
                self._slot.release(kill=False)
            except Exception:
                pass
            self._slot = None
