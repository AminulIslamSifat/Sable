#!/usr/bin/env python3
"""unwrap.py - restore beacon sources to readable Go.

Reads BOTH obfuscation layers and inlines them as plain string literals:
  - stealth/literals_generated.go  -> EncL_<sha10>   (AST-harvested)
  - stealth/constants_generated.go -> Enc<Name>      (hand-curated secrets)

Also removes the unused `sable-beacon/stealth` import from files that no
longer reference the stealth package after unwrapping.

Source-only. Never writes to the generated constant files.
After editing, run `bash build.sh` - the build stages a copy in /tmp,
regenerates obfuscation there, compiles, and leaves your source alone.
"""
import re
import sys
from pathlib import Path

DIR = Path(__file__).resolve().parent
LIT = DIR / 'stealth' / 'literals_generated.go'
CONST = DIR / 'stealth' / 'constants_generated.go'
BS = chr(92)   # backslash
BT = chr(96)   # backtick
DQ = chr(34)   # double-quote


def read_key(path):
    for line in path.read_text(errors='replace').splitlines():
        if 'xor-key-hex:' in line:
            return bytes.fromhex(line.split(':', 1)[1].strip())
    return None


def decrypt_slice(body, key):
    ct = bytes(int(x, 16) for x in re.findall(r'0x([0-9a-f]{2})', body))
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(ct)).decode('utf-8', errors='replace')


def parse_constants(path, key):
    """Parse all `Name = []byte{...}` entries from a generated file."""
    out = {}
    if not path.exists():
        return out
    txt = path.read_text(errors='replace')
    for m in re.finditer(r'\b(Enc[A-Za-z0-9_]+)\s*=\s*\[\]byte\{([^}]*)\}', txt):
        out[m.group(1)] = decrypt_slice(m.group(2), key)
    return out


def go_quote(s):
    needs_escape = (BS in s) or (DQ in s) or ('\n' in s) or ('\r' in s) or ('\t' in s)
    if not needs_escape:
        return DQ + s + DQ
    if (BT not in s) and ('\r' not in s):
        return BT + s + BT
    esc = s.replace(BS, BS + BS).replace(DQ, BS + DQ)
    esc = esc.replace('\n', BS + 'n').replace('\r', BS + 'r').replace('\t', BS + 't')
    return DQ + esc + DQ


def main():
    dry = '--dry-run' in sys.argv
    key = read_key(LIT)
    if key is None:
        sys.exit('unwrap: no xor-key-hex header in stealth/literals_generated.go')
    print('key: %d bytes' % len(key))

    lits = parse_constants(LIT, key)
    consts = parse_constants(CONST, key)
    print('loaded %d EncL_* + %d Enc* constants' % (len(lits), len(consts)))

    # Match `stealth.D(stealth.EncL_xxx)` or `stealth.D(stealth.EncName)` or
    # `stealth.D(EncName)` (inside the stealth package itself).
    pat = re.compile(
        r'stealth\.D\(\s*(?:stealth\.)?(EncL_[a-f0-9]{10}|Enc[A-Za-z0-9_]+)\s*\)'
    )
    import_multi = re.compile(r'^\t"sable-beacon/stealth"\n', re.MULTILINE)
    import_single = re.compile(r'^import "sable-beacon/stealth"\n', re.MULTILINE)

    total = 0
    changed = 0
    for go in sorted(DIR.glob('*.go')):
        if go.name in ('ast_lit.go', 'unwrap.py', 'recover_lost.py', 'gen_stealth.go'):
            continue
        src = go.read_text(errors='replace')
        orig = src

        def repl(m):
            name = m.group(1)
            if name in lits:
                return go_quote(lits[name])
            if name in consts:
                return go_quote(consts[name])
            return m.group(0)

        src, n = pat.subn(repl, src)
        if n == 0:
            continue

        # Drop the stealth import if nothing in the file references it now.
        if not re.search(r'\bstealth\.', src):
            src = import_multi.sub('', src)
            src = import_single.sub('', src)
            # clean up a now-empty import block
            src = re.sub(r'import \(\s*\n\s*\)\n', '', src)

        if src != orig:
            changed += 1
            total += n
            if not dry:
                go.write_text(src)
            print('  %s %s (%d refs)' % ('[dry]' if dry else 'unwrapped', go.name, n))

    print('unwrap: %d files, %d refs %s' % (changed, total, '(dry-run)' if dry else 'restored'))
    if not dry:
        print('source is plain Go. run `bash build.sh` to build.')


if __name__ == '__main__':
    main()
