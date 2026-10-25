"""Startup readiness banner — prints a clear boxed summary once the server is live.

Kept in its own module so server.py stays a thin launcher, and so the banner
can be reused by the service manager / launcher scripts if needed.
"""

from __future__ import annotations
from engine.config import PERSISTENT_ROOT  # frozen-build writable root

import os
import re
import socket
import time
from datetime import datetime
from pathlib import Path

# Raw string so the escape sequences aren't interpreted (avoids SyntaxWarning).
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _visible_len(s: str) -> int:
    """Length of a string ignoring ANSI escape codes (for box alignment)."""
    return len(_ANSI_RE.sub("", s))


def _read_version() -> str:
    try:
        import tomllib
        p = Path(__file__).resolve().parent.parent / "pyproject.toml"
        data = tomllib.loads(p.read_text(encoding="utf-8"))
        return data.get("project", {}).get("version", "?")
    except Exception:
        return "?"


def _active_account() -> str:
    try:
        p = PERSISTENT_ROOT / "system" / ".active_account"
        return p.read_text(encoding="utf-8").strip() or "default"
    except Exception:
        return "default"


def _lan_ip() -> str:
    """Best-effort LAN IP (UDP connect doesn't actually send packets)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _wait_for_port(host: str, port: int, timeout: float = 30.0) -> bool:
    """Poll until something accepts on (host, port), or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def print_startup_banner(host: str, port: int) -> None:
    """Print a boxed, colored startup summary.

    Waits until the port is genuinely accepting connections before printing,
    so the box always lands *after* the app is up instead of competing with
    (or getting buried under) the runtime log flood.
    """
    if not _wait_for_port("127.0.0.1", port):
        return  # never came up — don't print a misleading banner

    C = "\x1b[36m"   # cyan
    G = "\x1b[32m"   # green
    B = "\x1b[1m"    # bold
    D = "\x1b[2m"    # dim
    R = "\x1b[0m"    # reset

    version = _read_version()
    account = _active_account()
    lan_ip = _lan_ip()
    all_ifaces = host in ("0.0.0.0", "::")

    rows = [
        ("Status",  G + "online" + R),
        ("Version", "v" + version),
        ("Local",   B + "http://127.0.0.1:" + str(port) + R),
        ("Network", ("http://" + lan_ip + ":" + str(port)) if all_ifaces else "local only"),
        ("Bind",    host + ":" + str(port)),
        ("Account", account),
        ("PID",     str(os.getpid())),
        ("Started", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    ]

    label_w = max(len(k) for k, _ in rows)
    body_lines = ["  " + k.ljust(label_w) + "  " + v for k, v in rows]
    inner = max(_visible_len(line) for line in body_lines)

    title = " SABLE SERVER "
    top_fill = inner + 2 - len(title)
    left = top_fill // 2
    right = top_fill - left

    out = [""]
    out.append(C + "\u256d" + "\u2500" * left + B + title + R + C + "\u2500" * right + "\u256e" + R)
    for line in body_lines:
        pad = " " * (inner - _visible_len(line))
        out.append(C + "\u2502" + R + line + pad + "  " + C + "\u2502" + R)
    out.append(C + "\u2570" + "\u2500" * (inner + 2) + "\u256f" + R)
    out.append(D + "  Sable is ready. Logs below are runtime output \u2014 the server is live." + R)
    out.append("")
    print("\n".join(out), flush=True)
