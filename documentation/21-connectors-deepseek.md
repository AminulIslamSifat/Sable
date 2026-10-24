# 21 — `connectors/deepseek/client.py` (2,163 lines / 100 KB)

> [!IMPORTANT]
> The most battle-hardened connector in the codebase. Pure HTTP chat (browser only for initial token extraction), PoW solved by a compiled Go binary, SSE streaming, and a token rotation + session recovery system that exists because DeepSeek fails in about six distinct ways.

**Read depth:** full file, line 1 → 2,163.

***

## 1. Event contract

```
{"type": "answer",   "text": "..."}
{"type": "thinking", "text": "..."}
{"type": "done",     "parent_id": int}
{"type": "error",    "message": "..."}
{"type": "status",   "message": "deepseek_prepare" | "deepseek_transport_ghost" | ...}
{"type": "biz_error", "biz_code", "biz_msg", "mute_until"}
{"type": "token_rotation", "from_token", "to_token", "reason", "new_chat", ...}
```

Same shape Sable's stream parser expects from every provider.

***

## 2. Constants & paths

| Name | Value | Notes |
|:--|--:|:--|
| `BASE_URL` | `https://chat.deepseek.com` | |
| `_IDLE_TIMEOUT_SECS` | **30.0** | connect, first token, or mid-stream stall |
| `_MAX_RETRIES_PER_TOKEN` | 8 | local to `stream_chat` |
| `_RETRY_DELAY_SECS` | 5.0 | |
| `_POOL_FAILURE_THRESHOLD` | 2 | consecutive failures → recycle `AsyncSession` |
| `_MAX_SESSION_CHARS` | 100,000 | **dead** — see §14 |
| `MAX_TOKENS_PER_ACCOUNT` | 10 | **dead** — never enforced |
| `TOKEN_STORE_PATH` | `system/.deepseek_tokens.json` | |
| `FP_STORE_PATH` | `system/.deepseek_fp.json` | device fingerprint per account |
| `SOLVER_PATH` | `connectors/deepseek/pow_solver/pow_solver[.exe]` | |
| `_RAW_LOG_PATH` | `LOGS_DIR/deepseek_raw.txt` | every prompt + response |

***

## 3. DSML suppression

DeepSeek occasionally emits its own internal tool syntax (`<|DSML|tool_calls>`, `<｜DSML｜invoke>`) instead of Sable's format. Two regexes:

- `_DSML_LEAK_RE = [|｜]\s*DSML|DSML\s*[|｜]` — detection only
- `_DSML_STRIP_RE` — four alternatives: full paired blocks, partial opens (missing `>`), partial closes, stray pipe fragments

`_strip_dsml(text)` applies the strip regex then collapses `\n{3,}` → `\n\n` and ` {2,}` → ` `.

> [!WARNING]
> The third alternative in `_DSML_STRIP_RE` is `(?:</｜DSML｜[a-zA-Z_0-9\s=\"']*>)?` — the **entire group is optional**, so that branch matches the empty string. `re.sub` then matches at every position in the text. Output is unchanged (empty→empty), so it's correct-but-wasteful, not broken. §14.

The leak detector arms `_dsml_warn_chats` per chat, which makes `_get_dsml_guard` inject on **every subsequent turn** of that chat:

```
[WARNING - POTENTIAL CRASH] Don't use dsml, Use the defined tool calling structure given to you.
```

> [!TIP]
> The per-turn format reminder is deliberately off:
> ```
> # Per-message tool-format reminder is disabled — the format is already defined
> # in the system prompt, and re-injecting it on every turn wasted tokens and
> # nudged the model toward the very DSML tags we don't want. We now inject a
> # guard only when a leak is actually detected.
> ```

***

## 4. Token store — `system/.deepseek_tokens.json`

Shape: `{account: [token, ...]}` where account is `browser-data-accN`.

### 4.1 Three readers, three contracts

| Function | Falls back? | Use for |
|:--|:--|:--|
| `get_own_token_for_account(acct)` | **never** | capture/refresh — never persist a foreign token under the wrong key |
| `get_token_for_account(acct)` | requested → active → **any** | read-only chat calls: any live token beats none |
| `get_unique_tokens()` | n/a | dedup across all accounts, for rotation |
| `get_unique_tokens_with_accounts()` | n/a | `[{token, account}]`, first occurrence wins |

> [!IMPORTANT]
> The split exists because of a real bug:
> ```
> # Use this for capture/refresh paths so we never persist a foreign token
> # under the wrong account key (the bug that smeared 'ds' badges everywhere).
> ```

### 4.2 `_load_token_store` — the trimming rule

```python
keep = valid[-1:]                     # newest only
if pinned and pinned in valid and pinned not in keep:
    keep.insert(0, pinned)            # pinned survives, kept FIRST
```

```
# Collapse to the CURRENT token (latest) — older entries are stale
# duplicates left behind by a refresh. The manually PINNED token is the
# one exception: it must survive a refresh, otherwise the user's selection
# dangles out of the list, no key renders as active, and the rotation start
# silently reroutes to another account.
```

So despite the list shape and the "accumulate" docstring on `save_token_for_account`, **every load reduces each account to at most 2 entries**: the pinned token and the newest. The rotation pool size is therefore the *number of accounts*, not the number of tokens ever saved.

Migration handled in-place: old `{account: "str"}` → `{account: ["str"]}`; legacy `.token_cache.json` migrated once behind `_LEGACY_MIGRATED`.

> [!NOTE]
> `_LEGACY_MIGRATED` is a process-global. If the store is later emptied, the legacy file is never re-checked in that process — correct (it was already deleted on success), just noting the guard is one-shot by design.

Both `_save_token_store` and `_save_fp_store` use `mkstemp` + `os.replace`, with the temp file unlinked on any `BaseException`.

***

## 5. Pinned token — the single source of truth

`settings.json` keys: `deepseek_preferred_token`, `deepseek_preferred_account`.

Read by `_read_pinned_token()`, `_get_preferred_token_from_settings()`, and the `token` property — all three agree by construction.

### 5.1 The `token` property checks pinned FIRST

```
# The pinned preference is checked FIRST, not last. Checking the
# in-memory `self._token` first let an automatic refresh (set_token) make
# this report the freshly scraped JWT while _init_rotation() still
# started on the pinned one — so the UI flagged the new key as selected
# and the request went out on a different one.
```

That's a UI-truth bug: the settings page and the send path must resolve the same key.

### 5.2 `set_token(token, account, persist, pin)`

| Flag | Meaning | Who passes it |
|:--|:--|:--|
| `persist=True` (default) | write under the account | normal capture |
| `persist=False` | in-memory only | cross-account rotation handoff |
| `pin=True` | promote to `deepseek_preferred_token` | **refresh path only** |

> [!CRITICAL]
> ```
> # Pass pin=True from the REFRESH path only. ... Manual switches/rotation
> # handoffs must NOT pin — otherwise a handoff would silently steal the
> # user's selection.
> ```

`set_token` also **clears `_parent_ids` and drops every `_chat_sessions` entry keyed on that token** — a token switch is a new upstream namespace, so stale message ids would be invalid.

***

## 6. Rotation

`_init_rotation()` loads `get_unique_tokens()`, ensures the in-memory token is in the pool, then picks a start index by priority:

```
preferred (settings)  →  self._token  →  own-account token  →  0
```

> [!CRITICAL]
> The dangling-pin fallback is deliberate:
> ```
> # A dangling pin must NOT silently reroute rotation to
> # all_tokens[0]: that sent the request on one account's token
> # and made the UI report a foreign key as selected. Fall back
> # to this client's OWN account before anything else.
> ```

`_advance_rotation()` **no-ops when auto-rotate is disabled** for `deepseek` — so "auto-switch off" means "stay on the failing token and report the failure", which is the correct meaning of the toggle.

`_auth_headers_for(token)` fingerprints per **token owner**, not per active account:

```
# A pinned token from another account sent with the active account's
# device header (x-hif-leim) is exactly the token/device mismatch
# that gets DeepSeek accounts silently banned.
```

Same reasoning drives `_ghost_account()` — the ghost browser opens the *token's* account profile, not the global active one.

***

## 7. Session & PoW preparation

`_prepare_request_with_rotation(chat_id)` → `(session_id, headers)` or `None`.

Per `(token, chat_id)` pair:
1. Restore `chat_session_id` from the DB (`get_upstream_session_id`) if the process restarted — *"so we continue the same upstream conversation"*
2. Else `_create_session(headers=auth)` and persist via `set_upstream_session_id`
3. `_get_challenge(headers=auth)` → `_solve_pow` (thread executor) → `_build_pow_header`
4. Return headers with `X-DS-PoW-Response` and a `Referer` pointing at the session URL

`cid = chat_id or "__ephemeral__"` — ephemeral chats share one session per token.

### 7.1 PoW nonces are single-use

```python
async def _refresh_pow_header(headers) -> headers:
```

```
# DeepSeek PoW nonces are single-use. Replaying the header from a previous
# request (including the hello seed) makes the server silently return an
# empty 200. Must be called before every retry or resend.
```

That single sentence explains three separate call sites: after the hello seed, at the top of every retry, and after a session recreate.

`_solve_pow` runs the Go binary via `subprocess.run(..., timeout=30)`, fed the challenge as JSON on stdin, returning the nonce on stdout.

### 7.2 `_create_session` — muted-account detection

`data=null` on a 200 response is treated as failure:

```
raise DeepSeekAPIError(f"Session create returned no biz_data (code={code}, msg={msg})")
```

*"DeepSeek sometimes returns 200 with data=null for muted/rate-limited accounts."* Response shape accepted: `biz_data.chat_session.id` (new) or `biz_data.id` (legacy).

***

## 8. Transport — ghost vs curl

### 8.1 Ghost (preferred)

`GhostSession.for_account(acct)` — an in-page `fetch` inside a real Chromium tab, so the request carries genuine browser identity. `ghost.open_stream(token, body, pow_hdr, idle_secs=30)` streams SSE lines out of the page.

`_completion_opener` retries the ghost **once** with `force_respawn=True` before falling back to curl:

```
Ghost stream open failed (%s) — respawning browser & retrying
Ghost stream open failed again after respawn (%s) — falling back to curl for this request
```

`_ghost_broken` is reset to `False` at the top of every `stream_chat()`, so a curl fallback lasts **one message**, not the whole session.

### 8.2 curl fallback

```python
self._http = AsyncSession(impersonate="chrome", timeout=120)
```

```
# impersonate="chrome" replicates Chromium's exact TLS fingerprint
# (cipher suites, extensions, ALPN, HTTP/2 SETTINGS frame).
# This is what WAF checks — raw httpx/OpenSSL mismatches the Chrome UA.
```

### 8.3 `_EnterTimeoutCM` — bounds the handshake, not the body

```
# Wrap a curl_cffi stream context manager so that *opening* it — which is
# what actually fires the HTTP request and blocks for response headers — is
# bounded by an idle timeout. The body stream is left untouched so long
# responses aren't capped; only a stalled connect/header phase is killed.
```

`__aenter__` = `asyncio.wait_for(cm.__aenter__(), timeout)`. The body is then bounded separately by `_iter_with_idle_timeout`.

### 8.4 Pool recycling

At `_POOL_FAILURE_THRESHOLD` (2) consecutive `_mark_failure()` calls, `_get_http()` closes and rebuilds the `AsyncSession`:

> *"Fixes the 'requests hang forever after network change' bug where curl_cffi's pooled TCP sockets become half-open zombies."*

Connection-class exceptions (`ConnectionError`, `OSError`, `TimeoutError`, `CurlConnErr`, `CurlConnectTimeout`) **break the retry loop immediately** instead of burning 8 × 5 s on dead sockets.

***

## 9. `stream_chat` — the main flow

### 9.1 Fresh-chat detection survives restarts

```python
_parent_id = self._parent_ids.get(chat_id)
if chat_id and _parent_id is None:
    _db_parent = get_parent_id(chat_id, None)
    if _db_parent is not None:
        _parent_id = int(_db_parent)
```

Without this, a server restart made every existing chat look fresh and trigger the hello-seed flow, forking the upstream tree.

### 9.2 Hello-seed flow

For a fresh chat:
1. `_seed_hello_message(session_id, headers)` sends `prompt="hello"`, `model_type="default"`, `parent_id=None`
2. Captures `response_message_id` from the `done` event
3. **Refreshes PoW** — the seed consumed the nonce
4. Real message goes out chained to `_seed_id` with `model_type=None`

If the seed fails, the real message is sent as message #1 with `model_type="default"`.

> [!NOTE]
> `_seed_hello_message` docstring: *"Backend-only — callers MUST NOT forward any events to the frontend."* It returns only the id; `_send_and_capture_id` consumes the whole stream internally.

### 9.3 The prompt is never full history

```
# The prompt is ALWAYS just the current message (+ optional reminder).
# Never the full serialized history. DeepSeek chains server-side.
```

Client-side `history` is maintained **only** for sliding-window trimming, DB seeding on provider switch, and session-recreate re-seeding. It is never sent as context.

***

## 10. Failure taxonomy — six distinct recoveries

| Failure | Detected by | Recovery |
|:--|:--|:--|
| 401/403/429 | HTTP status | `break` → rotate token immediately |
| other non-200 | HTTP status | `break` → rotate |
| `biz_error` (muted etc.) | `biz_code != 0` | `break` → rotate |
| **invalid session** | `_is_session_invalid(biz_code, biz_msg)` | recreate session + re-seed **full history**, once per token |
| empty 200 | no answer + no thinking | retry same chain with fresh PoW (up to 8), then rotate |
| idle timeout / connection error | `asyncio.TimeoutError`, `CurlReadTimeout`, conn exceptions | `break` → recycle pool, rotate |

### 10.1 Session-invalid is not a token problem

```python
def _is_session_invalid(biz_code, biz_msg) -> bool:
```

```
# DeepSeek returns HTTP 200 + biz_code=1 with biz_msg "invalid chat session id"
# when the session UUID is stale, deleted, or belongs to another account.
# This is a SESSION-level failure — token rotation will NOT fix it, and
# nulling the parent chain just causes amnesia. The only correct recovery is
# to create a fresh upstream session and re-seed it with the full local
# conversation history.
```

Gate: `"session" in msg.lower()` **and** one of `invalid|not found|not_found|expired|deleted|does not exist`.

Recovery: create a new session, persist it, clear `_parent_ids[chat_id]`, repoint the `Referer`, serialize every non-system history message (DSML-stripped) into a `=== CONVERSATION HISTORY ===` preamble, set `model_type="default"` + `parent_message_id=None`, then `continue` so the next iteration refreshes PoW and sends.

> [!TIP]
> The preamble tells the model not to acknowledge it:
> *"continue naturally, do not acknowledge this preamble, do not repeat the history back."*

### 10.2 Empty response — commit, don't retry

The single most important guard in the file:

```
# If we already captured a response message_id from the 'done' event,
# DeepSeek DID create the node upstream — only delivery died (mid-stream
# disconnect). Retrying with the SAME parent_message_id would branch the tree
# (X→Y AND X→Z), which is exactly what poisons the chain on restart.
```

So: commit `_parent_ids[chat_id] = _response_parent_id`, yield an error, and `return` — **do not** resend.

And separately, when there is *no* message id:

```
# An empty 200 is treated like any other transient failure: retry the SAME
# chained request (with a fresh PoW each time) up to the per-token cap, then
# rotate. Dropping the parent chain here silently re-rooted the conversation
# and orphaned all prior context — worse than a clean rotation.
```

### 10.3 Missing message_id on a successful turn

```
# SAME token + SAME chat => NEVER null the parent on a missing message_id.
# A missing id is a transient upstream glitch; nulling it would silently
# re-root the conversation (full amnesia on the next turn).
```

### 10.4 Parent reset ONLY on a real token change

```python
if chat_id and _new_token != _old_token:
    self._parent_ids[chat_id] = None
```

*"Only a real token change (different account) may reset the chain, because the new account cannot see the old session's messages."* Same rule drops the cached session (`_chat_sessions.pop(...)`) — and only for a genuinely different token.

***

## 11. SSE parsing — `_iter_completion_events`

Accepts **both** framed SSE and a raw JSON body:

```
# DeepSeek returns errors as a plain application/json body with no
# SSE framing. The old `data: ` guard silently dropped those, which
# is exactly why an "invalid chat session id" error surfaced to the
# user as an empty response (and then got retried/rotated into
# amnesia instead of being handled as a session failure).
```

Event shapes handled:

| Path | Meaning |
|:--|:--|
| `response_message_id` present | capture `last_message_id` |
| `p == "response/status"` + `v == "FINISHED"` | yield `done` with `last_message_id` |
| `p == "response/fragments"` + `o == "APPEND"` | first fragment → sets `current_frag_type`; content → answer/thinking |
| `p == "response/fragments/-1/content"` | continuation; **`o` may be absent** |
| `v` is dict with `"response"` | metadata + fragment list |
| `v` is str, `p is None` | raw text |
| `p == "response"` + `o == "BATCH"` | skipped (usage/status) |

> [!CRITICAL]
> The `-1/content` branch exists because:
> ```
> # Handle both o="APPEND" and o=None — DeepSeek sometimes sends
> # content continuation events without the APPEND operation tag.
> # Without this, fragments like 'tool' in '<tool_call>' get dropped,
> # producing broken tags like '<tool_call>'.
> ```

A truncated tool tag is a silent correctness failure — the parser downstream sees a malformed action and the turn breaks.

`biz_code` is checked at top level **and** one level down under `data` (*"sometimes nested one level under `data` (plain JSON error body)"*), and on a hit it yields `biz_error` and `return`s — the generator stops, so no further events leak out of a dead stream.

***

## 12. Token refresh

Two paths in `_refresh_token`:

1. **Injected refresher** (`self._token_refresher`) — a browser session owned by `ChatService`
2. **Standalone fallback** — `launch_persistent_context(headless=True)` on `get_browser_data_dir("deepseek")`, `goto chat.deepseek.com`, wait 2 s, read `localStorage.userToken`

`_parse_localstorage_token` unwraps `{"value": "<jwt>", "__version": "0"}`.

Both paths call `set_token(token, pin=True)` — a refresh is a user-visible "use this now" action.

> [!NOTE]
> The standalone path hardcodes `--disable-gpu --no-sandbox` and `headless=True`, unlike `open_account_browser` which deliberately uses the real OS keyring (§ doc 18). Token extraction only reads `localStorage`, so the mock-vs-real keyring question doesn't apply — but it is a *second* browser launch path that ignores `resolve_browser_for_profile`.

***

## 13. Client pool & cancellation

```python
_clients: dict[str, DeepSeekClient] = {}      # account → client
_default_client: DeepSeekClient | None = None
```

`get_client(account=None)` returns the default (dynamic active account) or a per-account pinned client.

`cancel_active_streams()` iterates every cached client + the default, calling `client.cancel()` → `GhostSession.cancel_stream()`. Used by `/api/chat/stop`.

`close()` tears down the `AsyncSession` **and** pops this account's `GhostSession` from the registry — *"so Playwright's driver subprocess exits cleanly (otherwise its transport is GC'd after the event loop closes and spits a RuntimeError traceback)"*.

***

## 14. Findings

> [!CRITICAL]
> **1. `_response_parent_id` can be unbound in the idle-timeout handler.** It is declared *inside* the `async with await self._completion_opener(...)` block:
> ```python
> async with await self._completion_opener(headers, body, _ghost) as resp:
>     ...
>     _response_parent_id: int | None = None
> ```
> but read in `except (asyncio.TimeoutError, CurlReadTimeout)`. `_completion_opener` wraps the connect phase in `asyncio.wait_for`, which raises `asyncio.TimeoutError` — the exact exception this handler catches. On a first-attempt connect timeout, `_response_parent_id` was never assigned → `UnboundLocalError` raised *inside* the handler, masking the real timeout. Only bites when no previous retry iteration bound it (Python function scope keeps it alive otherwise). **Not fixed.**

> [!WARNING]
> **2. The whole tool-format reminder builder is dead code.** `_FORMAT_REMINDER_ENABLED = False` makes `_get_reminder()` return `""` on its first line, so all the Hermes/DSML/Native/JSON branches below it can never execute. Since `stream_chat` does `self._get_reminder() or self._get_dsml_guard(chat_id)`, only the DSML guard can ever be injected. §3. **Not fixed** — re-enabling it is a token-cost decision.

> [!WARNING]
> **3. `MAX_TOKENS_PER_ACCOUNT = 10` is never enforced.** `save_token_for_account` appends without a cap; `_load_token_store` trims to `[-1]` + pinned. The constant is decorative — the real cap is 2 per account. §4.2.

> [!WARNING]
> **4. `_MAX_SESSION_CHARS = 100_000` is dead.** The actual value comes from `get_model_config(self._model_id).get("max_session_chars", 100_000)` — the literal, not the constant.

> [!WARNING]
> **5. `_DSML_STRIP_RE`'s third alternative matches the empty string** (`(?:…)?` around the whole group), so `re.sub` scans every character position. Correct output, wasted work on every stripped message. §3.

> [!WARNING]
> **6. `_log_raw` writes full prompts and responses to `logs/deepseek_raw.txt` with no rotation, no redaction, and no size cap.** Every user message, every system instruction payload, and every re-seeded conversation history lands in plaintext on disk, forever. Real PII/secret exposure surface. **Not fixed.**

> [!NOTE]
> **7. `_is_session_invalid` requires `"session"` in the message.** A variant like `invalid chat id` would not match and would be treated as a generic `biz_error` → token rotation, which cannot fix it. Narrow but plausible.

> [!NOTE]
> **8. `_ghost_broken` and `_POOL_FAILURE_THRESHOLD` are class attributes** assigned per-instance later. Works correctly (instance assignment shadows), but a reader may assume they're shared state.

> [!NOTE]
> **9. `_session_recreated` is scoped per token attempt**, so a session can be recreated once per token. That appears intentional (a fresh token is a fresh namespace) but means a persistent upstream fault can trigger one full-history re-seed per token in the pool.

No code changed in this file.

***

## 15. Cross-references

- [[07-engine-config]] — `get_active_account("deepseek")`, `get_browser_data_dir`, `get_model_config`, `LOGS_DIR`
- [[12-connectors]] — `connectors.common.instruction_builder`, `context_summarizer.strip_summarize_tag`, `jitter`, `profile_hub`, `error_ctx`
- **`connectors/deepseek/ghost.py`** — `GhostSession.for_account`, `open_stream`, `cancel_stream`, `ghost_enabled`, `_SYSTEM_DIR`
- [[17-server-chat-route]] — `/api/chat/stop` → `cancel_active_streams`; `server.database.get/set_upstream_session_id`, `get_parent_id`
- [[18-server-settings-route]] — the settings UI reads `.token`, calls `switch_api_key` / `refresh-token`
- [[19-engine-library-index]] — same "checkpoint after each unit, never claim terminal" discipline

> [!IMPORTANT]
> The invariant that makes this file work: **the parent chain is only ever reset on a genuine token change or an explicit fork.** Every other failure path — empty response, idle timeout, missing message id, 8 failed retries — deliberately *preserves* the chain, because re-rooting the upstream conversation is indistinguishable from amnesia to the user. Any new failure branch must ask "would this lose the chain?" before it touches `_parent_ids`.
