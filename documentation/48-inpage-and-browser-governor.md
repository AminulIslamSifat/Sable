# 48 — Ghost Tab Engine & Browser Governor

Two files that between them own every browser Sable spawns for a chat: `engine/inpage.py` (994 lines) runs Qwen completions *inside* a live browser tab, and `engine/browser_governor.py` (954 lines) caps how many browsers can exist and kills the ones nobody owns.

Read together because they answer the same question from opposite ends: inpage asks *"how do I make this request look like it came from the SPA?"*, the governor asks *"and what happens when a dozen of those are running at once?"*

***

## 1. `engine/inpage.py` — the Qwen ghost

### 1.1 Why an in-page fetch exists at all

Qwen's `/api/v2/chat/completions` is guarded by Alibaba's Baxia SDK. The signature is bound to send-time state — a stale token replaying into a fresh signature is what the module docstring calls the *"frozen-fingerprint mismatch"*. `curl_cffi` sends a plausible HTTP request; the SPA sends a request with `x-request-id`, `timezone`, `bx-v`, and a `Bearer` pulled from `localStorage`, all minted in the page.

So the ghost opens `https://chat.qwen.ai` as a real tab and calls `fetch()` from inside it. Zero signature work in Python.

> [!IMPORTANT] The Bearer is not optional and not automatic
> `credentials: 'include'` sends cookies but **not** the Bearer. Cookies alone return `{"success":false,"code":"Unauthorized"}`. Every JS snippet therefore hand-extracts the token from `localStorage` and attaches it, handling both a bare JWT and a `{"value": ...}` envelope.

The extraction logic is duplicated verbatim in all three snippets (`JS_CREATE_CHAT`, `JS_STREAM_COMPLETIONS`, `JS_STS_TOKEN`) — same 8 lines, three copies. A fourth copy will appear the moment someone adds an endpoint.

### 1.2 Lifecycle — five paths in `ensure()`

| # | Condition | Action |
|--:|:--|:--|
| 0 | Ghost tab already healthy | reuse |
| 1 | `engine.chat.bm` has a live context | adopt it (`_shared_bm` set, never closed by us) |
| 2 | Marker file with a live CDP port | `connect_over_cdp` |
| 3 | Profile locked by a foreign live Sable | raise `GhostError` → caller falls back to curl |
| 4 | Profile locked by our own orphan | kill it, relaunch |

The foreign-lock check is the subtle one and the comments say why. The original check asked *"is the Sable server running?"* — which is **always true when the ghost itself runs inside the server**, so every launch bailed with "profile locked by the running Sable server's browser". `_lock_holder_is_foreign_live` instead walks the `/proc` ppid chain and asks *whose* descendant the lock holder is:

```
me in chain                      -> ours, kill it
any ancestor looks like server.py -> foreign, bail
no /proc (Windows)               -> old conservative behaviour
```

### 1.3 Marker adoption has a stale-port guard

`_adopt_existing` probes the port with `_port_open` **before** `connect_over_cdp`. The comment explains the failure it prevents: a marker written by an older launch can carry a dead port while a *newer* browser holds the profile lock. Connecting to the dead port timed out, fell into `_hard_cleanup()`, and SIGKILLed the healthy browser holding the lock. Probe first, discard the marker, leave the live browser alone.

### 1.4 Health gate

`_spawn_ghost` polls up to 16×500 ms for **both** `localStorage.token` and the cookie set `{acw_tc, isg}`. Anything less raises `GhostError("ghost tab not healthy …")`. This is the only login validation on the path — which matters for §1.6.

### 1.5 Idle gate and reconnect

`stream()` runs under `_stream_lock` with `max_attempts=3`. The in-page `AbortController` timer is set to `idle_secs + 5`, deliberately **above** Python's `wait_for` deadline, so a stall is always caught in Python and handled as a reconnect rather than surfacing as an in-page `error` event.

Reconnect is allowed **only before the first chunk**:

```python
if yielded_chunk or attempt == max_attempts - 1:
    raise GhostError(...)
```

Because re-opening a stream after partial content would duplicate the answer. Every event is tagged with a per-attempt `rid`; `_on_event` drops anything whose `rid` doesn't match `_current_rid`, so an abandoned fetch's trailing chunks can't pollute the retry.

### 1.6 Payload routing at `RAW_TEXT_LIMIT = 100_000`

Above 100k chars the message goes to a temp file in `/home/sifat/sable_output/sessions/`, gets uploaded via in-page STS → `oss2` PUT, and the prompt becomes `message[:300] + "[...full text attached as a document — read the file.]"`.

Two things worth flagging:

> [!WARNING] Hardcoded home directory
> `_dump_payload()` writes to `Path("/home/sifat/sable_output/sessions")` — an absolute path with a username in it. Every other output path in the tree resolves through `engine.config.OUTPUT_ROOT`. This is the *"correct on the machine it was written on"* pattern.

> [!CAUTION] The 300-char truncation is lossy, not a summary
> The model is told to read the attached file, but the first 300 characters are also inline. If the attachment fails to parse upstream, the model answers from 300 chars and never says so.

### 1.7 Two output surfaces, one parser

`ghost_stream_chat()` prints to stdout and returns `(chat_id, parent_id)`; `ghost_stream_events()` yields the server event protocol. Both parse the same SSE, but **the parsing is written twice** — `_parse_sse` handles the event-generator path, and `ghost_stream_chat` has its own inline copy with the same `response.created` / `phase` / `summary_thought` logic.

They have already drifted: `ghost_stream_chat` honours `d.get("error")` inside the choices loop and the `RGV587` check; `_parse_sse` also emits `tool_call` events, which the inline copy never produces. Two parsers, one wire format, silent divergence.

### 1.8 The `_pipe` swap race

`_Pipe` wraps an `asyncio.Queue`. Each attempt does `self._pipe = _Pipe()` *after* `self._current_rid = rid`. `_on_event` writes to `self._pipe.q`. Because the two assignments are not atomic with respect to an in-flight page event, a chunk arriving between them lands in the **new** queue (good) but was tagged with the new `rid` — so it passes the filter and is consumed. That ordering is correct, and the `rid` check is what makes it correct. Worth stating because it looks backwards.

***

## 2. `engine/browser_governor.py` — the fleet cap

### 2.1 Two guarantees, and one deliberate non-guarantee

1. **≤ `MAX_BROWSERS` (default 2) per Sable process.** Cross-process: the count lives in slot files under `$XDG_RUNTIME_DIR/sable-browser-slots`, matched by `owner_pid`. A dev tree and a stable install no longer share a quota.
2. **Chat browsers outrank ephemeral ones.** At the cap, a `chat` claim evicts the least-recently-used `ephemeral` browser. An `ephemeral` claim never evicts anything — it waits.

> [!NOTE] Explicitly not implemented: memory caps and idle killing
> The module docstring is emphatic. A browser dies when its owner closes it or when it is an orphan. The cgroup is a *label*, not a leash — no `memory.max`, no `oom.group`, so the kernel never OOM-kills a browser tree behind Sable's back.

### 2.2 The cgroup is an ownership marker only

`_confine()` writes the pid into `<delegated>/sable-browsers/browser-<pid>/cgroup.procs`. Its only consumer is `_is_sable_confined()`, used by `reap_orphans`.

This replaced a command-line string match. The comment names what went wrong: matching `sable` in `cmdline` reaped a dev daemon whose path merely contained the word, and an outside Chromium on a similar profile path. **Cgroup membership is the only trustworthy ownership signal** — that sentence is the whole design.

### 2.3 The attach grace window is load-bearing

`_prune_dead_locked` deletes a slot whose `browser_pid` is `None` only after `SABLE_SLOT_ATTACH_GRACE` (default **180s**), and only if `_profile_has_live_cgroup()` says no confined browser is already live for that profile.

The comment records the bug that made 180 the number: at 60s, a cold profile with a large session restore was pruned mid-launch, the next claim saw a free slot, a **second** browser launched on the same profile, and the two fought over `SingletonLock` in a kill/relaunch storm.

### 2.4 Reclaim never kills a live session

`_reclaim_own_dead_browsers()` frees slots only on positive evidence of death: pid gone, or (POSIX only) the pid no longer appears in `find_browser_pids(profile)`. On Windows a live pid always keeps its slot — the code says so explicitly rather than guessing.

### 2.5 `NoSlotAvailable` carries a UI payload

The exception holds `own_slot_snapshot()`: pid, owner, priority, profile, age, idle. That is enough to render a "kill this browser" card without a second round-trip. `kill_slot(pid)` is the back end for that button.

### 2.6 Orphan reaping honours a grace window it previously ignored

`reap_orphans` skips any browser younger than `SABLE_REAP_GRACE` (180s). The inline comment is an admission: *"`grace`/`now` were declared but never checked before, so even brand-new browsers were fair game."* Fixed, and now documented.

***

## 3. Findings

| # | Where | What |
|--:|:--|:--|
| 155 | `inpage._dump_payload` | Hardcoded `/home/sifat/sable_output/sessions`. Every sibling resolves `engine.config.OUTPUT_ROOT`. Breaks the moment the tree moves or runs as another user. |
| 156 | `inpage` | Two SSE parsers for one wire format (`_parse_sse` vs the inline copy in `ghost_stream_chat`). Already diverged: only `_parse_sse` emits `tool_call`. |
| 157 | `inpage` JS snippets ×3 | Identical 8-line Bearer-extraction block copy-pasted. Add an endpoint, add a fourth copy. |
| 158 | `inpage.RAW_TEXT_LIMIT` route | Truncates to 300 chars and instructs the model to read the attachment, with no detection of attachment-parse failure — the answer silently degrades. |
| 159 | `browser_governor._own_slots_locked` | Re-reads and re-parses **every** slot file on every call, and it is called inside the flock from `_try_claim`, `acquire`'s fast-fail check, `_cap_held_by_chat`, and `stats()`. Bounded by `MAX_BROWSERS_GLOBAL` so not a leak — but it is O(all slots) inside a lock on the hot claim path. |

> [!TIP] The pattern in 159
> A slot table of at most 8 entries is not a performance problem. It is a *lock-scope* problem: the same unbounded directory scan happens while holding a cross-process flock. Cheap now, and cheap to make correct — snapshot once per claim.

***

## 4. Conventions this file confirms

- **Cross-platform liveness is always `pid_exists()`, never `os.kill(pid, 0)`.** Both files state the reason: on Windows, `os.kill` with anything but `CTRL_C`/`CTRL_BREAK` *terminates* the target, so a liveness probe would kill what it tests. This is the third file in the tree to repeat that comment.
- **A comment that names the bug it prevents is worth keeping verbatim.** The 180s grace, the foreign-lock ppid walk, the stale-port probe, the grace-window admission — four bugs, four surviving explanations.
- **Fall back, don't fail.** `inpage` raises `GhostError` and the caller uses curl. The governor degrades to unmanaged slots if the slot dir can't be created, and logs one cgroup warning then never again.
