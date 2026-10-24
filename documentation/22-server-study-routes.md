# 22 — `server/api/routes/study.py` (2,340) + `study_ai.py` (1,641)

> [!IMPORTANT]
> The Study Library: PDF ingestion, page text, highlights, progress, full-text search, the Indexing tab, the knowledge graph, analytics, and two separate AI surfaces (document Q&A and note rewriting). Two files because the storage layer and the AI layer are cleanly separable.

**Read depth:** both files full, 3,981 lines total.

***

## 1. Storage layout

```
~/sable_output/library/
  pdfs/<doc-id>.pdf          source of truth for rendering
  meta/<doc-id>.json         all derived "connected data"
  text/<doc-id>.json         per-page text (split out of meta)
  orig/<doc-id>.<ext>        original upload, for converted docs
  thumb/<doc-id>.jpg         cached page-1 preview
  jobs/<doc-id>.json         index job state (doc 19)
  ocr_pages/<doc-id>/        rendered page cache (doc 19)
  embed/<doc-id>.{json,npy}  chunks + vectors (doc 19)
  academ/topics/notes/srs    see §12
```

> [!TIP]
> The module docstring states the invariant:
> *"The PDF is the source of truth for rendering; the meta JSON holds all derived 'connected data' — per-page text, outline, progress, and highlights — so the frontend never has to re-parse the PDF to draw the sidebar."*

Page text was **relocated out of meta** and is migrated lazily on first read:

```python
inline = meta.pop("page_text", None)
if inline and not _text_path(doc_id).exists():
    _write_page_text(doc_id, inline)
```

`_write_meta` also strips `page_text` defensively on every write.

***

## 2. Constants

| Name | Value |
|:--|--:|
| `_MAX_PDF_BYTES` | 100 MB |
| `_MAX_RECTS` | 200 highlight rects per selection |
| `_META_VERSION` | 1 |
| `_ACADEMICS_VERSION` | 2 |
| `_CONVERT_TIMEOUT` | 120 s |
| `_NATIVE_EXT` | `.pdf` |
| `_CONVERTIBLE_EXT` | 15 formats (docx, pptx, odt, rtf, txt, csv, md, epub, xlsx, …) |
| `_INDEX_MODES` | `none`, `search`, `rag` — **default `rag`** |
| `_OCR_MODES` | `auto`, `force`, `never` — **default `never`** |

> [!IMPORTANT]
> Uploads **do not OCR by default**. Verbatim:
> ```
> # Uploads default to "never": the ingest path extracts text only and never
> # auto-OCRs. Scanned pages land blank, and the user opts into OCR (or AI OCR)
> # afterwards from the document's menu, which hits the on-demand index job.
> ```
> The docstring for `ocr="auto"` in `_OCR_MODES` says the opposite of the default — the comment block above it is the accurate one.

***

## 3. Upload — staging, then a background pipeline

The request's only job is to get bytes onto disk, 1 MB at a time:

```
# The request's ONLY job is to get bytes onto disk. Conversion, extraction
# and indexing are all slow — a LibreOffice cold start, a full PyMuPDF walk
# of every page, then OCR — and every one of them is a SYNCHRONOUS call.
# Running them here held the response open for minutes on a textbook, and
# because there was no await in sight it froze the event loop with them, so
# even /index/status stalled.
```

Returns immediately with `{upload_id, queued: true, indexing: {mode, stages, ocr}}`. `stages` is computed from the flags so the UI can show what will run.

> [!CRITICAL]
> `_upload.mark_active(upload_id)` is called **before** `asyncio.create_task`. Verbatim: *"A status poll landing in the gap would otherwise see 'running' with nothing driving it and report an error."* Same ordering rule as `run_job` in doc 19.

### 3.1 `_run_upload_pipeline` — four phases

`convert → extract → persist → index`, each `await`ed through `asyncio.to_thread` so the event loop stays free.

Cancellation is checked **between** phases (`_UploadCancelled`) and then handed to the index job as its own `should_stop`:

```
# Awaited, not spawned: cancelling the upload has to stop the index
# pass too, and run_job already owns resumable checkpoints.
await _index.run_job(doc_id, ..., should_stop=lambda: _upload.is_cancelled(upload_id))
```

### 3.2 Scanned PDFs degrade gracefully

```python
try:
    extracted = await asyncio.to_thread(_extract, pdf_bytes)
except ValueError:
    # No text layer — almost certainly a scanned/image-only PDF. Those
    # are exactly what OCR exists for, so ingest with blank page text
    # and let the auto-OCR pass fill it in.
    pages = await asyncio.to_thread(_pdf_page_count, pdf_bytes)
    extracted = {"pages": pages, "page_text": [""] * pages, ...}
```

Page count falls back `pymupdf` → `pypdf` → `0`.

### 3.3 Cancel means gone

```python
except _UploadCancelled:
    # Cancelled means gone. Delete the document if it got far enough to
    # have one, then settle the job.
    if doc_id:
        await asyncio.to_thread(_delete_doc, doc_id)
    _upload.finish(upload_id, "cancelled")
```

`_delete_doc` is shared with the delete endpoint so a document can never be half-removed. It removes pdf/meta/text/lock/thumb/orig, then calls `_search.remove_document`, `_rag.remove`, `_catalog.remove_doc`, and `_index.delete_job`.

> [!NOTE]
> `_delete_doc` runs `_valid_id()` first and returns silently on a bad id — so the `_upload_cancel` path that calls it with a stale `doc_id` is a no-op rather than an error. Correct, just not obvious.

***

## 4. Atomic meta writes with cross-process locking

`_update_meta(doc_id, mutator)` is the single read-modify-write primitive:

```python
with _meta_lock(doc_id):
    meta = _read_meta(doc_id)
    if not meta: raise HTTPException(404)
    result = mutator(meta)
    _write_meta(doc_id, meta)
    return result
```

`_meta_lock` wraps `engine.filelock.exclusive_lock` on `meta/<id>.lock` — **cross-process**, not just asyncio. Everything that mutates a doc goes through it: progress, highlights (add/update/delete), bookmark, group, session totals.

`_write_meta` is atomic (`.json.tmp` → `replace`) and stamps `updated`.

***

## 5. Document IDs

```python
def _valid_id(doc_id: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{32}", doc_id or ""))
```

uuid4 `.hex`. Called before **every** disk touch in this file — meta read, pdf serve, delete, index start, upload-cancel. It is the primary path-traversal defence here.

> [!WARNING]
> `study_list_docs`, `study_collections`, `_library_meta_records`, `study_search_stats`, `study_graph`, and `_retag_docs` all `glob("*.json")` and parse without calling `_valid_id` — they trust the filename. They then often call `_read_meta(f.stem)` which *does* validate. So a malformed file is skipped downstream but still parsed once. Low risk; worth knowing the validation isn't universal.

***

## 6. Reading endpoints

| Endpoint | Notes |
|:--|:--|
| `GET /docs` | newest-first, `_summarize` strips page text |
| `GET /doc/{id}` | full meta |
| `GET /doc/{id}/text` | `{id, pages, page_text}` — separate so meta stays cheap |
| `GET /doc/{id}/pdf` | `FileResponse`, `Cache-Control: private, max-age=3600` |
| `GET /doc/{id}/thumb` | cached page-1 JPEG, backfilled on demand |
| `GET /doc/{id}/original` | converted docs only |
| `GET /formats` | `{accept, native, convertible, converter}` |
| `GET /collections` | Drive folders that have imported docs |

`_summarize` adds `has_text`, `has_thumb` (`thumb_path(id).exists()`), `source`/`source_url`/`collection`/`collection_id`, and `highlights` as a **count**, not the array.

### 6.1 `GET /doc/{id}/original` — the path guard worth copying

```python
raw_ext = str(meta.get("source_ext") or "").strip().lower().lstrip(".")
if not raw_ext or f".{raw_ext}" not in _CONVERTIBLE_EXT:
    raise HTTPException(404)
base = LIBRARY_ORIG_DIR.resolve()
path = (base / f"{doc_id}.{raw_ext}").resolve()
if path.parent != base or not path.exists():
    raise HTTPException(404)
```

```
# source_ext comes from meta JSON, which is not trusted input — a tampered
# value could escape originals/. Whitelist it, then confirm containment.
```

Whitelist **then** containment check. That's the correct order and it's the only place in these two files that does both.

***

## 7. Highlights

`_clean_rects(raw)` validates `{x,y,w,h}`:
- rejects non-list
- caps at `_MAX_RECTS` (200)
- each of x/y/w/h must be `float`-able and `math.isfinite`
- `w > 0 and h > 0`

*"Rejects non-list payloads and drops malformed entries instead of trusting client input; caps the count so a huge selection can't bloat the meta file."*

Highlight record: `{id (12 hex), page, rects, color (≤32, default #ffd400), text (≤2000), note (≤2000), created}`.

***

## 8. Progress

`PATCH /doc/{id}/progress` clamps `current_page` to `[1, total]` and `percent` to `[0, 100]`, derives `percent` from the page when not supplied, and stamps `last_opened`.

***

## 9. Full-text search

`GET /search?q=&limit=40&doc_id=` delegates to `library_search.search`, then **backfills titles from the meta store** for every hit and doc rollup:

*"Titles are attached from the meta store so a hit is readable even when the index was built without a title."*

`POST /search/reindex` — body `{search, rag, only_missing, doc_id, catalog}`:
- with `doc_id` → one doc, `_catalog.upsert_doc` + optional `_search.index_document` / `_rag.build`
- `catalog: true` → `_catalog.rebuild_from_meta()`
- `only_missing` → skip doc ids already in `_search.indexed_doc_ids()`
- otherwise `_search.rebuild_all()` / full RAG walk

***

## 10. Index endpoints (thin over doc 19)

| Endpoint | Delegates to |
|:--|:--|
| `POST /index/start` | `_spawn_index_job` (per doc) or one task per doc for `all:true` |
| `POST /index/pause` | `_index.request_stop` + settle-to-paused if not live |
| `POST /index/stop` | `_index.request_stop` |
| `GET /index/status` | `_index.status_for(id)` or `_index_overview()` |
| `GET /index/inspect/{id}` | real indexed output (§10.1) |
| `POST /index/stream` | SSE watcher over a **detached** job (§10.2) |

### 10.1 `index/inspect` — shows the actual product

Returns per-page `{page, chars, text}`, embedding chunks `{page, chars, text}`, `chunk_count`, `embed_built`, and a **staleness** flag:

```python
stale = bool(embed_built and text_mtime and text_mtime > embed_built + 60)
```

```
# Freshness: embeddings older than the page text mean a re-OCR happened
# without a re-embed — the vectors point at stale text.
```

The 60-second slack absorbs filesystem timestamp granularity.

> [!NOTE]
> This is the one place in the codebase that *detects* the stale-embedding problem described in doc 19 §14. It detects it but nothing acts on it — no auto re-embed.

### 10.2 `index/stream` — the job is detached from the connection

```
# The job runs DETACHED from this HTTP connection, in its own tracked
# background task. The SSE stream only WATCHES it. Closing the modal (or a
# dropped connection) must NOT kill the work — previously the whole job
# lived inside the stream generator, so a disconnect set the stop flag and
# cancelled the task mid-run, which is why AI OCR "stopped at one page"
# and everything vanished when the panel closed.
```

`should_stop=lambda: _index.is_stop_requested(doc_id)` — **only** an explicit stop request, never a disconnect. 15 s keepalive comments (`: keepalive\n\n`) so a proxy doesn't time out an idle SSE. Final drain loop ensures the last progress event isn't lost.

### 10.3 `_index_overview` — library-driven, not job-driven

```
# The Indexing tab is the one place that answers "what is indexed and what
# isn't", so it cannot be driven by job files alone: a document that was
# never indexed has no job file at all, while a document deleted by an older
# build can still be survived by one.
```

Self-heals by deleting job files whose doc is gone. State precedence:

| Condition | `state` |
|:--|:--|
| job status in `running/paused/error` | that status |
| `ocr_failed_permanent > 0` and not searchable | `error` |
| searchable and RAG | `indexed` |
| searchable only | `partial` |
| neither | `unindexed` |

> [!TIP]
> The `error` mapping exists so the panel offers **Re-index** instead of plain **Index**: *"the plain 'Index' that would silently re-queue it forever."*

***

## 11. Knowledge graph

`GET /graph` builds `topic → doc → {chat, note, deck, highlight}` in the server because *"assembling it client-side would mean cross-joining five separate endpoints."* Every edge is derived, nothing inferred:

| Edge | Source |
|:--|:--|
| `t:<topic> → d:<id>` | `meta.group.topic` |
| `d:<id> → c:<chat>` | chat id prefix `study-<docId>-` |
| `d:<id> → n:<note>` | `note.refs[].doc_id` |
| `d:<id> → k:<deck>` | `deck.doc_id` |
| `d:<id> → h:<docId>` | `meta.highlights[]` (one node, with count) |

```python
_CHAT_ID_RE = re.compile(r"^study-([0-9a-f]{32})-", re.I)
```

> [!CRITICAL]
> *"A chat opened from the reader gets the id `study-<docId>-<threadId>` (see study-chat.js). That naming convention is the ONLY link between a library document and its chats — **there is no join table**."*
>
> Rename that convention on either side and every study chat silently disappears from the graph. The regex is the contract.

Orphan filtering: `did not in doc_ids → skip`, and for notes `linked &= doc_ids`. A reference that outlives its document is dropped, not rendered as a dangling node.

`_read_json(path, default)` swallows corrupt files: *"A corrupt file must not take the graph down."*

***

## 12. Academics — three axes, one of them primary

```
{"version": 2,
 "topics":     [{id, name}],          ← PRIMARY, flat
 "sessions":   [{id, name}],          ← flat cohort axis
 "semesters":  [{id, name, subjects: [{id, name, topics: []}]}]}   ← legacy
```

> [!IMPORTANT]
> Topic is mandatory on every document. `study_upload` defaults it to `"Others"`, and `study_update_group` re-defaults a cleared topic back to `"Others"` before writing. `academics_delete_topic` re-buckets a deleted topic's documents into `"Others"` and recreates the bucket if `"Others"` itself was deleted.

`_read_academics` performs an **idempotent one-time migration** folding legacy nested `semesters→subjects→topics` into the flat `topics` list, deduped by name.

`_retag_docs(field, old, new, cascade=False)` walks every meta file and renames the tag. `cascade=True` (used on delete) also clears every deeper field in `semester → subject → topic` order so a doc never points at a subject that no longer exists.

> [!WARNING]
> `_ensure_academic_path` (semester/session/subject/topic chain) is defined but **never called** — `study_upload` uses `_ensure_topic` only. Dead code carrying an obsolete nested model.

***

## 13. Sessions & analytics

`POST /doc/{id}/session` → `library_sessions.record(doc_id, seconds, started, ended, pages)`. Returns `{recorded: False, reason: "session too short"}` when the recorder rejects it. On success mirrors the running total onto meta:

```python
m["study_seconds"] = round(float(m.get("study_seconds") or 0) + entry["seconds"], 1)
m["last_studied"] = entry["ended"]
```

So a doc card can show time-studied without reading the session store. `pages` is capped at 400 entries.

`GET /stats?days=30` — titles for `top_docs` resolved from meta, falling back to `"Deleted document"`. `GET /doc/{id}/stats`, `DELETE /stats?doc_id=`.

***

## 14. Markdown export

`_export_markdown(meta)` renders front-matter bullets (pages, progress, highlight count, time studied, grouping tags), then `## Highlights` sorted by `(page, added)`, then `## AI Answers`.

> [!NOTE]
> `_export_markdown` reads `meta["ai_notes"]` with keys `question`/`action`/`answer`/`content`/`page`. Those are written by `study_ai.py`'s `study_add_note` (§17) — the two files share a schema with no shared constant.

`GET /export` skips docs with neither highlights nor ai_notes and joins them with `***` separators. Both set `Cache-Control: no-store`.

***

## 15. `study_ai.py` — two distinct AI surfaces

| Surface | System prompt | Grounding | Tools |
|:--|:--|:--|:--|
| **Document Q&A** | `_build_study_system` → shared `build_instructions(layout_mode="study")` | `_build_context` over page text / RAG | full `get_study_tool_schemas()` |
| **Note rewrite** | `_NOTE_SYSTEM` or `_NOTE_EDIT_SYSTEM` | note body + optional refs | `study_note` only (or none for `ask`) |

Config in `system/study_ai_config.json`: `{model, thinking_mode, persona, custom_prompt}`. Empty model falls back to `get_all_models()[0]`.

### 15.1 Budgets

| Constant | Value |
|:--|--:|
| `_MAX_CTX_CHARS` | 24,000 (selection / page) |
| `_MAX_DOC_CTX` | 60,000 (whole document / topic) |
| `_MAX_SELECTION_CHARS` | 4,000 |
| `_MAX_PERSONA_CHARS` | 2,500 |
| `_MAX_BODY_NOTE` | 200,000 |
| `_MAX_ATTACH_BYTES` | 20 MB |
| `_MAX_TOOL_ROUNDS` | 12 |

***

## 16. `_build_context` — retrieval over truncation

> [!CRITICAL]
> ```
> # For whole-document scope we prefer retrieval over truncation: a long
> # textbook does not fit in ``_MAX_DOC_CTX``, and silently dropping its tail
> # hides exactly the passage the student is asking about.
> ```

Scope resolution, in order:

1. **`selection`** — the page containing the selection; if over budget, centres a window on `ctx.find(selection[:60])`, else head-first.
2. **`page`** — multi-page aware (`pages: [12,13,14]`); budget split evenly per page so one page can't crowd out the rest.
3. **`doc`, topic-scoped** — if the topic has >1 indexed doc, `_rag.context_for_docs(indexed, query, labels, current_doc)` fuses passages across the whole topic and tags each with `[CURRENT DOCUMENT]` / `[OTHER DOCUMENT]`.
4. **`doc`, fits budget** — send the whole document. *"retrieval can only lose context when nothing needs to be dropped."*
5. **`doc`, too big + query + index** — `_rag.context_for` top-ranked passages.
6. **`doc`, fallback** — `_pack_pages` head-first with a `… (N more pages truncated)` marker.

### 16.1 Topic peers via the catalog, not the meta store

```
# Reads the SQLite catalog rather than walking the meta store, so a topic
# lookup is one indexed SELECT instead of parsing every meta JSON.
```

`_topic_peers` returns `("", [])` for the `Others` bucket:

```
# That bucket is where unfiled PDFs land, so treating it as a topic would
# glue unrelated books together.
```

Self-heals an empty catalog: `if _catalog.count() == 0: _catalog.rebuild_from_meta()`.

### 16.2 Provenance hint is conditional

`_PROVENANCE_HINT` is appended **only** when `"OTHER DOCUMENT" in context`. *"so a single-doc answer is never distracted by provenance rules it cannot use."*

***

## 17. Study tool loop — two transports, one event shape

`_stream_with_tools` branches once:

- **API models** — pass `tools=[schemas]` to the connector; it owns the native tool loop.
- **Scraper (Qwen)** — `_scraper_tool_loop`, because *"The browser scraper has no native function-calling API — tools are invoked by emitting an `<action>` JSON block the model has been primed to use."*

### 17.1 `_scraper_tool_loop`

Per round: stream → `SkillParser.feed()` → forward `text`, collect `tag_found` → `engine.process_tag(..., namespace="study")` in a thread → `build_tool_feedback` → next round with `[TOOL RESULTS]`.

Session continuity:

```
# Round 0 passes NO id so the service creates a real upstream chat and
# reports it via a `meta` event — we adopt that id for every later round.
# (Fabricating an id would point at a conversation that does not exist.)
loop_chat_id = ev.get("chat_id") or loop_chat_id
loop_parent = ev.get("parent_id") or loop_parent
```

> [!TIP]
> Round 0 carries the full directive + schemas; later rounds send **only** the tool results:
> *"the browser session already holds the system context, so re-sending it each round would drown the model in repeated prose."*
>
> Files ride **only** round 0: *"re-uploading the same attachment every round is pure waste (and on Qwen it stacks duplicate copies into the conversation)."*

Text is held back from the parser before emission so a partial tag never flashes on screen. `parser.flush()` after the stream catches a trailing tail.

Loop ends when a round yields no `tag_found`, or at 12 rounds (logged, last text stands).

### 17.2 `_study_tool_sections` — why the harness was removed

```
# Previously this prepended get_tools_prompt_section(), which dumps the
# whole agent harness (ask_user, chat_title, critique, execute_command,
# spawn_agent, MCP, load_tool stubs) and THEN appended study tools. Study
# mode advertised chat_title + agent tooling it has no handlers for.
```

Study mode now exposes exactly `get_study_tool_schemas()` — the study-gated set plus the shared allow-list — so the prompt-injected and native paths cannot disagree.

### 17.3 Belt-and-braces title stripping

`_STUDY_SYSTEM` says *"Never output a chat_title block"*, and `_strip_title_tags` still removes `<chat_title>…</chat_title>` and stray `<action>`/`<tool_call>` tags from every `answer` event. Three layers: prompt, `layout_mode="study"` handler suppression, regex strip.

***

## 18. Attachment routing — the silent-drop bug that got fixed

```python
_DIRECT_READ_BACKENDS  = {"gemini", "groq", "mistral", "openai"}      # inline bytes, take paths
_SCRAPER_FILE_BACKENDS = {"deepseek_scraper", "qwen_scraper"}          # Playwright upload, take paths
```

```
# Scraper transports (deepseek_scraper) upload via Playwright: the
# connector's stream_chat() iterates `files` and calls engine.upload_file()
# on each local path. Hand them the paths directly.
#
# Without this branch the call fell through to _upload_attachment(), which
# has no scraper case and therefore hit the Qwen OSS uploader as its
# catch-all — returning a Qwen file id that DeepSeek's SPA can never use.
# The image was silently dropped and AI OCR saw no page at all.
```

`_upload_attachment` has explicit branches for `deepseek`, `zai`, `gemini_web`, `mimo`, `mistral_web`, `kimi`, then falls through to the Qwen uploader — and **logs loudly** when it does:

```
logger.warning(
    "study attach: backend %r has no explicit upload path — trying the "
    "Qwen uploader as a fallback, which is wrong for any non-Qwen "
    "transport. Add a branch in _upload_attachment/_resolve_attachments.",
    backend,
)
```

`_resolve_attachments` then shapes the return per backend: `ref_file_ids` for all, `ref_files` for `zai`/`gemini_web` (`id`/`blob`), `mimo` (`fileUrl`), `mistral_web` (`url`), `None`/Qwen (whole object). DeepSeek/Kimi take the id alone.

> [!IMPORTANT]
> `_persist_attachments` accepts a raw base64 string or a `data:` URL, rejects >20 MB, and **rebuilds the filename from the index**: `safe = f"att-{i}{ext}"` where `ext` comes only from `Path(name).suffix`. *"Keep only the extension from the client name — never trust the path."*

***

## 19. Note AI — edit via tool, never via chat text

`_NOTE_EDIT_SYSTEM` (verbatim core):

```
- Apply the requested task with the study_note tool. Never paste the whole
  note back as chat text.
- Prefer surgical actions: 'append' to add at the end, 'replace_section'
  to rewrite one heading, 'update' only when the entire body must change.
```

For edit actions, `tools` is filtered to **just** `study_note`:

```python
tools = [s for s in _study_tool_schemas()
         if s.get("function", {}).get("name") == "study_note"]
```

For `ask`, `tools = []` — it's a prose answer, no edit.

**Pre-sync** before every note AI call: if the editor body differs from the saved file, write the editor's version first.

```
# The editor may hold newer text than the file. Bring the file up to date so
# a tool edit applies to exactly what the user sees, never a stale body.
```

Done event differs by mode: `{type:"done", text: <answer> if ask else "", edited: not ask, note_id}`.

`_NOTE_ACTIONS` has 12 actions: `ask, refactor, redesign, organize, enhance, summarize, expand, clarify, fix-grammar, add-examples, flashcards, questions`. Unknown action → `enhance`.

### 19.1 `_NOTE_SYSTEM` and the fence problem

```
Return ONLY the rewritten note as markdown. Never wrap the whole note in a
code fence.
```

Despite that, `_strip_note_fence(text)` exists to unwrap it — `startswith("```")` → drop first line → drop trailing ` ``` `. It is defined but **never called** in the read file. Dead defensive code.

***

## 20. Saved AI notes

`POST/PATCH/DELETE /doc/{id}/notes` — stored in `meta["ai_notes"]`, the same store the reader already loads (*"so the frontend needs no second fetch"*).

`study_add_note` stores the **full thread** (`messages`, last 40, each ≤60k) so a saved note shows the whole conversation, not just the last answer. Also `page`, `rects` (validated), `text` (≤4k), `action`, `question` (≤2k), `answer` (≤60k), `model`, `scope`, `created`.

***

## 21. Findings

> [!CRITICAL]
> **1. `_stop_study_index`-style pause/stop can leave the job's `status` at `running` if the process dies** between `request_stop` and the pass's own settle. `status_for` downgrades it to `paused` on next read (doc 19 §13.2), so the UI recovers — but the file itself stays wrong until then. Not a new bug; noting the interaction.

> [!WARNING]
> **2. `_ensure_academic_path` is dead code** — never called, encodes the obsolete nested semester→subject→topic model. §12.

> [!WARNING]
> **3. `_strip_note_fence` is dead code** — defined, never called, despite `_NOTE_SYSTEM` explicitly telling the model not to fence. §19.1.

> [!WARNING]
> **4. `study_list_docs` / `study_collections` / `study_search_stats` / `study_graph` / `_retag_docs` glob and parse meta files without `_valid_id`.** §5. They mostly validate downstream, but the pattern is inconsistent with the rest of the file.

> [!WARNING]
> **5. `_OCR_MODES` comment contradicts its own default.** The tuple docstring says `"auto"` only touches blank pages, but `_DEFAULT_OCR_MODE = "never"` with a longer comment above it saying uploads never OCR. The effective behaviour is correct; the inline comment is stale. §2.

> [!NOTE]
> **6. `_export_markdown` and `study_add_note` share an `ai_notes` schema with no shared constant** — field names are duplicated as string literals across two files. §14.

> [!NOTE]
> **7. `_read_json` in `study_graph` swallows every exception with a warning,** including a permission error. A corrupt `notes.json` silently yields an empty note layer rather than surfacing.

> [!NOTE]
> **8. `_stream` (the non-tool path) duplicates most of `_stream_with_tools`'s API-connector branch.** The difference is only `tools` and the `TypeError` fallback depth. Consolidation candidate, not a bug.

No code changed in either file.

***

## 22. Cross-references

- [[19-engine-library-index]] — `run_job`, `request_stop`, `is_stop_requested`, `status_for`, `job_exists`, `has_embeddings`, `pages_needing_ocr`, `delete_job`, `_text_path`
- [[18-server-settings-route]] — sibling router, same `asyncio.to_thread` + atomic-write discipline
- [[07-engine-config]] — `LIBRARY_*` dirs, `get_all_models`, `get_model_config`
- **`engine/library_ingest`** — `clean_meta_title`, `convert_to_pdf`, `extract_pdf`, `pdf_tool`, `make_thumbnail`, `thumb_path`
- **`engine/library_catalog`** — `upsert_doc`, `remove_doc`, `count`, `rebuild_from_meta`, `docs_in_topic`, `OTHERS`
- **`engine/library_rag`** — `context_for`, `context_for_docs`, `build`, `remove`, `has_index`
- **`engine/library_search`** — `search`, `index_document`, `remove_document`, `rebuild_all`, `indexed_doc_ids`
- **`engine/library_upload`** — `staging_path`, `create_job`, `load_job`, `list_jobs`, `set_phase`, `set_doc_id`, `finish`, `is_cancelled`, `request_cancel`, `mark_active`, `mark_inactive`, `status_for`
- **`engine/study/context`** — `StudyContext`, `set_study_context`, `reset_study_context`
- **`engine/tools_loader`** — `get_study_tool_schemas`
- **`server/api/routes/study_notes.py`** — `_find`, `_read_body`, `_write_body`, `_read_index`
- [[17-server-chat-route]] — `build_study_turn` is called from `/api/chat` when `layout_mode == "study"`; `_study_grounding` rides the API payload only

> [!IMPORTANT]
> Three invariants hold these files together: **(1)** `_valid_id` before any disk touch; **(2)** every meta mutation goes through `_update_meta` under the cross-process lock; **(3)** a detached background job is never killed by a client disconnect — only by an explicit stop request. Break any of the three and you get path traversal, lost writes, or "AI OCR stopped at one page" respectively.
