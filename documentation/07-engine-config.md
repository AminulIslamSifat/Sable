# `engine/config.py` — The Single Source of Truth

> [!INFO] Scope
> **1,494 lines.** Read in full. Every other module imports from here, so this doc doubles as a map of Sable's state files.

***

## Path roots

```python
_ROOT = Path(__file__).resolve().parent.parent          # repo root

_PERSISTENT_OVERRIDE = os.getenv("SABLE_PERSISTENT_ROOT")
PERSISTENT_ROOT = Path(_PERSISTENT_OVERRIDE).resolve() if _PERSISTENT_OVERRIDE else _ROOT

_SYSTEM = _ROOT / "system"                              # ← note: _ROOT, not PERSISTENT_ROOT
```

> [!CRITICAL] Bug: `PERSISTENT_ROOT` is not actually persistent
> `PERSISTENT_ROOT` honours `SABLE_PERSISTENT_ROOT`, but `_SYSTEM` — which backs **every** state path (browser profiles, memory, agent config, Qwen tokens, settings, exhaustion/captcha stores) — is hardcoded to `_ROOT / "system"`.
>
> Six real call sites *do* use `PERSISTENT_ROOT`: `connectors/common/jitter.py`, `connectors/common/prompt_injection.py`, `server/api/application.py`, `server/api/routes/telegram.py`, `server/api/routes/telegram_bot.py`, `engine/drive/client.py`.
>
> **Consequence:** setting `SABLE_PERSISTENT_ROOT` splits state across two trees. Telegram config, drive credentials and jitter state move; browser profiles, memory and tokens stay put. A "shared persistent root" silently doesn't share the things that matter.
>
> Fix: `_SYSTEM = PERSISTENT_ROOT / "system"`. Check whether the six call sites relied on the old split first — they may double-prefix `system/`.

### Output tree — deliberately outside the repo

```python
OUTPUT_ROOT = HOME_DIR / "sable_output"
├── research/   notes/   prompts/   agent/   assets/   logs/   library/
```

> *"Lives outside project root so outputs survive tree wipes/rebuilds."* This is why `/home/sifat/sable_output/` exists as a convention throughout the codebase.

## Bind settings — env > file > default

```python
HOST = os.getenv("SABLE_HOST") or _host_from_settings() or "0.0.0.0"
PORT = int(os.getenv("PORT", os.getenv("SABLE_PORT", "61770")))
```

`_host_from_settings()` reads `system/network_settings.json` and accepts only `"0.0.0.0"` or `"127.0.0.1"` — anything else is ignored. So the UI can toggle LAN access without an env var.

> [!NOTE] `PORT` is checked before `SABLE_PORT`
> `os.getenv("PORT", os.getenv("SABLE_PORT", ...))` — an inherited `PORT` from some unrelated tooling wins over Sable's own variable.

***

## The Study Library — 20 paths

All under `OUTPUT_ROOT/library/`. The comments explain the layout decisions:

| Path | Why it exists |
|:--|:--|
| `pdfs/` | Uploaded PDFs |
| `meta/<doc-id>.json` | Everything about a doc except text |
| `text/<doc-id>.json` | **Split out** so *"the meta record stays small and highlight/progress writes stay cheap"* |
| `originals/` | Non-PDF uploads kept verbatim |
| `academics.json` | Semesters → subjects → topics tree |
| `cover.json` / `covers/` | Cover-page composer state + generated PDFs |
| `notes/` | Per-doc notes |
| `thumbs/` | **One small JPEG per doc** so *"the library grid doesn't have to re-render a PDF to draw a card"* |
| `srs/` | Spaced-repetition decks + scheduling |
| `dictionary/` | Bangla dictionary data |
| `search.db` | SQLite FTS5 index over `text/` |
| `sessions.json` | Time-on-doc analytics |
| `embeddings/` | Per-doc chunk vectors |
| `jobs/` | **Resumable** OCR/AI index cursors |
| `staging/` | Upload bytes land here first |
| `upload_jobs/` | staging → convert → extract → index state |
| `ocr_pages/` | Rendered page images, cached so resume is cheap |

> [!TIP] The staging split is a latency decision
> *"The HTTP request only streams the file to staging and returns; conversion/extraction happen in a background pipeline that reads from this directory."* Without it, uploading a 200-page PDF would hold the request open for minutes.

***

## Provider accounts — the per-provider slot system

### 11 providers, one primary

```python
BROWSER_PROVIDERS = (
    "qwen", "deepseek", "deepseek_scraper", "qwen_scraper",
    "zai", "gemini_web", "mimo", "kimi", "chatgpt",
    "mistral_web", "claude_scraper",
)
PRIMARY_BROWSER_PROVIDER = "qwen"
```

> *"`qwen` is the PRIMARY slot: internal callers that don't name a provider (default chat browser, DeepSeek, scraper fallbacks) resolve to it."*

### Two files, one legacy

| File | Status |
|:--|:--|
| `system/.active_account` | **Legacy.** *"kept ONLY for one-time migration… No provider reads it at runtime anymore."* Still written to by `set_provider_active_account()` for stragglers |
| `system/.active_accounts.json` | Live store: `{provider: "browser-data-accN"}` |

`_ensure_active_accounts_migrated()` seeds **every** provider slot from the legacy global on first run, guarded by `_ACTIVE_ACCOUNTS_MIGRATED`.

### `get_provider_active_account(provider)` — with filesystem validation

Three-stage resolution:

1. Alias-aware lookup via `_PROVIDER_ALIASES` (e.g. `"gemini"` → `"gemini_web"`).
2. Fall back to the primary provider's slot.
3. Auto-select via `get_next_available_account()` and **persist** so the repair sticks.

```python
val = store.get(key)
if val and _account_dir_exists(val):
    return val
if val:
    logger.warning("Active account %r for provider %r no longer exists — repairing", val, key)
    store.pop(key, None)
    dirty = True
```

> [!IMPORTANT] The validation exists because of a silent wrong-backend bug
> *"a stale id whose profile directory no longer exists (e.g. `browser-data-acc117` after the profile was deleted) is dropped and re-resolved instead of being returned. Without this a provider silently resolved to a missing dir, its connector reported `is_available == False`, and the chat router fell back to a DIFFERENT backend (deepseek-scraper silently ran on qwen)."*
>
> That failure mode is brutal to debug: nothing errors, a different model answers.

### Alias table

```python
_PROVIDER_ALIASES = {
    "gemini_web": ("gemini_web", "gemini"),
    "chatgpt":    ("chatgpt", "openai_chatgpt", "gpt"),
    "mimo":       ("mimo", "mimo_studio", "xiaomimimo"),
    "claude":     ("claude_scraper", "claude", "anthropic"),
    "claude_scraper": ("claude_scraper", "claude", "anthropic"),
    ...
}
```

### Deprecated constants

```python
BROWSER_DATA_DIR = _SYSTEM / "browser-data"            # resolves at import time
```

> *"Backward-compatible constant — resolves at import time. Code that needs the current active account should call `get_browser_data_dir()`."* The constant is a **symlink target** (`system/browser-data → browser-data-acc1`), not a live account lookup. Using it after an account switch gives you the wrong profile.

***

## Sync-fallback tracking

```python
_SYNC_FAILED_FILE = _SYSTEM / ".sync_failed_accounts"
```

When `sync_context()` (the Qwen persona push) fails for an account, that account is added to this file. Every chat on it then gets `[SYSTEM INSTRUCTION]…` prepended to the first message until a sync succeeds again.

| Function | Does |
|:--|:--|
| `mark_sync_failed(account)` | Add to the set |
| `clear_sync_failed(account)` | Remove; unlink the file if empty |
| `needs_instruction_fallback(account=None)` | Is this account in the set? |

Read by `service.py`'s `stream_events()` before building the request body.

***

## Model registry

### Schema

```python
{
  "id": "qwen3.8-max",
  "label": "Qwen3.8 Max",
  "api_backend": "deepseek",        # optional — absent means Qwen-native
  "api_model_type": None,           # backend-specific model pin
  "max_session_chars": 3_000_000,
  "experimental": True,             # optional flag
  "capabilities": {"image": True, "video": False, "document": True, "audio": False},
  "thinking_modes": [
    {"id": "fast", "label": "Fast", "thinking_enabled": False,
     "auto_thinking": False, "thinking_mode": "Fast"},
  ],
}
```

`thinking_mode` is the literal string sent upstream. It differs per backend — z.ai uses `Fast/Low/High/Max`, Gemini uses `Fast/Low/Medium/High`, Claude uses `auto/medium/high` (mapped to its `effort` field).

### The 24 static models

| Backend | Models |
|:--|:--|
| Qwen (no `api_backend`) | `qwen3.8-max`, `qwen3.7-max`, `qwen3.7-plus`, `qwen3.8-omni-flash` |
| `deepseek` | `deepseek-instant` |
| `zai` | `zai-glm-5.3-flash`, `zai-glm-5.3`, `zai-glm-5.2`, `zai-glm-5-turbo`, `zai-glm-5v-turbo`, `zai-glm-4.7`, `zai-glm-4.6v` |
| `gemini` | `gemini-2.5-flash`, `gemini-2.5-pro` |
| `gemini_web` | `gemini-web` |
| Others | `mimo-*` (3), `mistral-web`, `deepseek-scraper`, `qwen-scraper`, `kimi-k2.6`, `chatgpt-gpt-5`, `claude-sonnet` |

> [!NOTE] Two models carry unusual constraints, documented in-source
> **`deepseek-instant`**: *"DeepSeek unified all modes into one model. Vision auto-detected from `ref_file_ids`; expert mode removed upstream."*
>
> **`claude-sonnet`**: *"No model pinning: the SPA uses whatever model the account is set to. The engine only ever types into the composer and sends — it never touches the model picker."* Also `"experimental": True` and *"NOT tied to the global scraper-mode toggle."*

`max_session_chars` ranges 3M (Qwen) to 4M (everything else).

### Dynamic registry

```python
_CUSTOM_MODELS_PATH = _SYSTEM / ".custom_models.json"
_HIDDEN_MODELS_PATH = _SYSTEM / ".hidden_models.json"
```

```python
def get_all_models():
    hidden     = set(_load_hidden_models())
    custom     = _load_custom_models()
    custom_ids = {m["id"] for m in custom}
    merged = [m for m in MODELS if m["id"] not in custom_ids and m["id"] not in hidden]
    merged.extend(m for m in custom if m.get("id") not in hidden)
    return merged
```

> [!TIP] Three-way interaction
> A custom model with the same id as a static one **replaces** it. A hidden id removes it regardless of source. Deleting a model from the UI writes to `.hidden_models.json` — it does not touch `config.py`.

| Function | Does |
|:--|:--|
| `add_custom_model(def)` | Upsert by id |
| `remove_custom_model(id)` | Remove; returns `True` if it existed |
| `get_model_config(id=None)` | Search all, fall back to first entry — never raises |
| `get_thinking_mode_config(id, mode_id)` | Falls back to the model's first mode; always returns safe defaults |

```python
_defaults = {"thinking_enabled": False, "auto_thinking": False, "thinking_mode": "Fast"}
return {**_defaults, **selected}
```

> *"Always returns a dict with safe defaults for all expected keys so incomplete custom model configs never cause KeyError downstream."*

***

## Qwen token store

```python
_QWEN_TOKENS_PATH = _SYSTEM / ".qwen_tokens.json"
_QWEN_MAX_TOKENS_PER_ACCOUNT = 10
_QWEN_LEGACY_MIGRATED = False
```

Shape: `{account: [{cookies, bx_ua, bx_umidtoken, bearer?}, ...]}`

`load_qwen_token_store()` tolerates **three** historical shapes:

| Stored value | Handling |
|:--|:--|
| `list` of dicts | used as-is, filtered to entries with `cookies` |
| single `dict` | wrapped into a one-element list (old format) |
| anything else | `[]` |

Plus a one-time migration from the flat `.session_tokens.json` into the active account's slot.

### `sanitize_qwen_cookies()` — the silent-auth-failure fix

```python
_NON_QWEN_COOKIE_NAMES = frozenset({
    "SID", "HSID", "SSID", "APISID", "SAPISID", "LSID", "OSID",
    "__Secure-1PSID", "__Secure-3PSID", "__Secure-OSID",
    "__Secure-1PAPISID", "__Secure-3PAPISID",
    "__Secure-1PSIDTS", "__Secure-3PSIDTS",
    "__Secure-1PSIDCC", "__Secure-3PSIDCC", "__Host-1PLSID", "__Host-3PLSID",
    "SIDCC", "ACCOUNT_CHOOSER", "SEARCH_SAMESITE", "NID", "AEC", "OTZ",
    "smidV2", "__Host-GAPS",
})
```

Two jobs: drop Google/YouTube SSO cookies, and **collapse duplicate names to the last value**.

> [!CRITICAL] Why duplicate cookies break auth
> *"Alibaba's WAF treats a duplicated session cookie (two different `acw_tc` values in one string) as a tampered/replayed fingerprint and rejects the request with HTTP 200 + `{"code": "Unauthorized"}` — a silent auth failure even though the JWT login cookie is still valid."*
>
> This is the same failure `session.py`'s `sync_context()` self-heals from. Sanitizing on **write and read** means already-stored dirty blobs get cleaned without waiting for a refresh.

`get_qwen_tokens_for_account(account=None)`:
- Explicit account → **strict lookup, no fallback**. Returns `None` if that account has nothing.
- No account → active account only.

> [!NOTE] The strict-lookup rule matters
> An explicit account that has no tokens returns `None` rather than borrowing another account's. Borrowing would send account A's cookies while claiming to be B — exactly the wrong-user bug `get_mistral_web_cookies()` guards against.

`save_qwen_tokens_for_account()` **replaces** the entry (1 per account), sanitizes first, and optionally stores `bearer`.

***

## Exhaustion & captcha tracking

| File | Meaning | Reset |
|:--|:--|:--|
| `.qwen_exhaustion.json` | Daily quota used up | UTC midnight **+ per-account jitter** |
| `.qwen_captcha_blocks.json` | WAF/captcha wall | UTC date rollover |

### The jitter is a concurrency fix

```python
_jitter_minutes = hash(account) % 60
_reset_time = datetime.combine(now.date(), datetime.min.time(), tzinfo=timezone.utc) + timedelta(minutes=_jitter_minutes)
if dt < _reset_time:
    store[account] = {"exhausted": False, "exhausted_at": None}
```

> *"FIX #6: Per-account jitter (0-60 min) based on account name hash. Prevents all accounts from resetting at exactly UTC midnight, which causes concurrent requests to select the same freshly-reset account."*

Without it, 13 accounts all become available in the same second and the first request hammers whichever one the sort picked.

### `get_next_available_account(exclude, prefer_non_captcha=True)`

```
1. fresh (not exhausted, not captcha-blocked) → lowest account number first
2. no fresh → earliest captcha-blocked (oldest timestamp = most likely recovered)
3. else → None (everything exhausted)
```

Scans `_SYSTEM.iterdir()` with `re.match(r"browser-data-acc(\d+)$", entry.name)` — so the account list is **the filesystem**, not a config file. Create a directory and the account exists.

### `get_available_accounts_reverse(exclude, limit=5)`

Same scan, **highest number first**, includes captcha-blocked after fresh ones.

> *"Used by background tasks (summarizer, consolidation, subagents) so they don't compete with the main chat auto-switch which searches forward."* Main chat and background work approach the account pool from opposite ends — they meet in the middle instead of colliding.

### `auto_switch_account(current, error_type, exclude, reverse)`

| `error_type` | Marks as |
|:--|:--|
| `waf_blocked` | captcha-blocked |
| `empty_exhausted` | exhausted |
| `rate_limited` | exhausted |

Then searches forward (main chat) or reverse (background) with the failing account excluded.

***

## Tool output cap

```python
DEFAULT_MAX_TOOL_OUTPUT_CHARS = 100_000

def get_max_tool_output_chars() -> int:
    # reads system/settings.json → max_tool_output_chars
```

> [!WARNING] 100 K chars is roughly 25 K tokens
> A single `cat` of a large file can consume a quarter of a 100 K context window. This is the most likely cause of "why did the model forget everything" — the tool result ate the budget.

***

## Key numbers

| Constant | Value |
|:--|:--|
| Default port | 61770 |
| Default host | 0.0.0.0 |
| `MEMORY_SEARCH_MAX_PROMPT_CHARS` | 20,000 |
| `DEFAULT_MAX_TOOL_OUTPUT_CHARS` | 100,000 |
| `_QWEN_MAX_TOKENS_PER_ACCOUNT` | 10 (constant, but `save_*` keeps only 1) |
| Browser providers | 11 |
| Static models | 24 |
| Library paths | 20 |

> [!NOTE] `_QWEN_MAX_TOKENS_PER_ACCOUNT = 10` is dead
> It's declared but never read. `save_qwen_tokens_for_account()` does `store[acct] = [entry]` — a single entry, unconditionally. The constant suggests a history that no longer exists.
