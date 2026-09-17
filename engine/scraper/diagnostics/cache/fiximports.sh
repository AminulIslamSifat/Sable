#!/bin/bash
# Add sable-beacon/stealth import to any .go file that uses stealth.D(...)
# but doesn't yet import the stealth package. Handles all three layouts:
#   - existing `import (` block    → insert into the block
#   - single `import "x"` line     → expand to a block
#   - no import statement at all   → create one after `package main`
cd "$1" || exit 1
for f in *.go; do
  case "$f" in ast_lit.go) continue ;; esac
  if head -5 "$f" | grep -q '//go:build ignore'; then continue; fi
  grep -q 'stealth\.' "$f" || continue
  grep -q 'sable-beacon/stealth' "$f" && continue

  if grep -q '^import (' "$f"; then
    ln=$(grep -n '^import (' "$f" | head -1 | cut -d: -f1)
    sed -i "${ln}a\\t\"sable-beacon/stealth\"" "$f"
  elif grep -q '^import "' "$f"; then
    cur=$(grep -m1 '^import "' "$f" | sed 's/^import //')
    ln=$(grep -n '^import "' "$f" | head -1 | cut -d: -f1)
    sed -i "${ln}c\import (\n\t${cur}\n\t\"sable-beacon/stealth\"\n)" "$f"
  else
    ln=$(grep -n '^package ' "$f" | head -1 | cut -d: -f1)
    sed -i "${ln}a\\nimport \"sable-beacon/stealth\"" "$f"
  fi
  echo "fixed $f"
done
