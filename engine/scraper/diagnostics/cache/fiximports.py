#!/usr/bin/env python3
"""fiximports.py - add `sable-beacon/stealth` import to files that use stealth.*

Usage: python3 fiximports.py <dir>
Handles:
  - existing `import (` block   -> insert into the block
  - single `import "x"` line     -> expand into a block
  - no import statement at all   -> create one after the `package` clause
"""
import re
import sys
from pathlib import Path

NEEDLE = 'sable-beacon/stealth'

def fix(path: Path) -> bool:
    src = path.read_text(errors='replace')
    if 'stealth.' not in src:
        return False
    if NEEDLE in src:
        return False
    if '//go:build ignore' in src.splitlines()[0:5]:
        return False

    lines = src.splitlines(keepends=True)
    out = []
    inserted = False

    # 1. existing `import (` block
    for i, ln in enumerate(lines):
        out.append(ln)
        if not inserted and ln.rstrip() == 'import (':
            out.append('\t"' + NEEDLE + '"\n')
            inserted = True

    if inserted:
        path.write_text(''.join(out))
        return True

    # 2. single-line `import "x"`
    src2 = re.sub(
        r'^import ("[^"]+")$',
        lambda m: 'import (\n\t' + m.group(1) + '\n\t"' + NEEDLE + '"\n)',
        src, count=1, flags=re.MULTILINE)
    if src2 != src:
        path.write_text(src2)
        return True

    # 3. no import at all — insert after first `package X` line
    src3 = re.sub(
        r'^(package \w+\s*\n)',
        lambda m: m.group(1) + '\nimport "' + NEEDLE + '"\n',
        src, count=1, flags=re.MULTILINE)
    if src3 != src:
        path.write_text(src3)
        return True

    return False


def main():
    d = Path(sys.argv[1] if len(sys.argv) > 1 else '.').resolve()
    fixed = 0
    for go in sorted(d.glob('*.go')):
        if fix(go):
            print('fixed', go.name)
            fixed += 1
    print('fiximports: %d files' % fixed)


if __name__ == '__main__':
    main()
