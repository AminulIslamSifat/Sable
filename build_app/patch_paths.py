#!/usr/bin/env python3
"""Redirect bundle-relative `system/` paths to SABLE_PERSISTENT_ROOT.

Inserts the import immediately after `from __future__ import annotations`
(always top-level, never inside a parenthesised import block) so the patch
can't break syntax.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path("/home/sifat/hdd/projects/test/Sable-exp")
# Every shipped directory that derives a writable path from __file__.
# Missing one here means an Errno 30 at runtime inside the read-only mount:
# the code silently points at the AppImage squashfs instead of userData.
TARGETS = ["server", "engine", "tools", "skills", "connectors", "telegram_bot", "scripts"]

# Two shapes in the tree:
#   Path(__file__).resolve().parent.parent / "system"
#   Path(__file__).resolve().parents[3] / "system"
SYSTEM_PAT = re.compile(
    r'Path\(__file__\)\.resolve\(\)(?:\.parents?\[\d+\]|\.parent|\[\d+\])+\s*/\s*"system"'
)
CONST_PAT = re.compile(r'\b(?:_ROOT|_BASE_DIR|BASE_DIR|_BASE|SABLE_ROOT)\s*/\s*"system"')

IMPORT_LINE = "from engine.config import PERSISTENT_ROOT  # frozen-build writable root"
FUTURE = "from __future__ import annotations"


def insert_import(src: str) -> str:
    """Add the import right after the __future__ line, or after the docstring."""
    lines = src.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.strip().startswith(FUTURE):
            lines.insert(i + 1, IMPORT_LINE + "\n")
            return "".join(lines)

    # no __future__ — fall back to just after the module docstring
    in_doc = False
    for i, line in enumerate(lines):
        s = line.strip()
        if not in_doc and s.startswith(('"""', "'''")):
            if s.count('"""') >= 2 or s.count("'''") >= 2:
                lines.insert(i + 1, "\n" + IMPORT_LINE + "\n")
                return "".join(lines)
            in_doc = True
            continue
        if in_doc and ('"""' in s or "'''" in s):
            lines.insert(i + 1, "\n" + IMPORT_LINE + "\n")
            return "".join(lines)
    lines.insert(0, IMPORT_LINE + "\n")
    return "".join(lines)


def patch(path: Path) -> bool:
    src = path.read_text(encoding="utf-8")
    orig = src

    src = SYSTEM_PAT.sub('PERSISTENT_ROOT / "system"', src)
    src = CONST_PAT.sub('PERSISTENT_ROOT / "system"', src)
    if src == orig:
        return False

    if "from engine.config import PERSISTENT_ROOT" not in src:
        src = insert_import(src)

    path.write_text(src, encoding="utf-8")
    return True


def main() -> int:
    changed = []
    for sub in TARGETS:
        for py in (ROOT / sub).rglob("*.py"):
            if any(p in py.parts for p in ("__pycache__", ".editor_tools_backups")):
                continue
            if py.name.endswith(".bak") or py.name == "config.py" and sub == "engine":
                # engine/config.py DEFINES PERSISTENT_ROOT — never self-import
                if py.name == "config.py" and sub == "engine":
                    src = py.read_text(encoding="utf-8")
                    new = CONST_PAT.sub('PERSISTENT_ROOT / "system"', src)
                    if new != src:
                        py.write_text(new, encoding="utf-8")
                        changed.append(str(py.relative_to(ROOT)))
                continue
            try:
                if patch(py):
                    changed.append(str(py.relative_to(ROOT)))
            except Exception as e:
                print(f"SKIP {py}: {e}", file=sys.stderr)
    print(f"patched {len(changed)} files")
    for c in changed:
        print("  " + c)
    return 0


if __name__ == "__main__":
    sys.exit(main())