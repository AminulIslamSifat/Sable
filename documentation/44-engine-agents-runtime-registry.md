---
tags: [engine, agents, runtime, escalation]
status: full-read
read: agents/runtime.py (300L), agents/auto_turn.py (352L), agents/registry.py (290L), agents/teacher.py (241L)
---

# 44 — Agent Runtime, Auto-Turn, Registry, Teacher

> The four files doc 05 covered at contract level. 1,183 lines. Together they own spawn, completion signalling, role configuration, and escalation.

| File | Lines | Role |
|:--|--:|:--|
| `runtime.py` | 300 | Spawn, prune, semaphores, circuit breakers, kill |
| `auto_turn.py` | 352 | Turns an agent completion into a normal chat turn |
| `registry.py` | 290 | Role table, browser-account assignment, system-prompt assembly |
| `teacher.py` | 241 | Escalation to a stronger model when an agent is stuck |

Two findings matter: **`_tasks` is never populated**, so `wait()` and `kill()` cannot do what their names say; and **`export_roles()` rebuilds nine system prompts synchronously on every settings GET**.

***

## 1. `runtime.py` — `_tasks` is declared, iterated, and never written

```python
self._agents: dict[str, Agent] = {}
self._tasks: dict[str, asyncio.Task] = {}
```

`spawn()` constructs the `Agent`, persists it, emits `agent_spawned`, and returns. It never creates a task:

```python
self._agents[agent.id] = agent
…
insert_agent_run(…)
await self._emit(chat_id, AgentEvent(type="agent_spawned", …))
return agent
```

Three methods depend on `_tasks`:

```python
async def wait(self, agent_id, timeout=None):
    task = self._tasks.get(agent_id)        # always None
    if task and not task.done(): …          # never runs
    return self._to_result(agent_id)        # returns immediately

async def kill(self, agent_id):
    task = self._tasks.get(agent_id)        # always None
    if task and not task.done(): task.cancel()
```

So:

- **`wait()` and `wait_all()` never wait.** A caller in `collect` mode gets a result object for an agent that is still running — status `RUNNING`, empty summary. `wait_all` gathers an empty list.
- **`kill()` does not cancel the running turn.** It sets `agent.cancelled = True`, which the agent loop checks between tool calls, and marks the row `killed` in the DB. The actual work is driven by the frontend's `_runAgentTurn` (doc 35 §3.1) over `POST /api/chat`, which `kill()` never touches.

That second point is the interesting one, because the frontend *does* cancel correctly — `AgentPanel.stopAgent()` posts to `/api/chat/stop` as well as `/api/agents/{id}/kill` (doc 35). The backend's `kill()` is the weaker half.

Finding #138.

### 1.1 The rest is sound

- `_spawn_lock` serialises the check-then-create in `spawn()`, so `active_count >= _max_agents` is not a TOCTOU.
- Depth is computed from the parent's recorded depth, capped at 3.
- `_prune_stale_agents` runs on every spawn and drops non-running agents older than an hour — the same lifetime discipline as `library_upload._sweep_stale_staging`.
- Per-backend + global semaphores, and five `CircuitBreaker`s wired to the same threshold/reset values as the config panel.
- `update_config` recreates semaphores rather than mutating `_value` — the comment claims this is safe, and it is: a new `Semaphore` has no waiters.

One inherited gap: `_breakers` has no `record_failure` call site in this file. The breakers are constructed and configured but never exercised here — the failure recording must live in the (unread) agent loop. Noted, not filed.

***

## 2. `auto_turn.py` — the design is right and the state machine is careful

The docstring states the contract precisely:

> *If the model is idle → immediately signal the frontend with agent results. If the model is busy → queue results; the active stream's finally block calls drain().*

And it explains *why* it signals the frontend rather than running a turn itself:

> *The frontend receives an `auto_turn_trigger` event … and initiates a normal `POST /api/chat` call, so the response renders identically to a user-initiated message (full skill cards, stop button, markdown, history replay).*

That is the correct architecture — one turn pipeline, not two — and it matches doc 35's `sendAutoTurnMessage` on the frontend.

### 2.1 The busy-flag dance is correct and non-obvious

```python
async with state.lock:
    if state.busy:
        # Another turn started while we were preparing — queue instead
        for line in result_lines:
            state.queue.append({"role": "user", "content": line})
        return
    state.busy = True
```

The re-check under the lock in `_fire_turn` matters: `on_agent_done` already checked `busy` before calling, but a stream can start between that check and the signal. Setting `busy = True` inside the lock before awaiting the signal is what prevents two concurrent auto-turns.

`mark_stream_done` schedules `drain()` via `loop.create_task` and swallows `RuntimeError` when there is no running loop — correct, since it may be called from a sync context.

### 2.2 `get_pending_teacher_prompts` is a queue scan that rewrites the queue

```python
for item in state.queue:
    content = item.get("content", "")
    if content.startswith("[TEACHER ESCALATION REQUEST]"):
        prompts.append(content)
    else:
        remaining_queue.append(item)
state.queue = remaining_queue
```

It filters by string prefix rather than a typed field. It works because the prefix is a module constant used at the one site that enqueues it — but a user message beginning with that literal string would be silently promoted to a teacher escalation and removed from the queue. Finding #140, 🔵.

### 2.3 `_TeacherRequest.future` defaults to `None` on a non-optional type

```python
@dataclass
class _TeacherRequest:
    …
    future: asyncio.Future = field(default=None)  # type: ignore[assignment]
```

The `# type: ignore` is doing real work — the declared type is not optional but the default is `None`. Every construction site passes a real future, so it is harmless; the annotation is just wrong. Cosmetic.

***

## 3. `registry.py` — nine system prompts rebuilt per settings GET

`get_role_config(role)` does this on every call:

```python
from connectors.common.instruction_builder import build_instructions
…
system_prompt = build_instructions(agent_role=role, agent_skills=eff_skills, provider=_provider)
```

`build_instructions` (doc 42) reads `instruction/agents/{role}.md`, `instruction/personal.md`, `output_format.md`, the persona config, the disabled-skills list, the skills registry, the tool schemas, the MCP prompt section and `system/settings.json`. Every one of those is a disk read or a JSON parse.

And the caller loops over every role:

```python
def export_roles() -> dict[str, dict]:
    result = {}
    for name in AGENT_ROLES:
        cfg = get_role_config(name)     # ← full prompt assembly, 9×
        …
```

`export_roles` is called from `GET /api/agents/config` (`routes/agents.py:228`), which the settings panel hits on tab open (doc 35: `AgentSettings.load()`). So **opening the Agents settings tab rebuilds nine complete system prompts synchronously** to populate one read-only field per role.

The docstring's own word is `"Built lazily via shared instruction builder"` — on the `RoleConfig` dataclass field, which reads as though it is cached. It is not; `RoleConfig` is `frozen=True` and constructed fresh each call.

Finding #139. The fix is a memo keyed on `_instruction_version` — a counter that already exists in `instruction_builder.py` and is, per doc 42, incremented nowhere.

### 3.1 `get_next_account` searches backwards on purpose

```python
# Searches from the back (highest number first) to avoid competing with
# main chat auto-switch which searches forward.
```

Two consumers pulling from opposite ends of the same pool so they do not collide. That is a coordination scheme implemented as a convention plus a comment, and it is the kind of thing that breaks silently if someone "tidies" the loop direction. Worth the comment it has.

It also filters three ways before returning — `in_use`, `is_account_exhausted`, `is_account_captcha_blocked` — and normalises `in_use` entries from paths to bare names, since callers pass both.

### 3.2 `_DEFAULT_MODEL_CHAIN` is applied to every role

```python
if not chain:
    chain = list(_DEFAULT_MODEL_CHAIN)  # Never leave a role without fallback
```

A role with no configured chain silently inherits `deepseek-instant` → `gemini-2.5-flash`. The comment explains the intent, but it means a role whose default model is Qwen will fall back to two API models the operator may not have keys for. Noted, not filed.

***

## 4. `teacher.py` — four call paths, one shared prompt

`escalate_to_teacher` routes by backend into `_call_teacher_deepseek`, `_call_teacher_api`, or `_call_teacher_qwen`. All three accumulate `{"type": "answer"}` events and raise on `{"type": "error"}` — consistent with the connector contract.

Two things worth recording:

**The JSON parse degrades rather than fails.**

```python
except json.JSONDecodeError:
    logger.warning("[teacher] Could not parse teacher JSON, using raw response")
    return response[:2000]
```

A teacher that ignores the JSON instruction still gets its prose injected as guidance. That is the right trade for an escalation path — some guidance beats no guidance.

**`MAX_TEACHER_INTERVENTIONS = 2` is declared and never used.**

```
MAX_TEACHER_INTERVENTIONS = 2
```

One hit — the declaration. The cap is enforced elsewhere (the loop's escalation logic, not read here) or not at all. Same shape as doc 43's `SAME_TOOL_FAIL_HALT`. Finding #141, 🔵 — I cannot call it dead without reading the loop, but it is dead **in this file**, and the comment beside it (*"before giving up"*) reads as though it is the enforcement point.

***

## 5. Todo support was removed from the backend and left in the frontend

`assignment.todos` does not exist on the current `TaskAssignment`, and `Agent` has no `todos` attribute. But the backup files show both used to:

```
runtime.py.bak:175   if assignment.todos:
                     agent.todos = AgentTodoList.build_from_list(assignment.todos)
runtime.py.bak:214   "todos": [ … for t in agent.todos.todos ]
agent.py.bak:34      todos: list[TodoItem] = field(default_factory=list)
```

Meanwhile doc 35 found the frontend still fully wired for it: `agents.js` handles a `todo_progress` event, `openForStream(…, todos)` calls `_renderTodos`, and `_renderTodos`/`_clearTodos`/`.ap-todo-widget` are all live. Nothing emits `todo_progress` any more.

So the Agents settings panel shows a Plan widget that can never populate, and the frontend carries ~60 lines for a feature whose backend half was deleted. Finding #142.

***

## 6. Findings

**No 🔴, no 🟠.** Two 🟡 with real impact; the rest is dead code and a stale frontend contract.

### 🟡 Correctness

**#138 — `AgentRuntime._tasks` is never populated.** `spawn()` does not create a task, so `wait()`/`wait_all()` return immediately with a `RUNNING` agent's empty result, and `kill()`'s `task.cancel()` never fires — it only sets `agent.cancelled` and writes `killed` to the DB. The frontend's `stopAgent()` posts `/api/chat/stop` as well, so the user-visible behaviour is right; the backend API is not.

**#139 — `export_roles()` rebuilds nine full system prompts on every `GET /api/agents/config`.** Each `get_role_config` call runs `build_instructions`, which does ~8 disk reads plus a skills-registry build and tool-schema assembly. `routes/agents.py:228` calls it on settings-tab open, to populate one read-only string per role. `RoleConfig` is frozen and rebuilt each call; the `"Built lazily"` docstring reads as though it were cached. A memo keyed on `instruction_builder._instruction_version` (which exists and is never incremented) would fix it.

### 🔵 Dead code / cosmetic

| Where | What |
|:--|:--|
| `auto_turn.get_pending_teacher_prompts` | Filters the queue by a string prefix — a user message starting with `[TEACHER ESCALATION REQUEST]` is promoted and removed (**#140**) |
| `teacher.MAX_TEACHER_INTERVENTIONS` | Declared (=2), never used in this file; the comment reads as the enforcement point (**#141**) |
| Todo support | Backend removed (`assignment.todos`, `AgentTodoList` gone); frontend still handles `todo_progress` and renders a Plan widget that never populates (**#142**) |
| `auto_turn._TeacherRequest.future` | Non-optional annotation with `default=None` + `# type: ignore` |
| `runtime._breakers` | Constructed and configured; no `record_failure`/`can_execute` call site in this file |
| `registry._DEFAULT_MODEL_CHAIN` | Silently applied to any role with no chain — a Qwen role can fall back to two API models |

***

## 7. What's right, and worth copying

1. **`_fire_turn` re-checks `busy` under the lock before signalling.** Closes the race between `on_agent_done`'s check and the signal.
2. **Signal the frontend, don't run the turn.** One chat pipeline means identical rendering, stop button and history replay for agent results.
3. **`get_next_account` searching backwards** while main-chat auto-switch searches forwards — two consumers on one pool, coordinated by direction plus a comment.
4. **Teacher JSON parse degrading to raw text.** An escalation path that returns *some* guidance beats one that returns none.
5. **`_prune_stale_agents` on every spawn.** Agent records do not accumulate unboundedly.
6. **`update_config` recreating semaphores** rather than mutating an internal counter.

***

## 8. What this changes upstream

- **Doc 05** covered these four at contract level and listed `runtime.py`/`registry.py`/`teacher.py` as read. This is the actual read; **#138 and #139 supersede** its framing of the runtime as "manages agent lifecycle".
- **#142 pairs with doc 35 §3.3**: the frontend's todo widget is dead because the backend half was removed, not because the wiring broke.
- `engine/agents/` remaining: `agent.py` (102), `loop.py` (66), `protocol.py` (57), `notifications.py` (50) — 275 lines.
- Python coverage: **~75,750 → ~76,930 / 137,031 = ~56%**. Application Python: **~62%**. Overall: **~40%**.

***

## Cross-references

- [[05-engine-agents]] — contract-level treatment this supersedes for these four files
- [[43-engine-agents-resilience-memory]] — `CircuitBreaker`, constructed here and configured, exercised elsewhere
- [[35-web-agent-ui]] — the frontend half of #142, and `stopAgent()` which cancels correctly at both layers
- [[42-connectors-common-shared-layer]] — `build_instructions`, the per-call cost behind #139
