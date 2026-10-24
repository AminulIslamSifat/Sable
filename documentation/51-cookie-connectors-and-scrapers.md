# 51 — Cookie-Auth Connectors & Browser Scraper Engines

Two families that both avoid API keys but for opposite reasons. The cookie connectors speak the site's own JSON API using cookies lifted from a logged-in browser profile. The scraper engines never touch the API at all — they type into the SPA's composer and read the stream off the wire.

| File | Lines | Transport |
|:--|--:|:--|
| `mistral_web/client.py` | 622 | cookies → `POST /api/chat`, tRPC JSON-patch frames |
| `mimo/client.py` | 606 | cookies → `POST /bot/chat`, SSE |
| `deepseek_scraper/client.py` | 402 | headed browser, patch-protocol SSE tap |
| `qwen_scraper/client.py` | 281 | headed browser, OpenAI-shape SSE tap |
| `claude_scraper/client.py` | 269 | headed browser, Anthropic-shape SSE tap |

***

## 1. The cookie pattern

`mistral_web` and `mimo` are structurally identical:

1. `set_cookie_provider(fn)` — injected by `ChatService`, returns a `dict` of cookies read from the account's profile.
2. `_cookies(force=False)` caches the jar for **300 seconds**.
3. `_http_session()` is a shared `curl_cffi.AsyncSession(impersonate="chrome")` with `timeout=300`.
4. `validate_auth()` hits a lightweight authenticated endpoint and raises a human-readable reason.

> [!IMPORTANT] `is_available` only means "a provider is wired"
> Both classes return `self._cookie_provider is not None`. Neither checks that the cookies are *valid*. Availability is a wiring fact, not an auth fact — the auth failure surfaces at the first `validate_auth()` / stream.

### 1.1 MiMo's auth probe is unusually honest

`validate_auth` distinguishes three failures the old code collapsed into one:

| Condition | Message |
|:--|:--|
| missing cookie | `no xiaomichatbot_serviceToken cookie` |
| **replacement chars present** | `cookies are undecryptable (N replacement chars) — … Re-login will NOT help; fix engine/cookie_reader.py` |
| HTTP 401 | `rejected the session token (401) … log in again` |

The middle case is the valuable one. `\ufffd` in a decrypted cookie means `cookie_reader` shipped ciphertext, not a session. Telling the user to re-login would send them in a circle forever; the message says so explicitly.

### 1.2 MiMo strips a NUL separator the protocol sends

```python
content = content.replace("\x00", "")
if not content:
    continue
```

MiMo frames content with embedded NUL bytes. Nothing documents what they delimit; the code removes them because they break rendering.

### 1.3 MiMo's frames are not deltas — they overlap

This is the most interesting parsing detail in either file:

```
MiMo re-sends an OVERLAPPING TAIL of the answer rather than a pure delta
(e.g. content "INSTR_Q" then "STR_Q"). Appending blindly duplicates the tail,
so merge on the longest suffix/prefix overlap.
```

The implementation tries `n` from `min(len(acc), len(new))` down to 1, strips the matched prefix, and drops the frame entirely if nothing new remains. Correct, and O(n²) in the worst case per frame — irrelevant at chat sizes.

> [!NOTE] This is the only transport in the tree that needs it
> Every other provider sends true deltas. A future refactor that "unifies the delta handlers" would silently break MiMo.

### 1.4 MiMo's chat path is per-model, and it used to be wrong

```python
CHAT_ROUTES = {
    "mimo-v2.6-pro": "/react/chat",
    "mimo-v2.6-flash": "/react/chat",
    "mimo-v2.6-pro-ultraspeed-studio": "/bot/chat",   # <- not /react/chat
}
```

The comment records the bug: a hardcoded `/react/chat` 404'd for every `/fastchat` model, which is why UltraSpeed never worked while pro/flash did.

### 1.5 Mistral Web — tRPC JSON-patch

The wire is `'<tag>:<json>'` lines where tags 16/15/8 mean bootstrap/message/terminator. The interesting part is that the assistant message arrives as **JSON-Patch operations**:

```
replace /                        seed assistant message (id, turn, chatId)
replace /contentChunks           first chunk
add     /contentChunks/N         new chunk
append  /contentChunks/N/text    token delta
replace /contentChunks/N/_context  context tag update
```

`_frame_to_events` keeps a `chunk_kind: dict[int, str]` map so a chunk created as `answer` and later re-tagged `reasoning` routes its **subsequent** appends correctly. Appends that arrive before the tag are already emitted as answer — there is no retroactive correction.

### 1.6 The tRPC body parser has two shapes

```
def _parse_trpc_body(text):
    """?batch=1 POSTs return a JSON array of {"result":{"data":{"json": …}}}
    envelopes — NOT the '<tag>:<json>' SSE frames used by /api/chat."""
```

The docstring exists because the earlier version only handled the SSE shape, so `file.uploadFile` returned `[]` and every upload silently failed with *"returned no url: []"*. `_unwrap_trpc` flattens the batch envelope.

### 1.7 Two paths parse the same JSON patch differently

`_frame_to_events` handles `path == '/'` (the seed), `/contentChunks` (bulk), `/contentChunks/N` (single), `/contentChunks/N/_context`, and `/contentChunks/N/text` (append). It does **not** handle the generic `replace /contentChunks/N/text`, nor `_context` set via `add`. Anything outside those five shapes is dropped without a log line.

***

## 2. The scraper engines

All three subclass `BaseBrowserEngine` and only override `parse_sse_delta` (and, for DeepSeek, the composer toggles). The generic engine owns launch, login, send, stop, DOM fallback.

### 2.1 DeepSeek — patch protocol, and a routing bug already fixed

```python
def _route_delta(self, text):
    ftype = (self._sse_frag_type or "").upper()
    if "THINK" in ftype or "REASON" in ftype:
        return "", text
    return text, ""
```

The comment records why it is a substring match: DeepSeek shipped the reasoning fragment as `THINK`, then `thinking`, then `reasoning`. An exact `== "THINK"` check silently routed reasoning into the answer stream — the *"thinking rendered as the model response"* symptom.

### 2.2 The `o == "APPEND"` gate is the anti-duplication guard

```
MUST stay gated on o == "APPEND" and read only v[0].
DeepSeek re-sends the WHOLE fragments array on SET/RESET/metadata ops;
looping over every element (or accepting any op) re-emits all prior
fragments on each event, which is the Nx content duplication.
```

This is the best-documented invariant in the three engines, and the one most likely to be broken by a well-meaning simplification.

### 2.3 Search is force-disabled immediately before every send

```python
async def send_msg(self, message, raw=False):
    await self._ensure_search_off()
    return await super().send_msg(message, raw=raw)
```

Override on `send_msg`, not on the caller — so `_send_hello_warmup` (which calls `super().send_msg` internals separately) also gets it. Web search would otherwise be inherited from whatever state the last human left the profile in.

### 2.4 Soft refusals are text, not frames

```python
error_text_markers=("Messages too frequent", "Too many requests", "Try again later", "rate limit", "usage limit")
```

Verified live by rapid-firing until the limiter tripped: the refusal renders as a `<span>` sibling of the last `.ds-message`, with **no SSE frame and an empty assistant bubble**. Without markers the turn would look like a successful empty answer.

### 2.5 Claude — the `_sse_line_buf` naming bug

```
MUST be `_sse_line_buf` — the base engine's get_response() flushes a buffered
partial final line via `self._sse_line_buf`. This engine originally named it
`_line_buf`, so that flush raised AttributeError on EVERY turn, the connector
classified it as "unknown", tore the browser down, and retried 3×.
```

A missing-attribute error from a **base-class convention the subclass didn't know about**. There is no `__init_subclass__` check and no declared attribute — the base class reads an attribute the subclass is expected to define, and the failure mode is a full browser teardown.

> [!WARNING] Nothing prevents the next one
> `qwen_scraper` and `deepseek_scraper` both define `_sse_line_buf` correctly. The convention is enforced only by the memory of whoever wrote the last engine.

### 2.6 Claude's model is whatever the account is set to

> MODEL SELECTION: deliberately NONE. The engine only types into the composer and sends — it never touches the model picker.

Consistent with the scraper philosophy: drive the UI as a human would. It also means a Sable model entry named "claude-sonnet-5-5" can be answered by whatever the account default is.

### 2.7 Qwen — cumulative reasoning summary

Qwen sends `extra.summary_thought.content` as a **list that grows** on each event, not a delta. `_summary_sent` tracks what was already emitted and sends only the new tail:

```python
if full.startswith(prev):
    new = full[len(prev):]
else:
    new = full if not prev else ""   # non-monotonic: emit nothing
```

A panel rebuild therefore silently stops emitting reasoning rather than duplicating it. Reasonable, and undocumented outside the code.

***

## 3. Findings

| # | Where | What |
|--:|:--|:--|
| 173 | `claude_scraper` | `_sse_line_buf` is a **base-class contract with no declaration**. The base's `get_response()` reads it; a subclass that names it differently raises `AttributeError` on every turn, which the connector classifies as unknown and turns into a browser teardown + 3 retries. Fixed here; nothing enforces it for engine #4. |
| 174 | `mistral_web._frame_to_events` | Handles five patch shapes; anything else (generic `replace /contentChunks/N/text`, `add …/_context`) is dropped with no log. A wire-format change degrades silently. |
| 175 | `mistral_web` / `mimo` `is_available` | Returns `cookie_provider is not None`. A wired-but-logged-out provider reports available and fails on first use, unlike `zai` which at least has a token check (see #160 — which is wrong the other way). |
| 176 | `mimo.upload_file` | `content-md5` header is sent — correct per the capture — but the md5 is computed over the **whole file in memory** (`p.read_bytes()`), as is the PUT body. No size guard. |
| 177 | `deepseek_scraper._send_hello_warmup` | Sends a real `hello` message into the user's account before the first real turn, then discards the response. It consumes quota, appears in the account's history, and every exception is swallowed (`except Exception: pass`). |
| 178 | `qwen_scraper._parse_qwen_obj` | On a non-monotonic summary rebuild it emits **nothing** and keeps the old `_summary_sent`, so all subsequent frames compare against a stale prefix. Reasoning stops permanently for that turn. |

***

## 4. What this family shows

- **Two auth styles, one shape.** Cookie connectors inject a jar; scraper engines drive a page. Both are wired through `set_*_provider` / `_build_engine` by `ChatService`, so `engine/chat.py` is indifferent.
- **Every transport has one parsing invariant that is load-bearing and documented in a comment.** DeepSeek's `o == "APPEND"`, MiMo's overlap merge, Qwen's cumulative summary. Lose the comment and the next edit reintroduces the bug.
- **Base-class conventions enforced by nothing are a recurring failure.** `_sse_line_buf` here; `SAME_TOOL_FAIL_HALT` (#132), `_recovery_attempts` (#134), `_tasks` (#138) elsewhere. Sixth instance. This belongs in the conventions list.
