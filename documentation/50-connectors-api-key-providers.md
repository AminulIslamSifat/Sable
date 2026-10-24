# 50 — API-Key Providers (Gemini, Local, Pass-Throughs)

The providers that need no browser: a key in a file, an HTTP POST, an SSE read. Three files of substance and three 29-line pass-throughs.

| File | Lines | Transport |
|:--|--:|:--|
| `connectors/gemini/client.py` | 697 | `generativelanguage.googleapis.com` REST + SSE, multi-key rotation |
| `connectors/local/client.py` | 363 | OpenAI-compatible `/v1/chat/completions` on localhost |
| `connectors/groq/client.py` | 29 | `OpenAICompatProvider` subclass |
| `connectors/mistral/client.py` | 29 | `OpenAICompatProvider` subclass |
| `connectors/openai/client.py` | 29 | `OpenAICompatProvider` subclass |

The three pass-throughs are identical apart from base URL and key name — no findings, and reading one reads all three.

***

## 1. Gemini — the native tool loop is broken 🔴

### 1.1 What the code intends

`stream_chat` is supposed to run a bounded agent loop: send the request, and if the model returns `functionCall` parts instead of text, execute them through Sable's skill engine, append the results to history, and re-enter the API call. Up to 20 rounds.

```python
_max_tool_rounds = 20
_tool_round = 0

while _tool_round < _max_tool_rounds:
    _tool_round += 1

attempts = len(self._keys)          # <- dedented to the while's own level
for attempt in range(attempts):
    ...
```

### 1.2 What it actually does

Line 428 opens the `while` at indent 8. Line 429, `_tool_round += 1`, is at indent 12 — the **only** statement in the body. Line 432 (`attempts = …`) and line 433 (`for attempt …`) are both at indent 8, i.e. **outside the while, after it**.

So the sequence at runtime is:

1. `while` spins 20 times incrementing a counter. Nothing else happens.
2. Control falls through to the `for attempt` key loop — once.
3. A native function call arrives, handlers run, tool results are appended to `history`.
4. `continue` at line 586 — which continues the **`for attempt`** loop, not the `while`. It moves to the next API key.
5. With one key: the `for` ends, execution reaches line 636 `"all Gemini API keys failed (rate limited or invalid)"`, yields it as an error, and returns.

> [!CRITICAL] Gemini native tool calling cannot succeed
> Every tool call terminates the turn with `all Gemini API keys failed`, regardless of whether the tool worked. The model never sees the tool result. The 20-round cap is dead code — `_max_tool_rounds` limits nothing. With N keys the tool round is replayed N times against the same history before failing.

The `while` was clearly added around the `for` at some point and only the increment got indented with it.

### 1.3 The contrast that proves it is a slip, not a design

`connectors/local/client.py` has the same loop, structurally correct:

```python
while _tool_round < _max_tool_rounds:
    _tool_round += 1

    try:
        ...
```

Same variable names, same comment, same `continue  # Re-enter API call with tool results`. The local connector's loop works. Gemini's does not. **Two copies of one pattern, one indented wrong** — this is the fourth instance of the duplication convention (see #152 pairs with #57) and the first where the copies differ in correctness.

### 1.4 A second, narrower unbound-local

```python
async for line in response.aiter_lines():
    ...
    finish_reason = candidate.get("finishReason", "")   # only assigned inside the loop
...
logger.info(
    "Gemini stream ended: ...",
    model_id, finish_reason if got_response else "no_response",
    ...)
```

`got_response` is set to `True` at line 469, **before** the `async for`. If the response body yields zero SSE lines, `finish_reason` is never bound and line 526 evaluates it → `NameError`. It is caught by the broad `except Exception` at 629, which yields `error: cannot access local variable 'finish_reason'` and pops history. Wrong message for an empty stream, but not a crash.

### 1.5 Key rotation

`_keys` loads from `system/.gemini_api_keys.json`, migrating a legacy `.gemini_api_key` on first load. `_rotate_key()` advances a modulo index, and is a **no-op returning the same key** when `len(self._keys) <= 1` or when `account_auto_switch` is disabled for gemini.

Every failure path — 401/403/429, timeout, `httpx.HTTPError` — calls `_rotate_key()` then `continue`s. With one key that means the `for` loop body runs once and exits, which is why the "all keys failed" message is accurate there and misleading in §1.2.

> [!NOTE] Non-streaming summariser bypasses rotation policy
> `_call_self_summarize` runs its own `for _ in range(attempts)` with its own rotate-on-failure, and does **not** consult `_auto_rotate_enabled`. A user who turned gemini auto-rotate off still gets summariser key rotation.

### 1.6 History is a shared mutable list

```python
body: dict[str, Any] = {"contents": history}
```

`body` holds a **reference** to `history`, and the loop later does `history.append(...)`. The request therefore picks up tool results without an explicit reassignment. It works, and it is the reason the broken loop in §1.2 still *sends* the tool results — just to the wrong place (another key, not another round).

### 1.7 Safety filters

All four `HARM_CATEGORY_*` are set to `BLOCK_NONE`, both in the stream and in the summariser. Deliberate for a companion app; noted so nobody "fixes" it thinking it was an oversight.

***

## 2. `connectors/local/` — OpenAI-compatible localhost

### 2.1 The timeout is load-bearing

```python
_DEFAULT_TIMEOUT = httpx.Timeout(connect=15.0, read=None, write=120.0, pool=15.0)
```

The comment explains: a scalar timeout applies to connect/write/read/pool alike, so a slow model emitting a large tool-call `arguments` string (a big `create_file` body) blows the read clock and surfaces a bogus *"model too large"* error. `read=None` is correct for SSE — the server closes the stream when done.

### 2.2 Every request is dumped to disk, forever

```python
_log_file = _out_root / "local_model_payload.txt"
with open(_log_file, "a", encoding="utf-8") as _f:
    _f.write(f"\n{'='*80}\n")
    _f.write(f"TIMESTAMP: ...\nMODEL: ...\nENDPOINT: ...\nMESSAGE COUNT: ...\n")
    _f.write(_json.dumps(payload, indent=2, ensure_ascii=False))
```

No rotation, no size cap, no redaction, no setting to disable it. `payload` includes the **full message array** — the whole conversation, the system prompt, and every memory block injected for that turn. One file, appended to on every local-model request.

> [!WARNING] Same class as ledger #137
> `system/consolidation.txt` appends the full prompt, full raw LLM output and resolved result with no cap. This is the second unbounded conversation log in the tree — and unlike #137 it lives under `OUTPUT_ROOT`, where users are invited to look.

### 2.3 Thinking has two sources and both are handled

`delta.reasoning_content` (llama-server's native field) is yielded directly; `delta.content` is routed through `ThinkTagSplitter` so a model that leaks `` into content produces thinking events instead of visible tags. Same splitter the MiMo and Qwen paths use.

### 2.4 Streaming tool-call preview

Tool-call deltas accumulate by `index` into `_tc_buffers`, and every 32 bytes of new `arguments` emits a `tool_progress` event carrying `native_preview_lines(...)`. `tool_pending` fires once when the name first appears. This is what makes a native call render while it streams instead of popping in fully formed — and it is the consumer for `native_preview_lines`, which ledger #147 notes is unreachable on the `openai_compat` path.

### 2.5 No instruction builder

`inject_instructions` routes to `engine.cookbook.model_settings.build_system_prompt(model_id)` — the cookbook, not `connectors.common.instruction_builder`. Local models get cookbook settings; every other provider gets the shared persona/project/output-format stack. Correct for a model served from a cookbook entry, inconsistent for the same model reached through another provider.

***

## 3. Findings

| # | Where | What |
|--:|:--|:--|
| 167 | `gemini/client.py:428-433` | **🔴** The `while _tool_round < _max_tool_rounds:` body contains only `_tool_round += 1`; the `for attempt` key loop sits *outside* it. `continue` at 586 therefore retries the **next key** instead of the next tool round, so every native function call ends at `"all Gemini API keys failed"` and the model never sees the result. The 20-round cap is dead. |
| 168 | `gemini/client.py:526` | `finish_reason` is only bound inside `async for line in response.aiter_lines()`, but line 526 evaluates it whenever `got_response` is true — which is set *before* the loop. An empty SSE body raises `NameError`, caught by the broad handler and reported as a generic error instead of "empty stream". |
| 169 | `local/client.py:~150` | Every request appends the full payload — entire conversation, system prompt, injected memories — to `OUTPUT_ROOT/local_model_payload.txt`. No rotation, cap, or opt-out. Second instance of the #137 shape. |
| 170 | `gemini._call_self_summarize` | Rotates API keys without consulting `_auto_rotate_enabled("gemini")`, unlike the stream path. Turning rotation off does not turn it off. |
| 171 | `gemini.stream_chat` error paths | `history.pop()` removes the last entry, which after a tool round is a tool result, not the user turn it was meant to discard. |
| 172 | `local/client.py` vs `gemini/client.py` | Two hand-maintained copies of the tool-execution loop with identical variable names and comments. One is correct, one is not (#167). Third instance of the "two implementations, one flaw" convention (#152/#57, and see #156). |

***

## 4. What this pair establishes

- **A duplicated pattern where the copies disagree is a bug in the copy, not a design choice.** #152 found two trimmers with the same role-blindness. Here two tool loops differ in *whether they loop at all*.
- **`read=None` on SSE is correct and should be the default.** Gemini and local both stream without a per-chunk deadline; the ghost transports instead impose 30–45s idle gates because their upstreams can tarpit.
- **Debug logging that writes the whole conversation needs the same discipline as any other store** — a cap, a rotation, or an off switch. Neither file has one.
