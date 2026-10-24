# 31 — `library_search.py` + `browser/session.py`

> [!INFO] Why this file exists
> Priorities 1 and 6 on the README's coverage list. `library_search.py` is the FTS5 half of the study library — consumed by docs 19 and 22, never opened. `browser/session.py` (1,362L) is the persistent in-process browser that every `browser` tool call drives.
>
> Read end to end: `engine/library_search.py` (~340L), `engine/browser/session.py` (1,362L).

***

## 1. `library_search.py` — FTS5 over page text

The library already writes per-page text to `library/text/<doc-id>.json`. This mirrors it into one SQLite FTS5 table so the whole library answers *"which document mentions Dijkstra, and on what page?"*

**Design choices that are right, and say so in the docstring:**

- The index is **derived data**. Losing it costs a rebuild, never truth. That is why it gets to be destructive-then-insert per document.
- **Every public function opens its own connection.** FastAPI runs sync routes in a threadpool, so a shared handle would race. The comment states this explicitly.
- WAL + `synchronous=NORMAL` per connection, so concurrent reads are lock-free.
- FTS5 availability is **probed, not assumed** — `CREATE VIRTUAL TABLE temp._fts_probe USING fts5(x)` inside a try, with a `LIKE`-scan fallback table of identical shape.

### 1.1 Query construction is injection-safe

```python
def _fts_match(raw):
    tokens = re.findall(r"[\w\u0080-\uffff]+", raw or "")
    return " AND ".join(f'"{t}"*' for t in tokens[:_MAX_TOKENS])
```

Every token is extracted by an allowlist regex and wrapped in double quotes before the `*` prefix operator is appended. FTS5 operators (`OR`, `NEAR`, `-`, `:`, `^`) cannot survive that — a query of `foo OR bar` becomes `"foo"* AND "bar"*`, which is the intended semantics rather than an injection. `_MAX_TOKENS = 12` bounds the expression size. This is the correct way to build an FTS5 query from user text and it is worth copying.

`_like_terms` applies the same tokenisation for the fallback, and `_search_like` builds `lower(body) LIKE ?` with `%term%` parameters — parameterised, no string interpolation.

### 1.2 `bm25()` ordering is correct

```sql
bm25(pages, 0.0, 0.0, 5.0, 1.0) AS score ... ORDER BY score LIMIT ?
```

The weights map to the four columns: `doc_id` and `page` are `UNINDEXED` so they get `0.0`, `title` gets `5.0`, `body` gets `1.0`. SQLite's `bm25()` returns **negative** values where more-negative is a better match, so an ascending `ORDER BY` is correct. The score is then surfaced as-is (`round(float(...), 4)`) — a caller comparing a hit's score across engines would be comparing a negative BM25 to the literal `0.0` the LIKE path always emits, so the `engine` field is the only honest discriminator. Recorded because it is easy to "fix" that ordering and break ranking.

### 1.3 Snippets carry unescaped document text into markup

```python
snippet(pages, 3, '\u0002', '\u0003', '\u2026', 24) AS snip
...
"snippet": (r["snip"] or "").replace("\u0002", "<mark>").replace("\u0003", "</mark>"),
```

The `\u0002`/`\u0003` sentinels are swapped for real `<mark>` tags and the result is handed to the caller as a **string containing HTML**. The surrounding text is raw page text — `_norm()` only collapses whitespace, it does not escape angle brackets.

So a document containing the literal text `<img src=x onerror=alert(1)>` produces a snippet containing that same literal string, wrapped in `<mark>` tags. Whether that is exploitable depends entirely on how the consumer renders it, and **I have not read the consumer.** If it assigns the snippet via `innerHTML`, it is stored XSS from any PDF a user imports. If it renders as text, the `<mark>` tags never take effect and the whole feature is cosmetic.

> [!WARNING] Ledger #68 — conditional on the consumer
> The safe shape is to escape the snippet body and then re-introduce only the `<mark>` tags, or to return a structured `[{text, hit}]` array and let the frontend build elements. The current form forces the caller into one of two bad options: render HTML (XSS) or render text (no highlighting).
>
> Flagged as 🟠 with that condition stated rather than as a confirmed vulnerability, because I have not verified the render path.

### 1.4 A plain table poisons the FTS5 path in another process

```python
if _fts5_available(conn):
    conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS pages USING fts5(...)")
else:
    conn.execute("CREATE TABLE IF NOT EXISTS pages (...)")
```

`_fts5_ok` is cached **per process**. Two Sable processes sharing one `search.db` can therefore disagree:

| Process | FTS5 probe | Creates |
|:--|:--|:--|
| A | unavailable | plain `pages` table |
| B | available | `CREATE VIRTUAL TABLE IF NOT EXISTS pages` → **no-op**, table already exists |

Process B then runs `_search_fts`, whose SQL is `WHERE pages MATCH ?` — against a plain table, SQLite raises `unable to use function MATCH in the requested context`. The exception is not caught in `search()`; it propagates to the route.

Narrow (needs a mixed-capability fleet on one DB) but silent when it happens, and the fix is one line: check `sqlite_master` for the table's `sql` text before choosing the query path, rather than trusting a process-local flag.

### 1.5 Smaller items

- `_fts5_ok` latches on first probe. One transient failure pins the process into `LIKE` mode until restart, with a single `warning` at probe time.
- `index_document` does `DELETE` then `executemany(INSERT)` with one `commit()` at the end — correctly one transaction, so a crash mid-index leaves the old rows intact rather than a half-index.
- `rebuild_all` walks `LIBRARY_TEXT_DIR.glob("*.json")` with per-file `try/except` and counts skips. Correct shape.
- `_rollup` caps `pages` at 8 per document but counts all hits — the sidebar shows a count larger than the page list, which is intentional (it is a summary) but not stated.

***

## 2. `browser/session.py` — one Playwright loop, marshalled

The problem it solves is real and stated plainly in the module docstring: Playwright's async API binds to one event loop, but Sable's skill handlers are **synchronous generators**. So the session runs a single dedicated event loop on a background thread, and every public method marshals its coroutine across with `run_coroutine_threadsafe` and blocks on the result.

```python
def run(self, coro, timeout: float = 60.0):
    loop = self._ensure_loop()
    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    return fut.result(timeout)
```

Browser state — cookies, tabs, network log — survives between tool calls because the browser process outlives the call. This is the right architecture for the constraint, and the whole file is disciplined about routing through `run()`.

### 2.1 `run()` can orphan a coroutine

```python
fut = asyncio.run_coroutine_threadsafe(coro, loop)
return fut.result(timeout)      # raises concurrent.futures.TimeoutError
```

`fut.result(timeout=...)` does **not** cancel the future when it times out. It raises on the caller thread and returns control to the handler — while the coroutine keeps executing on the loop thread, still mutating `self.page`, `self.context`, and the logs.

The default is 60 s; `ensure_started` passes 120. A `screenshot(full_page=True)` on a very long page, a `wait_selector` with a large `timeout_ms`, or `har_replay` on a big capture can exceed it. The handler then reports a failure, the agent retries, and two Playwright operations now interleave on the same page — which is the one thing this design exists to prevent.

> [!IMPORTANT] Ledger #69
> The fix is `fut.cancel()` in an `except TimeoutError` before re-raising, or at minimum a `logger.warning` that the operation is still live. Cancelling a Playwright coroutine mid-await is not free — it can leave the driver in a bad state — but the current behaviour is strictly worse: the caller believes nothing is running.

### 2.2 The stated threading invariant is violated in one place

> *"All Playwright objects are touched ONLY from the loop thread. Every public method funnels through `run()`, so that invariant always holds."*

```python
@property
def alive(self) -> bool:
    try:
        return self.page is not None and not self.page.is_closed()
    except Exception:
        return False
```

`page.is_closed()` is a **synchronous** Playwright call, and `alive` is read from the caller thread — by `ensure_started`, by `status()`, and by `handle_browser` in the skills layer. `is_closed()` happens to be a local flag read with no round-trip, so it does not deadlock today, but it is still a Playwright object touched off the loop thread, and the docstring claims that never happens.

The invariant should either be restated as "no Playwright *I/O* off the loop thread" — which is what is actually true and what actually matters — or `alive` should be routed through `run()`. As written, a future contributor reading the docstring will trust a guarantee the code does not keep.

### 2.3 Logs are written on the loop thread, read on the caller thread

`_on_request` / `_on_response` / `_on_console` fire on the loop thread and append to `self._network_log` / `self._console_log`; `_trim()` does `del self._network_log[:-_MAX_LOG]`. `network_log()` / `console_log()` slice those lists from the caller thread.

`_MAX_LOG = 500` and CPython's GIL makes list `append` and slice-assignment individually atomic, so the realistic outcomes are a torn snapshot (a reader sees 499 entries mid-trim) rather than corruption. Low severity, but it is the same class as §2.2: state shared across threads with no lock and a docstring implying otherwise.

### 2.4 What is genuinely well built

Worth recording, because this file is long and mostly correct:

- **`_settle` is the standout.** The comment explains why `wait_for_load_state()` is useless right after a click — the *current* page is already loaded, so it returns instantly and misses a navigation that fires a beat later. So it polls the URL for a change first, then waits for the new document. Timeouts are swallowed on purpose: *"the action already happened, so a slow load is information, not failure."*
- **`_locator` raises immediately on an unknown `@eN` ref** instead of letting it become a CSS selector Playwright polls for 10 s. The comment names the exact symptom it killed.
- **`_click` swallows the post-dispatch `PlaywrightTimeout`** because Playwright 1.61 deprecated `no_wait_after` — the click may have fired before the timeout, and `_settle` decides whether a navigation actually happened. `find(do="click")` goes further and returns `timed_out: true` so a genuine miss stays visible.
- **`_snapshot` never fails the whole capture over one unreadable frame** — a cross-origin or detached frame is skipped, and the docstring says why omitting is better than pretending.
- **`_screenshot` waits two animation frames then retries once** on the `captureScreenshot` protocol error, because Chromium right after navigation has layout but no composited frame.
- **`_dump_html` raises on a missed selector** rather than falling back to the full page — *"the caller asked for `<h1>`, got `<html>`, and had no signal the element did not exist."*
- **`_scroll` waits two rAFs** before reading `scrollY`, because `mouse.wheel()` resolves before the scroll commits and an immediate read returns the old position.
- **`find(do="fill")` verifies the tag** after filling, because `fill()` is a silent no-op on non-input elements.

Every one of those carries a comment naming the specific failure it prevents. This is the most thoroughly debugged file read in this series so far.

### 2.5 Smaller items

- `self._routes` is written by `_route` and **never read** — dead bookkeeping.
- `_find(do="fill")` calls `loc.fill(...)` and *then* checks the tag, so the no-op fill happens before the `ValueError`. The error is right; the ordering means the message describes an action already attempted.
- `eval` accepts `file:<path>` and reads it with no containment check. Consistent with the tool being a general escape hatch, but it is arbitrary model-directed file read that bypasses the `get_file` handler.
- `wait()` uses `time.sleep` directly on the caller thread — correct, since it touches no Playwright object.
- `unannotate()` builds the coroutine (`self.page.evaluate(...)`) on the caller thread before passing it to `run()`. Creating a coroutine does not execute it, so this is safe — but it will raise on the caller thread if `self.page` is `None`, unlike every other method.

***

## 3. Findings

| # | Finding | Severity | Status |
|--:|:--|:--|:--|
| 68 | `library_search` snippets embed raw document text with `<mark>` tags around it — XSS if the consumer uses `innerHTML`, cosmetic-only if it uses text. Consumer not read. | 🟠 medium (conditional) | **not fixed** |
| 69 | `browser/session.run()` lets `fut.result(timeout)` expire without cancelling — the coroutine keeps running on the loop thread while the handler reports failure. | 🟠 medium | **not fixed** |
| 70 | `browser/session.alive` calls `page.is_closed()` off the loop thread, contradicting the module docstring's stated invariant. | 🟡 low | **not fixed** |
| 71 | `library_search`: a plain `pages` table created by one process makes `MATCH` fail in another that has FTS5 — `CREATE VIRTUAL TABLE IF NOT EXISTS` is a no-op against an existing plain table. | 🟡 low | **not fixed** |
| 72 | `browser/session` network/console logs are appended on the loop thread and sliced on the caller thread with no lock. | 🟡 low | **not fixed** |
| 73 | `library_search._fts5_ok` latches on first probe — one transient failure pins the process into `LIKE` mode until restart. | 🟡 low | **not fixed** |
| 74 | `browser/session._routes` written, never read; `find(do="fill")` checks the tag after filling; `eval("file:…")` reads any path. | 🔵 cosmetic | **not fixed** |

No 🔴 in 1,700 lines. Both files are examples of the codebase at its best — `library_search` is small and correct about its own data model, `browser/session` is the most carefully debugged file in this series. The two 🟠 findings are both about the boundary between a component and its caller, which is exactly where a file that is internally tidy still leaks.

***

## 4. What this changes upstream

- `documentation/README.md`: coverage tables, ledger #68–#74, priority list.
- [[19-engine-library-index]] / [[22-server-study-routes]]: cross-reference added — both consume `library_search` but neither opened it.
- [[29-engine-skills-handlers]]: `handle_browser` documents the 40 action verbs; this document covers the session object underneath them.

**Coverage after this batch:** Python ~58,900 → ~60,600 / 122,907 = **49.3%**. Overall ~65,300 → ~67,000 / 200,884 = **33.4%**.

**Next on the list:** `tools/computer_use.py` (1,549), `tools/online_search.py` (1,069), `engine/skills/handlers/dil_parser.py` (989).
