# Grep Search

Search file contents, find files by pattern, list directories. All paths sandboxed to allowed roots ($PROJECT_ROOT, $HOME, /tmp).

## Tags

### `<grep>` — Search file contents
Uses ripgrep (falls back to grep). Returns `file:line:match` format.
Default output capped at ~25k chars with a truncation notice. Use `full="true"` to bypass the cap.

<action>
<grep pattern="regex_pattern" path="/optional/dir" glob="*.py" exclude="dist/,*.lock" ignore_case="true" max_results="50" />
</action>

| Attribute | Required | Description |
|:--|:--|:--|
| pattern | ✅ | Regex pattern to search |
| path | ❌ | Directory to search (default: $PROJECT_ROOT) |
| glob | ❌ | File glob filter (e.g. `*.py`, `*.{js,ts}`) |
| exclude | ❌ | Comma-separated globs to exclude (e.g. `dist/,*.lock,build/`) |
| ignore_case | ❌ | Case-insensitive match (`true`/`false`) |
| max_results | ❌ | Max matches to return (default: 50, max: 200) |
| full | ❌ | `"true"` bypasses the 25k char output cap |

### `<glob>` — Find files by pattern
Returns relative paths sorted by modification time (newest first).

<action>
<glob pattern="**/*.py" path="/optional/base" />
</action>

| Attribute | Required | Description |
|:--|:--|:--|
| pattern | ✅ | Glob pattern (e.g. `**/*.py`, `src/**/test_*.py`) |
| path | ❌ | Base directory (default: $PROJECT_ROOT) |

### `<list_dir>` — List directory contents
Folders first, then files with sizes.

<action>
<list_dir path="/some/directory" />
</action>

| Attribute | Required | Description |
|:--|:--|:--|
| path | ❌ | Directory to list (default: $PROJECT_ROOT) |

## Rules
- Always use absolute paths or let defaults resolve
- Paths outside allowed roots are rejected
- Respects .gitignore when using grep (ripgrep default)
- For reading file content after finding it, use code_editor view_file
