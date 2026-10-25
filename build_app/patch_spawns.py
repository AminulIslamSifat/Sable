#!/usr/bin/env python3
"""Replace `sys.executable` + script-path spawns with `script_cmd()`.

Pattern being fixed:
    cmd = [sys.executable, _SCRIPT, *cli]        ->  [sys.executable, _SCRIPT, *cli]
    cmd = [sys.executable, str(_SCRIPT), ...]   ->  same

After:  cmd = script_cmd(_SCRIPT, *cli)

The helper returns a frozen-safe argv (binary + --run-script) and the caller
keeps passing it wherever it already went (subprocess.run/Popen/create_subprocess_exec).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path("/home/sifat/hdd/projects/test/Sable-exp")
FILES = [
    "engine/skills/handlers/image_generator.py",
    "engine/skills/handlers/memory_manager.py",
    "engine/skills/handlers/computer_use.py",
    "engine/skills/handlers/tracknote.py",
    "server/api/routes/image_stream.py",
    "server/api/routes/study_humanize.py",
]

# [sys.executable, <expr>, <rest...>]  — expr is a name, str(name), or a literal
SYS_EXEC_PAT = re.compile(
    r'\[\s*sys\.executable\s*,\s*(str\(\s*)?([A-Za-z_][\w\.]*(?:\([^)]*\))?)(\s*\))?\s*,'
)

IMPORT_LINE = "from engine.py_runner import script_cmd, child_env  # frozen-safe subprocess"
FUTURE = "from __future__ import annotations"


def insert_import(src: str) -> str:
    lines = src.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.strip().startswith(FUTURE):
            lines.insert(i + 1, IMPORT_LINE + "\n")
            return "".join(lines)
    in_doc = False
    for i, line in enumerate(lines):
        s = line.strip()
        if not in_doc and s.startswith(('"""', "'''")):
            q = '"""' if s.startswith('"""') else "'''"
            if s.count(q) >= 2:
                lines.insert(i + 1, "\n" + IMPORT_LINE + "\n")
                return "".join(lines)
            in_doc = True
            continue
        if in_doc and ('"""' in s or "'''" in s):
            lines.insert(i + 1, "\n" + IMPORT_LINE + "\n")
            return "".join(lines)
    lines.insert(0, IMPORT_LINE + "\n")
    return "".join(lines)


def rewrite(src: str) -> tuple[str, int]:
    """Turn `[sys.executable, X, ...rest]` into `script_cmd(X, ...rest)`."""
    count = 0

    def _repl(m: re.Match) -> str:
        nonlocal count
        expr = m.group(2)
        count += 1
        return f"script_cmd({expr},"

    out = SYS_EXEC_PAT.sub(_repl, src)
    if out == src:
        return src, 0

    # The list literal became a call — fix the closing bracket of that list.
    # Walk line by line: once a line gained `script_cmd(`, its matching `]`
    # (which may be on the same or a later line) becomes `)`.
    lines = out.splitlines(keepends=True)
    depth_stack: list[int] = []
    for i, line in enumerate(lines):
        for ch in line:
            if ch == "(":
                depth_stack.append(i)
            elif ch == ")" and depth_stack:
                depth_stack.pop()
        if "script_cmd(" in line:
            # find the matching ] for this list literal starting here
            pass

    # Simpler + safe: handle the common same-line and next-line `],` forms.
    out = re.sub(r'(script_cmd\([^\[\]]*?)\]', r'\1)', out, flags=re.S)
    return out, count


def main() -> int:
    total = 0
    for rel in FILES:
        p = ROOT / rel
        if not p.is_file():
            print(f"MISSING {rel}", file=sys.stderr)
            continue
        src = p.read_text(encoding="utf-8")
        new, n = rewrite(src)
        if n == 0:
            print(f"  no change: {rel}")
            continue
        if "from engine.py_runner import" not in new:
            new = insert_import(new)
        p.write_text(new, encoding="utf-8")
        print(f"  {rel}: {n} call site(s)")
        total += n
    print(f"rewrote {total} call site(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
