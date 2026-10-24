# Deep-Read Corrections — `browser_governor`, `response_normalizer`, `inpage`, `ui_format`

> [!INFO] Why this file exists
> Docs `03`–`06` described these four modules from grep output and file sizes. This document records what they **actually do**, read directly. Where it contradicts an earlier doc, this one is correct.
>
> **Caveat:** `get_file` truncates around 8,000 characters. For `browser_governor.py` (36 KB) and `ui_format.py` (36 KB) I read the full header, all module-level constants, and the first several hundred lines of implementation — not 100% of every function body. That is stated honestly rather than implied.

***

# 1. `engine/browser_governor.py` — corrected

## The docstring states the design contract

> *"Two guarantees, enforced for EVERY browser Sable spawns: 1. At most `MAX_BROWSERS` browser instances exist at once, per Sable process. The stable install and a dev tree can both be running, so an in-process semaphore would let each hold its own quota — the cap is kept in a shared slot directory instead. 2. Slots carry a PRIORITY."*

> [!IMPORTANT] Correction to doc 03
> I wrote that `MAX_BROWSERS_GLOBAL` was a machine-wide cap. It is not a cap in the enforcing sense: *"Kept only so the shared cgroup label tree can't grow without bound; it is NOT a per-instance cap and it never kills a browser by itself."*
>
> The only enforced cap is `MAX_BROWSERS` (default **2**) **per Sable process**, matched by `owner_pid`.

## There is deliberately no memory- or idle-based killing

> *"There is deliberately NO memory-based or idle-based killing here. A browser dies when the code that owns it says so (job done -> close), or when it is an orphan (owner gone). Each browser is still tagged with a cgroup v2 label so orphan detection can tell 'Sable spawned this' apart from an unrelated Chromium — but the cgroup carries no `memory.max` and no `oom.group`, so the kernel never OOM-kills a browser tree behind our back."*

This is a significant correction. Doc 03 implied the governor enforced memory limits. It does not — it uses cgroups purely as an **ownership marker**, never as a resource control. The kernel is explicitly prevented from intervening.

## Slot storage is a shared directory, not memory

```python
_RUNTIME  = Path(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir())
_SLOT_DIR = _RUNTIME / "sable-browser-slots"
```

Slot files are `slot-<ownerpid>-<uuid>.json` in `$XDG_RUNTIME_DIR/sable-browser-slots/`, guarded by a single `flock` on `.lock`.

> [!NOTE] Why a directory instead of an `asyncio.Semaphore`
> Because the cap must hold across **processes**. A stable install and a dev tree on the same machine each have their own Python process; an in-process semaphore would let each hold 2 browsers = 4 total. `owner_pid` scoping means each server enforces its own quota without starving the other.

On Windows there is no XDG, so it falls back to `tempfile.gettempdir()` rather than a bogus `C:\tmp`.

## `_is_sable_confined(pid)` — the definitive ownership test

```python
def _is_sable_confined(pid: int) -> bool:
    return "/sable-browsers/" in Path(f"/proc/{pid}/cgroup").read_text()
```

> *"This is the definitive 'Sable spawned this' marker… A process in any other cgroup — a user session scope, a manually-started automation daemon, an outside Chromium — was NOT spawned by Sable and must never be reaped, no matter what its command line happens to contain."*

Command-line matching is spoofable (any Chromium can be launched with `--user-data-dir=<sable path>`). Cgroup membership is not. This is the strongest ownership signal in the codebase.

## Eviction policy — exactly as I wrote it, with the precise rule

```python
def _evict_ephemeral_own_locked(owner_pid):
    # candidates = own slots, has browser_pid, priority == ephemeral
    candidates.sort(key=lambda fd: _slot_last_used(fd[1]))   # oldest first
```

> *"Only touches this process's own slots, so we never kill another Sable server's browser."*

Three conditions in `_try_claim()` when at cap:

| Priority | Behaviour |
|:--|:--|
| `EPHEMERAL` | `return None` — **waits**, never evicts |
| `CHAT` + an ephemeral slot exists | evicts the LRU ephemeral, then claims |
| `CHAT` + all slots are chat | `return None` — **waits** |

> [!WARNING] A chat claim can still fail
> If all owned slots are chat-priority, a new chat claim returns `None` rather than killing a live session. That surfaces as `NoSlotAvailable` → the `no_slot` SSE event → the user must kill a browser manually.

## Failure mode: it allows the launch

```python
except Exception:
    # Can't build the shared table — fall back to allowing the launch
    # rather than deadlocking the app. Better to run than to wedge.
    return Slot(f"unmanaged-{uuid.uuid4().hex[:8]}", None, owner, priority)
```

> [!TIP] Degrade open, not closed
> If `_SLOT_DIR` can't be created (read-only `/run`, permissions), the governor returns an **unmanaged slot** — the browser launches anyway, just uncounted. The alternative (refuse to launch) would make a permissions bug look like a total outage.

## `_pid_alive()` is not `os.kill(pid, 0)`

```python
# os.kill(pid, 0) is a POSIX-only liveness probe. On Windows any os.kill
# that is not CTRL_C/CTRL_BREAK *terminates* the target, so routing a
# liveness check through it would kill the very browser we're testing.
return pid_exists(pid)
```

> [!IMPORTANT] This pattern appears in three files
> Same comment, same reasoning, in `browser_governor.py`, `inpage.py`, and `engine/session.py`. A naive `os.kill(pid, 0)` cross-platform check is a **process-killer on Windows**. Worth remembering if you write new liveness code.

## `find_browser_pids()` — `/proc` scan

```python
def find_browser_pids(user_data_dir: str, *, roots_only: bool = True) -> list[int]
```

Scans every `/proc/<pid>/cmdline`, requires `--user-data-dir=` matching the realpath, and with `roots_only=True` skips any process with `--type=` (Chromium's renderer/GPU/zygote helpers). Returns only process-group leaders — what you want for cgroup placement and group kills.

Returns `[]` on Windows: *"no /proc to scan; slot table + pid liveness are the signals."*

`_proc_age(pid)` reads `/proc/stat` `btime` and `/proc/<pid>/stat` field 22 (`starttime`) divided by `SC_CLK_TCK` — the only reliable way to get process start time without `ps`.

***

# 2. `engine/response_normalizer.py` — corrected

## It is more than a normalizer; it is the format-unification layer

> *"THE single layer between connectors and Sable core. Every connector yields raw provider-specific events. This module converts ALL of them into a unified format."*

### Input types

```
answer, thinking, done, error      — text/lifecycle
raw_function_call                  — native API tool calls (Gemini, OpenAI)
```

### Output types

```
{"type": "answer", "text": ...}                          — tool tags stripped
{"type": "thinking", "text": ...}
{"type": "function_call", "name": ..., "args": {...}}   — canonical
{"type": "done", ...}
{"type": "error", ...}
```

> [!IMPORTANT] Correction to doc 03
> I described three dialects. There are **four**, plus a fifth incremental path:
> 1. Hermes `<tool_call>{JSON}</tool_call>`
> 2. Qwen native `<action>...</action>`
> 3. DeepSeek DSML `<｜DSML｜tool_calls><｜DSML｜invoke>`
> 4. Legacy XML `<tool_calls><invoke name=…><parameter …>`
> 5. **OpenAI-compatible incremental `tool_calls` deltas** — accumulated in `_tc_accum` and flushed on `done`

## The DSML orphan stripper is a three-pass regex cascade

This is the most intricate code in the file.

```python
_P  = r'[|\uff5c]+'        # one or more pipes (ASCII or fullwidth, handles ｜｜)
_PO = r'[|\uff5c]*'        # zero or more pipes
_KW = r'(?:tool_calls|invoke|parameter)'
```

| Pass | Regex | Catches |
|:--|:--|:--|
| 1 | `_ORPHAN_DSML_RE` | Tag-shaped fragments with angle brackets, attrs, optional `</` |
| 2 | `_DANGLING_DSML_RE` | Bare `</\|\|DSML\|\|` with **no keyword** — the keyword was eaten by a prior pass |
| 3 | `_BARE_DSML_MARKER_RE` | Pipe-delimited keyword with **no brackets at all** |
| 4 | `_ORPHAN_TAG_RE` | Bare `</invoke>`, `<parameter ...>` |

> [!TIP] The comment explains the cascade's necessity
> *"Handles every variant in one pass: `<｜DSML｜invoke name="x">`, `</｜DSML｜parameter>`, `<DSML|invoke>` (missing leading pipe), `｜DSML｜parameter` (no angle brackets at all — chunk split ate them), `/｜DSML｜invoke>` (leading `<` consumed by partial-tag hold), stray `>` after keyword."*
>
> The reason it must be **multi-pass** and ordered: pass 1 removes brackets, which can leave pass 3's bare marker behind. Running them in reverse order leaves half-stripped garbage.

## `_partial_tag_suffix()` — longest-first, and the comment says why

```python
def _partial_tag_suffix(text: str, tag: str) -> str:
    """Return the longest suffix of *text* that is also a proper prefix of *tag*.

    Iterates **longest-first** so we hold back the maximum possible
    partial-tag fragment instead of the shortest one.  This prevents
    mid-tag leaks (e.g. ``tool_call>`` or ``_call>``) when a provider
    like Qwen splits ``<tool_call>`` / ``</tool_call>`` across
    chunk boundaries at awkward token positions.
    """
    for i in range(len(tag) - 1, 0, -1):
```

> [!IMPORTANT] Longest-first is the whole correctness argument
> Iterating `range(1, len(tag))` (shortest-first) would hold back only `"<"` when the buffer ends with `"</tool_ca"`, then emit `"</tool_ca"` as prose on the next chunk. Counting down holds the **entire** partial prefix. The direction of the loop is a bug fix.

## Partial-tag buffering state

```python
_action_buffer = ""
_in_action = False
_pending_prefix = ""
```

Three variables carry cross-chunk state: the buffered text, whether a tool block is open, and the held partial tag prefix.

## Debug logging is opt-in and writes per chunk

```python
_debug_on = _os.environ.get("SABLE_DEBUG_CHUNKS", "0") == "1"
```

When on, it writes to **two** files:
- `$SABLE_OUTPUT_DIR/logs/raw_qwen_chunks.txt` — every event
- `../tests/qwen_output.txt` — a stream-delimited dump

> [!WARNING] The default path is hardcoded
> `_os.environ.get("SABLE_OUTPUT_DIR", "/home/sifat/sable_output")` — an absolute home path as the fallback. On any other machine this writes to a path that may not exist; `makedirs` creates it, so it silently succeeds in the wrong place.

## `_parse_json_action()` accepts four key conventions

```json
{"name": "execute_command", "args": {...}}
{"name": "web_search", "arguments": {...}}
{"tool": "...", "params": {...}}
{"tag": "...", "parameters": {...}}
```

```python
name = data.get("name") or data.get("tool") or data.get("tag") or ""
args = data.get("args") or data.get("params") or data.get("parameters") or {}
```

## `_parse_legacy_invoke()` — the `string` hint rules

| `string=` | Behaviour |
|:--|:--|
| `"true"` | Keep raw string verbatim |
| `"false"` | `json.loads`, fall back to string |
| absent | Try `json.loads`, fall back to string |

Identical semantics to `parser.py`'s `_coerce_xml_param()` — deliberately, so both paths agree.

***

# 3. `engine/inpage.py` — corrected

## The lifecycle is documented as a four-case ladder

> *"Lifecycle (Sifat spec, 2026-09-21): 1. ghost already healthy -> reuse; 2. same-process `BrowserManager` live -> adopt its context (shared); 3. marker file (CDP port) from a ghost launched by an earlier process -> try `connect_over_cdp`, fail -> kill + relaunch; 4. profile locked by a live foreign chromium -> Sable server running? bail to API, otherwise kill, clean lock, relaunch."*

> [!NOTE] The docstring names a date and an author
> *"Sifat spec, 2026-09-21"* — this module's design was specified explicitly, not accreted. The four cases are the spec.

## The payload routing rule, stated as a map

> *"Payload routing (the 2026-09-21 size map): `len(message) <= RAW_TEXT_LIMIT` -> raw in-page fetch; `len(message) > RAW_TEXT_LIMIT` -> STS -> OSS `.txt` upload (in-page STS), short message + `files=[file_obj]`."*

```python
RAW_TEXT_LIMIT = 100_000  # safe margin under the ~120k RGV587 punish wall
```

Above 100,000 characters, content is **never sent as text** — it becomes a `.txt` file upload with a short accompanying message. The 100k is a safety margin below the ~120k ban threshold.

## Idle timeout — three attempts, but only before content

```python
_GHOST_IDLE_SECS   = 30.0
_GHOST_MAX_ATTEMPTS = 3
```

> *"Idle timeout: if no event arrives within this window — while connecting or mid-stream — the in-page fetch is aborted and a fresh connection is opened. Only retried while NOTHING has been emitted yet (a stall after partial content can't be replayed without duplicating the answer)."*

> [!IMPORTANT] The retry gate is "no content yet"
> A 30 s stall mid-answer aborts but does **not** retry — replaying would duplicate the partial answer the user already saw. Only a stall before the first byte is retryable.

## `_lock_holder_is_foreign_live()` — a real bug fix, documented

```python
def _lock_holder_is_foreign_live(pid: int) -> bool:
    """True only when the profile lock belongs to a DIFFERENT, live Sable.

    The old check asked ``_server_running()``, which is *always* true when the
    ghost itself runs inside the server — so the ghost refused to reclaim a
    profile its own process held and every ghost launch bailed with
    "profile locked by the running Sable server's browser".
    """
    me = os.getpid()
    chain = _ppid_chain(pid)
    if not chain:
        return _server_running()          # Windows fallback
    if me in chain:
        return False                      # ours
    return any(_looks_like_sable_server(p) for p in chain if p != me)
```

> [!CRITICAL] This is the bug that made the ghost path never work
> `_server_running()` reads `.sable_server.pid` and checks it is alive. Inside the server process, that is **always true**. So every ghost launch concluded "profile locked by the running server" and fell back to the raw API path — meaning the entire ghost engine was dead code on any normal install.
>
> The fix is parent-chain analysis: walk `/proc/<pid>/stat` ppid up to 16 levels. If **our own PID** is in the chain, the lock holder is our own leaked browser → reclaim it. If a **different live** `server.py` is in the chain → genuinely foreign, bail.

`_ppid_chain(pid, max_depth=16)` parses `/proc/<pid>/stat` after the last `)` and takes field 1 as ppid. Returns `[]` when `/proc` is unavailable (Windows), falling back to the conservative old behaviour.

## `_adopt_existing()` — probe the port before connecting

```python
# The marker's port can be dead while a DIFFERENT live browser holds the
# profile (marker written by an older launch, lock taken by a newer one).
# Attempting connect_over_cdp on that dead port used to time out and fall
# into _hard_cleanup(), which SIGKILLs the healthy browser that holds the lock.
if not _port_open(port):
    marker.unlink(missing_ok=True)
    return False
```

> [!IMPORTANT] A stale marker used to kill a healthy browser
> The sequence: launch A writes `.ghost_engine.json{port:9301}`. Launch B takes the profile lock but crashes before writing a marker. Next boot reads A's stale marker, tries CDP on 9301 (dead), times out, calls `_hard_cleanup()` → SIGKILLs **B's healthy browser**. Probing the port first and discarding only the marker avoids the kill.

## `_spawn_ghost()` — health is token + two WAF cookies

```python
for _ in range(16):   # up to 8 s: auth token + WAF cookies bake
    tok = await page.evaluate("() => !!localStorage.getItem('token')")
    names = {c["name"] for c in await self._context.cookies()}
    if tok and {"acw_tc", "isg"} <= names:
        healthy = True
        break
    await page.wait_for_timeout(500)
```

All three must be present: `localStorage.token`, cookie `acw_tc`, cookie `isg`. Failure raises `GhostError("ghost tab not healthy (auth token / WAF cookies missing — stale profile?)")`.

> [!NOTE] `tfstk` is not checked here
> Doc 03 said three WAF cookies are mandatory. `session.py`'s `get_fresh_headers()` waits for `acw_tc` + `isg` only; `tfstk` is mentioned in its comment but not gated on. `inpage.py` gates on the same two.

## `_on_event` — Python exposed into the page

```python
await page.expose_function("__ghostEvent", self._on_event)
```

Playwright's `expose_function` binds a Python callable as a JS global. The in-page fetch loop calls `window.__ghostEvent(ev)` per SSE frame. `_exposed_pages` tracks which pages already got the binding so a re-spawn doesn't double-bind.

***

# 4. `engine/ui_format.py` — corrected

## It is a response format, not a tool — stated in the docstring

> *"This is a RESPONSE FORMAT, not a tool/skill. The model writes markup between `<render_component>` tags; the stream parser detects it, this module parses it to a node tree, and the frontend renders it natively. There is no skill lifecycle, no middleware, no tool feedback — the turn never pauses."*

## Two input modes

| Mode | Example |
|:--|:--|
| **Markup** (preferred) | JSX-like body the model writes naturally |
| **JSON** (legacy) | `components=[...]` attribute, back-compat only |

## Media is resolved server-side, not client-side

> *"Media is resolved HERE so the frontend only ever sees concrete sources."*

| Written | Resolved to |
|:--|:--|
| `<img query="cute cats" count="4"/>` | SearXNG image search |
| `<img src="https://..."/>` | Direct URL |
| `<img file="~/pics/a.png"/>` | Local file → **data URI** |
| `<video src="https://..."/>` | Direct video URL |
| `<link href="..." label="X"/>` | Styled link |

```python
_MAX_INLINE_FILE_BYTES = 8_000_000  # 8 MB cap on inlined local files
```

> [!WARNING] An 8 MB file becomes an ~11 MB base64 data URI
> Base64 inflates by 33%. An 8 MB local image becomes ~10.7 MB embedded in the JSON payload for one UI block. Two of those and a response exceeds most practical limits.

## Bounds and limits

```python
ALLOWED_TYPES = frozenset({"image", "card", "table", "callout", "chart", "flow", "keyvalue", "layout"})
MAX_COMPONENTS          = 8
MAX_IMAGES_PER_COMPONENT = 8
_MAX_NODE_DEPTH         = 20
_MAX_INLINE_FILE_BYTES  = 8_000_000
```

## SVG handling — the most careful code in the file

### `_svg_node_to_markup()` rebuilds raw SVG from a parsed tree

```python
def _svg_node_to_markup(node, depth=0) -> str
```

> *"The renderer's `R.svg` reads `markup`, and rebuild-from-tree is the only way to feed it raw `<svg><circle/></svg>` written inside a component block without shipping a half-parsed node it can't draw."*

### The raw-text escape exemption is a real bug fix

```python
_SVG_RAW_TEXT = frozenset({"style", "script"})
```

> *"Raw-text element (`<style>`/`<script>`): its content is CSS/JS, not markup. Escaping it corrupts the sheet — `>` becomes `&gt;`, `.a>b{}` stops parsing, and a `content:"<"` declaration silently mangles. Emit verbatim. Safety is the sanitizer's job, not the serializer's."*

> [!IMPORTANT] Escaping CSS is a data-corruption bug
> A generic XML serializer escapes `<` and `>` everywhere. Inside `<style>`, `.a > b { }` becomes `.a &gt; b { }` — invalid CSS, the rule silently dies. The child combinator stops working and the layout breaks with no error anywhere.

### `<script>` is dropped unconditionally

```python
if tag.lower() == "script":
    return ""
```

> *"Defence in depth: never serialize a `<script>`, whatever slipped through. The tokenizer already drops them; this catches a hand-built node tree."*

`_SVG_VOID` lists the self-closing SVG elements: `circle, ellipse, line, path, polygon, polyline, rect, stop, use, image, animate, animatetransform, animatemotion`.

`_SVG_SKIP_ATTRS = frozenset({"kind", "type", "children", "content", "markup", "__rawtype__"})` — parser-internal keys that must never become SVG attributes.

## `_resolve_svg_node()` — the `text` key trap

> *"NOTE: do NOT bail on a `text` key. The parser flattens `<svg><text>label</text></svg>` into `{kind:svg, viewBox:…, text:"label"}` — that `text` is SVG **content**, not an alternative to `markup`, and bailing on it dropped the whole drawing."*

## `_preprocess_markup()` — four rewrite passes

```python
def _preprocess_markup(markup):
    s = _strip_declarations(s)   # strip leading const/let/var
    s = _strip_handlers(s)       # strip onX={...}
    s = _rewrite_braces(s)       # {expr} / {{json}} / attr={expr}
    s = _collapse_text_leaves(s) # <badge>TEXT</badge> -> <badge text="TEXT"/>
    return s
```

### `_rewrite_braces()` — a hand-written scanner, not a regex

The class `_ExprScanner` walks the string tracking brace kind (`expr` vs `json`) and nesting depth, then rewrites:

| Input | Output |
|:--|:--|
| `attr={expr}` | `attr="{expr}"` (kept dynamic) |
| `attr={{json}}` | `attr="{...}"` (simplified) |
| bare `{expr}` in text | `{expr}` (kept for client eval) |
| bare `{{json}}` in text | simplified JSON |

> [!TIP] Why a scanner instead of regex
> `attr={a ? {x:1} : {y:2}}` has nested braces of different kinds. A regex cannot match balanced braces; the scanner tracks depth and kind together.

### `_collapse_text_leaves()` — and a visible bug

```python
def _collapse_text_leaves(s):
    tag_alt = "|".join(_TEXT_LEAF_TAGS)
    pattern = re.compile(
        r"<(" + tag_alt + r")(\s[^<>]*?)?>(?P<inner>[^<>]*?)</>",   # ← `</>` with no tag name
        re.IGNORECASE | re.DOTALL,
    )
```

> [!CRITICAL] The close tag in this regex is `</>` — it should be `</\1>`
> The pattern matches `<badge>text</>` — a close tag with **no name**. HTML never produces that. The intent (per the function's own docstring) is `<badge>TEXT</badge> -> <badge text="TEXT" />`.
>
> As written, the regex matches essentially nothing in valid markup, so `_collapse_text_leaves()` is a **no-op in practice**. The `_sub` callback handles `"</" + tag` correctly, so only the pattern is wrong.
>
> Impact: text-only leaves keep their inline body rather than becoming `text="…"`. Downstream `_convert()` already handles inline text (`text_joined` → `props["text"]`), so the visible result is usually the same — this is a dead optimization, not a rendering break. Still worth fixing so the two paths agree.

### `_strip_internal()` must keep `__expr__` keys

> *"NOTE: `__expr__<prop>` keys are deliberately KEPT. The parser evaluates a dynamic prop once, server-side, which freezes it — but the raw source is shipped alongside so the client can re-evaluate it against live state on every render (see `hydrate()` in `web/js/render-components.js`). Stripping them here is what made `{running?"success":"danger"}` never change."*

> [!IMPORTANT] This is the reactivity contract
> Server-side evaluation freezes a value at parse time. Shipping `__expr__<prop>` alongside lets the client re-run it on every render. Removing those keys — an obvious-looking "cleanup" — silently breaks all live-updating components: a status badge would compute once and never change colour again.

## `_UIParser` — lenient tree builder

```python
class _UIParser(HTMLParser):
    def handle_starttag(...)     # push unless in _LEAF_TAGS
    def handle_startendtag(...)  # never push (self-closing)
    def handle_endtag(...)       # pop to matching tag, or ignore
    def handle_data(...)         # append text if non-empty
```

`handle_endtag` searches **backwards** through the stack for a matching tag and truncates the stack there — a mismatched close tag (`<card><b>x</card>`) discards the intermediate unclosed elements rather than raising. That is why it is "lenient": model-written markup is not guaranteed well-formed.

`_coerce(name, value)`:

| Attribute class | Conversion |
|:--|:--|
| `_JSON_ATTRS` | `json.loads`, fall back to raw string |
| `_BOOL_ATTRS` | `value.strip().lower() in ("1","true","yes","on","")` — **empty string is True** (bare attribute) |
| other | raw string |

`_convert(node, depth)` maps the parsed tag through `_KIND_MAP`, applies `_INLINE_STYLE` defaults, collects text children into `props["text"]` (or a leading `{kind:"text"}` child when there are also element children), and recurses.

***

## Corrections summary

| Doc | Said | Actually |
|:--|:--|:--|
| 03 | `MAX_BROWSERS_GLOBAL` is a machine-wide cap | Not enforced; only bounds the cgroup tree. `MAX_BROWSERS=2` **per process** is the only cap |
| 03 | Governor enforces memory limits | Explicitly does **not** — cgroups carry no `memory.max`, no `oom.group` |
| 03 | `response_normalizer` handles 3 dialects | 4 text dialects + OpenAI incremental `tool_calls` |
| 03 | `inpage` waits for 3 WAF cookies | Gates on `acw_tc` + `isg` only |
| 03 | `_lock_holder_is_foreign_live` is a process-safety check | It is the bug fix that made the ghost path work at all |
| 06 | `_partial_tag_suffix` holds a partial prefix | Holds the **longest** possible prefix; the loop direction is the fix |
| 06 | `<style>`/`<script>` in SVG are escaped | Emitted **verbatim**; escaping corrupts CSS |
| — | — | `_collapse_text_leaves`'s regex is broken (`</>` instead of `</\1>`) — dead code, not a render break |
