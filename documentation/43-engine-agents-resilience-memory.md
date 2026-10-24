---
tags: [engine, agents, guardrails, memory]
status: full-read
read: agents/resilience.py (798L), agents/memory_trigger.py (772L)
---

# 43 — Agent Guardrails and Memory Trigger

> Doc 05 read the agent runtime at contract level and explicitly skipped `memory_trigger.py` as "skimmed". These two files are the guardrail layer and the post-run consolidation pass. 1,570 lines.

| File | Lines | Role |
|:--|--:|:--|
| `resilience.py` | 798 | Circuit breaker, loop detector, per-turn caps, main-chat guard |
| `memory_trigger.py` | 772 | Threshold-triggered memory consolidation after an agent finishes |

`resilience.py` has a genuinely good design — four independent detectors with escalating severities — and **three detectors that cannot fire**. `memory_trigger.py` duplicates ~200 lines of doc 39's route logic and writes the same files with the same absence of locks.

***

## 1. `resilience.py` — four detectors, three broken

### 1.1 CircuitBreaker is correct

```python
def can_execute(self) -> bool:
    if self.state == "closed":
        return True
    if self.state == "open":
        if time.time() - self.last_failure > self.reset_timeout:
            # Transition to half-open: THIS call is the single probe
            self.state = "half-open"
            return True
        return False
    # half-open: probe already in flight, block additional calls
    return False
```

The half-open state admits exactly one probe and blocks the rest — the comment says so, and the logic does it. `record_failure` returns half-open to open, `record_success` closes. No findings.

### 1.2 Detector 2 is declared, warned, and never halts

```python
SAME_TOOL_FAIL_WARN = 3   # Warn at 3 failures on same tool (any error)
SAME_TOOL_FAIL_HALT = 8   # Halt at 8
```

```
$ grep -n SAME_TOOL_FAIL_HALT engine/agents/resilience.py
119:    SAME_TOOL_FAIL_HALT = 8   # Halt at 8
```

One hit — the declaration. The docstring for `LoopDetector` promises *"Same-tool failure: same tool, any failure → warn@3, halt@8"*. `get_warning` only ever compares against `SAME_TOOL_FAIL_WARN`:

```python
for tool, count in self._tool_fail_counts.items():
    if count >= self.SAME_TOOL_FAIL_WARN and tool not in self._tool_fail_warned:
        …
        return self.SAME_TOOL_FAIL_WARNING.format(tool=tool, count=count)
```

There is no halt branch. A model can fail the same tool eight, twenty, fifty times and receive exactly one warning after the third. Finding #132.

### 1.3 The no-progress reset never resets

```python
np_key = self._hash_str(f"{sig}:{result_hash}")
self._no_progress_counts[np_key] += 1
…
else:
    # Different result — reset counter for this sig
    for k in list(self._no_progress_counts):
        if k.startswith(self._hash_str(sig)):
            self._no_progress_counts[k] = 0
```

Keys are `_hash_str("tool:args:result_hash")` — a 16-char MD5 prefix of the **whole** triple. The reset test is `k.startswith(_hash_str("tool:args"))` — a 16-char prefix of a **different** string.

A 16-char hash starting with another 16-char hash means the first 16 characters collide completely — i.e. the two hashes are equal, which is only true if `"tool:args:result_hash" == "tool:args"`. The reset loop therefore runs over the dict and zeroes nothing, ever.

Consequence: once a read-only tool produces the same result twice, the counter only ever climbs. A `view_file` on an unchanged file during a long session will eventually hit `NO_PROGRESS_BLOCK = 5` and start blocking a legitimate call — because a result that *did* change in between still left the old key's counter intact. Finding #133, and this one is user-visible.

### 1.4 `_recovery_attempts` makes the hard-stop single-use

```python
if attempts == 0:
    self._recovery_attempts[recovery_key] = 1
    …
    # Undo the hard-stop so the tool can execute once more after recovery
    self._exact_fail_stopped.discard(recovery_key)
    return GuardrailDecision(action="recover", …)
# Already recovered once → true hard block
return GuardrailDecision(action="block", …)
```

The design is sound: first hard-stop gives one recovery, second blocks. But `_recovery_attempts` is keyed on the **exact failure signature**, and `record_success` never clears it. So the sequence

```
fail×5 → recover → succeed → (10 turns later) fail×5 on the same tool+args+error → block immediately
```

takes the recovery away for the rest of the agent's life. There is no reset in `reset()` either — only `MainChatGuard.reset()` clears its flags, and `LoopDetector` has no equivalent. Finding #134, 🔵 — it only bites a long-lived agent that regresses.

### 1.5 Result stubbing is correct and useful

```python
if len(result) > 512:
    prev = self._last_results.get(sig)
    if prev == result:
        self._stub_count[sig] += 1
        if self._stub_count[sig] >= 2:  # From 2nd consecutive identical
            stub = f"[STUB] Same result as previous call ({len(result)} chars). Args: {args_preview}"
```

Byte-identical results over 512 chars are replaced with a stub from the second repeat. That is a real context saving and it is the only detector whose thresholds are actually enforced.

### 1.6 `MainChatGuard` is the correctly-wired one

`check_loop`, `check_failures`, `check_malformed_action` and `check_incomplete_action` all fire once per guard lifetime and all reset together via `reset()`. `_format_tags()` reads the **same** `get_tool_format_tags` the system prompt uses, so the malformed warning names whatever wrapper the model was actually told to use — including a settings override. That is the right way to write a format warning and it is documented as such.

***

## 2. `memory_trigger.py` — a second copy of the consolidation writer

`_apply_memory_changes` (~140 lines) and `_try_consolidation_call` (~60 lines) are near-verbatim duplicates of `routes/memory.py`'s `_consolidation_apply` and `_try_consolidation_call` (doc 39). They differ in three ways:

| | `routes/memory.py` | `agents/memory_trigger.py` |
|:--|:--|:--|
| `_normalize_entry` | inline, four branches | a shared helper — **better** |
| Merge resolution | `_resolve_candidates_against_memory` (imported) | same function (local) |
| Locks | none | none |

So the same seven shared JSON stores get a **second** unguarded writer. Doc 39's #120 counted 14 bare `write_text` calls in the route; this file adds five more (`_PROCEDURAL_PATH` ×2, `_MEMORY_PATH`, `_PROTECTED_PATH`, and the consolidation log). Finding #135.

The improvement worth noting: `_normalize_entry` **derives** `trigger` and `keywords` when the model omits them:

```python
if not trig:
    trig = out["key"].replace("_", " ").strip()
if not kws_clean:
    kws_clean = [w for w in out["key"].split("_") if len(w) > 2][:5]
```

The comment says why — *"so the procedural entry is at least retrievable rather than dead weight"*. `routes/memory.py` does not do this; a procedural entry without `trigger`/`keywords` is stored and never matches. That is the one place this file is better than its twin.

### 2.1 `_load_current_memory` reads the searcher's private state

```python
searcher._ensure_loaded()
if searcher._normed_vectors is not None and len(searcher._normed_vectors) > 0:
    query_vec = searcher._embed_texts([query], is_query=True)
    sims = (query_vec @ searcher._normed_vectors.T)[0]
    …
    meta = searcher._entry_meta[idx]
```

Four private attributes from another package, called synchronously. That inherits doc 41's #126 — if the configured embedding model is Gemini, `_embed_texts` does a blocking `httpx.post` on the event loop, here inside an async function that could `await asyncio.to_thread`. Finding #136.

### 2.2 The consolidation log grows without bound

```python
def _log_consolidation(section: str, content: str) -> None:
    …
    with open(_CONSOLIDATION_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(entry)
```

`system/consolidation.txt`, appended on every consolidation with the **full prompt, the full raw LLM output, and the full resolved result**. No rotation, no size cap, no redaction — and the prompt contains memory values, which per doc 2 include credentials. Called 6+ times per consolidation. Same class as ledger #15 and #58. Finding #137.

***

## 3. Findings

**No 🔴, no 🟠.** #132 and #133 are the two that change behaviour; the rest are duplication and unbounded logs.

### 🟡 Correctness

**#132 — `SAME_TOOL_FAIL_HALT` is declared and never used.** `LoopDetector`'s docstring promises "warn@3, halt@8" for same-tool failures; `get_warning` only warns. A model can fail one tool indefinitely and see exactly one warning.

**#133 — the no-progress reset never fires.** Keys are `_hash_str("tool:args:result_hash")`, the reset compares against `_hash_str("tool:args")`; a 16-char hash cannot be a prefix of a different 16-char hash. Once a read-only tool repeats a result, its counter only climbs — so a later legitimate `view_file` on a changed file can still be blocked at `NO_PROGRESS_BLOCK = 5`.

**#135 — a second unguarded writer for the same seven memory stores.** `_apply_memory_changes` duplicates `routes/memory._consolidation_apply` and adds five more bare `write_text` calls. Doc 39's #120 counted 14 in the route; this is a separate writer to the same files.

**#136 — `_load_current_memory` calls `searcher._embed_texts` synchronously.** Inherits doc 41's #126: a Gemini embedding model blocks the event loop for up to 2×60 s inside an async function.

**#137 — `system/consolidation.txt` is unbounded and unredacted.** Full prompt, full raw LLM output, and resolved result appended per consolidation, no rotation, contains memory values (which include credentials per ledger #14).

### 🔵 Dead code / cosmetic

| Where | What |
|:--|:--|
| `resilience._recovery_attempts` | Never cleared by `record_success` or any `reset()` — a recovered-then-regressed signature is blocked immediately for the agent's lifetime (**#134**) |
| `resilience.LoopDetector` | Has no `reset()`; `MainChatGuard` does. An agent reused across tasks carries counters forward |
| `resilience.TurnCapTracker.MAX_WEB_SEARCHES` | 50 — the docstring cites "Claude Code v2.1.212" as the source |
| `memory_trigger._normalize_entry` | **Better than its twin** in `routes/memory.py` — derives `trigger`/`keywords` when the model omits them |
| `memory_trigger` | `_load_consolidation_settings` is a third copy of the same 10-line function |

***

## 4. What's right, and worth copying

1. **`CircuitBreaker`'s half-open admitting exactly one probe.** Stated in a comment, implemented correctly.
2. **`MainChatGuard._format_tags()` reading the same resolver as the system prompt.** A format warning that names the wrapper the model was actually told to use — including settings overrides.
3. **Result stubbing over 512 chars from the second identical call.** The one detector whose thresholds are all enforced, and it saves real context.
4. **`_ThoughtOnlyRetry`-style separation** — in this file, the `NO_PROGRESS` vs `EXACT_FAIL` distinction routes to different severities instead of one generic "loop detected".
5. **`_normalize_entry` deriving `trigger`/`keywords` from the key.** A procedural entry without them is unretrievable; this guarantees a floor.
6. **`_log_consolidation` naming the section and timestamping every block.** Tailable, greppable, and the reason the merge-resolution path is debuggable at all.

***

## 5. What this changes upstream

- Doc 05 listed `memory_trigger.py` as "skimmed". It is now read in full, and **#135 supersedes** its framing of consolidation as a route-only concern.
- Doc 39's #120 counted the route's 14 writes; **#135** adds this file's five. The total is 19 across two modules writing the same seven stores.
- **#126 (doc 41) now has a second call site** — `_load_current_memory`, in an async context that could `to_thread`.
- Python coverage: **~74,180 → ~75,750 / 137,031 = ~55%**. Application Python: **~61%**. Overall: **~39%**.

***

## Cross-references

- [[05-engine-agents]] — the runtime these guard; `memory_trigger.py` was skimmed there
- [[39-server-memory-route]] — `_consolidation_apply` and `_try_consolidation_call`, the originals #135 duplicates
- [[41-memory-search-support]] — #126, the blocking embedder #136 inherits
- [[37-engine-research-engine]] — a circuit breaker that *does* count and open correctly
