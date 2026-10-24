# 57 — Drive, MCP, Notifications, Diary, Plugins, Scraper, Skill Support

6,873 lines across the remaining `engine/` subsystems. Closes `engine/drive/`, `engine/mcp/`, `engine/notifications/`, `engine/diary/`, `engine/plugins/`, and the `engine/scraper/` remainder. Also covers four `engine/skills/` support modules.

| Area | Lines | State |
|:--|--:|:--|
| `engine/drive/` | 1,401 | **closed** |
| `engine/mcp/` | 521 | **closed** |
| `engine/notifications/` | 289 | **closed** |
| `engine/diary/` | 244 | **closed** |
| `engine/plugins/` | 235 | **closed** |
| `engine/scraper/` remainder | ~840 | **closed** |
| `engine/skills/` — middleware, json\_depth, json\_repair, json\_sanitize | ~590 | closed |

***

## 1. `drive/` — the best-defended subsystem in `engine/`

### 1.1 Two credential paths, one client

```
Public  → ?key=<API_KEY>          (anything shared "anyone with the link")
Private → Authorization: Bearer   (OAuth 2.0, sees everything the account can)
```

`_auth(prefer=...)` picks, `_request` retries once with OAuth when a key-path 403 is *specifically* an auth failure. `_is_auth_failure` is the guard that makes that safe:

```python
# Google returns 403 for auth problems *and* for rate limits / quota. Blindly
# retrying every 403 doubles the request and forces a pointless token refresh.
```

A 403 with a reason in `_AUTH_FAILURE_REASONS` retries; a 403 with a different reason (quota, rate limit) does not. Correct and rarely done.

### 1.2 Credentials are written 0600 *before* they exist

```python
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
```

The mode is set at creation, not `chmod`'d afterwards, with the comment explaining why: *"closing the window where it's 0644."* This is the only place in the tree that gets that right — `system/.gemini_api_keys.json` is 0644 (ledger #128) and `system/.zai_tokens.json` is written with `mkstemp` but never re-checked.

### 1.3 Drive ids are validated before they reach a URL

```python
# Drive ids are opaque URL-safe tokens. Validating them stops a crafted id
# (e.g. "x/../../v3/permissions") from being normalized into a different
# Google API endpoint that accepts the same credential.
_ID_RE = re.compile(r"[A-Za-z0-9_-]{5,200}")
```

`_check_id` runs on every id, in both `client.py` and `public.py`. This is the same class of bug as ledger #33's `_normalize_url` — and unlike that one, it was written.

### 1.4 Download caps are enforced *while streaming*

```
declared = int(meta.get("size") or 0)
if declared > _MAX_DOWNLOAD: raise …
buf = bytearray()
async for chunk in self.iter_download(meta):
    buf.extend(chunk)
    if len(buf) > _MAX_DOWNLOAD: raise …
```

Two checks: the advertised size before the request, and the real accumulated size during. The comment says why both — *"so an oversized share is rejected without ever being fully buffered."*

### 1.5 🔴 `_adaptive_refresh` promises incremental and does a full scan

```python
async def _adaptive_refresh(...):
    """Refresh only changed items when possible, fall back to full re-scan.

    For authed sources: query files where modifiedTime > last indexed_at.
    """
    …
    # Full re-scan is simpler and more reliable than trying to query by
    # modifiedTime across all subfolders (which requires knowing every folder
    # id). The scan itself is fast enough for typical study folders.
    new_entries = await _full_scan(drive_id, use_authed=True)
```

The docstring describes an optimisation that was deliberately not implemented, and the inline comment contradicts it 40 lines later. Worse, the surrounding code is written for a diff that never happens:

```python
old_map = {e["id"]: e for e in old_entries}   # never read
new_entries = await _full_scan(...)
new_map = {e["id"]: e for e in new_entries}   # never read
merged = []
seen = set()                                  # never read
for entry in new_entries:
    merged.append(entry); seen.add(entry["id"])
return merged
```

`merged` is exactly `new_entries`. Three dicts and a set are built for a merge that is a copy. Every refresh re-walks the entire tree with 6 concurrent workers — which is the cost the docstring says it avoids.

### 1.6 Public folder listing is an HTML scrape, and says so

```
Tradeoff: the folder listing is scraped from HTML, so it's more fragile than
the JSON API. The authed client remains as a fallback when this fails.
```

`parse_folder_html` splits on `<div class="flip-entry"` and pulls id/name/modified with four regexes. A Google markup change breaks it silently — `list_folder` returns `[]` and `_scan_folder_keyless` returns an empty list, which `index_source` records as `status="ready"` with zero entries.

### 1.7 OAuth is PKCE + loopback

`build_auth_url` returns `(auth_url, code_verifier)`, `exchange_code` takes the verifier. `access_type=offline` + `prompt=consent` to guarantee a refresh token. Loopback redirect so the local server receives the code — no copy/paste. All of this is the Google-recommended flow and it is implemented correctly.

***

## 2. `mcp/` — one real hazard, one clever fix

### 2.1 🔴 the coroutine is created before the loop is checked

```python
if server:
    coro = manager.call_tool(server, tool, arguments)
else:
    coro = manager.call_tool_auto(tool, arguments)

loop = getattr(get_runtime(), "_loop", None)
if loop is None or loop.is_closed():
    yield _output_event(tag_id, "No running event loop available…", "stderr")
    yield _end_event(...)
    return
```

`call_tool` is an `async def`, so calling it creates a coroutine object immediately. On the early-return path that coroutine is never awaited and never closed — `RuntimeWarning: coroutine 'MCPManager.call_tool' was never awaited`, and the coroutine holds its frame until GC. Create it after the loop check, or `coro.close()` on the bail path.

### 2.2 `_isolate` exists for a reason worth keeping

```
MCP's stdio_client creates anyio cancel scopes via task groups. Running inside
FastAPI's ASGI middleware causes cancel-scope conflicts ('Attempted to exit a
cancel scope that isn't the current task's'). Spawning a fresh task isolates
those scopes from the request context.
```

This is the kind of fix that looks like a workaround and is actually the documented remedy for anyio's task-affinity requirement. It is repeated in `connect_server`, `disconnect_server` and `shutdown`, and the comment is written once.

> [!NOTE] `_isolate` leaks the inner task on cancellation
> It `create_task`s a runner and awaits an `asyncio.Event`. If the awaiting coroutine is cancelled, the task keeps running and `done.set()` fires into nothing. No `try/finally` cancels it.

### 2.3 Command resolution checks the project first

```python
if not Path(raw_cmd).is_absolute() and shutil.which(raw_cmd) is None:
    local_bin = project_root / "system" / raw_cmd
```

Falls back to `system/<cmd>` and `system/<cmd>.exe`, so a Windows install does not depend on the launcher mutating `PATH`. Small, and the alternative is a class of "works on my machine" bug.

***

## 3. `notifications/` — a policy layer that earns its name

`policy.py` exists so call sites stay dumb: they report an event, this module decides whether it is worth interrupting the user. Five categories, each independently toggleable, with a master switch that always wins.

The non-obvious rule is the fall-through:

```
Specific completions (image, research) win; the generic "turn complete" only
fires when nothing specific did, so a long turn can't double-notify.
```

`_IGNORED_SKILLS` excludes `chat_title`, `_loop_warning` and `action_parse` — bookkeeping that would otherwise satisfy the "3+ tool calls" rule on every turn.

`desktop._ensure_burnttoast` has a three-state flag (`None`/`True`/`False`) so a genuinely-unavailable BurntToast is not re-probed for 60 s on every notification. That is the correct shape for an expensive one-time check, and it is the only place in the tree that uses it.

***

## 4. `scraper/` — the split is good, one method is broken

### 4.1 🔴 `_cleanup_engine` returns before it cleans anything

```python
async def _cleanup_engine(self, *, kill_browser: bool = False) -> None:
    engine = self.engine
    if engine is None:
        return
    if not kill_browser:
        return                      # <- nothing below this runs
    self.engine = None
    self.loaded_path = None
    self.active_chat_id = None
    cleanup = getattr(engine, "cleanup", None)
    …
```

`stop()` calls `self._cleanup_engine(kill_browser=kill_browser)` with the default `False`, and `reset()` calls `stop()`. So `stop()` and `reset()` are complete no-ops: the engine reference, `loaded_path` and `active_chat_id` all survive. Only `kill_session()` — which passes `kill_browser=True` — does anything.

The `not kill_browser` early return reads as "don't kill the browser, but do detach". As written it means "don't kill the browser, don't detach either".

### 4.2 `_ensure_engine` dynamically loads a module and rewrites `sys.modules`

```python
saved_top_level = {"config": sys.modules.get("config"), "exceptions": …}
try:
    sys.modules["exceptions"] = exceptions_module
    sys.modules["config"] = config_module
    …
    engine_module = _load_py_module(module_name, engine_path)
finally:
    for name, old in saved_top_level.items():
        if old is not None: sys.modules[name] = old
        else: sys.modules.pop(name, None)
```

Each engine dir ships its own top-level `config` and `exceptions` modules, so they are swapped into `sys.modules` for the duration of the import and restored afterwards. Correct — and it means the engine module's own `import config` binds to whatever was in `sys.modules` at import time, which is why the restore matters and why two engines loaded concurrently would race.

`_load_engine_class` also injects `engine_dir` into `sys.path` and never removes it.

### 4.3 `_probe_existing_session` validates the profile path, not just the port

```python
remote_profile = data.get("userDataDir", "")
if os.path.realpath(remote_profile) != os.path.realpath(user_data_dir):
    return False
cmd_line = data.get("BrowserCommandLine", "")
if "--headless" in cmd_line:
    return False
```

Adopting a browser on the right port but the wrong profile would attach the scraper to a different account. The realpath comparison plus the headless check is the difference between "reconnect" and "hijack the wrong session".

### 4.4 `settings.py` coerces booleans from JSON

```python
# Coerce to a real bool so a hand-edited JSON string ("true") can't leak
# through as truthy-text and silently invert the launch mode.
```

`_as_bool` handles `None`, `bool`, `int`/`float`, and the strings `1/true/yes/on`. Applied to `headless` and `chatgpt_headless` on load, save and update. Small and complete.

### 4.5 `stream.py` — a three-case delta computer

`_compute_delta(full, prev)` handles growth (emit the tail), shrinkage from `_clean_garbage` dedup (emit nothing), and mid-text reformat (longest common prefix, emit only beyond it). The middle case is the one that matters: the scraper's cleanup pass can *shorten* the accumulated text, and emitting nothing is correct where a naive `full[len(prev):]` would emit garbage or go negative.

The live-display path is guarded by `_accepts_arg(engine.get_response, 'live_display')` so an engine that does not support streaming falls back to a single `await` — the same duck-typing trick as `_accepts_arg` on `thoughts_callback`.

***

## 5. `skills/` support modules

### 5.1 🔴 `json_sanitize._normalize_backslashes` corrupts valid escapes

```python
valid = set(chr(34) + BS + chr(47) + 'bfnrtu')
…
else:
    result.append(nc)      # backslash dropped
    i += 2
```

Any backslash followed by a character outside `" \ / b f n r t u` loses the backslash. A tool call containing `\d+` becomes `d+`, `\s` becomes `s`, `\.` becomes `.`. The sanitizer is meant to fix invalid JSON escapes, and instead it silently rewrites every regex and Windows path in the payload.

The irony is in the same subsystem: `_diagnose_json_failure`'s hint is *"Backslashes, regex patterns, and */ in strings often break JSON transport."* — the code knows backslashes are the hazard and then mangles them.

### 5.2 `json_depth` is the guard that makes streaming repair safe

```
# Only attempt repair if JSON is structurally complete (balanced brackets).
# Streaming chunks like '[{"name":' are NOT complete — repairing them
# produces valid-but-wrong JSON that gets consumed as real tool calls.
```

`json_structurally_complete` tracks brace/bracket depth with string and escape awareness, and returns `True` only when depth returns to zero after having entered a structure. This is the load-bearing check between the streaming parser and `json_repair` — without it, every partial chunk would be "repaired" into a plausible tool call and executed.

### 5.3 `middleware.py` documents what it removed

```
# NOTE: PermissionMiddleware used to live here as a pass-through stub. It was
# dead code — engine/skills/engine.py wires in SecurityMiddleware
# (engine/security/middleware.py) instead, which does the real work…
# Removed to avoid implying permissions are unenforced.
```

A removed stub, documented at the site, with the reason. That is how to delete code in a codebase where someone might otherwise assume the ordering guarantees are still there.

`SchemaValidationMiddleware` runs after `SecurityMiddleware` so security blocks take precedence, and calls `load_single_tool(ctx.name)` — which does a linear scan of every tool × function — on every tag execution.

### 5.4 `parser.py`'s bare-tag machinery is the most intricate code in the tree

`_scan_for_unterminated`, `_trim_incomplete_markup`, `_find_bare_open`, `_find_bare_start`, `_find_bare_close`, `_inside_code_span` — six functions whose only job is to decide whether a `<svg markup="…">` written mid-stream is a tag, prose, or a partial tag.

The comments record each failure in order:

1. `_inside_code_span` — a tag inside backticks is *quoted*, not requested; intercepting it spawned a phantom pane and ate the prose.
2. The `<svg>` leak — `marked` + DOMPurify parsed the outer self-closing `<svg>` as an empty element, so it *"painted during streaming, then vanished on the final render."*
3. `_find_bare_start` — anchoring on the *earliest* incomplete start, not the last `<`, is what keeps `markup="<svg …"` intact.
4. The single left-to-right scan replaced a per-name `str.find` loop that was O(N × tags) over a growing buffer.

Four bugs, four surviving explanations, in one function family.

***

## 6. `diary/` and `plugins/`

`diary/` is three CLI scripts, not a package. `summarizer.py` and `synthesizer.py` both do `from gemini_helpers import …` — a bare top-level import that only resolves when the script's own directory is on `sys.path`. Running them as `python engine/diary/summarizer.py` works; `import engine.diary.summarizer` raises `ModuleNotFoundError`. Same shape as `engine/scraper_engines`'s non-importable half (doc 28).

`plugins/opencode_free.py` fetches `https://models.opencode.ai/api.json` at every start and registers zero-cost, non-deprecated models as `oc-*`. Capabilities and thinking modes are derived from the catalog's own fields rather than hardcoded — `reasoning_options.type == "effort"` becomes one thinking mode per effort value. That is the correct direction of dependency; the alternative is a hardcoded model list that rots.

***

## 7. Findings

| # | Where | What |
|--:|:--|:--|
| 216 | `drive/indexer._adaptive_refresh` | **🔴** Docstring promises `modifiedTime`-based incremental refresh; the body does a full re-scan and says so in a contradicting comment. `old_map`, `new_map`, `seen` are built and never read — `merged` is a copy of `new_entries`. Every refresh re-walks the whole tree. |
| 217 | `scraper/lifecycle._cleanup_engine` | **🔴** `if not kill_browser: return` sits **before** the state reset, so `stop()` and `reset()` are complete no-ops — `self.engine`, `loaded_path` and `active_chat_id` all survive. Only `kill_session()` does anything. |
| 218 | `skills/json_sanitize._normalize_backslashes` | **🔴** Any backslash followed by a char outside `" \ / b f n r t u` loses the backslash: `\d+` → `d+`, `\.` → `.`. Silently rewrites every regex and Windows path in a tool payload. |
| 219 | `mcp/handler.handle_mcp_call` | `coro = manager.call_tool(...)` is created **before** the `loop is None` bail-out, so that path leaks an un-awaited coroutine (RuntimeWarning + unclosed frame). |
| 220 | `drive/indexer.index_source` | Cooldown path returns the previous index with no status change and no log, so a forced-update user sees stale data with no indication. |
| 221 | `drive/public.parse_folder_html` | A Google markup change returns `[]`, which `index_source` records as `status="ready"` with zero entries — an empty folder and a broken scraper are indistinguishable. |
| 222 | `mcp/manager._isolate` | `create_task`s a runner and awaits an event with no `try/finally`; cancelling the caller leaves the task running. |
| 223 | `scraper/lifecycle._load_engine_class` | Inserts `engine_dir` into `sys.path` and never removes it, so the first engine loaded wins all later top-level imports. |
| 224 | `diary/summarizer.py` + `synthesizer.py` | `from gemini_helpers import …` makes `engine.diary.*` unimportable as a package; only runnable as scripts. |
| 225 | `skills/middleware.SchemaValidationMiddleware` | Calls `load_single_tool(ctx.name)` per tag, which linearly scans every tool × function. Cached discovery makes it cheap-ish, not free. |

***

## 8. What this batch shows

- **`drive/` is the best-defended subsystem in `engine/`** — id validation, 0600-at-create credentials, two-layer download caps, an auth-failure classifier that distinguishes 403 reasons, and correct PKCE+loopback OAuth. Its one 🔴 is a *documentation* failure wrapped around dead code, not a safety one.
- **Two of the three 🔴 are `return` statements in the wrong place.** `_cleanup_engine` returns before resetting state; `_adaptive_refresh` returns the input unchanged. Neither is subtle logic — both are the kind of thing a single test would catch, and `tests/` is still unopened.
- **`json_sanitize` is the first finding that corrupts data silently while trying to fix it.** Everything prior either failed loudly or leaked state. This one rewrites the payload and reports success.
- **`parser.py` holds four separate bug-fix comments for one feature.** That is the highest comment-to-code ratio in the tree and it is entirely earned.

Python is now **~107,300 / 137,031 ≈ 78%**; overall **~56%**.

> [!WARNING] Partial reads in this batch
> `engine/drive/public.py`, `engine/diary/summarizer.py`/`synthesizer.py`, `engine/plugins/opencode_free.py`, `engine/scraper/stream.py` and `engine/skills/parser.py` were truncated mid-file by the 39 KB chunking. Findings above are confined to code that was fully visible. `engine/security/` was not reached at all — it is the next batch.
