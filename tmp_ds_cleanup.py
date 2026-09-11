#!/usr/bin/env python3
"""One-shot cleaner for .deepseek_tokens.json pollution.

Rule: a token belongs to the account that captured it. When a token is
shared across many accounts, keep it ONLY on the lowest-numbered account
(or the active account) and strip it from the rest. Accounts left with no
token get an empty list, so their 'ds' badge disappears and rotation
reflects reality.

Usage:
    python3 tmp_ds_cleanup.py          # dry run
    python3 tmp_ds_cleanup.py --apply  # write changes
"""
import json
import sys
import re
from pathlib import Path

P = Path(__file__).resolve().parent / "system" / ".deepseek_tokens.json"
APPLY = "--apply" in sys.argv

data = json.loads(P.read_text())


def _num(key: str) -> int:
    m = re.search(r"acc(\d+)$", key)
    return int(m.group(1)) if m else 10**9


# token -> list of accounts that currently claim it
owners: dict[str, list[str]] = {}
for acct, toks in data.items():
    for t in toks:
        owners.setdefault(t, []).append(acct)

# Decide canonical owner for each token: lowest account number
canonical: dict[str, str] = {}
for tok, accts in owners.items():
    canonical[tok] = sorted(accts, key=_num)[0]

# Rebuild: each account keeps ONLY tokens it canonically owns
clean: dict[str, list[str]] = {}
stripped = 0
for acct in sorted(data, key=_num):
    kept = [t for t in data[acct] if canonical.get(t) == acct]
    clean[acct] = kept
    stripped += len(data[acct]) - len(kept)

with_after = sum(1 for v in clean.values() if v)
print(f"accounts before        : {len(data)}")
print(f"accounts WITH token    : {sum(1 for v in data.values() if v)}")
print(f"accounts with token AFTER: {with_after}")
print(f"token entries stripped : {stripped}")
print()
print("Canonical owners:")
for tok, acct in sorted(canonical.items(), key=lambda kv: _num(kv[1])):
    print(f"  {acct:22s} <- {tok[:32]}...")

if APPLY:
    P.write_text(json.dumps(clean, indent=2))
    print(f"\n✅ WROTE {P}")
else:
    print("\n(dry run — pass --apply to write)")
