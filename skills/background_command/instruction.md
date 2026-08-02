# Background Command Execution & Process Monitoring

## execute_command (inline)
Runs a shell command and streams output back. Default timeout: 15s.
- **Custom timeout**: set `timeout` attribute (seconds, max 180). Use when a command legitimately needs more time (compilation, large test suites, package installs).
- Example: `<execute_command timeout="60">uv run pytest test/ -x</execute_command>`
- If a command will exceed 180s or run indefinitely, use background instead.

## Launching Background
Use execute_background_command tag (or execute_command with bg="true"):
- Returns PID, Log File (/tmp/ghost_bg_PID.log), status RUNNING.
- Use for: long builds, test runners, dev servers, downloads, anything > 180s.

## Checking
- Specific job: check_command tag with pid attribute.
- All jobs: check_command tag with no attributes.

## Rules
- Default 15s is fine for quick lookups. Override with timeout attr when needed.
- Prefer background for anything exceeding 180s or with no predictable end.
- Report PID back for later checking.
