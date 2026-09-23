# Kimi connector

Ghost-tab transport for kimi.ai — in-page gRPC-Web/Connect fetch with full
browser identity. No shield reverse-engineering, no JWT refresh loop.

## Files

| File | Role |
|---|---|
| `ghost.py` | Headless Helium session; in-page framed fetch; `__ghostEvent` bridge; upload + cancel helpers |
| `client.py` | `KimiClient` — Sable stream interface, frame→event mapping, error decode |
| `upload.py` | File upload + parse-poll |

## Endpoints

| Purpose | Path |
|---|---|
| Chat (stream) | `/apiv2/kimi.gateway.chat.v1.ChatService/Chat` |
| Cancel | `/apiv2/kimi.gateway.chat.v1.ChatService/CancelChat` |
| Upload | `/apiv2-files/file/upload` |
| Parse poll | `/apiv2-files/kimi.gateway.file.v1.FileService/GetFileParseProgress` |

## Wire format

Every message is `[flag:1][length:4 BE][JSON]`. Request = one `flag 0` frame.
Response = many `flag 0` frames, `flag 2` = end. Sending unframed JSON → `invalid_argument`.

## Request event mapping

| mask | op | → event |
|---|---|---|
| `block.think` / `block.think.content` | set/append | `thinking` |
| `block.text` / `block.text.content` | set/append | `answer` |
| `message` (assistant, GENERATING) | set | track current assistant id |
| `message.status` (COMPLETED) | set | `done` |
| `done` | — | `done` |

## The bugs we hit (and fixed)

1. **System message is `COMPLETED` first** — naive done-check ended the stream on frame 3.
2. **Continuation replays the old assistant as `COMPLETED`** — must track the *current* assistant id from `GENERATING`.
3. **`<REF>…</REF>` chrome** leaks into answer text — stripped.
4. **`parent_id` breaks continuations** — `chat_id` alone works; sending a reconstructed `parent_id` returns an empty stream. We deliberately omit it.
5. **Stale-bridge race** — a prior stream's draining `done` poisoned the next request's queue, making the first continuation return empty. Fixed with per-request `rid` tagging; `_on_event` drops events whose rid ≠ the active one.

## ⚠️ Known limitation

`GetFileParseProgress` polling returns `PROCESS_STATUS_PENDING` in our probe —
the response shape we read doesn't match what the server sends, so we never
observe `SUCCESS` and fall back to the optimistic default. Files still work
(Kimi finishes parsing before the chat turn lands), but the poll is not yet
reliable and should be fixed by decoding the actual progress frame.

## Config

- `system/settings.json` → `provider_accounts.kimi` (default `browser-data-kimi`)
- `system/settings.json` → `kimi_use_ghost` (default true)
- Dedicated profile per connector — **do not** share a profile with the DeepSeek ghost; they'll kill-war over `SingletonLock`.

## Model

`kimi-k2.6` → `api_backend: kimi`, `api_model_type: k2d6-chat`. Thinking modes map
to `reasoning_effort`: fast→`NONE`, low/medium/high→matching enum.
