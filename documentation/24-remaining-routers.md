# 24 — Remaining Routers (30 files, ~13,550 lines)

> [!IMPORTANT]
> Everything in `server/api/routes/` not covered by docs 17, 18, 22, 23. Grouped by concern. Three real bugs found here (§14).

**Read depth:** all files full.

***

## 1. Filesystem — `filesystem.py` (947)

VS-Code-style file explorer. Six browse roots (`home`, tempdir, `/mnt` on WSL2), `_HIDDEN_DIRS` skip-list, 512 KB read cap, extension→emoji icon map.

| Endpoint | Notes |
|:--|:--|
| `/list`, `/read`, `/serve`, `/serve-dir/{path}` | read-only browse |
| `/write`, `/mkdir`, `/copy`, `/move`, `/delete` | mutations, all gated by `_is_allowed` |
| `/roots` | quick-access paths |
| `/pick-folder` | 4-tier native picker |
| `/pending-diff`, `/changes`, `/change-detail` | inline diff review |
| `/accept-edit`, `/reject-edit` | backup-based accept/reject |
| `/editors`, `/open-in` | launch external editor |

### 1.1 `pick-folder` — four fallbacks, in order

1. **WSL2** → `powershell.exe` + `Shell.Application.BrowseForFolder`, then `_win_path_to_wsl` (`C:\Users\x` → `/mnt/c/Users/x`)
2. **xdg-desktop-portal** via `gdbus` — `FileChooser.OpenDirectory` then `gdbus monitor` on the request path for the `Response` signal, URI extracted by regex
3. **zenity / yad / kdialog** — `returncode == 1` means the user cancelled (returns `{cancelled: true}`, not an error)
4. **tkinter** — `filedialog.askdirectory`, ships with Python

`_get_display_env()` is the load-bearing part: it walks `loginctl list-sessions`, reads `/proc/<leader>/environ` for the missing `WAYLAND_DISPLAY`/`DISPLAY`/`XDG_RUNTIME_DIR`, and falls back to globbing `/run/user/*/wayland-*`. Without it a systemd-started server has no display vars and every GUI dialog fails silently.

### 1.2 Changed-files feed

`_walk_change_backups(root)` walks the tree, and on hitting a `.editor_tools_backups` dir, stops descending and instead pairs each `<name>.<ts>.bak` with its live file. `_CHANGE_SKIP` prunes 20+ heavy dirs. `filesystem_changes` then filters by `max_age` (default 3600 s) and **drops files whose backup is byte-identical to the live file** — *"A backup identical to the live file is not a pending change — the edit was already accepted."*

`change-detail` has no age cutoff, and a file with **no** backup returns `created: true` with an empty `original_content` — *"Diff it against an empty buffer so the whole file reads as added."*

> [!CRITICAL]
> **`_is_allowed` does not resolve symlinks.**
> ```python
def _is_allowed(path: str) -> bool:
    resolved = os.path.normpath(os.path.expanduser(path))
    for root in _BROWSE_ROOTS:
        if resolved == norm_root or resolved.startswith(norm_root + os.sep):
            return True
```
> `normpath` is purely lexical. A symlink at `~/link → /etc` makes `~/link/passwd` normalize to `~/link/passwd`, pass the prefix test, and then `Path.read_text()` follows the link to `/etc/passwd`. Combined with the **auth-exempt `/api/filesystem/` prefix** (doc 11), this is a LAN-reachable arbitrary-file-read via a symlink the user (or any tool) creates in their home dir. **Not fixed** — the fix is `Path(path).resolve()` against `Path(root).resolve()`, but that changes behaviour for legitimate symlinked project dirs, so it's your call.

***

## 2. Processes — `processes.py` (695)

`/proc`-only process monitor, no psutil. A process belongs to Sable if: in a bg-job registry, **or** descended from a Sable server, **or** its cmdline names a Sable root, **or** its cwd is inside a root *and* it holds a port or is an interpreter.

```
# Browser helper processes (crashpad handlers, zygotes) inherit their cwd from
# whoever launched the browser. A stray Helium crashpad sitting in the project
# directory is NOT Sable's work, so a cwd match alone must never admit them.
```

`_pid_exists` reads `/proc/<pid>/stat` state first, because *"a killed child whose parent never waited stays in /proc as a ZOMBIE with state 'Z'. os.kill(pid, 0) still succeeds for it."*

CPU% is a delta on `utime+stime` cached in `_CPU_SAMPLES`; RAM is `rss` from `/proc/<pid>/stat` × page size. Ports come from matching `/proc/net/tcp{,6}` LISTEN inodes against `/proc/<pid>/fd` socket symlinks.

> [!CRITICAL]
> **`_system_cpu_percent` references an undefined `prev`.**
> ```python
> prev_total = _SYS_CPU["total"]
> prev_idle = _SYS_CPU["idle"]
> _SYS_CPU.update({"ts": now, "total": total, "idle": idle})
> if not prev_total:
>     return 0.0
> d_total = total - prev_total
> d_idle = idle - prev_idle
> if d_total <= 0:
>     return round(prev["pct"], 1)   # ← NameError
> ```
> The comment above it explains the aliasing bug that was fixed (`prev = _SYS_CPU` would alias), but the fix introduced this. It fires when two polls land inside the same jiffy (`d_total == 0`) — i.e. exactly when the dashboard polls fast. `NameError` propagates out of `/api/processes` as a 500. **Not fixed** — should be `return round(_SYS_CPU.get("pct", 0.0), 1)`.

***

## 3. Agents — `agents.py` (576)

Per-chat persistent SSE (`/api/chat/{id}/agent-events`) with a 50-slot queue per client, 30 s keepalives, and `broadcast_agent_event()` for cross-chat delivery. Plus the full config surface: `DEFAULT_AGENT_CONFIG` (concurrency, circuit-breaker, per-role `allowed_skills`/`default_skills`/`required_sections`), hot-reload via `apply_role_overrides` + `apply_account_assignments`.

Per-agent SSE (`/api/agents/{id}/stream`) supports `?since=N` replay from the agent's stream history buffer.

> [!CRITICAL]
> **`kill_agent` calls `logger.warning` but `agents.py` never imports a logger.**
> ```python
>     except Exception as exc:
>         logger.warning("kill_agent: upstream stop failed for %s: %s", agent_id, exc)
> ```
> The module imports `asyncio, json, Path, typing` — no `logging`. So when the upstream stop fails (the exact case the handler exists for), the handler raises `NameError` and the kill request 500s instead of completing the teardown. **Not fixed.**

`spawn_agent` deliberately does **not** run an LLM loop — it emits `agent_trigger` and lets `/api/chat` own the turn (`"Uses the main chat pipeline (critique pattern)"`).

***

## 4. Scraper — `scraper.py` (136)

Session info/kill, model switching, and diagnostics replay. Two endpoint families: legacy `/api/scraper/*` (the global scraper-mode engine) and `/api/connectors/{backend}/*` (per-model browser backends, *"NOT tied to that toggle"*).

> [!CRITICAL]
> **`scraper.py` is missing two imports.** It uses `HTTPException` (three times) and `_resolve_connector` (three times) but only imports `APIRouter` and `scraper_service`. `_resolve_connector` is not defined anywhere in the file.
>
> Consequence: `GET /api/connectors/{backend}/models` and `POST /api/connectors/{backend}/model` raise `NameError` on every call. The legacy `/api/scraper/*` routes (which don't touch either name) work fine — which is why this went unnoticed. **Not fixed.**

***

## 5. Telegram — `telegram.py` (567) + `telegram_bot.py` (145)

Two unrelated features: a Telethon **read-only chat browser** (dialogs, messages, media, send, sign-in with 2FA) and **bot config** (token, allowed users, start/stop).

`_get_client()` is the interesting part — self-healing session recovery:

```
# Self-healing: if the session's auth key is permanently dead
# (AUTH_KEY_DUPLICATED / revoked / deactivated), the poisoned ``.session``
# file is deleted automatically and a 401 is raised telling the user to
# sign in again. No manual ``rm`` needed.
```

Critical detail: **`connect()` does not surface a dead auth key — an RPC does.**

```
# Probe with an RPC — AUTH_KEY_DUPLICATED only surfaces on a
# request, not on connect(), so is_user_authorized() is the
# earliest reliable detector.
```

`purge_dead_session_on_startup()` runs the same probe once at boot and never raises. `_detect_mime(buf, fallback)` sniffs magic bytes for PNG/JPEG/WebP/GIF/MP4/Ogg/MP3/PDF because Telethon's declared type is unreliable.

> [!NOTE]
> `disconnect_client()` references `_auth_client`, which is assigned at module level **further down the file**. It works (function body resolves globals at call time) but is fragile if anyone ever reorders.

`telegram_bot.py` stores its token in `PERSISTENT_ROOT/system/.telegram_bot_config.json`, masks it on GET, and merges on POST so a partial update never clears credentials.

***

## 6. Update — `update.py` (546)

The safest file in the codebase. Seven phases, each with a defined rollback point.

| Phase | Failure handling |
|:--|:--|
| 1. resolve release | abort |
| 2. download to temp | abort, temp cleaned |
| 3. **stage-extract** (validate first) | abort, install untouched |
| 4. **snapshot** every file about to be overwritten | abort before touching install |
| 5. overlay staged tree | **rollback** |
| 6. `uv sync` | **rollback** |
| 7. restart | point of no return |

`_PROTECTED_RELPATHS` + `_PROTECTED_PREFIXES` (`system/`, `output/`, `.venv/`, `.git/`, `.sable_backups/`) mean an update never touches memory, personas, auth tokens, or browser profiles.

`_backup_files` records `{rel, existed, backup}` so `_restore_files` can both **restore** overwritten files and **delete** newly-created ones.

```
# Point of no return: restart_service() calls os._exit(0), so nothing
# after this line runs and rollback is no longer possible.
```

`_pick_asset` never matches on platform alone: *"Never match on platform alone — that would install the wrong version."*

`_safe_extract` does `target.relative_to(dest)` on resolved paths, rejecting zip-slip.

`_overlay_tree` retries once on `ETXTBSY` (`"Text file busy"`) — a Windows-specific hazard.

***

## 7. Misc — `misc.py` (927)

The grab-bag: health, warmup, env, UI config, response stats, log SSE, `/api/models`, skills/tools browse + disable, tool-format matrix, sync-context, file revert, `index.html` serving, instruction-extra, and direct tool invocation.

### 7.1 `index.html` cache keyed on mtime

```
# The cache is keyed on the file's mtime, NOT just "is it loaded yet". A bare
# one-shot cache means editing index.html leaves the server serving the old
# bytes until a full restart — so adding a <script> tag silently does nothing
# and the feature looks broken. A stat() per request is far cheaper than the
# restart it saves.
```

### 7.2 `/api/models` — dynamic backend filtering

`_get_available_backends()` probes ~14 backends with different evidence per provider:

| Backend | Evidence |
|:--|:--|
| qwen | WAF token store non-empty **or** any `browser-data-acc*` dir |
| deepseek | `get_token_for_account()` returns something |
| gemini_web | captured template **and** `list_valid_accounts()` |
| zai | token cached **or** a browser profile exists |
| mimo | any `browser-data-acc*/.mimo_auth.json` |
| kimi / chatgpt / claude_scraper | pinned profile first, then any profile — marker file or **read-only SQLite cookie query** |
| gemini/groq/mistral/openai/opencode | `client.is_available` |

`local` and `opencode` are always available (custom endpoints; free anonymous tier).

> [!TIP]
> The Claude probe uses `name = 'sessionKey'` on `%claude.ai%` — same predicate as the settings route (doc 18 §15.2), same reasoning: `cf_clearance` exists pre-login.

### 7.3 Tool-format matrix

Per-provider map in `settings.json` under `tool_call_format_by_provider`, with a **one-way migration** from the legacy global `tool_call_format`:

```python
def _migrate_legacy_tool_format(data):
    legacy = data.get("tool_call_format", "")
    ...
    for row in rows:
        existing.setdefault(row["key"], legacy)
    data["tool_call_format_by_provider"] = existing
    data.pop("tool_call_format", None)
```

`setdefault` means a migration never overwrites an already-explicit per-provider choice. Writing always calls `invalidate_cache()`.

Formats: `qwen_json`, `hermes`, `dsml`, `mimo`, `native`, `none`, plus `""` = built-in default. Empty string is **removed** from the map rather than stored.

### 7.4 Direct tool invocation

`POST /api/tool/{tool_name}` whitelists `{image_generator, generate_image}`, routes by `provider` (`cloudflare` / `puter` / `pollinations` / perchance-CLI), and has a **route-ordering comment**:

```
# NOTE: These MUST be above the catch-all /api/tool/{tool_name} route below,
# otherwise FastAPI matches the parameterized route first.
```

***

## 8. Dashboard — `dashboard.py` (611)

Token/cost aggregation from `messages.prompt_tokens`/`completion_tokens` joined to `chats.provider`, a hardcoded `_PROVIDER_COST` table (per 1M input/output), 7-day daily burn, agent counts, provider key listing, and a **concurrent** liveness probe (`ThreadPoolExecutor(max_workers=6)`, 3 s timeout, one key per provider).

> [!IMPORTANT]
> `dashboard_provider_status` returns a **union type**: most providers are `bool`, but `qwen` is an **object** `{ok, browser_running, header_source, disk_tokens_available}`. A frontend doing `if (status[name])` works for both; one doing `status[name] === true` silently misreads qwen. Documented, not fixed.

`dashboard_status` shells `pgrep -f "chromium.*browser-data-acc"` for browser liveness — a full process scan every call, no cache.

***

## 9. Cookbook — `cookbook.py` (488)

Local model lifecycle over `engine.cookbook`: download (HF), serve (`llama-server`), presets ranked by hardware, cached-model scan, HF search, per-model instruction settings, and **three** purge paths (`/model`, `/model-by-id/{id}`, `/model-settings/{id}`).

`delete_model_file` guards the path:

```python
if not str(p.resolve()).startswith(str(models_dir.resolve())):
    raise HTTPException(403, "Path is outside the models directory")
if p.suffix != ".gguf":
    raise HTTPException(400, "Can only delete .gguf files")
```

`recommendations` runs in `asyncio.to_thread` — *"HF API calls + scoring are CPU/IO bound, not async"*.

***

## 10. OCR — `ocr.py` (412) + `ocr_local.py` (272)

### 10.1 Cloud (`ocr.py`)

BanglaOCR proxy with **free-proxy rotation**. `_fetch_proxies` caches 100 proxies for 5 min from two public sources. Per attempt: direct first, then up to 10 random proxies. A single `OVERALL_OCR_TIMEOUT = 300 s` deadline is checked before each attempt, and `PER_PROXY_TIMEOUT = 15 s` keeps a dead proxy from eating the budget.

PDF handling: `fitz` renders every page to 200 DPI PNG **upfront**, then OCRs with `Semaphore(MAX_CONCURRENT_OCR = 4)`.

Three endpoints: `/recognize` (one file), `/batch` (≤50, non-streaming), `/stream` (SSE with a **pre-scan** of PDF page counts so the progress bar has a denominator from the first event).

> [!NOTE]
> `MAX_RETRIES = 3` and `RETRY_BASE_DELAY = 2.0` are declared and never used — the retry is the proxy loop.

### 10.2 Local (`ocr_local.py`)

Four providers in a static registry: `banglaocr` (cloud, always ready), `sableocr` (pytesseract + bnunicodenormalizer + opencv), `paddleocr`, `pytesseract`. Dependencies are **not** in `pyproject.toml` — installed on demand into the project venv via `pip install`, streamed as SSE log lines.

`_is_package_installed` strips version specifiers and maps pip names to import names:

```python
_IMPORT_NAME_MAP = {"opencv-python-headless": "cv2", "paddlepaddle": "paddle", ...}
```

so `paddlepaddle==3.2.0` is probed as `import paddle`.

***

## 11. Study satellites

### 11.1 `study_notes.py` (396) — Obsidian-style notes

Index at `library/notes/notes.json`, body as a real `library/notes/<id>.md`. *"Keeping the body as a plain `.md` file means notes stay hand-editable and portable."*

`_valid_id` (uuid4 hex) before every disk touch, `_lock()` around every index read-modify-write, atomic `.tmp`→`replace` writes. `_clean_refs` normalizes `{doc_id, title, kind: doc|page|selection, page, text, rects}`. `notes_capture` derives a title from the anchored text when none is given and quotes the passage into the body.

### 11.2 `study_srs.py` (407) — SM-2

Decks index + per-deck card files, each under its own `exclusive_lock`. Grades map `again=0, hard=3, good=4, easy=5`. `_schedule()` implements compact SM-2: reps 1→1d/3d, reps 2→3d/6d, then `interval × factor`; ease clamped `[1.3, 2.8]`; a lapse resets reps and sets due `+600s`.

### 11.3 `study_dict.py` (341) — dictionary

Two keyless sources. English: freedictionaryapi.com → en.wiktionary REST fallback. Bangla: **offline index first** (`engine.bangla_dict`, ~106k headwords), then wikitext `{{t|bn|...}}` extraction, then **lemma candidates** (`studies→study`, `processes→process`, `running→run`) since Wiktionary files translations under the lemma.

The subtle part is the error/miss distinction:

```
# Returns [] for a page that genuinely has no Bangla table, but None when the
# request itself failed. The caller must not treat those the same: an empty
# list is a fact worth caching, a failed request is not.
```

`_CACHE_VERSION = 4` retires old entries on a pipeline change, and a result is **never** cached when the Bangla side errored — otherwise a rate-limit blip becomes a permanent "no translation".

### 11.4 `study_cover.py` (544) — cover-page PDF

JSON store with append-only rosters (`universities`, `courses`) and overwriting singletons (`student`, `teacher_profile`, `doc`, `dates`), all under `exclusive_lock`. `_normalise` dedupes rosters by normalised key and is idempotent.

Renderer priority: **chrome → edge → playwright → firefox(detect-only)**.

```
# WHY A BROWSER AT ALL: the cover is an HTML/CSS document. Only a browser
# engine lays out @page, mm units, flexbox, gradients and webfonts the way
# the template expects, so the browser IS the renderer.
```

> [!TIP]
> `--user-data-dir` is load-bearing:
> *"Without an isolated profile, an already running Chrome instance swallows the invocation (opens a tab in the user's session and exits) and no PDF is ever written."*

Firefox is detected but never used: *"its CLI only supports `--screenshot`, not `--print-to-pdf`."*

> [!NOTE]
> `_find_chrome()` and `_find_firefox()` are both dead — `_browser_candidates()` superseded them.

### 11.5 `study_humanize.py` (555)

Two-phase: LLM rewrite then rule pass (`skills/text_humanizer/scripts/humanize.py` via subprocess). `_humanize_stream` is deliberately **stateless and instruction-free** — no study directive, no persona, no output-format primer.

**Rewrite-distance retry** is the clever bit:

```python
def _overlap_ratio(a, b):
    # Measures shared *bigrams* (adjacent word pairs) rather than single words.
    # A light paraphrase reuses most content words but reorders them, so it
    # scores low; a lazy copy keeps the same word pairs, so it scores high.
```

Above 0.55 overlap it retries once with an explicit "rewrite harder" nudge and keeps whichever result is further from the input.

Also rejects non-answers: `"chat limit"`, `"rate limit"`, `"unauthorized"` etc. under 400 chars raise instead of being returned as humanized text.

***

## 12. Integration routers

| File | Lines | What |
|:--|--:|:--|
| `drive.py` | 1,022 | Google Drive — **keyless-first**, OAuth optional |
| `chat_export.py` | 537 | transcript markdown + PDF via vendored marked.js |
| `tracknote.py` | 540 | notes, schedules, calendar expansion, agent ops |
| `library.py` | 314 | browse generated content, PDF→markdown import |
| `email.py` | 426 | IMAP read / SMTP send |
| `search_render.py` | 393 | proxy real search pages into a same-origin iframe |
| `rooms.py` | 204 | bridge-proxied study rooms |
| `account.py` | 197 | bridge auth, session storage |
| `personas.py` | 213 | persona CRUD + active + output-format toggle |
| `terminal.py` | 199 | PTY over WebSocket |
| `image_stream.py` | 202 | SSE image generation with real cancellation |
| `research.py` | 180 | deep research start/poll/cancel/SSE |
| `setup.py` | 358 | first-run wizard |
| `checkpoints.py` | 78 | checkpoint diff/restore |
| `auth.py` | 48 | login + change-password |
| `deepseek.py` | 40 | file upload proxy |

### 12.1 `drive.py` — keyless by default

The design statement:

```
Public  → keyless (embeddedfolderview + usercontent)   ← default
Private → API key or OAuth 2.0                          ← optional
```

Every call tries `engine.drive.public` first and only falls back to the authed client when that fails **and** credentials exist. `drive_stream_pdf` does two jobs in one download: streams to the viewer *and* accumulates for `_ingest_once`. `/raw` exists because `/pdf` mislabels non-PDF bytes — *"/pdf is the wrong door for those: it mislabels the bytes and silently imports them into the library as a fake PDF."*

`_ingest_once` skips re-indexing when the doc id is unchanged:

```
# A Drive auto-save re-ingests the SAME file on every viewer save; re-running
# the embedding pass each time would burn CPU for nothing.
```

`_redirect_uri` normalizes `0.0.0.0` → `localhost` because Google requires loopback. `_oauth_state` is TTL'd (600 s) and capped (32) with oldest-first eviction.

### 12.2 `chat_export.py` — self-escaping fences

```python
def _fence(text, lang="text"):
    """Wrap text in a code fence that its own content cannot break out of."""
    longest = run = 0
    for ch in text:
        if ch == "`": run += 1; longest = max(longest, run)
        else: run = 0
    bar = "`" * max(3, longest + 1)
```

Content containing ```` ``` ```` gets a 4+ backtick fence. PDF path base64's the markdown into the page and parses with the project's own `web/vendor/marked.min.js`, so the PDF matches the UI exactly.

`_replay_events` mirrors `web/js/sse.js` ordering: `round_thinking` / `round_text` / `skill_start` / `skill_output` / `skill_end` folded back by tag id, plus `critique_*`, `permission_request`, `cwd_warning`, `agent_result`, `file_edit`, `memory_used`. Legacy `thinking` column only renders when no `round_thinking` events exist, to avoid duplication.

### 12.3 `search_render.py` — the frame trick

```
Why the page has to come from Sable's origin: DuckDuckGo (and Google, Bing,
Brave) send ``X-Frame-Options: SAMEORIGIN``, so a frame pointing straight at
them is refused by the browser and renders blank. When the HTML is served by
Sable instead, that rule is ours to omit and the frame is allowed.
```

`_proxy_page` rewrites `<base>`, all `link[rel~=stylesheet|preload|icon]`, `script[src]`, `img[src]`/`srcset`, inline `<style>`, and `style=` attributes to `/api/search/asset?u=<b64>`. A `SHIM` script overrides `fetch` and `XMLHttpRequest.open` so the page's own JS requests route through the proxy too.

The token rides in the **query string** on every subresource because *"a subresource load is a plain browser request and cannot set an Authorization header."*

`/api/search/g/{path}` is a second, different approach — proxy Google whole with cookies in a per-session `curl_cffi.Session`, rewriting `google.com` → `/api/search/g` so the JS proof-of-work gate completes on Sable's origin. `/api/search/frame/google` is a **third** approach: frame google.com directly with `igu=1`, which suppresses `X-Frame-Options`, so the gate runs in the user's own browser.

```
Proxying the gate was the wrong shape: it made every hop look like a server,
so Google answered with a CAPTCHA. A direct frame makes every hop look like
 the person actually asking.
```

Three coexisting strategies for one problem — worth knowing they're all there.

### 12.4 `terminal.py` — capability probes

PTY over WebSocket. POSIX uses `loop.add_reader` on the master fd (zero-copy async); Windows polls ConPTY in an executor. `_respond_to_probes` answers `xterm.js` capability queries server-side so fish doesn't hang:

```
# NOTE: do NOT answer XTGETTCAP (+q) queries — fish leaks our replies
# into the command line as typed text. It degrades gracefully without them.
```

Answers: kitty keyboard (`ESC[?0u`), XTVERSION, OSC 11 background, DA1, CPR.

### 12.5 `image_stream.py` — real cancellation

```
Cancellation is real: the subprocess is killed when the client disconnects, so
 the Stop button aborts work in flight rather than just hiding the spinner.
```

`watch_disconnect` polls `request.is_disconnected()` every 250 ms and `proc.kill()`s. `PROGRESS {json}` lines on stderr become per-image SSE events; anything else on stderr becomes a `log` line. Final stdout JSON is compared against already-emitted images (`json.dumps(sort_keys=True)` set) so nothing is duplicated.

### 12.6 `personas.py` — route ordering + Qwen gating

```
# ─── Specific routes FIRST (before /{name} catch-all) ─────────────────────
```

Activating a persona only pushes to Qwen when the caller is actually on Qwen:

```
# Only push to Qwen's personalization slot when the caller is ACTUALLY on
# Qwen. On any other provider the persona rides in the request prompt, and
# pushing here would spam chat.qwen.ai with settings/update calls for a
# provider the user isn't even using.
```

Setting `Agent` auto-disables `output_format`.

### 12.7 `account.py` — never clear on a bridge 401

```
# NEVER clear the local auth token here. A bridge 401 only means the
# bridge session expired/unreachable — the local token is still valid
# for the API middleware. Clearing it caused the login → reload → 401
# → login loop.
```

`logout` revokes **only this browser's token** (`_auth.revoke_token`), not the whole install.

### 12.8 `auth.py` — accept any live session

```
# Accept ANY live session token, not just the primary one. A secondary
# browser validates its cached token through here on every boot — comparing
# only against AUTH_TOKEN used to 401 it and bounce it back to login.
```

`change-password` writes the file, sets `AUTH_TOKEN`, **and** registers the new token in `VALID_TOKENS` + `_persist_sessions()` so other browsers keep working.

### 12.9 `setup.py` — memory/file sync

```
# IMPORTANT: consult the in-memory AUTH_TOKEN (what the auth middleware
# actually validates against), not just the on-disk file. If the startup
# flow mutates the token after import, the file and memory can diverge and
# the frontend will loop between setup/login because setup says 'done'.
```

`set_password` asserts the in-memory assignment took before returning, and `_set_default_persona()` switches first-run users to `Agent` with `output_format_enabled: false`.

`setup_add_api_key` is a two-step: save the key, then **fetch and auto-register** that provider's models as custom models (skipping ids that already exist, adding a Thinking mode for `think`/`reason`/`o1`/`o3`/`r1`).

### 12.10 `library.py` — delete scoped to two dirs

`library_asset_delete` searches **both** `ASSETS_DIR` and `UPLOAD_DIR`:

```
# The gallery lists images from BOTH ASSETS_DIR (generated) and UPLOAD_DIR
# (uploads). This route used to search only ASSETS_DIR, so deleting any
# uploaded image answered "Not found" even though it was on screen.
```

Uses `Path(body["filename"]).name` (strips any directory part) then verifies `target.parent == base.resolve()` — which can now only fail on a symlink escape.

### 12.11 `tracknote.py` — calendar expansion

`/api/calendar/events` expands three schedule types into per-day occurrences: `daily` (bounded by start/end), `weekly` (`day_of_week`, 0=Mon), `occasional` (start_date only). Plus notes with `due_date` and enabled agent ops with `next_run`. Then a fourth source — **online schedules** — from either MongoDB (`_fetch_mongo_source`, all non-`system.*` collections) or HTTP JSON (`_fetch_http_source`), cached 5 minutes. Seeded with a default RUET CSE source on first run.

### 12.12 `checkpoints.py` (78) + `deepseek.py` (40) + `research.py` (180) + `rooms.py` (204) + `email.py` (426)

Thin wrappers. `checkpoints` → `engine.checkpoint` diff/restore. `deepseek` → `service.upload_deepseek_file` for a temp file. `research` → `engine.research.manager` with SSE subscribe/unsubscribe + desktop notification on terminal phase. `rooms` → `server.bridge` REST proxy; the WS URL is returned **with the token** because *"Browsers cannot set an Authorization header on a WebSocket handshake."* `email` → IMAP/SMTP with `_find_sent_folder` probing 5 common names and STARTTLS-vs-SSL chosen by port (465 = direct SSL).

> [!NOTE]
> `deepseek.py` annotates `dict[str, Any]` but never imports `Any`. Harmless because `from __future__ import annotations` makes annotations lazy strings that are never evaluated — but a `typing.get_type_hints()` call on that route would raise.

***

## 13. Pattern summary

Six conventions hold across all 30 files:

1. **Validate ids before touching disk** — `_valid_id` (uuid hex) or an extension whitelist in nearly every file.
2. **Atomic writes** — `.tmp` → `replace`, everywhere.
3. **`exclusive_lock` for read-modify-write** on any shared JSON store.
4. **`asyncio.to_thread` for every blocking call** — `subprocess.run`, `shutil`, PDF parsing, HF API.
5. **Background tasks held in a module-level `set`** with `add_done_callback(discard)`.
6. **Per-item `try/except` so one failure never kills a batch.**

***

## 14. Findings

> [!CRITICAL]
> **1. `processes.py` `_system_cpu_percent` → `NameError` on `prev["pct"]`.** §2. Fires when two polls land in the same jiffy. **Not fixed.**

> [!CRITICAL]
> **2. `agents.py` `kill_agent` → `NameError` on `logger`.** §3. `logging` is never imported. Fires exactly when the upstream-stop fails — the case the handler exists for. **Not fixed.**

> [!CRITICAL]
> **3. `scraper.py` missing `HTTPException` and `_resolve_connector`.** §4. Both `/api/connectors/{backend}/*` routes raise `NameError` on every call. **Not fixed.**

> [!CRITICAL]
> **4. `filesystem.py` `_is_allowed` is lexical only — symlinks escape the browse roots.** §1.2. Combined with the auth-exempt `/api/filesystem/` prefix this is a LAN-reachable arbitrary read. **Not fixed** (behaviour change needed).

> [!WARNING]
> **5. `dashboard_provider_status` returns `bool` for most providers but an object for `qwen`.** §8.

> [!WARNING]
> **6. Three coexisting search-proxy strategies** in `search_render.py` (full proxy, Google passthrough with session cookies, direct `igu=1` frame). Only the last is documented as correct; the other two remain live. §12.3.

> [!NOTE]
> **7. Dead code:** `ocr.py` `MAX_RETRIES`/`RETRY_BASE_DELAY`, `study_cover.py` `_find_chrome`/`_find_firefox`, `search_render.py` `_g_sessions`/`Optional`.

> [!NOTE]
> **8. `deepseek.py` uses `Any` without importing it** — safe only because annotations are lazy. §12.12.

No code changed in these files.

***

## 15. Cross-references

- [[17-server-chat-route]] — `/api/chat` is the consumer of `fork_history`, `request.cwd`, `_spawn_index_job`
- [[18-server-settings-route]] — shares `_set_account_browser`, `_read_system_settings`, the profile regex
- [[19-engine-library-index]] — `study.py` and `drive.py` both drive `run_job`
- [[22-server-study-routes]] — `study_notes`, `study_srs`, `study_dict`, `study_cover`, `study_humanize` are its satellites
- [[23-server-memory-and-chats]] — `personas.py` calls `chat.mark_qwen_synced`
- [[11-server]] — `server.auth` (`is_valid_token`, `revoke_token`, `VALID_TOKENS`), `server.bridge`, `server.database`
- [[12-connectors]] — `_get_available_backends` probes every one of them

> [!IMPORTANT]
> The three `NameError` bugs (findings 1–3) share a cause: each sits in an error path that only executes when something else has already failed. They are invisible in normal operation and fatal in the one moment they matter. That is the single best argument for the per-file review this documentation set records.
