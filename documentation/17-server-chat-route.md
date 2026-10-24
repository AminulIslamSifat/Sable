# 17 — `server/api/routes/chat.py` (4,404 lines)

> [!IMPORTANT]
> This is the single most important file in `server/`. It is the agentic loop: every chat turn, every tool call, every retry, every guardrail, every approval pause goes through here. Read after [[11-server]] (application/auth/database) and [[04-engine-skills]] (parser/handlers), because this file is the glue between them.

**Read depth:** full file, line 1 → 4,404.

***

## 1. What lives in this one file

| Section | Purpose |
|:--|:--|
| Module helpers | `_auto_switch_enabled`, `_build_account_debug_info`, `_reconstruct_message_content`, history cache, `_get_skill_engine` |
| `POST /api/chat` | The main streaming endpoint — **~3,300 lines**, one giant `while True` loop |
| `POST /api/chat/stop` | Upstream cancellation (Qwen stop API, scraper abort) |
| Auto-switch context builder | Head/tail extraction + LLM summarization of long chats before account switch |
| `/api/skills/approve|deny/{tag_id}` | Permission approval replay |
| `/api/skills/cwd-approve|deny/{tag_id}` | CWD-warning approval replay |
| `POST /api/skills/batch-complete/{chat_id}` | Merge a whole permission batch into one model-facing message |

It imports from nearly every subsystem: `engine.config`, `engine.memory_search`, `engine.scraper`, `engine.skills`, `engine.think_parser`, `engine.agents.resilience`, `connectors`, `connectors.deepseek.client`, `server.database`, `server.config`.

***

## 2. The history cache — the OOM fix

> [!TIP]
> The best comment in the file, quoted verbatim:
>
> ```
> # Per-turn cache of the (expensive) DB history reconstruction. Keyed by
> # (chat_id, message_row_count) so it auto-invalidates the moment a new
> # message is persisted, but is reused across every round of a single turn.
> # Without this, a 700-tool-call chat re-parsed the entire message table +
> # all skill_events on EVERY loop iteration — the quadratic flood that
> # OOM-killed a 16GB+20GB-swap machine.
> ```

`_HISTORY_CACHE: dict[tuple[str, int], list[dict]]` — key is `(chat_id, row_count)`.

- `_chat_row_count(chat_id)` does a cheap `COUNT(*)` purely as the cache key. Returns `-1` on DB error (so errors don't collide with a real empty chat).
- `_load_history_with_skills(chat_id)` rebuilds history **including tool output**, then trims.
- Cache is flushed wholesale (`clear()`) once it exceeds **32 entries** — crude but bounded.

> [!CRITICAL]
> The second half of that comment is the real lesson:
> *"Loading the whole table then trimming still materializes every row + every skill event in RAM — that was the crash. **The LIMIT must be in the query.**"*
>
> `get_messages(..., limit=MAX_DB_HISTORY_MESSAGES, include_skill_events=True)` — bounded at SQL level. Then `trim_history_messages()` runs as an explicit invariant, not the primary defence.

`_reconstruct_message_content()` rebuilds `content + build_tool_feedback(skill_events)` so **token counting sees what the model actually saw** — text plus every tool result fed back. This is the same accumulation philosophy as the per-round token totals.

`_get_skill_engine()` is a lazy module-global singleton with `agent_id="maria"` — skills are discovered once, not per request.

***

## 3. `POST /api/chat` — pre-flight (before the stream)

Order of operations, before any SSE is emitted:

1. **Session startup** — resolve/create upstream session; failure returns `{"error": "Session startup failed: ..."}` or `"Could not create chat session"`.
2. **Memory search** — `get_searcher()`, gated by `_MEMORY_SEARCH_SETTINGS` JSON.
3. **Local-model cookbook toggles** — `_is_local_model` from `get_model_config().api_backend == "local"`, then `use_memory` / `use_utilities` from `engine.cookbook.model_settings`.
4. **Project memory resolution** — *early*, because it decides category filtering.
5. **Then** the stream opens.

### 3.1 Memory gating chain

Every gate must pass, in this order:

- `_ms_cfg["enabled"]` (settings JSON)
- **not** low-RAM — auto-disabled below **8 GB** total (`SC_PAGE_SIZE * SC_PHYS_PAGES`)
- `_local_use_memory` (cookbook, local models only)
- `len(request.message) <= max_prompt_chars` (`_DEFAULT_MAX_PROMPT_CHARS` fallback)

> [!NOTE]
> The low-RAM auto-disable is silent — no user-visible signal. A user on a 4 GB box will never know why memory "stopped working".

**Context window for thin follow-ups:** before searching, it builds `_mem_context` from the **last 8 messages**, keeps user turns only, drops the one being sent now, keeps the **2 most recent prior turns**, joins oldest-first, truncates to **600 chars**. This is what lets `"go on"` or `"move to /legacy"` still retrieve the right memory.

**Dedupe:** results are filtered against `get_injected_memory_keys(chat_id)` — a key already injected in this chat is never injected again. Survives restarts because it's persisted via `save_injected_memory_keys()`.

`_memory_used` carries `{key, value, category, score}` (score rounded to 3dp) out to the frontend as a `memory_used` SSE event.

### 3.2 Project memory & category filtering

```
early_proj = get_project(get_chat_project_id(chat_id))
  use_universal_memory == False  →  _allowed_mem_cats = {"protected"}   # universal OFF: only protected passes
  project_memory_enabled         →  _proj_mem_enabled = True
```

Project memory uses a **separate searcher** (`engine.memory_search.get_project_searcher(proj_id)`), `top_k=5`, path `system/projects/<id>/Memory.json`, and shares the same injected-key dedupe set.

### 3.3 Checkpoint capture

If `request.cwd` and a user row was saved:

```
get_checkpoint_manager(cwd).save_checkpoint(chat_id, user_msg_id, "turn_start")
save_checkpoint(chat_id, user_msg_id, "turn_start", sha, cwd)
```

Before the agent touches anything. Failures are `logger.warning`, never fatal.

***

## 4. Context assembly — `api_message` vs stored message

> [!IMPORTANT]
> **`timestamped_message` is what gets persisted. `api_message` is what gets sent.** These diverge deliberately.

`_context_parts` is built first, user message is **always last**:

```
api_message = "\n\n".join(_context_parts) + "\n\n" + timestamped_message
```

What goes into `_context_parts`:

| Part | Gate |
|:--|:--|
| `chat_title` mandate | `_msg_count <= 1`, `parent_id is None`, not a sub-chat, `_layout_mode != "study"`, `_inj["chat_title"]` |
| Platform hint | `_inj["platform"]` — per-OS paths/commands (Linux/Windows/Darwin table) |
| Schedule context | `_inj["schedule"]` — next **10 days**, formatted daily/weekly/occasional |
| Memory block | `_inj["memory"]` |
| Study grounding | always if present — **API payload only** |

> [!TIP]
> `_is_sub_chat = "-critique-" in chat_id or "-agent-" in chat_id`. Sub-chats skip the mandatory `chat_title` nudge because *"forcing a title tool call wastes a round on every sub-chat turn."*

The study-mode exclusion is documented in-code:

```
# ponytail: study mode has no chat_title tool (get_study_tool_schemas) and
# its directive replaces the chat harness — injecting the mandatory-title
# nudge just makes the model hallucinate a tool call it can't make.
```

And study grounding:

```
# Study grounding rides in the API payload only — it must never be written
# to the stored user message (timestamped_message is what gets persisted),
# so reopening the chat shows the user's real question, not the doc dump.
```

### 4.1 Backend guard

```
# Guard: a model that DECLARES an api_backend but whose connector is not
# available must NOT silently fall through to the Qwen browser path.
```

Without this, a misconfigured model (e.g. `deepseek-scraper`) silently runs on the Qwen browser transport — wrong credentials, wrong behaviour, no error. This guard exists because that happened.

***

## 5. Fork-history injection — a bug that got fixed twice

Runs only when `parent_id is None` (first turn of a fork). Reads `chats.fork_history`, prepends it to `current_message`, sets `_fork_history_injected`.

> [!CRITICAL]
> **Read + prepend only. Do NOT clear here.** Verbatim:
>
> ```
> # NOTE: read + prepend only. fork_history is NOT cleared here — it is
> # NULLed after a fully successful turn (see success path below). Clearing
> # at injection time meant any failed / empty-200 / rotated first send lost
> # the fork context permanently — the "context vanishes for no reason" bug.
> ```

No provider gate — it's purely "is the column non-NULL". Works for Qwen (server-side `upstream_session_id`) and DeepSeek (client-side `chat_session_id` keyed on local chat id).

***

## 6. The loop — state that must survive a cancel

```
answer_parts, _raw_answer_parts, thinking_parts, skill_events, round_skill_events
```

> [!IMPORTANT]
> `round_skill_events` is **bound outside the loop and outside the try** on purpose:
> *"They are merged into skill_events only AFTER a round fully completes, so this must survive until the cancel handler runs."* Same for `_main_iter` and `round_event_source`, so the cancel path can always close an in-flight upstream generator.

Per-round reset at the top of `while True`:

```
round_skill_events, round_thinking_parts, round_answer_parts, round_flow = [], [], [], []
_pending_teacher_escalation = {"needed": False}
```

`round_flow` preserves **exact emission order** of text and UI blocks. Verbatim comment: *"was: all text, then all tools"* — i.e. replay used to reorder the conversation.

### 6.1 Three independent guards

| Guard | Catches |
|:--|:--|
| `LoopDetector` (`engine.agents.resilience`) | Error-aware repeats, no-progress, stubbing. **Primary.** |
| `MainChatGuard` | Malformed / incomplete `<action>` markup. **Secondary.** |
| `TurnCapTracker` | Per-turn caps on web searches + subagent spawns |

`LoopDetector.check_decision(name, args, error_msg)` returns one of four actions:

- **`block`** — HARD STOP, tool never executes, error event streamed.
- **`recover`** — reset upstream session (`current_parent = None; final_parent = None`), build a recovery prompt, **`break`** out of the dispatch loop to force a re-think. Also flags teacher escalation if this is a subagent.
- **`warn`** — `_loop_warning` skill event, execution continues.
- (none) — proceed, subject to `_turn_caps.check_and_record(name)`.

> [!TIP]
> The `recover` path passing `original_task=api_message` is what lets the recovery prompt restate the actual goal instead of just saying "you failed".

### 6.2 Teacher escalation is split across sync/async

The flag is set inside `_dispatch_events` (sync), consumed **after** dispatch returns (async), because the agent runtime `await` can't happen inside the sync dispatch. A comment marks the boundary: *"collected here (sync), executed after `_dispatch_events` returns (async context)."*

***

## 7. Tool execution path

Each parsed item goes through, in order:

1. `permission_request` passthrough (sets `_halt_dispatch = True` — **stops the rest of the round**)
2. `_guard.record_command(name, content)`
3. `LoopDetector.check_decision(...)`
4. `TurnCapTracker.check_and_record(...)`
5. `engine.process_tag(name, attrs, content, chat_id, cwd)` → generator of events
6. Events of type `skill_start|skill_output|skill_end|file_edit|permission_request|cwd_warning` are appended to `round_skill_events` **and** streamed

`HANDLER_MAP` is injected at `SkillEngine` construction — this file never dispatches by name itself.

***

## 8. Stream resilience — three separate retry machines

### 8.1 Session recovery (`chat_not_found`)

Upstream Qwen session expired. Sequence:

1. `_ensure_headers()` → `create_new_chat()`
2. On failure: `_refresh_headers()` → retry once
3. `set_upstream_session_id(chat_id, new_id)` — **local chat id never changes**
4. Rebuild history from `get_messages(chat_id, include_skill_events=True)`, serialising tools as:
   - `[tool_call]: name({attrs})`
   - `[tool_result]: name (ok=True): <result[:2000]>`
5. Wrap in `[PREVIOUS CONVERSATION] … [END PREVIOUS CONVERSATION]`, re-stream with `parent_id=None`

> [!WARNING]
> Tool results are truncated to **2,000 chars** here — independent of the 75,000-char caps raised in `native_tools.py` / `skills/events.py`. A recovered session sees a much thinner history than a live one. Documented, not fixed.

No `meta` event is emitted because the local chat id is unchanged.

### 8.2 Stall timeout

`_MAIN_STALL_TIMEOUT` seconds with **no chunk after the first** → retry, up to **3 attempts**. Then `status: stream_stall_timeout_triggering_switch` + `waf_blocked` event → auto-switch handler.

Retry differs by transport: API models rebuild `_connector.stream_chat(**_stream_kwargs)`; browser models `close_scoped(bdd)` → `_ensure_headers(bdd)` → `service.stream_events(...)`.

> [!NOTE]
> `# ponytail: removed retry_stream wrapper (see L1475)` appears twice — the wrapper was removed because it double-counted retries. The raw `stream_events` call is deliberate.

### 8.3 Empty response

`StopAsyncIteration` with no accumulated answer text and no `stream_error` → retry. `_empty_response_total` is a **global** cap (`_EMPTY_RESPONSE_MAX_TOTAL`) across accounts; hitting it fails cleanly with a message naming the provider and "likely rate limited", so the frontend can send again without pressing stop.

***

## 9. The `done` event — exactly once

> [!CRITICAL]
> Upstream `done` is **swallowed**: `final_parent` updated, chat URL persisted, then `continue`. The single user-facing `done` is emitted in `finally` with the final parent.
>
> Verbatim: *"Falling through to the unconditional `yield sse(event)` below is what produced duplicate done events for one round."*

`_done_emitted` guarantees one `done` per stream. `_agent_killed` (set on `CancelledError`) prevents `finally` from marking a killed agent `completed`.

On `done`, `save_chat_url(chat_id, event["chat_url"])` persists the upstream URL so a reopened chat resumes the same upstream conversation (scraper transports).

***

## 10. Pauses — three kinds, all stop the round

| Trigger | Detection | Result |
|:--|:--|:--|
| `ask_user` | `skill_end.result.pause` | Stream stops, waits for user's next message |
| `permission_request` | any event of that type | `_halt_dispatch`, batch summary |
| `cwd_warning` | any event of that type | Same |

On pause, a `permission_batch` SSE event carries `permission_ids`, `cwd_ids`, `total`, and a **desktop notification** fires via `engine.notifications.policy.notify_category(..., urgency="critical")`.

> [!IMPORTANT]
> Guard checks are **skipped** during a pause:
> *"the tool call is valid but paused; flagging it as malformed/incomplete causes the model to see an error and retry the same operation."* — i.e. without this, approving a command made the model do it twice.

***

## 11. Queued user messages

Messages typed while the model is working are drained at the **round boundary**, not by aborting the stream.

- `_q_hard_stop` — no delivery during error / pause states.
- Persisted as a real `user` row branching from `final_parent`, so replay keeps the interjection in place.
- `queued_injected` SSE carries `temp_ids` (frontend placeholders) + real `ids`.
- If the round produced tool feedback, the user text is **appended to the end** of that feedback ("visually fenced so the model reads it last"); otherwise it becomes the next user turn outright.

***

## 12. `chat_title` micro-optimisation

```
_only_chat_title = all skill_end name == "chat_title"
if _only_chat_title and _has_answer_text:
    round_skill_events.clear()
```

If the only tool this round was `chat_title` and the model already emitted real text, the `[chat_title] OK` feedback is dropped — no extra round burned to acknowledge a rename.

***

## 13. Subagent format validation

For `-agent-` chats only, after a clean round:

- `get_role_config(role).required_sections` → check each header appears in the round text (case-insensitive substring).
- Threshold: **≥50%** of required sections.
- On failure: a `format_error` triple (`skill_start`/`skill_output`/`skill_end`) is appended to `round_skill_events` **and** to `feedback`.

> [!TIP]
> The comment explains why it's a skill event and not just an injected nudge:
> *"Surface it as a user-visible skill event so the format correction is observable in the chat, not just injected into the model's next-round context."*

***

## 14. `wait_agents` collection

Tools returning `{collect: true, agent_id}` are accumulated; after dispatch, `runtime.wait_all(ids, timeout=300)` runs and results are injected as a synthetic `wait_agents` `skill_output` — `[Agent id (role) result]` / `FAILED]`. `asyncio.TimeoutError` and generic exceptions become their own synthetic outputs rather than killing the turn.

***

## 15. `POST /api/chat/stop`

Four-stage resolution:

1. `_prov == "qwen_scraper"` → `connectors.qwen_scraper.client.cancel_active_streams()`
2. No `response_id` from frontend → fall back to **`_ACTIVE_RESPONSE_IDS[chat_id]`**, captured server-side when the `response_id` event fired
3. Still none → `{"success": False, "error": "response_id not available yet"}`
4. Otherwise resolve **upstream** id via `get_upstream_session_id(chat_id) or chat_id`, then:
   - scraper enabled → `scraper_service._engine.stop_generation(chat_id=upstream_id, response_id=...)`
   - API mode → `service._stop_upstream_generation(upstream_id, response_id)`

> [!WARNING]
> Qwen's stop endpoint **requires `response_id`** — this is the whole reason `_ACTIVE_RESPONSE_IDS` exists. Stop pressed before the first chunk arrives has no response id at the frontend.

***

## 16. Auto-switch context builder (summarizer)

Constants:

| Constant | Value | Meaning |
|:--|--:|:--|
| `_SUMMARIZER_CHUNK_LIMIT` | 300,000 | max chars per summarizer call |
| `_TAIL_PRESERVE_LIMIT` | 250,000 | max chars kept verbatim at the end |
| `_TAIL_MIN_CHARS` | 100,000 | min chars preserved from the end |
| `_SUMMARIZER_MIN_HEAD` | 5,000 | skip summarization if head is smaller |
| `_SUMMARIZER_MAX_CONCURRENCY` | 3 | parallel summarizer calls (`_summarizer_semaphore`) |
| `_SUMMARIZER_CACHE_TTL` | 300 s | per-chat summary cache |
| `_HEAD_TOOL_CALLS` | 3 | first N tool call/result pairs kept verbatim |
| `_TAIL_TOOL_CALLS` | 5 | last N pairs kept verbatim |

`_extract_head_tail(messages) -> (head_text, tail_text, middle_messages)`:

- **Head:** first user message + first **3** tool call/result pairs.
- **Tail:** last user message + last **5** pairs, then expanded backwards until `_TAIL_MIN_CHARS`.
- **Middle:** everything between, sent to the summarizer.

Tool detection is regex on content, not role: `_tool_call_re = /<\s*tool_calls?[\s>]/i`, `_tool_result_re = /<tool_result[\s>]/i`. `role == "tool"` also counts.

Guard: `if tail_start_idx < head_end_idx: tail_start_idx = head_end_idx` — head and tail never overlap.

> [!NOTE]
> Head and tail are preserved **verbatim**, only the middle is summarized. That's the right call for agent chats: the original task and the most recent work are what the model must not lose.

***

## 17. Approval endpoints

All four share the shape: consume pending → optionally cache session permission → re-run `engine.process_tag` with an extra attr → `build_tool_feedback` → `append_skill_event` → return `{ok, feedback}`.

| Endpoint | Extra attr | Failure text |
|:--|:--|:--|
| `/api/skills/approve/{tag_id}` | `approved="true"` | — |
| `/api/skills/deny/{tag_id}` | *(does not execute)* | `FAILED — User denied this command: <preview[:200]>` |
| `/api/skills/cwd-approve/{tag_id}` | `cwd_approved="true"` | — |
| `/api/skills/cwd-deny/{tag_id}` | *(does not execute)* | `FAILED — User denied file operation outside project: <path[:200]>` |

- `approve`/`cwd-approve` cache session permission **before** consuming, so subsequent tools in that chat skip the prompt.
- Execution goes through `loop.run_in_executor(None, _run)` — `process_tag` is sync, the route is async.
- `skill_start` is prepended if the handler didn't emit one, so feedback formatting never breaks.
- Deny paths still `append_skill_event` both `skill_start` and `skill_end`, so the denial is visible in history.
- Missing/expired pending → `{"ok": False, "error": "Not found or expired"}`.

### 17.1 `batch_complete`

Merges resolved approvals into one model-facing message:

```
[System: All N pending tool(s) have been resolved by the user. Here are the combined results:]

--- Tool 1/N [APPROVED] ---
<feedback>
...
[System: Continue based on ALL results above.]
```

> [!TIP]
> This exists because fragmented per-tool feedback made the model re-ask or re-run tools. One combined message fixes that.

***

## 18. Findings / issues

> [!WARNING]
> **1. Session-recovery truncates tool results at 2,000 chars**, inconsistent with the 75,000-char caps elsewhere. A chat that recovers a lost session sees a materially thinner history than one that didn't. Documented only.

> [!WARNING]
> **2. `_HISTORY_CACHE` clears entirely at 32 entries.** With many concurrent chats, one new chat can wipe the cache for all others, reintroducing the per-round reconstruction cost. Correctness is fine (key includes row count); performance is spiky. Documented only.

> [!WARNING]
> **3. Memory auto-disables below 8 GB RAM with no user-visible signal.** Users can't tell "no relevant memory" from "memory is off". Documented only.

> [!WARNING]
> **4. `_allowed_mem_cats = {"protected"}` when universal memory is off** — a hardcoded category name string, not a constant. A rename in the memory subsystem silently makes this an empty set. Documented only.

> [!NOTE]
> **5. `_summarizer_cache` has a TTL constant but no visible eviction sweep in this file** — it's keyed by `chat_id` and checked against `time.time()`. Unbounded in count (one entry per chat ever summarized). Low risk, but worth knowing.

No bugs fixed in this file — everything above is behaviour-by-design or a documented inconsistency.

***

## 19. Cross-references

- [[03-engine-root]] — `service.stream_events`, `_ensure_headers`, `_refresh_headers`
- [[04-engine-skills]] — parser output shape, `process_tag`, `build_tool_feedback`
- [[05-engine-agents]] — `LoopDetector`, `MainChatGuard`, `TurnCapTracker`, `wait_all`
- [[07-engine-config]] — `get_model_config`, `AGENT_CONFIG`
- [[09-memory-search-and-research]] — `get_searcher`, `get_project_searcher`, scoring
- [[11-server]] — `database`, `dependencies.service`, `config`
- [[16-telegram-bot]] — the other consumer of the same agentic loop

> [!IMPORTANT]
> Any change to `round_skill_events` lifetime, the single-`done` rule, or the `recover` path will surface as duplicate events or lost context. Those three are load-bearing and each has a "this used to be broken" comment attached.
