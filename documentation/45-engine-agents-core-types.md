---
tags: [engine, agents, sse, notifications]
status: full-read
read: agents/agent.py (102L), agents/loop.py (66L), agents/protocol.py (57L), agents/notifications.py (50L), agents/__init__.py (40L), agents/decomposer.py (26L)
---

# 45 — Agent Core Types, and a Shim on the Wrong Port

> The last files in `engine/agents/`. Together 341 lines. One of them is a deprecated shim that hardcodes a port Sable does not use — and it is the only path the scheduler has.

| File | Lines | Role |
|:--|--:|:--|
| `agent.py` | 102 | The `Agent` dataclass + its SSE replay buffer |
| `loop.py` | 66 | **DEPRECATED** shim — forwards to `/api/chat` over HTTP |
| `protocol.py` | 57 | `AgentStatus`, `AgentResult`, `TaskAssignment`, `AgentEvent` |
| `notifications.py` | 50 | Per-chat completion queue Maria drains at turn start |
| `__init__.py` | 40 | Package re-exports |
| `decomposer.py` | 26 | `needs_decomposition` heuristic |

***

## 1. `loop.py` — the deprecated shim posts to port 8080

```python
"""Agent LLM loop — DEPRECATED.

Subagents now use the main chat pipeline (POST /api/chat) via frontend SSE events.
This module exists only as a compatibility shim for the scheduler.
"""
```

```python
resp = await client.post("http://127.0.0.1:8080/api/chat", json=payload)
```

Sable's default port is **61770** (README, doc 00). The scheduler calls this function; it posts to `127.0.0.1:8080`.

Two consequences:

- If nothing is listening on 8080 — the normal case — the scheduler's agent runs fail with `ConnectError`, are logged as `Agent loop failed`, and `agent.mark_failed(...)` fires. Silent from the user's side; a scheduler job simply does not run.
- If **something else** is on 8080 (a dev server, another framework's default), the request goes to a stranger's `/api/chat` with the agent's full task and system prompt in the body.

There is no config read, no `engine.config` import, no environment lookup — the port is a literal.

> [!WARNING] Finding #143 — hardcoded wrong port on the only scheduler path
> The module is 66 lines and marked deprecated, which is exactly why nobody re-read it. It is still imported by the scheduler and it is the only way an agent runs without a browser attached.

The rest of the shim is reasonable: it resolves `system_prompt` from `agent.system_prompt` then falls back to `get_role_config(agent.role).system_prompt`, appends `agent.instruction` as a prefix, builds the message from `context` + `task`, and forwards `model` and `browser_data_dir`. `timeout` defaults to 600 s. The only defect is the destination.

***

## 2. `agent.py` — the replay buffer's index is not stable

```python
_STREAM_HISTORY_SIZE = 300
…
_stream_history: deque[dict[str, Any]] = field(
    default_factory=lambda: deque(maxlen=_STREAM_HISTORY_SIZE), repr=False
)

def get_stream_history(self, since_index: int = 0) -> list[dict[str, Any]]:
    """Args:
        since_index: client sends last event index it received; we return
                     everything after that point.
    """
    history = list(self._stream_history)
    if since_index <= 0:
        return history
    return history[since_index:]
```

The contract is "client sends the last index it received, we return what's after it". That is an **absolute index** into the event sequence — but it is applied to a `deque(maxlen=300)`, which silently discards from the front once it is full.

Once event 301 arrives, `history[0]` is event 2. A client reconnecting with `since_index=250` gets `list(deque)[250:]` — which is event 252 onward **only if** the deque has never wrapped. After wrapping, index 250 is event 301, and the client silently skips everything between 251 and 300.

There is no wrapping counter, no offset tracking, no `total_pushed` field. The replay is correct for a panel that reconnects within the first 300 events and silently lossy after that.

Finding #144. The fix is a monotonic counter: store `(index, event)` tuples and slice by index, not by position in a rolling buffer.

### 2.1 Everything else is clean

- `push_stream_event` appends to history **and** `put_nowait`s, swallowing `QueueFull` with the comment *"Panel disconnected or too slow — event is still in history"*. Correct: the history is the recovery path.
- `stream_queue` is `maxsize=200`, history is 300 — so a reconnecting panel can recover events that were dropped from the live queue. Deliberate and the sizes match that intent.
- `id` is `{8 hex}-agent-{4 hex}`, which is what the frontend's `#([a-f0-9]{6,8})$` regex in doc 35 §3.5 matches against.
- `cancelled`, `pending_user_messages`, `teacher_interventions`, `model_chain` + `_fallback_index` are all declared fields with a single consumer each — no dead state.

***

## 3. `notifications.py` — correct eviction, unbounded map

```python
def push(self, chat_id, event) -> None:
    q = self._queues.setdefault(chat_id, asyncio.Queue(maxsize=50))
    try:
        q.put_nowait(event)
    except asyncio.QueueFull:
        logging.getLogger("sable").warning(… "dropping oldest" …)
        try:
            q.get_nowait()
            q.put_nowait(event)
        except (asyncio.QueueEmpty, asyncio.QueueFull):
            pass
```

The eviction is right — drop the oldest, keep the newest, and log it. That is the correct policy for a completion queue where the newest event is the most actionable.

`drain()` empties the queue but **never removes the chat's entry from `_queues`**. One `asyncio.Queue` per chat id, for the process lifetime. Same shape as ledger #23 (`library_index._doc_locks`) and #118 (`memory_search._project_searchers`), and it is the third instance of that pattern in this batch of docs. Finding #145, 🔵 — 50 events × N chats is small, but the map is monotonic.

Also worth noting: `import logging` sits **inside** the `except` branch, and the logger is constructed inline rather than module-level. It works; it is just the only place in the file that logs.

***

## 4. `protocol.py` — four dataclasses, no defects

`AgentStatus` has seven members, and `AgentResult.success` correctly treats `DEGRADED` as success. `TaskAssignment` has no `todos` field — confirming doc 44 §5's finding that todo support was removed from the backend while the frontend still renders it.

`AgentEvent.type`'s comment lists four types (`agent_spawned | agent_progress | agent_completed | agent_failed`), but the frontend handles six (doc 35: plus `auto_turn_trigger` and the manual-spawn `agent_start` / `agent_trigger`). The comment is a subset, not a contract. Cosmetic.

***

## 5. `decomposer.py` and `__init__.py`

`decomposer.py` (26 lines) is a single `needs_decomposition` heuristic; `__init__.py` re-exports 14 names from six modules. Both are clean, and the `ContextVar` in `__init__.py` carries the right comment:

```python
# Per-request chat_id — safe across concurrent async tasks (each task gets its own context)
current_chat_id: ContextVar[str | None] = ContextVar("current_chat_id", default=None)
```

That claim is **true** for `ContextVar` (unlike doc 40's #124, where the comment made the same kind of claim about generators and was wrong). Worth noting the contrast: same assertion, one correct, one not.

***

## 6. Findings

**No 🔴.** One 🟡 that stops the scheduler working at all; one 🟡 that silently truncates SSE replay.

### 🟡 Correctness

**#143 — `loop.py` posts to `http://127.0.0.1:8080/api/chat`; Sable runs on 61770.** The deprecated shim is the only path the scheduler uses to run an agent without a browser. Nothing listening on 8080 → every scheduled agent fails with `ConnectError`. Something listening → the task and system prompt go to a stranger's `/api/chat`. No config read, no env lookup, no `engine.config` import.

**#144 — `Agent.get_stream_history(since_index)` treats a rolling deque as an absolute index.** The docstring's contract is "last index received → return what's after"; `_stream_history` is `deque(maxlen=300)` and silently drops from the front. After 300 events, `history[250:]` is not event 252 onward. No monotonic counter, no offset — replay is silently lossy past the buffer size.

### 🔵 Dead code / cosmetic

| Where | What |
|:--|:--|
| `notifications._queues` | Never removes a chat's entry on `drain()` — one `Queue` per chat for the process lifetime (**#145**), third instance of this pattern (cf. #23, #118) |
| `notifications.push` | `import logging` inside the `except` branch; logger built inline |
| `protocol.AgentEvent.type` | Comment lists 4 event types; the frontend handles 6 |
| `loop.py` | The whole module is deprecated-but-live — the class of file that goes unread |

***

## 7. What's right, and worth copying

1. **History is the recovery path, and it is bigger than the live queue.** `maxsize=200` for delivery, `maxlen=300` for replay — so a slow panel loses live events but can still catch up. The `QueueFull` handler says exactly that.
2. **Notification eviction drops the oldest and logs it.** The newest completion is the most actionable one.
3. **`AgentResult.success` counting `DEGRADED`.** A degraded run produced output; treating it as failure would discard work.
4. **`current_chat_id`'s `ContextVar` comment is accurate.** Contrast doc 40's #124 — same claim, wrong there, right here.
5. **A deprecated module that says so in the first line of its docstring.** The port bug is not a documentation failure; the deprecation notice is correct and complete.

***

## 8. What this changes upstream

- **`engine/agents/` is now read in full** — 12 modules, ~4,550 lines: docs 43, 44, 45 plus doc 05's contract-level pass.
- **#143 is a scheduler outage**, not a code smell: it is the only way an agent runs headless.
- **#145 is the third unbounded-map finding in this batch** (#23 `_doc_locks`, #118 `_project_searchers`, #145 `_queues`). Worth a convention: any module-level `dict[str, …]` keyed by request id needs a stated eviction rule.
- Python coverage: **~76,930 → ~77,270 / 137,031 = ~56%**. Application Python: **~63%**. Overall: **~41%**.

***

## Cross-references

- [[44-engine-agents-runtime-registry]] — `spawn()`/`wait()`/`kill()`, the runtime this `Agent` dataclass flows through
- [[43-engine-agents-resilience-memory]] — `LoopDetector`, the guard the deprecated loop no longer runs
- [[35-web-agent-ui]] — the frontend consumer of `push_stream_event` and the `#<8hex>` id format
- [[00-root-and-launchers]] — the port 61770 that #143 gets wrong
