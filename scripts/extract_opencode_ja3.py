#!/usr/bin/env python3
"""Extract the TLS ClientHello fingerprint that opencode's Bun runtime sends.

Why this exists: opencode.ai's free inference tier accepts only requests whose
TLS handshake matches Bun's. Python's httpx/OpenSSL handshake is rejected with a
fake "free usage limit" 429. This script captures the real handshake once and
writes it to a JSON file that connectors/opencode/client.py reads at startup.

Usage:
    python scripts/extract_opencode_ja3.py              # capture from live opencode
    python scripts/extract_opencode_ja3.py --from FILE  # parse an existing strace capture

Requires: strace, and the opencode binary on PATH (capture mode only).
Output:   system/opencode_ja3.json
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

GREASE = {
    0x0a0a, 0x1a1a, 0x2a2a, 0x3a3a, 0x4a4a, 0x5a5a, 0x6a6a, 0x7a7a,
    0x8a8a, 0x9a9a, 0xaaaa, 0xbaba, 0xcaca, 0xdada, 0xeaea, 0xfafa,
}

OUT_PATH = Path(__file__).resolve().parent.parent / "system" / "opencode_ja3.json"
HEXPAIR = re.compile("\\\\x([0-9a-f]{2})")


def _capture(timeout: int = 90) -> Path:
    """Run opencode under strace and return the capture file path."""
    if not shutil.which("opencode"):
        sys.exit("opencode not found on PATH — cannot capture (use --from FILE instead)")
    if not shutil.which("strace"):
        sys.exit("strace not found — install it, or use --from FILE")

    fd, name = tempfile.mkstemp(prefix="opencode_tls_", suffix=".txt")
    os.close(fd)
    capture = Path(name)

    cmd = [
        "strace", "-f", "-e", "trace=sendto,sendmsg", "-xx", "-s", "2500",
        "-o", str(capture),
        "timeout", str(timeout - 10),
        "opencode", "run", "reply PONG",
        "-m", "opencode/deepseek-v4.1-flash-free", "--standalone",
    ]
    print(f"[*] capturing handshake -> {capture}")
    try:
        subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        print("[!] capture timed out; parsing whatever landed")
    return capture


def _client_hello(capture: Path) -> bytes | None:
    """Pull the largest TLS ClientHello record out of an strace -xx capture."""
    data = capture.read_text(errors="replace")
    best = None
    for m in re.finditer(r'"([^"]*)"', data):
        pairs = HEXPAIR.findall(m.group(1))
        if not pairs:
            continue
        raw = bytes(int(h, 16) for h in pairs)
        if len(raw) > 5 and raw[0] == 0x16 and raw[1] == 0x03 and raw[2] == 0x01:
            if best is None or len(raw) > len(best):
                best = raw
    return best


def _parse(rec: bytes) -> dict:
    """Parse a ClientHello record into its JA3 components."""
    body = rec[5:]           # strip TLS record header
    p = 4                    # strip handshake header (type + 3-byte length)
    version = int.from_bytes(body[p:p + 2], "big"); p += 2
    p += 32                  # random
    p += 1 + body[p]         # session id

    cs_len = int.from_bytes(body[p:p + 2], "big"); p += 2
    ciphers = [int.from_bytes(body[p + i:p + i + 2], "big") for i in range(0, cs_len, 2)]
    p += cs_len
    p += 1 + body[p]         # compression methods

    ext_total = int.from_bytes(body[p:p + 2], "big"); p += 2
    end = p + ext_total
    exts, curves, point_fmts = [], [], []
    while p + 4 <= end:
        et = int.from_bytes(body[p:p + 2], "big")
        el = int.from_bytes(body[p + 2:p + 4], "big")
        ed = body[p + 4:p + 4 + el]
        exts.append(et)
        if et == 10 and len(ed) >= 2:      # supported_groups
            n = int.from_bytes(ed[0:2], "big")
            curves = [int.from_bytes(ed[2 + i:4 + i], "big") for i in range(0, n, 2)]
        elif et == 11 and len(ed) >= 1:    # ec_point_formats
            point_fmts = list(ed[1:1 + ed[0]])
        p += 4 + el

    c = [x for x in ciphers if x not in GREASE]
    e = [x for x in exts if x not in GREASE]
    cr = [x for x in curves if x not in GREASE]
    ja3 = "%d,%s,%s,%s,%s" % (
        version,
        "-".join(map(str, c)),
        "-".join(map(str, e)),
        "-".join(map(str, cr)),
        "-".join(map(str, point_fmts)),
    )
    return {
        "ja3": ja3,
        "ja3_md5": hashlib.md5(ja3.encode()).hexdigest(),
        "ciphers": c,
        "extensions": e,
        "curves": cr,
        "point_formats": point_fmts,
        "record_bytes": len(rec),
    }


def _opencode_version() -> str:
    try:
        out = subprocess.run(["opencode", "--version"], capture_output=True, text=True, timeout=15)
        return (out.stdout or out.stderr).strip().splitlines()[0] if (out.stdout or out.stderr) else "unknown"
    except Exception:
        return "unknown"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="src", type=Path, help="parse an existing strace capture instead of running opencode")
    ap.add_argument("--out", type=Path, default=OUT_PATH, help=f"output path (default: {OUT_PATH})")
    ap.add_argument("--timeout", type=int, default=90, help="capture timeout in seconds (default 90)")
    args = ap.parse_args()

    capture = args.src or _capture(args.timeout)
    try:
        rec = _client_hello(capture)
        if not rec:
            sys.exit(f"no TLS ClientHello found in {capture}")
        parsed = _parse(rec)
    finally:
        if args.src is None:
            capture.unlink(missing_ok=True)

    parsed["opencode_version"] = _opencode_version()
    parsed["captured_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    parsed["source"] = "strace capture of `opencode run`"

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(parsed, indent=2) + "\n", encoding="utf-8")

    print(f"[*] JA3     {parsed['ja3']}")
    print(f"[*] MD5     {parsed['ja3_md5']}")
    print(f"[*] version {parsed['opencode_version']}")
    print(f"[*] written {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
