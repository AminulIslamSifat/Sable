#!/usr/bin/env python3
"""
editor_tools.py — A small, dependable file view/create/edit toolkit for
LLM coding agents, modeled on how Claude Code, Aider, and pi's coding
agent avoid the failure modes of "have the model paste a whole file
through a shell heredoc."

v2 changes (see CHANGELOG at bottom of SKILL.md):
  - Preserves the file's original line-ending style (CRLF vs LF) instead
    of silently normalizing CRLF -> LF on every write.
  - edit_file accepts a single edit OR a list of edits, applied
    atomically: every edit is validated (unique match found) BEFORE any
    of them are written, so a rename touching 5 spots either fully
    succeeds or fails with zero changes made — never a half-applied file.
  - New insert_file() for anchor-less insertion (e.g. "add this import
    at line 3", "add this function right after this line") — edit_file
    requires replacing existing text; insert_file adds new text without
    needing to match-and-replace something.
  - edit_file()'s return value includes a short unified-diff snippet so
    the caller gets visual confirmation of exactly what changed, not
    just "Edited 'x.py'".
  - Backups are capped per-file (default 20, oldest pruned) so
    .editor_tools_backups/ doesn't grow forever on a long agent session.

Design principles (see SKILL.md for full rationale):
  1. Files on disk are NEVER modified except through create_file(),
     edit_file(), and insert_file(). No heredocs, no shell redirection
     for content.
  2. view_file() adds line numbers ONLY in the string returned to the
     model. The numbers are never written back to disk.
  3. edit_file() requires each old_str to match EXACTLY ONE location
     after normalization. Zero matches or multiple matches is a hard
     error with a message that tells the model how to fix it — never a
     silent "guess and apply."
  4. Matching is layered: exact match first, then a narrow normalization
     pass (smart quotes, unicode dashes, trailing whitespace, NBSP) —
     never a fuzzy/edit-distance match. Fuzzy matching can silently
     apply an edit to the wrong block of similar-looking code; a failed
     exact/normalized match returns an error instead, which is the
     safer failure mode for code.
  5. Every successful edit_file()/insert_file() call writes a
     timestamped backup before touching the real file, so a bad edit is
     always one copy away from reversible.

Exit codes for CLI use: 0 = success, 1 = user/model error (bad match,
missing file, etc — safe to retry with corrected args), 2 = unexpected
internal error.
"""

import argparse
import difflib
import json
import os
import shutil
import sys
import time
import unicodedata

MAX_VIEW_CHARS = 16000      # above this, view_file truncates (no range given)
HEAD_TAIL_LINES = 60        # lines shown from each end when truncating
BACKUP_DIR_NAME = ".editor_tools_backups"
MAX_BACKUPS_PER_FILE = 20   # oldest backups pruned beyond this count
DIFF_CONTEXT_LINES = 2      # lines of context shown in the returned diff


# --------------------------------------------------------------------------
# Normalization — narrow and explicit. This is NOT fuzzy matching. It only
# collapses characters that are visually/semantically identical but differ
# in encoding (smart quotes vs straight quotes, unicode dashes, NBSP, etc),
# and trailing whitespace per line. It never tolerates different words,
# reordered code, or approximate matches.
# --------------------------------------------------------------------------

_QUOTE_MAP = {
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"',
}
_DASH_MAP = {c: "-" for c in "\u2010\u2011\u2012\u2013\u2014\u2015\u2212"}
_SPACE_MAP = {c: " " for c in "\u00a0\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u202f\u3000"}


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    trans = {}
    trans.update({ord(k): v for k, v in _QUOTE_MAP.items()})
    trans.update({ord(k): v for k, v in _DASH_MAP.items()})
    trans.update({ord(k): v for k, v in _SPACE_MAP.items()})
    text = text.translate(trans)
    lines = text.split("\n")
    lines = [ln.rstrip() for ln in lines]
    return "\n".join(lines)


def _count_occurrences(haystack: str, needle: str) -> int:
    if not needle:
        return 0
    return haystack.count(needle)


class ToolError(Exception):
    """Raised for user/model-correctable errors (exit code 1)."""


# --------------------------------------------------------------------------
# Line-ending handling — read files leaving \r\n intact (newline=""), track
# which style the file actually uses, and always write back in that style.
# This is what stops a CRLF (Windows) file from silently becoming LF on the
# first edit, which would otherwise show up as a full-file diff in git.
# --------------------------------------------------------------------------

def _read_raw(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
        return f.read()


def _detect_line_ending(text: str) -> str:
    crlf = text.count("\r\n")
    lf_only = text.count("\n") - crlf
    return "\r\n" if crlf > lf_only else "\n"


def _write_raw(path: str, text: str, line_ending: str) -> None:
    if line_ending == "\r\n":
        # normalize any stray bare \n first, then apply \r\n uniformly
        text = text.replace("\r\n", "\n").replace("\n", "\r\n")
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


# --------------------------------------------------------------------------
# view
# --------------------------------------------------------------------------

def list_dir(path: str, max_depth: int = 2) -> str:
    if not os.path.isdir(path):
        raise ToolError(f"'{path}' is not a directory")

    ignore = {"node_modules", "__pycache__", "venv"}
    lines = []

    def walk(dir_path, depth, prefix):
        try:
            entries = sorted(os.listdir(dir_path))
        except PermissionError:
            lines.append(f"{prefix}[permission denied]")
            return
        entries = [e for e in entries if e not in ignore and not e.startswith(".")]
        for name in entries:
            full = os.path.join(dir_path, name)
            is_dir = os.path.isdir(full)
            lines.append(f"{prefix}{name}{'/' if is_dir else ''}")
            if is_dir and depth < max_depth:
                walk(full, depth + 1, prefix + "  ")

    lines.append(path.rstrip("/") + "/")
    walk(path, 1, "  ")
    return "\n".join(lines)


def view_file(path: str, start: int = None, end: int = None, full: bool = False) -> str:
    if os.path.isdir(path):
        return list_dir(path)
    if not os.path.isfile(path):
        raise ToolError(f"'{path}' does not exist")

    text = _read_raw(path).replace("\r\n", "\n")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]  # don't count a trailing newline as an extra blank line
    total = len(lines)
    if total == 0:
        return "(empty file)"

    explicit_range = start is not None or end is not None
    s = max(1, start or 1)
    e = total if (end is None or end == -1) else min(total, end)
    if s > total:
        raise ToolError(f"start line {s} is beyond end of file ({total} lines total)")

    selected = lines[s - 1:e]
    joined = "\n".join(selected)

    if not full and not explicit_range and len(joined) > MAX_VIEW_CHARS:
        head = lines[:HEAD_TAIL_LINES]
        tail = lines[-HEAD_TAIL_LINES:]
        head_txt = "".join(f"{i + 1:6d}\t{l}\n" for i, l in enumerate(head))
        tail_start = total - len(tail)
        tail_txt = "".join(f"{tail_start + i + 1:6d}\t{l}\n" for i, l in enumerate(tail))
        omitted = total - HEAD_TAIL_LINES * 2
        return (
            head_txt
            + f"\n    ... [{omitted} lines omitted — file has {total} lines total; "
              f"call view with start/end to see a specific range, or full=True for "
              f"the entire file] ...\n\n"
            + tail_txt
        )

    numbered = "".join(f"{s + i:6d}\t{l}\n" for i, l in enumerate(selected))
    return numbered


# --------------------------------------------------------------------------
# create
# --------------------------------------------------------------------------

def create_file(path: str, content: str, overwrite: bool = False) -> str:
    if os.path.exists(path) and not overwrite:
        raise ToolError(
            f"'{path}' already exists. Use edit_file for changes, "
            f"or pass overwrite=True if a full rewrite is really intended."
        )
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(content)
    return f"Created '{path}' ({len(content)} bytes, {content.count(chr(10)) + 1} lines)"


# --------------------------------------------------------------------------
# backups — capped per file, oldest pruned
# --------------------------------------------------------------------------

def _backup(path: str) -> str:
    backup_dir = os.path.join(os.path.dirname(os.path.abspath(path)) or ".", BACKUP_DIR_NAME)
    os.makedirs(backup_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = os.path.basename(path)
    name = f"{base}.{stamp}.bak"
    dest = os.path.join(backup_dir, name)
    # avoid collision if two backups happen within the same second
    i = 1
    while os.path.exists(dest):
        dest = os.path.join(backup_dir, f"{base}.{stamp}.{i}.bak")
        i += 1
    shutil.copy2(path, dest)

    existing = sorted(
        (f for f in os.listdir(backup_dir) if f.startswith(base + ".")),
        key=lambda f: os.path.getmtime(os.path.join(backup_dir, f)),
    )
    while len(existing) > MAX_BACKUPS_PER_FILE:
        oldest = existing.pop(0)
        try:
            os.remove(os.path.join(backup_dir, oldest))
        except OSError:
            pass
    return dest


def _diff_snippet(before: str, after: str, path: str) -> str:
    diff = list(difflib.unified_diff(
        before.split("\n"), after.split("\n"),
        fromfile=path, tofile=path,
        n=DIFF_CONTEXT_LINES, lineterm="",
    ))
    if not diff:
        return ""
    # skip the two header lines (---/+++), keep it compact
    body = diff[2:]
    max_lines = 40
    if len(body) > max_lines:
        body = body[:max_lines] + [f"... ({len(diff) - 2 - max_lines} more diff lines omitted)"]
    return "\n".join(body)


# --------------------------------------------------------------------------
# edit — layered matching, mandatory uniqueness, atomic multi-edit
# --------------------------------------------------------------------------

def _find_unique_span(content: str, old_str: str):
    """Returns (start_index, end_index, replacement_note) for the unique
    match of old_str in content, trying exact then normalized matching.
    Raises ToolError if not found or ambiguous."""
    count = _count_occurrences(content, old_str)
    if count == 1:
        idx = content.index(old_str)
        return idx, idx + len(old_str), None
    if count > 1:
        raise ToolError(
            f"old_str matches {count} locations. Add more surrounding lines "
            f"to old_str (and the identical lines to new_str) so it matches "
            f"exactly once.\n--- old_str was ---\n{old_str}"
        )

    # normalized fallback, aligned by line
    content_lines = content.split("\n")
    old_lines = old_str.split("\n")
    norm_content_lines = [_normalize(l) for l in content_lines]
    norm_old_lines = [_normalize(l) for l in old_lines]
    n = len(norm_old_lines)
    matches = [
        i for i in range(len(norm_content_lines) - n + 1)
        if norm_content_lines[i:i + n] == norm_old_lines
    ]
    if len(matches) == 1:
        i = matches[0]
        start_idx = len("\n".join(content_lines[:i])) + (1 if i > 0 else 0)
        end_idx = start_idx + len("\n".join(content_lines[i:i + n]))
        return start_idx, end_idx, "matched after normalizing quotes/dashes/whitespace"
    if len(matches) > 1:
        raise ToolError(
            f"old_str matches {len(matches)} locations after normalization. "
            f"Add more surrounding lines so it matches exactly once.\n"
            f"--- old_str was ---\n{old_str}"
        )
    raise ToolError(
        "old_str was not found, even after normalizing quotes/dashes/whitespace. "
        "Call view_file on this path again and copy old_str exactly from the "
        "output — do not retype it from memory.\n"
        f"--- old_str was ---\n{old_str}"
    )


def edit_file(path: str, edits, backup: bool = True) -> str:
    """edits: either a single {"old_str", "new_str"} dict, or a list of them.
    All edits are validated (unique match found) against the ORIGINAL content
    before any are applied — so a batch either fully succeeds or fails with
    the file untouched."""
    if not os.path.isfile(path):
        raise ToolError(f"'{path}' does not exist")

    if isinstance(edits, dict):
        edits = [edits]
    if not edits:
        raise ToolError("no edits provided")

    for i, e in enumerate(edits):
        if "old_str" not in e or "new_str" not in e:
            raise ToolError(f"edit #{i + 1} is missing 'old_str' or 'new_str'")
        if e["old_str"] == "":
            raise ToolError(f"edit #{i + 1}: old_str is empty — refusing to match an empty string")
        if e["old_str"] == e["new_str"]:
            raise ToolError(f"edit #{i + 1}: old_str and new_str are identical — nothing to change")

    raw = _read_raw(path)
    line_ending = _detect_line_ending(raw)
    content = raw.replace("\r\n", "\n")  # work in \n internally, restore on write

    # Validate all matches up front against the ORIGINAL content, using
    # original character offsets. Edits must not overlap.
    spans = []
    for i, e in enumerate(edits):
        try:
            start, end, note = _find_unique_span(content, e["old_str"])
        except ToolError as err:
            raise ToolError(f"edit #{i + 1} of {len(edits)} failed: {err}")
        for s2, e2, *_ in spans:
            if start < e2 and s2 < end:
                raise ToolError(
                    f"edit #{i + 1} overlaps another edit in this same batch — "
                    f"split into separate calls or merge them into one old_str/new_str"
                )
        spans.append((start, end, i, note))

    # Apply back-to-front so earlier offsets stay valid
    new_content = content
    for start, end, i, note in sorted(spans, key=lambda t: t[0], reverse=True):
        new_content = new_content[:start] + edits[i]["new_str"] + new_content[end:]

    if backup:
        _backup(path)
    _write_raw(path, new_content, line_ending)

    diff = _diff_snippet(content, new_content, path)
    notes = [n for *_, n in spans if n]
    warn = ""
    if notes:
        warn = f"\n(note: {len(notes)} of {len(edits)} edit(s) matched only after " \
               f"normalizing quotes/dashes/whitespace — consider re-copying old_str " \
               f"from view_file next time)"
    n = len(edits)
    header = f"Edited '{path}' ({n} change{'s' if n != 1 else ''}){warn}"
    return f"{header}\n{diff}" if diff else header


# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# insert — anchor-less insertion (new lines, not a replace)
# --------------------------------------------------------------------------

def insert_file(path: str, content: str, at_line: int = None, after_str: str = None,
                 backup: bool = True) -> str:
    """Insert `content` as new lines, either:
      - at_line: insert BEFORE this 1-indexed line number (1 = top of file)
      - after_str: insert immediately after the unique line(s) matching this
        anchor text (matched the same exact->normalized way as edit_file)
    Exactly one of at_line / after_str must be given."""
    if not os.path.isfile(path):
        raise ToolError(f"'{path}' does not exist")
    if (at_line is None) == (after_str is None):
        raise ToolError("insert_file requires exactly one of: at_line, after_str")

    raw = _read_raw(path)
    line_ending = _detect_line_ending(raw)
    text = raw.replace("\r\n", "\n")
    lines = text.split("\n")
    trailing_newline = text.endswith("\n")
    if trailing_newline:
        lines = lines[:-1]
    total = len(lines)

    if at_line is not None:
        if at_line < 1 or at_line > total + 1:
            raise ToolError(f"at_line {at_line} is out of range (file has {total} lines)")
        insert_pos = at_line - 1
    else:
        start, end, _note = _find_unique_span(text, after_str)
        # convert char offset `end` to a line index
        insert_pos = text[:end].count("\n")
        if not text[:end].endswith("\n"):
            insert_pos += 1  # anchor doesn't end on a line boundary edge case

    new_block = content.split("\n")
    new_lines = lines[:insert_pos] + new_block + lines[insert_pos:]
    new_text = "\n".join(new_lines) + ("\n" if trailing_newline else "")

    if backup:
        _backup(path)
    _write_raw(path, new_text, line_ending)

    diff = _diff_snippet(text, new_text, path)
    header = f"Inserted into '{path}' at line {insert_pos + 1}"
    return f"{header}\n{diff}" if diff else header


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _cli():
    parser = argparse.ArgumentParser(
        description="File view/create/edit/insert tools for coding agents."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_view = sub.add_parser("view", help="View a file (with line numbers) or list a directory")
    p_view.add_argument("path")
    p_view.add_argument("--start", type=int, default=None)
    p_view.add_argument("--end", type=int, default=None)
    p_view.add_argument("--full", action="store_true", help="Return the entire file, numbered, bypassing truncation")

    p_create = sub.add_parser("create", help="Create a new file")
    p_create.add_argument("path")
    p_create.add_argument("--content-file", help="Read content from this file instead of stdin")
    p_create.add_argument("--overwrite", action="store_true")

    p_edit = sub.add_parser("edit", help="Replace old_str with new_str. JSON: {old_str,new_str} or [{old_str,new_str}, ...]")
    p_edit.add_argument("path")
    p_edit.add_argument("--json-file", help="File containing the edit JSON")
    p_edit.add_argument("--no-backup", action="store_true")

    p_insert = sub.add_parser("insert", help="Insert new content without replacing anything")
    p_insert.add_argument("path")
    p_insert.add_argument("--json-file", help='File containing {"content": "...", "at_line": N} or {"content": "...", "after_str": "..."}')
    p_insert.add_argument("--no-backup", action="store_true")

    args = parser.parse_args()

    try:
        if args.command == "view":
            print(view_file(args.path, args.start, args.end, full=args.full))

        elif args.command == "create":
            if args.content_file:
                with open(args.content_file, "r", encoding="utf-8") as f:
                    content = f.read()
            else:
                content = sys.stdin.read()
            print(create_file(args.path, content, overwrite=args.overwrite))

        elif args.command == "edit":
            if args.json_file:
                with open(args.json_file, "r", encoding="utf-8") as f:
                    payload = json.load(f)
            else:
                payload = json.load(sys.stdin)
            print(edit_file(args.path, payload, backup=not args.no_backup))

        elif args.command == "insert":
            if args.json_file:
                with open(args.json_file, "r", encoding="utf-8") as f:
                    payload = json.load(f)
            else:
                payload = json.load(sys.stdin)
            print(insert_file(
                args.path, payload["content"],
                at_line=payload.get("at_line"), after_str=payload.get("after_str"),
                backup=not args.no_backup,
            ))

    except ToolError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Internal error: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    _cli()