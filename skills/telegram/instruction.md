
# Telegram: Chat Read / Send Skill

Send and read Telegram messages via Telethon. Reads saved settings from the
Sable system automatically. Uses a separate session to avoid conflicting with
the running server instance.

---

## Trigger Guard

| Condition | Action |
|---|---|
| User says "check telegram", "read my messages", "any new TG messages" | Fire this skill |
| User says "send a telegram message to", "message someone on TG" | Fire this skill |
| User asks to search contacts or find a chat | Fire this skill |
| User wants to change Telegram settings | NOT this skill — use Sable settings panel |
| User wants to download media/files from Telegram | NOT this skill — use Video Downloader or Browser |

---

## Script Path

    PROJECT_ROOT/skills/telegram/scripts/telegram_client.py

All commands use `uv run` for automatic telethon dependency resolution.

---

## Commands

### Check Status

    uv run PROJECT_ROOT/skills/telegram/scripts/telegram_client.py status

### List Chats

    uv run PROJECT_ROOT/skills/telegram/scripts/telegram_client.py chats [--limit N]

### Get Messages

    uv run PROJECT_ROOT/skills/telegram/scripts/telegram_client.py messages CHAT_ID [--limit N] [--offset-id ID]

### Send Message

    uv run PROJECT_ROOT/skills/telegram/scripts/telegram_client.py send CHAT_ID --text "message"

### Search Contacts

    uv run PROJECT_ROOT/skills/telegram/scripts/telegram_client.py search QUERY [--limit N]

---

## Parameters

### chats

| Flag | Default | Notes |
|---|---|---|
| --limit | 30 | Max chats to return |

### messages

| Arg/Flag | Required | Default | Notes |
|---|---|---|---|
| CHAT_ID | Yes | - | Numeric chat/entity ID |
| --limit | No | 30 | Max messages to return |
| --offset-id | No | 0 | Fetch messages older than this ID |

### send

| Arg/Flag | Required | Notes |
|---|---|---|
| CHAT_ID | Yes | Numeric chat/entity ID |
| --text | Yes | Message text to send |

### search

| Arg/Flag | Required | Default | Notes |
|---|---|---|---|
| QUERY | Yes | - | Name substring to match |
| --limit | No | 10 | Max results |

---

## Execution Protocol

### Step 1 — Identify intent
- Determine: status, list chats, read messages, send, or search.
- If user says "check telegram" without specifics → run `chats` to show recent activity.
- If user wants to message someone by name → `search` first to get the chat_id.

### Step 2 — Resolve target
- For `messages` and `send`, a numeric `chat_id` is required.
- If user gives a name instead of ID → run `search` to find it.
- If multiple matches, present options and let user choose.

### Step 3 — Execute
- Always use `uv run` — telethon is resolved via PEP 723.
- For `send`, confirm recipient and message with the user before executing unless explicit.
- Timeout: set `timeout="30"` on execute_command since connections can be slow.

### Step 4 — Report
- **status**: Show authorized/not + username.
- **chats**: Show name, unread count, last message preview.
- **messages**: Show sender, text, date. Note media types if present.
- **send**: Confirm success or show error.
- **search**: Show matching chats with IDs.

---

## Failure Handling

| Failure | Symptom | Action |
|---|---|---|
| Not configured | Error about missing settings | Tell user to configure in Sable settings |
| Not authorized | Authorization error | User needs to sign in via Sable settings first |
| telethon missing | Import error | Run `uv pip install telethon` |
| Chat not found | ValueError from Telethon | Verify chat_id with `search` or `chats` |
| Connection timeout | Hangs or timeout error | Retry once; check network |

---

## Examples

### Check recent chats

User: "What's new on Telegram?"

    uv run PROJECT_ROOT/skills/telegram/scripts/telegram_client.py chats --limit 15

### Find someone and message them

User: "Tell Maria I'll be late"

1. First: `search "Maria"` to get chat_id
2. Then: `send <chat_id> --text "I'll be late!"`

### Read messages from a group

User: "What did I miss in the dev group?"

1. First: `search "dev"` to find the group's chat_id
2. Then: `messages <chat_id> --limit 20`

### Check connection status

User: "Is Telegram working?"

    uv run PROJECT_ROOT/skills/telegram/scripts/telegram_client.py status

---

## Global Rules

1. Always use `uv run` — telethon is resolved via PEP 723 inline metadata.
2. Never expose sensitive data in output.
3. For `send`, always confirm with the user before executing unless the request was unambiguous.
4. Use `timeout="30"` on execute_command — connections take time.
5. When listing chats/messages, format as readable summary — don't dump raw JSON.
6. This skill uses a separate session from the server — no conflicts.
7. Media download is NOT supported by this skill. Only text messages and metadata.
