# 33 — `online_search.py` + `dil_parser.py`

> [!INFO] Why this file exists
> Priorities 1 and 2 on the README's coverage list. `online_search.py` (1,069L) is the tool behind `web_search` / `web_fetch` / `openweb`; `dil_parser.py` (989L) is the server-side half of the Rich UI component language, and the one handler module that was only ever covered through its JS twin.
>
> Read end to end: `tools/online_search/scripts/online_search.py` (1,069), `engine/skills/handlers/dil_parser.py` (989).

***

## 1. `online_search.py` — the fetch pipeline is good; the URL gate is missing

The three-stage fetch design is sound and each stage has a stated reason:

| Stage | Mechanism | Why |
|:--|:--|:--|
| 1 | `curl_cffi` with browser impersonation, rotated across 5 profiles | Raw urllib/httpx send Python's fixed TLS fingerprint, which Cloudflare/Akamai flag **at the handshake** — before any HTML is served |
| 2 | `trafilatura` markdown extraction, falling back to a DOM scorer | trafilatura is benchmark-best F1 and emits clean markdown for an LLM |
| 3 | Playwright render — **only** when a JS challenge is detected | ~200 MB/instance; `_RENDER_LOCK` serialises it |

`_classify_failure` is the part that makes stage 3 safe. It buckets every attempt into retryable (`js_challenge`, `rate_limited`, `error`) vs fatal (`dns`, `refused`, `timeout`, `ssl`, `invalid_url`), and the caller **returns immediately on fatal** rather than burning five fingerprints and a browser launch on a dead host. `_looks_like_js_challenge` also treats a tiny 403/503 body as a challenge wall, which is right — a 400-byte Cloudflare page is never content.

### 1.1 Arbitrary URL fetch with no host or scheme validation

```python
def _normalize_url(raw_url: str) -> str:
    ...
    if url.startswith("//"):
        url = "https:" + url
    elif not parsed.scheme:
        url = "https://" + url
    return url
```

There is no allowlist and no private-address block. `parsed.scheme` is only used to decide whether to *prepend* `https://` — an existing scheme is passed through untouched.

`fetch_webpage_content` is reached from `fetch_specific_urls(urls)`, which is reached from `handle_online_search`, whose `urls` attribute comes **straight from the model**. So the model can ask Sable to fetch:

| Target | Result |
|:--|:--|
| `http://127.0.0.1:61770/api/settings/...` | Sable's own API, rendered as text and handed back |
| `http://169.254.169.254/latest/meta-data/` | cloud instance metadata, on any cloud host |
| `file:///home/sifat/.ssh/id_rsa` | `curl_cffi` supports `file://`; scheme is non-empty so it passes `_normalize_url` unchanged |

The third one is the sharpest: it needs no network at all, and `truncate(..., 10000)` returns the first 10 KB of whatever it reads.

> [!CAUTION] Ledger #81 — SSRF, model-directed
> This is not a classic unauthenticated SSRF (the model has to be asked), but the model can be *told* to do it: any page fetched through this same tool is wrapped by `wrap_untrusted` yet still reaches the model as text, and a prompt injection in a page is exactly the input that would make it call `web_fetch` on an internal URL.
>
> The fix belongs in `_normalize_url`: an explicit `{http, https}` scheme allowlist, then resolve the host and reject loopback / link-local / RFC1918 / `169.254.169.254` unless an env var opts in. `engine/skills/handlers/common.py` already ships `safe_under()` for paths; there is no equivalent for URLs.

### 1.2 `save_research_state` is a bare write with no lock

```python
def save_research_state(path: str, state: dict[str, Any]) -> None:
    Path(path).write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
```

Violates README convention #2 (atomic writes) and #3 (`exclusive_lock` for read-modify-write on a shared store). The research state is exactly that — `research_mark` does load → mutate → save, and `main`'s `--fetch-urls` path does load → filter → fetch → record → save. Two concurrent calls race and one write is lost.

`load_research_state` validates only `isinstance(data, dict) and "topics" in data`. A file with `{"topics": "oops"}` passes and then `state["topics"].setdefault(...)` raises `AttributeError` in `_record_queries` — not caught there.

### 1.3 `infer_time_filter` matches substrings, not intent

```python
DAY_HINTS = ("today", "latest", "breaking", "this morning", "right now", "currently")
if any(kw in q_lc for kw in DAY_HINTS):
    return "day"
```

`"how do I download a file today"` gets a `day` freshness filter applied, which silently drops every older result. `"currently"` is in the day list, so `"how is currently-implemented X defined"` also gets filtered. Low severity — a filter narrows results, it does not corrupt them — but it is an implicit behaviour the caller never asked for.

### 1.4 Smaller items

- `_playwright_fetch` closes the `browser` but never the `context`; inside `with sync_playwright()` that is usually fine, but a leaked context can hold the profile lock on a retry.
- `comprehensive_web_search` builds a ` ```sources ` fence at the top of its context string. The `handle_online_search` caller then wraps the whole thing again with `wrap_untrusted` — nested fences are a known source of markdown-parser confusion.
- `TextExtractor.get_text` joins chunks with `" "` after already appending `"\n"` for block tags, so `<p>a</p><p>b</p>` yields `"a \n b \n"` — the newlines survive the `[ \t\f\v]+` collapse but not the `\n\s*\n+` one, so paragraph breaks usually collapse to a single newline.
- `image_search_only` filters out entries with no `image` **after** truncating to `max_results` — a page of ten results where two lack images returns eight, not ten.

***

## 2. `dil_parser.py` — the most heavily-annotated file read so far

Every non-obvious line carries a comment naming the exact symptom it was written to kill. That is not decoration; it is the file's actual value, because almost every behaviour here is a fix to something that produced visibly-wrong output. Four examples worth preserving:

| Construct | The bug it fixed |
|:--|:--|
| `_RAW_TEXT_TAGS = ("style", "script")` | Brace-tokenising CSS (`.a{fill:red}`) turned every rule into a `{expr}`; `_eval` couldn't resolve it, returned a `("raw", …)` tuple, and stamped `__expr__` on the node — which made `_svg_subtree_has_expr()` true, blocked SVG flattening, and forced the client to rebuild `<style>` children as SVG `<text>`, destroying the stylesheet |
| `_split_top` with multi-char separators | The old one-char compare split `tab === "A"` on `=` into four pieces; the comparison handler bailed on `len(parts) == 2` and the expression fell through to `_literal` — a truthy tuple. Net effect: **every `{#if}` branch rendered at once** |
| `_display` unwrapping `("raw", src)` | `str()` on the raw tuple leaked the Python repr into the output — `('raw', 'fill:none;stroke:#8b95a7')` appeared as visible text inside a drawing |
| `coalesce`'s `at_start` flag | An unconditional `.lstrip()` on every flush ate the space after any `{expr}` node: `{clicks} mode-switch{cond ? "" : "es"} logged.` coalesced to `0mode-switcheslogged.` |

The `parse_if` / `parse_each` design is also correct in a way that is easy to get wrong: both emit a **re-evaluable** node (`__if__` with every branch and its raw condition; `_rerender` for loop bodies) rather than baking the parse-time winner. The comment says why — collapsing to the winner threw the other branches away, so `{#if tab === "B"}` could never appear no matter what the user clicked.

### 2.1 Self-closing `<script/>` bypasses the drop

```python
m = re.match(r"<\s*(%s)\b" % "|".join(_RAW_TEXT_TAGS), tag_src, re.I)
if m and not tag_src.rstrip().endswith("/>"):
    ...
    if m.group(1).lower() == "script":
        pass          # drop <script>, body, close tag
```

The drop is deliberate and the reasoning is stated: *"The server has no DOM to prune against, so shipping the element at all leaves the client sanitizer as the only thing between it and execution. A generated diagram never needs one."*

But the `and not tag_src.rstrip().endswith("/>")` guard excludes self-closing tags from that whole branch. `<script/>` therefore falls through to the normal tag path and **is emitted as a `script` node** in the tree.

> [!WARNING] Ledger #82 — conditional on the client
> Whether this executes depends entirely on how `SableComponents.render` and DOMPurify treat a `script` node with no children — which I have not read. What is certain is that the one path the author explicitly hardened is bypassable by typing one `/`, and the comment's own reasoning ("a generated diagram never needs one") applies just as much to `<script/>` as to `<script>`.
>
> Fix is a one-line reorder: check `m.group(1) == "script"` **before** the self-closing guard.

### 2.2 `+`/`-` associativity

```python
for op in ("+", "-", "*", "/", "%"):
    parts = _split_top(e, op)
```

Splitting on `+` first means `a + b - c` parses as `a + (b - c)` rather than `(a + b) - c`. Pure `+` and pure `-` chains are fine (left-to-right within one split); only a mixed `+` then `-` is affected, and for ordinary numbers the result is identical. Noted for completeness, not actioned.

### 2.3 `_loads_loose` mangles values containing apostrophes

```python
candidates = [
    s,
    s.replace("'", '"'),                                       # <- naive
    re.sub(r"([{,]\s*)([A-Za-z_]\w*)\s*:", r'\1"\2":', s),
    re.sub(..., s.replace("'", '"')),
]
```

For `{name: 'it's fine'}`, candidate 2 becomes `{name: "it"s fine"}` — invalid. All four candidates fail and the function returns `{}` (or `[]`). The caller gets an empty object rather than the data. Degrades safely, but silently.

### 2.4 The `state` dict carries three kinds of internal key

`parse` returns `(nodes, state)` where `state` mixes user state with bookkeeping:

| Prefix | Meaning |
|:--|:--|
| `__decl__<name>` | source of a bare `const x = …` so the client can re-evaluate |
| `__setter__<name>` | the setter half of `useState` |
| `__hook__<name>` | `{hook, args}` for hooks the client resolves at mount |

This is documented in the code and works. Worth naming only because it means `state` is not a plain key/value store — anything iterating it must filter these, and nothing in the file says that at the return site.

***

## 3. Findings

| # | Finding | Severity | Status |
|--:|:--|:--|:--|
| 81 | `online_search._normalize_url` has no scheme allowlist and no private-address block — model-directed fetch reaches `file://`, loopback, and cloud metadata. | 🔴 high | **not fixed** |
| 82 | `dil_parser`: self-closing `<script/>` bypasses the raw-text drop and is emitted as a node. | 🟠 medium (conditional) | **not fixed** |
| 83 | `online_search.save_research_state` — bare `write_text`, no lock, on a read-modify-write store. `load_research_state` accepts a non-dict `topics`. | 🟡 low | **not fixed** |
| 84 | `online_search.infer_time_filter` substring-matches, so `"download a file today"` silently gets a `day` filter. | 🔵 cosmetic | **not fixed** |
| 85 | `dil_parser._loads_loose` mangles any value containing an apostrophe; `+`/`-` chains associate right. | 🔵 cosmetic | **not fixed** |

One 🔴 — and it is the first in this series that is a genuine security boundary rather than a correctness bug. `dil_parser.py` is otherwise the best-documented file read so far; its one gap is a guard clause in the wrong order.

***

## 4. What this changes upstream

- `documentation/README.md`: coverage tables, ledger #81–#85, priority list.
- [[29-engine-skills-handlers]]: `handle_online_search` and `handle_openweb` are documented there as the callers; this document covers the pipeline they call into.

**Coverage after this batch:** Python ~62,150 → ~64,200 / 122,907 = **52.2%**. Overall ~68,550 → ~70,600 / 200,884 = **35.1%**.

**Next on the list:** `tools/code_editor.py` (1,017), `engine/catalog.py` + `upload.py` + `sessions.py` + `ingest.py` (~1,810), `engine/research/engine.py` (974).