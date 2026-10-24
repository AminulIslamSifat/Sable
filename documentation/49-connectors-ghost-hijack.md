# 49 — Hijack-Class Ghost Transports (ChatGPT, Kimi, z.ai)

Three providers that cannot be driven by replaying a captured request: ChatGPT gates each send behind a single-use sentinel bundle, Kimi behind a per-request `x-msh-shield-data` token, z.ai behind an HMAC signature **plus** an Aliyun captcha token. Each therefore needs a live browser page — but they solve it three different ways.

| Provider | Lines | Strategy |
|:--|--:|:--|
| `chatgpt/client.py` | 440 | **Hijack.** Let the SPA build and send, intercept, swap the prompt, tee the response |
| `kimi/client.py` | 581 | **In-page fetch.** Own the request; the SPA mints the shield token for its own page |
| `zai/client.py` | 960 | **In-page script.** Whole flow in one `page.evaluate`: create chat → mint captcha → HMAC-sign → POST → stream |

All three expose the same `stream_chat` async generator and the same event vocabulary (`answer` / `thinking` / `done` / `error`), so `engine/chat.py` cannot tell them apart.

***

## 1. ChatGPT — the hijack

### 1.1 What is being hijacked

`POST /backend-api/f/conversation` needs a sentinel bundle (proof-of-work + Turnstile + chat-requirements) and an `x-conduit-token` JWT, all single-use and bound to the SPA's own send. So the ghost lets the SPA build the request, then rewrites the prompt and tees the SSE.

### 1.2 `has_token()` must never be called here

The comment block above `open_stream` is the most valuable thing in the file:

```
DO NOT call session.has_token() here.
has_token() fires an in-page fetch('/api/auth/session') right before
open_stream(). The SPA then runs /f/conversation/prepare but never issues
the actual POST — the send is silently swallowed.
Reproduced deterministically: with has_token -> 0 lines, without -> 54 lines in 5s.
```

Kimi **does** call `has_token()` before every stream. That is not a contradiction — different SPA, different side effect. It is why copying a sibling connector wholesale is dangerous, and the comment is the only thing standing between the next reader and a day of debugging.

### 1.3 Instruction re-injection on an existing thread

An API provider can attach a fresh system message per turn. ChatGPT cannot — the ghost drives **one** composer submit. So `_chat_instr_ver[chat_id]` tracks the instruction version last injected, and when `get_instruction_version()` changes (persona edit, output-format edit, project switch) the next turn re-injects with an explicit marker:

```
[SYSTEM INSTRUCTION UPDATED — supersedes all earlier instructions in this conversation]
```

Without this, a persona change never reached a live ChatGPT chat — the old persona stayed baked into the upstream thread permanently.

### 1.4 Three separate truncation bugs, all commented

1. **Pathless deltas.** `{"v": "6\n7\n..."}` with no `p`/`o` continues the current content part. Missing that branch dropped the *middle* of multi-chunk answers — first and last chunks kept, middle gone.
2. **`message_stream_complete`.** Modern ChatGPT streams emit this, not the legacy `/message/status == finished_successfully`. Only checking `finished` meant the drain sat out the full 45s idle timeout after the answer had already ended.
3. **No retry, ever.** A retry re-fires `requestSubmit()` and duplicates the user turn — three copies of the prompt in the SPA. One `open_stream` per call.

> [!IMPORTANT] `_IDLE_TIMEOUT_SECS = 45.0` is the *failure* path
> On a healthy turn the stream ends on `stream_complete`. The 45s only elapses when something is wrong. Bug #2 made every turn take 45s and look fine.

### 1.5 `_apply_patches` has no JSON-pointer parser

It string-prefix-matches `path.startswith("/message/content/parts/")` and appends the value. It never resolves the actual pointer, never checks whether the patch is for the *current* assistant message, and ignores `op` values other than `append`/`add`/`replace`. Adequate against the observed stream; a `replace` targeting a different part index would append instead of overwrite.

***

## 2. Kimi — Connect over gRPC-Web

The transport is gRPC-Web over the Connect protocol, and the response frames are `{mask, op, block}` patches rather than OpenAI `choices[]`:

```
mask="block.think"         op=set     -> thinking block created
mask="block.think.content" op=append  -> thinking delta
mask="block.text"          op=set     -> answer block (content = first delta)
mask="block.text.content"   op=append  -> answer delta
```

### 2.1 The continuation-replay trap

On a continuation, Kimi **replays the existing conversation** before the new turn — including the previous assistant message, already marked `COMPLETED`. Treating any completion as terminal ends turn 2 before it starts.

The fix tracks `state["assistant_id"]` when a message enters `MESSAGE_STATUS_GENERATING`, and only accepts a completion for **that** id. The fallback branch (`aid is None and role not in ("system","user")`) exists for the final `mask="message.status"` frame that carries no role.

### 2.2 `parent_id` is deliberately not sent

```
NOTE: we deliberately do NOT send message.parent_id. Empirically,
chat_id alone continues the conversation correctly (context preserved),
while chat_id + a reconstructed parent_id makes Kimi return an empty stream.
```

`self._parent_ids` is still tracked — but for `cancel()`, not for the request body.

### 2.3 Empty-stream retry, once

Kimi returns a COMPLETED-but-empty stream on continuation roughly **1 in 3** times. `stream_chat` retries the whole open exactly once when `not got_answer and attempt == 0`, with a fresh parse state per attempt (`state["assistant_id"]` from a prior attempt would break terminal matching).

### 2.4 `_decode_error` is a protobuf scrap

The useful error text is base64 inside `details[].value`. Decoded, it is protobuf-ish; the code takes the longest printable run ≥12 chars, then strips a single leading length byte by checking `ord(text[0]) == len(text) - 1`. Clever, fragile, and honest about it in the comment.

### 2.5 `<REF>` stripping

`_REF_TAG_RE` removes Kimi's own UI chrome (`<REF>status✦added memory</REF>`) from the answer stream. If the tag arrived split across two frames, `_clean_answer` — which runs per-delta — would miss it.

***

## 3. z.ai — everything in one evaluate

### 3.1 The `_ZAI_JS` blob

~250 lines of JavaScript inside a raw string. Four phases: create chat via `/api/v1/chats/new`, load the Aliyun captcha SDK if absent and mint a token, compute the two-level HMAC signature, then POST `/api/v2/chat/completions` and stream.

Two hardcoded secrets live in that string — the HMAC key `key-@@@@)))()((9))-xxxx&&&%%%%%` and `SceneId: 'didk33e0'`. Both are z.ai web-client constants, not Sable secrets, but they are also not configuration, and nothing records where they came from or when they were last confirmed.

### 3.2 Captcha SDK loading is defensive

`window.initAliyunCaptcha` is only defined after the SPA lazy-loads `AliyunCaptcha.js` on first interaction. The script finds an existing `<script>` tag or injects one, resolves on `load`/`onerror`, and hard-times-out at 8s. `AliyunCaptchaConfig` must be set **before** the script loads — the comment says so.

### 3.3 Per-turn binding rebind, with a real hazard

```
Playwright rejects duplicate expose_function for the same name,
and a silent failure would leave the page pushing chunks into the
PREVIOUS turn's dead queue -> queue.get() hangs forever.
```

`_rebind_chunk_handler` therefore exposes a **stable dispatcher** once per page and swaps `self._chunk_handler` per turn. `_bound_page` identity is tracked so a swapped page re-exposes.

> [!CAUTION] `_ensure_page` can return the provider page or a fresh standalone page
> When `self._page_provider` returns a *different* page object than `_bound_page`, `_binding_exposed` is reset. But the standalone fallback path also resets it. If the provider ever alternates between two valid pages, each switch re-exposes — fine — but the previous page's in-flight stream would still be writing to a `_chunk_handler` closure that is now stale. Nothing guards that.

### 3.4 History is flattened when there is no chain

z.ai's completions endpoint ignores an embedded message tree when `current_user_message_parent_id` is null. The comment explains that a fork therefore sees *nothing* prior. The fix serialises the whole conversation into one flat prompt, exactly as DeepSeek and Qwen do in the same situation — and pops the trailing user turn because `db_history` is loaded after `chat.py` already committed it.

### 3.5 `is_available` is wrong for a fresh login — **🔴**

```python
@property
def token(self) -> str | None:
    if self._token:
        return self._token
    self._token = get_token_for_account(self._account)   # reads .zai_tokens.json only
    return self._token

@property
def is_available(self) -> bool:
    return self.token is not None
```

`get_token_for_account` reads `system/.zai_tokens.json`. The browser's `localStorage` is only consulted in `_ensure_page` (as a *readiness poll*) and `_extract_token_from_page`, which is only reached via `refresh_token()` — and **nothing calls `refresh_token()` on the availability path.**

Meanwhile `_ZAI_JS` reads `localStorage.getItem('token')` directly and would work fine. So a user who has just logged in to chat.z.ai in a browser profile, with no token ever having been written to the JSON store, sees z.ai **absent from the model list** while the transport is fully capable. The reverse also holds: a token in the JSON store reports available while the browser profile may be logged out.

### 3.6 Attachments bypass the browser entirely

`upload_file` uses `httpx` (or `urllib`) with `Bearer` auth against `/api/v1/files/`. The comment states the verified fact: only the **completions** endpoint is signature-gated; uploads are plain multipart. Sensible — but it means an attachment upload can succeed on a token the browser has already rotated past.

***

## 4. Cross-provider comparison

| | ChatGPT | Kimi | z.ai |
|:--|:--|:--|:--|
| Instruction injection | fresh + on version change | fresh only | fresh only |
| `has_token()` before stream | **never** | every turn | n/a |
| Retry | none (duplicates the turn) | once on empty/timeout | none |
| Stop/cancel | `session.cancel_stream()` | `cancel_chat(chat, msg_id)` | `window.__zaiAbort()` |
| Terminal signal | `stream_complete` | `{"done":{}}` | `__done__` binding |
| History fallback | `hist_txt` folded into prompt | `_build_body(history=...)` | flat serialised prompt |
| Idle timeout | 45s | 30s | none (queue blocks) |

> [!NOTE] z.ai has no idle timeout
> `while True: kind, payload = await queue.get()` — unbounded. If the in-page fetch hangs without firing `__done__` or `__error__`, the generator blocks forever holding `_stream_lock`. A `wait_for` on the `queue.get()` would match the other two.

***

## 5. Findings

| # | Where | What |
|--:|:--|:--|
| 160 | `zai.is_available` | **🔴** Reads only `system/.zai_tokens.json`; never consults `localStorage`. A fresh browser login reports unavailable (provider hidden) and a stale JSON token reports available against a logged-out profile. `_ZAI_JS` reads `localStorage` and would have worked. |
| 161 | `zai.stream_chat` | No idle timeout on the chunk queue. A hung in-page fetch blocks the generator and holds `_stream_lock` forever; Kimi and ChatGPT both bound this. |
| 162 | `zai._ZAI_JS` | HMAC key + `SceneId` hardcoded in a JS string with no provenance note; no config path, no test that pins them. |
| 163 | `kimi._clean_answer` | `<REF>` stripping runs per delta, so a tag split across two frames survives into the answer. |
| 164 | `chatgpt._apply_patches` | Prefix-matches the JSON pointer; never resolves part index, never checks the patch targets the current message, treats any `add`/`replace` as append. |
| 165 | `chatgpt` `_conversation_ids` / `_chat_instr_ver` / `_sessions` | Three per-chat dicts on a module-level singleton with no eviction. Grows with every chat ever opened. Same shape as ledger #23 / #118 / #145. |
| 166 | `kimi.has_token()` vs `chatgpt` "DO NOT call has_token" | Two connectors, opposite answers to the same question, one comment explaining each. Correct — but nothing prevents a future refactor from "unifying" them. |

### 🟠 one more, quieter

`kimi.stream_chat` calls `await session.has_token()` before `open_stream`. If that in-page fetch has the same side effect ChatGPT documented, Kimi would exhibit the identical swallowed-send. Kimi's retry-once would mask it as "1 in 3 empty streams" — which is exactly the rate the comment attributes to continuation. **Not proven**; the retry makes it unobservable without an instrumented run.

***

## 6. What these three files establish

- **A ghost transport is defined by what it *cannot* replay.** ChatGPT: single-use sentinel. Kimi: per-request shield token. z.ai: captcha + HMAC. The mechanism follows the constraint, not the provider.
- **Comments that name a reproduced bug are the highest-value lines in the tree.** "with has_token → 0 lines, without → 54 lines in 5s" is worth more than the 200 lines around it.
- **Every ghost holds per-chat state on a singleton, and none of them evict.** Four instances now (#23, #118, #145, #165). This should be a convention entry, not a repeated finding.
