"""One-shot converter: print(f"[LEVEL] ...") -> logger.level(...).

Only touches single-line prints whose string starts with a known level tag.
Multi-line prints and CLI-facing prints are left alone and reported.

Usage:
    python scripts/convert_prints.py --dry-run   # report only
    python scripts/convert_prints.py --apply     # write changes
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# level tag -> logging method
LEVEL_MAP = {
    "DEBUG": "debug",
    "ERROR": "error",
    "WARN": "warning",
    "STREAM": "debug",   # per-attempt tracing = debug
    "CALLER": "debug",
    "GHOST": "debug",
}

# print(f"[LEVEL] rest")  or  print("[LEVEL] rest")
PATTERN = re.compile(
    r'^(?P<indent>\s*)print\((?P<f>f?)(?P<q>["\'])\[(?P<lvl>\w+)\]\s?(?P<body>.*)(?P=q)\)\s*$'
)


def convert_line(line: str) -> tuple[str, str] | None:
    """Return (new_line, level) if converted, else None."""
    m = PATTERN.match(line.rstrip("\n"))
    if not m:
        return None
    lvl = m.group("lvl")
    method = LEVEL_MAP.get(lvl)
    if method is None:
        return None
    indent = m.group("indent")
    f = m.group("f")
    q = m.group("q")
    body = m.group("body")
    new = f"{indent}logger.{method}({f}{q}{body}{q})\n"
    return new, lvl


def process(path: Path, apply: bool) -> dict[str, int]:
    stats = {"converted": 0, "skipped_multiline": 0, "lines": 0}
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    out: list[str] = []
    for i, line in enumerate(lines):
        stripped = line.rstrip("\n")
        # Detect multi-line print (print( with no closing paren on same line)
        if re.match(r'^\s*print\($', stripped) or (
            stripped.lstrip().startswith("print(")
            and stripped.count("(") != stripped.count(")")
        ):
            stats["skipped_multiline"] += 1
            out.append(line)
            continue
        res = convert_line(line)
        if res:
            out.append(res[0])
            stats["converted"] += 1
        else:
            out.append(line)
    stats["lines"] = len(lines)
    if apply and stats["converted"]:
        path.write_text("".join(out), encoding="utf-8")
    return stats


def main() -> None:
    apply = "--apply" in sys.argv
    targets = [
        Path("engine/session.py"),
        Path("engine/service.py"),
        Path("engine/inpage.py"),
    ]
    total = 0
    for t in targets:
        if not t.exists():
            print(f"MISSING {t}")
            continue
        s = process(t, apply)
        total += s["converted"]
        print(f"{'WROTE' if apply else 'DRY  '} {t}: "
              f"{s['converted']} converted, {s['skipped_multiline']} multi-line skipped")
    print(f"\nTOTAL converted: {total}")


if __name__ == "__main__":
    main()
