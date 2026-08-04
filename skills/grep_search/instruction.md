
# Grep Search

Search file contents, find files by pattern, list directories. All paths sandboxed to allowed roots ($PROJECT_ROOT, $HOME, /tmp).

## Tags

### `<grep>` — Search file contents
Uses ripgrep (falls back to grep). Returns `file:line:match` format.

```xml
<grep pattern="regex_pattern" path="/optional/dir" glob="*.py" ignore_case="true" max_results="50" />
```

| Attribute | Required | Description |
|:--|:--|:--|
| pattern | ✅ | Regex pattern to search |
| path | ❌ | Directory to search (default: $PROJECT_ROOT) |
| glob | ❌ | File glob filter (e.g. `*.py`, `*.{js,ts}`) |
| ignore_case | ❌ | Case-insensitive match (`true`/`false`) |
| max_results | ❌ | Max matches to return (default: 50, max: 200) |

### `<glob>` — Find files by pattern
Returns relative paths sorted by modification time (newest first).

```xml
<glob pattern="**/*.py" path="/optional/base" />
```

| Attribute | Required | Description |
|:--|:--|:--|
| pattern | ✅ | Glob pattern (e.g. `**/*.py`, `src/**/test_*.py`) |
| path | ❌ | Base directory (default: $PROJECT_ROOT) |

### `<list_dir>` — List directory contents
Folders first, then files with sizes.

```xml
<list_dir path="/some/directory" />
```

| Attribute | Required | Description |
|:--|:--|:--|
| path | ❌ | Directory to list (default: $PROJECT_ROOT) |

## Rules
- Always use absolute paths or let defaults resolve
- Paths outside allowed roots are rejected
- Respects .gitignore when using grep (ripgrep default)
- For reading file content after finding it, use code_editor view_file
