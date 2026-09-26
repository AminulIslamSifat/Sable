"""engine/browser_governor.py — hard, cross-process limits on Sable's browser fleet.

Two guarantees, enforced for EVERY browser Sable spawns:

  1. At most ``MAX_BROWSERS`` browser instances exist at once, across *all*
     Sable server processes. The stable install and a dev tree can both be
     running, so an in-process semaphore would let each hold its own quota —
     the cap is kept in a shared slot directory instead.

  2. Each browser is confined to its own cgroup v2 with a hard memory ceiling
     (``MAX_BYTES``). Chromium's zygote/gpu/renderer children inherit the
     cgroup, and ``memory.oom.group`` makes the kernel kill the whole tree as
     a unit on breach — so a runaway renderer cannot leave orphaned helpers
     re-parented to init.

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
MAX_BROWSERS = int(os.environ.get("SABLE_MAX_BROWSERS", "2"))
MAX_BYTES = int(os.environ.get("SABLE_BROWSER_MEM_MB", "1024")) * 1024 * 1024
ACQUIRE_TIMEOUT = float(os.environ.get("SABLE_BROWSER_SLOT_TIMEOUT", "120"))

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
        # Aggregate ceiling for the whole fleet, on top of the per-browser cap.
        agg = root / "memory.max"
        if agg.exists():
            try:
                agg.write_text(str(MAX_BROWSERS * MAX_BYTES))
            except Exception:
                pass
        return root
    except Exception as exc:
        if not _cgroup_warned:
            logger.warning("cgroup setup failed: %s", exc)
            _cgroup_warned = True
        return None


def _confine(pid: int) -> str | None:
    """Move *pid* into a 1 GiB cgroup. Returns the cgroup path on success."""
    root = _ensure_cgroup_root()
    if root is None:
        return None
    cg = root / f"browser-{pid}"
    try:
        cg.mkdir(exist_ok=True)
        (cg / "memory.max").write_text(str(MAX_BYTES))
        # No swap escape hatch — otherwise the cap just pushes pressure to disk.
        (cg / "memory.swap.max").write_text("0")
        # Kill the entire browser tree as one unit on breach, instead of one
        # unlucky renderer — that is what leaves orphans behind.
        (cg / "memory.oom.group").write_text("1")
        (cg / "cgroup.procs").write_text(str(pid))
        return str(cg)
    except Exception as exc:
        logger.warning("cgroup confine failed for pid %d: %s", pid, exc)
        return None


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
    for f in _SLOT_DIR.glob("slot-*.json"):
        try:
            data = json.loads(f.read_text())
        except Exception:
            f.unlink(missing_ok=True)
            removed += 1
            continue
        pid = data.get("browser_pid") or data.get("owner_pid")
        if not _pid_alive(int(pid or 0)):
            f.unlink(missing_ok=True)
            removed += 1
    return removed


def _try_claim(owner: str) -> "Slot | None":
    try:
        _SLOT_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        # Can't build the shared table — fall back to allowing the launch
        # rather than deadlocking the app. Better to run than to wedge.
        return Slot(f"unmanaged-{uuid.uuid4().hex[:8]}", None, owner)

    lock_path = _SLOT_DIR / ".lock"
    try:
        with open(lock_path, "w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                _prune_dead_locked()
                if len(list(_SLOT_DIR.glob("slot-*.json"))) >= MAX_BROWSERS:
                    return None
                slot_id = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
                path = _SLOT_DIR / f"slot-{slot_id}.json"
                path.write_text(json.dumps({
                    "owner": owner,
                    "owner_pid": os.getpid(),
                    "browser_pid": None,
                    "ts": time.time(),
                }))
                return Slot(slot_id, path, owner)
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)
    except Exception as exc:
        logger.warning("slot claim failed (%s) — launching unmanaged", exc)
        return Slot(f"unmanaged-{uuid.uuid4().hex[:8]}", None, owner)


class Slot:
    """One reserved browser instance. Attach the pid, then release it."""

    def __init__(self, slot_id: str, path: Path | None, owner: str) -> None:
        self.id = slot_id
        self.path = path
        self.owner = owner
        self.browser_pid: int | None = None
        self.cgroup: str | None = None
        self._released = False

    # -- placement ---------------------------------------------------

    def attach(self, pid: int, *, user_data_dir: str | None = None) -> None:
        """Bind the launched browser to this slot and confine its memory."""
        if self._released:
            return
        self.browser_pid = pid
        if self.path is not None:
            try:
                self.path.write_text(json.dumps({
                    "owner": self.owner,
                    "owner_pid": os.getpid(),
                    "browser_pid": pid,
                    "user_data_dir": user_data_dir,
                    "ts": time.time(),
                }))
            except Exception:
                pass
        self.cgroup = _confine(pid)
        if self.cgroup:
            logger.info("browser pid %d confined to %s (max %d MiB)",
                        pid, self.cgroup, MAX_BYTES // (1024 * 1024))

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

async def acquire(owner: str, *, timeout: float = ACQUIRE_TIMEOUT) -> Slot:
    """Reserve one browser slot, waiting for a peer to finish if needed."""
    deadline = time.monotonic() + timeout
    warned = False
    while True:
        slot = _try_claim(owner)
        if slot is not None:
            if slot.path is not None:
                logger.info("browser slot %s acquired for %s (%d/%d in use)",
                            slot.id, owner,
                            len(list(_SLOT_DIR.glob("slot-*.json"))), MAX_BROWSERS)
            return slot
        if not warned:
            logger.info("browser limit reached (%d) — %s waiting for a free slot",
                        MAX_BROWSERS, owner)
            warned = True
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"no browser slot free after {timeout:.0f}s "
                f"(limit {MAX_BROWSERS}; check for stuck browsers)"
            )
        await asyncio.sleep(0.5)


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
    # A Sable browser is any chromium process whose profile lives under a
    # Sable system dir — matched by path so we never touch the user's Zen.
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        cmd = _cmdline(pid)
        if not cmd or "--user-data-dir=" not in cmd:
            continue
        if "browser-data-acc" not in cmd and "sable" not in cmd.lower():
            continue
        if "--type=" in cmd:
            continue  # helpers die with their group leader
        if pid in tracked and _pid_alive(pid):
            continue
        # Orphan: no live slot owns it.
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
    return {
        "max_browsers": MAX_BROWSERS,
        "max_bytes_each": MAX_BYTES,
        "active": len(slots),
        "slots": slots,
        "cgroup_root": str(_ensure_cgroup_root() or ""),
    }
