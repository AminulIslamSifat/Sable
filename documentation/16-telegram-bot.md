# `telegram_bot/bot.py` — Full Read (2,654 lines)

> [!INFO] Scope
> Read in full across six slices. This is a **second, independent chat surface** — it talks to Sable only over HTTP, never imports `engine/` beyond config paths.

***

## The architectural rule

> *"Talks to Sable server ONLY via existing HTTP endpoints."*

| Endpoint | Used for |
|:--|:--|
| `POST /api/chat` | Send message, receive SSE stream |
| `POST /api/chat/new` | Create chat session |
| `POST /api/chat/stop` | Stop button |
| `POST /api/skills/approve/:id` | Approve permission |
| `POST /api/skills/deny/:id` | Deny permission |
| `GET /api/chats` | History panel |
| `GET /api/models` | Model panel, image-capability check |
| `GET /api/personas` | Persona panel |
| `GET /api/library/{gallery,notes,research}` | Media panels |
| `POST /api/stt/transcribe` | Voice messages |

> [!IMPORTANT] No engine imports at runtime
> The only `engine` import is `get_server_url()`, which does `from engine.config import PORT, HOST` inside a try/except with an env-var fallback. Everything else is HTTP. The bot could run in a separate process on a separate machine.

***

## Constants

```python
_MAX_TG_MESSAGE_LEN  = 4096     # Telegram hard limit
_STREAM_TIMEOUT      = 300      # 5 min max wait for agent response
_TG_TOOL_OUTPUT_MAX  = 800      # chars per tool output in TG summary
_STOP_CALLBACK_PREFIX = "stop_stream:"
```

### Callback prefixes — all short on purpose

```python
_CB_PANEL_MODEL   = "p_mod"    _CB_SET_MODEL    = "s_mod:"
_CB_PANEL_PERSONA = "p_per"    _CB_SET_PERSONA  = "s_per:"
_CB_PANEL_THINKING= "p_thk"    _CB_SET_THINKING = "s_thk:"
_CB_PANEL_RESEARCH= "p_res"    _CB_SWITCH_CHAT  = "s_cht:"
_CB_PANEL_NOTES   = "p_not"    _CB_PAGE         = "pg:"
_CB_PANEL_GALLERY = "p_gal"    _CB_FILE_GALLERY = "f_gal:"
_CB_PANEL_HISTORY = "p_his"    _CB_FILE_NOTE    = "f_not:"
_CB_PANEL_MEDIA   = "p_med"    _CB_FILE_RESEARCH= "f_res:"
```

> [!IMPORTANT] The comment states the constraint
> *"Panel callback prefixes (must be short — TG has 64-byte limit)"*. Telegram rejects `callback_data` over 64 bytes. That's why every id is truncated at the call site: `f"{_CB_SET_MODEL}{mid[:50]}"` = 6 + 50 = 56 bytes, leaving headroom.

## State files

| Path | Content |
|:--|:--|
| `system/.telegram_bot_config.json` | `{bot_token, allowed_users, ...}` |
| `system/.telegram_bot_sessions.json` | `{tg_user_id: chat_id}` |
| `system/.telegram_bot_prefs.json` | `{tg_user_id: {model, persona, thinking_mode}}` |
| `system/.auth_token` | Sable Bearer token |

### Authorization

```python
def get_allowed_users() -> list[int]:
    raw = cfg.get("allowed_users", [])
    if isinstance(raw, str):
        return [int(x.strip()) for x in raw.split(",") if x.strip().isdigit()]
    return [int(x) for x in raw if str(x).isdigit()]

def is_user_allowed(user_id: int) -> bool:
    allowed = get_allowed_users()
    return not allowed or user_id in allowed
```

> [!WARNING] Empty allowlist means **allow everyone**
> `not allowed` → `True`. A bot with no `allowed_users` configured accepts any Telegram user who finds it. That is fine for a bot nobody knows the handle of, but it is a fail-open default. It also accepts a comma-separated **string** as well as a list, which suggests it was edited by hand at some point.

Every handler starts with the same three lines:

```python
if not user or not is_user_allowed(user.id):
    logger.warning("... REJECTED — user %s not allowed", ...)
    return
```

***

## `StreamDisplay` — the message-chaining class

Telegram caps a message at 4,096 chars. A long agent response cannot fit in one message, and `edit_text` on a 4,096-char message near the limit is fragile.

```python
class StreamDisplay:
    MIN_EDIT_INTERVAL = 0.8      # seconds between edits
    CHAIN_THRESHOLD   = 3500     # spawn new message above this
```

| Method | Does |
|:--|:--|
| `start()` | Create the initial message with the stop button |
| `append_thinking(text)` | Accumulate reasoning, maybe edit |
| `append_answer(text)` | Accumulate answer, maybe edit |
| `_maybe_edit()` | Throttled edit; chains if over threshold |
| `_chain_new_message()` | Finalize current, spawn a fresh one |
| `finalize()` | Flush pending, drop the stop button |

### The chaining mechanism

```python
if len(display) > self.CHAIN_THRESHOLD:
    await self._chain_new_message()
    return
```

`_chain_new_message()` edits the current message **without** a reply_markup (locking it as a finished record), resets `_thinking`/`_answer` to empty, and sends `"⏳ …continuing…"` with the stop button attached. `self._chain` accumulates every message.

> [!TIP] Why 3500, not 4096
> 3,500 leaves ~600 chars of headroom for the Markdown rendering and the `💭 _thinking_` prefix added by `_build_display()`. Editing at 4,090 would sometimes fail on the final token burst.

> [!NOTE] Throttling is 0.8 s, not per-token
> `MIN_EDIT_INTERVAL = 0.8` — Telegram rate-limits `edit_text` and a per-token edit would be throttled into uselessness anyway. 0.8 s gives ~1.25 visible updates/sec, which reads as smooth streaming.

### `_build_display()` caps thinking at 2,000 chars

```python
if len(t) > 2000:
    t = t[:2000] + "…"
```

Reasoning is shown in italic and truncated — the answer is what matters, the thinking is context.

***

## `stream_chat_events()` — the SSE consumer

```python
async def stream_chat_events(chat_id, message, *, model=None, thinking_mode=None,
                             on_status=None, on_tool_event=None,
                             on_thinking=None, on_answer=None) -> StreamResult
```

### Non-SSE response handling is explicit

```python
content_type = resp.headers.get("content-type", "")
if "text/event-stream" not in content_type:
    raw = await resp.text()
    try:
        body = json.loads(raw)
        if isinstance(body, dict) and body.get("error"):
            result.error = body["error"]
            return result
    except (json.JSONDecodeError, TypeError):
        pass
    if raw.strip():
        result.error = f"Unexpected response format: {raw[:300]}"
    else:
        result.error = "Empty response from server (possible model/provider lock). Try switching models or starting a new chat."
    return result
```

> [!IMPORTANT] The empty-body branch has a real diagnosis baked in
> *"possible model/provider lock"* — that message came from someone hitting exactly this and working out why. An empty 200 with no content-type is not a network failure; it's the server refusing to stream.

### Event handling table

| Event | Action |
|:--|:--|
| `answer` / `token` | `answer_parts.append`, `on_answer` |
| `thinking` | `thinking_parts.append`, `on_thinking` |
| `skill_start` | Reset output accumulator, `on_tool_event("start")` |
| `skill_output` (ask_user) | Parse JSON into `result.ask_user_payload` |
| `skill_output` (other) | Accumulate into `current_tool_output_parts` |
| `skill_end` | Build tool activity record, `on_tool_event("end")`; `result.paused` if `result.pause` |
| `permission_request` | `result.permission_request`, `result.paused = True` |
| `approval_pending` | Status callback |
| `status` | `msg.replace("_", " ").title()` if lowercase/underscore, `on_status` |
| `done` | `break` |
| `error` | `result.error`, return |
| `meta`, `chat_title`, `rate_limited`, `waf_blocked`, `account_switched`, … | ignored |

> [!NOTE] The status prettifier is a heuristic
> ```python
> display_msg = msg.replace("_", " ").title() if msg.islower() or "_" in msg else msg
> ```
> Internal codes like `qwen_transport_ghost` become `Qwen Transport Ghost`. A human-readable status already (mixed case, no underscore) passes through untouched. Crude but it works for the codes Sable emits.

### `StreamResult`

```python
__slots__ = ("answer", "thinking", "ask_user_payload", "permission_request",
             "error", "skills", "paused", "tool_activities")
```

`paused` is the signal that the agent loop stopped waiting for user input — `ask_user` or a permission request. The caller must not finalize the stream in that case; it must show buttons.

***

## `chunk_message()` — fence-aware splitting

```python
def chunk_message(text: str, max_len: int = 4096) -> list[str]
```

Three-step split: newline near the limit → space → hard cut. Then:

```python
open_fences = chunk.count("```")
if open_fences % 2 == 1:
    last_fence = chunk.rfind("```")
    lang_line = chunk[last_fence:]
    lang = lang_line.split("\n")[0].replace("```", "").strip()
    chunk += "\n```"
    remaining = f"```{lang}\n{remaining}" if lang else f"```\n{remaining}"
```

> [!TIP] Odd fence count means the split landed inside a code block
> It closes the fence on the current chunk and **reopens it with the same language tag** on the next. Without the language carry-over, chunk 2 would render as unstyled text instead of code. This is the correct way to split Markdown.

***

## The four callbacks

| Callback | Signature |
|:--|:--|
| `on_status` | `async (text: str)` |
| `on_tool_event` | `async (event_type: "start"|"end", data: dict)` |
| `on_thinking` | `async (text: str)` |
| `on_answer` | `async (text: str)` |

> [!WARNING] `on_status` is passed in but does nothing
> Every call site defines:
> ```python
> async def update_status(new_text: str) -> None:
>     pass  # Thinking/answer now stream via on_thinking/on_answer
> ```
> The parameter is still threaded through `stream_chat_events` and still fires `await on_status(...)` for `status` / `approval_pending` / `permission_request` events — all into a no-op. Dead plumbing left from when status used a separate ephemeral message.

***

## Real problem: `send_tool_event` is duplicated four times

This is the single biggest code-quality issue in the file. The **same ~45-line closure** appears in:

1. `handle_message` (line ~1053)
2. `handle_callback_query` → `ask_user:` branch (line ~1660)
3. `handle_callback_query` → `perm_` branch (line ~1870)
4. `handle_voice` (line ~2450)

Each copy:

```python
async def send_tool_event(event_type: str, data: dict) -> None:
    nonlocal status_msg, current_tool_args, stream_display
    name = data.get("name", "unknown")
    if event_type == "start":
        await stream_display.finalize()
        attrs = data.get("data", {}).get("attrs") or data.get("attrs", "")
        if isinstance(attrs, dict):
            args_str = ", ".join(f"{k}={v}" for k, v in attrs.items() if v)
        else:
            args_str = str(attrs).strip() if attrs else ""
        current_tool_args = args_str
        display = f"⚡ `{name}`"
        if args_str:
            display += f"\n`{args_str[:500]}`"
        try:
            status_msg = await update.message.reply_text(display, parse_mode="Markdown", reply_markup=stop_kb)
        except Exception:
            pass
    elif event_type == "end":
        # … 30 more lines …
        stream_display = StreamDisplay(update.message, stop_kb, thinking_enabled=bool(user_thinking))
        await stream_display.start()
        status_msg = stream_display.msg
        current_tool_args = ""
```

The only differences between copies are the variable names (`status_msg` vs `chat_status_msg`) and the parent object (`update.message` vs `query.message`).

> [!CRITICAL] Four copies means four places to fix every bug
> The `reply_markup=stop_kb` on the tool card is itself questionable — a finished tool record keeps a live stop button. If you fix that in one copy, three others keep the bug. This should be one factory:
> ```python
> def make_tool_event_handler(message, stream_ref, stop_kb, thinking):
>     ...
>     return send_tool_event
> ```
> Same for the ~25-line stream-result tail (`if result.error … if result.permission_request … finalize`), which is also copy-pasted four times.

### `_send_stream_result()` exists but is only half-used

```python
async def _send_stream_result(message, result, context, chat_id) -> None
```

It correctly handles `error`, `permission_request`, `ask_user_payload`, and normal chunked output. But `handle_message` **inlines all of that logic** instead of calling it, and only the `ask_user:` / `perm_` / `handle_voice` paths call the helper. Two implementations of the same behaviour, drifting.

***

## Panels — the persistent keyboard

```python
def _build_persistent_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([
        [Model, Persona, Thinking],
        [Research, Notes, Gallery],
        [History, Media],
    ], resize_keyboard=True, is_persistent=True)
```

> [!NOTE] `is_persistent=True` is a Telegram Bot API 7.0+ feature
> The keyboard stays visible after a message is sent, rather than collapsing. Without it the user has to tap the input field to get the buttons back — which is why it was added.

`handle_message` routes on **exact text match**:

```python
_keyboard_routes = {
    _KB_MODEL: _panel_model,
    _KB_PERSONA: _panel_persona,
    _KB_THINKING: _panel_thinking,
    _KB_RESEARCH: _panel_research,
    _KB_NOTES: _panel_notes,
    _KB_GALLERY: _panel_gallery,
    _KB_HISTORY: _panel_history,
    _KB_MEDIA: _panel_media,
}
handler = _keyboard_routes.get(text_stripped)
```

> [!WARNING] A user typing "🤖 Model" literally opens the panel
> The route is on the button's label text. Anyone who types the exact emoji+word gets the panel instead of a chat message. Low impact (it's a panel, not a crash), but it means those eight strings are reserved and can never be sent to the model.

### Panels

| Panel | Endpoint | Pagination |
|:--|:--|:--|
| Model | `GET /api/models` | — |
| Persona | `GET /api/personas` | — |
| Thinking | `GET /api/models` (thinking_modes) | — |
| Research | `GET /api/library/research` | 8/page |
| Notes | `GET /api/library/notes` | 8/page |
| Gallery | `GET /api/library/gallery` | 8/page |
| History | `GET /api/chats` | 10/page |
| Media | *(static text)* | — |

`_build_library_keyboard(items, cb_prefix, page, page_size=8, label_key, sub_key)` returns `(keyboard_rows, total_pages)`. Filenames are truncated to 50 chars in callback data.

> [!NOTE] History pagination is duplicated too
> The page-render block inside `_CB_PAGE` → `his:` is a near-verbatim copy of `_panel_history`'s body, differing only in the page number. Same problem as `send_tool_event`.

***

## Photo and voice handling

### `handle_photo`

```python
supported, model_label = await _check_model_image_support(user.id)
if not supported:
    await update.message.reply_text(
        f"⚠️ *{model_label}* doesn't support image input.\n"
        f"Switch to a vision-capable model to send photos.", parse_mode="Markdown")
    return
```

> [!IMPORTANT] The capability check is fail-open
> ```python
> # Default: assume supported if we can't verify
> return True, user_model or "default"
> ```
> If `/api/models` errors, the photo is accepted and then fails downstream. Better to reject with "can't verify" than to accept and fail later — but the current default at least doesn't block a working setup.

The photo is saved to `system/uploads/tg_{user_id}_{file_unique_id}.jpg`. With a caption it becomes `[Image: <path>]\n\n<caption>` and is routed through `handle_message` by **mutating `update.message.text`**.

> [!WARNING] Mutating `update.message.text` is fragile
> ```python
> update.message.text = text
> await handle_message(update, context)
> ```
> `handle_message` starts with `text = update.message.text` — so it works. But `Message.text` is a property on the python-telegram-bot object; assigning it depends on the library version and on whether the object is frozen. It is a duck-punch, not an API.

### `handle_voice`

Download → `POST /api/stt/transcribe` (multipart) → transcribed text → same streaming flow.

```python
filtered = stt_result.get("filtered", "")
if filtered == "hallucination":
    await status_msg.edit_text("🎤 Could not transcribe — audio was unclear or silent.")
elif filtered == "tts_echo":
    await status_msg.edit_text("🎤 Skipped — detected TTS echo feedback loop.")
```

> [!TIP] The `tts_echo` filter is a real feedback-loop guard
> The bot can speak (via Sable's TTS) and can hear. If a voice reply is played near the microphone, the STT would transcribe Sable's own TTS output and feed it back as user input. `filtered: "tts_echo"` is the server telling the bot "that was me, ignore it."

Extension mapping handles `audio/mpeg → .mp3`, `audio/mp4 → .m4a`, `audio/wav → .wav`, `audio/webm → .webm`, `audio/x-m4a → .m4a`, default `.ogg` (Telegram voice is OGG/Opus).

***

## Permission flow

`permission_request` → inline keyboard:

```
[✓ Approve] [✓ Allow Session]
[✗ Deny]
```

Callbacks `perm_approve:<tag_id>`, `perm_session:<tag_id>`, `perm_deny:<tag_id>`.

```python
async def resolve_permission(tag_id, action, chat_id=None) -> dict:
    if action == "deny":
        endpoint = f"/api/skills/deny/{tag_id}"
        body = {"chat_id": chat_id}
    else:
        endpoint = f"/api/skills/approve/{tag_id}"
        body = {"chat_id": chat_id, "session": action == "session"}
```

After a successful approve the bot sends `"continue"` as a fresh user message to resume the agent loop:

```python
result = await stream_chat_events(chat_id, "continue", ...)
```

> [!IMPORTANT] The source comments admit this was uncertain
> ```python
> # Auto-continue: send empty message to trigger next agent turn
> # The Sable backend auto-continues after approval, so we need to poll
> # Actually — the approve endpoint returns feedback but doesn't auto-continue.
> # We need to send a follow-up message to continue the conversation.
> ```
> Three comments in a row changing their mind. The final answer — send literal `"continue"` — means the word "continue" appears in the conversation as a user turn. The model sees it as an instruction, which mostly does the right thing but is not a real resume protocol.

***

## Two entry points

| Function | Mode |
|:--|:--|
| `main()` | `app.run_polling(...)` — standalone script |
| `start_bot_in_background()` | Async task inside Sable's event loop |

`start_bot_in_background()` waits for `/api/health` for up to 30 s before polling:

```python
for attempt in range(30):
    async with sess.get(f"{server_url}/api/health", ...) as resp:
        if resp.status < 500:
            break
    await asyncio.sleep(1)
else:
    logger.warning("Auto-start: server not ready after 30s, starting anyway")
```

> [!NOTE] It starts anyway after 30 s
> The `for…else` means the loop exhausted without breaking. The warning is honest — polling starts, and the first few messages may fail until the server is up. That's better than never starting the bot.

Shutdown cancels via `asyncio.CancelledError` → `updater.stop()` → `stop()` → `shutdown()`, in that order. Correct sequence: the updater must stop before the application, or in-flight updates are lost mid-handler.

***

## Registered handlers

```python
app.add_handler(CommandHandler("start", cmd_start))
app.add_handler(CommandHandler("new", cmd_new))
app.add_handler(CommandHandler("reset", cmd_reset))
app.add_handler(CommandHandler("status", cmd_status))
app.add_handler(CommandHandler("help", cmd_help))
app.add_handler(CallbackQueryHandler(handle_callback_query))
app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
```

`filters.TEXT & ~filters.COMMAND` — commands go to `CommandHandler`s, plain text to `handle_message`. Without the exclusion, `/new` would also reach `handle_message` and be sent to the model.

***

## Gotchas

> [!WARNING] Six things to know
> 1. **Empty `allowed_users` = allow everyone.** Fail-open by default.
> 2. **`send_tool_event` is duplicated 4×** (~45 lines each). Fixing a bug means fixing it four times.
> 3. **History pagination duplicates `_panel_history`.** Same class of problem.
> 4. **`on_status` is dead plumbing** — every call site passes a `pass`-only function.
> 5. **Tool cards keep the stop button** (`reply_markup=stop_kb` on a finished record). A completed tool still shows "⏹ Stop".
> 6. **Permission resume sends literal `"continue"`** — not a real resume protocol, just a user turn that says the word.

> [!NOTE] Polling, not webhooks
> `run_polling` and `updater.start_polling`. No public URL required, which is right for a self-hosted bot behind no tunnel — at the cost of latency and constant long-poll connections.
