"""Running-process API — every system process that belongs to Sable.

Filtering strategy (a process is "Sable's" if ANY of these hold):

  1. listed in a background-job registry
  2. descended from a Sable server process
  3. its cwd lives under a Sable root
  4. its command line references a Sable root
  5. it is a Sable server process itself

That survives restarts, orphaned jobs and non-bg tool subprocesses — the
registries are in-memory and lose everything on restart, so they are treated
as one signal among several rather than the source of truth.

Stats come straight from /proc (no psutil): RSS for RAM, and a delta on
utime+stime for CPU% cached between polls.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

router = APIRouter()

# pid -> (monotonic_ts, total_cpu_ticks) for CPU% deltas between polls
_CPU_SAMPLES: dict[int, tuple[float, int]] = {}

# Cached /proc walks, so a 4s poll doesn't re-walk twice. Roots and servers
# keep separate timestamps: sharing one meant whichever refreshed last would
# block the other for up to _ROOT_TTL seconds.
_ROOTS_CACHE: dict[str, Any] = {"ts": 0.0, "roots": []}
_SERVERS_CACHE: dict[str, Any] = {"ts": 0.0, "servers": []}
_ROOT_TTL = 10.0


def _sable_roots() -> list[str]:
    """The single project base this server runs from.

    Deliberately NOT a list of every Sable checkout on disk. The user runs
    multiple checkouts side by side; listing them all made this project's
    panel show the *other* checkout's browsers and servers, which is
    confusing and dangerous (killing them kills the wrong project).

    The base is the repo root this file lives in. SABLE_PROCESS_ROOT
    overrides it if someone genuinely needs a wider or narrower scope.
    """
    now = time.time()
    if now - _ROOTS_CACHE["ts"] < _ROOT_TTL and _ROOTS_CACHE["roots"]:
        return _ROOTS_CACHE["roots"]

    override = os.environ.get("SABLE_PROCESS_ROOT", "").strip()
    if override:
        base = Path(os.path.expandvars(os.path.expanduser(override)))
    else:
        # server/api/routes/processes.py -> parents[3] is the repo root.
        base = Path(__file__).resolve().parents[3]

    roots: list[str] = []
    try:
        if base.is_dir():
            roots.append(str(base.resolve()))
    except OSError:
        pass

    _ROOTS_CACHE["roots"] = roots
    _ROOTS_CACHE["ts"] = now
    return roots


# ── /proc helpers ───────────────────────────────────────────────
def _pid_exists(pid: int) -> bool:
    """True only for a *live* process.

    A killed child whose parent never waited stays in /proc as a ZOMBIE with
    state 'Z'. os.kill(pid, 0) still succeeds for it, so a signal-0 probe
    would report a dead job as running forever.
    """
    if pid <= 0:
        return False
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(errors="replace")
        close = raw.rfind(")")
        if close != -1 and raw[close + 2:close + 3] in ("Z", "X", "x"):
            return False
    except OSError:
        pass
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True


def _read_stat(pid: int) -> dict[str, Any] | None:
    """Parse /proc/<pid>/stat. comm may contain spaces/parens, so split on
    the last ')' rather than trusting whitespace."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(errors="replace")
    except OSError:
        return None

    close = raw.rfind(")")
    if close == -1:
        return None

    fields = raw[close + 2:].split()
    if len(fields) < 22:
        return None

    return {
        "comm": raw[raw.find("(") + 1:close],
        "state": fields[0],
        "ppid": int(fields[1]),
        "cpu_ticks": int(fields[11]) + int(fields[12]),
        "start_ticks": int(fields[19]),
        "rss_bytes": int(fields[21]) * os.sysconf("SC_PAGE_SIZE"),
    }


def _cpu_percent(pid: int, cpu_ticks: int) -> float:
    """CPU% since the previous call for this pid (0.0 on first sight)."""
    now = time.monotonic()
    prev = _CPU_SAMPLES.get(pid)
    _CPU_SAMPLES[pid] = (now, cpu_ticks)
    if not prev:
        return 0.0

    prev_ts, prev_ticks = prev
    elapsed = now - prev_ts
    if elapsed <= 0:
        return 0.0

    hz = os.sysconf("SC_CLK_TCK") or 100
    pct = ((cpu_ticks - prev_ticks) / hz) / elapsed * 100.0
    return round(max(0.0, min(pct, 100.0 * (os.cpu_count() or 1))), 1)


def _boot_time() -> float:
    try:
        for line in Path("/proc/stat").read_text().splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
    except OSError:
        pass
    return 0.0


def _start_time(start_ticks: int, btime: float) -> float:
    return btime + (start_ticks / (os.sysconf("SC_CLK_TCK") or 100))


def _cmdline(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        return raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()
    except OSError:
        return ""


def _cwd(pid: int) -> str:
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return ""


def _listening_ports() -> dict[int, list[int]]:
    """Map pid -> [ports] by matching /proc/net/tcp{,6} LISTEN inodes against
    /proc/<pid>/fd socket symlinks."""
    inode_to_port: dict[str, int] = {}
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(table).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            parts = line.split()
            if len(parts) < 10 or parts[3] != "0A":  # 0A == LISTEN
                continue
            try:
                inode_to_port[parts[9]] = int(parts[1].split(":")[1], 16)
            except (IndexError, ValueError):
                continue

    if not inode_to_port:
        return {}

    pid_ports: dict[int, list[int]] = {}
    for proc_dir in Path("/proc").iterdir():
        if not proc_dir.name.isdigit():
            continue
        pid = int(proc_dir.name)
        try:
            for fd in (proc_dir / "fd").iterdir():
                try:
                    target = os.readlink(fd)
                except OSError:
                    continue
                if not target.startswith("socket:["):
                    continue
                inode = target[8:-1]
                if inode in inode_to_port:
                    pid_ports.setdefault(pid, []).append(inode_to_port[inode])
        except OSError:
            continue

    return {p: sorted(set(v)) for p, v in pid_ports.items()}


def _all_pids() -> list[int]:
    out: list[int] = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            out.append(int(entry.name))
    return out


# ── registries ──────────────────────────────────────────────────
def _registry_jobs() -> dict[int, dict[str, Any]]:
    """Both in-memory job stores, merged. Neither survives a restart."""
    merged: dict[int, dict[str, Any]] = {}

    try:
        from engine.skills.handlers.execute import BG_JOBS
        for pid, info in list(BG_JOBS.items()):
            entry = dict(info)
            entry.setdefault("namespace", "default")
            entry["source"] = "bg-job"
            merged[int(pid)] = entry
    except Exception:
        pass

    try:
        from engine.skills import get_skill_engine
        for namespace, bucket in getattr(get_skill_engine().bg_jobs, "_jobs", {}).items():
            for pid, info in bucket.items():
                if int(pid) in merged:
                    continue
                entry = dict(info)
                entry["namespace"] = namespace
                entry["source"] = "engine"
                merged[int(pid)] = entry
    except Exception:
        pass

    return merged


# ── server roots ────────────────────────────────────────────────
def _sable_server_pids() -> set[int]:
    """Live Sable server processes (python running server.py in a Sable root)."""
    now = time.time()
    if now - _SERVERS_CACHE["ts"] < _ROOT_TTL and _SERVERS_CACHE["servers"]:
        return set(_SERVERS_CACHE["servers"])

    roots = _sable_roots()
    found: set[int] = {os.getpid()}
    for pid in _all_pids():
        cmd = _cmdline(pid)
        if "server.py" not in cmd:
            continue
        if any(r in cmd for r in roots):
            found.add(pid)
            continue
        # Relative invocation (`.venv/bin/python server.py`) carries no
        # absolute path in argv, so fall back to its working directory.
        cwd = _cwd(pid)
        if cwd and any(cwd == r or cwd.startswith(r + os.sep) for r in roots):
            found.add(pid)
    _SERVERS_CACHE["servers"] = list(found)
    _SERVERS_CACHE["ts"] = now
    return found


def _ancestors(pid: int, ppid_map: dict[int, int]) -> set[int]:
    seen: set[int] = set()
    cur = pid
    while cur in ppid_map and cur not in seen:
        seen.add(cur)
        cur = ppid_map[cur]
        if cur <= 1:
            break
    return seen


# Browser helper processes (crashpad handlers, zygotes) inherit their cwd from
# whoever launched the browser. A stray Helium crashpad sitting in the project
# directory is NOT Sable's work, so a cwd match alone must never admit them.
_BROWSER_HELPER_HINTS = ("crashpad", "zygote", "--type=")


def _is_helper(comm: str, cmd: str) -> bool:
    c = comm.lower()
    cl = cmd.lower()
    return any(h in c or h in cl for h in _BROWSER_HELPER_HINTS)


def _belongs_to_sable(
    pid: int,
    roots: list[str],
    servers: set[int],
    ppid_map: dict[int, int],
    registry: dict[int, dict[str, Any]],
    ports: dict[int, list[int]],
) -> str | None:
    """Return a short reason string if *pid* belongs to THIS project, else None.

    Membership is ancestry- and command-line-based. A process merely having its
    cwd under the project root is NOT sufficient — that admitted unrelated
    browsers whose crashpad handlers happened to inherit the directory.
    """
    if pid in registry:
        return registry[pid].get("source", "registry")

    # 1. Descended from any Sable server of this project.
    if pid in servers:
        return "server-child"

    parent = ppid_map.get(pid)
    if parent in servers:
        # A direct child of a server that is not fixed infrastructure is a
        # background job. This is the only signal that survives a restart,
        # because BG_JOBS is memory-only.
        comm = (_read_stat(pid) or {}).get("comm", "")
        if not _is_infra(comm, _cmdline(pid)):
            return "bg-job"
        return "server-child"

    if _ancestors(pid, ppid_map) & servers:
        return "server-child"

    cmd = _cmdline(pid)

    # 2. Its own command line names this project's path — strong evidence it
    #    was launched from here (or with a data dir inside here).
    if cmd and any(r in cmd for r in roots):
        return "sable-path"

    # 3. cwd inside the project, but only for a process doing real work:
    #    it must hold a port, or be an interpreter/build tool. Helpers are
    #    excluded so foreign browser shrapnel cannot slip in.
    cwd = _cwd(pid)
    if cwd and any(cwd == r or cwd.startswith(r + os.sep) for r in roots):
        comm = (_read_stat(pid) or {}).get("comm", "")
        if _is_helper(comm, cmd):
            return None
        if ports.get(pid):
            return "sable-cwd"
        if any(t in comm.lower() for t in ("python", "uv", "node", "deno", "bun", "cargo", "go")):
            return "sable-cwd"

    return None


# ── system-wide stats ───────────────────────────────────────────
_SYS_CPU: dict[str, Any] = {"ts": 0.0, "total": 0, "idle": 0, "pct": 0.0}


def _meminfo() -> dict[str, float]:
    """MemTotal / MemAvailable / SwapTotal / SwapFree in MB."""
    out: dict[str, float] = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            key = parts[0].rstrip(":")
            if key in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
                out[key] = int(parts[1]) / 1024.0
    except OSError:
        pass
    return out


def _system_cpu_percent() -> float:
    """Overall CPU utilisation since the previous call."""
    now = time.monotonic()
    try:
        line = Path("/proc/stat").read_text().splitlines()[0]
        vals = [int(v) for v in line.split()[1:]]
    except (OSError, ValueError, IndexError):
        return 0.0

    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    total = sum(vals)

    # Snapshot BEFORE updating. `prev = _SYS_CPU` would alias the dict, so the
    # update() below would overwrite the very values we are about to subtract
    # from — making the delta always zero and pinning system CPU at 0%.
    prev_total = _SYS_CPU["total"]
    prev_idle = _SYS_CPU["idle"]
    _SYS_CPU.update({"ts": now, "total": total, "idle": idle})

    if not prev_total:
        return 0.0

    d_total = total - prev_total
    d_idle = idle - prev_idle
    if d_total <= 0:
        return round(_SYS_CPU["pct"], 1)

    pct = (1.0 - d_idle / d_total) * 100.0
    _SYS_CPU["pct"] = pct
    return round(max(0.0, min(100.0, pct)), 1)


def _system_stats() -> dict[str, Any]:
    mem = _meminfo()
    mem_total = mem.get("MemTotal", 0.0)
    mem_avail = mem.get("MemAvailable", 0.0)
    mem_used = max(0.0, mem_total - mem_avail)

    swap_total = mem.get("SwapTotal", 0.0)
    swap_free = mem.get("SwapFree", 0.0)
    swap_used = max(0.0, swap_total - swap_free)

    try:
        load = [float(x) for x in Path("/proc/loadavg").read_text().split()[:3]]
    except (OSError, ValueError):
        load = [0.0, 0.0, 0.0]

    return {
        "cpu_percent": _system_cpu_percent(),
        "cpu_count": os.cpu_count() or 1,
        "mem_total_mb": round(mem_total, 0),
        "mem_used_mb": round(mem_used, 0),
        "mem_percent": round(mem_used / mem_total * 100, 1) if mem_total else 0.0,
        "swap_total_mb": round(swap_total, 0),
        "swap_used_mb": round(swap_used, 0),
        "swap_percent": round(swap_used / swap_total * 100, 1) if swap_total else 0.0,
        "load": load,
    }


# ── API ─────────────────────────────────────────────────────────
# Bare interactive shells are the user's terminals, never Sable's work.
_USER_SHELLS = {"fish", "bash", "zsh", "sh", "dash", "ksh", "nu", "tcsh", "csh"}

_BROWSER_HINTS = (
    "chrome", "chromium", "electron", "firefox", "playwright",
    "crashpad", "msedge", "brave", "opera", "webkit", "geckodriver",
)


# Processes a Sable server always forks for itself; never a user's bg job.
_INFRA_COMMS = {
    "node", "mainthread", "github-mcp-serv", "chrome", "chromium",
    "chrome_crashpad", "crashpad", "electron", "uv", "sable-mcp",
}


def _is_infra(comm: str, cmd: str) -> bool:
    c = (comm or "").lower()
    if c in _INFRA_COMMS or c.startswith("crashpad") or c.startswith("chrome_crashpad"):
        return True
    cl = (cmd or "").lower()
    return ("playwright" in cl or "mcp" in cl or "remote-debugging-port" in cl)


def _state_label(state: str) -> str:
    """Short ps-style state for the S column (R/S/D/Z/T)."""
    return {"R": "R", "S": "S", "D": "D", "Z": "Z", "T": "T", "I": "I"}.get(state, state or "?")


def _display_comm(comm: str, cmd: str) -> str:
    """Readable label. Playwright's driver reports its thread name as the
    process comm, so every automation browser showed up as "MainThread"."""
    if comm == "MainThread" and "playwright" in cmd.lower():
        return "playwright-driver"
    if comm.startswith("chrome_crashpad"):
        return "crashpad"
    return comm


def _is_browser(comm: str, cmd: str) -> bool:
    c = comm.lower()
    cl = cmd.lower()
    return any(h in c or h in cl for h in _BROWSER_HINTS)


def _is_bare_shell(comm: str, cmd: str) -> bool:
    """A shell with no script argument — i.e. someone's open terminal."""
    if comm not in _USER_SHELLS:
        return False
    parts = cmd.split()
    return len(parts) <= 1 or (
        len(parts) == 2 and parts[1].startswith("-") and parts[1] not in ("-c",)
    )


def _categorise(source: str, comm: str, cmd: str) -> str:
    """Bucket a process for the UI: spawned | browsers | others."""
    if _is_browser(comm, cmd):
        return "browsers"
    if source in ("bg-job", "engine", "server-child"):
        return "spawned"
    return "others"



def _collect() -> list[dict[str, Any]]:
    roots = _sable_roots()
    registry = _registry_jobs()
    servers = _sable_server_pids()
    ports = _listening_ports()
    btime = _boot_time()
    now = time.time()

    ppid_map: dict[int, int] = {}
    stats: dict[int, dict[str, Any]] = {}
    cmds: dict[int, str] = {}
    for pid in _all_pids():
        st = _read_stat(pid)
        if st:
            stats[pid] = st
            ppid_map[pid] = st["ppid"]

    # Pass 1: decide membership, then drop noise.
    owned: dict[int, str] = {}
    for pid, st in stats.items():
        if st["state"] in ("Z", "X", "x"):
            continue
        reason = _belongs_to_sable(pid, roots, servers, ppid_map, registry, ports)
        if not reason:
            continue

        cmd = _cmdline(pid)
        cmds[pid] = cmd

        # The user's own terminals share the project cwd but are not Sable's
        # children — killing them closes the user's shell.
        if pid not in registry and pid not in servers and _is_bare_shell(st["comm"], cmd):
            continue

        # One-shot helper churn (curl/sh/uv per tool call) unless tracked or
        # holding a port — otherwise the list flickers with things already gone.
        started = _start_time(st["start_ticks"], btime)
        age = now - started
        if reason in ("sable-cwd", "sable-path") and not ports.get(pid) and age < 8.0:
            continue

        owned[pid] = reason

    mem_total = _meminfo().get("MemTotal", 0.0)
    return _build_rows(owned, stats, cmds, ppid_map, registry, servers, ports, btime, now, mem_total)



def _build_rows(owned, stats, cmds, ppid_map, registry, servers, ports, btime, now, mem_total=0.0):
    """Collapse owned processes into tree roots and attach display metadata."""

    def is_browser_pid(p: int) -> bool:
        st = stats.get(p)
        if not st:
            return False
        return _is_browser(st["comm"], cmds.get(p, ""))

    def top_owned(pid: int, same_family) -> int:
        best = pid
        cur = pid
        seen: set[int] = set()
        while cur in ppid_map and cur not in seen:
            seen.add(cur)
            parent = ppid_map[cur]
            if parent <= 1:
                break
            if parent in owned and (same_family is None or same_family(parent)):
                best = parent
            cur = parent
        return best

    groups: dict[int, list[int]] = {}
    for pid in owned:
        if is_browser_pid(pid):
            # Fold renderer / crashpad shrapnel into its browser root.
            root_pid = top_owned(pid, is_browser_pid)
        else:
            # Everything else keeps its own row. Collapsing non-browser
            # children into a parent is what hid running background jobs
            # (a bg job is a child of the server, so it vanished into "+1").
            root_pid = pid
        groups.setdefault(root_pid, []).append(pid)

    out: list[dict[str, Any]] = []
    for root_pid, members in groups.items():
        st = stats.get(root_pid)
        if not st:
            continue

        reg = registry.get(root_pid, {})
        started = reg.get("started") or _start_time(st["start_ticks"], btime)

        child_pids = [p for p in members if p != root_pid]
        child_ram = sum(stats[p]["rss_bytes"] for p in child_pids if p in stats) / (1024 * 1024)
        child_ports: list[int] = []
        for p in child_pids:
            child_ports.extend(ports.get(p, []))

        root_cmd = reg.get("command") or cmds.get(root_pid) or st["comm"]
        own_ram = st["rss_bytes"] / (1024 * 1024)

        out.append({
            "pid": root_pid,
            "command": root_cmd,
            "comm": _display_comm(st["comm"], root_cmd),
            "namespace": reg.get("namespace", "spawned"),
            "source": owned[root_pid],
            "category": _categorise(owned[root_pid], st["comm"], root_cmd),
            "log": reg.get("log", ""),
            "started": started,
            "uptime": max(0, round(now - started)) if started else None,
            "running": True,
            "cpu": _cpu_percent(root_pid, st["cpu_ticks"]),
            "state": _state_label(st["state"]),
            "ram_mb": round(own_ram, 1),
            "mem_percent": round(own_ram / mem_total * 100, 1) if mem_total else 0.0,
            "ports": sorted(set(ports.get(root_pid, []) + child_ports)),
            "is_server": root_pid in servers,
            "children": len(child_pids),
            # Summed child RSS — an upper bound, not a real total, because
            # shared pages are counted once per process.
            "children_ram_mb": round(child_ram, 1),
            "total_ram_mb": round(own_ram + child_ram, 1),
        })

    out.sort(key=lambda p: (not p["is_server"], not p["ports"], -p["total_ram_mb"]))
    return out



@router.get("/api/processes")
def list_processes() -> dict[str, Any]:
    """Every system process belonging to Sable, with live stats + ports."""
    all_pids = _all_pids()
    procs = _collect()
    return {
        "processes": procs,
        "running": len(procs),
        "total_scanned": len(all_pids),
        "system": _system_stats(),
    }


@router.post("/api/processes/{pid}/kill")
def kill_process(pid: int) -> dict[str, Any]:
    """Kill a Sable-owned process tree (SIGTERM, then SIGKILL)."""
    if pid <= 0:
        raise HTTPException(status_code=400, detail="invalid pid")
    if pid == os.getpid():
        raise HTTPException(status_code=400, detail="refusing to kill the Sable server itself")

    if not any(p["pid"] == pid for p in _collect()):
        raise HTTPException(status_code=404, detail="pid is not a Sable-owned process")

    if not _pid_exists(pid):
        return {"ok": True, "pid": pid, "already_exited": True}

    from engine.process_utils import SIGKILL, kill_process_tree

    kill_process_tree(pid)
    deadline = time.time() + 3.0
    while time.time() < deadline:
        if not _pid_exists(pid):
            break
        time.sleep(0.1)
    else:
        kill_process_tree(pid, sig=SIGKILL)
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if not _pid_exists(pid):
                break
            time.sleep(0.1)

    try:
        from engine.skills.handlers.execute import BG_JOBS
        BG_JOBS.pop(pid, None)
    except Exception:
        pass
    try:
        from engine.skills import get_skill_engine
        for bucket in getattr(get_skill_engine().bg_jobs, "_jobs", {}).values():
            bucket.pop(pid, None)
    except Exception:
        pass

    _CPU_SAMPLES.pop(pid, None)
    running = _pid_exists(pid)
    return {"ok": True, "pid": pid, "running": running, "killed": not running}
