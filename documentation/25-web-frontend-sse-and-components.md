# 25 — Frontend: `web/js/sse.js` (3,753) + `web/js/render-components.js` (2,562)

> [!IMPORTANT]
> The two largest frontend modules. `sse.js` is the stream consumer — it owns the turn model, the typewriters, history replay, approval batches, critique sessions, and the read watchdog. `render-components.js` is the Rich UI renderer — a small reactive framework with its own expression evaluator, SVG sanitizer, and chart engine.

**Read depth:** both files full, 6,315 lines.

***

# Part 1 — `sse.js`

## 1. The turn model

Everything renders into a `turn` div. `addBotStreaming()` creates one per response and returns a closure API (`appendAnswer`, `addSkillStart`, `finalize`, …).

```js
let turn = document.createElement("div");   // `let`, not `const`
```

```
// `let`, not `const`: a queued user message delivered mid-stream seals this
// turn and rolls a fresh one out below the user bubble (see
// injectUserMessage). Every closure here shares the binding, so reassigning
// it moves subsequent renders to the new turn automatically.
```

Inside a turn: one `.msg.bot` bubble holding **ordered `.md-content` segments** — text, then an inline UI block, then more text. `_openAnswerSegment()` starts a new segment; `ensureAnswer()` creates the bubble lazily.

### 1.1 `injectUserMessage` — the sealed-turn dance

When the server delivers a message queued mid-stream:

1. `closeCurrentThinking()` and **drop** the closed reasoning block (so the next delta can't re-open it above the user bubble)
2. `closeAnswer()`
3. `_settleTurn(turn)` — freeze `_sableStats.tEnd`, strip `.streaming`, mark pending skill cards `interrupted`
4. push onto `_sealedTurns`, set `_injected = true`, `_skillRoundFloor = skillRounds.length`
5. flip the OLD turn's backend-status card to `done` (it belongs to the sealed turn — the next `appendAnswer` would look in the NEW turn and find nothing)
6. **reassign `turn`** to a fresh div and reset `_stats`

> [!TIP]
> `_injected` exists so `finalize()` can tell *"the model genuinely returned nothing"* from *"the answer lives in a sealed turn now"*:
> ```js
> const _sealedHasContent = _injected && _sealedTurns.some(
>   (t) => t.querySelector(".msg.bot") || t.querySelector(".skill-card")
> );
> ```
> Without it every mid-stream injection fired a false *"Empty response from upstream"*.

***

## 2. Two typewriters, one tuning source

Both the answer and the thinking reveal use `requestAnimationFrame` with **rate-based** consumption and a **fractional carry**:

```js
_ansFrac += cps * dt / 1000;
const nChars = Math.floor(_ansFrac);
_ansFrac -= nChars;
```

```
// Carry the fractional remainder between frames. Rounding 2.49 down to
// 2 every frame silently loses 20% of the target rate, making the
// animation lag the model and dump the tail. Accumulate instead.
```

Backlog catch-up: `cps = Math.max(cps, _ansQueue.length / 0.2)` — a growing queue drains at an elevated but steady rate instead of bursting.

### 2.1 Answer fast paths

| Condition | Path |
|:--|:--|
| not in fence, no structural char, trailing `<p>` text node | append to `lastChild.textContent` |
| inside a code fence | append to last `pre code`; detect fence close → full re-render |
| otherwise | `innerHTML = renderMarkdown(raw)` |

```js
const _ANS_STRUCTURAL_RE = /[\n`|<>*_~=\[]/;
```

```
// Deliberately excludes `-` and `#`: both are only structural at line start,
// which is always preceded by \n (already in the set). Keeping them meant every
// hyphenated word — "well-known", "1-5", "e-mail" — triggered a full DOM
// rebuild, which was the main source of streaming jank.
```

### 2.2 The removed final tick

```
// NOTE: an "final tick" block used to live here that re-ran
// `innerHTML = renderMarkdown(raw)` whenever (!_ansTimer && !fast).
// It was dead weight: the `if (!fast)` block above already rendered the
// exact same `raw` in this same tick, so the second pass re-parsed
// identical input and threw away the lucide icons created in between.
```

### 2.3 Thinking stays animated even for fast models

```js
if (tune.flush) tune = { chars: TW_MAX_CHARS, ms: TW_MIN_MS, flush: false };
```

```
// Thinking is a cheap textContent append (no markdown re-parse), so keep
// the reveal animation alive even for fast models — just crank the rate
// instead of dumping the whole block at once. The old flush here is what
// made fast thinking pop in fully-formed ("frog jump") instead of typing.
```

***

## 3. Thinking-block reattachment (DeepSeek interleaving)

DeepSeek interleaves `THINK` and `RESPONSE` fragments, so an early `answer` delta calls `closeThinking()` while reasoning is still arriving. Without protection the next `thinking` delta built a **second** card.

```js
if (!_thinkRoundDone && lastThinkWrap && lastThinkWrap.isConnected) {
    currentThinkWrap = lastThinkWrap;   // reattach, don't fork
    ...
}
```

`_thinkRoundDone` is set by `markThinkRoundDone()`, called from `addSkillStart()` and `nextSkillRound()` — i.e. **only** a real tool card opens a genuinely new round. That's what preserves the `t1, c1, t2, c2` layout.

Ordering is also handled: a new thinking block is `turn.insertBefore(currentThinkWrap, answerEl)` when the answer bubble already exists, never `appendChild` — otherwise the reasoning card lands *below* the answer it explains.

***

## 4. Speed badge — token-weighted, not averaged

```
// Token-weighted mean of per-segment speeds. A flat average let a
// 30-char segment at 20 tok/s count as much as a 500-char segment at
// 140 tok/s, dragging the result down — and tool-heavy turns produce
// more short segments, so the bias hit exactly that case.
```

Bursts are opened on first answer text and closed on `tool_pending` / `skill_start` / stream end:

```
// Closing here keeps the tool-call XML generation time out of the measurement
// window; the XML never reaches us as text, so it would otherwise be counted
// as time spent writing characters we never saw.
```

Label is honest about measurability: `"140.2 tok/s · 12.4s"` when bursts exist, bare `"12.4s"` when the whole reply arrived in one chunk. Posts to `/api/stats/response` (server rejects `tps <= 0`, `tokens < 8`, `gen_ms < 100`).

***

## 5. `consumeChatStream` — the read watchdog

The most important defensive code in the frontend.

```
// reader.read() has NO timeout. If the server wedges (hung upstream, dead
// browser, blocked generator) the body never closes, this loop never
// exits, sendMessage's finally never runs, endStream() is never called,
// and isStreaming() stays true forever — the send button silently turns
// into a stop button until the user presses it manually.
```

| Budget | When |
|--:|:--|
| **75 s** | normal read |
| **900 s** (15 min) | a tool is in flight |

Tool-in-flight tracking is explicit, because a tool legitimately produces **no bytes for minutes**:

```
//   tool_pending ──> skill_start ──> skill_output… ──> skill_end
//        └──────────── parse_error (tool never ran)
let _toolDepth = 0;    // between skill_start and skill_end
let _toolPending = 0;  // between tool_pending and its skill_start/parse_error
const _toolsInFlight = () => _toolDepth + _toolPending > 0;
```

On stall: set `gotError`, render `replaceWithError(...)`, `reader.cancel()`, `break` — deliberately **not** `throw`, so the caller's empty-response handling still runs.

> [!WARNING]
> The stall message hardcodes `"no data for 75s"` even when the budget in force was 900 s (tool in flight). Cosmetic, but misleading in exactly the case you'd be debugging. **Not fixed.**

***

## 6. Transient cards survive a turn rebuild

```
// The status card is a TRANSIENT element: it is created by addBotStreaming
// and is NOT a skill_event, so it is absent from the DB. A mid-stream
// re-render (host.innerHTML = '') destroys the .turn that hosts it, and
// every later status update was then dropped on the floor — the card sat at
// "working…" with no steps while the real pipeline message went nowhere.
```

So `status`, `account_switch`, `token_rotation` and `done` all resolve their container as:

```js
const _bsTurn = streamPane?.querySelector('.turn:last-child') || streamPane;
```

— falling back to the **pane**, so the card is found (or recreated) even if the `.turn` wrapper was replaced.

Three transient card types, each idempotent (find-or-create by class, append one row per event):

| Card | Events | Rows |
|:--|:--|:--|
| `.backend-status-card` | `status` | retries, delays, file attach, transport |
| `.account-switch-card` | `account_switch` | 10-step pipeline, `_ascCompleted` survives retries |
| `.token-rotation-card` | `token_rotation` | one row per rotation with reason + masked tokens |

### 6.1 Retry-cycle reset

On `step === "retrying"` the account-switch card **clears** the intermediate steps from `_ascCompleted` and removes their rows' `done`/`active` classes:

```js
const resetKeys = ["searching", "switching", "syncing", "summarizing", "creating_session", "warming_up"];
```

Without it a second switch attempt rendered as already-complete.

### 6.2 Auto-dismiss

`complete` schedules removal after 1.6 s, and **any** new activity cancels that timer:

```js
if (step !== "complete" && card._ascDismissTimer) { clearTimeout(...); card.classList.remove("asc-exit"); }
```

***

## 7. History replay — `_renderSkillEvents`

Replays an ordered `skill_events` array into the same DOM shape the live stream produces. Keyed lookups: `cards[id]` for skill cards, `_uiSegs[id]` + `_uiSigs[id]` for streaming UI blocks, `_htmlHist[id]` for HTML panes.

### 7.1 UI-block idempotency

```js
let _sig = null;
try { _sig = JSON.stringify(evt.payload); } catch (e) { _sig = null; }
if (_sig === null || _uiId === null || _uiSigs[_uiId] !== _sig) { ... render ... }
```

```
// Identical payload as last tick → nothing to redraw. Also covers
// the seal (partial:false) that repeats a partial verbatim.
```

Render is atomic (`SableComponents.renderInto`) so a bad payload can never blank a block that already drew.

### 7.2 History caps

```js
const HISTORY_SKILL_OUTPUT_CAP = 20000;
const HISTORY_MAX_OUTPUT_EVENTS_PER_CARD = 50;
```

```
// Backend caps live streaming at 20K chars, but DB may hold unlimited
// output from before the cap existed. Prevents DOM explosion on load.
```

Both caps are **history-only** — live streaming uses the backend's own caps.

### 7.3 History-load batching

`window._historyLoading` short-circuits per-element icon scans (`_actIcons = () => {}`) and defers Mermaid/MathJax; `loadMessages` runs one pane-wide pass after the fragment is attached.

***

## 8. Approval batching — two parallel queues

### 8.1 `ask_user` batching

```js
const _askBatches = new Map(); // chatId → { pending: Set, answers: Map<cardId, string> }
```

```
// Multiple ask_user cards can arrive in one round. The user must answer
// ALL of them before the model gets any feedback — otherwise the model
// replies to the first answer while the rest are still unanswered.
```

When the pending set empties, answers are combined (numbered if >1) into one `inputEl.value` and one `sendMessage()`.

Stale cards are swept at the start of every new turn:

```js
card.classList.add("stale");
note.textContent = "skipped — a newer message was sent";
```

### 8.2 Permission / CWD batching

`_approvalBatches` tracks `{pendingIds, results, total}` per chat. `initApprovalBatch` is driven by the server's `permission_batch` event; each resolved card calls `_resolveBatchItem`, and when the set empties the client POSTs `/api/skills/batch-complete/{chatId}` → one combined feedback turn via `sendAutoTurnMessage`.

```js
// Fallback: send results individually
const fallback = allResults.map(r => r.feedback).join('\n\n');
```

`refreshPendingBanner(chatId)` rehydrates from `/api/skills/pending/{chatId}` on tab switch and — crucially — **preserves already-resolved results**:

```js
const preservedResults = existingBatch ? existingBatch.results : new Map();
...
// Restore previously resolved results
```

Because those tools already executed on the backend but haven't been sent to the model yet.

### 8.3 One queue container

`_ensureQueueContainer()` repurposes the old single `#approvalBanner` into a scrolling `#approvalQueueContainer` with `.aq-progress`. `_hideQueueIfEmpty()` hides it when the last card resolves.

***

## 9. Critique sessions

`_runCritiqueTurn` fires a normal `POST /api/chat` against a **sub-chat** id `"<parent>-critique-<id>"`, rendering through the standard `addBotStreaming` + `consumeChatStream` pipeline.

```
// The parent chat MUST be captured at trigger time and passed in — reading
// activeChatId here is unsafe because the critique sub-chat can already
// have been focused (sidebar refresh), which would make the critic write
// its report back into its own history instead of the main chat.
```

### 9.1 Format validation with retries

```js
const MAX_FORMAT_RETRIES = 2;
const _MARK_RE = /^\s*(?:[>#*\-•]+\s*)*\**\s*Mark\s*\**\s*[:\-—–.]/im;   // + Why, Suggestions
```

```
// A malformed report is NEVER forwarded to the main chat —
// forwarding junk would poison the main model's context.
```

On failure it re-prompts the **same** critique session with `_formatReminder(reason)` — a fixed template where only the reason varies, and the reason is built from a closed set of section names (injection-safe).

`_extractReport(text)` slices from the `Mark` header onward, dropping lead-in chatter.

> [!NOTE]
> Header regexes accept `**Mark:**`, `### Mark:`, `- Mark - 8/10` and `> Mark:`. That tolerance is deliberate — models decorate headers inconsistently — but it also means a sentence starting with the word "Mark:" would validate. Low risk in practice.

### 9.2 Stop is two-phase

The stop button first tells the **server** to stop upstream generation (`/api/chat/stop` with the critique chat id + `response_id`), *then* aborts the client `AbortController`. Reversed, the upstream would keep generating into a discarded stream.

***

## 10. Agent streams

`_runAgentTurn` → `sendAutoTurnMessage({ targetChatId: agentId, controller })`. `_activeAgentControllers` maps agent id → controller.

Lifecycle is **SSE-driven**, not promise-driven:

```
// We do NOT call finishCard here because sendAutoTurnMessage may
// return immediately (setTimeout retry when parent is streaming),
// which previously caused instant false "done" state.
```

`window._finishAgentStream(agentId, {abort})` defaults to **not** aborting:

```
// NOTE: we do NOT abort by default — endStream() is owned by
// sendAutoTurnMessage's finally block. Aborting here can truncate the
// last answer/done frames if the server hasn't flushed yet.
```

A 600 s safety timer removes the controller entry so a lost completion event can't leak it forever.

`_abortAllAgents()` is the nuclear stop — aborts every controller *and* POSTs `/api/chat/stop` for each agent and each active critique box.

***

## 11. Tool activity card

`showToolPending` reuses **one** card per turn (cancels `_tacExitTimer`) so consecutive tools don't cause layout shift. `showToolDone` fades it after 600 ms; a new `showToolPending` cancels that timer.

`_settleTurn` and `finalize` both mark any still-pending card `interrupted` and fade it — a stream that dies mid-tag never leaves a frozen spinner.

***

# Part 2 — `render-components.js`

## 12. Architecture

One IIFE, one namespace `R` (a dispatch table `kind → renderer`). `renderNode` hydrates, normalises, dispatches:

```js
function renderNode(node, depth, ctx) {
  node = hydrate(node, ctx);
  if (node.props && typeof node.props === "object") { /* hoist props → flat */ }
  var fn = R[kindOf(node)];
  if (!fn) return unknownNode(node, ctx);
  try { return fn(node, depth, ctx); }
  catch (e) { return el("div", "rc-error", "render error: " + k + " — " + e.message); }
}
```

`MAX_DEPTH = 20`, `MAX_CHILDREN = 150`.

> [!TIP]
> `props` hoisting exists because JSON-schema trees nest attributes (`{type:"badge", props:{tone:"success"}}`) while DIL puts them flat. One set of handlers reads both; **top-level keys win**, so `node.tone` beats `props.tone`.

`KIND` maps ~150 aliases (`card/panel/section → box`, `h1..h4 → title`, `metric/kpi → stat`, `b/strong → bold`, …).

***

## 13. `evalExpr` — a mini expression evaluator

The server evaluates `{expr}` **once** at parse time and ships the source as `__expr__<prop>`. `hydrate()` re-evaluates on every render, which is what makes a slider actually move a derived value.

`evalExpr` handles, in order: literals → arrow literal → balanced outer parens → ternary (chained) → `.filter(x => x.k === v).length` → `.map(...).length` → comparison → arithmetic (`+ - * / %`) → `!` → `.length` → `.toFixed(n)` → `Math.PI`/`Math.E` → `Math.*(...)` → path lookup.

### 13.1 Three fixes recorded in comments

```
// `<`, `>`, `<=`, `>=` were MISSING here while the Python
// evaluator had them -- so `hrs < 5` resolved to undefined (falsy) in the
// browser and every ternary silently took its final else-branch.
```

```
// A chained ternary (`a ? b : c ? d : e`) yields 3+ pieces and bailed under the
// old `q.length === 2` check.
```

```
// `.toFixed(n)` — must precede the Math block, or `Math.round(x).toFixed(1)`
// gets swallowed by the Math regex and evaluates to undefined.
```

And `toFixed` returns a **string**, matching both JS semantics and the Python side — returning a Number made the readout flip from `"9.0"` on first paint to `"9"` on first re-render.

`splitTop(s, ch)` is depth- and quote-aware, so `(a, b)` and `"x,y"` never split.

***

## 14. Three re-computation passes

`renderLayout` seeds state once (`resolveHooks`), then on every `ctx.set()` re-render runs:

1. `recomputeMemos(state)` — `useMemo` bodies
2. `recomputeDecls(state)` — `{@body const x = EXPR}` shipped as `__decl__<name>`
3. full tree rebuild + `lucide.createIcons()` + `typesetMath(root)`

```
// Derived `const` declarations must be replayed on every pass, BEFORE the
// tree is rebuilt -- otherwise `{hrs.toFixed(1)}` re-evaluates against a
// stale `hrs` and the readout never moves.
```

Insertion order is declaration order (Python dicts + JSON preserve it), so `hrs` recomputes before `verdict`, which depends on it.

`resolveHooks` covers `Breakpoint` (window width), `Theme` (`prefers-color-scheme`), `Memo`, `Callback`, `Effect`.

***

## 15. SVG — sanitizer, healer, live tree

The SVG story has three distinct paths and they matter.

### 15.1 Allowlist sanitizer (replaces a broken denylist)

```
// Replaces the old regex denylist. That denylist was bypassable: a `/`
// separates attributes just like whitespace, so `<svg /onload=alert(1)>`
// passed BOTH gates (`^\s*<svg[\s>]` saw the space; `\son\w+\s*=` never
// saw a space before `on`) and then innerHTML fired the handler. Denylists
// for SVG are a dead end.
```

`sanitizeSvgString` parses into a detached document, then prunes:
- non-allowlisted elements removed (`SVG_OK_ELEMS`)
- `<script>` removed unconditionally
- every `on*` attribute removed
- `href`/`xlink:href`/`src` kept only if it starts with `#`
- `<style>` content passed through `scrubCss`: `@import` stripped, `url(...)` allowed only for `#fragment` and `data:`, `expression(` and `javascript:` neutralised

### 15.2 Lenient fallback for streaming

```
// Streaming case: the SVG is still being written, so it is NOT
// well-formed XML yet. XML parsing rejects it outright, which used to
// make a partial drawing vanish into a <pre> until the closing tag
// landed. Fall back to a lenient HTML parse so the SVG renders
// bit-by-bit as it streams.
```

The prune runs on either result, so security is unchanged.

### 15.3 `healSvgMarkup`

Repairs what a streamed/generated SVG actually breaks on: wrapping code fence, truncated `markup="` prefix, double-encoded entities, XML prolog/doctype, missing `xmlns`, and **unbalanced tags** via `_balanceSvgTags` (a stack that closes whatever an interrupted stream left open). Best-effort — returns input unchanged when it can't improve it.

### 15.4 `buildSvgTree` — dynamic geometry

When the subtree carries `__expr__` coordinates the parser does **not** flatten it to `markup` (doc 20 §3.3), so the client rebuilds real SVG DOM and `hydrate` resolves each attr per render.

The prose-run discriminator is subtle:

```
//   {kind:"text", text:"A (start)"}                          -> prose run
//   {kind:"text", class:"lbl", x:"100", y:"176", text:"A"}   -> real element
// Checking `children` alone was wrong: `<text x='1'>label</text>` has its
// content hoisted into `.text` with NO children, so every <text> label in
// every diagram got demoted to a bare text node and vanished.
```

`SVG_RAW_TEXT` (`style`, `script`) children are installed via `textContent`, never parsed as markup — *"the one correct way to install a stylesheet via DOM."*

***

## 16. `richText` — math first

```js
var re = /\$\$([\s\S]+?)\$\$|(?<!\$)\$(?!\$)([^\n$]+?)\$(?!\$)|\{\{([^}]+)\}\}|\*\*([^*]+)\*\*|(?<!\*)\*([^*]+)\*(?!\*)|`([^`]+)`/g;
```

```
// Math comes FIRST in the alternation: TeX like `a*b` or `x_i` would
// otherwise be eaten by the emphasis branches, and MathJax needs the raw
// source intact.
```

`makeMathSpan` builds its delimiters from `String.fromCharCode(92)`:

```
// Backslash built via fromCharCode on PURPOSE: a source literal "\(" has its
// backslash swallowed, so MathJax never sees a delimiter and silently ignores
// the span.
```

`interp()` uses negative lookarounds `(?<!\{)\{([\w.]+)\}(?!\})` so `{{chips}}` survive interpolation.

***

## 17. Images — pending, pool, onerror chain

A `query`-based image arrives `pending: true`. `_pendingImage` draws a placeholder grid, fires `/api/search/image` (deduped by `query|count` in `_imgInflight`), and swaps in `R.image` when it lands — **or removes the placeholder entirely if nothing was found** so the grid closes up.

`R.image` keeps an ordered **pool** and a `used` map. On `error`:

```js
// Pull the next unused URL from the same query's pool.
// Pool exhausted — drop this tile so the grid closes up cleanly.
```

Click opens `window.openLightbox` with **every live tile's current src**, so a pool swap is reflected in the viewer.

`data-kind="svg"/"gif"` lets CSS letterbox vector/animated assets instead of cover-cropping — *"An SVG with only a viewBox has no intrinsic size — cover-fitting it into a fixed box is what collapsed the schematic to nothing."*

***

## 18. Charts — one scale, two shapes

`niceScale(max, ticks)` picks a 1/2/5×10ⁿ step and derives the axis top from it:

```
// No floor — a max of 3 must not become an axis of 20.
```

(the old code hard-floored at 20, squashing small datasets into the bottom 15%).

`R.chart` accepts `content` as an object **or a JSON string**, normalises 1–4 series (`["water"]` or `[{dataKey,name}]`), and renders grouped bars + legend + a dashed **mean line** with an `avg N` label in the right gutter. `R.barchart` is the JSON-schema single-series shape and reuses the same maths and CSS.

Tooltips read `data-tip`, not `<title>`:

```
// Prefer data-tip. A native <title> would ALSO fire the browser's own
// delayed tooltip, so chart bars carry data-tip + aria-label instead.
```

***

## 19. Quiz stepper

`R.quiz` renders **one** `<choices>` child at a time, gates *Next* until answered, and ends on a score card with *Try again*.

```
// Scope this step's answer state to the quiz + step. Without it, R.choices
// falls back to a key derived from the option labels -- so two questions
// with the same labels shared one `saved` slot, and "Try again" had no
// prefix to clear. That was the answer that wouldn't reset.
```

Answer detection is DOM-based: `.rc-quiz-result, .rc-choice-picked, .rc-choice-correct, .rc-choice-wrong`. Results are `1` / `0` / `-1` (poll, not scored), so the final score correctly ignores polls.

***

## 20. `__if__` — client-side branch selection

```
// The parser ships EVERY branch along with its raw `cond` source, and the
// client picks the winner against live state on each render. Without this the
// parser collapsed to the first matching branch at parse time and dropped the
// rest, so `{#if tab==="B"}` could never appear no matter what the user clicked.
```

***

## 21. Auto button rows

`renderChildren` collects consecutive non-`block` buttons into one `.rc-btn-row`:

```
// `<row>` was removed from the DSL, but a run of adjacent buttons is a
// button GROUP (a mode switcher, a set of choices) and reads badly stacked
// one-per-line.
```

Also batches adjacent inline-text children so `**` + `Planner` + `**` joins into one string **before** `richText` parses it — otherwise bold never forms.

***

## 22. `renderInto` — atomic, with last-good preservation

```
// The old contract appended straight into the live container, so every
// caller had to clear it first (`innerHTML = ""`) — and if render() threw or
// produced nothing, that left the container BLANK, destroying a drawing that
// had already rendered correctly.
```

Two levels:
1. **Whole-block** — nothing rendered → previous DOM untouched, returns `false`
2. **Per-drawing** — `carryOverSvg(stage, live)` copies the old `<svg>` into the new tree where the new one failed to parse (indexed positionally, best-effort)

***

## 23. `typesetMath` — one timer, many scopes

```
// Multiple component trees can be on screen at once and each calls this on
// every re-render. A single debounce timer would clobber itself -- the last
// caller wins and every earlier scope silently never typesets. Collect scopes
// in a set instead, flush them all in one pass.
```

Guarded by `if (!scope.querySelector(".rc-math")) return;` so a card with no math costs nothing.

***

## 24. Findings

> [!CRITICAL]
> **1. `renderAskUserCard` makes the LAST option unusable.**
> ```js
> options.forEach((opt, i) => {
>   btn.addEventListener("click", () => {
>     if (i === options.length - 1) {      // last option = manual escape hatch
>       manualWrap.style.display = "flex";
>       return;
>     }
>     submitAnswer(opt);
> ```
> Clicking the final option opens the free-text box instead of answering with it. If the server does not append a sentinel option, a 3-option question silently loses its third answer. **Not fixed** — depends on whether `ask_user` always reserves the last slot, which is a server-side contract I won't change blind.

> [!CRITICAL]
> **2. `retryLastCommand` POSTs to `/api/retry-command`, which no router registers.** No file in `server/api/routes/` defines that path (docs 17, 18, 22–24 cover all of them). The *Resend tool results* button therefore 404s → `replaceWithError("Retry failed 404…")`. **Not fixed.**

> [!WARNING]
> **3. The stall message hardcodes 75 s** even when the 900 s tool budget fired. §5.

> [!WARNING]
> **4. `_askBatches` is keyed by the *active* chat.** If the user switches chats while an `ask_user` card is unanswered, `_askBatchKey()` changes, a new empty batch is created, and the old one is never cleared. The stale card is swept on the next turn in *that* chat, but the map entry itself lingers. Bounded leak, not a correctness bug.

> [!NOTE]
> **5. `KIND` has `column: "col"` twice** (two groups in the literal). Harmless — identical value, last wins.

> [!NOTE]
> **6. `carryOverSvg` matches `.rc-svg` nodes positionally.** If the new tree has a different number of drawings than the old, the carry-over can put the wrong picture in a slot. Documented as best-effort in-code.

> [!NOTE]
> **7. `consumeChatStream` returns `gotTitle` but no caller uses it.** Dead return field.

No code changed in either file.

***

## 25. Cross-references

### `sse.js`
- [[17-server-chat-route]] — every SSE event type handled here is emitted there
- **`web/js/msg-actions.js`** — `MsgActions.ensureToolbar` / `addCopy` / `addTTS` / `attach`
- **`web/js/render-html.js`** — `SableHtml.mount/push/finish`, `SablePreview`
- **`web/js/agents.js`** — the agent EventSource that fires `agent_trigger`
- **`web/js/agent-panel.js`** — `AgentPanel.*` mirror targets
- **`web/js/agent-topbar.js`** — `AgentTopBar.failCard`
- **`web/js/markdown.js`** — `renderMarkdown`, `renderMermaidDiagrams`, `renderMathJax`, `countOpenFences`
- **`web/js/core.js`** — `twTuneFor`, `twRecord`, `TW_CHARS/TW_MS/TW_MAX_CHARS/TW_MIN_MS`

> [!NOTE] Correction — there is no `typewriter.js`
> Earlier revisions of this line (and of the README's coverage table) cited a `web/js/typewriter.js` holding the `tw*` typewriter-tuning symbols. `find web -name '*typewriter*'` returns nothing. The symbols live in **`core.js`**, and are also referenced from `sse.js` and `settings-init.js`. Corrected here and in the README; see [[26-engine-library-rag]] for the same class of ghost-file error.

### `render-components.js`
- [[20-engine-ui-format]] — the server side of this contract: `__expr__`, `pending`, DIL parsing, SVG freeze rules
- **`engine/skills/handlers/dil_parser.py`** — `_eval`, the Python twin of `evalExpr`
- **`web/js/markdown.js`** — `_lazyLoadMathJax`
- **`web/js/sse.js`** — `SableInteract` (send/fill/busy), `openLightbox`, `renderInto` caller

> [!IMPORTANT]
> Three invariants span both files and are easy to break: **(1)** a streaming block is re-rendered on every partial, so rendering must be idempotent for identical payloads (signature dedupe); **(2)** a transient card must be findable after a turn rebuild (pane fallback); **(3)** an expression must be re-evaluated on every render pass, never cached at parse time (`__expr__` → `hydrate` → `recomputeDecls`).
