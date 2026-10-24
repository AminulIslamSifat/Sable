---
tags: [connectors, browser, profiles, instruction]
status: full-read
read: profile_hub.py (883L), scraper_connector.py (809L), instruction_builder.py (738L)
---

# 42 — The Shared Connector Layer

> Three files under `connectors/common/` that every browser-scraped provider sits on: the profile-level browser multiplexer, the connector wrapper, and the system-prompt assembler. 2,430 lines.

| File | Lines | Role |
|:--|--:|:--|
| `profile_hub.py` | 883 | One browser per profile dir, multiplexed by tab |
| `scraper_connector.py` | 809 | `ConnectorProtocol` over any `BaseBrowserEngine` |
| `instruction_builder.py` | 738 | The system prompt — persona, tools, formats, skills |

These are the most careful files in the connector tree. The findings are **one design flaw in the prompt assembler** and a set of dead constants — no 🔴, no 🟠.

***

## 1. `profile_hub.py` — the kill-war fix, and it holds

The module docstring states the problem and the fix with unusual precision:

> *Kimi, DeepSeek, Gemini-web, etc. each need a real browser page … The naive design gives each connector its own `--user-data-dir`, but Chromium allows only ONE browser process per profile dir (SingletonLock).*
>
> *One shared browser per profile dir, multiplexed by TAB.*

And then it names the specific three-way bug that motivated it:

```
* each ghost launches its own Helium on that dir -> the loser dies on the lock;
* each ghost only adopts its OWN marker (``.kimi_ghost.json`` vs
  ``.deepseek_ghost.json``), so neither can see the other's live browser;
* ``_pid_is_our_helium()`` matches BOTH (same binary + same profile dir), so
  each connector treats the other's browser as its own orphan and kills it.
```

That is a post-mortem written before the next person re-introduces it. The fix — a profile-level `.profile_hub.json` marker recording the CDP port, so *any* process can adopt — is the right one.

### 1.1 Four port-discovery sources, with the reason for each

```python
def _adopt(self):
    ports = []
    # 1. our marker — fastest, no probing
    # 2. the holders' --remote-debugging-port flag — the only source
    #    that works for Helium, which writes no DevToolsActivePort
    # 3. DevToolsActivePort — standard chromium, kept as a bonus
    # 4. a listening-port sweep of the holders' fds — resolves =0 (random port)
```

Each fallback exists because a specific browser or launch mode defeats the previous one. `_devtools_port`'s docstring even records the negative result — *"Helium does **not** (verified: its profile dir only ever holds `Singleton*`)"*.

### 1.2 PID recycling is handled explicitly

```python
def _lock_is_stale(profile: Path) -> bool:
    """Junk means: unparsable pid, dead pid, lock minted on another host, or —
    the case that used to wedge us — **no process actually holds the
    profile**, i.e. the pid was recycled onto something unrelated."""
```

A Chromium `SingletonLock` is a symlink to `hostname-pid`. The kernel reuses PIDs, so a lock from a browser that died an hour ago can name a live, unrelated process. Checking `pid_alive` alone is not enough; `_profile_holder_pids` verifies by scanning `/proc/*/cmdline` for the actual `--user-data-dir`. That is the correct check and the comment says which bug it fixed.

### 1.3 `_cmdline_tokens` handles a malformed `/proc`

```python
# /proc/<pid>/cmdline is *supposed* to be NUL-separated, but some
# launchers (and Helium's own wrapper) rewrite it as a single
# space-separated string with no NUL bytes. Splitting only on \0 then
# yields one giant token and every arg check fails — a silent false negative.
text = raw.replace(b"\0", b" ").decode("utf-8", "replace")
```

An ownership check that silently returns "not ours" is how a browser gets killed by the wrong connector. Normalising both layouts so they parse identically is the fix, and the comment names the false-negative it prevents.

### 1.4 The background-timer flags have a measured justification

```python
# A ghost tab is never the focused tab, so Chromium throttles its timers
# to 1s (then 1min once hidden >5min). That stretched the SPA's sentinel
# proof-of-work + fetch dispatch to 60-120s BEFORE the request even left
# the page — the "5 minute send" was almost entirely client-side throttling.
"--disable-background-timer-throttling",
"--disable-backgrounding-occluded-windows",
"--disable-renderer-backgrounding",
```

Five flags that look like cargo cult, each traceable to a 60–120 s delay. Without the comment someone trims them and the send latency silently returns.

### 1.5 `_owns_browser` guards adoption teardown

```python
# Only close the browser if WE launched it. An adopted browser belongs
# to another launcher — closing it here would kill their session.
if self._owns_browser:
    …
# Don't delete a marker we didn't write — it may be another Sable
# process's adopt handle.
if self._owns_browser:
    self._marker.unlink(missing_ok=True)
```

Two separate guards for the same boolean, and both are load-bearing. An adopted hub that tore down the browser would kill the parent connector's live session — exactly the failure the hub exists to prevent.

***

## 2. `scraper_connector.py` — recovery with a duplicate-send guard

The core of this file is a retry loop where the hard part is **not sending the message twice**. The design is documented in-line:

```
# _sent_upstream is the duplicate guard: once the message has reached
# the SPA, a retry must NOT send it again (the model would answer
# twice). Capture-phase failures therefore retry capture only, after
# navigating back to the same upstream conversation.
```

And a second flag for the case where the browser is healthy:

```python
class _ThoughtOnlyRetry(Exception):
    """Internal signal: re-capture on the SAME browser, never tear it down.

    A thought-only turn (the model streamed a full chain of thought and then
    stopped with no answer text) is not a browser fault. … killing + relaunching
    it cannot conjure an answer upstream never sent.
    """
```

That distinction — *browser fault* vs *upstream produced nothing* — is the one most retry loops get wrong. This one routes them to different handlers, and says why in the exception docstring.

### 2.1 Rate limiting is not retried

```python
# Rate limit is NOT a browser fault. Relaunching the browser and
# re-sending the same message to a limiter that just said "too
# frequent" cannot succeed, and every retry re-hits the locked
# endpoint. Surface it as `rate_limited` so the chat route can
# switch accounts instead of this loop burning 3 browser restarts
# and then reporting a bogus capture_empty.
if code == browser_errors.RATE_LIMITED:
    yield {"type": "rate_limited", …}
    return
```

Same principle as doc 37's circuit breaker: classify the failure and let the layer that can actually fix it act. Three restarts against a rate limiter is three wasted browsers.

### 2.2 The engine-scope keying solves a real deadlock

```python
# A stream that pins its own browser_data_dir (critique and agent
# sub-chats) gets a wholly separate engine: separate browser profile,
# separate page, and — critically — its own active_chat_id. Without
# this the sub-chat and the parent chat take turns tripping the
# chat_id != active_chat_id check, each call firing new_chat() which
# resets system_injected and replays the hello warmup forever.
```

And the `::shared` fallback when isolation is impossible:

```python
# if the pinned dir resolves to the SAME profile the account already owns,
# isolation is impossible — engines launch with kill_existing=True, so a
# second browser would kill the first.
```

Plus `_shared_parent` as a second guard in the loop — *"a sub-chat riding a shared parent engine must NEVER call new_chat()"*. Two layers for one hazard, and both explained.

### 2.3 `_capture` kills its task on abort

```python
# The capture task MUST die with this generator. If the frontend aborts
# the SSE stream (Stop button), this generator is closed at the `yield`
# below and the `while not task.done()` loop never resumes — leaving the
# task alive as an orphan. That orphan keeps polling the SPA and, worse,
# keeps draining engine._sse_chunks with a stale index, so the NEXT
# turn's chunks get split between the orphan and the real capture and
# the new answer comes back truncated or empty.
```

`except (asyncio.CancelledError, GeneratorExit)` cancels and awaits the task, then `finally` cancels again if still pending. Compare `browser/session.run()` (ledger #69), which lets a timeout expire without cancelling — this is the correct version.

***

## 3. `instruction_builder.py` — the persona is gated, the **prompt structure** is not

The file assembles the system prompt from up to nine parts. Most of it is well-guarded:

| Section | Gate |
|:--|:--|
| persona / personal.md | `_inj.get("persona", True)` |
| output_format.md | `_inj.get("output_format")` + persona config + project config |
| facts / git | `_inj.get("facts")` / `_inj.get("git")` |
| skills registry | `_inj.get("skills", True)` |
| tool format primer | `_inj.get("tool_format", True)` |
| tool schemas | `_inj.get("tools", True)` |
| MCP | `_inj.get("mcp", True)` |
| output dir | `_inj.get("output_dir", True)` |

`layout_mode` then branches three ways — `study` (persona + study tool schemas + directive), `chat` (**persona + personal.md + output format + tools, no skills**), and the default `agent` path (everything).

### 3.1 Chat mode keeps the persona and drops everything that controls the model

```python
else:
    # Chat mode: only web_search + chat_title, no skills, no MCP, no format primer
    _chat_allowed = ["online_search", "chat_title"]
    _chat_schemas = get_all_tool_schemas([], allowed=_chat_allowed, tier=None)
```

The comment is accurate about *tools*. It omits that the persona, `personal.md` and `output_format.md` are appended **before** this branch, and that `_active_tool_format` is left `None` so the final reinforcement block never runs.

So a `layout_mode="chat"` prompt is: full Maria persona + personal details + output-format rules + two tool schemas + **no tool-call format instruction at all**. The model is told who it is and that it has two tools, but never told how to call them.

That is fine *if* chat mode always uses native function calling, where the format section is `_TOOL_FORMAT_NONE` ("handled via native API function calling") and the absence is correct. It is wrong for any provider that relies on a prompt-injected format — which per `_PROVIDER_TOOL_FORMATS` is most of them (`deepseek`, `qwen`, `zai`, `mimo`, `kimi`, `chatgpt`, `mistral_web`, `claude_scraper`).

Finding #129.

### 3.2 The DSML constants are self-consistent

```python
# DeepSeek V4 DSML format — uses ASCII pipe | (U+007C), NOT fullwidth ｜
_DSML_O = "<|DSML|tool_calls>"
```

Verified: the constants use ASCII `|`, matching the comment. `_NAMED_FORMAT_DESCRIPTIONS["dsml"]` uses fullwidth `｜` (`\uff5c`) instead — an inconsistency inside this file, but that entry is only consumed for **human-readable guidance** in the malformed-tool-call warning, not for parsing. Finding #131, 🔵.

### 3.3 `_filter_skills_prompt` is dead

A 25-line substring-based section filter, defined at line 78, called nowhere. The live path filters by passing `disabled=` into `SkillEngine` before the prompt is generated, which is the correct approach. Finding #130, 🔵.

### 3.4 Two smaller dead things

- `_instruction_version` / `invalidate_cache()` / `get_instruction_version()` — an exported cache-busting counter, incremented nowhere and read nowhere in this file. The prompt is rebuilt from disk on every call, so there is nothing to invalidate.
- `_TOOL_FORMAT_QWEN` and `_TOOL_FORMAT_HERMES` both wrap in `<action>`, but `_NAMED_FORMAT_DESCRIPTIONS["hermes"]` describes `<invoke>` inside `<action>` while `_TOOL_FORMAT_HERMES` instructs exactly that. Consistent — noting only because the two formats share an open tag and a reader will assume they differ.

***

## 4. Findings

**No 🔴, no 🟠.** One 🟡 in the prompt assembler; the rest is dead code.

### 🟡 Correctness

**#129 — `layout_mode="chat"` emits no tool-call format instruction.** The `else` branch says *"no format primer"* in a comment that only mentions tools. But persona, `personal.md` and `output_format.md` are still appended, and `_active_tool_format` stays `None`, so neither the primer nor the end-of-prompt reinforcement runs. Correct for `native`; wrong for the eight providers whose `_PROVIDER_TOOL_FORMATS` entry is a prompt-injected format. A chat-mode turn on DeepSeek/Qwen/Kimi gets two tool schemas and no instruction on how to call them.

### 🔵 Dead code / cosmetic

| Where | What |
|:--|:--|
| `instruction_builder._filter_skills_prompt` | 25 lines, zero callers — filtering happens via `SkillEngine(disabled=…)` instead (**#130**) |
| `instruction_builder.invalidate_cache` / `get_instruction_version` | Cache-busting counter with no writer and no reader; the prompt is rebuilt from disk every call |
| `instruction_builder._NAMED_FORMAT_DESCRIPTIONS["dsml"]` | Uses fullwidth `｜`; the actual constants use ASCII `\|` (**#131**) |
| `profile_hub._pid_is_headless` | Returns `None` for unreadable cmdline; `_profile_is_headless` treats `None` as not-headless, so an unreadable holder reads as headed |
| `scraper_connector._lock_for` | Per-scope `asyncio.Lock` — the lock is acquired for the whole send+capture, so two turns on one scope serialise even when the second is a different chat |

***

## 5. What's right, and worth copying

1. **The three-bullet post-mortem in `profile_hub`'s docstring.** It names the exact interaction that caused the kill-war, so the next person recognises it before re-introducing it.
2. **Four port-discovery sources, each with the case that defeats the previous one.** Including a recorded *negative* result for Helium.
3. **`_lock_is_stale` checking for real profile holders, not just a live PID.** PID recycling is a real failure mode and the comment says it wedged them once.
4. **`_cmdline_tokens` normalising both NUL and space layouts.** A false negative in an ownership check kills the wrong browser.
5. **`_sent_upstream` + `_ThoughtOnlyRetry`.** Two flags that separate "resend is dangerous" from "the browser is fine, the model said nothing".
6. **`RATE_LIMITED` short-circuits the retry loop.** Three restarts against a limiter is three wasted browsers.
7. **Every background-timer flag with its measured delay in the comment.**

***

## 6. What this changes upstream

- `connectors/common/` is now read: `browser_scraper.py` (1,517, doc 27), `openai_compat.py` (989, doc 30), `profile_hub.py` + `scraper_connector.py` + `instruction_builder.py` (2,430, this doc). Remaining: `native_tools.py` (346), `media.py` (335), `prompt_injection.py` (283), `browser_errors.py` (219), `jitter.py` (169), `context_summarizer.py` (162), `history_budget.py` (89), `error_ctx.py` (84) — ~1,690 lines.
- Python coverage: **~71,750 → ~74,180 / 137,031 = ~54%**. Application Python: **~60%**. Overall: **~38% → ~39%**.

***

## Cross-references

- [[27-connectors-browser-scraper]] — `browser_scraper.py`, the engine this connector wraps
- [[30-markdown-compat-humanizer]] — `openai_compat.py`, the API-side counterpart
- [[12-connectors]] — the provider contract and `get_connector`
- [[29-engine-skills-handlers]] — `critique` and agent sub-chats, the `::shared` scope's users
