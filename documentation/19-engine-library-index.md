# 19 — `engine/library_index.py` (1,184 lines)

> [!IMPORTANT]
> The resumable OCR + embedding pipeline behind the Study Library. One *index job* per document, three possible stages, two of them checkpointed page-by-page / chunk-by-chunk so a crash, restart, or user pause resumes exactly where it stopped.

**Read depth:** full file, line 1 → 1,184.

***

## 1. The design in one paragraph

A job owns a plan (`pipeline`) of up to three stages — **ocr → search → ai** — and persists state to `library/jobs/<doc-id>.json` after every unit of work. Rendered page images are cached under `library/ocr_pages/<doc-id>/` so a resumed pass never re-renders. Page text lives in `library/text/<doc-id>.json` as a plain `list[str]`. Both job state and page cache are **derived** — the module docstring is explicit:

```
Both are derived data — losing them costs time, never truth
(the PDF and its page text remain the source of truth).
```

***

## 2. Tuning constants

| Constant | Value | Why |
|:--|--:|:--|
| `BANGLA_OCR_PAGE_TIMEOUT` | **5.0 s** | a slow cloud page must not wedge the job |
| `_CLOUD_CONCURRENCY` | 4 | network-bound, fan-out is fine |
| `_LOCAL_CONCURRENCY` | 2 | CPU-bound, shares cores |
| `_AI_OCR_CONCURRENCY` | **1** | serial — see below |
| `_MIN_TEXT_CHARS` | 12 | below this a page counts as "needs OCR" |
| `_MAX_OCR_STRIKES` | 3 | retire a page as permanently un-OCR-able |
| `_JOB_VERSION` | 1 | |

> [!CRITICAL]
> `_AI_OCR_CONCURRENCY = 1` is not timidity, it's a deadlock fix:
>
> ```
> # AI OCR drives the vision model through the SCRAPER path, which is backed by a
> # real browser session and capped by engine.browser_governor.MAX_BROWSERS (2).
> # Running pages 4-wide deadlocks on browser slots: two pages hold the slots and
> # never release them while two more wait forever — CPU pins, progress freezes.
> # Vision OCR therefore runs SERIALLY. It is slower but it actually finishes.
> ```

***

## 3. Three module-level sets, three different questions

| Set | Question it answers | Lifetime |
|:--|:--|:--|
| `_stop_requests` | "has someone asked this doc to stop?" | cleared on start + on settle |
| `_active_jobs` | "is a job executing **in this process**?" | added before status write, discarded in `finally` |
| `_doc_locks` | per-doc serialisation | **never cleaned** — see §14 |

`is_live()` checks `_active_jobs` only. That's what makes the restart-detection work:

```
# Documents with a job actually executing in THIS process. A job file that
# says "running" but has no entry here belongs to a process that died (server
# restart) — reporting it as running forever would be a lie.
```

And the ordering inside `run_job` is deliberate:

```
# Register as live BEFORE the status is written. A poll landing between
# the two would otherwise see "running" with no live task and downgrade
# the job to paused — a flicker the user would read as a stalled job.
```

***

## 4. Cooperative cancellation

`request_stop(doc_id)` sets a flag and returns whether a job was actually live. Passes poll `should_stop()` between pages/chunks. The docstring explains why not `task.cancel()`:

> *"Cancelling the asyncio task outright would leave a half-written checkpoint."*

`clear_stop` runs twice: at the top of `run_job` (a leftover stop must not abort a fresh run) and in the `finally`.

***

## 5. Job state schema

```python
{
  "version": 1, "doc_id": str,
  "status": "idle|running|paused|done|error",
  "stage":  "ocr|search|ai|\"\"",
  "pipeline": ["ocr","search","ai"],
  "completed": ["ocr","search"],
  "ocr":    {"cursor", "total", "engine", "lang", "done_pages", "failed_pages", "failed_permanent", "strikes", "ai_ocr", "model", "thinking_mode"},
  "ai":     {"cursor", "total", "model", "thinking_mode"},
  "search": {"done", "total"},
  "error": "", "updated": float,
}
```

> [!TIP]
> `completed` exists because counters can't express "finished with nothing to do":
> *"a stage can legitimately have nothing to do (an OCR pass on a text PDF) and would otherwise never reach 100%."*

`load_job` merges onto `_empty_job()` and re-`setdefault`s `ocr`/`ai`, so an old job file missing a key still loads. `save_job` is atomic (`.json.tmp` → `replace`) but **swallows write errors** with a warning — a full disk means the job silently stops checkpointing.

`job_exists` vs `load_job` is a deliberate split:

```
# load_job() deliberately invents an empty job when the file is missing,
# which is convenient for callers mid-pipeline but lies to anything trying to
# ask "has this document ever been indexed?". This is the honest check.
```

`has_embeddings(doc_id)` is a bare `LIBRARY_EMBED_DIR/<doc>.json` existence check — **it does not verify the vectors file or that the two agree** (see §14, finding 2).

***

## 6. Page text helpers

- `_load_pages` / `_save_pages` — `library/text/<doc>.json`, atomic write, tolerant of a corrupt file (returns `[]`)
- `page_needs_ocr(text)` — `len(text.strip()) < 12`
- `pages_needing_ocr` — 1-based indices
- `_doc_title` — best-effort read from `library/meta/<doc>.json`

***

## 7. Rendering

`_render_page_png(pdf, n, dpi=200)` uses **`pymupdf`** (`import pymupdf` — the modern package name, not `fitz`). Opens, validates range, `get_pixmap`, `tobytes("png")`, closes in `finally`.

`_cached_page_png` returns `library/ocr_pages/<doc>/00042.png` if present, else renders and writes. Cache-write failures are ignored — the bytes are still returned, so a read-only cache dir degrades to re-rendering rather than failing.

***

## 8. OCR engines — three of them

### 8.1 `_ocr_bangla_ocr` — cloud, with a shared deadline

One deadline for the *entire* attempt: proxy fetch **and** every candidate POST.

```python
deadline = asyncio.get_event_loop().time() + BANGLA_OCR_PAGE_TIMEOUT
```

Candidate list: direct first, then up to 4 random proxies from `server.api.routes.ocr._fetch_proxies`. The proxy probe itself is capped at `min(2.0, max(0.5, remaining))`. Before each POST: `if budget <= 0.3: break`.

The 403 story is in the docstring: *"The API 403s on a direct connection from some networks, so — exactly like the panel route — we try direct first, then a few free proxies."*

### 8.2 `_ocr_local` — SableOCR

`server.api.routes.ocr_engines.get_runner("sableocr")`, called via `asyncio.to_thread`. Default lang `eng+ben`. Failure → `""`, never raises.

### 8.3 `_ocr_vision` — the AI path

The most interesting function in the file, because it had to defeat a harness bug rather than a model limitation.

> [!CRITICAL]
> The root-cause comment:
> ```
> # Root-cause fix: the Qwen scraper session carries a GLOBAL instruction
> # set that includes the chat_title tool. No amount of prompt-prefixing
> # removes it — the model sees the tool and calls it mid-transcription.
> # Sync a bare, tool-less context for OCR so there is simply no tool to
> # call.
> ```

Flow:
1. Render (cached) PNG → temp file
2. `await _ensure_ocr_context(model)` — once per batch, see §9
3. Lazy import `server.api.routes.study_ai._resolve_attachments, _stream` (deliberate: a top-level import would cycle)
4. Stream, collecting `answer` events; capture `chat_id` from the first `meta` event
5. Strip harness leakage, in this order: `<chat_title>…</chat_title>` → tool tags → bare tool-call JSON → stray code fence

```python
_TITLE_TAG_RE      = r"<chat_title>.*?</chat_title>"
_TOOL_TAG_RE       = r"</?(?:action|tool_call|chat_title)\b[^>]*>"
_BARE_TOOL_JSON_RE = r"\[\s*\{\s*\"(?:name|tool|function)\"\s*:.*?\}\s*\]"
```

> [!WARNING]
> `_BARE_TOOL_JSON_RE` uses a non-greedy `.*?` up to the **first** `}]`. A transcription that legitimately contains a JSON array of objects with `"name"` keys will have everything from `[{` to the next `}]` silently deleted from the page text. Low frequency on prose PDFs, real on API-reference pages. Documented, not fixed.

**Attachment visibility** — the single best debugging line in the module:

```
# Visibility: this is the ONE line that tells us whether the page image
# actually made it onto the request. Without it, a failed upload looks
# identical to a model that simply ignored the image.
logger.info("AI OCR page %d: model=%s attach kwargs=%s", ...)
if not kwargs:
    logger.warning("... NO attachment resolved ... the page image will NOT reach the model")
```

**Session reuse** is the other half of the deadlock fix:

```
# One Qwen chat session reused for every page of an OCR run. Opening a fresh
# session per page exhausts the browser governor's account pool: page 1 grabs
# the session, page 2 waits the full 120s acquire timeout, the run dies having
# done a single page.
```

`_OCR_CHAT_ID` is set from the first `meta` event and passed as `chat_id` on every later page. `reset_ocr_session()` clears it (and `_OCR_CONTEXT_SYNCED`) at the start of each AI OCR run.

### 8.4 `ocr_page` — the arbitration

```
ai_ocr=True   → vision model ONLY. Fail = blank page, never fall back.
ai_ocr=False  → BanglaOCR (5s) → local SableOCR → ""
```

> [!IMPORTANT]
> *"When `ai_ocr` is set, the vision model is the FINAL word: no BanglaOCR, no SableOCR fallback. An AI OCR page that fails is left blank rather than silently filled by a different engine — the user asked for AI OCR and gets exactly that (or an explicit failure to retry)."*

Returns `{page, text, engine, ok}`. Never raises.

***

## 9. `_ensure_ocr_context` — one sync per run

Guarded by `_OCR_CONTEXT_LOCK` and keyed on `_OCR_CONTEXT_SYNCED == model`. Skips API-connector models entirely (`_is_api_model`) because they carry an explicit system prompt and never touch the scraper session.

Calls `service.sync_context(custom_instructions=...)` with a minimal tool-less directive:

> *"You are an OCR engine. Transcribe page images to text EXACTLY as they appear. Output only the transcription. Never call tools. Never emit tool tags or a chat title."*

Why it can't just be a prompt prefix:

```
# the Qwen scraper session carries a global instruction set that includes the
# chat_title tool. The model sees that tool and calls it while transcribing,
# leaking a tool-call block into the page text. Prompt prefixes and output
# stripping treat the symptom; syncing a context with NO tools removes the cause.
```

Re-sync happens because `reset_ocr_session()` clears the marker — so a later run with a *different* model re-syncs.

***

## 10. `run_ocr_pass`

### 10.1 Target selection — two very different branches

**AI OCR branch** — deliberately ignores every resume heuristic:

```
# AI OCR means AI OCR. Every page, full stop — no blank-page scanning,
# no resume skipping, and no honouring a stale ``pages`` list either.
# A caller that passed pages=[4] (a leftover blank-page target) must
# NOT narrow an AI OCR run down to one page.
```

So `targets = 1..len(pages)`, `done = set()` (clean slate). If `pages` **is** given it's honoured, and those pages get their strikes/`permanent` status cleared — a manual pick re-arms a retired page.

**Default branch** — `targets = pages or pages_needing_ocr(...)`, minus `permanent`, minus `done_pages` (that subtraction *is* the resume).

### 10.2 The strike system

Failure → `strikes[p] += 1`; at `>= 3` → `permanent.add(p)`.

```
# Without this a genuinely blank/image-only page keeps re-entering the
# "needs indexing" set forever: it fails, is not marked done, looks
# unindexed, gets re-queued on the next scan, fails again — an infinite loop
# that burns browser slots and CPU.
```

Success clears the strike and discards `permanent`. Manual `pages=[…]` re-arms too.

### 10.3 Per-page worker

`asyncio.Semaphore(concurrency)` + one `asyncio.Lock` around the shared mutable state (`text_pages`, `done`, `failed`, `strikes`, `permanent`, `job`). Every mutation is followed by `_save_pages` **and** `save_job`, so a kill between pages loses at most one page.

Only a **successful** page joins `done`:

```
# Only a SUCCESSFUL page is marked done — a page that failed both engines
# stays in the todo set so the next run retries it instead of silently
# leaving a blank forever.
```

### 10.4 Terminal state is not this pass's job

```
# Only a stop settles the job here. Declaring "done" at the end of one pass
# made the UI flash "Indexed" between OCR and the embedding pass, and left
# the status wrong while later stages were still running. run_job owns the
# terminal status.
```

So: stop → `paused`; otherwise `stage = ""`, `_mark_stage_done(doc_id, "ocr")`, return.

***

## 11. `run_ai_pass` — chunk + embed

The embedding pass takes **no model**:

> *"The embeddings are produced by `engine.library_rag` (local fastembed model). This pass takes NO model — the vision model belongs to AI OCR, not to embedding, and conflating the two was a bug."*

Flow: `_rag._chunk_pages(pages)` → `_rag._load_embedder()` → loop in slices of **32**, `embed()` each slice, `existing.extend(batch)`, then checkpoint both files:

| File | Contents |
|:--|:--|
| `LIBRARY_EMBED_DIR/<doc>.json` | `{version, doc_id, built, chunks: [...]}` |
| `LIBRARY_EMBED_DIR/<doc>.npy` | `float32` matrix |

Resume loads both back (`np.load(vec_path)`) only when `model_obj is not None`.

> [!WARNING]
> **Bug — chunk/vector length divergence.** The `existing.extend(batch)` line is **outside** the `try` that calls `embed()`. If embedding a slice raises, the chunks for that slice are still appended and the cursor still advances, but no vectors are added for them, so the `.npy` ends up with fewer rows than the `.json` has chunks. **Not fixed** — the correct fix is to extend `existing` inside the same try, or to truncate on failure.
>
> **Corrected failure mode.** This was originally recorded here as 🔴 *silent corruption*, claiming chunk *N* retrieves vector *N+k* and search "returns plausible-but-wrong passages." That is not what happens. `library_rag._vector_scores` validates `vectors.shape[0] != len(chunks)` and returns `None`, and `retrieve()` falls back to lexical scoring. A divergent pair degrades to lexical retrieval — it never cites a passage from the wrong page. The bug is real; the consequence is silent **quality loss**, not misattribution. See [[26-engine-library-rag]] §7, and `tests/test_library_rag.py::test_retrieve_falls_back_to_lexical_on_misalignment` for the proof.

> [!WARNING]
> Same function: if `_load_embedder()` returns `None`, the entire loop is skipped for embedding (`if model_obj is not None`) but `cursor` still advances to `total` and the stage is marked complete. The result is a `chunks` file with **zero** vectors and a job that reports 100%. **Not fixed.**

Empty page text is a hard error, not a silent skip: `status="error"`, `error="no page text to embed"`.

***

## 12. `run_job` — orchestration

```python
lock = await _doc_lock(doc_id)
async with lock:
    clear_stop(doc_id)
    ...
```

Serialised per document because otherwise:

> *"an auto-OCR task and a manual job must never run at the same time, or the second sees the first's cursors mid-write and skips pages whose text isn't committed yet."*

### 12.1 AI OCR forces the embedding pass

```python
if ai_ocr and do_ocr:
    do_ai = True
```

```
# AI OCR rewrites page text, which makes any existing embeddings STALE
# (they were built from the pre-OCR text). Force the embedding pass on
# so the index reflects what OCR actually produced. Without this the
# job ran ocr+search only and the vectors kept pointing at old garbage.
```

### 12.2 Pipeline plan is recorded up front

*"The UI weights each stage equally, so the overall percentage never depends on a total we don't know yet (chunk count only exists after the AI pass has chunked the document)."*

### 12.3 The `finally` always settles

```python
_active_jobs.discard(doc_id)
clear_stop(doc_id)
final = load_job(doc_id)
final["stage"] = ""
if final.get("status") == "running":
    final["status"] = "done"
```

> [!WARNING]
> This is unconditional: **any** exit from the `try` that leaves status at `running` — including an exception propagating out of `run_ocr_pass` — is recorded as `done`, not `error`. The `error` field is only ever set by `run_ai_pass`'s no-text guard. A crashed job therefore reports success. **Not fixed.**

***

## 13. `run_search_pass` + `status_for`

`run_search_pass` mirrors page text into the FTS index via `library_search.index_document`. Idempotent (delete-then-insert per doc), gets its own stage purely to keep the bar honest:

> *"Cheap next to OCR or embedding, but it still walks every page, so it gets a stage of its own to keep the overall progress bar honest."*

### 13.1 The percentage algorithm — stage-weighted, not unit-weighted

```
# Stage-weighted, NOT unit-weighted. A stage's unit total is often unknown
# until that stage begins (the chunk count only exists once the document
# has been chunked), so dividing by a running sum made the bar hit 100%
# during OCR and then fall backwards when search joined the denominator.
```

Per stage in `pipeline`: `1.0` if in `completed`, else `done/total` (or `0.0` if `total <= 0`). `percent = round(sum(fractions)/len(fractions) * 100)`. `status == "done"` short-circuits to `100`.

### 13.2 Restart downgrade

```python
if status == "running" and not is_live(doc_id):
    status = "paused"
```

*"Reporting that as running would show a bar that never moves, so downgrade it to paused (resumable)."*

Output is flat: `doc_id, status, stage, pipeline, completed, live, stop_requested, ocr_done/total/failed/failed_permanent, search_done/total, ai_cursor/total, percent, updated`.

***

## 14. Findings

> [!CRITICAL]
> **1. Chunk↔vector divergence on embedding failure.** `existing.extend(batch)` sits outside the embedding `try` (§11). One failed slice corrupts every later chunk's vector alignment, silently. **Not fixed.**

> [!CRITICAL]
> **2. `model_obj is None` produces a 100%-complete job with no vectors.** §11. `has_embeddings()` can't detect it. **Not fixed.**

> [!WARNING]
> **3. `run_job`'s `finally` maps every non-stop exit to `done`,** including exceptions. A crashed pass reports success. §12.3. **Not fixed.**

> [!WARNING]
> **4. `_doc_locks` is never pruned.** One `asyncio.Lock` per document, forever, for the life of the process. Trivial per entry, but a library with thousands of documents accumulates them and nothing ever removes a lock for a deleted doc. **Not fixed.**

> [!WARNING]
> **5. `save_job` swallows write errors.** A full disk or permission error means checkpoints silently stop landing while the job keeps reporting progress in memory. **Not fixed.**

> [!NOTE]
> **6. `_reindex_search()` is dead code.** Defined at line ~1000, never called — `run_search_pass` calls `_search.index_document` directly. Harmless duplicate, but its docstring describes the OCR-needs-reindex rationale that `run_search_pass` actually implements.

> [!NOTE]
> **7. `_BARE_TOOL_JSON_RE` can eat legitimate JSON** from a transcribed page. §8.3.

> [!NOTE]
> **8. `_LOCAL_CONCURRENCY = 2` is declared but unused** — the semaphore only uses `_CLOUD_CONCURRENCY` or `_AI_OCR_CONCURRENCY`. `_ocr_local` runs inside the same semaphore as the cloud path.

No code changed in this file.

***

## 15. Cross-references

- [[07-engine-config]] — `LIBRARY_JOBS_DIR`, `LIBRARY_OCR_DIR`, `LIBRARY_TEXT_DIR`, `LIBRARY_EMBED_DIR`, `LIBRARY_PDF_DIR`, `LIBRARY_META_DIR`
- [[09-memory-search-and-research]] — `library_search.index_document`, `library_rag._chunk_pages`, `_load_embedder`, `_embed_dir`
- [[15-deep-dive-corrections]] — `browser_governor.MAX_BROWSERS = 2`, the slot cap behind `_AI_OCR_CONCURRENCY = 1`
- [[11-server]] — `server.api.dependencies.service.sync_context`
- **`server/api/routes/ocr.py`** — `BANGLA_OCR_URL`, `OCR_HEADERS`, `_fetch_proxies`
- **`server/api/routes/ocr_engines.py`** — `get_runner("sableocr")`
- **`server/api/routes/study_ai.py`** — `_resolve_attachments`, `_stream` (lazy import, cycle)

> [!IMPORTANT]
> The reusable lesson from this module: **every long-running pass writes its checkpoint immediately after the unit of work, and never declares the job terminal.** `run_job` owns the terminal status, and `is_live()` is what makes a stale `"running"` recoverable. Copy that pair into anything else that resumes.
