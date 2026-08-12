# Grep Search

Search file contents, find files by pattern, list directories. All paths sandboxed to allowed roots ($PROJECT_ROOT, $HOME, /tmp).

## Tag Reference

| Tag | Attributes | Description |
|:--|:--|:--|
| `grep` | `pattern` (req), `path`, `glob`, `exclude`, `ignore_case`, `max_results`, `full` | Search file contents via ripgrep. Returns `file:line:match`. Output capped ~25k chars; `full="true"` bypasses. `max_results` default 50, max 200. Respects `.gitignore`. |
| `glob` | `pattern` (req), `path` | Find files by glob pattern. Returns relative paths sorted by mtime (newest first). |
| `list_dir` | `path` | List directory contents. Folders first, then files with sizes. |

All tags are self-closing. Default path is `$PROJECT_ROOT` when omitted.

## Rules

- Always use absolute paths or let defaults resolve
- Paths outside allowed roots are rejected
- For reading file content after finding it, use `view_file` from code_editor
