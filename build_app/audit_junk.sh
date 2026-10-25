#!/usr/bin/env bash
set -uo pipefail
cd /home/sifat/hdd/projects/test/Sable-exp

echo '=== any subprocess/exec of the opencode binary? ==='
grep -rn --include='*.py' -E 'opencode/bin|bin/opencode' connectors engine server 2>/dev/null \
  | grep -v '\.bak' | grep -v 'opencode\.ai' | head -10 || true

echo
echo '=== the comment in connectors/__init__.py ==='
sed -n '125,145p' connectors/__init__.py

echo
echo '=== total junk size in source tree ==='
find web connectors skills tools instruction documentation Brain \
  -type d -name '.editor_tools_backups' -o -type d -name '.sable_backups' 2>/dev/null \
  | while read -r d; do du -sh "$d"; done | sort -rh | head -10

echo
echo '=== .bak file count + size ==='
find web connectors skills tools instruction documentation Brain -name '*.bak*' -type f 2>/dev/null | wc -l
find web connectors skills tools instruction documentation Brain -name '*.bak*' -type f -print0 2>/dev/null \
  | xargs -0 -r du -ch 2>/dev/null | tail -1
