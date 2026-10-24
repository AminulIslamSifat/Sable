# 26 — `engine/library_rag.py` + `library_index` verification

> [!INFO] Why this file exists
> `library_rag.py` was **priority 1** on the README's "push coverage up" list and the only remaining engine file consumed by docs 19 and 22 without ever being opened. It is 352 lines and was read end to end for this document.
>
> The stated reason for reading it was to **confirm or clear** the chunk↔vector misalignment flagged as a 🔴 critical bug in [[19-engine-library-index]] §11. It does not survive: the guard the doc missed is three lines long. That correction is in §7, and the ledger has been revised.
>
> Reading it also required re-reading `library_index.py` (1,184 lines) in full rather than the §11 excerpt, which turned up two findings the original pass missed — §9 and §10.

***

## 1. What it is

The retrieval half of Study AI. `study_ai.py` used to hand the model a **truncated head slice** of a document (`_MAX_DOC_CTX = 60_000` chars), so a long textbook silently lost its tail — the part a student is most likely to be examined on. This module chunks the extracted page text, optionally embeds it, and retrieves only the passages that matter for a given question.

Two retrieval paths, chosen automatically per call:

| Path | When | Cost |
|:--|:--|:--|
| **vector** | a local fastembed model loaded **and** a `.npy` whose row count matches the chunk count | cosine top-k, one matmul |
| **lexical** | otherwise — no model, no vectors, shape mismatch, matmul error, empty query tokens | IDF-weighted token overlap, stdlib only |

The lexical path is not a degraded fallback bolted on for tests. It is the reason retrieval works on a host that never installed fastembed, which is the default state of a fresh install — `fastembed` is an optional extra.

***

## 2. Storage layout

Derived data under `LIBRARY_EMBED_DIR` (= `library/embeddings/`, [[07-engine-config]]):

| File | Contents | Always written? |
|:--|:--|:--|
| `<doc-id>.json` | `{version, doc_id, built, chunks: [{page, text}]}` | yes, when there is at least one chunk |
| `<doc-id>.npy` | `float32` matrix, **one row per chunk** | only when an embedder is available |

Both are rebuildable from `library/text/<doc-id>.json`, so losing them costs time and never truth. The row-count invariant between the two files is the single most important contract in the module — everything in §7 hangs off it.

***

## 3. Chunking — `_chunk_pages`

```python
_CHUNK_CHARS = 1400
_CHUNK_OVERLAP = 200
_MAX_CHUNKS = 4000
```

Per page, in `enumerate(pages, start=1)`:

1. Collapse all whitespace runs to a single space and strip. A page that reduces to `""` is skipped — but its **page number is preserved**, because the number comes from `enumerate`, not from the index into the filtered list. A blank page 2 does not shift page 3's citations.
2. Page shorter than `_CHUNK_CHARS` → one chunk, no splitting.
3. Otherwise slide a 1400-char window in steps of `1400 - 200 = 1200`, so consecutive windows share 200 chars.
4. A window under **80 chars** stops that page — a stub tail is not worth a chunk.
5. At `_MAX_CHUNKS` the whole function returns early.

> [!IMPORTANT] The `break` is scoped to one page
> Step 4's `break` exits the *per-page* `for start in range(...)` loop only. The outer `for number, raw in enumerate(pages)` continues, so a short tail on page 1 cannot suppress page 2. Verified empirically — `pages = ["y"*2401, "SECOND PAGE CONTENT"]` yields 3 chunks spanning pages `{1, 2}`.
>
> Worth stating because the shape of the code (`break` inside a nested loop, `return` for the cap) invites exactly that misreading, and the consequence would be silent truncation of every page after the first short tail.

**Chunks never span a page boundary.** That is deliberate, not incidental: a citation card has to be able to name one page number, and a merged page-1/2 chunk could not.

*Chunk sizing rationale, inferred:* 1400 chars ≈ 350 tokens, comfortably inside every embedder's window and small enough that six of them fit a 60k-char context budget.

***

## 4. The embedder probe — process-global, latched, and cached in the repo

```python
_embedder: Any = None
_embedder_probed = False
_EMBED_MODEL = "snowflake/snowflake-arctic-embed-xs"
```

`_load_embedder()` returns the same object for the life of the process. `_embedder_probed` latches on **first call**, so a failure is never retried — one transient failure at import time pins the process into lexical mode until restart. That is a deliberate trade (probing loads a model from disk), but it means "restart the server" is the only recovery, and nothing logs at a level a user would see (`logger.info`).

> [!WARNING] The model cache does not honour `SABLE_PERSISTENT_ROOT`
> ```python
> _ROOT = Path(__file__).resolve().parent.parent
> cache = _ROOT / "system" / "fastembed_cache"
> ```
> `_ROOT` is the **repo root**, resolved from `__file__`. It ignores the env var that [[07-engine-config]] documents as the relocation mechanism. This is the same defect class as ledger entry #24 (`engine/config.py` hardcoding `_SYSTEM = _ROOT / "system"`), and it is a second instance of it — so the model blob lands in the source tree even when every library path has been moved elsewhere. A read-only or container-mounted source tree turns that into a probe failure, which §4's latch then makes permanent for the process.

The probe is deliberately tolerant: `ImportError` → lexical with an `info` line; model-load failure → lexical with the exception text at `info`. Neither raises. `_write_vectors` catches a third class (embedding the batch itself) at `warning`.

***

## 5. Build / load / remove

```python
build(doc_id, pages) -> {"doc_id", "chunks", "embedded"}
```

- No `doc_id` or no pages → `{chunks: 0, embedded: False}`, and **nothing is written**.
- No chunks after chunking (all pages blank) → same early return, nothing written.
- Otherwise: `.json` written atomically (`.tmp` → `replace()`), then `_write_vectors`.

`_write_vectors` is where the module's one destructive behaviour lives — see §8.

`load()` is defensive in the right way: missing file → `None`; malformed JSON → `None`; valid JSON whose `chunks` is not a list → `None`. The caller in `retrieve()` treats `None` as "unindexed" and returns `[]` without raising.

`has_index()` is a bare `.exists()` on the `.json`. Note this is **metadata presence, not vector presence** — the same imprecision that makes `library_index.has_embeddings()` lie (§8).

`remove()` unlinks both files with `missing_ok=True`, so it is idempotent and is safe to wire into the document-delete path — which is exactly what `study.py:112` does (`_search.remove_document`, `_rag.remove`, `_catalog.remove_doc` as one cleanup tuple).

***

## 6. `retrieve()` — the public contract

```python
retrieve(doc_id, query, k=_DEFAULT_K) -> list[{page, text, score}]
```

- Unindexed doc, or a query that strips to `""` → `[]`.
- `k` clamped to `[1, 40]`: `max(1, min(int(k), 40))`. A caller asking for 500 gets 40, not an error and not 500 chunks.
- Vector scores first; `None` from `_vector_scores` means lexical.
- Sorted by score descending, then **filtered to `score > 0`**. This is why a query whose terms appear nowhere returns `[]` rather than 40 zero-score chunks, which is what lets `context_for` fall back cleanly instead of grounding an answer on noise.
- Scores rounded to 4dp for the wire. Cosine and IDF scores are not on the same scale — nothing downstream may compare a score from one path to a score from the other.

**`_lexical_scores`** in detail, because it is the path that actually runs on a default install:

```python
idf = math.log(1 + total / (1 + doc_freq[term]))
score += (1 + math.log(tf)) * idf
score /= 1.0 + math.log(1 + len(tokens) / 200.0)
```

Sublinear `tf`, smoothed `idf`, and a logarithmic length normaliser — a standard tf-idf shape, chosen so a long chunk cannot win on length alone. Stopwords are a hardcoded frozenset of 40 English words; single-character tokens are dropped. Tokens match `[a-z0-9\u0080-\uffff]+`, so Bangla survives — correct for a library whose primary language is not English.

`_lexical_scores` tokenises **every chunk on every query**. No index, no cache, O(chunks × query-terms) per call. At the 4,000-chunk cap that is fine; it is also why the cap exists.

***

## 7. The correction — divergence is detected, not silent

[[19-engine-library-index]] §11 and ledger #3 both state that a chunk/vector length divergence causes chunk *N* to retrieve vector *N+k* — **silently**. That is wrong, and the guard is three lines:

```python
def _vector_scores(doc_id, query, chunks):
    model = _load_embedder()
    if model is None:
        return None
    ...
    vectors = np.load(path)
    if vectors.shape[0] != len(chunks):
        return None          # <-- the guard
    ...
```

`None` propagates to `retrieve()`, which falls back to `_lexical_scores`. So a divergent pair degrades to lexical retrieval; it does **not** return a passage from the wrong page.

> [!NOTE] Why the doc's reading was reasonable but wrong
> The divergence it describes is real and reachable — `run_ai_pass` does keep `existing.extend(batch)` outside the embedding `try` ([[19-engine-library-index]] §11). What the doc missed is that the consumer of those files validates their row counts before use. The bug exists; the *failure mode* is quality degradation, not misattribution.
>
> Consequence for triage: this drops from 🔴 *silent corruption* to 🟡 *degraded retrieval*, because the wrong-page-passage scenario it predicted cannot occur. It still deserves a fix — a partially embedded document silently loses its vector path and nobody is told.

Proven by `tests/test_library_rag.py::test_retrieve_falls_back_to_lexical_on_misalignment`: a 3-chunk document with a 2-row `.npy`, queried for `photosynthesis`, still cites page 2 — the page that contains the term.

Three other exits from `_vector_scores` return `None` for the same fall-back reason: no `.npy`, a `np.load`/matmul exception, and no model. Every one lands on lexical, so there is no code path where a mismatched vector matrix reaches the user.

***

## 8. The bug that is left — `build()` destroys vectors

```python
def _write_vectors(doc_id, texts) -> bool:
    model = _load_embedder()
    if model is None:
        (_embed_dir() / f"{doc_id}.npy").unlink(missing_ok=True)
        return False
```

When no embedder is available, `build()` **deletes any existing `.npy`**. So re-indexing a document on a host without fastembed silently downgrades a fully embedded document to lexical-only, and reports `embedded: False` in a return value nobody checks.

This is reachable in bulk. `study.py:1287` walks the entire library on `want_rag`:

```python
for path in sorted(LIBRARY_TEXT_DIR.glob("*.json")):
    ...
    _rag.build(path.stem, pages)
    built += 1
```

No embedder on that host, and one click flattens every vector in the library. The embeddings are rebuildable once fastembed returns, so this is recoverable — but it is unrecoverable *silently*, and the route reports `{indexed: N}` with no indication it produced lexical-only metadata.

The asymmetry is the tell: `build()` is defensive about the `.json` (early return, atomic write, never a bare `write_text`) and destructive about the `.npy`. Deleting a derived file is defensible when it is *stale* — the case §11 of doc 19 forces (`ai_ocr` → `do_ai`). It is not defensible when the reason is *"this host cannot make a new one."*

> [!CAUTION] Ledger #4 is the same root cause, seen from the other end
> `library_index.has_embeddings()` returns `(LIBRARY_EMBED_DIR / f"{doc_id}.json").exists()` — it checks the **metadata**, not the vectors. Combined with this `unlink()`, a document can report `has_rag = True` (consumed at `study.py:1466`) while holding zero vectors. One function destroys the vectors, another reports them present, and neither is wrong about its own file.

**Suggested fix, not applied:** drop the `unlink()`; return `False` and leave whatever `.npy` is on disk for the shape guard to reject. If staleness is genuinely the concern, gate it on a reason flag (`rebuild_stale=True`) rather than inferring it from `model is None`.

***

## 9. Context assembly

### 9.1 `context_for` — one document

Returns `(context, pages)`. Budget `max(2_000, max_chars)`; blocks are `[page N]\n{text}` joined by `\n\n---\n\n`; the loop **breaks** rather than skipping when a block would overflow, so order (score order) is preserved and the budget is honoured to within one block. `pages` is `sorted(set(...))` — deduplicated, because two top-scoring chunks routinely come from the same page and a citation list saying `[3, 3, 7]` reads like a bug.

Falls back to `("", [])` so the caller keeps its old path — which `study_ai.py:412` relies on:

```python
retrieved, pages = _rag.context_for(doc_id, query, max_chars=_MAX_DOC_CTX)
if retrieved:  # else: _pack_pages(page_text, _MAX_DOC_CTX)
```

That call sits *behind* a `full_size <= _MAX_DOC_CTX` check, so a document that already fits is sent whole and never retrieved against. Correct call — retrieval can only lose context when nothing needs dropping.

### 9.2 `context_for_docs` — topic scope

```python
total_k = max(_DEFAULT_K, min(24, int(k) * len(ids)))
per_doc = max(2, total_k // len(ids) + 1)
```

Asks every indexed document in the topic for `per_doc` passages, merges, sorts by score globally, then walks the merged list under the same character budget and the `total_k` ceiling. So a six-document topic gets more room than the single-document default, bounded at 24.

The tagging is the point:

```python
origin = "CURRENT DOCUMENT" if hit["doc_id"] == current_doc else "OTHER DOCUMENT"
block = f"[{origin}: {label} | page {hit['page']}]\n{hit['text']}"
```

`label` comes from the `labels` map with a `doc_id` fallback, so a caller that forgets to pass titles still gets a usable block. Every hit carries `doc_id`, `page`, `score`, `current` — `study_ai.py:382` uses `current` to count siblings for its header.

Three edge behaviours worth recording:

- `dict.fromkeys(doc_ids)` deduplicates while preserving order, so a topic whose peer list repeats the open document does not double-count it.
- Ids with no index contribute nothing; `retrieve()` returns `[]` and the loop just moves on. A topic of six documents where one is indexed returns that one document's passages, not an error.
- `k` is the *base* k, and `total_k` scales from it — passing `k=1` over 20 documents still yields `total_k=6`, because the floor is `_DEFAULT_K`.

> [!NOTE] One asymmetry
> `context_for`'s overflow `break` and `context_for_docs`' overflow `break` are both hard stops. A single oversized block that exceeds the budget at position 0 returns `("", [])` for the single-doc path and `("", [])` for the topic path — the caller then falls back to head-packing. `max_chars` under `2_000` is clamped up, so this needs a chunk larger than 2,000 chars, which `_CHUNK_CHARS = 1400` prevents. Guarded by construction, not by code.

***

## 10. Callers

| Caller | Uses | Notes |
|:--|:--|:--|
| `library_index.run_ai_pass` | `_chunk_pages`, `_embed_dir`, `_load_embedder` | The `# noqa: SLF001` trio. Deliberate reuse — one chunker, not two that drift. |
| `study.py:1240` | `build(target, pages)` | Single-doc reindex route. |
| `study.py:1287` | `build(path.stem, pages)` | Library-wide loop — the §8 blast radius. |
| `study.py:112` | `remove` | Document-delete cleanup tuple. |
| `study_ai.py:378` | `has_index` | Filters topic peers to the indexed ones before retrieving. |
| `study_ai.py:382` | `context_for_docs` | Wrapped in `try/except` → logs and falls back to whole-document packing. |
| `study_ai.py:412` | `context_for` | Single-doc retrieval, also `try/except`-guarded. |

Both retrieval call sites are exception-guarded and both degrade to the pre-RAG behaviour. That is the right posture: RAG is an enhancement, and an enhancement that can 500 a study answer is a regression.

> [!IMPORTANT] `run_ai_pass` does not use `build()`
> It reimplements the write path to checkpoint per 32-chunk slice — that is the whole reason the job is resumable. So `build()`'s `unlink()` bug does **not** fire during indexing; it fires only on the two `study.py` reindex routes. The indexing job has the *other* bug (ledger #3/#4, no vectors but stage marked done). Two write paths, two different failure modes, one shared invariant.

***

## 11. Tests — `tests/test_library_rag.py`

37 cases, all passing, no network and no real model:

```bash
.venv/bin/python -m pytest tests/test_library_rag.py -q
# 37 passed in 0.30s
```

Coverage by group: chunking (8 — page anchoring, whitespace, overlap arithmetic, 80-char floor, cap, tail-break scoping), tokenising (2 — stopwords, Bangla), lexical scoring (3 — IDF preference, empty query, length normalisation), build/load/remove (7 — both modes, idempotence, malformed JSON, empty input), the §7 guard (3 — mismatch, fall-back correctness, shape-agreement), `retrieve` contract (4 — unindexed, empty query, `k` clamping, zero-score filtering), context assembly (10 — budget, minimum budget, empty inputs, CURRENT/OTHER tagging, label fallback, dedupe).

> [!WARNING] Fixture trap worth knowing
> `fastembed 0.8.0` **is installed** in this venv, so resetting `_embedder_probed = False` does *not* give you lexical mode — it re-probes and loads the real model. The first run of this suite failed 4 tests for that reason, all of them mine. The fixture patches `_load_embedder` itself:
> ```python
> monkeypatch.setattr(lr, "_load_embedder", lambda: None)      # lexical
> monkeypatch.setattr(lr, "_load_embedder", lambda: model)     # fake vectors
> ```
> Any future test touching this module must do the same, or it will silently exercise a 30 MB model instead of the path it claims to test.

***

## 12. Findings summary

| # | Finding | Severity | Status |
|--:|:--|:--|:--|
| 1 | Chunk↔vector divergence **is guarded** by a shape check → lexical fallback, not wrong pages. Ledger #3 was overstated. | correction | ledger revised |
| 2 | `build()` deletes a live `.npy` when no embedder is present → silent library-wide downgrade via `study.py:1287`. | 🟠 medium | **not fixed** |
| 3 | `has_embeddings()` checks the `.json`, so "embedded" is metadata presence. With (2) it can report vectors that do not exist. | 🟡 low–med | **not fixed** |
| 4 | `fastembed_cache` resolves from `__file__`, ignoring `SABLE_PERSISTENT_ROOT` — second instance of ledger #24. | 🟡 low | **not fixed** |
| 5 | `run_ocr_pass`'s comment says AI OCR ignores a passed `pages` list; the code assigns `targets = pages`. Comment and code disagree. | 🟡 low | **not fixed**, doc 19 §9 |
| 6 | `_reindex_search` has no caller in `library_index.py`; `run_search_pass` calls `_search.index_document` directly. | 🔵 dead code | **not fixed** |

Finding 5, precisely: the comment reads *"no honouring a stale `pages` list either. A caller that passed `pages=[4]` … must NOT narrow an AI OCR run down to one page"* — and the next two lines are `if pages is not None: targets = pages`. Either the comment is stale or a fix regressed. Behaviour today is the code's, not the comment's.

***

## 13. What this changes upstream

- [[19-engine-library-index]] §11 and its 🔴 callout: the divergence is real, the *silent misattribution* is not. Downgraded, with a pointer here.
- `documentation/README.md`: coverage tables, ledger #3/#4, and the priority list all updated.
- `tests/test_library_rag.py` added — the module's first tests.

**Next on the list:** `connectors/common/browser_scraper.py` (1,517 lines), now priority 1.
