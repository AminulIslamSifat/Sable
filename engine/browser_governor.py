"""engine/browser_governor.py — priority-aware, cross-process limits on Sable's browser fleet.

Two guarantees, enforced for EVERY browser Sable spawns:

  1. At most ``MAX_BROWSERS`` browser instances exist at once, per Sable
     process. The stable install and a dev tree can both be running, so an
     in-process semaphore would let each hold its own quota — the cap is kept
     in a shared slot directory instead.

  2. Slots carry a PRIORITY. ``chat`` browsers (ghost tabs, scraper sessions)
     are long-lived and outrank ``ephemeral`` ones (header / token refreshes).
     When the cap is full a chat claim evicts the least-recently-used
     ephemeral browser instead of waiting; an ephemeral claim never evicts
     anything — it waits.

There is deliberately NO memory-based or idle-based killing here. A browser
dies when the code that owns it says so (job done -> close), or when it is an
orphan (owner gone). Each browser is still tagged with a cgroup v2 label so
orphan detection can tell "Sable spawned this" apart from an unrelated
Chromium — but the cgroup carries no ``memory.max`` and no ``oom.group``, so
the kernel never OOM-kills a browser tree behind our back.

Everything degrades gracefully: if cgroups are unavailable the concurrency
cap still holds and a warning is logged once.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import shutil
import signal
import time
import uuid
from pathlib import Path

logger = logging.getLogger("sable.browser_governor")

# ── Tunables (env-overridable) ──────────────────────────────────────────────
# Per-Sable-instance concurrency cap: each running Sable server may hold at
# most this many browsers at once. Enforcement is per server process (slots are
# matched by owner_pid), so a second install / dev tree on the same host no
# longer steals this server's slots and starves its relaunches.
MAX_BROWSERS = int(os.environ.get("SABLE_MAX_BROWSERS", "2"))
# Fleet-wide concurrency backstop across every Sable server on the host. Kept
# only so the shared cgroup label tree can't grow without bound; it is NOT a
# per-instance cap and it never kills a browser by itself.
MAX_BROWSERS_GLOBAL = int(os.environ.get("SABLE_MAX_BROWSERS_GLOBAL", "8"))
ACQUIRE_TIMEOUT = float(os.environ.get("SABLE_BROWSER_SLOT_TIMEOUT", "120"))

# Slot priorities. A chat browser is the user's live connection and is never
# evicted to make room; an ephemeral browser exists to mint a token and dies.
PRIORITY_CHAT = "chat"
PRIORITY_EPHEMERAL = "ephemeral"

_RUNTIME = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp")
_SLOT_DIR = _RUNTIME / "sable-browser-slots"

_cgroup_warned = False


# ── process helpers ────────────────────────────────────────────────────────

def _pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _cmdline(pid: int) -> str:
    try:
        return (Path(f"/proc/{pid}/cmdline").read_bytes()
                .decode(errors="replace").replace("\0", " "))
    except Exception:
        return ""


def _is_sable_confined(pid: int) -> bool:
    """True iff *pid* lives in Sable's own browser cgroup.

    This is the definitive "Sable spawned this" marker: ``_confine()`` moves
    every browser Sable launches into
    ``<delegated>/sable-browsers/browser-<pid>``. A process in any other
    cgroup — a user session scope, a manually-started automation daemon, an
    outside Chromium — was NOT spawned by Sable and must never be reaped, no
    matter what its command line happens to contain.
    """
    try:
        return "/sable-browsers/" in Path(f"/proc/{pid}/cgroup").read_text()
    except Exception:
        return False


def _proc_age(pid: int) -> float:
    """Seconds since *pid* started, or -1.0 if it can't be determined."""
    try:
        btime = None
        with open("/proc/stat") as f:
            for line in f:
                if line.startswith("btime "):
                    btime = float(line.split()[1])
                    break
        if btime is None:
            return -1.0
        stat = Path(f"/proc/{pid}/stat").read_text()
        fields = stat[stat.rfind(")") + 2:].split()
        start_ticks = float(fields[19])  # field 22 = starttime
        hz = os.sysconf("SC_CLK_TCK")
        return time.time() - (btime + start_ticks / hz)
    except Exception:
        return -1.0


def find_browser_pids(user_data_dir: str, *, roots_only: bool = True) -> list[int]:
    """Every live browser process bound to *user_data_dir*.

    ``roots_only`` skips Chromium's ``--type=`` helpers and returns just the
    process-group leaders, which is what you want for cgroup placement and
    for process-group kills.
    """
    try:
        target = os.path.realpath(user_data_dir)
    except Exception:
        return []
    if not target:
        return []

    out: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        cmd = _cmdline(pid)
        if not cmd or "--user-data-dir=" not in cmd:
            continue
        if roots_only and "--type=" in cmd:
            continue
        for tok in cmd.split(" "):
            if not tok.startswith("--user-data-dir="):
                continue
            try:
                if os.path.realpath(tok.split("=", 1)[1]) == target:
                    out.append(pid)
            except Exception:
                pass
            break
    return out


def _kill_tree(pid: int, *, sig: int = signal.SIGTERM) -> None:
    try:
        os.killpg(os.getpgid(pid), sig)
    except Exception:
        try:
            os.kill(pid, sig)
        except Exception:
            pass


# ── cgroup v2 ──────────────────────────────────────────────────────────────

def _cgroup_base() -> Path | None:
    uid = os.getuid()
    cand = Path(f"/sys/fs/cgroup/user.slice/user-{uid}.slice/user@{uid}.service")
    try:
        if cand.is_dir() and os.access(cand, os.W_OK):
            return cand
    except Exception:
        pass
    return None


def _ensure_cgroup_root() -> Path | None:
    """Create ``<delegated>/sable-browsers`` with the memory controller on."""
    global _cgroup_warned
    base = _cgroup_base()
    if base is None:
        if not _cgroup_warned:
            logger.warning("cgroup v2 not delegated — memory cap disabled")
            _cgroup_warned = True
        return None

    root = base / "sable-browsers"
    try:
        root.mkdir(exist_ok=True)
        # Enable the memory controller for children. The "no internal process"
        # rule is satisfied because we never place a process in this cgroup —
        # only in its per-browser children.
        st = root / "cgroup.subtree_control"
        if st.exists():
            cur = st.read_text().strip()
            if "memory" not in cur:
                try:
                    st.write_text("+memory")
                except Exception as exc:
                    if not _cgroup_warned:
                        logger.warning("could not enable memory controller: %s", exc)
                        _cgroup_warned = True
        # No memory.max / oom.group here on purpose: the cgroup is an ownership
        # LABEL, not a kill switch. Sable decides when a browser dies.
        return root
    except Exception as exc:
        if not _cgroup_warned:
            logger.warning("cgroup setup failed: %s", exc)
            _cgroup_warned = True
        return None


def _confine(pid: int) -> str | None:
    """Tag *pid* into Sable's browser cgroup. Returns the cgroup path.

    This is a LABEL, not a leash: no ``memory.max``, no ``memory.oom.group``.
    Its only job is to make ``_is_sable_confined`` — and therefore orphan
    detection — reliable, so ``reap_orphans`` can tell a browser Sable
    launched from an unrelated Chromium on a similar profile path.
    """
    root = _ensure_cgroup_root()
    if root is None:
        return None
    cg = root / f"browser-{pid}"
    try:
        cg.mkdir(exist_ok=True)
        (cg / "cgroup.procs").write_text(str(pid))
        return str(cg)
    except Exception as exc:
        logger.warning("cgroup tagging failed for pid %d: %s", pid, exc)
        return None


def _profile_has_live_cgroup(user_data_dir: str) -> bool:
    """True if any confined Sable browser is currently alive for this profile.

    Reads the cgroup tree under ``sable-browsers/`` and matches by the
    ``user_data_dir`` recorded in each browser's cmdline. Used as a guard
    before pruning a never-attached slot: a slow attach (large session
    restore) must not be mistaken for an abandoned launch.
    """
    if not user_data_dir:
        return False
    try:
        want = os.path.realpath(user_data_dir)
    except Exception:
        return False
    cg = _cgroup_base()
    if cg is None:
        return False
    browsers = cg / "sable-browsers"
    if not browsers.is_dir():
        return False
    for entry in browsers.iterdir():
        procs = entry / "cgroup.procs"
        if not procs.exists():
            continue
        try:
            pids = [int(x) for x in procs.read_text().split() if x.isdigit()]
        except Exception:
            continue
        for pid in pids:
            try:
                cmd = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            except Exception:
                continue
            for part in cmd:
                if not part.startswith(b"--user-data-dir="):
                    continue
                try:
                    udd = os.path.realpath(part.split(b"=", 1)[1].decode())
                except Exception:
                    continue
                if udd == want:
                    return True
    return False


def _release_cgroup(cg_path: str | None) -> None:
    if not cg_path:
        return
    try:
        Path(cg_path).rmdir()
    except Exception:
        pass


# ── slot table (cross-process concurrency cap) ─────────────────────────────

def _prune_dead_locked() -> int:
    """Delete slot files whose owning process is gone. Caller holds the lock."""
    removed = 0
    now = time.time()
    # A slot that never bound a browser_pid within this window is an abandoned
    # launch (find_browser_pids returned empty, launch crashed before attach),
    # NOT a live browser. Without this, a never-attached slot pins MAX_BROWSERS
    # forever on the still-alive owner_pid — wedging every future relaunch.
    #
    # NOTE: the grace must be generous. A cold profile with a large session
    # restore can take >60s to reach attach(). If we prune mid-launch, the
    # next _try_claim sees a free slot, launches a SECOND browser on the same
    # profile, and we get a SingletonLock purge war (kill_existing=True) that
    # manifests as an endless kill/relaunch storm.
    attach_grace = float(os.environ.get("SABLE_SLOT_ATTACH_GRACE", "180"))
    for f in _SLOT_DIR.glob("slot-*.json"):
        try:
            data = json.loads(f.read_text())
        except Exception:
            f.unlink(missing_ok=True)
            removed += 1
            continue
        bpid = data.get("browser_pid")
        if bpid is None:
            age = now - float(data.get("ts", 0) or 0)
            if age <= attach_grace:
                continue
            # Before pruning, double-check no live Sable browser is already
            # confined for this profile — an attach may just be slow.
            if _profile_has_live_cgroup(str(data.get("user_data_dir") or "")):
                continue
            f.unlink(missing_ok=True)
            removed += 1
            continue
        if not _pid_alive(int(bpid or 0)):
            f.unlink(missing_ok=True)
            removed += 1
    return removed


def release_slots_for_profile(user_data_dir) -> int:
    """Delete every slot file bound to ``user_data_dir``.

    Call AFTER killing a profile's browsers (``_kill_profile_holders``) so the
    relaunch is not blocked by the old slot still counting against
    ``MAX_BROWSERS``. Matching is by realpath of the recorded user_data_dir, so
    a slot whose browser_pid was never attached (the leak that pinned the
    fleet) is also cleared.
    """
    try:
        want = os.path.realpath(str(user_data_dir))
    except Exception:
        return 0
    removed = 0
    lock_path = _SLOT_DIR / ".lock"
    try:
        _SLOT_DIR.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                for f in _SLOT_DIR.glob("slot-*.json"):
                    try:
                        data = json.loads(f.read_text())
                    except Exception:
                        continue
                    udd = data.get("user_data_dir")
                    if not udd:
                        continue
                    try:
                        if os.path.realpath(str(udd)) != want:
                            continue
                    except Exception:
                        continue
                    f.unlink(missing_ok=True)
                    removed += 1
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)
    except Exception as exc:
        logger.warning("release_slots_for_profile failed (%s)", exc)
    if removed:
        logger.info("released %d stale slot(s) for profile %s",
                    removed, os.path.basename(want))
    return removed


def _own_slots_locked(owner_pid: int) -> list[tuple[Path, dict]]:
    """Slot files owned by THIS Sable process. Caller holds the lock."""
    out: list[tuple[Path, dict]] = []
    for f in _SLOT_DIR.glob("slot-*.json"):
        try:
            data = json.loads(f.read_text())
        except Exception:
            continue
        if int(data.get("owner_pid") or 0) == owner_pid:
            out.append((f, data))
    return out


def _slot_last_used(data: dict) -> float:
    """Best-effort 'when was this browser last touched' timestamp."""
    for k in ("last_used", "ts"):
        try:
            v = float(data.get(k) or 0)
            if v > 0:
                return v
        except (TypeError, ValueError):
            continue
    return 0.0


def _evict_slot_locked(path: Path, data: dict) -> bool:
    """Kill the browser behind one slot and delete the slot file.

    Caller holds the lock. Kills the whole process tree (SIGKILL), drops the
    cgroup, and removes the slot — so the freed capacity is immediately
    claimable by the caller's relaunch.
    """
    bpid = data.get("browser_pid")
    if bpid and _pid_alive(int(bpid)):
        logger.info("evicting idle browser pid %d (owner=%s)",
                    int(bpid), data.get("owner"))
        _kill_tree(int(bpid), sig=signal.SIGKILL)
    cg = _cgroup_base()
    if cg is not None and bpid:
        _release_cgroup(str(cg / "sable-browsers" / f"browser-{int(bpid)}"))
    try:
        path.unlink(missing_ok=True)
    except Exception:
        return False
    return True


def _slot_priority(data: dict) -> str:
    """Priority of a slot; anything unlabelled counts as ephemeral."""
    p = str(data.get("priority") or "").strip().lower()
    return PRIORITY_CHAT if p == PRIORITY_CHAT else PRIORITY_EPHEMERAL


def _evict_ephemeral_own_locked(owner_pid: int) -> bool:
    """Evict the least-recently-used EPHEMERAL browser owned by this Sable.

    Called only when a ``chat`` claim needs room. Chat browsers are the user's
    live sessions and are NEVER evicted; a token-mint browser is killed first,
    oldest first. If every slot we own is a chat browser this returns False and
    the caller waits instead of tearing a chat down.

    Only touches this process's own slots, so we never kill another Sable
    server's browser. Caller holds the lock.
    """
    candidates = [
        (f, d) for (f, d) in _own_slots_locked(owner_pid)
        if d.get("browser_pid")
        and _slot_priority(d) == PRIORITY_EPHEMERAL
    ]
    if not candidates:
        return False
    candidates.sort(key=lambda fd: _slot_last_used(fd[1]))  # oldest first
    for f, d in candidates:
        if _evict_slot_locked(f, d):
            return True
    return False


def touch(browser_pid: int) -> None:
    """Mark a browser as recently used so it isn't picked for idle eviction.

    Call on tab open / activity. Cheap: one flock + one file rewrite.
    """
    if not browser_pid:
        return
    lock_path = _SLOT_DIR / ".lock"
    try:
        with open(lock_path, "w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                for f in _SLOT_DIR.glob("slot-*.json"):
                    try:
                        data = json.loads(f.read_text())
                    except Exception:
                        continue
                    if int(data.get("browser_pid") or 0) != browser_pid:
                        continue
                    data["last_used"] = time.time()
                    try:
                        f.write_text(json.dumps(data))
                    except Exception:
                        pass
                    return
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)
    except Exception:
        pass


def _try_claim(owner: str, priority: str = PRIORITY_EPHEMERAL) -> "Slot | None":
    priority = PRIORITY_CHAT if priority == PRIORITY_CHAT else PRIORITY_EPHEMERAL
    try:
        _SLOT_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        # Can't build the shared table — fall back to allowing the launch
        # rather than deadlocking the app. Better to run than to wedge.
        return Slot(f"unmanaged-{uuid.uuid4().hex[:8]}", None, owner, priority)

    me = os.getpid()
    lock_path = _SLOT_DIR / ".lock"
    try:
        with open(lock_path, "w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                _prune_dead_locked()
                # Per-INSTANCE cap: only this process's slots count. A second
                # Sable server on the host can no longer consume our quota.
                if len(_own_slots_locked(me)) >= MAX_BROWSERS:
                    # Full. A CHAT claim outranks every ephemeral browser, so
                    # it may kill one to make room. An EPHEMERAL claim never
                    # evicts anything — it waits its turn rather than tear
                    # down a live chat session just to mint a token.
                    if priority != PRIORITY_CHAT:
                        return None
                    if not _evict_ephemeral_own_locked(me):
                        return None
                    if len(_own_slots_locked(me)) >= MAX_BROWSERS:
                        return None
                slot_id = f"{me}-{uuid.uuid4().hex[:8]}"
                path = _SLOT_DIR / f"slot-{slot_id}.json"
                now = time.time()
                path.write_text(json.dumps({
                    "owner": owner,
                    "owner_pid": me,
                    "browser_pid": None,
                    "priority": priority,
                    "ts": now,
                    "last_used": now,
                }))
                return Slot(slot_id, path, owner, priority)
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)
    except Exception as exc:
        logger.warning("slot claim failed (%s) — launching unmanaged", exc)
        return Slot(f"unmanaged-{uuid.uuid4().hex[:8]}", None, owner, priority)


class Slot:
    """One reserved browser instance. Attach the pid, then release it."""

    def __init__(self, slot_id: str, path: Path | None, owner: str,
                 priority: str = PRIORITY_EPHEMERAL) -> None:
        self.id = slot_id
        self.path = path
        self.owner = owner
        self.priority = (PRIORITY_CHAT if priority == PRIORITY_CHAT
                         else PRIORITY_EPHEMERAL)
        self.browser_pid: int | None = None
        self.cgroup: str | None = None
        self._released = False

    # -- placement ---------------------------------------------------

    def attach(self, pid: int, *, user_data_dir: str | None = None) -> None:
        """Bind the launched browser to this slot and confine its memory."""
        if self._released:
            return
        self.browser_pid = pid
        now = time.time()
        if self.path is not None:
            try:
                self.path.write_text(json.dumps({
                    "owner": self.owner,
                    "owner_pid": os.getpid(),
                    "browser_pid": pid,
                    "user_data_dir": user_data_dir,
                    "priority": self.priority,
                    "ts": now,
                    "last_used": now,
                }))
            except Exception:
                pass
        self.cgroup = _confine(pid)
        if self.cgroup:
            logger.info("browser pid %d tagged %s (priority=%s)",
                        pid, self.cgroup, self.priority)

    # -- teardown ----------------------------------------------------

    def release(self, *, kill: bool = False, sig: int = signal.SIGTERM) -> None:
        if self._released:
            return
        self._released = True

        if kill and self.browser_pid:
            _kill_tree(self.browser_pid, sig=sig)

        # Reap anything still bound to this browser's cgroup, then drop it.
        if self.cgroup:
            try:
                procs = Path(self.cgroup) / "cgroup.procs"
                if procs.exists():
                    for line in procs.read_text().split():
                        try:
                            _kill_tree(int(line), sig=sig)
                        except Exception:
                            pass
            except Exception:
                pass
            _release_cgroup(self.cgroup)
            self.cgroup = None

        if self.path is not None:
            try:
                self.path.unlink(missing_ok=True)
            except Exception:
                pass

    def __enter__(self) -> "Slot":
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


# ── public API ─────────────────────────────────────────────────────────────

async def acquire(owner: str, *, timeout: float = ACQUIRE_TIMEOUT,
                  priority: str = PRIORITY_EPHEMERAL) -> Slot:
    """Reserve one browser slot, waiting for a peer to finish if needed.

    ``priority=PRIORITY_CHAT`` marks a long-lived browser the user is actively
    talking to (ghost tab, scraper session). At the cap, a chat claim evicts
    the oldest ephemeral browser to get in — so a token refresh can never
    starve a chat. ``priority=PRIORITY_EPHEMERAL`` (the default) is for
    browsers that do a job and die; they never evict anything and simply wait.
    """
    priority = PRIORITY_CHAT if priority == PRIORITY_CHAT else PRIORITY_EPHEMERAL
    deadline = time.monotonic() + timeout
    warned = False
    reclaim_attempts = 0
    while True:
        # Fast-fail: if every slot we hold is a chat browser there is nothing
        # to evict, so this claim can only ever wait. Surface the choice NOW
        # (kill one, or wait) instead of blocking the chat turn for the whole
        # timeout first. Applies to chat claims too — a chat cannot evict
        # another chat.
        if _cap_held_by_chat():
            # Every slot we hold is a CHAT browser, so _try_claim() would never
            # evict one. Before giving up, sweep for browsers that are actually
            # gone: a slot whose pid died, or whose browser no longer answers on
            # its profile. Those are reaped, which frees capacity without ever
            # touching a live session. Retried up to the acquire timeout, since
            # the holder may simply be mid-teardown.
            if reclaim_attempts < 3 and _reclaim_own_dead_browsers():
                reclaim_attempts += 1
                continue
            if time.monotonic() >= deadline:
                raise NoSlotAvailable(
                    f"all {MAX_BROWSERS} browser slots are held by live chat sessions",
                    slots=own_slot_snapshot(),
                )
            await asyncio.sleep(0.5)
            continue
        slot = _try_claim(owner, priority)
        if slot is not None:
            if slot.path is not None:
                mine = len(_own_slots_locked(os.getpid()))
                logger.info("browser slot %s acquired for %s [%s] (%d/%d in use by this Sable)",
                            slot.id, owner, priority, mine, MAX_BROWSERS)
            return slot
        if not warned:
            logger.info("browser limit reached (%d per Sable, all chat) — %s [%s] waiting for a free slot",
                        MAX_BROWSERS, owner, priority)
            warned = True
        # Before declaring the cap full, try reclaiming our OWN slots whose
        # browser is already dead. Bounded, so a healthy fleet never pays for it.
        if reclaim_attempts < 3 and _reclaim_own_dead_browsers():
            reclaim_attempts += 1
            continue
        if time.monotonic() >= deadline:
            raise NoSlotAvailable(
                f"no browser slot free after {timeout:.0f}s "
                f"(limit {MAX_BROWSERS} per Sable; all held by chat)",
                slots=own_slot_snapshot(),
            )
        await asyncio.sleep(0.5)


def _reclaim_own_dead_browsers() -> bool:
    """Free slots THIS process holds whose browser is already gone.

    Only ever reaps a slot when there is positive evidence the browser is dead:
    the pid is gone, or the profile has no live root browser process bound to
    it. A slot with a live browser is left strictly alone — this function can
    never kill a session the user is actively talking to.

    Returns True if at least one slot was freed (caller should re-try the
    claim immediately). Caller must NOT hold the slot lock.
    """
    try:
        _SLOT_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        return False

    me = os.getpid()
    freed = False
    lock_path = _SLOT_DIR / ".lock"
    try:
        with open(lock_path, "w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                _prune_dead_locked()   # drops slots whose owner_pid is gone
                for path, data in _own_slots_locked(me):
                    bpid = int(data.get("browser_pid") or 0)
                    if not bpid:
                        # Claimed but never attached — a launch that failed
                        # between claim and Popen. Safe to drop.
                        path.unlink(missing_ok=True)
                        freed = True
                        continue
                    if _pid_alive(bpid):
                        # Pid is alive — but is it still OURS? A recycled pid or
                        # a browser that closed its profile would otherwise pin
                        # the slot forever.
                        udd = data.get("user_data_dir")
                        if udd:
                            try:
                                live = find_browser_pids(str(udd))
                            except Exception:
                                live = [bpid]
                            if bpid not in live:
                                logger.info(
                                    "reclaiming slot %s — browser pid %d no longer "
                                    "holds profile %s", path.name, bpid, udd,
                                )
                                _kill_tree(bpid)
                                cg = _cgroup_base()
                                if cg is not None:
                                    _release_cgroup(str(cg / "sable-browsers" / f"browser-{bpid}"))
                                path.unlink(missing_ok=True)
                                freed = True
                        continue
                    # Pid is genuinely dead — reap it and free the slot.
                    logger.info("reclaiming slot %s — browser pid %d is dead",
                                path.name, bpid)
                    cg = _cgroup_base()
                    if cg is not None:
                        _release_cgroup(str(cg / "sable-browsers" / f"browser-{bpid}"))
                    path.unlink(missing_ok=True)
                    freed = True
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)
    except Exception as exc:
        logger.debug("dead-browser reclaim skipped: %s", exc)
        return False
    return freed


def _cap_held_by_chat() -> bool:
    """True iff we are at the cap and nothing we hold is evictable.

    An ephemeral claim cannot make progress in that state, so it is better to
    report it immediately than to sit in the acquire loop.
    """
    try:
        own = _own_slots_locked(os.getpid())
    except Exception:
        return False
    if len(own) < MAX_BROWSERS:
        return False
    return all(_slot_priority(d) == PRIORITY_CHAT for _f, d in own)


class NoSlotAvailable(TimeoutError):
    """Every slot is held by a live chat browser and none can be reclaimed.

    Carries a snapshot of the holders so the UI can show the user exactly
    which browsers are blocking, and offer to kill one.
    """

    def __init__(self, message: str, slots: list[dict] | None = None) -> None:
        super().__init__(message)
        self.slots = slots or []


def own_slot_snapshot() -> list[dict]:
    """Live, chat-safe view of the browsers this Sable process is holding.

    Used to render the "no slot available" card: each entry carries the pid
    to kill, what it is, and how long it has been alive.
    """
    try:
        _prune_dead_locked()
    except Exception:
        pass
    now = time.time()
    out: list[dict] = []
    for _f, d in _own_slots_locked(os.getpid()):
        bpid = d.get("browser_pid")
        if not bpid:
            continue
        out.append({
            "pid": int(bpid),
            "owner": d.get("owner") or "browser",
            "priority": _slot_priority(d),
            "profile": Path(str(d.get("user_data_dir") or "")).name,
            "age_sec": int(max(0.0, now - float(d.get("ts") or now))),
            "idle_sec": int(max(0.0, now - _slot_last_used(d))),
        })
    out.sort(key=lambda s: s["idle_sec"], reverse=True)
    return out


def kill_slot(pid: int) -> bool:
    """Kill one browser by pid and release its slot.

    Backs the "kill this browser" button on the no-slot card. Deliberately
    blunt: kills the whole process group and drops the slot so the next chat
    message can claim the freed capacity immediately.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    removed = False
    lock_path = _SLOT_DIR / ".lock"
    try:
        with open(lock_path, "w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                for f in _SLOT_DIR.glob("slot-*.json"):
                    try:
                        data = json.loads(f.read_text())
                    except Exception:
                        continue
                    if int(data.get("browser_pid") or 0) != pid:
                        continue
                    _evict_slot_locked(f, data)
                    removed = True
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)
    except Exception as exc:
        logger.warning("kill_slot(%d) failed: %s", pid, exc)
    if _pid_alive(pid):
        _kill_tree(pid, sig=signal.SIGKILL)
    return removed or not _pid_alive(pid)


def reap_orphans() -> int:
    """Kill Sable browser processes that belong to no live slot.

    Catches the failure mode that produced hundreds of stale profiles: a Sable
    server dies mid-session and its browsers are re-parented to init, where
    nothing ever collects them again.
    """
    _SLOT_DIR.mkdir(parents=True, exist_ok=True)

    tracked: set[int] = set()
    tracked_profiles: set[str] = set()
    for f in _SLOT_DIR.glob("slot-*.json"):
        try:
            data = json.loads(f.read_text())
        except Exception:
            continue
        pid = data.get("browser_pid")
        if pid:
            tracked.add(int(pid))
        udd = data.get("user_data_dir")
        if udd:
            try:
                tracked_profiles.add(os.path.realpath(str(udd)))
            except Exception:
                pass

    # Never touch a browser younger than this. Launch takes seconds and a
    # sweep landing mid-startup would otherwise reap a healthy process.
    grace = float(os.environ.get("SABLE_REAP_GRACE", "180"))
    now = time.time()

    killed = 0
    # A Sable browser is one Sable actually spawned: its cgroup is under
    # ``sable-browsers/``. We NEVER identify browsers by command-line string —
    # that matched unrelated processes (a dev daemon whose path merely
    # contained "sable", an outside Chromium on a similar profile path) and
    # reaped them. Cgroup membership is the only trustworthy ownership signal.
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if not _is_sable_confined(pid):
            continue  # not Sable's child — leave it alone, full stop
        cmd = _cmdline(pid)
        if not cmd:
            continue
        # Belt-and-suspenders: it must also be an actual browser process.
        if not any(b in cmd.lower() for b in ("helium", "chrome", "chromium")):
            continue
        if "--type=" in cmd:
            continue  # helpers die with their group leader
        if pid in tracked and _pid_alive(pid):
            continue
        # Never reap a browser younger than the grace window — a sweep landing
        # mid-startup would otherwise kill a healthy process. (``grace``/``now``
        # were declared but never checked before, so even brand-new browsers
        # were fair game.)
        age = _proc_age(pid)
        if 0.0 <= age < grace:
            continue
        # Orphan: confined to Sable but no live slot owns it.
        ppid = 0
        try:
            for line in Path(f"/proc/{pid}/status").read_text().splitlines():
                if line.startswith("PPid:"):
                    ppid = int(line.split()[1])
                    break
        except Exception:
            pass
        logger.info("reaping orphaned browser pid %d (ppid=%d)", pid, ppid)
        _kill_tree(pid, sig=signal.SIGKILL)
        killed += 1

    return killed


def stats() -> dict:
    """Current fleet state — handy for a health endpoint or a log line."""
    try:
        _prune_dead_locked()
    except Exception:
        pass
    slots = []
    for f in _SLOT_DIR.glob("slot-*.json"):
        try:
            slots.append(json.loads(f.read_text()))
        except Exception:
            pass
    me = os.getpid()
    own = [s for s in slots if int(s.get("owner_pid") or 0) == me]
    return {
        "max_browsers": MAX_BROWSERS,          # per Sable instance
        "max_browsers_global": MAX_BROWSERS_GLOBAL,
        "active": len(slots),                  # host-wide
        "active_own": len(own),                # this instance
        "chat_own": sum(1 for s in own
                        if _slot_priority(s) == PRIORITY_CHAT),
        "ephemeral_own": sum(1 for s in own
                             if _slot_priority(s) == PRIORITY_EPHEMERAL),
        "slots": slots,
        "cgroup_root": str(_ensure_cgroup_root() or ""),
    }
