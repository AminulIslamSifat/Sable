# Background Command Execution & Process Monitoring

## execute_command (inline)
Runs a shell command and streams output back. Default timeout: 15s.
- Custom timeout: set the timeout attribute (seconds, max 180) — use when a command legitimately needs more time (compilation, large test suites, package installs).
- Example: <action><execute_command timeout="60">uv run pytest test/ -x</execute_command></action>
- If a command will exceed 180s or run indefinitely, use background instead.

## Launching Background
Use execute_background_command (or execute_command with bg="true"):
- Returns PID, log file (/tmp/ghost_bg_PID.log), status RUNNING.
- Use for: long builds, test runners, dev servers, downloads — anything > 180s.

## Checking
- Specific job: check_command pid=<pid>
- All jobs: check_command with no attributes

## Rules
- Default 15s is fine for quick lookups; override with timeout when needed.
- Prefer background for anything exceeding 180s or with no predictable end.
- Report the PID back for later checking.