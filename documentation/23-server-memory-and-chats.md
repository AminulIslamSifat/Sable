# 23 — `server/api/routes/memory.py` (1,233) + `chats.py` (1,196)

> [!IMPORTANT]
> Two routers that look unrelated but share a theme: **derived state that must stay consistent with files on disk.** `memory.py` owns the memory files and consolidation; `chats.py` owns chat/project CRUD, unified search, context passing, and forking.

**Read depth:** both files full, 2,429 lines total.

***

# Part 1 — `memory.py`

## 1. Memory stores

| Store | Path | Shape |
|:--|:--|:--|
| Main | `Memory.json` | `{semantic: [], episodic: [], ephemeral: []}` |
| Procedural | `Procedural.json` | `{procedural: []}` |
| Protected | `Protected.json` | `{protected: []}` |
| Personality | `Brain/user_personality.json` | `{strengths, weaknesses, contradictions, blind_spots, summary}` |
| Personal prefs | `instruction/personal.md` | markdown |
| Search settings | `system/memory_search_settings.json` | `{enabled, top_k, top_memory, top_procedural, top_total, max_prompt_chars, model, model_thresholds}` |

> [!CRITICAL]
> **Procedural was split out of `Memory.json` and the split is only half-enforced.** `update_memory` pops `procedural` before writing main, and `_consolidation_apply` does `existing.pop("procedural", None)`. But `_consolidation_apply` also reads `existing.get("protected", [])` and then **writes `existing` back to `Memory.json`** at the end — so a `protected` key already present in `Memory.json` survives, while new protected entries go to `Protected.json`. Protected can therefore live in two files at once. Not fixed.

Every mutation calls `get_searcher().reload_memory()` so the vector index is never stale relative to the file — except project-scoped writes, which call `reload_project_searcher(proj_id)` instead. That branch is easy to miss.

***

## 2. Artifact sanitisation

Five regexes strip injected junk from stored message content:

| Regex | Removes |
|:--|:--|
| `_MEM_BLOCK_RE` | `[RELEVANT MEMORY CONTEXT]…` up to a blank line |
| `_TS_CTX_RE` | `[2026-09-19 08:32:16 \| cwd: /path]` |
| `_TS_RE` | plain `[timestamp]` |
| `_SWITCH_CTX_RE` | `## First User Message` **and everything after** |
| `_UI_GARBAGE_RE` / `_UI_CLOSE_RE` | sidebar DOM text, runs of `×` |

`sanitize_message_content(content, max_chars=10_000)` applies all five, collapses blank lines, and truncates.

> [!WARNING]
> **`_SWITCH_CTX_RE = re.compile(r'## First User Message[\s\S]*')` deletes everything from that heading onward** — it is not a bounded block strip, it is a truncation. `sanitize_message_content`'s own docstring admits the intent (*"everything from here is recycled garbage"*), and `_build_switch_context` presumably marks the boundary correctly. But anything legitimate that happens to contain the literal string `## First User Message` is silently destroyed. No caller of `sanitize_message_content` is visible in this file — `_format_conversation` re-implements just the memory-block and timestamp strips inline. Likely dead, but it is the kind of dead code that gets reused.

> [!CRITICAL]
> **`_TS_CTX_RE` and `_TS_RE` are both applied**, and `_TS_CTX_RE` is a superset of `_TS_RE` (`(?:\s*\|[^\]]*)?` is optional). Harmless duplication, but it means the second sub can never match anything the first didn't already remove. Cheap dead work on every message.

***

## 3. `_format_conversation` — what the consolidator sees

Per message, in order: `[User]/[Assistant]: content`, then `[Memory used]:` with each `{key, value}`, then per skill event `[Tool call]: name({attrs})` and `[Tool result]: name (ok=…): <result[:500]>`.

Budget is `max_chars=50_000` across the whole block, with `...[N more messages truncated]` as the terminator. Tool results are capped at **500 chars** here — deliberately terse, since consolidation only needs to know *what happened*, not the output.

> [!TIP]
> Including `memory_used` is what lets the consolidator see which memories were actually retrieved — that's how it can decide an entry was useless versus just unused.

***

## 4. Consolidation — two entry points, one apply path

| Endpoint | LLM transport | Prep |
|:--|:--|:--|
| `POST /api/memory/consolidate` | API backends only (`_consolidation_llm_phase`) | `_consolidation_prep` |
| `POST /api/memory/consolidate-scraper` | `scraper_service.stream_events(raw=True)` | `_consolidation_scraper_prep` |

Both converge on `_consolidation_apply(raw_answer, mem_path, proj_id, new_entries)`.

### 4.1 API-only, by design

```python
_CONSOLIDATION_BACKENDS = frozenset({"groq", "gemini", "mistral"})
```

```
# Memory consolidation MUST use API backends only — never scraper/browser mode.
# If the model doesn't resolve to an API backend, returns None immediately.
```

`_try_consolidation_call` resolves three ways: a `_CONSOLIDATION_BACKENDS` connector, the DeepSeek client (with `thinking_mode="fast"`), or `service.chat`. Anything else returns `None` before any network call.

`_consolidation_llm_phase` tries primary then up to **3** fallbacks, accumulating a `["model ✓", "model ✗"]` attempts list that becomes the error detail on total failure. `browser_profiles` is accepted and **ignored** — signature compatibility only.

### 4.2 Prep / apply split for the event loop

```python
prep  = await asyncio.to_thread(_consolidation_prep, chat_id, payload)
...
apply_result = await asyncio.to_thread(_consolidation_apply, raw_answer, _mem_path, _proj_id, new_entries)
```

Everything between is the async LLM call plus merge resolution. Prep does DB reads, settings load, memory filtering, prompt assembly. Apply does dedup, file writes, searcher reload, optional skill creation.

### 4.3 The prompt only sees memories that were actually injected

```python
injected_keys = get_injected_memory_keys(chat_id)
...
matched = [e for e in entries if e.get("key") in injected_keys]
current_memory = json.dumps(filtered_memory) if filtered_memory else "{}"
```

So consolidation reasons about *what the model actually saw*, not the whole memory file. That is the single most important property of this function.

### 4.4 Project-scoped memory

```python
_proj_id = get_chat_project_id(chat_id)
if _proj_id and get_project(_proj_id).get("project_memory_enabled"):
    _mem_path = system/projects/<proj_id>/Memory.json
```

The apply path then reloads the **project** searcher rather than the global one:

```python
if mem_path != _MEMORY_PATH:
    reload_project_searcher(proj_id)
else:
    get_searcher().reload_memory()
```

### 4.5 `_consolidation_apply` — the add/delete mechanics

Two accepted response shapes:

```python
adds = new_entries.get("add") if "add" in new_entries else {
    k: v for k, v in new_entries.items()
    if k in ("semantic", "episodic", "procedural", "protected", "ephemeral")
}
deletes = new_entries.get("delete", []) if "add" in new_entries else []
```

Legacy flat shape (`{semantic: [...], delete: [...]}`) and new nested shape (`{add: {...}, delete: [...]}`) both work. Convoluted but tolerant.

**Protected entries are undeletable:**

```python
protected_keys = {e["key"] for e in existing.get("protected", [])}
protected_keys |= {e["key"] for e in adds.get("protected", [])}
delete_keys = {str(k) for k in deletes if k} - protected_keys
```

Delete sweep covers `semantic`, `episodic`, `ephemeral` in main + `procedural` in its own file. Protected is excluded by set subtraction.

Per-entry field preservation is consistent across all four write paths: `source_query` (str, stripped), `tags` (list, lowercased+stripped), `triggers` (list, stripped). Procedural additionally keeps `trigger` and `keywords`. Ephemeral keeps `expires_at`.

> [!NOTE]
> `dedup_skipped` / `dedup_updated` are hardcoded to `0` with an explicit comment:
> ```
> # Dedup disabled — the LLM already sees existing memory and decides what to add.
> # Post-hoc vector similarity was overriding correct model decisions.
> ```
> So `_dedup_and_resolve_adds` (~110 lines, thresholds 0.88 merge / 0.80 update) is **dead code**, kept for reference. Those two counters in the response are always zero — a frontend reading them sees nothing meaningful.

### 4.6 Optional skill creation

`new_entries["create_skill"]` → `_save_user_skill` → `Brain/skills.json`, deduped by name (update in place if the name exists), stamped with `created` on first insert. Surfaces as `result["skill_created"]`.

***

## 5. Personality assessment — dead and broken

> [!CRITICAL]
> **`_run_personality_assessment` references an undefined name `previous`.** Line ~430:
> ```python
> if previous:
>     try:
>         prev_data = json.loads(previous)
> ```
> `previous` is never assigned in the function — there is no parameter, no local, no global. Reaching that line raises `NameError`, caught by the function's own bare `except Exception: return False`. So the function always returns `False` in practice.
>
> **This is latent, not live:** both call sites are commented out in the same file:
> ```python
> # ponytail: personality assessment disabled — was doubling LLM calls per consolidation
> # asyncio.create_task(_run_personality_assessment(conv_text, model, prep["browser_profiles"]))
> ```
> If anyone re-enables it, it fails silently on every call. The docstring's merge logic (dedup by `trait`/`claimed`/`pattern`) is correct in intent; the missing `previous = _PERSONALITY_PATH.read_text() if _PERSONALITY_PATH.exists() else ""` line is the fix. **Not fixed** — it's dead code and I won't guess at intent.

`GET /api/settings/personality` reads the file and returns `{"personality": data | None}`. No write endpoint — the file is only written by the (disabled) assessment.

***

## 6. Memory search settings

`GET /api/settings/memory-search` returns the persisted config **plus** live searcher state: `model_thresholds`, `current_model`, `current_threshold`, `available_models`.

`POST` patches the file and, for `model` and `model_thresholds`, also pushes into the live searcher:

```python
if model is not None:
    cfg["model"] = str(model); get_searcher().set_model(str(model))
if isinstance(model_thresholds, dict):
    clean = {k: float(v) for ...}   # skips None/""/"auto", swallows bad floats
    cfg["model_thresholds"] = clean; get_searcher().set_thresholds(clean)
```

Numeric clamps: `top_memory/top_procedural/top_total >= 1`, `max_prompt_chars >= 1000`.

`POST /refresh-cache` → `rebuild_cache()` (re-embeds everything) and reports the count. `POST /query` → raw searcher search for the Knowledge Base search mode, `top_k` default **20** (vs 10 everywhere else).

***

## 7. Findings — `memory.py`

> [!CRITICAL]
> **1. `_run_personality_assessment` has an undefined `previous`** → `NameError` on every call, swallowed. §5. **Not fixed.**

> [!WARNING]
> **2. Protected memory can be written to `Memory.json` as well as `Protected.json`.** §1. Existing `protected` in main is preserved and rewritten; only *new* protected entries go to the dedicated file. **Not fixed.**

> [!WARNING]
> **3. `_dedup_and_resolve_adds` is dead code** (~110 lines). §4.5.

> [!WARNING]
> **4. `sanitize_message_content` has no caller in this file** and `_SWITCH_CTX_RE` is an unbounded truncation, not a block strip. §2. **Not fixed.**

> [!NOTE]
> **5. `_TS_CTX_RE` fully subsumes `_TS_RE`.** §2. Redundant sub on every message.

> [!NOTE]
> **6. `dedup_skipped`/`dedup_updated` are always `0`** in the API response. §4.5. A frontend rendering them shows a permanent zero.

> [!NOTE]
> **7. `_consolidation_prep` returns `{"skip": True}` and `{"error": …}` sentinels** rather than raising — the endpoint checks `prep.get("skip")` / `prep.get("error")`. Works, but a `None` prep and a skip prep take different branches and both are checked. Fragile if a fourth sentinel is added.

***

# Part 2 — `chats.py`

## 8. Search scoring — no BM25, deliberately

```
# Simple, reliable scoring: exact substring match + token overlap ratio.
# No external libraries, no IDF/BM25 that breaks on small pre-filtered corpora.
```

`_tokenize` lowercases, strips 120+ stop words and 1-char tokens. `_score_doc`:
- exact substring → **1.0**
- else `matched / len(q_tokens)` where an exact token hit = 1, a prefix/stem hit (`dt.startswith(qt) or qt.startswith(dt)`) = **0.5**

> [!TIP]
> The bidirectional `startswith` is what lets `config` match `configuration` **and** `hypr` match `hyprland`. One-directional would miss half of them.

`_rank_results(query, docs, text_key, top_k=10, min_score=0.3)` filters then sorts descending.

***

## 9. `unified_search` — 8 sources, one shape

```python
sources = ("messages", "skills", "memory", "notes", "todos", "schedules", "research", "agents")
```

`_PER_SECTION = 10`, `_MIN_SCORE = 0.3`.

### 9.1 SQL pre-filter is AND-of-terms, per column OR'd

```python
_like_clauses = " AND ".join(["content LIKE ? COLLATE NOCASE"] * len(_q_terms))
```

For multi-column tables:

```python
def _make_like(columns):
    for col in columns:
        col_parts = " AND ".join([f"{col} LIKE ? COLLATE NOCASE"] * len(_q_terms))
        parts.append(f"({col_parts})")
    return " OR ".join(parts), params
```

So `(title LIKE %a% AND title LIKE %b%) OR (content LIKE %a% AND content LIKE %b%)` — every term must appear **in the same column**, but different terms may live in different columns. Terms need not be adjacent.

> [!WARNING]
> `_like_params` is built once as `tuple(f"%{t}%" for t in _q_terms)` and reused for **both** the messages query (`content` only) and the skill query (`se.event_data` only). Fine — both are single-column. But `_make_like` builds its own params in column order, so a caller that mixed `_like_clauses` with a multi-column query would silently bind the wrong count. No caller does; noting the trap.

### 9.2 Per-source notes

| Source | Pre-filter | Post-filter |
|:--|:--|:--|
| messages | `messages JOIN chats`, `LIMIT 200` | strips memory block + timestamp from content, searches `title + content` |
| skills | `skill_events JOIN messages JOIN chats`, `LIMIT 200` | dedupe by `message_id`; searches `skill_name + summary/result/output + title` |
| memory | vector search first | **fuzzy fallback** if fewer than 10 hits |
| notes | `note_type != 'todo' AND archived = 0`, `LIMIT 100` | |
| todos | `note_type = 'todo' AND archived = 0`, `LIMIT 100` | |
| schedules | `LIMIT 100` | |
| research | filesystem glob `RESEARCH_DIR/*.md` | strips YAML frontmatter, first non-heading paragraph as preview |
| agents | `AGENT_OUTPUT_DIR/*.md` | skips `*_conversation.md` |

> [!TIP]
> The memory fuzzy fallback is the interesting bit — it reaches into `searcher._entries` (private) and scans `"key: value"` strings with `_score_doc`, so a typo that defeats the embedding model still finds the entry:
> ```
> # Fallback: if vector search missed results (e.g. typos), brute-force fuzzy scan
> ```
> It compares against both `mkey` and `mval` and takes `max()`, and skips keys already returned by vector search via `seen_keys`.

Every source is individually `try/except`ed with a `logger.warning` — one broken table costs one section, never the response.

### 9.3 `search_full_content` — the expansion endpoint

Per source: `message` (re-reads and re-strips), `skill` (all events for a message, `ORDER BY seq ASC`), `note`/`todo` (`get_note`), `schedule` (row), `research`/`agent` (`<id>.md`), `memory` → explicit `{"error": "Memory results don't expand"}`.

---

## 10. Chat & project CRUD

| Endpoint | Notes |
|:--|:--|
| `GET /api/chats` | optional `project_id` filter |
| `POST /api/chat/new` | local uuid, upstream session created lazily |
| `GET /api/chats/{id}/messages` | `limit`, `before_id` (infinite scroll), `include_skill_events` |
| `GET /api/chats/{id}/context-breakdown` | per-role chars + cumulative tokens |
| `GET /api/chats/{id}/messages/{mid}/events` | lazy skill events |
| `DELETE /api/chats` / `/{id}` | bulk and single |
| `PATCH /api/chats/{id}` | `{pinned}` only |
| `POST /api/chats/{id}/model-settings` | persist model + thinking mode per tab |
| projects CRUD | `list/create/update/delete`, `+ /instruction`, `/activate`, `/deactivate` |

### 10.1 `new_chat` never blocks on upstream

```
# Always use a local Sable UUID as the chat_id.
# Upstream Qwen session is created lazily on first message send
# (session recovery in /api/chat handles it). This avoids blocking
# chat creation on slow/unreachable upstream servers.
```

Scraper mode (`enabled: true`) uses a `browser-<uuid>` prefix; otherwise plain `uuid4().hex`.

### 10.2 Context accounting

`chat_messages` computes `context_chars` from **three** terms even when paginating:

```
SUM(LENGTH(content)) + SUM(LENGTH(thinking))          -- messages
+ SUM(LENGTH(se.event_data))                          -- skill_events
+ _system_instruction_chars(chat_id)                  -- the prompt itself
```

```
# The system instruction occupies real context too — include it so the
# status-bar ring matches the breakdown popup.
```

`_system_instruction_chars` calls `build_instructions(project_id=…)` and caches by project id with a **60 s TTL** (`_SYS_CHARS_CACHE`), because building the instruction tree reads many files.

> [!WARNING]
> `_system_instruction_chars` mirrors chat.py's logic **by calling `build_instructions` without `provider=` or `layout_mode=`** — so it sizes the default instruction bundle. A chat running on a provider with a different injection matrix (see doc 18's `prompt_injection` toggles) or in `study` layout mode will have a different real system prompt. The ring will be slightly off for those chats. Not fixed.

### 10.3 `context_breakdown`

Returns `{total, system, user, assistant, thinking, tool, prompt_tokens, completion_tokens, usage_tokens}`.

> [!IMPORTANT]
> `prompt_tokens` / `completion_tokens` are `SUM()` over the chat's messages — i.e. the **corrupted last-round-only values** described in doc 17. Chats created before the accumulator fix will show a number far below the true API cost. The field is honest about what's stored, not about what was spent.

`tool` chars are summed in Python from `event_data` strings rather than SQL — *"Sum tool chars directly from event_data"*.

***

## 11. Project activation — process-global state

```python
_pre_project_cwd: str | None = None
```

`projects_activate` does:

```python
_pre_project_cwd = os.getcwd()
if proj.get("path") and Path(proj["path"]).is_dir():
    os.chdir(str(p))
await service.sync_context(project_id=project_id)
```

`projects_deactivate` restores `_pre_project_cwd` (or the Sable root as fallback) and re-syncs with `project_id=None`.

> [!CRITICAL]
> **`os.chdir` is process-global and `_pre_project_cwd` is a module global with no per-session or per-chat scoping.** Two clients (or one client in two tabs) activating different projects interleave:
> - A activates P1 → `_pre_project_cwd = /home/sifat`
> - B activates P2 → `_pre_project_cwd = /P1`
> - A deactivates → cwd becomes `/P1`, not `/home/sifat`
>
> Worse, `os.chdir` affects **every concurrent request** — a chat turn running in another coroutine sees the changed CWD mid-flight, and `request.cwd` (the per-request value from `chat.py`) is a separate mechanism that does not follow this. Two CWD concepts in one codebase.
>
> Correct fix is a per-project CWD resolved at request time (which `chat.py` already does via `request.cwd`) and dropping `os.chdir` entirely. **Not fixed** — it's a design decision about whether "active project" is a global app mode or a per-chat attribute.

> [!NOTE]
> `projects_activate` does not verify the project is not already active, so activating the same project twice sets `_pre_project_cwd` to its own path. Deactivate then restores into the project dir — the "revert" becomes a no-op that looks like a success.

***

## 12. `context_pass` — head/tail compression

`POST /api/context/pass` with `{chat_id, model}`. Extracts head/tail from `get_messages(req.chat_id)`, summarizes only the middle, reassembles.

| Constant | Value |
|:--|--:|
| head tool pairs | 3 (6 entries) |
| tail tool pairs | 5 (10 entries) |
| `_TAIL_MIN_CHARS` | 100,000 |
| `_TAIL_PRESERVE_LIMIT` | 250,000 |
| middle transcript cap | 60,000 |
| per-message cap | 3,000 |

Output structure:

```
## First User Message
<verbatim>
## Early Tool Calls
<…>
## Conversation Flow
<summarizer output>
## Last User Message
<verbatim>
## Recent Tool Calls
<…>
continue
```

> [!CRITICAL]
> **This re-implements `_extract_head_tail` from `chat.py` (doc 17 §16) with different constants** — 3/5 pairs here vs `_HEAD_TOOL_CALLS=3` / `_TAIL_TOOL_CALLS=5` there (same), but here `_TAIL_MIN_CHARS=100_000` / `_TAIL_PRESERVE_LIMIT=250_000` are **local literals**, while chat.py has them as module constants. Two copies of the same algorithm and the same magic numbers, in two files, with no shared helper. Any tuning of one silently diverges from the other. **Not fixed.**

### 12.1 The summarizer prompt

Heavily specified. Key constraints, verbatim:

- `"DO NOT reproduce"` head/tail
- Output **only** `## user` / `## model` turn pairs, plus optional `## Intermediary Summary` when >5 tool calls
- `"NEVER add high-level summaries, synthesis, overview, or commentary"`
- `"Preserve code, paths, commands, errors VERBATIM"`
- **Bug-fix reporting is mandatory**: when a bug was fixed, the model turn must include `File: path:L##`, `PROBLEM: <code>`, `FIX: <code>`; unresolved issues must not be mentioned at all

> [!TIP]
> The explicit `## First User Message` / `## Early Tool Calls` / … negative list exists because the summarizer was reproducing the sections it was given. Naming the forbidden headers is what stopped it.

### 12.2 Three-tier model fallback

1. `summarizer_model` from `system/context_pass_settings.json`, else `req.model`
2. `fallback_models[:2]`
3. **Browser pool** — `get_available_accounts_reverse(limit=5)`, each spinning a temporary `ChatService(user_data_dir=acc_dir)` and closing it in `finally`

> [!WARNING]
> Tier 3 constructs a full `ChatService` per attempt. Each one launches a browser, syncs context, and shuts down. Five failed attempts is five browser lifecycles — minutes of latency and possible slot exhaustion. It is the last resort so it rarely runs, but a wedged primary + dead fallbacks makes this the slowest possible path.

Note tier 1/2 go through `_resolve_api_backend` → connector, or `service.chat` when there's no API backend. Only tier 3 uses the explicit browser pool.

***

## 13. `fork_chat` — exclusive, not inclusive

```python
"""Fork a chat from a specific message.

Creates a new chat containing all messages up to and including the specified
message_id.
"""
```

But the query is:

```sql
SELECT ... FROM messages WHERE chat_id = ? AND id < ? ORDER BY id ASC
```

> [!CRITICAL]
> **Docstring says "up to and including"; the SQL is `id < ?` — exclusive.** The forked chat does **not** contain the fork message. The endpoint still returns `fork_message: <content>` (read separately) so the client can pre-fill the input box — which suggests exclusive is the *intended* behaviour ("branch before this message and re-ask") and the docstring is wrong. But anything relying on the docstring will be surprised. **Not fixed** — the behaviour is plausible; the doc is what's wrong.

### 13.1 Provider-gated history injection

```python
_INJECTION_PROVIDERS = {"qwen", "deepseek", "mimo"}
needs_injection = source_provider in _INJECTION_PROVIDERS
```

```
# Qwen: server-side session (upstream_session_id)
# DeepSeek: client-side chat_session_id, chained via parent_message_id
# MiMo: server-side conversationId (stateful), requires history injection on fork
```

When injection is needed, the pre-fork history (including tool calls and results, results truncated to **2,000 chars**) is serialised into `[PREVIOUS CONVERSATION]…[END PREVIOUS CONVERSATION]` and written to `chats.fork_history`. `chat.py` reads and prepends it on the first turn, and **nulls it only after a fully successful turn** (doc 17 §5).

> [!IMPORTANT]
> `parent_id` is explicitly **not** copied (`None` in every `add_message`), and skill events **are** copied via `get_skill_events_for_message(row["id"])`. So a fork preserves the tool-call history in the DB but starts a fresh lineage. That pairing is what makes the fork both complete and independently editable.

> [!NOTE]
> The fork result is `{chat_id, message_count, fork_message}` — no provider field. A client that needs to know whether injection happened must call `get_chat_provider` itself.

***

## 14. Findings — `chats.py`

> [!CRITICAL]
> **1. `os.chdir` + module-global `_pre_project_cwd` are not safe under concurrency.** §11. Cross-tab and cross-request CWD corruption; conflicts with `chat.py`'s per-request `request.cwd`. **Not fixed.**

> [!CRITICAL]
> **2. `fork_chat`'s docstring says "inclusive", the SQL is exclusive.** §13. **Not fixed.**

> [!WARNING]
> **3. `context_pass` duplicates `chat.py`'s head/tail extractor** with independently-defined constants. §12. Drift risk.

> [!WARNING]
> **4. `context_breakdown` token totals are the pre-fix (last-round-only) values** for older chats. §10.3.

> [!WARNING]
> **5. `_system_instruction_chars` sizes the default instruction bundle**, ignoring provider injection toggles and `layout_mode`. §10.2. The context ring under-reports for those chats.

> [!WARNING]
> **6. `context_pass` tier-3 fallback constructs a `ChatService` (browser lifecycle) per attempt**, up to 5. §12.2.

> [!NOTE]
> **7. `projects_activate` doesn't guard against re-activating the active project**, making `_pre_project_cwd` self-referential and deactivate a silent no-op. §11.

> [!NOTE]
> **8. `_score_doc` can return exactly 0.5 for a query whose every token only prefix-matches.** Fine mathematically (0.5·n/n), but a document that matches every term loosely scores the same as one that matches half exactly. Acceptable for a simple scorer; noting it because `_MIN_SCORE = 0.3` means a single loose prefix hit (0.5/1) passes the threshold.

No code changed in either file.

***

## 15. Cross-references

### memory.py
- [[09-memory-search-and-research]] — `get_searcher`, `reload_memory`, `reload_project_searcher`, `_embed_texts`, `list_available_models`, thresholds
- [[05-engine-agents]] — `_resolve_candidates_against_memory`, `_log_consolidation`
- **`instruction/mem_cmd.py`** — `_CONSOLIDATE_PROMPT_TEMPLATE_STANDALONE`, `_CONSOLIDATE_PROMPT_TEMPLATE_HISTORY`, `_MERGE_RESOLUTION_PROMPT`, `_PERSONALITY_ASSESSMENT_TEMPLATE`
- [[11-server]] — `_MEMORY_PATH`, `_PROTECTED_PATH`, `_PROCEDURAL_PATH`, `_PERSONALITY_PATH`, `_PERSONAL_PATH`, `_MEMORY_SEARCH_SETTINGS`
- [[17-server-chat-route]] — `get_injected_memory_keys`, `save_injected_memory_keys`, the injection flow this file consumes
- [[18-server-settings-route]] — writes the same `consolidation_settings.json` via `/api/settings/consolidation`

### chats.py
- [[17-server-chat-route]] — `fork_history` consumption, `request.cwd`, the summarizer this file re-implements
- [[18-server-settings-route]] — `context_pass_settings.json` is written by `/api/settings/context-pass`
- [[22-server-study-routes]] — `study-<docId>-<thread>` chat ids are listed here by `list_chats` and consumed by the study graph
- [[11-server]] — `server.database.*` (`add_message`, `search_messages`, `get_skill_events_for_message`, projects), `server.models`
- **`engine/service.py`** — `ChatService(user_data_dir=…)` for the browser-pool fallback, `sync_context`

> [!IMPORTANT]
> Both files follow the same rule and it is the one to copy: **read from disk, mutate, then explicitly reload whatever cache read it** (`reload_memory` / `reload_project_searcher` / `_SYS_CHARS_CACHE` invalidation). Every staleness bug in this area comes from skipping the reload step.
