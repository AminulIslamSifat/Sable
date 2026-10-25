#!/usr/bin/env bash
# kill.sh — kill processes matching PATTERN without killing the caller or its ancestors.
#
# Why the naive version fails:
#   pkill -f 'sable-server'
# matches ANY process whose full command line contains that string — including
# the shell that typed it, because the string is in its own argv. So the shell
# SIGTERMs itself and the tool call dies with exit -15.
#
# The bracket trick ([s]able-server) only stops the *pattern* from matching
# itself. It does nothing when the literal argument sits in a parent shell's
# command line, which is exactly the case here.
#
# Correct fix: enumerate candidates with pgrep, then drop our own PID and every
# ancestor PID before signalling anything.
#
#   ./kill.sh sable-server          # TERM
#   ./kill.sh sable-server KILL     # force
set -euo pipefail

pat="${1:?usage: kill.sh <pattern> [TERM|KILL]}"
sig="${2:-TERM}"

# Build the protected set: this script, its parent shell, and all ancestors.
protected=" $$ "
pid=$$
while :; do
  ppid=$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ')
  [ -z "$ppid" ] && break
  [ "$ppid" -le 1 ] && break
  protected+="$ppid "
  pid=$ppid
done

# Collect matches, filter out anything in the protected set.
mapfile -t targets < <(pgrep -f -- "$pat" 2>/dev/null || true)
killed=0
for t in "${targets[@]}"; do
  case "$protected" in
    *" $t "*) continue ;;
  esac
  if kill -"$sig" "$t" 2>/dev/null; then
    killed=$((killed + 1))
  fi
done

if [ "$killed" -gt 0 ]; then
  echo "killed ($sig) $killed process(es) matching: $pat"
else
  echo "no match: $pat"
fi
