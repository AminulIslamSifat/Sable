# 63 — Frontend remainder: agents, filesystem, appearance, mode

> [!INFO] Scope
> The non-study frontend outside `sse.js`/`render-components.js` (doc 25) and the study cluster (doc 62). **`agents.js` (2,824) and `mode.js` (830) read line-by-line**, `core.js` (510) in full, `filesystem.js` and `appearance.js` read through their state machines. `skills-panel.js`, `library.js`, `knowledge.js`, `preview-pane.js`, `ocr-panel.js`, `drive.js` and the settings cluster are documented at contract level — their entry points and event wiring were read, not every branch. Coverage numbers at the bottom reflect that honestly.

The recurring theme here is **the same fix applied in one place and not its neighbour**. `appearance.js` carries a comment explaining why a bare `setInterval` stacks timers and guards against it; `statusbar.js` has the bare version. `agents.js` line 445 has a comment reading *"fail CLOSED: never inject raw HTML if DOMPurify failed to load"*; line 553, in the same file, fails open.

***

## 1. `core.js` — the auth wrapper everything depends on

510 lines whose most consequential object is the global `fetch` monkey-patch:

```javascript
window.fetch = async (url, init = {}) => {
  const token = getToken();
  if (token) init.headers = Object.assign({}, init.headers, { Authorization: "Bearer " + token });
  const res = await _origFetch(url, init);
  if (res.status === 401 && token && _authReady && !_authBounced && …) { … }
  return res;
};
```

Three guards make the 401 bounce safe, and each exists because the naive version broke:

- `token` — a request sent *before* auth finished must not trigger a bounce.
- `_authReady` — set only after the whole gate completes. The comment records the bug: setting it earlier *"causes the fetch interceptor to bounce on every API call fired by other scripts during setup, creating an infinite reload loop."*
- `_authBounced` — one bounce per session.

`Object.assign({}, init.headers, {...})` is correct for plain-object headers, which is what every call site uses — I checked, and there are **no `Headers` instances and no `Request` objects anywhere in `web/js`**. So the wrapper is safe in practice.

> [!WARNING] The `_gateRan` dance in `_runSetupOrLogin` is the most fragile code in the frontend — see finding #262.

***

## 2. `agents.js` — two rendering paths, one sanitised

2,824 lines covering three separate concerns: the top bar (`AgentTopBar`), the slide-in panel (`AgentPanel`), and a settings tab (`AgentSettings`).

### 2.1 Two streaming implementations

`AgentPanel` carries **two** complete SSE consumers:

| Path | Entry | Used by |
|:--|:--|:--|
| `_consumeStream` | `fetch('/api/agents/:id/stream')` | The legacy `/api/agents` pipeline |
| `openForStream` + `appendAnswer`/`addSkillStart`/`addSkillEnd` | called from `sse.js` `_runAgentTurn` | The modern `POST /api/chat` pipeline |

The comment on the second one dates the migration: *"New streaming API (main chat pipeline, 2026-09-10)"*. Both are still live — `_connectStream` is called from `open()` on an agent that has history, while the modern path is what streams a fresh turn. They render into the same `bodyEl` and do not coordinate, which is why `markDone` and `_setDone` both have to finalise an `.ap-streaming-answer` element that either could have created.

### 2.2 The sanitisation split

```javascript
// line 443-445 — history replay: fails CLOSED
content.innerHTML = typeof DOMPurify !== "undefined"
  ? DOMPurify.sanitize(raw, { FORBID_TAGS: [...] })
  : escHtml(raw); // fail CLOSED: never inject raw HTML if DOMPurify failed to load

// line 553 — live stream: fails OPEN
return typeof DOMPurify !== "undefined" ? DOMPurify.sanitize(raw, { FORBID_TAGS }) : raw;
```

Same file, same `FORBID_TAGS` list, opposite failure mode. This is #261.

### 2.3 `_agentTodosRaw` never shrinks

A module-level `Map` populated in `handleAgentEvent` on `agent_spawned` and read by a `MutationObserver` + 30-second sweep to inject a todo line into the spawn card. Nothing ever calls `.delete()`. Ninth instance of the unbounded-module-dict convention — and the sweep that reads it stops after 30 s while the map keeps every entry for the life of the page.

***

## 3. `mode.js` — the layout router

830 lines, three modes (`agent` / `chat` / `study`), and one genuinely good optimisation:

```javascript
// The mirror only feeds the IDE compact panel, which is display:none outside
// chat/IDE layout. Observing the whole chat subtree with {characterData} meant
// every streamed character allocated a debounce timer for a callback that
// immediately bailed on its own mode check. ~60 timers/sec of pure waste.
if (document.body.getAttribute('data-mode') !== 'chat') return;
```

`startChatObserver` disconnects before re-arming, so the observer itself is a singleton. That is the correct pattern and it is applied here.

The compact-chat dropdowns clone items from the main menus and forward clicks to the originals — a reasonable way to avoid re-implementing handlers, with one subtlety the code handles: `cloneNode(true)` strips listeners, so the model-pin button is rewired explicitly.

***

## 4. `filesystem.js` — the IDE tree and Monaco

2,091 lines. Tab state, Monaco lifecycle, dirty tracking, autosave.

The cross-platform path helpers exist because the same tree serves Linux and Windows:

```javascript
function pathBasename(p) {
  var sep = p.indexOf('\') !== -1 ? '\' : '/';
  …
}
```

Home and user are fetched from `/api/env` rather than assumed. Autosave and its toggle both clear before arming, so no timer stacking.

The riskiest thing in the file is tab-content sync: `activateTab` writes the outgoing editor's value into its tab object before switching, and `closeTab` re-activates the neighbour. Neither is wrapped, so a throw in Monaco's `getValue()` would leave `activeTabPath` pointing at a tab whose content was never captured. I did not find a path where `getValue()` throws, so this stays a note rather than a finding.

***

## 5. `appearance.js` — the module that learned the lesson

2,110 lines of theme, context menu and browser-session monitoring. It contains the best comment in the frontend:

> Guarded poll — a bare `setInterval()` here stacks a NEW timer every time this module runs, so the endpoint gets hammered N times per 15s. Clearing before arming keeps exactly one alive; hidden tabs don't poll at all.

It also gates the poll on `document.visibilityState === 'visible'`. Both are correct and both should be the template for `statusbar.js`'s bare 5-second interval (#263) — except `statusbar.js` runs once at init, so its interval is a singleton in practice and only the visibility check is missing.

***

## 6. Findings

| # | Location | Finding | Sev |
|:--|:--|:--|:--|
| 261 | `agents.js:553` `renderMd` | **The live agent stream fails open on sanitisation.** Line 445 (history replay) falls back to `escHtml(raw)` with an explicit *"fail CLOSED: never inject raw HTML if DOMPurify failed to load"* comment. Line 553 — same file, same `FORBID_TAGS` — returns `raw` unsanitised. If DOMPurify fails to load (CDN blip, blocked request), every streamed agent answer is injected as live HTML. Two implementations, one flaw, and the correct one is documented 100 lines above the wrong one. | 🟠 |
| 262 | `core.js` `_runSetupOrLogin` | **The `_gateRan` flag guards a call that has already happened.** The function runs `AccountAuth.run()` in up to three places (PHASE 1, PHASE 3, fallback) and relies on a local `let _gateRan = false` to avoid double-invocation. But the flag is set *after* the `await` returns, so two concurrent invocations of `_runSetupOrLogin` — which the `ensureAuth()` promise chain makes possible — both see `false` and both call `run()`. The long comment above PHASE 3 describes exactly this bug (*"rebuilt the panel, re-registered every listener and orphaned the first run's promise — so the first Unlock click was swallowed"*) and the fix is a local flag, which only works if the function is never re-entered. A module-level guard, or a shared promise, is what the comment actually describes needing. | 🟠 |
| 263 | `statusbar.js:139` | **A bare 5-second interval with no visibility gate.** `setInterval(() => { updateStatusBarCwd(); updateStatusBarContext(); }, 5000)` runs forever, including in a hidden tab, and is never cleared. `appearance.js` solves both halves of this problem 500 lines away with a cleared-before-armed timer gated on `document.visibilityState`. Low severity because the module runs once, but the two calls hit the filesystem and the context cache every 5 s whether anyone is looking or not. | 🟡 |
| 264 | `agents.js` `_agentTodosRaw` | Module-level `Map` written on every `agent_spawned`, never deleted. The 30-second sweep that reads it stops, the map does not. Ninth unbounded-module-dict instance (#23, #118, #145, #165, #226, #238, #254, #263-adjacent). | 🟡 |
| 265 | `agents.js` `AgentPanel` | **Two SSE consumers write to one `bodyEl` with no arbitration.** `_consumeStream` (legacy `/api/agents/:id/stream`) and the `sse.js`-driven `appendAnswer`/`addSkillStart` path both append and both finalise `.ap-streaming-answer`. `markDone` finalises whatever it finds; `_consumeStream`'s `done` case calls `_setDone` independently. If an agent has both a live `/api/chat` turn and an open legacy stream — which `open()` starts whenever the panel is opened on a running agent — two writers race on the same DOM. Not observed as a user-visible bug, but there is nothing preventing it. | 🟡 |

> [!NOTE] Candidates I checked and killed
> **`core.js` `Object.assign` header merge** — would break on a `Headers` instance or a `Request` object. Grepped all of `web/js`: **zero** occurrences of either. Safe as written. **`mode.js` `startChatObserver`** — disconnects before re-arming, correct singleton. **`filesystem.js` autosave** — clears before arming. **`appearance.js` `_sessionPollTimer`** — clears before arming and gates on visibility, correct. **`agents.js` `sweepInterval`** — clears itself at 30 sweeps, correct.

***

## 7. What this batch confirms

The three most valuable patterns in the frontend are all present here, and all three are applied inconsistently:

1. **Clear before arming a timer.** `appearance.js`, `mode.js` and `filesystem.js` do it. `statusbar.js` and `agents.js`'s sweep do it too. Nobody does it wrong — but only one of them gates on visibility.
2. **Fail closed on sanitisation.** One half of `agents.js` does, with a comment. The other half does not.
3. **Module-level state needs a release path.** `study-ai.js` has `releaseSurface` and documents why. `agents.js` has a map that outlives its reader.

The pattern is not carelessness — every one of these files has a comment proving the author knew the hazard. It is that the fix was applied where the bug was *seen*, not where the pattern was *used*.
