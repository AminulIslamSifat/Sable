name: code-editor
description: Use this skill whenever you need to view, create, or edit files. Uses native Python tags.
***

# code-editor

File I/O uses four native tags. They call Python directly — **no shell, no heredocs, no quoting**.
**Never** use `<execute_command>` with `editor_tools.py` CLI for file edits — breaks on quotes, braces, f-strings, newlines.

## Tag Reference

| Tag | Attributes | Body | Key Rules |
|:--|:--|:--|:--|
| `view_file` | `path` (req), `start`, `end`, `full` | *(empty)* | Omit range → auto (~60 lines). `full="true"` before large edits. Also lists directories. Returns `LINENUM\t` prefix (display only). **Always view before editing.** |
| `edit_file` | `path` (req) | `<<<<<<< SEARCH` / `=======` / `>>>>>>> REPLACE` block(s) | Multiple blocks = atomic batch. Empty REPLACE = delete. `old_str` must match **exactly once** — add context if ambiguous. Always copy from fresh `view_file`. Re-view before chaining edits. |
| `create_file` | `path` (req), `overwrite` | File content | Fails if file exists unless `overwrite="true"`. |
| `insert_file` | `path` (req), `at_line` XOR `after_str` | Lines to insert | Exactly one anchor required. `after_str` uses exact-match like `edit_file`. |

## Workflow

1. **Locate** — `view_file` on directory or grep to find target
2. **Read** — `view_file` with range or `full="true"` before editing
3. **Edit** — copy `old_str` verbatim from view output (strip `LINENUM\t`)
4. **Re-view** before next edit on same file
5. New file → `create_file`, never `edit_file` on non-existent path

***

### Rules for files containing action tags
- If your edit has <action> tag in it then use python to edit the code, **Always** use `execute_command` with a Python heredoc to write such files. Don't mention <action> in the response, instead say `action` just.
- Build tag strings by concatenation inside the script: '<' + 'action' + '>' — never type the assembled tag as a single literal.
- Same applies to any system-parsed tag appearing as *content*: grep, glob, view_file, etc.
- In response prose, refer to tags by backtick-quoted name only.
