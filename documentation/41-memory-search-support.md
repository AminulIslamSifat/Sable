---
tags: [engine, memory, embedding, async]
status: full-read
read: tokenize.py (108L), cache.py (68L), embedder.py (100L), scoring.py (35L), __init__.py (37L)
---

# 41 — Memory Search: Tokenizer, Cache, Embedder, Weights

> The four support modules behind doc 38's `searcher.py`. 348 lines, and the one that matters is `embedder.py` — because a **synchronous `httpx.post` with a 60 s timeout is called from a sync method that runs on the event loop.**

| File | Lines | Role |
|:--|--:|:--|
| `tokenize.py` | 108 | Stopwords, path stripping, `_is_expired` |
| `cache.py` | 68 | `.npz` vector cache, best-effort |
| `embedder.py` | 100 | Gemini API embedding client |
| `scoring.py` | 35 | Thresholds and fusion weights, with benchmark notes |
| `__init__.py` | 37 | Re-exports |

***

## 1. The blocking-call finding

`MemorySearcher.search` is a plain `def` — synchronous. It calls `self._embed_texts([query], is_query=True)`, which for a Gemini model constructs `_GeminiEmbedder` and does:

```python
resp = httpx.post(url, json=payload, params={"key": key}, timeout=60.0)
if resp.status_code != 200:
    key = self._next_key()
    resp = httpx.post(url, json=payload, params={"key": key}, timeout=60.0)
```

`httpx.post` — the module-level convenience function, which creates a client and blocks. Worst case two 60 s timeouts **per search call**.

And the callers:

```
server/api/routes/chat.py:1043      _mem_results = _searcher.search(...)
server/api/routes/chat.py:1074      _proj_results = _proj_searcher.search(...)
server/api/routes/chat.py:3811      for r in _searcher.search(...)
server/api/routes/chats.py:243      for mr in searcher.search(...)
server/api/routes/memory.py:359     results = searcher.search(query, top_k=top_k)
```

```
$ grep -c 'to_thread' server/api/routes/chat.py
0
```

So on a chat turn with a Gemini model selected, the retrieval happens **on the event loop**, and a slow or rate-limited Gemini call blocks every other request in the process for up to two minutes. With the default FastEmbed model there is no network call, but the embedding itself is still CPU work on the loop.

This is README convention #4 — *`asyncio.to_thread` for every blocking call* — and it is the one convention `search()` cannot satisfy as written, because it is synchronous and its callers are not.

> [!WARNING] Finding #126 — sync network call on the event loop
> Not theoretical: `httpx.post(..., timeout=60.0)` twice, reachable from the main chat path, no `to_thread` anywhere in `chat.py`. The fix is either `await asyncio.to_thread(_searcher.search, ...)` at the call sites, or making `_embed_batch_httpx` async and threading the change through. The call-site fix is one line each and does not touch the searcher.

***

## 2. `cache.py` — `allow_pickle=True` on a file built from memory content

```python
data = np.load(cache_path, allow_pickle=True)
return {
    "vectors": data["vectors"],
    "entries": list(data["entries"]),
    "meta": json.loads(str(data["meta"])),
}
```

`entries` is saved as `np.array(entries, dtype=object)` — an object array of memory strings — and loaded back with `allow_pickle=True`. Unpickling an object array executes the pickle. The file lives at `system/memory_cache_<model>.npz`, written by `save_cache` and never verified.

The threat model is thin — an attacker who can write `system/` can already write `Brain/Memory.json` and get their text into a prompt. But the codebase has a **stronger** posture elsewhere (`library_ingest` uses `json`, `library_sessions` uses `json`, `library_upload` uses `json`), so this one `.npz` is the outlier. Finding #127, 🟡.

The rest of the module is exemplary for what it is:

- `save_cache` wraps everything in `try/except Exception: pass` with the comment *"cache is best-effort; never break search over it."*
- `load_cache_data` returns `None` on any failure, and the caller falls back to a full embed.
- `_cache_path_for` slugs the model name so switching models does not invalidate another model's cache.
- `clear_cache_file` swallows `OSError`.

That is exactly the right contract for a cache: every failure is a miss, never an error.

***

## 3. `embedder.py` — key rotation without a health check

```python
def _next_key(self) -> str:
    key = self._keys[self._key_idx % len(self._keys)]
    self._key_idx += 1
    return key
```

Round-robin on every call, and one retry with the next key on a non-200. There is no failure counting — unlike `research/engine._complete`, which has a three-strike circuit breaker per `model:account`. So a revoked key costs one wasted request **every time** it comes up in rotation, forever.

Two smaller items:

- `import httpx` appears twice inside methods (lines 56 and 65) — the lazy-import habit, applied inconsistently.
- `_GEMINI_DIMS` hardcodes two model names with native dimensions, defaulting to 768 for anything else. A new Gemini embedding model with a different native size gets 768 whether it supports that dimensionality or not.

### 3.1 The keys file is world-readable

```
$ ls -la system/.gemini_api_keys.json
-rw-r--r-- 1 sifat sifat 432  …
```

`0644` — readable by any local user. Same class as ledger #14 (`Brain/Protected.json` holds the Google password in plaintext). Finding #128, 🟡.

***

## 4. `tokenize.py` — the stopword list is doing real work

The docstring is one line, but the list is unusually deliberate. Beyond the standard English stopwords it carries three labelled groups:

```python
# Casual insults / filler — high frequency in user prompts, zero retrieval value
"fuck","fucking","fucked","shit",…,"motherfucker",…,"asshole","bitch",
"ok","okay","hey","hi","hello","please","thanks",…

# Vague/filler words with zero retrieval signal
"actual","something","anything","everything","nothing",…,"lol","lmao",
"help","sure","really","maybe","ever","never","always","much","many",
```

That is a stopword list tuned against **real prompts**, not a copy of the NLTK default. `MIN_QUERY_TOKENS = 3` in `scoring.py` only works if short filler-heavy messages tokenise to nothing — and these lists are what make that true.

`_strip_paths` is the other piece of real work:

```
/home/user/Projects/odysseus → "odysseus"
/home/user/.config/waybar/style.css → "waybar"
```

Paths are replaced by their last two *meaningful* components, with a `_PATH_NOISE` set (`home`, `usr`, `local`, `src`, `node_modules`, …) filtered out. The comment says why: *"tool calls pass raw paths as content"* — i.e. without this, every file-tool turn would retrieve memory entries by matching `/home/user/`.

`_is_expired` handles tz-aware and naive `expires_at` by stripping tzinfo before comparing to `datetime.now()`. Comparing aware to naive raises `TypeError`; this avoids it by normalising both to naive local time. Correct for a single-machine deployment, subtly wrong across timezones — worth a note, not a finding.

***

## 5. `scoring.py` — weights with their benchmark attached

```python
# Hybrid fusion weights (2026-09-26 benchmark recalibrated).
# Vector-dominant: IDF token-overlap signals normalized query-side and misfired on
# short queries (a single common token scored 1.0). Embeddings proved far more
# robust. Measured on the 100-prompt hand-labelled set: retrieval 33%->44%,
# rejection 50%->60% vs the previous weights (both metrics improved).
VECTOR_WEIGHT = 0.70
KEYWORD_WEIGHT = 0.05
KEY_TOKEN_WEIGHT = 0.05
TRIGGER_IDF_WEIGHT = 0.15
SOURCE_QUERY_WEIGHT = 0.05
```

Every weight sums to 1.00 and the comment names the benchmark, the date, the two metrics, and the *reason* the previous weights failed (query-side normalisation gave a single common token a score of 1.0). `MODEL_THRESHOLDS` carries the same treatment — *"garbage ceiling ~0.26, true-match floor ~0.40"* — which is what makes 0.30 a defensible number rather than a guess.

This is the second file in the batch (with `cover_html_elegant.py`) where a magic number is accompanied by its measurement. It should be the pattern.

***

## 6. `__init__.py` — `_project_searchers` is re-exported, deliberately mutable

```python
from .searcher import (…, _project_searchers)
```

A module-private dict re-exported at package level. Something outside the package mutates it — that is the only reason to export it. It is the same lifetime concern as doc 38's #118: the dict is never pruned. No new finding; recorded because the export is the evidence that outside code depends on it.

***

## 7. Findings

**One 🟡 of real consequence (#126), two security-adjacent 🟡, and nothing 🔴.**

### 🟡 Correctness / security

**#126 — `_GeminiEmbedder._embed_batch_httpx` does a synchronous `httpx.post` (60 s timeout, ×2 on retry) from `MemorySearcher.search`, which is a sync method called inline from async routes.** `chat.py` has zero `to_thread` calls. A chat turn with a Gemini model selected can block the event loop for up to two minutes. Violates convention #4.

**#127 — `cache.py` loads `.npz` with `allow_pickle=True`.** `entries` is saved as an object array of memory strings; unpickling executes. The file is under `system/` and every other store in the codebase is JSON. Low practical risk, inconsistent posture.

**#128 — `system/.gemini_api_keys.json` is mode `0644`.** Readable by any local user. Same class as ledger #14.

### 🔵 Cosmetic

| Where | What |
|:--|:--|
| `embedder._next_key` | Round-robin with no failure counting — a revoked key costs one wasted request every rotation, permanently |
| `embedder` | `import httpx` duplicated inside two methods |
| `embedder._GEMINI_DIMS` | Any unlisted Gemini model gets 768 dims regardless of its native size |
| `tokenize._is_expired` | Strips tzinfo and compares to local `now()` — correct on one machine, wrong across timezones |
| `tokenize._PATH_NOISE` | `"lib"` appears twice in the frozenset literal |
| `tokenize` | Stopword list contains `"wtf"` twice |

***

## 8. What's right, and worth copying

1. **`scoring.py` — every weight with its benchmark, date, and the failure of the previous value.** The best-justified constants in the codebase.
2. **The cache contract: every failure is a miss.** `save` swallows, `load` returns `None`, the caller re-embeds. A cache that can break search is worse than no cache.
3. **A stopword list tuned against real prompts, with the groups labelled.** `MIN_QUERY_TOKENS = 3` depends on it and the comments make the dependency visible.
4. **`_strip_paths` collapsing a path to its meaningful tail.** Prevents every file-tool turn from matching memory on `/home/user/`.
5. **Per-model cache filenames.** Switching embedding models does not destroy the previous model's cache.

***

## 9. What this changes upstream

- `engine/memory_search/` is now **read in full**: `searcher.py` (665, doc 38), `scoring.py` (doc 09, re-read here), `tokenize.py`, `cache.py`, `embedder.py`, `__init__.py` (348, this doc).
- **#126 is the most actionable finding in docs 38–41.** One `await asyncio.to_thread(...)` per call site in `chat.py` removes a two-minute event-loop stall from the main chat path.
- Python coverage: **~71,400 → ~71,750 / 137,031 = ~52%**. Application Python: **~58%**. Overall: **~38%**.

***

## Cross-references

- [[38-research-manager-study-store-memory-searcher]] — `searcher.py`, the sync method this doc's `embedder.py` blocks under (#116, #118)
- [[09-memory-search-and-research]] — first read of `scoring.py`; this doc re-reads it with the weight rationale
- [[17-server-chat-route]] — `chat.py`, the async route with zero `to_thread` calls (#126)
- [[37-engine-research-engine]] — `_complete`'s circuit breaker, the health-tracking `_next_key` lacks
