# Background Command Execution & Process Monitoring

## Launching
Use execute_background_command tag (or execute_command with bg="true"):
- Returns PID, Log File (/tmp/ghost_bg_PID.log), status RUNNING.
- Use for: long builds, test runners, dev servers, downloads.

## Checking
- Specific job: check_command tag with pid attribute.
- All jobs: check_command tag with no attributes.

## Rules
- Prefer background for anything exceeding 15s timeout.
- Report PID back for later checking.
