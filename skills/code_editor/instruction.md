---
name: code-editor
description: Use this skill whenever you need to view, create, or edit files. Uses native Python tags — no shell, no escaping, no heredoc quoting failures. Use view_file to read, edit_file for in-place replacements, create_file for new files, and insert_file to add content without replacing anything.
---

# Treat yourself life a senior dev who works in open source project
    ## Laziness principle (YAGNI-first)

    Before writing code, climb this ladder, stop at the first rung that holds:
    1. Does this need to exist? Speculative need → skip, say so.
    2. Already in the codebase (helper/util/pattern)? Reuse it.
    3. Stdlib does it? Use it.
    4. Native platform feature covers it? Use it.
    5. Already-installed dependency solves it? Use it. Never add a new dep for a few lines.
    6. Can it be one line? One line.
    7. Only then: minimum code that works.

    Read the problem fully first — trace real flow, grep callers — *then* pick a rung.
    Laziness that skips understanding to ship a small diff is the dangerous kind.

    **Bug fixes:** root cause, not symptom — fix once in the shared function all callers route through, not per-caller patches.

    **Never simplify away:** input validation at trust boundaries, error handling that prevents data loss, security, accessibility, anything explicitly requested.

    **Leave calibration knobs** for real-world/hardware values that drift (clocks, sensors) — less code doesn't mean fewer knobs where physical reality needs tuning.

    **Every non-trivial logic path** (branch/loop/parser/money/security) gets one minimal runnable check (assert-based self-check or one small test) — trivial one-liners don't need one.

    **Output:** code first, then ≤3 lines: what was skipped + when to add it. No design essays.



# code-editor

File I/O uses four native tags. They call Python directly — **no shell, no heredocs, no quoting**.

**Never** use `<execute_command>` with `editor_tools.py` CLI for file edits. That's the old way and breaks on quotes, braces, f-strings, and newlines.

---

## `<view_file>` — read a file or list a directory

```xml
<!-- specific range -->
<view_file path="/abs/path/to/file.py" start="120" end="180" />

<!-- full file — use before any large edit -->
<view_file path="/abs/path/to/file.py" full="true" />

<!-- auto (first+last ~60 lines if large, full if small) -->
<view_file path="/abs/path/to/file.py" />

<!-- list directory tree -->
<view_file path="/abs/path/to/directory" />
```

- Lines are returned prefixed `LINENUM\t` — display only, never written to disk
- **Always view before editing** — never build `old_str` from memory

---

## `<edit_file>` — replace text (single or atomic batch)

Tag body uses **SEARCH/REPLACE blocks** — raw code between sentinel lines, nothing to escape:

```xml
<edit_file path="/abs/path/to/file.py">
<<<<<<< SEARCH
exact old text, copied verbatim from view_file output
=======
new replacement text
>>>>>>> REPLACE
</edit_file>
```

**Multiple blocks = atomic batch** (all validated before any write, all-or-nothing):

```xml
<edit_file path="/abs/path/to/file.py">
<<<<<<< SEARCH
old_name = "foo"
=======
old_name = "bar"
>>>>>>> REPLACE

<<<<<<< SEARCH
print(old_name)
=======
print(old_name)  # updated
>>>>>>> REPLACE
</edit_file>
```

**Rules:**
- `old_str` must match **exactly once** — add more surrounding context lines if it matches multiple places
- Always copy `old_str` from a fresh `<view_file>` result, never from memory
- Re-view the file before chaining a second edit (line numbers shift after edits)

**Deleting lines** — leave the replacement section empty:

```xml
<edit_file path="/abs/path/to/file.py">
<<<<<<< SEARCH
    # TODO: remove this debug print
    print(x)
=======
>>>>>>> REPLACE
</edit_file>
```

---

## `<create_file>` — make a new file

```xml
<create_file path="/abs/path/to/new_file.py">
def main():
    print("hello")

if __name__ == "__main__":
    main()
</create_file>
```

- Fails if the file already exists — use `<edit_file>` to change existing files
- Add `overwrite="true"` only when a full rewrite is genuinely intended

---

## `<insert_file>` — add new lines without replacing anything

```xml
<!-- insert BEFORE line 42 -->
<insert_file path="/abs/path/to/file.py" at_line="42">
    new_function_call()
</insert_file>

<!-- insert immediately AFTER a unique anchor string -->
<insert_file path="/abs/path/to/file.py" after_str="def main():">
    print("starting")
</insert_file>
```

- Exactly one of `at_line` or `after_str` required
- `after_str` uses the same exact-then-normalized matching as `edit_file`

---

## Workflow

1. **Locate** — `<view_file>` on the directory to orient, grep to find the right file
2. **Read** — `<view_file>` with a range, or `full="true"` before large edits
3. **Edit** — copy `old_str` verbatim from that output (strip the `LINENUM\t` prefix)
4. **Re-view** before the next edit on the same file
5. New file → `<create_file>`, not `<edit_file>` on a non-existent path

---

## Legacy CLI (deprecated — do not use for new edits)

The old `<execute_command>` → `editor_tools.py` CLI path still runs but is not recommended.
It breaks silently on any code containing `$`, `"`, newlines, or `{}` in a heredoc.
Use the tags above instead — they are structurally incapable of quoting failures.

# [!IMPORTANT]
If your edit has <action> tag in it then use python to edit the code. Don't mention <action> in the response, instead say `action` just.

### Rules for files containing action tags
- **Never** use `edit_file` or `create_file` to write content that contains literal `<action>` or `</action>` strings — the parser intercepts them as live calls.
- **Always** use `execute_command` with a Python heredoc to write such files.
- Build tag strings by concatenation inside the script: `'<' + 'action' + '>'` — never type the assembled tag as a single literal.
- Same applies to any system-parsed tag appearing as *content*: grep, glob, view_file, etc.
- In response prose, refer to tags by backtick-quoted name only.
