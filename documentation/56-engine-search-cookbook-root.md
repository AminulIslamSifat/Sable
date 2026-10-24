# 56 — `engine/search/`, `engine/cookbook/`, and Four Root Files

5,817 lines. Closes `engine/search/` and `engine/cookbook/`, plus the last four unread `engine/*.py` files.

| Area | Lines | State |
|:--|--:|:--|
| `engine/search/` | 2,528 | **closed** — 7 files |
| `engine/cookbook/` | 2,407 | **closed** — 9 files |
| `wsl_browser.py` + `cookie_reader.py` | 576 | full |
| `think_parser.py` + `process_utils.py` | 283 | full |

***

## 1. `search/` — six providers behind one interface

### 1.1 `build_enhanced_query` corrupts every time-filtered query — 🔴

```python
time_map = {"day": "d", "week": "w", "month": "m", "year": "y"}
if time_filter in time_map:
    enhanced_query = f"{enhanced_query} after:{time_map[time_filter]}"
```

That appends the literal string `after:d` / `after:w` / `after:m` / `after:y` to the query. Google's `after:` operator takes a **date** (`after:2024-01-01`). A single letter is not a date, so the operator is either ignored or the whole clause is treated as search terms.

Every caller then *also* passes a proper freshness parameter:

| Provider | Broken query suffix | Correct param also sent |
|:--|:--|:--|
| `brave_search` | `after:d` | `freshness=pd` |
| `google_pse_search` | `after:d` | `dateRestrict=d1` |
| `serper_search` | `after:d` | `tbs=qdr:d` |
| `tavily_search` | `after:d` | `topic=news` |

So a time-filtered search sends a malformed operator **and** the working one. The result is a query the engine has to interpret around, on every provider, on every time-filtered call.

> [!IMPORTANT] It is `build_enhanced_query`, not a per-provider quirk
> `providers.py` calls it at the top of all four functions. Fixing it once fixes all four; nothing currently exercises it because the tests directory is unopened.

### 1.2 `duckduckgo_search` blocks the event loop for up to 4 s

```python
_DDG_MIN_INTERVAL = 4.0
wait = _DDG_MIN_INTERVAL - (_time.monotonic() - _last_ddg_request_ts)
if wait > 0:
    _time.sleep(wait)
```

`time.sleep` in a `def` that the tool handler dispatches through `asyncio.to_thread` is fine. Reached from an async route directly, it stalls the loop. The throttle itself is correct and well-explained — DDG serves an image CAPTCHA to fast IPs — but the blocking primitive is chosen without regard to the caller.

### 1.3 `_probe_image` streams a GET to read only headers

```
Uses a streamed GET so only headers are read — the body is never downloaded.
HEAD is unreliable: plenty of CDNs 403 it while serving GET.
```

Correct, and the reasoning is the part people get wrong. `searxng_image_results` then validates candidates with a 10-worker thread pool and **early exit** once `count` pass — with a comment explaining that without early exit a 2 s search became 15 s. Junk-engine filtering (`lucide`, `artic`, `devicon`) is listed explicitly, because SearXNG aggregates icon libraries alongside real image engines.

### 1.4 `sable_provider._read_marker` kills the shell on Windows — 🔴

```python
pid = data.get("pid")
if isinstance(pid, int) and pid > 0:
    try:
        os.kill(pid, 0)
    except OSError:
        return None
```

On POSIX `os.kill(pid, 0)` is a liveness probe. On Windows, Python's `os.kill` only special-cases `CTRL_C_EVENT` and `CTRL_BREAK_EVENT`; **every other signal value — including 0 — calls `TerminateProcess`**. So on Windows, reading the search-bridge marker terminates the desktop shell whose pid it just read.

`engine/platform_paths.pid_exists()` exists for exactly this reason, with the comment:

```
# os.kill(pid, 0) is POSIX-only and *terminates* the target on Windows.
```

Five other files in the tree use `pid_exists()`. This one does not.

### 1.5 The bridge is authenticated per run

```
The marker carries a per-run bearer token; the bridge refuses requests
without it, so nothing else on the machine can drive the shell.
```

`X-Sable-Token` from `~/.cache/sable/search-bridge.json`. Availability is gated on the marker's existence, so on a plain web deployment the provider simply does not exist rather than failing at call time. Both correct.

### 1.6 `browser_provider` is named for something it no longer does

```
The provider is still registered under the name ``browser`` because that is what
settings.json and the settings dropdown already point at, but the name is now
historical: nothing here starts a process.
```

Plain `curl_cffi` with `impersonate="chrome124"`. Weighted RRF over Brave (1.0) and Bing (0.7), `k=10`, with an agreement multiplier. Google and Yandex are excluded because they need JavaScript to produce results. The docstring says all of this; the module name says the opposite.

### 1.7 `ranking.py` guards a real deprecation

```python
def _utcnow_naive() -> datetime:
    """Naive UTC 'now'. Safe on Python 3.14 where datetime.utcnow() is removed."""
```

Every other module that needs "now" calls `datetime.now(timezone.utc)`. This one needed a naive value to compare against `strptime` output and did it without the deprecated call.

`news_quality_adjustment` is the most opinionated scoring in the tree: `+1.2` trusted news domains, `-0.8` for `msn.com`/`yahoo.com`/`facebook.com`, `-1.5` for sports content in a non-sports query, `-1.0` when no subject term appears in the title, snippet or domain. That last one is the real filter — it demotes a result that matches only the news words.

***

## 2. `cookbook/` — local model lifecycle

### 2.1 `purge.py` documents the five ghost problem

```
A model touches five places. Deleting only the .gguf leaves four ghosts:
  1. cookbook_state.json        -> servers[] entries
  2. .custom_models.json        -> _cookbook registry entry
  3. cookbook_model_settings.json -> per-model instruction card
  4. cookbook_logs/<serve-id>.log -> server logs
  5. models/llm/<file>.gguf     -> the weights themselves
```

`purge_model` handles all five and returns a count per category. The cascade is the whole point of the module and the docstring is why anyone can verify it did the right thing.

### 2.2 `state.cleanup_stale` is a three-stage self-heal

1. Mark `running` servers stopped if `pid_exists` says the pid is gone.
2. Drop entries whose `.gguf` no longer exists (running ones exempt).
3. Dedupe stopped entries per model path, keeping the newest — and delete the log files of the entries it drops.

Called from `get_state()` on first access, so a stale entry can never reach the UI.

### 2.3 `server.py` — `start_new_session=True` and a 1.5 s liveness check

`Popen(..., start_new_session=True)` so `kill_process_tree` can `killpg` the whole server. Then:

```python
await asyncio.sleep(1.5)
if proc.poll() is not None:
    task.status = "failed"
    raise ServeError(f"Process exited immediately with code {proc.returncode}. Check logs.")
```

A 1.5 s window is generous for a binary-not-found failure and short for a slow model load. A server that dies at 2 s is recorded `running` and only corrected by the next `cleanup_stale`.

### 2.4 `model_settings.build_system_prompt` reads the persona twice

```python
_pcfg_path = instruction_dir / ".persona_config.json"
# first read: active persona
... _pc = _json.loads(_pcfg_path.read_text(...))
# second read: output_format_enabled
... _pc2 = _json.loads(_pcfg_path.read_text(...))
```

Same file, parsed twice, in the same function. Harmless and obviously the result of the output-format check being added later.

The bigger point: this is the **only** provider path that builds its system prompt from cookbook settings rather than `connectors.common.instruction_builder`. A local model served through the cookbook and the same model served through the `local` connector get different prompts. Noted in doc 50 §2.5.

### 2.5 `downloader.py` — cancel means kill

```
Downloads run in subprocesses so cancel = kill. No polite stop_event that
gets ignored by a blocking snapshot_download call.
```

The inline `_DL_SCRIPT` runs in the child, writes progress to a JSON file the parent polls, and substitutes a `HfProgressTqdm` shim so `huggingface_hub` reports real byte counts through a class it never expected. `_dedup_gguf_files` solves a real problem: repos ship both `model-q4_k_m.gguf` and the split `-00001-of-00002` parts, so a glob downloads the model twice.

***

## 3. The four root files

### 3.1 `cookie_reader.py` — the reason `zai` (#160) is broken

The module reads cookies from a profile's SQLite DB **without launching a browser**, which is what makes it usable while a ghost holds the profile. Two schemes:

| Prefix | Key source |
|:--|:--|
| `v10` | PBKDF2 over the hardcoded `b"peanuts"` |
| `v11` | PBKDF2 over the OS keyring secret via `secret-tool` |

Both share `b"v1x" + IV(16) + AES-128-CBC`. `_decrypt_v10` then strips the trailing-16-bytes-of-`SHA256(host_key)` domain tag that modern Chromium prepends, trying 0/16/32-byte strips and picking the candidate with the fewest `\ufffd`.

The rejection rule is the important one:

```
# _decrypt_v10 returns the candidate with the FEWEST replacement chars, which
# for a wrong key is garbage rather than None. Reject lossy output outright: a
# mojibake value is worse than a missing one, because callers send it and get
# an opaque 401.
if dec and "\ufffd" not in dec:
```

That is the exact check MiMo's `validate_auth` reports on (#1.1 in doc 51) — the two files are the two halves of one contract.

### 3.2 `wsl_browser.py` — a Windows Chromium downloader inside a Linux repo

On WSL2, headed Playwright has no display, so this launches Windows-side Chrome with `--remote-debugging-port` and connects over CDP. If no Windows Chrome exists it downloads Chrome-for-Testing from `cdn.playwright.dev` into `system/chromium-win/`, parsing the version out of `playwright install --dry-run` output.

`_get_playwright_chromium_info` does `parts[3]` on a split line and `int(rev_str)` on a regex-free slice, inside a bare `except Exception: pass` returning `None`. A Playwright output-format change silently disables the fallback download with no log line.

### 3.3 `think_parser.py` — the only streaming-safe tag splitter

The state machine holds a partial tag across chunk boundaries: a `<thi` at the end of one chunk and `nk>` at the start of the next become one tag, never leaked as text. `flush()` drains whatever is left.

The comment on the non-tag branch is the subtle part:

```
# Candidate is not a tag prefix. Flush only the buffered text (which
# excludes ch), then handle ch on its own. This avoids emitting ch twice
# when it can seed a new candidate.
```

Used by `local/client.py`, `mimo/client.py`, `qwen_scraper`, and `deepseek/ghost`. Doc 51 §2.4 notes MiMo needs it because its frames are overlapping rather than deltas.

### 3.4 `process_utils.kill_process_tree` — the zombie comment

```
# Why: when a shell does `cmd & exit` under start_new_session=True, the
# shell IS the group leader (pgid == pid). It exits, becomes a ZOMBIE, and
# is never reaped by us — yet its group is still valid and still holds the
# live descendants we actually need to kill. A zombie-only guard here would
# skip the signal entirely and leak the whole tree.
```

Resolve `pgid` **before** any liveness check, `killpg` only when `pgid == pid` (we own the group), and only then fall back to a single-pid kill with the zombie check. The `pgid == pid` guard is what stops a kill from taking out the parent's process group.

***

## 4. Findings

| # | Where | What |
|--:|:--|:--|
| 209 | `search/query.build_enhanced_query` | **🔴** Appends `after:d` / `after:w` / `after:m` / `after:y` to the query — `after:` takes a date, not a letter. Every time-filtered search sends a malformed operator on top of the correct `freshness`/`tbs`/`dateRestrict` param. One function, four providers. |
| 210 | `search/sable_provider._read_marker` | **🔴** `os.kill(pid, 0)` — on Windows Python treats every non-`CTRL_*` signal as `TerminateProcess`, so reading the marker **kills the desktop shell**. `engine/platform_paths.pid_exists()` exists for exactly this and is used by five other files. |
| 211 | `search/providers.duckduckgo_search` | `time.sleep(wait)` up to 4 s inside a plain `def`. Correct when dispatched through `to_thread`; an event-loop stall if a route calls it directly. |
| 212 | `cookbook/server.start_server` | A 1.5 s liveness check cannot distinguish "binary missing" from "slow model load". A server that dies at 2 s stays `running` until the next `cleanup_stale`. |
| 213 | `cookbook/model_settings.build_system_prompt` | Parses `.persona_config.json` twice in one function — once for the active persona, once for `output_format_enabled`. |
| 214 | `wsl_browser._get_playwright_chromium_info` | Parses Playwright's `--dry-run` output by index (`parts[3]`, `split("chromium v")[-1]`) inside a bare `except`. A format change silently disables the Windows-Chromium download with no log. |
| 215 | `search/browser_provider` | Registered as `"browser"` and documented as *"the name is now historical: nothing here starts a process."* Accurate, and a trap for the next reader. |

***

## 5. What this batch shows

- **`engine/search/` was at method level since doc 08 and held two 🔴.** Both are single-line errors with total blast radius: one corrupts every time-filtered query across four providers, the other kills a process on one OS.
- **The `os.kill(pid, 0)` hazard is documented in `platform_paths.py` and still reached a sixth file.** That is now six instances of a documented convention with no mechanism enforcing it — the pattern this whole pass keeps finding.
- **`cookie_reader.py` is the well-written half of a contract whose other half is `mimo.validate_auth`.** Reading them together is the only way to see that the `\ufffd` rejection and the `\ufffd` report are the same design decision.
- **`cookbook/` is the only subsystem whose self-heal runs on access rather than on a timer**, and `purge.py` is the only module that enumerates every store it must clean. Both are good instincts that the rest of the tree does not share.

Python is now **~100,400 / 137,031 ≈ 73%**; overall **~53%**. `engine/search/` and `engine/cookbook/` closed. Remaining in `engine/`: `drive/` (1,401), `security/` + `mcp/` + `notifications/` + `diary/` + `plugins/` (1,978), `scraper/` remainder (~848), `skills/` remainder (~1,611), `browser/` remainder (~9).

> [!NOTE] Partial reads in this batch
> `cookbook/downloader.py` and `search/browser_provider.py` were truncated mid-file by the 39 KB chunking. The functions covered here are the ones fully visible; `downloader.py`'s parent-side polling loop and `browser_provider`'s Bing parser were not read.
