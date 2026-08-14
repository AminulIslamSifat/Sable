
#!/usr/bin/env python3
"""
editor_tools.py — v4: file view/create/edit toolkit for LLM coding agents.

v4 changes:
  - Structural matching layer: tolerates indentation differences (tabs vs
    spaces, leading whitespace), blank-line variance, and internal spacing.
    Matching cascade: exact → normalized → structural. All layers still
    require a UNIQUE match; fuzzy/edit-distance is never used.
  - Nearest-match suggestion on failure: when old_str matches zero locations,
    a sliding-window SequenceMatcher scan finds the closest block (ratio ≥ 0.6)
    and includes it in the error message with line numbers.
  - Structured stats feedback: every mutation (edit/insert/create) returns a
    "── stats ──" block with lines_before, lines_after, added, removed, net,
    and affected_range so the model gets numeric feedback without re-viewing.
  - replace_all mode: skip uniqueness check, replace ALL occurrences, report
    count. Useful for renames and import path changes.
  - dry_run mode: validate all SEARCH blocks, show the diff that WOULD apply,
    but do NOT write to disk.
  - Shebang detection: create_file auto-chmods 0o755 if content starts with #!
  - Encoding guard: non-UTF-8 files produce a clear error instead of garbled text.
  - Blank-line collapse in structural matching: consecutive blank lines are
    collapsed to one before comparison.

Design principles (unchanged from v3):
  1. Files on disk are NEVER modified except through create_file(),
     edit_file(), and insert_file().
  2. view_file() adds line numbers ONLY in the returned string.
  3. edit_file() requires each old_str to match EXACTLY ONE location
     (unless replace_all=True). Zero or multiple = hard error.
  4. Matching is layered: exact → normalized → structural. Never fuzzy.
  5. Every successful edit/insert writes a timestamped backup first.
  6. Raw code never survives a hand-typed JSON escaping pass.

Exit codes: 0 = success, 1 = user/model error, 2 = internal error.
"""

import argparse
import difflib
import json
import os
import re
import shutil
import stat
import sys
import time
import unicodedata

MAX_VIEW_CHARS = 16000
HEAD_TAIL_LINES = 60
BACKUP_DIR_NAME = ".editor_tools_backups"
MAX_BACKUPS_PER_FILE = 20
MAX_DIFF_LINES = 400
DIFF_CONTEXT_LINES = 3
NEAREST_MATCH_THRESHOLD = 0.6
NEAREST_MATCH_WINDOW_SLACK = 4  # lines of slack when searching for nearest match


# --------------------------------------------------------------------------
# Normalization layers
# --------------------------------------------------------------------------

_QUOTE_MAP = {
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"',
}
_DASH_MAP = {c: "-" for c in "\u2010\u2011\u2012\u2013\u2014\u2015\u2212"}
_SPACE_MAP = {c: " " for c in "\u00a0\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u202f\u3000"}


def _normalize(text: str) -> str:
    """Layer 2: smart quotes, unicode dashes, NBSP, trailing whitespace, tabs→4sp."""
    text = unicodedata.normalize("NFKC", text)
    trans = {}
    trans.update({ord(k): v for k, v in _QUOTE_MAP.items()})
    trans.update({ord(k): v for k, v in _DASH_MAP.items()})
    trans.update({ord(k): v for k, v in _SPACE_MAP.items()})
    text = text.translate(trans)
    text = text.replace("\t", "    ")
    lines = text.split("\n")
    lines = [ln.rstrip() for ln in lines]
    return "\n".join(lines)


def _normalize_structural_line(line: str) -> str:
    """Layer 3 per-line: strip ALL leading/trailing whitespace, collapse internal."""
    return " ".join(line.split())


def _collapse_blank_lines(lines: list[str]) -> list[str]:
    """Collapse consecutive blank lines into a single blank line."""
    result = []
    prev_blank = False
    for ln in lines:
        is_blank = ln.strip() == ""
        if is_blank and prev_blank:
            continue
        result.append(ln)
        prev_blank = is_blank
    return result


def _structural_span_count(content_lines: list[str], old_lines: list[str], start_i: int) -> int:
    """Count how many original content lines a structural match spans,
    starting at index start_i. Handles extra blank lines in the file that
    were collapsed during matching.

    Strategy: match non-blank tokens in order; all blank lines between
    matched tokens are consumed as part of the span."""
    norm_old_struct = [_normalize_structural_line(l) for l in old_lines]
    collapsed_old_struct = _collapse_blank_lines(norm_old_struct)
    while collapsed_old_struct and collapsed_old_struct[0] == "":
        collapsed_old_struct.pop(0)
    while collapsed_old_struct and collapsed_old_struct[-1] == "":
        collapsed_old_struct.pop()

    # Extract non-blank targets — these must appear in order
    non_blank_targets = [l for l in collapsed_old_struct if l != ""]
    if not non_blank_targets:
        return len(old_lines)

    span_count = 0
    target_idx = 0

    for j in range(start_i, len(content_lines)):
        ln_struct = _normalize_structural_line(content_lines[j])

        if target_idx >= len(non_blank_targets):
            break  # all targets matched

        if ln_struct == "":
            # Blank line — consume as part of the span
            span_count += 1
        elif ln_struct == non_blank_targets[target_idx]:
            # Matched next non-blank target
            target_idx += 1
            span_count += 1
        else:
            # Non-blank line that doesn't match — stop
            break

    if span_count == 0 or target_idx < len(non_blank_targets):
        span_count = len(old_lines)  # fallback if match incomplete
    return span_count


def _structural_lines_match(content_lines: list[str], old_lines: list[str]) -> list[int]:
    """Layer 3: match by structural equivalence (whitespace-insensitive).
    Returns list of starting indices where old_lines matches content_lines."""
    norm_content = [_normalize_structural_line(l) for l in content_lines]
    norm_old = [_normalize_structural_line(l) for l in old_lines]

    # Collapse blank lines on both sides for comparison
    # Build a mapping from collapsed-index → original-index for content
    collapsed_content = []
    collapsed_to_orig = []
    prev_blank = False
    for i, ln in enumerate(norm_content):
        is_blank = ln == ""
        if is_blank and prev_blank:
            continue
        collapsed_content.append(ln)
        collapsed_to_orig.append(i)
        prev_blank = is_blank

    collapsed_old = _collapse_blank_lines(norm_old)
    # Remove leading/trailing empty strings from old (models often add/remove edge blanks)
    while collapsed_old and collapsed_old[0] == "":
        collapsed_old.pop(0)
    while collapsed_old and collapsed_old[-1] == "":
        collapsed_old.pop()

    if not collapsed_old:
        return []

    n = len(collapsed_old)
    matches = []
    for i in range(len(collapsed_content) - n + 1):
        if collapsed_content[i:i + n] == collapsed_old:
            matches.append(collapsed_to_orig[i])
    return matches


# --------------------------------------------------------------------------
# Utilities
# --------------------------------------------------------------------------

def _count_occurrences(haystack: str, needle: str) -> int:
    if not needle:
        return 0
    return haystack.count(needle)


class ToolError(Exception):
    """Raised for user/model-correctable errors (exit code 1)."""


# --------------------------------------------------------------------------
# Line-ending handling
# --------------------------------------------------------------------------

def _read_raw(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="strict", newline="") as f:
            return f.read()
    except UnicodeDecodeError:
        # Try to detect encoding
        with open(path, "rb") as f:
            raw_bytes = f.read(4096)
        # Simple heuristic
        if b"\x00" in raw_bytes:
            hint = "likely binary or UTF-16"
        else:
            hint = "not UTF-8 (possibly latin-1 or cp1252)"
        raise ToolError(
            f"'{path}' is {hint}. This tool only handles UTF-8 text files. "
            f"Use <get_file> to upload non-text files to context."
        )


def _detect_line_ending(text: str) -> str:
    crlf = text.count("\r\n")
    lf_only = text.count("\n") - crlf
    return "\r\n" if crlf > lf_only else "\n"


def _write_raw(path: str, text: str, line_ending: str) -> None:
    if line_ending == "\r\n":
        text = text.replace("\r\n", "\n").replace("\n", "\r\n")
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


# --------------------------------------------------------------------------
# view
# --------------------------------------------------------------------------

def list_dir(path: str, max_depth: int = 2) -> str:
    if not os.path.isdir(path):
        raise ToolError(f"'{path}' is not a directory")

    ignore = {"node_modules", "__pycache__", "venv", ".git"}
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
        lines = lines[:-1]
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
        head_txt = "".join(f"{l}\n" for l in head)
        tail_start = total - len(tail)
        tail_txt = "".join(f"{l}\n" for l in tail)
        omitted = total - HEAD_TAIL_LINES * 2
        return (
            head_txt
            + f"\n    ... [{omitted} lines omitted — file has {total} lines total; "
              f"call view with start/end to see a specific range, or full=True for "
              f"the entire file] ...\n\n"
            + tail_txt
        )

    return "\n".join(selected)


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

    # Auto-chmod if shebang present
    if content.startswith("#!"):
        st = os.stat(path)
        os.chmod(path, st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    line_count = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
    stats = (
        f"\n── stats ──\n"
        f"lines: {line_count}\n"
        f"bytes: {len(content.encode('utf-8'))}\n"
        f"executable: {'yes (shebang detected)' if content.startswith('#!') else 'no'}"
    )
    return f"Created '{path}' ({len(content)} bytes, {line_count} lines){stats}"


# --------------------------------------------------------------------------
# backups
# --------------------------------------------------------------------------

def _backup(path: str) -> str:
    backup_dir = os.path.join(os.path.dirname(os.path.abspath(path)) or ".", BACKUP_DIR_NAME)
    os.makedirs(backup_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = os.path.basename(path)
    name = f"{base}.{stamp}.bak"
    dest = os.path.join(backup_dir, name)
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


# --------------------------------------------------------------------------
# diff + stats
# --------------------------------------------------------------------------

def _full_diff_body(before: str, after: str, path: str) -> list[str]:
    """Full unified diff body (hunks only, no file headers, never truncated)."""
    diff = list(difflib.unified_diff(
        before.split("\n"), after.split("\n"),
        fromfile=path, tofile=path,
        n=DIFF_CONTEXT_LINES, lineterm="",
    ))
    return diff[2:] if diff else []


def _diff_snippet(body: list[str]) -> str:
    """Render a diff body for display, truncating past MAX_DIFF_LINES."""
    if not body:
        return ""
    if len(body) > MAX_DIFF_LINES:
        body = body[:MAX_DIFF_LINES] + [f"... ({len(body) - MAX_DIFF_LINES} more diff lines omitted)"]
    return "\n".join(body)


def _compute_stats(before: str, after: str, diff_body: list[str]) -> str:
    """Compute structured stats from before/after content and full diff body."""
    before_lines = before.count("\n") + (1 if before and not before.endswith("\n") else 0)
    after_lines = after.count("\n") + (1 if after and not after.endswith("\n") else 0)

    added = 0
    removed = 0
    first_hunk_line = None
    last_hunk_line = None
    for line in diff_body:
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
        elif line.startswith("@@"):
            # Parse @@ -old_start,old_count +new_start,new_count @@
            m = re.search(r"\+(\d+)", line)
            if m:
                hunk_start = int(m.group(1))
                if first_hunk_line is None:
                    first_hunk_line = hunk_start
                last_hunk_line = hunk_start

    net = after_lines - before_lines
    net_str = f"+{net}" if net > 0 else str(net)

    affected = ""
    if first_hunk_line is not None:
        if last_hunk_line and last_hunk_line != first_hunk_line:
            affected = f"affected_range: L{first_hunk_line}–L{last_hunk_line + added}"
        else:
            affected = f"affected_range: L{first_hunk_line}–L{first_hunk_line + added + removed}"

    stats = (
        f"\n── stats ──\n"
        f"lines_before: {before_lines}\n"
        f"lines_after: {after_lines}\n"
        f"added: {added}\n"
        f"removed: {removed}\n"
        f"net: {net_str}\n"
        f"{affected}"
    )
    return stats


# --------------------------------------------------------------------------
# edit — layered matching, mandatory uniqueness, atomic multi-edit
# --------------------------------------------------------------------------

def _find_nearest_match(content: str, old_str: str) -> str:
    """Find the closest block in content to old_str using SequenceMatcher.
    Returns a suggestion string or empty string if nothing close enough."""
    content_lines = content.split("\n")
    old_lines = old_str.split("\n")
    n = len(old_lines)
    if n == 0:
        return ""

    best_ratio = 0.0
    best_start = -1
    # Sliding window with slack
    window_sizes = [n, n + NEAREST_MATCH_WINDOW_SLACK, max(1, n - NEAREST_MATCH_WINDOW_SLACK)]
    for ws in window_sizes:
        if ws > len(content_lines):
            continue
        for i in range(len(content_lines) - ws + 1):
            candidate = "\n".join(content_lines[i:i + ws])
            ratio = difflib.SequenceMatcher(None, old_str, candidate).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_start = i

    if best_ratio < NEAREST_MATCH_THRESHOLD or best_start < 0:
        return ""

    # Show the closest block with line numbers
    ws = min(n + NEAREST_MATCH_WINDOW_SLACK, len(content_lines) - best_start)
    snippet_lines = content_lines[best_start:best_start + ws]
    numbered = "".join(f"{l}\n" for l in snippet_lines)
    return (
        f"\nClosest match (ratio {best_ratio:.2f}) at lines {best_start + 1}–{best_start + ws}:\n"
        f"{numbered}"
        f"Re-view that range and copy old_str exactly from the output."
    )


def _find_unique_span(content: str, old_str: str, replace_all: bool = False):
    """Returns (start_index, end_index, note) for the unique match of old_str.
    Matching cascade: exact → normalized → structural.
    If replace_all=True, returns the FIRST match (caller handles iteration).
    Raises ToolError if not found or ambiguous."""

    # Layer 1: exact
    count = _count_occurrences(content, old_str)
    if count >= 1:
        if count > 1 and not replace_all:
            raise ToolError(
                f"old_str matches {count} locations. Add more surrounding lines "
                f"to old_str (and the identical lines to new_str) so it matches "
                f"exactly once, or use replace_all mode.\n--- old_str was ---\n{old_str}"
            )
        idx = content.index(old_str)
        return idx, idx + len(old_str), None

    # Layer 2: normalized (quotes, dashes, trailing-ws, tabs)
    content_lines = content.split("\n")
    old_lines = old_str.split("\n")
    norm_content_lines = [_normalize(l) for l in content_lines]
    norm_old_lines = [_normalize(l) for l in old_lines]
    n = len(norm_old_lines)
    matches = [
        i for i in range(len(norm_content_lines) - n + 1)
        if norm_content_lines[i:i + n] == norm_old_lines
    ]
    if len(matches) == 1 or (len(matches) >= 1 and replace_all):
        i = matches[0]
        start_idx = len("\n".join(content_lines[:i])) + (1 if i > 0 else 0)
        end_idx = start_idx + len("\n".join(content_lines[i:i + n]))
        note = "matched after normalizing quotes/dashes/whitespace"
        if len(matches) > 1:
            note += f" ({len(matches)} total occurrences, replace_all mode)"
        return start_idx, end_idx, note
    if len(matches) > 1:
        raise ToolError(
            f"old_str matches {len(matches)} locations after normalization. "
            f"Add more surrounding lines so it matches exactly once, "
            f"or use replace_all mode.\n--- old_str was ---\n{old_str}"
        )

    # Layer 3: structural (indentation-insensitive, blank-line collapse)
    struct_matches = _structural_lines_match(content_lines, old_lines)
    if len(struct_matches) == 1 or (len(struct_matches) >= 1 and replace_all):
        i = struct_matches[0]
        span_count = _structural_span_count(content_lines, old_lines, i)
        start_idx = len("\n".join(content_lines[:i])) + (1 if i > 0 else 0)
        end_idx = start_idx + len("\n".join(content_lines[i:i + span_count]))
        note = "matched after structural normalization (indentation/blank-lines ignored)"
        if len(struct_matches) > 1:
            note += f" ({len(struct_matches)} total occurrences, replace_all mode)"
        return start_idx, end_idx, note
    if len(struct_matches) > 1:
        raise ToolError(
            f"old_str matches {len(struct_matches)} locations after structural "
            f"normalization (whitespace-insensitive). Add more surrounding lines "
            f"so it matches exactly once, or use replace_all mode.\n"
            f"--- old_str was ---\n{old_str}"
        )

    # Total failure — find nearest match for a helpful suggestion
    suggestion = _find_nearest_match(content, old_str)
    raise ToolError(
        "old_str was not found, even after normalizing quotes/dashes/whitespace "
        "and structural (indentation-insensitive) matching. "
        "Call view_file on this path again and copy old_str exactly from the "
        "output — do not retype it from memory.\n"
        f"--- old_str was ---\n{old_str}\n"
        f"--- repr ---\n{repr(old_str)}"
        + (f"\n{suggestion}" if suggestion else "")
    )


def _find_all_spans(content: str, old_str: str) -> list[tuple[int, int, str | None]]:
    """Find ALL non-overlapping spans of old_str in content (for replace_all).
    Uses the same matching cascade but collects all matches."""
    spans = []

    # Try exact first
    search_start = 0
    while True:
        idx = content.find(old_str, search_start)
        if idx == -1:
            break
        spans.append((idx, idx + len(old_str), None))
        search_start = idx + len(old_str)

    if spans:
        return spans

    # Try normalized line-by-line
    content_lines = content.split("\n")
    old_lines = old_str.split("\n")
    norm_content_lines = [_normalize(l) for l in content_lines]
    norm_old_lines = [_normalize(l) for l in old_lines]
    n = len(norm_old_lines)
    for i in range(len(norm_content_lines) - n + 1):
        if norm_content_lines[i:i + n] == norm_old_lines:
            start_idx = len("\n".join(content_lines[:i])) + (1 if i > 0 else 0)
            end_idx = start_idx + len("\n".join(content_lines[i:i + n]))
            spans.append((start_idx, end_idx, "normalized"))

    if spans:
        return spans

    # Try structural
    struct_matches = _structural_lines_match(content_lines, old_lines)
    for i in struct_matches:
        span_count = _structural_span_count(content_lines, old_lines, i)
        start_idx = len("\n".join(content_lines[:i])) + (1 if i > 0 else 0)
        end_idx = start_idx + len("\n".join(content_lines[i:i + span_count]))
        spans.append((start_idx, end_idx, "structural"))

    if not spans:
        suggestion = _find_nearest_match(content, old_str)
        raise ToolError(
            "old_str was not found anywhere in the file (replace_all mode).\n"
            f"--- old_str was ---\n{old_str}"
            + (f"\n{suggestion}" if suggestion else "")
        )
    return spans


def edit_file(path: str, edits, backup: bool = True,
              replace_all: bool = False, dry_run: bool = False) -> str:
    """edits: a single {"old_str", "new_str"} dict, or a list of them.
    All edits validated against ORIGINAL content before any write (atomic).
    replace_all: replace ALL occurrences instead of requiring uniqueness.
    dry_run: validate and show diff but do NOT write."""
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
    content = raw.replace("\r\n", "\n")

    # Collect all spans
    all_spans = []  # (start, end, edit_index, note)

    if replace_all:
        for i, e in enumerate(edits):
            spans = _find_all_spans(content, e["old_str"])
            for start, end, note in spans:
                all_spans.append((start, end, i, note))
    else:
        for i, e in enumerate(edits):
            try:
                start, end, note = _find_unique_span(content, e["old_str"])
            except ToolError as err:
                raise ToolError(f"edit #{i + 1} of {len(edits)} failed: {err}")
            # Check overlap
            for s2, e2, *_ in all_spans:
                if start < e2 and s2 < end:
                    raise ToolError(
                        f"edit #{i + 1} overlaps another edit in this batch — "
                        f"split into separate calls or merge them into one old_str/new_str"
                    )
            all_spans.append((start, end, i, note))

    if not all_spans:
        raise ToolError("no matches found for any edit in the batch")

    # Sort and check overlaps for replace_all mode
    all_spans.sort(key=lambda t: t[0])
    for j in range(1, len(all_spans)):
        if all_spans[j][0] < all_spans[j - 1][1]:
            raise ToolError(
                "overlapping matches detected in replace_all mode — "
                "the old_str occurrences overlap in the file"
            )

    # Apply back-to-front
    new_content = content
    total_replacements = len(all_spans)
    for start, end, i, note in sorted(all_spans, key=lambda t: t[0], reverse=True):
        new_content = new_content[:start] + edits[i]["new_str"] + new_content[end:]

    diff_body = _full_diff_body(content, new_content, path)
    diff = _diff_snippet(diff_body)
    stats = _compute_stats(content, new_content, diff_body)

    if dry_run:
        header = f"DRY RUN — '{path}': {total_replacements} replacement(s) validated, NO changes written"
        notes = [n for *_, n in all_spans if n]
        if notes:
            header += f"\n(note: {len(notes)} match(es) used normalization/structural matching)"
        return f"{header}\n{diff}{stats}" if diff else header + stats

    if backup:
        _backup(path)
    _write_raw(path, new_content, line_ending)

    notes = [n for *_, n in all_spans if n]
    warn = ""
    if notes:
        warn = f"\n(note: {len(notes)} of {total_replacements} match(es) used normalization/structural matching)"

    n_edits = len(edits)
    if replace_all and total_replacements > n_edits:
        header = f"Edited '{path}' ({total_replacements} replacements across {n_edits} pattern{'s' if n_edits != 1 else ''}){warn}"
    else:
        header = f"Edited '{path}' ({total_replacements} change{'s' if total_replacements != 1 else ''}){warn}"
    return f"{header}\n{diff}{stats}" if diff else header + stats


# --------------------------------------------------------------------------
# insert
# --------------------------------------------------------------------------

def insert_file(path: str, content: str, at_line: int = None, after_str: str = None,
                backup: bool = True, dry_run: bool = False) -> str:
    """Insert `content` as new lines. Exactly one of at_line / after_str required."""
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
        insert_pos = text[:end].count("\n")
        if not text[:end].endswith("\n"):
            insert_pos += 1

    new_block = content.split("\n")
    new_lines = lines[:insert_pos] + new_block + lines[insert_pos:]
    new_text = "\n".join(new_lines) + ("\n" if trailing_newline else "")

    diff_body = _full_diff_body(text, new_text, path)
    diff = _diff_snippet(diff_body)
    stats = _compute_stats(text, new_text, diff_body)

    if dry_run:
        header = f"DRY RUN — insert into '{path}' at line {insert_pos + 1}, NO changes written"
        return f"{header}\n{diff}{stats}" if diff else header + stats

    if backup:
        _backup(path)
    _write_raw(path, new_text, line_ending)

    header = f"Inserted into '{path}' at line {insert_pos + 1} ({len(new_block)} lines added)"
    return f"{header}\n{diff}{stats}" if diff else header + stats


# --------------------------------------------------------------------------
# SEARCH/REPLACE block parser
# --------------------------------------------------------------------------

_SEARCH_MARK = "<<<<<<< SEARCH"
_DIVIDER_MARK = "======="
_REPLACE_MARK = ">>>>>>> REPLACE"


def _parse_search_replace_blocks(text: str):
    """Parse SEARCH/REPLACE marker blocks. Returns dict or list of dicts."""
    lines = text.split("\n")
    blocks = []
    state = "outside"
    cur_old, cur_new = [], []
    for line in lines:
        stripped = line.strip()
        if state == "outside":
            if stripped == _SEARCH_MARK:
                state, cur_old = "search", []
        elif state == "search":
            if stripped == _DIVIDER_MARK:
                state, cur_new = "replace", []
            else:
                cur_old.append(line)
        elif state == "replace":
            if stripped == _REPLACE_MARK:
                blocks.append({"old_str": "\n".join(cur_old), "new_str": "\n".join(cur_new)})
                state = "outside"
            else:
                cur_new.append(line)

    if state != "outside":
        raise ToolError(
            "Unterminated SEARCH/REPLACE block: missing the closing "
            f"'{_DIVIDER_MARK}' or '{_REPLACE_MARK}' marker line."
        )
    if not blocks:
        raise ToolError(
            f"No SEARCH/REPLACE blocks found. Expected at least one block "
            f"delimited by '{_SEARCH_MARK}' / '{_DIVIDER_MARK}' / '{_REPLACE_MARK}'."
        )
    return blocks if len(blocks) > 1 else blocks[0]


# --------------------------------------------------------------------------
# JSON payload loading (legacy)
# --------------------------------------------------------------------------

def _load_json_payload(raw_text: str, source_label: str):
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError as e:
        pos = e.pos
        window = 40
        snippet = raw_text[max(0, pos - window):pos + window]
        raise ToolError(
            f"JSON parse error in {source_label} at byte {pos}: {e.msg}\n"
            f"Context: ...{snippet}...\n"
            f"Hint: if this is hand-typed code, use SEARCH/REPLACE blocks instead of JSON."
        )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _cli():
    parser = argparse.ArgumentParser(description="editor_tools v4")
    sub = parser.add_subparsers(dest="command", required=True)

    p_view = sub.add_parser("view")
    p_view.add_argument("path")
    p_view.add_argument("--start", type=int, default=None)
    p_view.add_argument("--end", type=int, default=None)
    p_view.add_argument("--full", action="store_true")

    p_create = sub.add_parser("create")
    p_create.add_argument("path")
    p_create.add_argument("--content-file")
    p_create.add_argument("--overwrite", action="store_true")

    p_edit = sub.add_parser("edit")
    p_edit.add_argument("path")
    p_edit.add_argument("--diff-file")
    p_edit.add_argument("--json-file")
    p_edit.add_argument("--no-backup", action="store_true")
    p_edit.add_argument("--replace-all", action="store_true",
                        help="Replace ALL occurrences instead of requiring unique match")
    p_edit.add_argument("--dry-run", action="store_true",
                        help="Validate and show diff without writing")

    p_insert = sub.add_parser("insert")
    p_insert.add_argument("path")
    p_insert.add_argument("--content-file")
    p_insert.add_argument("--at-line", type=int, default=None)
    p_insert.add_argument("--after-str", default=None)
    p_insert.add_argument("--after-file", default=None)
    p_insert.add_argument("--json-file")
    p_insert.add_argument("--no-backup", action="store_true")
    p_insert.add_argument("--dry-run", action="store_true")

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
                    payload = _load_json_payload(f.read(), f"--json-file '{args.json_file}'")
            else:
                if args.diff_file:
                    with open(args.diff_file, "r", encoding="utf-8") as f:
                        raw_text = f.read()
                else:
                    raw_text = sys.stdin.read()
                payload = _parse_search_replace_blocks(raw_text)
            print(edit_file(
                args.path, payload,
                backup=not args.no_backup,
                replace_all=args.replace_all,
                dry_run=args.dry_run,
            ))

        elif args.command == "insert":
            if args.content_file and (args.at_line is not None or args.after_str or args.after_file):
                with open(args.content_file, "r", encoding="utf-8") as f:
                    content = f.read()
                after_str = args.after_str
                if args.after_file:
                    if after_str is not None:
                        raise ToolError("pass only one of --after-str / --after-file")
                    with open(args.after_file, "r", encoding="utf-8") as f:
                        after_str = f.read()
                print(insert_file(
                    args.path, content,
                    at_line=args.at_line, after_str=after_str,
                    backup=not args.no_backup,
                    dry_run=args.dry_run,
                ))
            elif args.json_file:
                with open(args.json_file, "r", encoding="utf-8") as f:
                    payload = _load_json_payload(f.read(), f"--json-file '{args.json_file}'")
                print(insert_file(
                    args.path, payload["content"],
                    at_line=payload.get("at_line"), after_str=payload.get("after_str"),
                    backup=not args.no_backup,
                    dry_run=args.dry_run,
                ))
            elif args.content_file:
                raise ToolError("--content-file requires --at-line, --after-str, or --after-file")
            else:
                payload = _load_json_payload(sys.stdin.read(), "stdin")
                print(insert_file(
                    args.path, payload["content"],
                    at_line=payload.get("at_line"), after_str=payload.get("after_str"),
                    backup=not args.no_backup,
                    dry_run=args.dry_run,
                ))

    except ToolError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Internal error: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    _cli()
