#!/usr/bin/env python3
"""Redirect bundle-relative `instruction/` and `Brain/` paths to PERSISTENT_ROOT.

These directories ship as read-only assets, but several modules write config
files into them (.persona_config.json, disabled_skills.json, ...). In a frozen
build that raises OSError [Errno 30]. Same treatment as the earlier `system/`
pass: rewrite the path expression, keep the import safe.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path("/home/sifat/hdd/projects/test/Sable-exp")
TARGETS = ["server", "engine"]

# <expr> / "instruction"   or   <expr> / "Brain"   where <expr> is a
# __file__-derived chain or a module-level root constant.
CHAIN = r'(?:Path\(__file__\)\.resolve\(\)(?:\.parent|\.parents\[\d+\])+|_ROOT|_BASE_DIR|BASE_DIR|_BASE|SABLE_ROOT|PROJECT_ROOT)'
PAT = re.compile(rf'\b{CHAIN}\s*/\s*"(instruction|Brain)"')

IMPORT_LINE = "from engine.config import PERSISTENT_ROOT  # frozen-build writable root"
FUTURE = "from __future__ import annotations"


def insert_import(src: str) -> str:
    lines = src.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.strip().startswith(FUTURE):
            lines.insert(i + 1, IMPORT_LINE + "\n")
            return "".join(lines)
    # no __future__: after the module docstring
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


def patch(path: Path) -> bool:
    src = path.read_text(encoding="utf-8")
    orig = src
    src = PAT.sub(lambda m: f'PERSISTENT_ROOT / "{m.group(1)}"', src)
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
            if ".bak" in py.name:
                continue
            # engine/config.py defines PERSISTENT_ROOT — never self-import
            if sub == "engine" and py.name == "config.py":
                src = py.read_text(encoding="utf-8")
                new = PAT.sub(lambda m: f'PERSISTENT_ROOT / "{m.group(1)}"', src)
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
