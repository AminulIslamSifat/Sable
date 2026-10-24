---
tags: [engine, research, study, memory, persistence]
status: full-read
read: research/manager.py (550L), study/store.py (619L), memory_search/searcher.py (665L)
---

# 38 — Research Manager, Study Store, Memory Searcher

> The three files at the top of the priority list: what drives `DeepResearcher`, where study documents and notes actually live, and the retrieval path memory questions go through. 1,834 lines.

| File | Lines | Role |
|:--|--:|:--|
| `engine/research/manager.py` | 550 | Task registry, persistence, SSE fan-out for research runs |
| `engine/study/store.py` | 619 | The only read/write path for docs, highlights, notes |
| `engine/memory_search/searcher.py` | 665 | 5-signal hybrid retrieval over `Brain/Memory.json` |

`store.py` is the best-locked file in the codebase — every mutation goes through `_mutate_meta` under an `exclusive_lock`, and the docstring explains the concurrency it prevents. `searcher.py` is careful about *ranking* and careless about *writing*. `manager.py` has the most consequential bug of the three, and it is one of omission.

***

## 1. `research/manager.py` — the checkpoint that isn't

### 1.1 What it does

`ResearchManager` holds `_tasks: dict[session_id, entry]` and `_subscribers: dict[session_id, set[Queue]]`. `start_research` builds a `DeepResearcher` with an `on_progress` callback that (a) stores counter events as the progress snapshot and (b) fans every event out to SSE queues. The runner task sets `status`/`result`/`finished_at` and persists in `finally`.

Session IDs are `uuid4().hex[:16]` and `_SESSION_FILE_RE` pins that exact shape — so `list_incomplete` cannot pick up OCR dumps or fetch summaries from the same directory. Good defensive detail, and the comment says why.

### 1.2 The resume feature cannot work as designed

`resume_research` loads findings, sources, and nodes, derives `completed_topics` from `nodes where kind=topic and status=done`, and injects all four into a new `DeepResearcher`. That is the right shape.

But look at where `_persist` is called:

| Line | When |
|:--|:--|
| 144 | immediately after spawn (empty state) |
| 162 | on explicit cancel |
| 139 | in the runner's `finally` (final state) |
| 420, 425 | the same three for a resumed run |

**There is no `_persist` on progress.** The only writes are the initial empty snapshot and the terminal one. So for the case resume exists to handle — *the server died mid-run* — the on-disk JSON contains:

```json
{ "status": "running", "findings": [], "sources": [], "nodes": [] }
```

`list_incomplete` correctly downgrades that to `interrupted` and offers it in the UI. `resume_research` then loads it, finds zero findings, zero completed topics, and starts a **fresh** run with the original query. The user clicks "resume" and gets a full re-run from scratch, at full cost.

> [!WARNING] Finding #115 — the resume path restores nothing on a crash
> The four `resume_*` arguments are wired correctly all the way from `manager.py:390` into `DeepResearcher.__init__`. The data never gets there because nothing writes a mid-run checkpoint. This also explains why doc 37's finding #110 (`resume_nodes` unused by the engine) has never been noticed in practice — on a crash there are no nodes to pass.
>
> The fix is one line in `on_progress`: throttle a `_persist` every N seconds or on every `graph_status` transition. The cost is the JSON write; the benefit is that the feature works at all.

### 1.3 `cancel_research` sets `cancelled`, then the runner overwrites it

```python
entry["status"] = "cancelled"          # in cancel_research
task.cancel()
…
async def runner():
    except asyncio.CancelledError:
        entry["status"] = "cancelled"  # fine
    finally:
        self._persist(session_id, entry)
```

Here the two paths agree, so it works. The ordering is worth noting because it is load-bearing: `cancel_research` sets `cancelled` **before** `task.cancel()`, and the runner's `finally` persists whatever the runner last set. If the runner ever gained a non-`CancelledError` exit path it would clobber the cancel — the same shape as ledger #5 in `library_index.run_job`.

### 1.4 Smaller

- `_save_markdown` skips a `-2`, `-3` suffix probe — it appends a fresh file per run, so a repeated query produces `slug.md`, `slug-2.md`, `slug-3.md`. Never cleaned.
- `delete_research` has a `for md in RESEARCH_DIR.glob("*.md"): pass` loop. The comment explains it cannot match markdown to session; the loop is a no-op with a comment where the code should be. Honest, but dead.
- `list_active(owner)` accepts `owner` and ignores it — every running session is returned regardless of owner.
- `public_status` falls back to disk for unknown sessions, so a deleted in-memory entry still reports for 120 s. Fine.

***

## 2. `study/store.py` — the locking is right, and the sessions path is not

### 2.1 The pattern

```python
@contextmanager
def _meta_lock(doc_id): …

def _mutate_meta(doc_id, mutator):
    with _meta_lock(doc_id):
        meta = read_meta(doc_id)
        if meta is None:
            raise FileNotFoundError(…)
        result = mutator(meta)
        _write_meta(doc_id, meta)
        return result
```

Every highlight, bookmark and progress write goes through this. Read-modify-write under a per-document flock, `_write_meta` does `.tmp` → `replace()`. That is README conventions #1, #2 and #3 all satisfied in one function, and the module docstring names the exact race it prevents:

> *so a handler writing a highlight concurrently with a browser PATCH cannot tear the read-modify-write cycle.*

Notes get the same treatment with a single `.notes.lock`. ID validation is `re.fullmatch(r"[0-9a-f]{32}")` before any path is built — `_valid_doc_id` on every entry point.

### 2.2 `_sessions_summary` reads a file nothing else agrees on

```python
path = LIBRARY_DIR / "sessions.json"
…
records = data if isinstance(data, list) else data.get("sessions", [])
```

That path **is** `LIBRARY_SESSIONS_PATH` — they resolve to the same file. But the reader assumes a different shape from the writer:

| | Writes | Reads |
|:--|:--|:--|
| `library_sessions._write` | `{"version": 1, "sessions": [{…, "seconds": 1.5}]}` | — |
| `store._sessions_summary` | — | `data.get("sessions")` → `float(r.get("seconds"))` |

Those agree. What does not agree is the *lock*: `library_sessions.record` takes `exclusive_lock`; `store._sessions_summary` reads bare. Safe only because `_write` uses `replace()`. Same asymmetry doc 36 noted, now on a second consumer.

More importantly, `study_overview()` counts sessions from a file that `library_sessions.record` prunes at 20,000 entries and 400 days. A dashboard that says "1,204 sessions" while the file has been silently truncated is reporting a number it cannot justify. Finding #117, cosmetic.

### 2.3 `replace_note_section` is correct and deserves the comment it doesn't have

```python
for j in range(start + 1, len(lines)):
    m = re.match(r"^(#{1,6})\s+", lines[j])
    if m and len(m.group(1)) <= level:
        end = j
        break
```

Section ends at the next heading of the **same or shallower** level — so `## A` swallows its `### children` but stops at the next `##`. That is the right semantic and it is the kind of thing that is usually wrong. The docstring above it says the rule in prose, which is why it is verifiable.

### 2.4 Smaller

- `_clean_refs` caps at 50 and `_clean_tags` at 30, both deduplicating — but `update_note_refs(mode="add")` merges with `[r for r in clean if r not in existing]`, a dict-membership scan that is O(n²) and compares whole dicts. Fine at 50, wrong if the cap ever moves.
- `read_note_body` returns `""` for a missing file, so a note whose body was deleted is indistinguishable from an empty one. `find_note` still lists it.
- `_write_meta` sets `meta["updated"] = time.time()` on **every** write — including `set_bookmark`, so bookmarking reorders `list_docs()` only if the caller sorts on `updated` (it sorts on `added`). Harmless but misleading.

***

## 3. `memory_search/searcher.py` — good ranking, unguarded writes

### 3.1 Five signals, fused without per-query normalisation

```
scores = TRIGGER_IDF_WEIGHT * trigger_scores
       + SOURCE_QUERY_WEIGHT * sq_scores
       + VECTOR_WEIGHT       * vector_scores
       + KEY_TOKEN_WEIGHT    * key_scores
       + KEYWORD_WEIGHT      * keyword_scores
```

Every signal is already on 0–1, so the weights are absolute rather than relative-to-this-query. Then two gates:

- **Episodic decay** — `scores[i] *= EPISODIC_DECAY_RATE ** days` against `last_accessed`.
- **Vector-only gate** — if an entry has no text signal *and* its cosine is below `VECTOR_ONLY_MIN`, zero it.

The comments here are benchmark-derived rather than theoretical, which is rare and worth preserving:

> *naive context prepending was measured to HURT retrieval on the 100-prompt benchmark (28% -> 22%): prior turns are often noise and dilute the query.*

and

> *Below MIN_QUERY_TOKENS the message carries too little standalone intent … Measured to improve BOTH retrieval and rejection on the 100-prompt benchmark.*

So `context` is accepted for API compatibility and used **only** when the query itself tokenises to nothing. That is a deliberate non-feature, documented at the site, and the benchmark number is the justification. Good.

### 3.2 `_persist_episodic_access` writes `Memory.json` with no lock

```python
data = json.loads(self._memory_path.read_text(...))
…
self._memory_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), ...)
```

Called from `search()` on every retrieval that touches an episodic entry. It is a full read-modify-write of the shared memory file, with **no `exclusive_lock` and no `.tmp` → `replace()`**. It sits behind `self._load_lock`, which is a `threading.RLock` guarding *this instance* — it does nothing against `server/api/routes/memory.py`, which writes the same file from another path:

```
$ grep -c exclusive_lock server/api/routes/memory.py
0
$ grep -c write_text server/api/routes/memory.py
15
```

Fifteen bare `write_text` calls to `Memory.json`, `Protected.json` and `Procedural.json`, zero locks. So a memory search that updates `last_accessed` can interleave with a settings route that rewrites `Memory.json`, and whichever writes last wins with a **partial** view. Finding #116.

This is the same class as ledger #13 / #105 (correct pattern exists, one path skips it) and the same class as convention #3, which explicitly says `exclusive_lock` for any read-modify-write on a shared JSON store. `study/store.py` in this same batch does it correctly for a different store.

### 3.3 The incremental cache path has an ordering bug

```python
if cached["entries"] == self._entries:
    self._populate_from_cache(cached)
    return
…
reuse_vectors[i] = cached["vectors"][cache_index[text]]   # matched by TEXT
```

Vectors are reused by matching entry **text**, not position. That is correct for an append, and it is also correct for a delete in the middle — which is more than a positional scheme would manage. But `self._entry_tokens` is rebuilt wholesale from `self._entries` while `_normed_vectors` is assembled from a mix of cached and fresh rows; if `_embed_texts` ever returned rows in a different order than `new_texts` (it does not today), the tokens and the vectors would silently desynchronise. Latent, not a bug. Worth a comment.

### 3.4 `get_project_searcher` deliberately breaks the singleton

```python
searcher = object.__new__(MemorySearcher)
searcher._initialized = False
searcher.__init__(memory_path=mem_path, …)
```

`object.__new__` bypasses the `__new__` singleton override, then resets `_initialized` so `__init__` runs. It works and the comment says what it is doing. Two things follow:

- `_project_searchers` is never pruned. One searcher (with a model, vectors, token sets) per project, forever, for the process lifetime. Same shape as ledger #23 (`library_index._doc_locks`).
- `reload_project_searcher` resets `_normed_vectors` and `_entries` but **not** `_entry_meta`, `_entry_tokens`, `_trigger_tokens`, `_source_query_tokens`, or `_idf_table`. The next `_ensure_loaded` calls `_load_memory_entries`, which resets all of those — so it is correct today by accident. Compare `reload_memory()` (the non-project version), which clears all seven fields explicitly. Finding #118.

***

## 4. Findings

**No 🔴.** But #115 is the highest-value 🟡 in this batch by a wide margin — it makes a shipped feature inert, and it explains why a second bug in the same feature has never been reported.

### 🟡 Correctness / data integrity

**#115 — `ResearchManager` never writes a mid-run checkpoint.** `_persist` is called only at spawn and at terminal state. A server crash leaves `{status: running, findings: [], sources: [], nodes: []}`, so `resume_research` starts a **fresh** run with the original query and restores nothing. The entire four-argument resume plumbing (manager → engine) is correct and unreachable. Also explains why doc 37's #110 (`resume_nodes` ignored by the engine) has gone unnoticed.

**#116 — `MemorySearcher._persist_episodic_access` writes `Memory.json` unguarded.** No `exclusive_lock`, no `.tmp`/`replace()`. `server/api/routes/memory.py` writes the same file from 15 bare `write_text` calls with zero locks. A memory search updating `last_accessed` can interleave with a settings write and lose one of them. Violates README convention #3 — and `study/store.py`, read in this same batch, does it correctly for a different store.

**#117 — `study_overview` counts sessions from a pruned file.** `library_sessions` truncates to 20,000 entries / 400 days; `store._sessions_summary` reports the count in that file as if it were lifetime. The dashboard number is not what it claims.

**#118 — `reload_project_searcher` clears two of seven fields.** `_entry_meta`, `_entry_tokens`, `_trigger_tokens`, `_source_query_tokens`, `_idf_table` are left stale. Correct only because `_load_memory_entries` resets them on the next load; `reload_memory()` (the non-project path) clears all seven explicitly. Any future change to `_ensure_loaded`'s early-return makes this a live desync.

### 🔵 Cosmetic / latent

| Where | What |
|:--|:--|
| `manager.delete_research` | `for md in RESEARCH_DIR.glob("*.md"): pass` — a no-op loop with a comment where the code should be |
| `manager.list_active` | Accepts `owner`, ignores it — returns every running session |
| `manager._save_markdown` | One new `.md` per run; `slug-2.md`, `slug-3.md` accumulate forever |
| `manager.cancel_research` | Sets `cancelled` before `task.cancel()`; the runner's `finally` persists whatever the runner last set — works, but only because the two paths agree |
| `store._clean_refs` | `update_note_refs(add)` does dict-membership against the whole list — O(n²) at a cap of 50 |
| `store.read_note_body` | Returns `""` for a missing file, indistinguishable from an empty note |
| `searcher._ensure_loaded` | Vectors keyed by text, tokens rebuilt from `_entries` — correct today, no comment saying why |
| `searcher._project_searchers` | Never pruned; one full searcher per project for the process lifetime (cf. ledger #23) |

***

## 5. What's right, and worth copying

1. **`_mutate_meta` — one function, all three conventions.** Per-doc flock, `.tmp` → `replace()`, ID validated before the path is built. Every mutation in `store.py` goes through it.
2. **Benchmark numbers in the comments.** `28% -> 22%` and `MIN_QUERY_TOKENS` are the justification for two deliberate non-features; without them both look like bugs.
3. **Five signals, each normalised before weighting.** No per-query rescaling, so weights mean the same thing every time.
4. **`_SESSION_FILE_RE` pins the ID shape.** Prevents unrelated JSON in the same directory from being read as a session, and the comment names the artifacts.
5. **Section-end detection in `replace_note_section`.** "Next heading of the same or shallower level" is the right rule and is stated in the docstring where it can be checked.
6. **`object.__new__` with a stated reason.** Breaking a singleton deliberately, with a comment, is better than adding a `singleton=False` flag nobody understands.

***

## 6. What this changes upstream

- Doc 37's `research/engine.py` read is now paired with the manager that drives it. **#115 supersedes the framing of #110**: `resume_nodes` being ignored is real, but on a crash there are no nodes to ignore.
- `engine/study/` is no longer unopened — `store.py` read in full. `pdf.py` was read earlier; the package's remaining files are small.
- `engine/memory_search/` is no longer method-level only: `searcher.py` (665) read in full. `tokenize.py`, `scoring.py` (doc 09), `cache.py`, `embedder.py` remain.
- Python coverage: **~67,800 → ~69,600 / 137,031 = ~51%** of all Python (~56% of application Python). Overall: **~39% → ~40%**.

***

## Cross-references

- [[37-engine-research-engine]] — the engine `manager.py` drives; #110 pairs with #115 here
- [[36-library-stack]] — `library_sessions.py`, the writer whose file `store._sessions_summary` reads (#117)
- [[09-memory-search-and-research]] — `scoring.py` and the weight table `searcher.py` consumes
- [[23-server-memory-and-chats]] — `memory.py`, the 15 unguarded `write_text` calls in #116
