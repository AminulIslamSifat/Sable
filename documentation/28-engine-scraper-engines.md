# 28 — `engine/scraper_engines/deepseek/` + `qwen/`

> [!INFO] Why this file exists
> Priority 2 on the README's coverage list — 3,977 lines across two provider directories. [[06-engine-browser-scraper]] covers `engine/scraper/core.py`; [[27-connectors-browser-scraper]] covers the connector-side engine. Neither covers these.
>
> Read end to end: `deepseek/deepseek_engine.py` (1,487), `qwen/engine.py` (1,369), `deepseek/engine.py` (509), `deepseek/config.py` (51), `qwen/config.py` (50), `deepseek/exceptions.py` (1). **Not** read: `qwen/exceptions.py` (3 lines, and its import is wrapped in `try/except ImportError`), `platforms.json` (data), `qwen/qwen_engine.py.old` (marked dead by its own filename), and `engine/scraper/core.py` (already [[06-engine-browser-scraper]]).
>
> The headline is not a bug — it is that **this directory contains two generations of engine and the older one is not importable.**

***

## 1. What is actually in here

Each provider directory holds **two** engines with the same purpose:

| File | Class | Base | Importable? |
|:--|:--|:--|:--|
| `deepseek/deepseek_engine.py` | `GhostChat` | **none** | ✗ |
| `deepseek/engine.py` | `DeepSeekEngine` | `BaseScraperEngine` | ✓ (given the shim) |
| `qwen/engine.py` | `QwenEngine` | `BaseScraperEngine` | ✓ (given the shim) |
| `qwen/qwen_engine.py.old` | — | — | self-marked dead |

`deepseek_engine.py`'s class is named **`GhostChat`** and declares **no base class at all** — it reimplements `launch_chrome`, `connect`, `send_msg`, `get_response`, `cleanup` from scratch. It is a vendored standalone script, not a subclass of anything.

`qwen/engine.py` even carries the alias for the loader that expects the old name:

```python
# Backward compatibility for dynamic scraper loader that looks for GhostChat class.
GhostChat = QwenEngine
```

That comment is the archaeology: a loader once looked for `GhostChat`, the clean engines renamed themselves, and the alias was left behind.

> [!IMPORTANT] Qwen's old engine was renamed to `.old`; DeepSeek's was not
> `qwen/qwen_engine.py.old` declares itself dead in its filename. `deepseek/deepseek_engine.py` is in the same state and carries **no such marker** — it sits in the tree at 1,487 lines looking like a live engine.

***

## 2. Neither directory is importable as a package

Both `config.py` shims open with a bare module import:

```python
def get_ui_metadata(self) -> dict[str, Any]:   # deepseek_engine.py
```

```python
from config import console, PLATFORM, PLATFORMS_CONFIG, OUTPUT_ROOT, ...
from exceptions import ResponseCaptureError
```

There is no top-level `config` module in Sable — there is `engine/config.py` and `server/config.py`. So `import engine.scraper_engines.qwen.engine` raises `ModuleNotFoundError: No module named 'config'` unless the provider directory itself is on `sys.path`.

`qwen/config.py` also sets:

```python
SABLE_ROOT = PROJECT_ROOT.parents[2]   # comment says "-> parents[3]"
```

The comment and the code disagree, and the code is right: `qwen/` → `scraper_engines` → `engine` → repo root is three `parents` steps, not four. Harmless, but it means the comment was copied from a deeper layout and never re-checked.

**Net effect:** these files work only when executed as scripts from inside their own directory (`python deepseek_engine.py`). They are not part of the running application. `deepseek/engine.py` and `qwen/engine.py` are *closer* to package-clean — they import `engine.scraper.core` — but the bare `from config import` still breaks them as modules.

An import scan run for this document found **no live importer** of `engine.scraper_engines.deepseek.*`. Consistent with script-mode, not proof of it — flagged rather than asserted.

***

## 3. `deepseek_engine.py` cannot load

No `from __future__ import annotations` at the top, and no `from typing import Any`:

```python
def get_ui_metadata(self) -> dict[str, Any]:
```

Annotations are evaluated at `def` time without the future import, so this line raises `NameError: name 'Any' is not defined` **during class body execution** — i.e. at import. The file never gets as far as running.

Two more undefined names would fire immediately after:

| Symbol | Used in | Defined? |
|:--|:--|:--|
| `Any` | `get_ui_metadata` annotation | ✗ (import-time) |
| `Path` | `launch_chrome`: `Path(self.user_data_dir).name` | ✗ (no `from pathlib import Path`) |
| `_SESSION_ID_RE` | `session_id_from_url`: `_SESSION_ID_RE.search(url)` | ✗ |
| `self._wait_for_sse_activity` | `get_response_deepseek` loop | ✗ — no base class to inherit it from |

`session_id_from_url` is the documented DeepSeek URL-comparison helper (its docstring explains `/a/chat/s/<id>`), and it references a regex constant that does not exist anywhere in the file. `_wait_for_sse_activity` was added to `browser_scraper.py` as a latency win; here the call site was copied without the method.

***

## 4. `deepseek_engine.py` is the *unfixed* copy

This is the part that matters for anyone reading it as a reference. Compared with [[27-connectors-browser-scraper]], every fix in the connector engine is **absent** here:

| Concern | `browser_scraper.py` | `deepseek_engine.py` |
|:--|:--|:--|
| SSE reset | **rebinds** `self._sse_chunks = []` so an orphan task cannot write into the next turn | `.clear()` on the shared list — the exact bug the rebind fixed |
| Chunk wake | `_sse_wake` event, `_wait_for_sse_activity()` | none — 0.3s poll only (and the call it does make is undefined) |
| Completion | network-first: `_stream_finished`, `_sse_done`, idle, hard deadline | `while await stop_locator.is_visible()` — if the stop button never paints, the loop never runs |
| Settle | 2.0s done-wait + 0.4s + 1.5s drain + 6s empty-window | none |
| Final line | flushes `_sse_line_buf` | no buffer, no flush |
| Soft rate limit | `SoftRateLimitError` + `detect_soft_error()` + `error_text_markers` | **absent** — a rate-limit banner is indistinguishable from an empty completion |
| Reasoning echo guard | `_last_reasoning_seen`, folds in `dom_thinking` | `_last_thinking` only |
| Send-enable check | real `disabled` / `aria-disabled` / `pointer-events` | substring test on `class` for `ds-button--disabled` |
| Stale marking | unions `response`, `content`, `thoughts`, `old_mark_selector` | hardcoded `div.ds-message, .ds-assistant-message-main-content, .ds-think-content` |

So `deepseek_engine.py` is a faithful snapshot of the engine **before** the last round of capture fixes. Reading it to understand current DeepSeek behaviour gives you the wrong answer on eight separate mechanisms.

> [!CAUTION] The soft-rate-limit gap is the one that bites
> `browser_scraper.py`'s `error_text_markers` exists specifically because DeepSeek renders "Messages too frequent. Try again later." as a **sibling span**, leaving the assistant bubble empty and emitting no SSE. This file has no equivalent: `get_response_deepseek` returns `""`, the connector classifies it as an empty response, retries, rotates the account — the documented *"Sable repeats the same reply forever"* symptom. Present and unfixed here.

***

## 5. `deepseek/engine.py` — the clean one

509 lines, `DeepSeekEngine(BaseScraperEngine)`, and it is a **third** implementation — not a subclass of `browser_scraper.BaseBrowserEngine`, not the `GhostChat` script. It re-implements `send_msg`, `get_response`, `set_thinking_mode`, `_expand_thoughts_panel_if_needed`, `setup_provider` against the `engine/scraper/core.py` base.

Its `get_response` is the **poll-only** design: `while await stop_locator.is_visible()` with a 0.3s sleep, no SSE tap at all. That is the design `browser_scraper.py` replaced. It also re-reads `.last.inner_text()` of the whole response element every tick and re-runs `_clean_garbage` on the full text each time — the O(n²)-per-chunk cost that the connector engine's comment calls out as *"what made long responses progressively laggier than curl_cffi."*

Its `send_msg` reminder is **populated**, unlike the empty `_TOOL_REMINDER` in both other engines:

```python
reminder = (
    "[QUICK REMINDER]\n"
    "1. Use <action>[...]</action> JSON array format for all tool calls (NOT DSML).\n"
    "2. Use <execute_command> to run any command.\n"
    "3. Always use appropriate tags to run commands or use skills.\n\n"
)
```

So there are three different answers to "what does a follow-up turn get prepended with": nothing (`browser_scraper.py`), nothing (`deepseek_engine.py`, all commented), and this block (`deepseek/engine.py`).

> [!NOTE] `_INSTRUCTION_PATHS` is computed and unused in two of three
> `deepseek_engine.py` defines `_INSTRUCTION_PATHS` from `INSTRUCTIONS_DIR / "Maria.md"` etc. and never reads it — `_load_instructions()` delegates to `build_instructions(provider="deepseek")`. `deepseek/engine.py` sets `self.INSTRUCTION_PATHS = ["Maria.md", ...]` (bare filenames, no dir) and also never reads it. `qwen/engine.py` does the same. Three files, three declarations of a constant none of them uses, two of them disagreeing about whether it holds paths or names.

***

## 6. `qwen/engine.py` — the Obsidian proxy is a stub

Qwen's engine is the most elaborate of the three: Obsidian CDP proxying, API-based stop, slash commands, bridge sessions, diary processing, clipboard upload, keep-alive.

### 6.1 `ProxyHTTPHandler` does nothing

```python
class ProxyHTTPHandler:
    ws_port: int = 0
    target_port: int = 0
    def log_message(self, format: str, *args: Any) -> None:
        pass
    def do_GET(self) -> None:
        pass
```

It is served by `socketserver.TCPServer(..., ProxyHTTPHandler)` — not `http.server.HTTPServer`. `BaseRequestHandler.handle()` is a no-op, so every HTTP connection to `http_port` is accepted and then closed with no response. Then:

```python
cdp_port = http_port
...
self.browser = await self.pw.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
```

Playwright is pointed at that port. The **WebSocket** half is real (`websockets.serve(ws_proxy_handler, ...)`, with a `"type": "webview"` → `"type": "page"` rewrite), but CDP discovery starts with a plain HTTP `GET /json/version`, which this handler cannot answer.

So the Obsidian path — `PLATFORMS_CONFIG.get("use_obsidian", True)`, i.e. **on by default** — replaces a working CDP port with a port that answers nothing.

### 6.2 `logger` is undefined

```python
logger.info("Ghost Engine Online")
return
except Exception as e:
    if attempt < 4:
        logger.warning("Retrying connection (%d/5): %s", attempt + 1, e)
```

No `import logging`, no `logger = ...` in the file. The `logger.info` raises `NameError`, which the enclosing `except Exception` catches, whose handler then raises `NameError` again — propagating out of `connect()` on the **first** attempt, with the misleading surface of a connection failure. The `for attempt in range(5)` retry never actually retries.

### 6.3 `process_diary` looks for its scripts in the wrong tree

```python
repo_root = os.path.dirname(os.path.abspath(__file__))     # .../engine/scraper_engines/qwen
summarizer = os.path.join(repo_root, "skills", "diary_creator", "summarizer.py")
```

That resolves to `engine/scraper_engines/qwen/skills/diary_creator/summarizer.py`. The skill lives at `<repo>/skills/diary_creator/`. `repo_root` is the **engine directory**, not the repo root — and this file already imports `PROJECT_ROOT` from its config shim, which is the correct value. The subprocess is launched with a path that cannot exist, and `_summarize_sessions` swallows the failure per-item, so `process_diary` reports *"Something went wrong while summarizing."* with no clue why.

### 6.4 `get_response` can spin forever

```python
while True:
    try:
        ...
    except ResponseCaptureError:
        raise
    except Exception:
        await asyncio.sleep(0.5)
```

`START_TIMEOUT` (120s) only guards the **zero-elements** branch. Once any response element exists, a persistent exception in the body — a selector that throws, an element detached between `query_selector_all` and `_get_element_text_direct` — is swallowed and retried with no deadline and no counter. The three timeouts (`FROZEN`, `SILENCE`, `START`) all live inside the `try`, so an exception that fires before them means they are never consulted.

### 6.5 The keep-alive accumulates

```python
async def _inject_keep_alive(self) -> None:
    setInterval(() => { window.dispatchEvent(new MouseEvent('mousemove', ...)); }, 30000);
```

Called from **both** `connect()` and `setup_provider()`. The script is installed with `context.add_init_script` (survives navigation) *and* `page.evaluate` (runs now), so one call already double-installs on the current page; two calls leave two live intervals per navigation, growing without bound. Nothing clears them — `cleanup()` stops Playwright but never clears the interval, and navigations re-run every accumulated init script.

The script also opens an `AudioContext` with an oscillator at `gain = 0.001` on first mousedown/keydown, and never closes it. A silent, permanent audio node in a headless-capable browser is exactly the kind of thing that keeps an audio server awake.

### 6.6 Fonts are blocked

```python
"**/*.{woff,woff2,ttf}",
```

Icon fonts are the usual source of a SPA's button glyphs. Blocking them changes what renders, which changes what selectors match — and this engine's whole capture strategy is selector-based with a fallback ladder. Not a bug on its own; worth knowing before debugging a selector mismatch here.

***

## 7. Dead code inventory

| File | Symbol | State |
|:--|:--|:--|
| `deepseek_engine.py` | `_get_relevant_memories` | defined, never called |
| | `_INSTRUCTION_PATHS`, `_MEMORY_PATH` | computed, never read |
| | `_is_meaningful_response_text` | defined, never called (Qwen calls its own copy) |
| | `_scroll_to_bottom`, `_auto_skip_preference_vote` | defined, never called |
| | `extract_thoughts` → `_expand_thoughts_panel_if_needed` | only caller is each other; `get_response_deepseek` reads the DOM directly |
| | `clipboard_tool`, `last_tokens`, `last_instruction_hash`, `final_tokens` | assigned, never read |
| | `ResponseCaptureError`, `signal`, `shutil` | imported, unused (`shutil` only via `_detect_clipboard_tool`) |
| `deepseek/engine.py` | `_INSTRUCTION_PATHS`, `self.INSTRUCTION_PATHS` | assigned, never read |
| `qwen/engine.py` | `handle_command` `/bridge` branch | returns the same `(False, False, None)` as the default fall-through |
| | `ProxyHTTPHandler` | §6.1 |
| | `signal` | imported, unused |

***

## 8. Findings

| # | Finding | Severity | Status |
|--:|:--|:--|:--|
| 39 | `deepseek_engine.py` is not importable: `Any` used in an annotation with no future-import, `Path` and `_SESSION_ID_RE` undefined, `self._wait_for_sse_activity` undefined with no base class. | 🟠 medium (dead file) | **not fixed** |
| 40 | `deepseek_engine.py` is the pre-fix copy of the engine — `.clear()` SSE reset, no soft-rate-limit detection, no settle windows, no final-line flush. Reading it as reference gives eight wrong answers. | 🟠 medium | **not fixed** |
| 41 | `qwen/engine.py` `connect()` references undefined `logger`; the `NameError` propagates on attempt 1, so the 5-attempt retry never runs. | 🔴 high | **not fixed** |
| 42 | `qwen/engine.py` `ProxyHTTPHandler` is a stub on a plain `TCPServer`, yet `use_obsidian` defaults to **True** and Playwright is pointed at that port. Obsidian CDP path cannot work. | 🔴 high | **not fixed** |
| 43 | `qwen/engine.py` `process_diary` resolves skill scripts from the engine dir, not the repo root — the subprocess path cannot exist. | 🟠 medium | **not fixed** |
| 44 | `qwen/engine.py` `get_response` swallows every non-`ResponseCaptureError` exception and retries with no deadline once any element exists. | 🟠 medium | **not fixed** |
| 45 | `qwen/engine.py` `_inject_keep_alive` accumulates `setInterval`s across `connect`+`setup_provider` and navigations; never cleared. | 🟡 low | **not fixed** |
| 46 | Neither provider dir is importable as a package — bare `from config import` / `from exceptions import` with no top-level `config` module. | 🟡 low (script-mode by design?) | **not fixed** |
| 47 | Three engines, three different follow-up-turn preambles: empty, empty, and a populated `[QUICK REMINDER]` block. | 🟡 low | **not fixed** |

Nothing here changes the *running* application if these files are script-mode orphans, which the import scan suggests. That is precisely why the two 🔴 entries matter: **nobody would notice**, and the directory reads as live code.

***

## 9. What this changes upstream

- `documentation/README.md`: coverage tables, ledger #39–#47, priority list.
- [[27-connectors-browser-scraper]] §1 pointed here as "the engine copies this file diverged from" — the correction is that they diverged *before* its last round of fixes, and are not the same lineage.
- [[06-engine-browser-scraper]]: note added that there are now **four** browser engines in the tree, not two.

**Next on the list:** `web/js/markdown.js` (809) and `engine/skills/handlers/` (5,401).
