# 20 — `engine/ui_format.py` (1,032 lines)

> [!IMPORTANT]
> The Rich UI block parser. The model writes `<render_component>…</render_component>` markup; the stream parser hands the body here; this module produces the node tree the frontend renders. **No skill lifecycle, no middleware, no tool feedback, the turn never pauses.**

**Read depth:** full file, line 1 → 1,032.

***

## 1. Two input modes

| Mode | Input | Path |
|:--|:--|:--|
| **Markup** (preferred) | JSX-like body the model writes | DIL parser → legacy `HTMLParser` fallback |
| **JSON** (legacy) | `components=[...]` attribute or a bare JSON body | `_parse_components` + `_normalize` |

Media is resolved **here** so the frontend only ever sees concrete sources — except image *queries*, which are deliberately deferred (§6).

***

## 2. Limits

| Constant | Value |
|:--|--:|
| `MAX_COMPONENTS` | 8 |
| `MAX_IMAGES_PER_COMPONENT` | 8 |
| `_MAX_NODE_DEPTH` | 20 |
| `_MAX_INLINE_FILE_BYTES` | 8 MB |
| `ALLOWED_TYPES` | `image, card, table, callout, chart, flow, keyvalue, layout` |

Depth is enforced in `_convert`, `_resolve_media`, `_walk_nodes`, `_svg_node_to_markup` and `_svg_subtree_has_expr` — every recursive walker has its own guard.

***

## 3. SVG handling — the subtle part

SVG inside a component block can be written three ways, and the module has to make all of them produce a `markup` string (because `R.svg` on the client reads `markup`).

### 3.1 Serialization

`_svg_node_to_markup(node, depth)` rebuilds a nested node tree into `<svg>…</svg>`. Rules:

- `_SVG_SKIP_ATTRS` (`kind`, `type`, `children`, `content`, `markup`, `__rawtype__`) are never re-emitted
- `_SVG_VOID` elements (`circle`, `path`, `rect`, `stop`, `use`, …) self-close when childless
- `__text__` nodes are escaped via `_svg_escape_text`
- `<script>` is dropped unconditionally — *"Defence in depth: never serialize a `<script>`, whatever slipped through."*

> [!CRITICAL]
> `_SVG_RAW_TEXT = {"style", "script"}` children are emitted **verbatim**, not escaped:
> ```
> # Raw-text element (<style>/<script>): its content is CSS/JS, not markup.
> # Escaping it corrupts the sheet — `>` becomes `&gt;`, `.a>b{}` stops
> # parsing, and a `content:"<"` declaration silently mangles. Emit verbatim.
> # Safety is the sanitizer's job, not the serializer's.
> ```
> That is the correct division of labour, and it means **the client sanitizer is load-bearing**. If `R.svg` ever stops sanitizing, inline `<style>` in an SVG becomes a CSS injection vector.

### 3.2 `_resolve_svg_node` — don't bail on `text`

```
# NOTE: do NOT bail on a `text` key. The parser flattens
# `<svg><text>label</text></svg>` into `{kind:svg, viewBox:…, text:"label"}`
# — that `text` is SVG *content*, not an alternative to `markup`, and bailing
# on it dropped the whole drawing.
```

A node "looks like SVG" if it has children, any of `viewBox`/`xmlns`/`width`/`height`/`preserveAspectRatio`, or non-empty `text`.

### 3.3 The expression freeze

`_svg_subtree_has_expr(obj)` walks the subtree looking for any `__expr__*` key, **skipping `_SVG_RAW_TEXT` containers entirely**:

```
# Never scan raw-text containers. CSS/JS legitimately contains `{` and
# `}`; if any of it ever got stamped `__expr__`, treating it as a live
# expression would (a) freeze the drawing onto the client's
# `buildSvgTree` path and (b) make `_resolve_svg_node` refuse to build
# `markup`, so the SVG never reaches the sanitizer at all.
```

If any expression is found, `_resolve_svg_node` **returns without building markup**. Rationale:

> *"`_svg_node_to_markup` writes the baked attribute values and drops the `__expr__` keys, so every `{expr}` coordinate freezes at its parse-time value and the drawing can never move. Leaving the tree intact lets the client re-evaluate."*

So: static SVG → `markup` string; dynamic SVG → live node tree + `__expr__` keys. Two rendering paths on the client, chosen by whether the drawing moves.

***

## 4. Markup pre-processing — React → HTML

The model writes React-flavoured syntax because the system prompt shows it that. `HTMLParser` can't read any of it, so `_preprocess_markup` runs four passes, each brace-depth aware via `_ExprScanner`:

| Pass | Removes |
|:--|:--|
| `_strip_declarations` | `{@body ...}`, `{@state ...}`, `{@let ...}`, `{@const ...}` |
| `_strip_handlers` | ` onX={...}` (any `on` + capital) |
| `_rewrite_braces` | `{expr}` → plain text; `attr={expr}` → `attr="…"`; `{{json}}` → `attr="{{…}}"` |
| `_collapse_text_leaves` | `<badge>TEXT</badge>` → `<badge text="TEXT" />` |

`_ExprScanner._consume_braced` distinguishes `{…}` from `{{…}}` by peeking two chars, and counts depth so nested braces don't terminate early. It returns `"json:<inner>"` or `"expr:<inner>"`.

`_simplify_expr` collapses a leftover expression to a static string: a number stays, a ternary `a?b:c` yields `b`, a quoted string is unquoted, everything else becomes `"—"`. That em-dash is what a client that doesn't re-evaluate will display.

> [!TIP]
> `attr={expr}` handling uses `re.search(r"(\w[\w-]*)=$", prefix)` on the already-emitted output, then **truncates `out` back to before the attribute name** and re-emits it quoted. That's why it works on attributes mid-tag without a tokenizer.

> [!CRITICAL]
> **Bug — `_collapse_text_leaves` regex matches nothing.** Line ~318:
>
> ```python
> pattern = re.compile(
>     r"<(" + tag_alt + r")(\s[^<>]*?)?>(?P<inner>[^<>]*?)</>",   # ← `</>`
>     re.IGNORECASE | re.DOTALL,
> )
> ```
>
> The closing token is `</>` — a literal, invalid tag — instead of `</\1>` or `</(?:tag_alt)>`. It cannot match any valid markup, so the whole optimization is dead: every `<badge>text</badge>` goes through the generic `_convert` path (which builds a `text` child and then sets `props["text"]`). Downstream handles it, so **nothing visibly breaks** — this is a silent no-op, not a render bug.
>
> Already flagged in [[15-deep-dive-corrections]]. Still present. **Not fixed** — fixing it changes which nodes reach `_convert` and I won't touch render behaviour unasked.

`_JS_EXPR_RE = re.compile(r"[{]([^{}]*)[}]")` is declared at module level and **never used** — dead, superseded by `_ExprScanner`.

***

## 5. Parsing

`_parse_markup` tries **DIL first**:

```python
from engine.skills.handlers.dil_parser import parse as _dil_parse
nodes, state = _dil_parse(markup)
if nodes:
    return _strip_internal(nodes), state
```

If DIL yields nothing (exception or empty), it falls back to `_UIParser` (an `HTMLParser` subclass) over `_preprocess_markup(markup)`, so older markup keeps working.

### 5.1 `_UIParser`

- `_LEAF_TAGS` are never pushed on the stack — *"so a missing `/` cannot swallow the rest of the document as children"*
- `handle_endtag` searches **backwards** through the stack for a matching tag and truncates there, so an unclosed `<row>` doesn't corrupt the rest of the tree
- `handle_data` keeps only non-whitespace text

### 5.2 `_coerce`

- `_JSON_ATTRS` → `json.loads`, falling back to the **raw string** on failure (not an error)
- `_BOOL_ATTRS` → true for `1/true/yes/on/""` (a bare attribute value is `None` → `True` before this)
- everything else → string

### 5.3 `_convert`

Maps tag → `kind` via `_KIND_MAP` (40 entries: `img`→`image`, `card/panel/section`→`box`, `h1..h4`→`title`, `b/strong`→`text` with `_INLINE_STYLE` weight, `select/radio/options`→`choices`, `collapse/accordion/switch`→`toggle`, …).

Text handling:
- texts + kids → text becomes the **first child**
- texts, no kids, no `text`/`value` prop → text becomes the `text` prop
- an existing `text`/`value` prop always wins over body text

A single top-level `<ui>`/`<render>`/`<layout>` wrapper is unwrapped and its `state` attribute (JSON-coerced) becomes the block's initial state.

### 5.4 `_strip_internal` keeps `__expr__`

```
# NOTE: `__expr__<prop>` keys are deliberately KEPT. The parser evaluates a
# dynamic prop once, server-side, which freezes it — but the raw source is
# shipped alongside so the client can re-evaluate it against live state on
# every render (see `hydrate()` in web/js/render-components.js). Stripping
# them here is what made `{running?"success":"danger"}` never change.
```

So every dynamic prop ships **twice**: the baked value and the `__expr__` source. The client picks. Same philosophy as the SVG freeze decision.

***

## 6. Media resolution — non-blocking by design

This is the most important architectural decision in the file.

> [!CRITICAL]
> ```
> # Resolving an image means a network search, and this runs inside the SSE
> # generator — so doing it here blocked the entire chat stream for up to
> # `REQUEST_TIMEOUT` seconds per image, serially. Instead we ship the query
> # untouched plus `pending: true`; the browser fetches it asynchronously (all
> # queries in parallel) against `/api/search/image`, drawing a placeholder in
> # the meantime.
> ```

`_resolve_media(obj, depth, fetch=False)` — the **default is `fetch=False`**, so `parse_ui_block` never hits the network. `query` images get `pending: True` and are resolved client-side against `/api/search/image`, all in parallel.

`fetch=True` exists (and would block) but nothing in this file passes it. Treat it as dormant.

### 6.1 Per-node isolation

```
# Each node resolves in isolation — a dead image query or a bad local
# path must NOT abort the whole block's parse (which used to drop the
# entire card).
```

`try/except: pass` per node. One bad `<img>` costs you that image, not the card.

### 6.2 `_resolve_image_node` precedence

```
images (JSON string or list, local paths inlined)
  → src (local path → data URI)
    → file (→ data URI)
      → query (→ pending, or fetch if asked)
```

When `images` is present it also sets `src = _first_src(images[0])` if no `src` exists, so a component that only understands `src` still works.

### 6.3 Local files

`_file_to_data_uri(path)` — `expanduser`, `is_file`, `stat().st_size <= 8 MB`, `mimetypes.guess_type`, base64 → `data:<mime>;base64,…`. Returns `None` on **any** failure, and every caller falls through (`_file_to_data_uri(p) or p` for image entries — i.e. the raw path is kept rather than dropped).

`_is_local_path(s)` = truthy and not starting with `http://`, `https://`, `data:`, `//`, `blob:`.

### 6.4 The image cache — why it exists

```
# A UI block streams in as a SERIES of partial events, and the server re-parses
# the whole block on every one (that is the only way to handle nodes appearing
# mid-stream). Without a cache, each re-parse fired a FRESH SearXNG search, so
# the resolved URLs changed between partials — the client tore down the old
# <img> and drew a different picture, which read as "the image loads, then
# instantly gets swapped for another".
```

| Constant | Value |
|:--|--:|
| `_IMG_CACHE_TTL` | 600 s |
| `_IMG_CACHE_MAX` | 256 entries |

Guarded by a `threading.Lock` (not asyncio — this can be called from a worker thread). Callers get **copies** of the rows *"callers mutate freely"*. Empty results are deliberately **not** cached:

> *"a transient SearXNG failure should recover on the next partial, not be frozen for ten minutes."*

Eviction is a cheap FIFO — `min()` over the timestamp key.

### 6.5 Provider choice: sable vs SearXNG

`_image_rows_for(query, want)` picks `sable` (the Electron fused multi-engine index) when `sable_available()` is true, else SearXNG.

> [!TIP]
> The availability signal is a **marker file**:
> ```
> # The marker file that proves the bridge is listening
> # (``~/.cache/sable/search-bridge.json``) exists only while that shell runs.
> # Its presence IS the "am I in the desktop app?" signal, so no extra
> # configuration or user setting is needed.
> ```
> And a sable miss falls through rather than blanking: *"a gated engine should degrade, not blank the card."*

### 6.6 Reserve pool

`_resolve_images` (the `fetch=True` path) asks for `want = max(count, min(count*3, 12))` and returns both:

- `images` — the display slice (`[:count]`)
- `pool` — the full ordered list, so the client can swap in a spare when one 404s or is hotlink-blocked

`_mark_image_pending` clamps `count` to `MAX_IMAGES_PER_COMPONENT` and sets `pending: True`.

***

## 7. Legacy JSON path

`_parse_components(raw)` tries `json.loads` then `ast.literal_eval` — mirroring `ask_user`'s strategy — accepting either a list or a single dict.

`_normalize(comp)` validates: is a dict, has a `type`, type ∈ `ALLOWED_TYPES`, has a `data` dict. Per type:
- `image` with a `query` and no `src`/`images` → `_mark_image_pending` (same non-blocking rule); with neither → dropped as `"image query returned nothing"`
- `layout` → `_walk_nodes(data)` (which also marks pending, never fetches), requires `nodes`/`node`

Rejections are collected as human-readable `reason` strings and surfaced in `payload["dropped"]` — the model's malformed components become diagnostics rather than a silent empty render.

***

## 8. `parse_ui_block` — the entry point

```python
parse_ui_block(content, attrs) -> {"components": [{"type": "layout", "data": {...}}]} | None
```

1. `_strip_fences(content)` — tolerates a wrapping ```` ``` ```` fence
2. If it contains `<` → `_parse_markup` → if nodes, `_resolve_media(nodes)` → payload
3. Else (or if markup produced nothing) → legacy JSON from `attrs["components"]`, or from the body if it starts with `[`/`{`
4. JSON path caps at `MAX_COMPONENTS`, normalizes each, `return None` if none survive
5. `dropped` reasons attached when non-empty

Returns `None` rather than raising, ever. Called **directly by the stream parser** — no skill, no events.

> [!IMPORTANT]
> Because the same block is re-parsed on every streaming partial, `parse_ui_block` must be **idempotent for unchanged input**. Three mechanisms enforce that: the image cache (§6.4), `pending` instead of a network fetch (§6), and `__expr__` sources shipped alongside baked values (§5.4). If you add anything to this function that varies between calls, you reintroduce the "image swaps itself mid-stream" class of bug.

***

## 9. Findings

> [!CRITICAL]
> **1. `_collapse_text_leaves` regex is a no-op** — closing token `</>` instead of `</\1>`. §4. Downstream `_convert` compensates, so no visible break. **Not fixed.**

> [!WARNING]
> **2. `_SVG_RAW_TEXT` (`<style>`) is emitted verbatim** into client markup. §3.1. Correct per the design (*"safety is the sanitizer's job"*), but it makes `R.svg`'s sanitizer a security boundary, not a nicety. Worth an explicit note in the frontend docs.

> [!WARNING]
> **3. `_parse_components` uses `ast.literal_eval` as a fallback**, which will happily parse a Python tuple/`None`/number literal. The result is then rejected by `_normalize` (not a dict) — so it's safe — but it means a model emitting `{...}` Python-dict syntax gets accepted where strict JSON would reject. Behaviour is intentional per the docstring ("mirrors ask_user's strategy"), just worth knowing.

> [!NOTE]
> **4. `_JS_EXPR_RE` is dead code** — declared, never used, superseded by `_ExprScanner`. §4.

> [!NOTE]
> **5. Two leaf sets** — `_LEAF_TAGS` (parser: 24 tags) and `_TEXT_LEAF_TAGS` (collapser: 21 tags) overlap but differ. `_LEAF_TAGS` has `divider/hr/separator/spacer/progress/bar/meter/badge/tag/stat/metric/kpi/chart/code/pre/quote/avatar/input/link/file/attachment/video/audio/icon/img/image/pic`; `_TEXT_LEAF_TAGS` omits `divider/hr/separator/spacer` and `img/image/pic`, adds nothing. Not a bug — the collapser only makes sense for tags whose body is text — but the names invite confusion.

> [!NOTE]
> **6. `_IMG_CACHE` eviction is FIFO on `min(timestamp)`,** not LRU. `_IMG_CACHE` docstring says "cheap FIFO, no need for real LRU here" — accurate, just noting the cache can evict a hot entry while a cold one survives.

No code changed in this file.

***

## 10. Cross-references

- [[04-engine-skills]] — the stream parser that calls `parse_ui_block` directly, `_bt_parity`
- **`engine/skills/handlers/dil_parser.py`** — `parse()`, the primary path, `{#if}`/`{#each}`/`{@body}`
- [[15-deep-dive-corrections]] — the `_collapse_text_leaves` finding, first recorded there
- [[08-engine-search]] — `searxng_image_results`, `sable_provider.sable_available` / `sable_image_search`
- **`web/js/render-components.js`** — `R.svg`, `R._pendingImage`, `hydrate()`, the sanitizer
- **`server/api/routes/search.py`** (or equivalent) — `/api/search/image`, the client's pending-image endpoint

> [!IMPORTANT]
> The design invariant to preserve: **`parse_ui_block` must never block and must be idempotent.** Every non-obvious decision in this file — `pending`, the image cache, `__expr__` preservation, the SVG freeze check — exists to protect that one property against re-parsing on every streaming partial.
