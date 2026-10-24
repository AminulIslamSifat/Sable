# 30 — `markdown.js`, `openai_compat.py`, `humanize.py`

> [!INFO] Why this file exists
> Priorities 1–3 on the README's coverage list, and the first time this series has read frontend rendering, a shared provider base, and a skills script in one pass. All three sit on a hot path — every streamed character crosses `markdown.js`, every OpenAI-compatible turn crosses `openai_compat.py`, and every humanizer run crosses `humanize.py`.
>
> Read end to end: `web/js/markdown.js` (809), `connectors/common/openai_compat.py` (989), `skills/text_humanizer/scripts/humanize.py` (1,052).

***

## 1. `web/js/markdown.js` — two renderers in one file

**809 lines, two complete markdown parsers.**

| Path | When | Lines |
|:--|:--|--:|
| `marked` + DOMPurify | `window.marked && window.DOMPurify && !usesLegacyExtras()` | ~80 |
| Hand-written `parseBlocks` | fallback, and **always** for callouts / `==highlight==` | ~350 |

That split is deliberate and documented: `usesLegacyExtras` returns true when the text contains a callout (`> [!note]`) or `==…==`, because marked has no support for either. So the legacy parser is not dead — it is the only path that renders Obsidian callouts, and any message containing one is parsed entirely by hand.

### 1.1 The security claim is accurate

```js
/* All raw text is HTML-escaped before any tag is emitted, so this is safe
   against injected markup by construction — no separate sanitizer needed. */
```

For the legacy path this is true: `inlineMd` calls `escHtml(raw)` first, and the two regexes that emit tags (images, links) both refuse anything not matching `^https?:|^data:image/` / `^https?:|^mailto:|^#`. There is no `innerHTML` on unescaped input anywhere in `parseBlocks`.

The `svg` fence branch is the one exception and it is handled explicitly — DOMPurify with `USE_PROFILES: {svg: true, svgFilters: true}`, or the raw string when DOMPurify is absent:

```js
const clean = window.DOMPurify ? DOMPurify.sanitize(rawSvg, {...}) : rawSvg;
```

> [!WARNING] The un-sanitized fallback is real
> When DOMPurify is missing, an ` ```svg ` fence is injected raw. The marked path is gated on `window.DOMPurify` existing, but the **legacy** path is not — `parseBlocks` runs whenever the text has a callout, and its svg branch takes the `: rawSvg` arm. In practice DOMPurify ships with the app, so this only bites if the script fails to load, in which case the whole renderer degrades. Recorded because the comment above it claims safety "by construction" and this branch is the one place that is not.

### 1.2 Emoji → Lucide replacement corrupts code blocks and attributes

```js
function lucideReplaceEmoji(html) {
  if (document.documentElement.getAttribute("data-icon-style") !== "lucide") return html;
  return html.replace(_EMOJI_RE, (match) => { /* returns <svg>…</svg> */ });
}
```

`lucideReplaceEmoji` is applied to the **finished HTML string**, not to text nodes:

```js
return _unstashMath(lucideReplaceEmoji(DOMPurify.sanitize(html, ...)));
return _unstashMath(lucideReplaceEmoji(parseBlocks(...)));
```

So when the user has `data-icon-style="lucide"` set, every matching emoji anywhere in the HTML is swapped for inline SVG — including inside `<pre><code>` blocks and inside attribute values.

A fenced Python block containing `print("✅")` renders an SVG glyph inside the code sample. A markdown image with `alt="🚀 launch"` gets SVG markup spliced into its `alt` attribute, which DOMPurify has already finished sanitising.

> [!CAUTION] Ledger #56
> The fix is to walk text nodes, not the string. `TreeWalker` over `document.createTreeWalker(root, NodeFilter.SHOW_TEXT)`, skipping nodes whose ancestor is `pre`/`code`, and never touching `Element.attributes`. The string-replace approach cannot distinguish markup from content.

### 1.3 Math stashing

TeX is pulled out before rendering and swapped back after, because marked would otherwise read `_` and `*` inside a formula as emphasis (`$a_b$` → `a<em>b</em>`). Four delimiter forms are handled: `$$…$$`, `\[…\]`, `$…$`, `\(…\)`. Fenced code is stashed first so TeX inside a code block survives untouched. All correct, and the comment explains the failure it prevents.

The inline rule is `(?<!\$)\$(?!\$)([^\n$]+?)\$(?!\$)` — a single `$`, no `$` inside, not part of a `$$` pair. That also matches currency ranges:

> *"the plan costs $5 and the add-on costs $10"* → `$5 and the add-on costs $` is treated as inline math.

The result renders as `\(5 and the add-on costs \)` fed to MathJax, which renders it as a formula or throws. Narrow (needs two `$` on one line with no other `$`) but it is user-visible text corruption on any pricing discussion.

### 1.4 `countOpenFences` disagrees with the other two fence scanners

Three functions parse fence state, and two of them differ on how a closing fence is recognised:

| Function | Closing test |
|:--|:--|
| `normalizeMd` | `lines[i].trim() === fenceChar` |
| `escapeNonHtmlTags` | `lines[i].trim() === fenceChar` |
| `countOpenFences` | `lines[i].trim().startsWith(fenceChar)` |

For a line ```` ```js ```` inside an already-open ```` ``` ```` block, `countOpenFences` closes the fence and the other two do not. `closeUnclosedFences` uses `countOpenFences`, so the two can disagree about whether a fence is open — which means the auto-close can append a fence that the parser then treats as opening a new block.

Low frequency (requires a fence-marker line with content inside a fenced block) but it is the one place the file keeps two sources of truth for the same state.

***

## 2. `connectors/common/openai_compat.py`

One class serving OpenAI, Groq, Mistral, and every custom endpoint. Provider differences are class attributes: `BASE_URL`, `KEYS_PATH`, `INSTRUCTION_MODE`, `ENABLE_REASONING`, `SUPPORTS_MULTIMODAL_CONTENT`, `MAX_TOOL_ROUNDS`.

The design is sound — key rotation, session history with a character budget, self-summarisation, native tool-call streaming, and a sanitisation pass for providers that cannot accept array content. Three findings are load-bearing.

### 2.1 Every turn writes full conversation history to disk — for every provider

```python
# DEBUG: dump full payload to file for inspection (append mode)
_dbg_path = str(_LD2 / "cloudflare_payload.jsonl")
with open(_dbg_path, "a") as _f:
    _f.write(_dbg_json.dumps({"ts": ..., "payload": payload}, default=str) + "\n")
```

This sits **outside** the `if not self.SUPPORTS_MULTIMODAL_CONTENT:` guard, on the unconditional path. Every OpenAI-compatible turn — OpenAI, Groq, Mistral, any custom endpoint — appends the complete `payload` to `logs/cloudflare_payload.jsonl`. Then a second write inside the attempt loop overwrites `logs/cloudflare_sent_payload.json` with the same content on every retry.

No rotation, no cap, no redaction, no opt-out. The payload is the full message history plus the injected system prompt (persona, tool schemas, output-directory rules).

> [!IMPORTANT] Same class of defect as ledger #15
> `connectors/deepseek/client.py`'s `_log_raw` writes every prompt and response to `logs/deepseek_raw.txt` — already recorded as 🟠. This is the same defect in the *shared* connector, so it covers more providers. The filenames say `cloudflare` because that is the provider it was written to debug; it never got scoped back.

### 2.2 `_trim_history` can orphan a tool result

```python
def _trim_history(history, prefix_len, max_chars=None):
    prefix = history[:prefix_len]
    msgs = history[prefix_len:]
    total = sum(_msg_chars(m) for m in msgs)
    while total > limit and len(msgs) > 1:
        total -= _msg_chars(msgs.pop(0))
    return prefix + msgs
```

It pops from the front until under the budget, with no awareness of message roles. In a tool-calling conversation the history is:

```
assistant {tool_calls: [...]}     <- carries arguments
user      {role: "tool", ...}     <- result
assistant {content: "..."}
```

If the pop lands on the `assistant` message with `tool_calls` and stops there, the `tool` message that follows it is left with **no preceding tool_call** — and both OpenAI and Groq reject that payload with a 400. The turn then rotates the key (§2.4) and retries the same malformed history with a different key, so the error looks like an auth failure and burns the whole pool.

`_msg_chars` also returns `0` for a message whose `content` is `None` (which is exactly what an `assistant` tool-call message carries), so the trimming loop can pop several zero-cost messages without the total dropping — it exits on `len(msgs) > 1` instead, which is how it can land mid-pair.

> [!CAUTION] Ledger #57
> Trimming must snap to a role boundary: after popping, if `msgs[0]` has `role == "tool"`, keep popping until it does not. Or drop whole assistant+tool groups as a unit.

### 2.3 A hard failure retries `MAX_TOOL_ROUNDS × keys` times

```python
_tool_round = 0
while _tool_round < self.MAX_TOOL_ROUNDS:
    _tool_round += 1
    attempts = len(self._keys)
    for attempt in range(attempts):
        ...
        if response.status_code in (401, 403, 429):
            self._rotate_key()
            continue          # next key
        ...
    # for-loop exhausted -> falls through to end of while body -> loops again
```

When every key answers 429, the `for` loop `continue`s through all of them, then execution falls off the end of the `while` body and the `while` condition is re-evaluated. `_tool_round` is still under `MAX_TOOL_ROUNDS`, so the **same exhausted pool is retried from the top** — 20 rounds × N keys before the loop exits and the final `all API keys exhausted` error fires.

On a rate-limited pool that is 20× the request volume for a single user turn, against an endpoint that is already refusing.

### 2.4 Key rotation on *any* exception

```python
except Exception as exc:
    self.logger.warning("%s stream error (attempt %d): %s", self.NAME, attempt + 1, exc)
    self._rotate_key()
    continue
```

A bug in our own JSON handling, a malformed chunk, a `KeyError` on an unexpected delta shape — all rotate the key. Combined with §2.3, a deterministic client-side bug walks the entire key pool and then retries the walk 20 times.

### 2.5 Smaller items

- `self.logger.warning("SANITIZE: running for %s, %d messages", ...)` logs at **warning** for a normal, expected path on Cloudflare. It fires on every turn for that provider.
- `_save_keys` uses a bare `write_text` — violates convention #2 (atomic writes). A crash mid-write loses the whole key pool.
- Tool-progress preview calls `json.loads(buf["args_str"])` on every 32-byte growth. For a 4 KB argument string that is ~128 parse attempts, most of them failing. Cheap per attempt, O(n²) overall.
- `_minimal_instructions` builds the tag strings by concatenation so the module never contains a literal closing `</tool_call>` — correct, and the reason is in the comment (it would break agent transport).

***

## 3. `skills/text_humanizer/scripts/humanize.py`

1,052 lines of rule-based post-processing: discourse swaps, perplexity swaps, n-gram collapse, burstiness engineering, AI-tell removal, and two "chaos" passes that inject typos and grammatical slips.

The engineering discipline here is unusually good for a heuristic script. `enforce_burstiness` keeps a best-of guard that discards any mutation which lowers the score, so the pass **cannot** make text measurably worse than it started. `_break_runons` runs independently of the burstiness score because — as the comment says — a 1-word fragment next to a 60-word monster yields a high coefficient of variation while reading as machine-broken. `_find_split_point` refuses to split on a conjunction unless the tail starts a fresh subject, so "tools are sturdy and expandable" is not split into "tools are sturdy. and expandable…".

Two defects, both in the chaos pass.

### 3.1 The `cap_mid` rule is inert

```python
_CAP_MIDSENTENCE = re.compile(
    r"([a-z,;]\s+)(Which|That|This|It|Thus|But|And|Where|When|However)\b"
)
...
elif kind == "cap_mid":
    m = _CAP_MIDSENTENCE.search(text)
    if m:
        text = text[:m.start(2)] + m.group(2) + text[m.end(2):]
```

Group 2 is already capitalised by the pattern, and the "replacement" writes group 2 back into its own span. **`text` is byte-identical after this branch.**

It cannot be a no-op by design either: the outer loop does `if text != before: break`, so the iteration is wasted and the next kind is tried. The docstring advertises this as a real mutation ("capitalise a discourse word after a comma"), and it never mutates anything.

> [!NOTE] Ledger #58
> The likely intent was to *lowercase* a mid-sentence capital — which is what the separate `lower` branch does. See §3.2.

### 3.2 The `lower` rule is unreachable

```python
strong = ["sv", "article", "wrong_article", "plural", "possessive"]
weak   = ["cap_mid", "slop", "splice"]
```

`lower` appears in **neither list**, so `elif kind == "lower":` can never execute. The docstring lists "lowercase sentence start — 'And that matters'" as one of the six kinds the pass produces; it produces five.

Together with §3.1 that means two of the six documented chaos kinds never fire, and one of them costs an iteration every time the budget loop runs.

### 3.3 `_TYPO_APOSTROPHE` contains meaning-changing entries

```python
"it's": "its",
"we're": "were",
```

Dropping an apostrophe is a plausible human slip for `don't → dont`, where the result is not a word. For `it's → its` and `we're → were` the result **is** a different valid word, so the typo silently changes the sentence's meaning:

> *"it's a problem"* → *"its a problem"* (ungrammatical, reads as a typo)
> *"we're going"* → *"were going"* (reads as past tense, changes meaning)

`we're → were` is the worse of the two: the output is grammatical and the sentence now says something else. In a humanizer whose entire contract is "same meaning, different surface", that is a contract violation, not a typo.

### 3.4 The bare `the fact that` rule can produce a fragment

```python
(r"\bthe fact that\s+(?![^.?!]*\b(?:is|was|were|are)\b)", ""),
```

The lookahead protects "The fact that X **is** Y" — the subject keeps its verb. But it fires on object position:

> *"I like the fact that tools scale"* → *"I like tools scale"*

No be-verb follows, so the phrase is deleted and the clause loses its complementiser. The comment two lines above shows the author was aware of exactly this class of breakage ("or stripping it leaves 'X is Y' with a dangling verb") and guarded one case but not the other.

***

## 4. Findings

| # | Finding | Severity | Status |
|--:|:--|:--|:--|
| 56 | `markdown.js` `lucideReplaceEmoji` replaces emoji in the finished HTML string — inside `<pre><code>` and inside attribute values. | 🟠 medium | **not fixed** |
| 57 | `openai_compat._trim_history` pops by character budget with no role awareness — can leave a `tool` message without its preceding `tool_calls`, which the API rejects with 400. | 🔴 high | **not fixed** |
| 58 | `openai_compat` writes the **full conversation history** to `logs/cloudflare_payload.jsonl` on every turn for **every** provider, unconditionally, unbounded and unredacted. | 🔴 high | **not fixed** |
| 59 | `openai_compat` retries an exhausted key pool `MAX_TOOL_ROUNDS` (20) times — 20×N requests for one turn against an already-429 endpoint. | 🟠 medium | **not fixed** |
| 60 | `openai_compat` rotates keys on **any** exception, including deterministic client-side bugs. Combined with #59, walks the pool 20 times. | 🟠 medium | **not fixed** |
| 61 | `humanize.py` `cap_mid` rule writes the match back into its own span — provably inert. | 🟡 low | **not fixed** |
| 62 | `humanize.py` `lower` rule is in neither the `strong` nor `weak` list — unreachable dead branch. | 🟡 low | **not fixed** |
| 63 | `humanize.py` `_TYPO_APOSTROPHE` maps `we're → were` and `it's → its` — changes meaning, violates the "same meaning" contract. | 🟠 medium | **not fixed** |
| 64 | `humanize.py` bare `the fact that` collapse produces fragments in object position ("I like tools scale"). | 🟡 low | **not fixed** |
| 65 | `markdown.js` inline-math regex matches currency ranges (`$5 … $10`) as TeX. | 🟡 low | **not fixed** |
| 66 | `markdown.js` three fence scanners disagree on the closing test (`startsWith` vs `===`). | 🟡 low | **not fixed** |
| 67 | `markdown.js` legacy `svg` fence injects raw markup when DOMPurify is absent, while the comment claims safety "by construction". | 🟡 low | **not fixed** |
| 68 | `openai_compat._save_keys` uses a bare `write_text`; `SANITIZE` logs at warning on a normal path; tool-args JSON re-parsed every 32 bytes. | 🔵 cosmetic | **not fixed** |

Two 🔴 in this batch, both in `openai_compat.py`, and both are the kind that only show up under load or over time — one corrupts a retry, the other grows a log file forever.

***

## 5. What this changes upstream

- `documentation/README.md`: coverage tables, ledger #56–#68, priority list.
- [[25-web-frontend-sse-and-components]]: cross-reference added — it documents `sse.js`, not the renderer `sse.js` calls into.
- [[12-connectors]]: cross-reference added — it documents the provider contract, not the shared base implementation.

**Coverage after this batch:** Python 56,900 → 58,900 / 122,907 = **47.9%**. `web/js` 6,315 → 7,124 / 52,114 = **13.7%**. Overall ~63,200 → ~65,300 / 200,884 = **32.5%**.

**Next on the list:** `engine/library_search.py` (FTS5), `tools/computer_use.py`, and the 18 remaining provider directories.
