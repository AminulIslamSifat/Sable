# Background Command Execution & Process Monitoring

## Tag Reference

| Tag | Attributes | Description |
|:--|:--|:--|
| `execute_command` | `timeout`, `bg` | Run shell command, stream output back. Default timeout: 15s (max 180). Set `bg="true"` to launch in background instead — returns PID + log file (`/tmp/ghost_bg_PID.log`). Use background for anything > 180s or indefinite. |
| `check_command` | `pid` | Check background job status. Omit `pid` to check all jobs. |

## Rules

- Default 15s is fine for quick lookups; override `timeout` when needed
- Prefer `bg="true"` for anything exceeding 180s or with no predictable end
- Report the PID back for later checking
