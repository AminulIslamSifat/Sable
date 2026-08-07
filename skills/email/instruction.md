
# Email: IMAP Read / SMTP Send Skill

Send and read emails via IMAP/SMTP using stdlib only. Reads saved credentials
from the Sable system configuration automatically. Zero extra dependencies —
runs with `uv run` for isolation.

---

## Trigger Guard

| Condition | Action |
|---|---|
| User says "check my email", "read inbox", "what emails do I have" | Fire this skill |
| User says "send an email to", "email someone" | Fire this skill |
| User asks about email folders, search emails | Fire this skill |
| User wants to configure credentials | NOT this skill — use Sable settings panel |
| User needs to scrape a website for data | NOT this skill — use Browser Control |

---

## Script Path

    PROJECT_ROOT/skills/email/scripts/email_client.py

All commands use `uv run` for zero-install dependency resolution (stdlib only).

---

## Commands

### Check Status

    uv run PROJECT_ROOT/skills/email/scripts/email_client.py status

### List Folders

    uv run PROJECT_ROOT/skills/email/scripts/email_client.py folders

### List Messages

    uv run PROJECT_ROOT/skills/email/scripts/email_client.py list [--folder FOLDER] [--limit N] [--offset N] [--search QUERY]

### Read Full Message

    uv run PROJECT_ROOT/skills/email/scripts/email_client.py read UID [--folder FOLDER]

### Send Email

    uv run PROJECT_ROOT/skills/email/scripts/email_client.py send --to ADDR --subject SUBJ --body TEXT [--cc ADDR] [--html]

---

## Parameters

### list

| Flag | Default | Notes |
|---|---|---|
| --folder | INBOX | Mailbox folder name |
| --limit | 20 | Max messages to return |
| --offset | 0 | Skip N newest messages |
| --search | None | Search in subject and sender |

### read

| Arg/Flag | Required | Notes |
|---|---|---|
| UID | Yes | Message UID from list output |
| --folder | No | Defaults to INBOX |

### send

| Flag | Required | Notes |
|---|---|---|
| --to | Yes | Recipient address |
| --subject | Yes | Subject line |
| --body | Yes | Body text |
| --cc | No | CC recipients (comma-separated) |
| --html | No | Send body as HTML instead of plain text |

---

## Execution Protocol

### Step 1 — Identify intent
- Determine: status check, list, read, or send.
- If user says "check email" without specifics → run `list` with default params.
- If user mentions a specific sender or subject → use `--search`.

### Step 2 — Execute
- Always use `uv run` — never bare `python3`.
- For `read`, always get the UID from a prior `list` call first.
- For `send`, confirm recipient and subject with the user before executing unless they were explicit.

### Step 3 — Report
- **status**: Show connected/disconnected + account info.
- **folders**: List folder names cleanly.
- **list**: Show from, subject, date in a readable format. Don't dump raw JSON unless debugging.
- **read**: Show from, subject, date, body. Mention attachments if present.
- **send**: Confirm success or show error clearly.

---

## Failure Handling

| Failure | Symptom | Action |
|---|---|---|
| Not configured | Error about missing config | Tell user to configure in Sable settings |
| Login failed | Connection error in status | Credentials may be wrong or expired |
| Folder not found | Cannot open folder error | Run `folders` to show valid names |
| Message not found | Message not found error | UID may be stale — re-run `list` |
| Send failed | ok: false with error | Check network or rate limits |

---

## Examples

### Check inbox

User: "Any new emails?"

    uv run PROJECT_ROOT/skills/email/scripts/email_client.py list --limit 10

### Search for specific sender

User: "Do I have any emails from GitHub?"

    uv run PROJECT_ROOT/skills/email/scripts/email_client.py list --search "GitHub" --limit 10

### Read a specific email

User: "Read that email from Alice"

1. First: `list --search "Alice"` to find the UID
2. Then: `read <uid>`

### Send an email

User: "Send an email to john@example.com saying the meeting is at 3pm"

    uv run PROJECT_ROOT/skills/email/scripts/email_client.py send --to john@example.com --subject "Meeting Time" --body "Hi John, the meeting is at 3pm today."

---

## Global Rules

1. Always use `uv run` — the script uses PEP 723 inline metadata.
2. Never expose sensitive data in output. The script reads config internally.
3. For `send`, always confirm with the user before executing unless the request was unambiguous.
4. Default to INBOX unless the user specifies another folder.
5. When listing messages, show a summary table — don't dump raw JSON.
6. If the user asks to read an email, always `list` first to get fresh UIDs.
