# 52 — Ghost Sessions (DeepSeek, Kimi, ChatGPT) & Image Providers

Closes the `connectors/` tree. Three session managers that hold a browser tab open, and three image-generation backends that share nothing but a return shape.

| File | Lines | Role |
|:--|--:|:--|
| `connectors/deepseek/ghost.py` | 761 | Headed ghost tab, CDP identity sniff |
| `connectors/kimi/ghost.py` | 931 | Connect-protocol framing, JWT persistence |
| `connectors/chatgpt/ghost.py` | 861 | Fetch hijack, submit watchdog |
| `connectors/cloudflare/client.py` | 275 | FLUX via Workers AI, single token |
| `connectors/puter/client.py` | 302 | GPT Image / Imagen via driver endpoint, multi-key |
| `connectors/opencode/client.py` | 164 | `OpenAICompatClient` subclass, anonymous gate |

***

## 1. Three session managers, one skeleton

All three do the same five things, and diverge only where the provider forces it:

| | DeepSeek | Kimi | ChatGPT |
|:--|:--|:--|:--|
| Browser | `ProfileHub.acquire`, headed | same, headed or headless | same, headed |
| Identity | CDP sniff of a real UI send | `localStorage` JWT + `__snaker__id` | SPA mints everything |
| Bridge | `__ghostEvent` | `__ghostEvent` + `rid` filter | `__ghostEvent`, 40ms/8KB batching |
| Lock | 8s timeout → force-close stale | **bare acquire** | 8s timeout → force-close stale |
| Stop | `force_close()` | `cancel_chat()` RPC | `force_close()` |

### 1.1 DeepSeek sniffs identity because a cached set gets banned

```
ALWAYS sniff fresh. x-hif-leim rotates and x-hif-dliq is per-device but was
never captured by the old page.route sniffer. A stale cached signature replayed
against a fresh token is the frozen-fingerprint mismatch that gets accounts
silently banned.
```

The sniff uses **CDP `Network.requestWillBeSent`**, not `page.route` — because Playwright's route API never surfaces `x-hif-dliq`. It drives one real UI send (`hi` + Enter), waits 7s, captures the completion POST headers, and requires `x-hif-dliq`, `x-hif-leim`, `x-device-id` to all be present or it raises. Cached identity is a 24h fallback only, and only if the fresh sniff *fails*.

The cost is one real message in the user's account per session start. The comment accepts that trade explicitly.

### 1.2 ChatGPT batches the bridge

```js
let buf = ''; let flushing = false;
const timer = setInterval(() => { flush().catch(() => {}); }, 40);
...
if (buf.length >= 8192) await flush();
```

Every `__ghostEvent()` call is one CDP roundtrip. Flushing at 40 ms or 8 KB collapses hundreds of roundtrips into a handful. DeepSeek and Kimi emit per-chunk and pay for it — visible in their comment: *"cheap (one append per event)"* on the debug capture path.

### 1.3 ChatGPT's `_resend_watchdog` is the only workaround for a silent swallow

Observed: `/f/conversation/prepare` fires but `POST /f/conversation` never does. The SPA's submit handler aborted. `JS_SEND` types a **short placeholder** (`hi`), not the real prompt, specifically so React's async composer commit is out of the send path — the fetch hook rewrites `messages[]` anyway.

When the POST still doesn't appear, the watchdog re-fires `requestSubmit()` at 6s and 12s, guarded on `window.__ghostSentAt`. Safe because nothing was sent.

> [!IMPORTANT] Three separate defences for one flaky submit
> Placeholder text (§1.3), composer cleared inside the fetch hook rather than in `JS_SEND`, and the watchdog. Each comment records a distinct reproduced failure. This is the most defended send path in the tree.

### 1.4 Kimi's JWT handling is the only correct token check in the tree

```python
def _jwt_not_expired(token, skew=30):
    claims = json.loads(base64.urlsafe_b64decode(payload))
    exp = claims.get('exp')
    if not isinstance(exp, (int, float)):
        return True          # no exp claim — can't prove it's live
    return exp > (time.time() + skew)
```

`has_token()` decodes the `exp` claim instead of checking presence. The docstring explains why: Kimi's bearer expires in 900s, so a presence check reports "logged in" for a dead token — *"which is exactly what made the refresh endpoint lie."*

It also persists every valid token to `system/.kimi_tokens.json` and injects it on cold start, so a restart doesn't wait for the SPA's silent refresh.

> [!TIP] Contrast with z.ai (#160)
> z.ai's `is_available` reads only a JSON store and never looks at the browser. Kimi reads the browser, validates the JWT, and writes the store. Same problem, opposite ends of correctness.

### 1.5 🔵 Kimi carries ~150 lines of unreachable launch code

`_launch()`, `_adopt_existing()`, and `_ensure_profile_free()` have **zero callers** in the file. `_reclaim_orphans()` is called only from `_ensure_profile_free()`, so it is unreachable too. `start()` goes straight to `ProfileHub.acquire`.

This is a superseded generation — the CDP-marker adoption that DeepSeek and inpage still use, left behind when Kimi moved to `ProfileHub`. It is not harmful, but `_ensure_profile_free` contains the tree's best explanation of the *refuse-to-kill-a-foreign-browser* rule, and that explanation is now unreachable documentation.

### 1.6 Kimi's stream lock has no timeout

```python
await self._stream_lock.acquire()          # kimi/ghost.py:757
```

DeepSeek and ChatGPT both wrap this in `wait_for(..., timeout=8.0)` and force-close a stale `_active_response` on timeout. Kimi does not. A stream abandoned before `__aexit__` runs — the exact case `force_close` exists for — leaves the lock held and every future message on that account blocks forever.

Kimi is also the only one whose `__aexit__` has **no `wait_for` bound** on `await self._task`, so a wedged evaluate task holds the lock open even when `__aexit__` does run.

***

## 2. Image providers

### 2.1 Cloudflare — one token, synchronous, auto-fetched account

`generate_image` is a plain `def` using `httpx.Client` (sync). `_ensure_account_id()` calls `GET /accounts` and takes `accounts[0]` if no account id is configured — silently correct with exactly one account, silently wrong with two.

Model params are filtered by name substring:

```python
if "sd" in model_id or "dreamshaper" in model_id:
    payload["width"] = width; payload["height"] = height
```

So a FLUX request never receives `width`/`height`. `_SHAPES` is therefore decorative for FLUX — the returned `width`/`height` in the result dict are the *requested* shape, not the generated one. The image is whatever the model defaults to.

### 2.2 Puter — multi-key, and the key is sanitised

```python
safe_key = key.encode("ascii", "ignore").decode("ascii")
```

Copy-pasted keys carry Unicode quotes and en-dashes from rendered pages. Stripping non-ASCII is a blunt fix that also silently mangles a legitimately non-ASCII key — acceptable for a bearer token, and the only place in the tree that does it.

Rotation is per-key with `_auto_rotate_enabled("puter")` respected (unlike Gemini's summariser, #170).

### 2.3 OpenCode — subclass with two gate requirements

Both are verified-and-commented, which makes them maintainable:

1. **No `Authorization` header.** The free tier is anonymous; a dummy key satisfies the base class and is never sent.
2. **A tool named exactly `read` must be in `tools[]`.** Without it the request 403s. `_tweak_payload` appends a stub `read` tool if missing.

> [!WARNING] The `read` tool cannot be executed
> `grep` across the tree finds **no** skill handler named `read` — not in `engine/skills/`, not in `engine/skills/handlers/`. The stub is appended so the gate accepts the request, and the model is documented as never invoking it unless it asks. If a model *does* call `read`, the dispatch finds nothing.

`stream_chat` catches a 403 and retries once with a fresh `ses_` id — but the base class raises rather than yielding an error event, so the wrapper only works if `OpenAICompatClient` propagates exceptions out of the generator. That is an assumption about the parent class, not a contract stated anywhere in this file.

***

## 3. Findings

| # | Where | What |
|--:|:--|:--|
| 179 | `kimi/ghost.py` `open_stream` | `await self._stream_lock.acquire()` with no timeout, and `__aexit__` awaits the evaluate task unbounded. DeepSeek and ChatGPT both bound this at 8s and force-close a stale stream; a stream abandoned before `__aexit__` wedges every future Kimi message on that account. |
| 180 | `kimi/ghost.py` | `_launch`, `_adopt_existing`, `_ensure_profile_free` and (transitively) `_reclaim_orphans` have zero callers — ~150 lines superseded by `ProfileHub`. Includes the best foreign-browser-refusal explanation in the tree, now unreachable. |
| 181 | `opencode/client.py` | The mandatory `read` tool has no handler anywhere in `engine/skills`. Appended to satisfy the gate; a model that calls it dispatches to nothing. |
| 182 | `cloudflare/client.py` | FLUX requests never receive `width`/`height` (gated on `"sd" in model_id`), yet the result dict reports the requested shape as if it were the generated size. |
| 183 | `cloudflare._ensure_account_id` | Takes `accounts[0]` from `GET /accounts` when no account id is configured. Correct with one account, silently wrong with more. |
| 184 | `chatgpt/ghost.py` `_on_event` | Appends **every** bridge event to `~/sable_output/logs/chatgpt_sse_<account>.jsonl`, always, with no cap or rotation — the comment says *"enabled always for now"*. Third unbounded conversation log (#137, #169). |
| 185 | `deepseek/ghost.py` `_sniff_identity` | Requires three `x-hif-*` headers or raises; costs one real `hi` message in the user's account per session start. Correct and deliberate, but it is a quota cost per respawn. |

***

## 4. `connectors/` — closing summary

18,733 lines read across 20 provider dirs and `common/`.

**What held up.** `connectors/common/` (6,623 lines, docs 27/30/42/46/47) is the most carefully written block in the tree — the four best-commented files there produced zero 🟡+ findings. `profile_hub.py` solves a real cross-connector lock war with a documented design.

**What did not.** The provider dirs are near-copies written against whichever failure the author had just hit. Every one carries at least one load-bearing comment explaining a reproduced bug, and none of them share a base class that would have preserved those lessons.

**The duplicated-invariant count, final:**

| Pattern | Instances |
|:--|:--|
| Two implementations, one flaw | #57/#152, #156, #172 |
| Base-class convention enforced by nothing | #132, #134, #138, #173, #180 |
| Unbounded module-level dict keyed by chat | #23, #118, #145, #165 |
| Unbounded conversation log | #137, #169, #184 |
| Correct only on the author's machine | #143, #155, #157, #181 |

> [!NOTE] The `read` finding (#181) is the clearest example of the third row
> OpenCode is asked to declare a tool it cannot execute, and nothing in the codebase records that fact. A model that calls `read` gets silence.

Python is now **~89,700 / 137,031 ≈ 65%**. `engine/research/` and `connectors/` both closed. Remaining large blocks: `skills/document_skills/` (~4,000), `tests/` + `scripts/` (~13,560), and the 18 `web/js` modules.
