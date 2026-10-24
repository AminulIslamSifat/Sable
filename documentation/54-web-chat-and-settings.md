# 54 — `web/js` chat.js & the Settings Cluster

The last large frontend surface: 3,840 lines across four files that build the composer, send every user message, and drive the ~120 endpoints doc 18 catalogued.

| File | Lines | Role |
|:--|--:|:--|
| `chat.js` | 1,067 | toasts, dialogs, tabs, scroll intent, skill cards, `sendMessage`, queueing |
| `settings-init.js` | 992 | service control, consolidation, `createChat`, attachments, stop, `sendAutoTurnMessage` |
| `settings-projects.js` | 970 | projects CRUD, folder dropdown, `loadChats`, `loadMessages`, pagination, `selectChat` |
| `settings-ui.js` | 811 | settings overlay, shortcut system, universal save, update flow |

***

## 1. Scroll intent — the hardest thing in the file

Streaming re-renders markdown with `innerHTML =`, which destroys every node and the browser's scroll anchor. Four separate mechanisms defend the user's reading position:

| Mechanism | Why |
|:--|:--|
| `_bindScrollListener` — wheel/touch/scrollbar-drag | Synchronous `_atBottom` update, **before** any `scrollBottom()` in the same tick |
| `scrollBottom()` — rAF-coalesced | Dozens of per-line calls during tool output become one layout per frame |
| `pinScrollIfAtBottom()` | Writes `scrollTop` in the **same tick** as the re-render, closing the one-frame bounce |
| `window._sableNotifyWheel` | Sandboxed `render_html` iframes swallow wheel events; the bridge forwards the delta |

Each has a comment naming the failure it prevents. The iframe bridge one is the clearest: *"their wheel / touch events never bubble to this pane, so `_atBottom` stayed true and every streamed chunk yanked the view back down while the user was reading up."*

### 1.1 🔴 `_loadOlderMessages` can permanently disable scrolling

```js
window._historyLoading = true;
const prevPane = activePane;
const tmp = document.createElement("div");
activePane = tmp;
for (const msg of batch) addHistoryMessage(msg);   // <- can throw
activePane = prevPane;
window._historyLoading = false;
```

No `try/finally`. If `addHistoryMessage` throws — a malformed message row, a missing `MsgActions`, a bad markdown payload — then:

- `window._historyLoading` stays `true`, and `scrollBottom()` **returns immediately at the top of the function**, forever.
- `activePane` stays pointing at the detached `tmp` div, so every subsequent `addMessage` renders into a container that is not in the document.

The chat appears frozen: new messages vanish, and the view never scrolls again. Only a page reload recovers.

### 1.2 🔴 `loadMessages` restores one of the two globals

```js
} catch (err) {
    console.error("Failed to load messages:", err);
    window._historyLoading = false;
    return [];
}
```

`_historyLoading` is reset. `activePane` is **not** — it stays as `tmpContainer`. Same symptom as §1.1 but reached through the outer catch: every message after the failure renders into a detached div. The `_historyLoading` reset makes it look like recovery happened.

> [!IMPORTANT] Both are one `try/finally` away
> ```js
> const prevPane = activePane; activePane = tmp;
> window._historyLoading = true;
> try { … } finally { activePane = prevPane; window._historyLoading = false; }
> ```
> The pattern already exists correctly in `chat.js`'s `endStream` and `sendMessage`'s `finally`. These two are the exceptions.

***

## 2. `sendMessage` — the send pipeline

Eight gates run before a byte goes out, in this order:

1. `_stopInFlight` — a stop request is mid-flight; block (prevents `CHAT_IN_PROGRESS`)
2. `isStreaming()` → `stopGeneration()` and **return**
3. empty message
4. `selModel === "__none__"` → "No AI provider configured"
5. speech-interruption note prepended if the user spoke over TTS
6. `@mention` → `spawnAgentFromMention` instead of a chat message
7. `_consolidationPromise && scraperMode` → queue the message
8. no `activeChatId` → `createChat()`
9. provider/scraper mode cross-guard (skipped for forks)
10. study thread → `studyChat.send(...)` and return

Gate 7 is mode-conditional on purpose: API-mode consolidation runs without blocking the composer, but scraper-mode consolidation uses the same account session and would collide.

### 2.1 `createChat` fires consolidation without awaiting it

```js
} else if (oldChatId) {
    consolidateMemory(oldChatId, selectedModel);
}
```

No `await`. The new chat is created while the old one consolidates. That is intentional — the comment says the backend handles fallback internally — but it means `createChat()` can return and the user can send into the new chat before consolidation has read the old one. If consolidation mutates memory the new chat then loads, the ordering is not defined.

### 2.2 `finally` sweeps `activeStreams` unconditionally

```js
try { if (activeStreams.has(streamChatId)) activeStreams.delete(streamChatId); } catch (_) {}
try { if (activeChatId === streamChatId) updateSendBtn(); } catch (_) {}
```

The comment is honest: *"a concurrent stop/recovery can leave a migrated key behind… so `isStreaming()` can never stay true after a turn ends — that state locks the send button into stop-mode with no way back except a manual click."* A defensive sweep compensating for `endStream`'s migration logic, which itself has an `_isSubChat` special case. Three layers, one invariant.

***

## 3. Queue-send

`queueMessage` renders an optimistic bubble with `tempId`, POSTs `/api/chat/queue`, and finalises on a `queued_injected` SSE event via `window._sableFinalizeQueued`.

The fallback path is the interesting one:

```js
if (!data.queued) {
    _pendingQueued.delete(tempId); _updateQueueBadge();
    if (bubble) bubble.remove();
    inputEl.value = message; autoResize();
    sendMessage();
}
```

If the stream ended between rendering the bubble and the request landing, the message is restored to the composer and sent normally. Correct — but the bubble is removed and re-created by `sendMessage`, so the message visibly disappears and reappears.

Badge counts only when `> 1` (`badge.hidden = n <= 1`) — a single queued message shows the button but no number.

***

## 4. Shortcuts

31 bindings, `localStorage`-persisted, user-rebindable with a recorder overlay.

### 4.1 The capture-phase dispatcher

```js
document.addEventListener('keydown', (e) => { … }, true);
```

The `true` is the point, and the comment says why: *"so no component's bubble-phase handler (or Monaco / xterm, which both swallow chords wholesale) can eat a chord first — that was the old 'almost as good as broken' wiring."*

Two allowlists gate what survives a focused input:

| Set | Contents |
|:--|:--|
| `GLOBAL_INPUT_OK` | 12 ids that must work while typing (terminal, files, search, settings, cheat-sheet…) |
| `TERMINAL_INPUT_OK` | 2 ids that punch through an xterm (`toggle-terminal`, `cheat-sheet`) — readline owns the rest |

`stop-generation` (Ctrl+X) is special-cased: inside an input it fires only when there is **no selection**, so Ctrl+X still cuts text.

### 4.2 Key matching falls back to physical codes

```python
if (!keyMatch && parsed.key.length === 1 && e.code)
    keyMatch = e.code.toLowerCase() === 'key' + parsed.key;
```

Plus three literals: `Ctrl+,` can arrive as `<`, `Ctrl+/` as `?`, and `Escape`. Each is a layout quirk that would otherwise silently kill a binding.

### 4.3 `parsed.ctrl === (e.ctrlKey || e.metaKey)`

A Ctrl binding also fires on Cmd. Intentional for macOS, but it means a user cannot bind Ctrl+X and Cmd+X separately — the recorder stores `Ctrl` for both.

***

## 5. `_universalSave`

One floating "Save All" bar; individual save buttons hidden by a hardcoded id list:

```js
const hideIds = ["personalSaveBtn", "msSaveBtn", "searchSaveBtn", "cbSaveSettings", "agentConfigSave"];
```

Dirty detection is a delegated `input`/`change` listener on `.settings-body`, filtered by `data-noSaveTrack`.

### 5.1 The load guard is a 2-second timer

```js
let _loading = true;
setTimeout(() => { _loading = false; }, 2000);
```

Any tab whose initial population takes longer than 2 s marks itself dirty on load, and the Save All bar appears before the user has touched anything. `markDirty` also checks `_loading` but nothing else does — and `_loading` is closure-scoped, so `register()` from a slow loader cannot re-arm it.

### 5.2 Errors are shown with `showToast(msg, true)`

```js
showToast("Some saves failed: " + errors.join(", "), true);
```

The second parameter is the toast **type**. `true` is not `"error"`, so `TOAST_ICONS[true]` is `undefined` and the icon falls back to `info` — a failed save renders as an informational toast.

***

## 6. Smaller findings

`applyUpdate` uses the native `confirm()` while every other dialog in the file uses `sableConfirm` — the only native dialog left in the cluster.

`loadChats`'s retry button injects an inline handler:

```js
chatsEl.innerHTML = `… onclick="loadChats('${mode || ''}')"`
```

`mode` is only ever `'scraper'` or `''`, so it is not exploitable today — but it is string interpolation into an HTML attribute, and the tree's own rule is `textContent` for anything derived.

`chatDialog` resolves the same promise twice on a backdrop click after Enter (both handlers fire, `overlay.remove()` is called twice). Harmless — `Promise.resolve` is idempotent and `remove()` on a detached node is a no-op.

`endStream` fetches `/api/chats/{id}/messages?limit=1` on every stream end just to refresh the context ring, fire-and-forget with `.catch(() => {})`. One extra request per turn.

***

## 7. Findings

| # | Where | What |
|--:|:--|:--|
| 194 | `settings-projects._loadOlderMessages` | **🔴** No `try/finally` around the fragment build. A throw from `addHistoryMessage` leaves `window._historyLoading = true` (so `scrollBottom()` returns forever) **and** `activePane` pointing at a detached div (so every later message renders invisibly). Only a reload recovers. |
| 195 | `settings-projects.loadMessages` catch | **🔴** Resets `_historyLoading` but **not** `activePane`. After a failure every message renders into `tmpContainer`. Same symptom as #194, and the `_historyLoading` reset makes it look recovered. |
| 196 | `settings-ui._universalSave` | The dirty-suppression guard is `setTimeout(…, 2000)`. Any tab loading slower than 2 s marks itself dirty unprompted and shows the Save All bar. |
| 197 | `settings-ui.saveAll` | `showToast(msg, true)` — `true` is passed as the toast **type**, so `TOAST_ICONS[true]` is `undefined`, the icon falls back to `info`, and a failed save looks informational. |
| 198 | `settings-ui.applyUpdate` | The only remaining native `confirm()` in the cluster; every other dialog uses `sableConfirm`. |
| 199 | `settings-projects.loadChats` | Retry button is `onclick="loadChats('${mode || ''}')"` interpolated into `innerHTML` — string interpolation into an HTML attribute, against the tree's own `textContent` rule. |
| 200 | `settings-init.createChat` | Calls `consolidateMemory(oldChatId, …)` without awaiting, so the new chat can be created and used while the old one is still consolidating. Ordering between the two is undefined. |

***

## 8. What the frontend shows

- **Scroll position during streaming is the hardest problem in the UI**, and it is solved with four cooperating mechanisms, each documented by the bug it prevents. This is the frontend equivalent of the connector comments.
- **Every global-assignment pattern in this cluster lacks `try/finally`** — `_historyLoading` and `activePane` are set before a loop that can throw, in two places. `activePane` is a *module-level mutable* used as an implicit parameter, which is why it is so easy to leave dangling.
- **The backend's `activeStreams` invariant needs three layers of defence** (`startStream`/`endStream`/the `finally` sweep), because chat ids migrate mid-stream and sub-chats stream under a different id.
- **Frontend findings are almost all "state left inconsistent after a throw"** — not logic errors. Seven of seven here.
