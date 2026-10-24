---
tags: [connectors, errors, throttling, context]
status: full-read
read: browser_errors.py (219L), jitter.py (169L), context_summarizer.py (162L), history_budget.py (89L), error_ctx.py (84L)
---

# 47 — Error Taxonomy, Jitter, History Budget

> The last five files in `connectors/common/`. 723 lines, and the package is now closed. One load-bearing **ordering** invariant, one budget that can split a tool-call pair, and three unused helper functions.

| File | Lines | Role |
|:--|--:|:--|
| `browser_errors.py` | 219 | Stable failure codes, classification, recovery sets |
| `jitter.py` | 169 | Per-provider anti-burst delay, with a smart-skip |
| `context_summarizer.py` | 162 | Two-tier context compression for non-Qwen models |
| `history_budget.py` | 89 | Bounded DB history seeding |
| `error_ctx.py` | 84 | `[provider | account | model]` error prefixes |

***

## 1. `browser_errors.py` — the ordering is the whole design

```python
# Most-specific first: the first pattern that matches wins. Order matters —
# "browser has been closed" must resolve to CONTEXT_CLOSED, not BROWSER_DEAD.
_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    # Must precede CAPTURE_EMPTY/BROWSER_DEAD: a soft rate-limit banner leaves
    # the assistant bubble empty, so without this the failure classifies as a
    # retryable capture problem and gets hammered 3x.
    (RATE_LIMITED, ("messages too frequent", …)),
    …
    (CONTEXT_CLOSED, ("target page, context or browser has been closed", …)),
    …
    (BROWSER_DEAD, (… "browser has been closed" …)),
)
```

`classify()` lowercases `"{type}: {exc}"` and returns the first pattern that matches. Because `"browser has been closed"` is a substring of `"target page, context or browser has been closed"`, `CONTEXT_CLOSED` **must** precede `BROWSER_DEAD` — and it does, with the reason in the comment.

The `RATE_LIMITED`-first rule is the sharper one. A soft rate-limit banner leaves an empty assistant bubble, which is textually identical to `CAPTURE_EMPTY` — a *recoverable* code. Without the rate-limit pattern first, every soft limit would burn three browser restarts against a locked endpoint. That is precisely the bug doc 42 §2.1 shows the connector defending against at the retry site; this is the same defence at the classification site.

Two structural type checks come before the pattern scan:

```python
try:
    from connectors.common.browser_scraper import SoftRateLimitError
    if isinstance(exc, SoftRateLimitError): return RATE_LIMITED
except Exception: pass

try:
    from engine.browser_governor import NoSlotAvailable
    if isinstance(exc, NoSlotAvailable): return SLOT_FULL
except Exception: pass
```

Both are lazy imports wrapped in `except Exception`, so a missing module degrades to pattern matching rather than breaking classification. That is the right shape for a leaf module that must never fail.

### 1.1 Four sets, two of them unused

| Set | Members | Consumed by |
|:--|:--|:--|
| `RECOVERABLE` | 14 codes | `scraper_connector` — `code not in browser_errors.RECOVERABLE` ✅ |
| `NEEDS_USER` | `NO_BROWSER_BINARY`, `NOT_LOGGED_IN` | `scraper_connector` — `code in browser_errors.NEEDS_USER` ✅ |
| `NO_RETRY` | `RATE_LIMITED` | ❌ **nothing** |
| `CAPTURE_ONLY` | `CAPTURE_EMPTY`, `CAPTURE_TIMEOUT` | ❌ **nothing** |

And the three module-level predicates are unused:

```
$ grep -rn 'no_retry(|recoverable(|needs_user(' connectors/ | grep -v backups
browser_errors.py:105:def recoverable(code: str) -> bool:
browser_errors.py:109:def needs_user(code: str) -> bool:
browser_errors.py:113:def no_retry(code: str) -> bool:
```

Nine hits, all definitions. `scraper_connector` uses the sets directly and handles rate limiting with an explicit `if code == browser_errors.RATE_LIMITED:` rather than calling `no_retry(code)`.

`NO_RETRY` and `CAPTURE_ONLY` also duplicate facts that are encoded elsewhere: `RATE_LIMITED` is absent from `RECOVERABLE`, and `CAPTURE_ONLY` describes the `_sent_upstream` guard that `scraper_connector` tracks with a boolean. Two representations of one policy, one of them never read. Finding #151, 🔵.

### 1.2 The comment on `RATE_LIMITED` is a design note worth keeping

```python
# RATE_LIMITED belongs here even though a *different account* could succeed:
# relaunching the SAME browser and re-sending the SAME message to a limiter that
# just said "Messages too frequent. Try again later." cannot possibly work, and
# each retry re-hits the locked endpoint. Account switching is the chat route's
# job — it needs this code surfaced as a `rate_limited` event, not swallowed by
# a browser-recovery retry loop.
```

It names the layer that should own the fix. Same instinct as doc 37's circuit breaker and doc 42 §2.1.

***

## 2. `jitter.py` — the smart-skip is the good part

```python
def jitter_for(provider: str) -> float:
    …
    now = time.monotonic()
    avg = (cfg["min"] + cfg["max"]) / 2.0
    last = _last_send_ts.get(provider)

    if last is not None and avg > 0 and (now - last) >= avg:
        _last_send_ts[provider] = now  # nothing to throttle — mark and move on
        return 0.0

    delay = random.uniform(cfg["min"], cfg["max"])
    _last_send_ts[provider] = now + delay  # projected send time
    return delay
```

Two ideas worth naming:

1. **Skip when natural pacing already broke the burst.** If the model took longer than the average jitter window to respond, there is no burst to break and an artificial delay only slows the turn. The comment says exactly that.
2. **Record the *projected* send time, not the check time.** `_last_send_ts[provider] = now + delay` means the next call measures request-to-request interval. Recording `now` would make every call see a gap that includes the delay it just applied — and after one jitter, every subsequent call would skip forever.

That second detail is subtle and correct. It is the kind of thing that silently disables a throttle.

Config parsing is defensive throughout: `float()` in `try/except` with a fallback, negatives clamped to 0, and `if max_s < min_s: min_s, max_s = max_s, min_s` so a reversed pair still yields a valid range.

The docstring's rationale is also worth preserving — *"EVERY tool call + its output is sent upstream as a NEW user message — so a single turn can produce a machine-gun sequence of requests"*. That is why the throttle exists at all.

`apply_jitter()` is correctly marked `.. deprecated::` with the reason (it sleeps before the UI can show the delay) and has no current callers — only `.bak` files. Consistent with doc 42 §2.1's `jitter_for` + status + `sleep_jitter` pattern.

***

## 3. `history_budget.py` — the fix that worked, and the shape it can still split

```python
"""
Without a cap, a long agentic chat (hundreds of tool calls) hands the connector
the ENTIRE message table at once — which is what ballooned RAM to 16GB + 20GB
swap and OOM-killed the box.
"""
MAX_DB_HISTORY_MESSAGES = 50
MAX_DB_HISTORY_CHARS = 1_000_000
```

A named, measured incident as a module docstring. The `.bak` history shows the constants were once `4000` / `4_000_000` — so the cap was tightened by ~80× after the OOM.

### 3.1 The pin-then-tail algorithm is right, and can still cut a tool pair

```python
if len(msgs) > max_messages:
    pinned = msgs[0]
    tail = msgs[-(max_messages - 1):]
    msgs = [pinned, *tail]
```

Pinning the first message keeps the task framing. Taking a contiguous tail keeps ordering. Never mutates the input. Both trims are applied in sequence and both pin.

What it does **not** do is keep `assistant(tool_calls) → tool` adjacent. A contiguous tail guarantees adjacency *within* the tail, but the cut point can land between a `tool_calls` message and its `tool` result — the `tool` message survives at the head of the tail while the assistant message that requested it is dropped.

That is exactly the payload an OpenAI-compatible API rejects with 400, and it is the same failure doc 30's ledger #57 describes in `openai_compat._trim_history`. Two independent trimmers in one codebase, both cutting by position with no role awareness.

Finding #152, 🟡. Not currently known to fire — the pin helps, and the caller list is short — but the mechanism is identical to a bug that has already been observed elsewhere.

### 3.2 Callers are real and narrow

`trim_history_messages` is called from `chatgpt/client.py:291`, `mimo/client.py:367`, `local/client.py:72`, `zai/client.py:807`. `trim_openai_history` has **zero** callers — a one-line convenience wrapper nobody used. Finding #153, 🔵.

***

## 4. `context_summarizer.py` — live, and narrower than it claims

The docstring says *"Proactive context compression for non-Qwen models"*. Verified against production: `gemini/client.py:30-31,255` uses `should_inject_hint` / `should_force_summarize` / `get_hint_text`, and `deepseek/client.py:400` imports `strip_summarize_tag`. The `openai`, `groq` and `mistral` client references are all in `.bak` files — those paths dropped the two-tier system.

So it is live for Gemini and partially for DeepSeek (tag-strip only). Not dead — worth stating because the backup files make it look abandoned.

### 4.1 `strip_summarize_tag`'s no-strip comment is a real save

```python
def strip_summarize_tag(text: str) -> str:
    """NOTE: Does NOT .strip() whitespace — this function is called on
    individual streaming chunks where leading/trailing spaces are
    meaningful inter-token spacing."""
    return _SUMMARIZE_TAG_RE.sub("", text)
```

Called on per-chunk text, so a `.strip()` would concatenate words across chunks. The function is one line and the docstring is the reason it must stay one line. Good.

### 4.2 `rewrite_history_with_summary` inserts an ack message

```python
summary_msg = {"role": "user", "parts": [{"text": f"[Earlier context summary]\n{summary_text}"}]}
ack_msg = {"role": "model", "parts": [{"text": "Understood, I have the earlier context."}]}
return prefix + [summary_msg, ack_msg] + remaining
```

Some providers reject a history that ends on a non-user turn or alternates incorrectly; emitting the ack guarantees a user→model pair before `remaining`. The synthetic assistant turn is a small token cost for structural safety, and both formats (Gemini `parts` / OpenAI `content`) get the same treatment.

One gap: `build_summary_prompt` takes `msg_char_fn` as a parameter and **never uses it**. Finding #154, 🔵.

***

## 5. `error_ctx.py` — small, correct, one lazy attribute

```python
def set_account_resolver(self, fn, provider: str | None = None) -> None:
    self._account_fn = fn      # ← not declared in __init__
```

`_account_fn` is created by this method and read through `getattr(self, "_account_fn", None)` — so a client that never calls `set_account_resolver` works. Defensive and correct; the only cost is that the attribute does not appear in `__init__` where a reader looks for it.

The prefix shortening is the useful bit:

```python
m = re.match(r"browser-data-(acc\d+)$", acct)
parts.append(m.group(1) if m else acct)
```

`browser-data-acc3` → `acc3`, so the UI tag stays short while the full name still works if the pattern does not match. `fmt()` and `error()` both prepend the same `prefix` property, so there is one place to change the format.

***

## 6. Findings

**No 🔴, no 🟠.** One 🟡 with a known sibling bug; the rest is unused policy duplication.

### 🟡 Correctness

**#152 — `trim_history_messages` can split an `assistant(tool_calls)` from its `tool` result.** The trim keeps a pinned first message plus a contiguous tail, with no role awareness, so the cut point can leave a `tool` message at the head of the tail whose requesting assistant message was dropped. That is the payload shape OpenAI-compatible APIs reject with 400 — and it is the same mechanism as doc 30's ledger #57 in `openai_compat._trim_history`. Two trimmers, same flaw, independently written.

### 🔵 Dead code / cosmetic

| Where | What |
|:--|:--|
| `browser_errors.no_retry()` / `recoverable()` / `needs_user()` | Zero callers; the sets are used directly (**#151**) |
| `browser_errors.NO_RETRY` / `CAPTURE_ONLY` | Never read; duplicate facts already encoded in `RECOVERABLE` and `scraper_connector`'s `_sent_upstream` (**#151**) |
| `history_budget.trim_openai_history` | One-line wrapper, zero callers (**#153**) |
| `context_summarizer.build_summary_prompt` | Accepts `msg_char_fn`, never uses it (**#154**) |
| `context_summarizer` | Live for Gemini + `strip_summarize_tag` on DeepSeek; the `openai`/`groq`/`mistral` integrations exist only in `.bak` files |
| `error_ctx._account_fn` | Created in `set_account_resolver`, not declared in `__init__` |

***

## 7. What's right, and worth copying

1. **`_PATTERNS` ordering with the reason written in.** `CONTEXT_CLOSED` before `BROWSER_DEAD`, `RATE_LIMITED` before everything — each with the failure it prevents.
2. **Structural type checks behind `except Exception`.** A missing module degrades to pattern matching instead of breaking classification.
3. **`jitter_for` recording the *projected* send time.** Recording `now` would disable the throttle after one use.
4. **Skip-when-natural-pacing-already-broke-the-burst.** A throttle that only fires when there is a burst.
5. **The OOM incident as a module docstring, with the cap tightened ~80×.** The constants have a reason and a history.
6. **`strip_summarize_tag`'s "does NOT .strip()" note.** One line of code that must stay one line, and the docstring is why.
7. **`[provider | account | model]` prefixes from one `prefix` property.** One place to change the format.

***

## 8. What this changes upstream

- **`connectors/common/` is now read in full** — 14 modules, ~5,650 lines: docs 27, 30, 42, 46, 47.
- **#152 pairs with ledger #57** (`openai_compat._trim_history`). Same flaw, two implementations — worth fixing together.
- **#151 is the fourth unused-policy duplicate in this batch** (cf. #132 `SAME_TOOL_FAIL_HALT`, #141 `MAX_TEACHER_INTERVENTIONS`, #134 `_recovery_attempts`): a constant or function that encodes a rule the live code enforces elsewhere.
- Python coverage: **~78,234 → ~78,957 / 137,031 = ~58%**. Application Python: **~65%**. Overall: **~41%**.

***

## Cross-references

- [[42-connectors-common-shared-layer]] — `scraper_connector`, the consumer of `RECOVERABLE` / `NEEDS_USER` / `jitter_for`
- [[30-markdown-compat-humanizer]] — ledger #57, the identical trim flaw in `openai_compat._trim_history`
- [[27-connectors-browser-scraper]] — `SoftRateLimitError`, the typed exception `classify()` matches structurally
- [[45-engine-agents-core-types]] — #144, another silently-lossy index into a rolling buffer
