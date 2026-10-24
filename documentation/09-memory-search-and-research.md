# `engine/memory_search/` + `engine/research/` — Retrieval and Deep Research

> [!INFO] Scope
> **1,686 lines (research) + 1,013 lines (memory_search).** Scoring weights, tokenization, and both engines' method surfaces read directly.

***

# 1. `engine/memory_search/` — Hybrid Memory Retrieval

Five small modules plus a 665-line `MemorySearcher`.

| File | Lines | Role |
|:--|--:|:--|
| `searcher.py` | 665 | `MemorySearcher` — the whole pipeline |
| `tokenize.py` | 108 | Stopwords, path stripping, tokenization |
| `embedder.py` | 100 | Gemini API embedding backend |
| `cache.py` | 68 | `.npz` vector cache |
| `scoring.py` | 35 | **Fusion weights + thresholds** |
| `__init__.py` | 37 | Exports |

***

## `scoring.py` — the numbers, with their justification

```python
DEFAULT_MODEL = "snowflake/snowflake-arctic-embed-xs"
DEFAULT_TOP_K = 5

VECTOR_WEIGHT      = 0.70
KEYWORD_WEIGHT     = 0.05
KEY_TOKEN_WEIGHT   = 0.05
TRIGGER_IDF_WEIGHT = 0.15
SOURCE_QUERY_WEIGHT= 0.05
PROTECTED_BOOST    = 0.15
EPISODIC_DECAY_RATE= 0.95      # score multiplier per day since last access

VECTOR_ONLY_MIN  = 0.50
MIN_QUERY_TOKENS = 3
```

> [!IMPORTANT] The weights were recalibrated from a measured benchmark, and the comment proves it
> *"Hybrid fusion weights (2026-09-26 benchmark recalibrated). Vector-dominant: IDF token-overlap signals normalized query-side and misfired on short queries (a single common token scored 1.0). Embeddings proved far more robust. Measured on the 100-prompt hand-labelled set: retrieval 33%->44%, rejection 50%->60% vs the previous weights (both metrics improved)."*
>
> That is a real before/after on a real labelled set. The keyword weights were cut to near-zero (0.05 each) because they were actively harmful on short queries.

### The two gates

| Gate | Value | Meaning |
|:--|--:|:--|
| `VECTOR_ONLY_MIN` | 0.50 | *"if NO strong textual signal fired, require very high vector score"* |
| `MIN_QUERY_TOKENS` | 3 | *"Below this, the message carries too little intent and noise dominates"* — retrieval doesn't run at all |

> [!TIP] `MIN_QUERY_TOKENS = 3` is a cheap refusal
> "ok" or "thanks" never triggers a vector search. Without it, every trivial message would embed and scan the whole memory store.

`EPISODIC_DECAY_RATE = 0.95` per day means a 14-day-old episodic memory scores at roughly 0.49 of its original weight — half-life is about 13.5 days. Procedural and semantic entries are **not** decayed.

***

## `tokenize.py` — aggressive noise removal

```python
_PATH_RE = re.compile(r"[/~][\w./\-]+")
_DIR_RE  = re.compile(r"\b\w+/\s")
_EXT_RE  = re.compile(r"\b[\w\-]+\.(?:css|json|jsonc|js|ts|py|md|txt|html|bak|toml|yaml|yml|conf|cfg|log|sh|rs|go|c|h|svg|xml)\b")
```

`_strip_paths(text)` removes filesystem paths, directory fragments, and file extensions **before** tokenization.

> [!IMPORTANT] Why paths must be stripped
> Memory entries are full of absolute project paths. Tokenized naively, one path contributes `home`, `sifat`, `hdd`, `projects`, `sable`, `exp`, `engine`, `config`, `py` — nine tokens that match almost any technical query. They'd swamp the real signal.

`_PATH_NOISE` is a supplementary set of tokens that survive stripping but carry no meaning. `_is_expired(entry)` checks `expires_at` for ephemeral entries.

***

## `searcher.py` — `MemorySearcher`

### Singleton via `__new__`

Line 41 overrides `__new__` — the class is a singleton. `get_searcher()` and `get_project_searcher(project_id)` also expose module-level accessors. Per-project searchers exist so a project-scoped chat doesn't retrieve global memories.

### Loading

| Method | Does |
|:--|:--|
| `_ensure_model()` | Lazy-load the fastembed model |
| `_ensure_loaded()` | Load `Memory.json` + `Protected.json` + `Procedural.json` |
| `_load_memory_entries()` | Parse all three files into the internal index |
| `_add_entry(...)` | Append one entry |
| `_populate_from_cache(cached)` | Restore vectors from `.npz` |
| `_save_cache()` / `_load_cache_data()` | `.npz` round-trip |
| `reload_memory()` / `rebuild_cache()` / `clear_cache()` | Invalidation |

> [!NOTE] All three Brain files are indexed
> `Memory.json`, `Protected.json` **and** `Procedural.json`. That resolves the earlier confusion about two procedural stores — search sees both.

### The five scoring components

Each produces a `np.ndarray` of per-entry scores:

| Method | Signal |
|:--|:--|
| `_embed_texts(texts, is_query=)` | Vector similarity (weight 0.70) |
| `_keyword_score(query_tokens, entry_tokens)` | Plain token overlap (0.05) |
| `_key_token_scores(query_tokens, query_lower)` | Match against the entry's **`key`** field (0.05) |
| `_idf_trigger_scores(query_tokens)` | IDF-weighted match against `triggers` (0.15) |
| `_idf_source_query_scores(query_tokens)` | IDF-weighted match against `source_query` (0.05) |

`_build_idf_table()` computes inverse document frequency across all entries, so a trigger word appearing in 60% of memories contributes almost nothing while a rare one contributes a lot.

> [!TIP] `is_query=True` matters for the embedder
> fastembed uses asymmetric retrieval — some models want a different prefix or pooling for queries vs documents. Passing the flag wrong halves retrieval quality silently.

### `search()` and `format_for_prompt()`

`search(...)` at line 444 runs the full pipeline: gate → embed → five scores → weighted fusion → threshold → top-k.

`format_for_prompt(results)` at line 615 renders results into the text block injected into the system prompt. Keeping formatting in the same class means the prompt shape and the scoring are always in sync.

### Episodic access persistence

```python
def _persist_episodic_access(self, keys: list[str], timestamp: str) -> None:
```

Every retrieval writes `last_accessed` back into `Memory.json` for episodic entries — that's what feeds `EPISODIC_DECAY_RATE`. Without it the decay would never progress.

***

## `cache.py` + `embedder.py`

`cache.py` stores vectors in `system/memory_cache_<model>.npz` — `save_cache`, `load_cache_data`, `clear_cache_file`. The filename includes the model name so switching models doesn't reuse incompatible vectors.

`embedder.py` is the **Gemini API** backend (`_GeminiEmbedder`), reading keys from `system/.gemini_api_keys.json`, with `_GEMINI_EMBED_DIM = 768` as the unknown-model fallback.

> [!NOTE] Two embedding paths
> The default is fastembed (`snowflake-arctic-embed-xs`, local). The Gemini backend trades local compute for API latency. `list_available_models()` at line 661 enumerates what's usable.

***

# 2. `engine/research/` — Recursive Deep Research

| File | Lines | Role |
|:--|--:|:--|
| `engine.py` | 974 | `DeepResearcher` — the fractal tree |
| `manager.py` | 550 | `ResearchManager` — background runs, persistence, SSE |
| `llm.py` | 157 | Stateless Qwen completion + JSON extraction |
| `__init__.py` | 5 | *"Odysseus-style LLM-in-the-loop iterative research"* |

***

## `engine.py` — nine prompts define the whole algorithm

The prompts are module-level constants, which makes the algorithm readable at a glance:

| Prompt | Stage |
|:--|:--|
| `EXPAND_PROMPT` | *"decomposing a question into angles"* |
| `SPLIT_PROMPT` | *"deciding whether a research topic needs further decomposition"* |
| `QUERY_PROMPT` | *"planning web searches for a specific research topic"* |
| `EXTRACT_PROMPT` | *"extracting relevant findings from a web page for a research topic"* |
| `SYNTH_PROMPT` | *"synthesizing research on a sub-topic from its parts"* |
| `FINAL_PROMPT` | *"Write a long, detailed, comprehensive research report"* |
| `ALTERNATIVE_URLS_PROMPT` | Recover from failed fetches |
| `SELECT_PAGES_PROMPT` | *"choosing which web pages to read for a research topic"* |
| `_date_context()` | Injects today's date so "latest" means something |

> [!TIP] `_date_context()` is not cosmetic
> Without a date anchor, an LLM asked for "the latest research on X" will confidently cite things from its training cutoff. Injecting today's date makes it search rather than recall.

### The tree

```
add_node(parent, depth, kind, label, status) -> node dict
_new_id() -> 16-hex-char id
_emit_node() / _set_status() / _emit() / _emit_progress()
```

Every node is emitted as an SSE event as it's created and as its status changes — that's what draws the live research tree in the UI.

### The recursion

```
_expand_root()                      -> angle list
  -> _maybe_split(topic, depth)     -> None or child topics
       -> _gen_queries(topic)       -> search queries
            -> _search(queries, topic)
                 -> _select_pages(topic, candidates)
                      -> _fetch_one(url, topic)
                           -> _extract(topic, page) -> findings
                                -> _synth() -> sub-topic summary
  -> FINAL_PROMPT -> report
```

`_is_topic_completed(label)` prevents re-researching a topic already covered — important because `_maybe_split` can produce overlapping children.

### Recovery paths

| Method | Handles |
|:--|:--|
| `_reformulate_queries(topic, original)` | Search returned nothing |
| `_suggest_alternatives(failed_urls, topic)` | Pages failed to fetch |
| `_fetch_one(url, topic)` | Returns `Optional[dict]` — a single dead URL doesn't kill the branch |

> [!IMPORTANT] Two levels of failure tolerance
> A failed *search* reformulates the query. A failed *fetch* is simply skipped. Neither aborts the run — the tree keeps growing on the branches that work.

### Health tracking and budget

```python
def _health_key(model, account) -> str
def _is_healthy(model, account) -> bool
def _record_success(model, account)
def _record_failure(model, account)
def _time_exceeded() -> bool
def _stop() -> bool
def cancel()
```

> [!NOTE] Per-model-per-account health, not per-model
> `_health_key(model, account)` means `qwen3.8-max` on `acc3` and `acc6` are tracked separately. One exhausted account doesn't mark the model unhealthy.

`_dispatch(model, account, ...)` picks a healthy backend. `_complete(prompt, timeout=180, system_prefix)` is the LLM call; `_llm_json(prompt, timeout=120)` expects structured output via `extract_json()`.

### External search

```python
SEARCH_SCRIPT = tools/online_search/scripts/online_search.py
_run_search_script(args, timeout=90) -> (ok, output)
```

The research engine shells out to the search tool rather than importing it — a subprocess boundary that keeps a slow or crashing search from taking down the research run.

`_domain_from_url(url)` dedups by domain so one site can't dominate a sub-topic's findings.

***

## `manager.py` — `ResearchManager`

```python
_SESSION_RE        = re.compile(r"^[A-Za-z0-9-]{1,128}$")
_SESSION_FILE_RE   = re.compile(r"^[0-9a-f]{16}\.json$")
_VALID_INCOMPLETE_STATUSES = {"cancelled", "error", "interrupted", "running"}
```

Two regexes validate session ids on the way in and out — path-traversal defence, since session ids become filenames.

### Lifecycle

| Method | Does |
|:--|:--|
| `start_research(...)` | Spawn the runner task |
| `cancel_research(session_id)` | Cooperative cancel |
| **`resume_research(session_id)`** | **Restart an interrupted run** |
| `get_status` / `get_result` / `public_status` | Read state (public one redacts internals) |
| `list_active(owner)` / `list_incomplete()` | Enumerate |
| `delete_research(session_id)` | Remove state + files |

### Resumability

`_VALID_INCOMPLETE_STATUSES = {cancelled, error, interrupted, running}` is what `list_incomplete()` filters on. A run interrupted by a server restart appears in that list and can be resumed.

`_persist(session_id, entry)` writes `session_id.json`; `_save_markdown(session_id, entry)` writes the final report as markdown. `_trim_nodes(nodes)` collapses the full node dict into a compact list for storage — the live tree is much larger than what needs persisting.

### SSE fan-out

```python
def subscribe(session_id) -> asyncio.Queue
def unsubscribe(session_id, q)
async def _broadcast(session_id, payload)
```

One queue per subscriber. `on_progress(payload)` (defined inside both `start_research` and `resume_research`) calls `_broadcast`. Multiple tabs can watch the same research run.

***

## `llm.py`

```python
async def qwen_complete(...)          # stateless Qwen call
_strip_thinking(text)                 # remove reasoning blocks
def extract_json(text) -> dict | list | None
```

> [!TIP] `_strip_thinking` before `extract_json`
> Reasoning models emit a thinking block before their answer. Parsing JSON from that raw text finds braces inside the reasoning and fails. Stripping first is mandatory.

`extract_json` returns `None` rather than raising — the research engine treats a missing JSON block as a normal branch failure.

***

## Gotchas

> [!WARNING] Five things to know
> 1. **`MIN_QUERY_TOKENS = 3`** — messages shorter than three informative tokens get **zero** memory retrieval. That's by design, not a bug.
> 2. **Paths are stripped before tokenization.** Searching for an absolute path will not match memory entries via those tokens.
> 3. **Episodic memories decay, others don't.** `0.95/day`. A two-week-old episodic memory is worth half.
> 4. **`_health_key` includes the account.** A model isn't unhealthy globally — only on the account that failed.
> 5. **Research sessions are resumable.** After a restart, `list_incomplete()` finds interrupted runs.

> [!NOTE] The benchmark comment is the most valuable thing in `memory_search`
> *"Measured on the 100-prompt hand-labelled set: retrieval 33%->44%, rejection 50%->60%."* If you retune the weights, that labelled set is the thing to re-run. Changing them blind is how the previous weights ended up harming retrieval.
