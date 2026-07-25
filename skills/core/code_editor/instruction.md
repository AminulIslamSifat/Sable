---
name: code-editor
description: Use this skill whenever an agent needs to view, create, or edit source files during a coding task. Covers reading files with line numbers (with automatic truncation for large files, or the full file via --full), creating new files, and making precise in-place edits via exact/normalized string matching — including large multi-line section replacements. Do NOT let the model write, overwrite, or splice file content through bash heredocs redirected to a file, `cat > file << EOF`, shell redirection, or ad-hoc scripts (`python3 -c`, inline readlines()/write() splicing, etc.). ALL file mutation goes through these tools, with no exceptions for "big" edits.
---

# code-editor

File I/O goes through these tools, never through the model
hand-authoring file mutation itself — not via `cat > file << EOF`,
not via shell redirection (`>`, `>>`), and not via an ad-hoc script
(`python3 -c "..."`, a throwaway `.py` file that does
`open(path).readlines()` / `.write()`, etc.). Bash is for *running*
things (tests, builds, git, the tool itself); it is never used to
*author or splice file content* directly onto disk. If a change feels
too big or awkward to express as `edit`/`insert` calls, that's a signal
to re-read the "Large section replacements" section below — not a
reason to reach for Python.

This applies equally to a single-line fix and a 200-line section
rewrite. There is no size threshold past which bypassing the tool
becomes acceptable.

## ⚠️ Shell quoting — read this before piping anything into the tool

All these tools take content on **stdin** (or `--content-file` /
`--json-file`). How you pipe that content into stdin matters:

**Default: heredoc with a quoted delimiter — `cat << 'EOF'`.**
The quotes around the delimiter (`'EOF'`, not `EOF`) tell the shell to
pass everything between the markers as raw, literal bytes: no `$`
expansion, no backtick execution, no brace expansion, no quote
stripping. This is what makes heredoc safe for real code (f-strings,
nested quotes, `{}`, `$var`-looking text, JSON) — it is piped *into*
`editor_tools.py`'s stdin, not redirected to a file, so the tool still
does the writing:

```bash
cat << 'PYEOF' | python3 PROJECT_ROOT/skills/core/code_editor/scripts/editor_tools.py create path/to/file.py
def greet(name: str) -> str:
    sep = "=" * 40
    print(f"\n{sep}")
    return f"Hello, {name}!"
PYEOF
```

```bash
cat << 'JSON' | python3 PROJECT_ROOT/skills/core/code_editor/scripts/editor_tools.py edit path/to/file.py
{"old_str": "print(f\"hello {name}\")", "new_str": "print(f\"bye {name}\")"}
JSON
```

**`echo "..."` is only for trivial one-liners with zero special
characters** (no quotes-within-quotes, no `{}`, no `$`, no backticks,
no f-strings):

```bash
# OK — nothing for the shell to mangle
echo "import json" | python3 PROJECT_ROOT/skills/core/code_editor/scripts/editor_tools.py create path/to/file.py
```

```bash
# NEVER — will break, silently or loudly
echo 'print(f"{'='*40}")' | python3 ... create file.py
echo '{"old_str": "def foo():\n    return 1"}' | python3 ... edit file.py
```

If you're unsure whether a piece of content is "trivial," it isn't —
use heredoc. Heredoc is never wrong for this; `echo` is only ever a
shortcut for the simplest case.

**Delimiter choice:** always quote it (`'PYEOF'`, `'JSON'`, `'EOF'`).
An unquoted delimiter (`<< EOF`) re-enables shell expansion and defeats
the entire point. Pick a delimiter unlikely to appear as a literal
line in your content (`'PYEOF'` for Python, `'JSON'` for JSON); if the
content might contain that exact line, use something rarer like
`'CONTENT_7F3A'`.

**Symptom checklist** — if you see any of these, it's a quoting
failure, not a real syntax error in your intended content: a
`SyntaxError` pointing at a brace or `=` inside an f-string you know is
correct; a JSON parse error where quotes look "shifted"; content that
got silently truncated at an apostrophe. Fix: redo the same call as a
heredoc. Do not "fix" it by rewriting the actual code to avoid the
character (e.g. replacing a string literal with `chr()` calls) —
that changes program behavior to dodge a shell problem and is worse
than the original bug.

## The tools

### 1. `view` — read a file (line-numbered) or list a directory

```bash
python3 PROJECT_ROOT/skills/core/code_editor/scripts/editor_tools.py view path/to/file.py
python3 PROJECT_ROOT/skills/core/code_editor/scripts/editor_tools.py view path/to/file.py --start 120 --end 180
python3 PROJECT_ROOT/skills/core/code_editor/scripts/editor_tools.py view path/to/file.py --full
python3 PROJECT_ROOT/skills/core/code_editor/scripts/editor_tools.py view path/to/directory
```

- Every line is prefixed `LINENUM\t` — display-only, never written to
  disk, **including with `--full`**. Use these numbers to talk about
  locations; don't maintain a separate numbered copy of the file.
- On a directory, returns a depth-limited tree (ignores `.git`,
  `node_modules`, `__pycache__`, venvs).
- On a large file with no `--start`/`--end`/`--full`, shows the first
  and last ~60 lines with a note on how many were omitted, instead of
  dumping everything. Prefer requesting a specific range once you know
  roughly where the relevant code is (e.g. from a `grep`).
- Pass `--full` to get the **entire file, numbered, no truncation**,
  regardless of size. Use it before any edit that spans a large or
  uncertain range — including the large-section-replacement case below
  — so `old_str` is built from ground truth, not memory.

### 2. `create` — make a new file

```bash
# Multi-line content — heredoc (default)
cat << 'PYEOF' | python3 PROJECT_ROOT/skills/core/code_editor/scripts/editor_tools.py create path/to/new_file.py
def main():
    print("hello world")
PYEOF

# Trivial one-liner — echo is fine
echo "import json" | python3 PROJECT_ROOT/skills/core/code_editor/scripts/editor_tools.py create path/to/new_file.py

# From a staged temp file
python3 PROJECT_ROOT/skills/core/code_editor/scripts/editor_tools.py create path/to/new_file.py --content-file /tmp/staged_content.txt
```

Refuses to run if the path already exists — use `edit` for changes to
an existing file. Pass `--overwrite` only when a full rewrite is
genuinely intended.

### 3. `edit` — precise in-place replacement (single or batch, atomic)

```bash
# single edit — heredoc for the JSON spec (default; safe for any content)
cat << 'JSON' | python3 PROJECT_ROOT/skills/core/code_editor/scripts/editor_tools.py edit path/to/file.py
{"old_str": "def foo():\n    return 1", "new_str": "def foo():\n    return 2"}
JSON

# batch edit — pipe the JSON array directly on stdin, same as a single edit.
# No temp file is required for batches; a temp file is only useful if you
# want to inspect/reuse the edit list. Prefer the direct pipe by default.
cat << 'JSON' | python3 PROJECT_ROOT/skills/core/code_editor/scripts/editor_tools.py edit path/to/file.py
[
    {"old_str": "old_name = 1", "new_str": "new_name = 1"},
    {"old_str": "print(old_name)", "new_str": "print(new_name)"},
    {"old_str": "result = old_name + 5", "new_str": "result = new_name + 5"}
]
JSON

# Optional: staged via a temp file first (useful if the edit list is huge
# or you want to review it before applying)
cat << 'JSON' | python3 PROJECT_ROOT/skills/core/code_editor/scripts/editor_tools.py create /tmp/edit.json --overwrite
[
    {"old_str": "old_name = 1", "new_str": "new_name = 1"}
]
JSON
python3 PROJECT_ROOT/skills/core/code_editor/scripts/editor_tools.py edit path/to/file.py --json-file /tmp/edit.json
```

- **Each `old_str` must match exactly one location.** Zero matches or
  multiple matches is a hard error that tells you how to fix it (add
  more surrounding context to make it unique) — never a guess.
- **A list of edits is applied atomically.** Every `old_str` is
  validated against the original content before anything is written.
  If any one fails to match uniquely, the whole call fails and the
  file is left completely untouched.
- **Matching is layered, not fuzzy:** exact match first; if that
  fails, a narrow normalization pass (smart quotes → straight quotes,
  unicode dashes → hyphen, odd whitespace → regular space, trailing
  whitespace stripped). If it still doesn't match uniquely, it fails
  loudly rather than guessing which block you meant.
- Original line-ending style (LF or CRLF) is preserved on write.
- Returns a short unified-diff snippet of what changed.
- Every edit is backed up first to `.editor_tools_backups/` next to
  the file (capped at 20 most recent per file).
- Always copy `old_str` from a fresh `view` result (plain or `--full`),
  not from memory — the file may have changed since you last saw it.
  Re-view before chaining a second edit onto the same file.

### Large section replacements (replacing dozens/hundreds of lines)

This is still an `edit` call, not a special case — it just has a big
`old_str`. Never drop into `python3 -c` or a throwaway script to slice
`readlines()` and rewrite the file; that skips the uniqueness check,
the automatic backup, and the diff confirmation that make `edit` safe.

1. `view --full` (or a `--start`/`--end` range covering the whole
   section) to get the exact current text, line-numbered.
2. Copy the section verbatim (strip the `LINENUM\t` prefix) as
   `old_str`, from its unique starting anchor to its unique ending
   anchor — it doesn't need to be the *entire* rest of the file, just
   enough to uniquely bound the block you're replacing.
3. Write the full new section as `new_str`.
4. Pipe the `{"old_str": ..., "new_str": ...}` object in via heredoc
   (the content will contain quotes/braces/newlines — never `echo` it).
5. Check the returned diff snippet before moving on.

If `old_str` is large enough that hand-typing the JSON escaping is
error-prone, stage `old_str`/`new_str` into a JSON file with `create`
(heredoc, `--overwrite` if reusing the path) and apply with
`edit --json-file`, per the batch example above — still never with a
hand-rolled Python read/write.

### Deleting lines (no separate tool — use `edit` with an empty `new_str`)

There's no `delete` command. To remove lines without adding anything,
call `edit` with `new_str` set to `""`. `old_str` can never be empty
(there'd be nothing to locate), but `new_str` being empty is fine and
is the intended way to delete:

```bash
cat << 'JSON' | python3 PROJECT_ROOT/skills/core/code_editor/scripts/editor_tools.py edit path/to/file.py
{"old_str": "    # TODO: remove this debug print\n    print(x)\n", "new_str": ""}
JSON
```

Include the line's trailing `\n` in `old_str` (copy it as a full line,
not just the visible text) — otherwise you'll delete the text but leave
a stray blank line behind. This works in a batch too: mix
`{"old_str": ..., "new_str": ""}` deletions with normal replacements in
the same atomic call.

### 4. `insert` — add new content without replacing anything

For pure additions (a new import, a new function, a line after some
anchor) where there's no existing text to replace:

```bash
# insert BEFORE a specific 1-indexed line number
cat << 'JSON' | python3 PROJECT_ROOT/skills/core/code_editor/scripts/editor_tools.py insert path/to/file.py
{"content": "import json", "at_line": 2}
JSON

# insert immediately AFTER a uniquely-matching anchor line
cat << 'JSON' | python3 PROJECT_ROOT/skills/core/code_editor/scripts/editor_tools.py insert path/to/file.py
{"content": "    print(\"starting\")", "after_str": "def main():"}
JSON
```

Exactly one of `at_line` / `after_str` required. Same backup and
diff-snippet behavior as `edit`.

## Recommended workflow

1. **Locate** — `view` a directory to orient, then `grep`/`ripgrep`
   for the relevant file(s) and approximate line numbers.
2. **Read** — `view` with a `--start`/`--end` range around what
   `grep` found, or `--full` if you need the whole thing (always use
   `--full` before a large section replacement).
3. **Edit** — build `old_str` from lines copied verbatim out of that
   output (strip the line-number prefix), write `new_str`, pipe both
   in via heredoc, call `edit`.
4. **Re-view before the next edit on the same file** — text can shift
   after any edit.
5. New file → `create` directly, don't `edit` a path that doesn't
   exist yet.
6. **If a step feels like it needs raw Python file I/O, stop.** Re-read
   "Large section replacements" — there's an `edit`/`insert` shape for
   it. The tool is never the wrong choice; a bigger `old_str` is.

## Wiring this into an agent

`PROJECT_ROOT/skills/core/code_editor/scripts/editor_tools.py` is pure Python 3 standard library, usable as a CLI
subprocess (exit 0 = success, 1 = correctable error — safe to retry
with fixed arguments, 2 = unexpected internal error) or as an import:
`from editor_tools import view_file, create_file, edit_file,
insert_file, list_dir, ToolError`.

Tool schemas to expose to the model:

```json
{
  "name": "view_file",
  "input_schema": {
    "path": "string",
    "start": "integer, optional",
    "end": "integer, optional (-1 = end of file)",
    "full": "boolean, optional — return the whole file numbered, bypassing truncation (ignores start/end)"
  }
}
{
  "name": "create_file",
  "input_schema": { "path": "string", "content": "string" }
}
{
  "name": "edit_file",
  "input_schema": {
    "path": "string",
    "edits": "array of {old_str: string, new_str: string} — one item for a single edit, several for an atomic batch"
  }
}
{
  "name": "insert_file",
  "input_schema": {
    "path": "string",
    "content": "string",
    "at_line": "integer, optional",
    "after_str": "string, optional — give exactly one of at_line / after_str"
  }
}
```