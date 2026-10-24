# 18 — `server/api/routes/settings.py` (4,742 lines, ~120 endpoints)

> [!IMPORTANT]
> The largest router in the codebase. Everything the Settings UI touches: scraper/connector transports, browser profiles and accounts, provider API keys, custom endpoints and models, MCP servers, search providers, TTS, STT, data export/import, and general app settings.

**Read depth:** full file, line 1 → 4,742.

***

## 1. Shape of the file

Nine near-independent subsystems sharing one `APIRouter`, in this order:

| Block | Endpoints | Notes |
|:--|--:|:--|
| Profile helpers | — | `_validate_profile_name`, `_strip_one_profile`, `_STRIP_KEEP` |
| Scraper settings | 4 | engine swap tears down live browsers |
| Connector transport/headless | 3 | data-driven capability map |
| Browser headless + slots | 4 | slot kill UI |
| Network bind mode | 2 | `127.0.0.1` vs `0.0.0.0` |
| Token refresh (per provider) | 9 | DeepSeek, z.ai, Gemini, MiMo, Mistral, Kimi, all, WAF |
| Jitter + prompt-injection | 4 | toggle matrices |
| API key pools | ~24 | Gemini/Groq/Mistral/OpenAI/Puter/Cloudflare |
| Custom endpoints + models | 10 | OpenAI-compatible bring-your-own |
| MCP servers | 8 | add/update/connect/call |
| Accounts & browser profiles | ~30 | the biggest block |
| Data export/import | 2 | NDJSON streaming |
| Context-pass / consolidation | 4 | summarizer model selection |
| TTS | 7 | Kokoro + Edge |
| Search | 10 | providers, keys, test, compare |
| STT | 7 | faster-whisper |
| General | 2 | tool output cap, notifications |

***

## 2. Profile-name validation — the one security gate

```python
_PROFILE_RE = re.compile(r"^browser-data-[A-Za-z0-9._-]+$")
```

`_validate_profile_name()` rejects: empty, non-matching, `.bak` suffix, any `/`, any backslash, `.`/`..`. Called by `profile_size`, `delete_account`, `backup_account` (first copy), `restore_account` (first copy), `rename_account`, `open_account_browser`.

> [!WARNING]
> The **second** copies of `backup_account`/`restore_account` (see §18) do *not* use this helper — they use a stricter `^browser-data-acc\d+$` regex. Same URL, different validation.

***

## 3. `_strip_one_profile` — profile slimming

Keeps only session data, deletes the rest. `_STRIP_KEEP` is a whitelist of 19 paths: `Local State`, `Last Version`, `Default/Cookies`, `Local Storage`, `Session Storage`, `IndexedDB`, `Preferences`, `Login Data`, `Web Data`, `TransportSecurity`, `Trust Tokens`, etc.

Mechanism: copy whitelist → temp dir, `rmtree` the profile, recreate, copy back, delete temp. That's a full rewrite, not a delete-in-place — it guarantees no orphaned files but needs 2× free space momentarily.

> [!CRITICAL]
> **The active-account guard raises `RuntimeError` inside a `try` that only catches `OSError`.** The raise is deliberate (it must abort the strip), and it propagates correctly — but it means the guard does not distinguish "refused on purpose" from "crashed". A caller logging `except Exception` can't tell. Verbatim intent:
> *"never strip a profile that is ACTIVE for ANY provider. Matters when invoked as a background task after a switch."*

Callers: `strip_browser_profiles` (manual endpoint), `switch_account` (auto-strip the old profile, fire-and-forget).

***

## 4. Scraper settings — why a toggle needs a restart

`update_scraper_settings_route` compares old vs new and acts only on *changes*:

| Change | Action |
|:--|:--|
| `engine_type` changed or `enabled` → false | `scraper_service.stop()` |
| `headless` flipped | `_restart_scraper_connectors()` — shuts down every cached DeepSeek/Qwen scraper client |
| `chatgpt_headless` flipped | `_restart_chatgpt_ghost_sessions()` |
| still enabled | `scraper_service.prelaunch()` |

> [!TIP]
> The comment is the whole rationale:
> *"the running browser keeps its launch flags, so the mode only takes effect on a fresh launch. Live scraper sessions that are held by connector engines are restarted for real; otherwise the user flips the toggle, sees nothing happen, and thinks it is broken."*

`_restart_scraper_connectors` reaches into module-level caches (`connectors.deepseek_scraper.client._clients`, `_default_client`, same for qwen) and calls `shutdown()` on each unique client, deduped by `id()`.

***

## 5. Connector capabilities — the data-driven toggle map

```python
_GHOST_KEY = {"qwen": "qwen_use_ghost", "deepseek": "deepseek_use_ghost", "chatgpt": "chatgpt_use_ghost"}
_HEADLESS_SETTINGS_KEY = {
    "qwen": "qwen_headless", "deepseek": "deepseek_headless",
    "claude_scraper": "claude_headless",
}
```

`_connector_capabilities()` returns, per provider, `{label, transport, headless}` where `None`/`False` means "not offered".

| Provider | transport | headless | Why |
|:--|:--|:--|:--|
| qwen | ✓ | ✓ | ghost/curl + launch mode |
| deepseek | ✓ | ✓ | ghost/curl + launch mode |
| chatgpt | ✗ | ✓ | ghost-only; headless relaunches the ghost |
| kimi | ✗ | ✗ | ghost-only; browser is token-refresh-only, cannot go headed |
| claude_scraper | `None` | ✓ | no ghost/curl split → row omitted entirely |

> [!IMPORTANT]
> `transport: None` (Claude) means the UI **omits the row**. `transport: False` (ChatGPT/Kimi) means the row shows, locked. That distinction is deliberate and worth preserving — a `None` mislabelled as `"ghost"` was a real prior bug.

`update_connectors_settings` validates against the map *before* touching anything: `"qwen does not support a transport toggle"` is a 400, not a silent no-op.

> [!NOTE]
> Claude's launch mode deliberately does **not** share the global scraper `headless` flag:
> *"Claude scraper keeps its own launch mode — it must NOT share the global scraper-engine `headless` flag with the Qwen/DeepSeek engines."*

***

## 6. Browser fleet slots

`GET /api/settings/browser/slots` → `browser_governor.own_slot_snapshot()` + `stats()`, both via `asyncio.to_thread`. Returns `{max_browsers, slots}`.

`POST .../slots/kill` takes a **pid** and calls `browser_governor.kill_slot(pid)`. Verbatim:

```
# Deliberately blunt, and behind an explicit user click: the pid comes from
# the no-slot card, so the user is choosing which of THEIR chat browsers to
# sacrifice. Kills the whole process group and drops the slot so the next
# chat message can claim the capacity right away.
```

This is the backend for the "no browser slot available" card. It pairs with `MAX_BROWSERS = 2` per process (see doc 15).

***

## 7. Network bind mode

`_NETWORK_MODE_HOSTS = {"local": "127.0.0.1", "lan": "0.0.0.0"}` → `system/network_settings.json`.

`_current_bind_host()` precedence: **`SABLE_HOST` env var** → settings file → default `"0.0.0.0"`.

> [!WARNING]
> The default is **LAN-exposed**. Combined with `/api/filesystem/` being auth-exempt (doc 11), a fresh install with no `SABLE_HOST` set binds to all interfaces. The POST response does correctly say `restart_required: true` — the setting has no live effect.

***

## 8. Token refresh endpoints

Nine routes, all following "read live cookies from the provider's pinned browser → snapshot to disk → invalidate the client's in-memory jar".

| Route | Cookie(s) | Validates? |
|:--|:--|:--|
| `/deepseek/refresh-token` | client-managed | yes (client) |
| `/zai/refresh-token` | client-managed | yes (client) |
| `/gemini_web/refresh-token` | `SAPISID` / `__Secure-1PSID` / `__Secure-3PSID` | local `has_google_auth()` only |
| `/mimo/refresh-token` | `xiaomichatbot_serviceToken`, `userId`, `xiaomichatbot_ph` | **yes — live `validate_auth()` probe** |
| `/mistral_web/refresh-token` | `ory_session_*` | live probe |
| `/kimi/refresh-token` | client-managed | yes (client) |
| `/refresh-all-tokens` | all of the above | per-provider |
| `/browser/refresh-waf` | WAF `acw_tc`/`isg` + `bx-ua` | yes |

> [!TIP]
> MiMo's docstring explains a real fix:
> *"Unlike the old version, this actually probes MiMo with the token before reporting success — a stale cookie now returns a real error instead of a fake 'refreshed' toast."*

Every refresh masks its token for the response (`tok[:6]...tok[-4:]`, or `***` under 12 chars). Gemini's route reads cookies under `service._lock` — the only refresh path that takes the service lock, because it drives the shared `BrowserManager` directly.

***

## 9. Jitter & prompt-injection matrices

Both are "read config object → patch allowed fields → persist to `settings.json` → return fresh config".

`set_jitter_settings` clamps `min`/`max` at `0.0`, and **swaps them if inverted** rather than erroring:

```python
if cfg["max"] < cfg["min"]:
    cfg["min"], cfg["max"] = cfg["max"], cfg["min"]
```

`set_prompt_injection_settings` accepts static backends **and** discovered `ep:<url>` endpoint keys, rejecting anything else:

```
# reject anything else so a typo can't silently create a dead row.
```

Then it busts the connector cache so the change applies to the next turn, not the next restart:

```python
from connectors.common.instruction_builder import invalidate_cache
invalidate_cache()
```

> [!IMPORTANT]
> This is the general pattern for every toggle in this file: **persist, then invalidate the cache that read it.** Missing the invalidate is why "I changed the setting and nothing happened until I restarted" used to be a thing.

***

## 10. API key pools

Six providers with identical CRUD: `gemini`, `groq`, `mistral`, `openai`, `puter`. Pattern per provider:

- `POST .../api-key` — 400 if empty, **409 if duplicate**, else `client.add_key(key)`
- `GET .../keys` — `client.list_keys()` (already masked)
- `DELETE .../api-key/{index}` — 404 if out of range

Plus `puter/usage` (monthly allowance) and `puter/models` (image model catalog), and Cloudflare's single-credential variant (`POST/GET/DELETE .../cloudflare/credentials`) which validates the token by attempting to fetch the account ID, then deletes `.cloudflare_ai_creds.json` and resets the module singleton.

***

## 11. Provider model discovery

`GET /api/settings/providers/{provider}/models` — hand-written per provider: `gemini`, `groq`, `mistral`, `openai`. Each hits the provider's own `/models` endpoint with `httpx`, 15 s timeout, returns `{models: [{id, label}], available}`.

Filtering is provider-specific:
- **Gemini** keeps only `supportedGenerationMethods` containing `generateContent`.
- **OpenAI** skips anything containing `embedding`, `tts`, `whisper`, `dall-e`, `babbage`, `davinci`.

> [!WARNING]
> OpenAI's filter is a substring blacklist, so new non-chat families (e.g. `gpt-image-1`, `sora`) will leak into the chat model picker until someone adds the substring. It's a maintenance trap, not a bug.

Errors return `{models: [], available: True, error: "..."}` — **`available: True` even on failure**, so the UI distinguishes "no key configured" (`available: False`) from "key works but the call failed" (`available: True` + `error`). That's a deliberate three-state.

***

## 12. Custom endpoints (OpenAI-compatible bring-your-own)

Stored in `system/.custom_endpoints.json` as `[{id, name, base_url, api_key}]`, ids are `ep-<8 hex>`.

- `GET /endpoints` — lists with `api_key_masked`, `has_key`
- `POST /endpoints` — name defaults to the URL's netloc
- `DELETE /endpoints/{id}` — 404 if absent
- `GET /endpoints/{id}/models` — `_fetch_openai_models(base_url, key)`

`_fetch_openai_models` special-cases Cloudflare: if the URL contains `api.cloudflare.com` **and** `/accounts/`, it delegates to `_fetch_cloudflare_models`, because Workers AI has no `/v1/models` endpoint at all. That function extracts the account ID from the URL by regex and hits `/ai/models/search?per_page=300`, keeping only `task.name == "Text Generation"` and decorating labels with `🧠` (reasoning) / `🔧` (function_calling) badges from the model's `properties`.

> [!CRITICAL]
> **Bug found — the decorator is on the wrong function.** Lines 1367–1416:
>
> ```python
> @router.post("/api/settings/providers/custom/models")
> async def _fetch_cloudflare_models(base_url: str, api_key: str) -> dict[str, Any]:
>     ...
>
> async def fetch_custom_endpoint_models(request: Request) -> dict[str, Any]:
>     ...  # NO DECORATOR
> ```
>
> Consequences:
> 1. `POST /api/settings/providers/custom/models` is bound to `_fetch_cloudflare_models`, whose `base_url`/`api_key` parameters are **not** `Request`/`Body` — FastAPI treats them as **query parameters**. A JSON body is ignored.
> 2. `fetch_custom_endpoint_models` — the general-purpose "test any endpoint" handler that reads `await request.json()` — **has no route at all**. It is dead code.
> 3. Any non-Cloudflare URL passed to the live route fails immediately at the account-ID regex, returning `"Could not extract Cloudflare account ID from URL"`.
>
> **Not fixed** (needs your call — it's a behaviour change for whatever the UI currently sends).

***

## 13. Custom model management

`POST /api/settings/models` builds a model def: `{id, label, api_backend, api_model_type, max_session_chars (default 500_000), capabilities, thinking_modes, _custom: True}`.

Thinking modes are synthesised: `supports_thinking` true → two modes (`fast`, `high`), false → one (`fast`). An explicit `thinking_modes` array always wins.

If `endpoint_id` is given, `api_backend` is forced to `"local"` and `local_endpoint`/`local_api_key` are pulled from the saved endpoint.

`PATCH /api/settings/models/{model_id:path}` — `:path` so model ids containing slashes work. Partial merge of `capabilities` (only the four known keys), `label`, `max_session_chars`, `supports_thinking`. **Static models get persisted as custom overrides** — `add_custom_model(updated)` with `_custom: True` — so the hardcoded `MODELS` list is never mutated.

`DELETE` tries custom first, then falls back to adding the id to a **hidden-models list** (`_load_hidden_models` / `_save_hidden_models`) for static models. Always calls `delete_model_settings(model_id)` to clean orphaned cookbook settings, even if the model isn't found.

***

## 14. MCP server management

8 endpoints over `engine.mcp.manager.get_mcp_manager()`: list, add, update, remove, connect, disconnect, call, list-all-tools.

`_persist_mcp_enabled(name, enabled)` writes the `enabled` flag into `MCP_CONFIG_PATH` so a restart restores the last live state — connect/disconnect persist, and `update` persists `False` because the config change force-disconnects:

```python
await manager.disconnect_server(name)
_persist_mcp_enabled(name, False)
```

`add` returns **409 on duplicate name**, `update` merges only the four known keys (`command`, `args`, `env`, `enabled`) and 404s if absent, `call` maps `RuntimeError` → 400 and everything else → 500.

***

## 15. Accounts — the largest block

### 15.1 Two scanners, deliberately

| Endpoint | `size_mb` | Cost |
|:--|:--|:--|
| `GET /api/settings/accounts` (legacy) | **computed** via `_dir_size_mb` | walks every file per profile |
| `GET /api/settings/accounts/by-provider` | **`None`** | fast |

`GET /api/settings/accounts/profile-size?profile=X` exists purely to fill that gap one card at a time:

```
# The accounts list payload intentionally ships ``size_mb=None`` so tab open
# does not walk every file in every profile. The UI fetches this per visible
# card instead, keeping the rglob off the critical path.
```

### 15.2 `_probe_profile_auth` — one SQLite open, four answers

Probes `kimi`, `chatgpt`, `mistral_web`, `claude_scraper` for one profile. Snapshot-first (`.kimi_tokens.json`, `.mistral_web_auth.json`), then a **single** read-only SQLite connection to `Default/Cookies` (`file:...?mode=ro`, `uri=True`) answering whatever the snapshots couldn't.

Verbatim: *"Opens the Chromium Cookies SQLite DB at most ONCE per profile and runs all three cookie queries on that one connection (previously three separate connects per profile)."*

| Provider | Cookie predicate |
|:--|:--|
| chatgpt | `host_key LIKE '%chatgpt%' AND name LIKE '__Secure-next-auth.session-token%'` |
| kimi | `host_key LIKE '%kimi%' AND name IN ('access_token','refresh_token','kimi_session')` |
| mistral_web | `host_key LIKE '%mistral%' AND name LIKE 'ory_session_%'` |
| claude_scraper | `host_key LIKE '%claude.ai%' AND name = 'sessionKey'` |

> [!TIP]
> The Claude note is a genuine gotcha:
> *"Claude web auth cookie is `sessionKey` on .claude.ai (verified live). cf_clearance/hmt_id also exist but are bot cookies present BEFORE login — only sessionKey proves a real session."*

### 15.3 Two profile scans run concurrently

```python
browser_accounts, unregistered_browser_data = await asyncio.gather(
    asyncio.to_thread(_scan_browser_accounts),
    asyncio.to_thread(_scan_unregistered_browser_data),
)
```

`_scan_unregistered_browser_data` finds `browser-data-*` dirs that **don't** match `acc{N}` (e.g. `browser-data-kimi`) by name keywords or by the presence of `Default`/`Preferences`/`Cookies`/`Local Storage`/`IndexedDB` children.

### 15.4 `_browser_provider` — the shaping helper

Sorts accounts **token-holders first, then by number**, shallow-copies each dict (because the same account object appears in ~10 provider lists and each has its own active slot), flags `provider_available` per provider, and sets `pinned` on the one matching that provider's active slot.

> [!IMPORTANT]
> The docstring is the design statement:
> *"Every provider owns its OWN active account — `active` is that provider's slot (never the global/primary slot of another provider)."*
>
> A tokenless profile is still listed and still selectable: *"you pick it, then refresh to mint a token."*

### 15.5 Providers returned

**Browser-backed** (each with its own active slot): `qwen`, `zai`, `gemini_web`, `mimo`, `kimi`, `chatgpt`, `mistral_web`, `claude_scraper`, `deepseek_scraper`, `qwen_scraper`, `deepseek`.

**API-key**: `gemini`, `groq`, `mistral`, `openai`, `puter` — uniform `{type: "api_key", keys, active_index, available}`.

**Credential**: `cloudflare` — single, `{type: "credential", available}`.

`deepseek` is special: it's a browser provider *plus* a parsed JWT key list (`keys`, `active_index`, `available`) via `get_unique_tokens_with_accounts()`.

Every block is wrapped in `try/except` returning a shaped error object with `available: False` — one broken connector never 500s the whole settings tab.

***

## 16. Account switching

### 16.1 `switch_provider_account` (canonical)

`_BROWSER_PROVIDERS_ALL` = 12 names. Flow:

1. Validate provider ∈ set, validate target profile regex + existence
2. `old_dir = get_provider_browser_dir(provider)` **before** changing the slot
3. `set_provider_active_account(provider, target)`
4. Teardown: if provider is `PRIMARY_BROWSER_PROVIDER` (qwen) → `service.close()`, clear `_headers`/`_headers_account`, repoint `_browser.user_data_dir`, set `_account_override`. Otherwise `service.close_scoped(old_dir)`.
5. Invalidate the connector's cookie jar
6. Resolve new dir + email

> [!CRITICAL]
> Step 5 exists because of a real bug:
> *"Browser-auth clients (mistral_web, gemini_web, mimo) cache their jar for minutes; without this a fresh pin keeps serving the PREVIOUS account's session until the TTL expires, so the switch appears to do nothing on the backend."*

### 16.2 `switch_account` (legacy, qwen-only)

Sets the primary slot, repoints the default browser, then **defers everything else off the request path** via `_spawn_bg`:

- `_post_switch_warmup` — refresh every provider's tokens against its own active account, `invalidate_cache()`, `sync_context()`, mark/clear sync-failed
- `_strip_bg` — strip the *old* profile to reclaim disk

> [!TIP]
> The ordering comment is worth keeping:
> *"Ordering guarantees at most ONE browser launch even when both token sets are cold: warmup may open a browser, DeepSeek extraction reuses it (never closes a browser it didn't open), and persona sync is pure HTTP once headers are cached."*

`_spawn_bg` keeps a module-level `set[asyncio.Task]` with `add_done_callback(discard)` so fire-and-forget tasks aren't garbage-collected mid-flight. That's the correct pattern and it's applied consistently.

***

## 17. Account lifecycle

| Endpoint | Behaviour |
|:--|:--|
| `create` | next free `acc{N}`; `payload.providers` list opens one login tab per provider; **raw `subprocess.Popen(start_new_session=True)`, no Playwright** |
| `delete` | **auto-switches** every provider pinned to it first, then deletes, then a post-delete sweep force-clears any slot still pointing at the dead dir |
| `backup` / `restore` | `.bak` snapshot — restore **refuses** if the profile is active for any provider |
| `backup-all` / `restore-all` | restore-all **skips** active profiles |
| `rename` | display label in `accounts.json`; empty string clears it |
| `open` | full release dance (see below) |
| `clear-history` / `-all` / `-status` | Qwen chat cleanup, fire-and-forget |
| `available-browsers` | `engine.platform_paths.list_available_browsers()` |
| `profile-size` | lazy single-profile size |

### 17.1 `delete` — no more dead ends

```
# If the profile is currently pinned as the active account for one or more
# providers, those providers are auto-switched to the next available account
# FIRST (token-holders get priority), then the profile is deleted. No more
# "Cannot delete an active profile" dead-end — deletion just works.
```

`_pick_replacement` sorts candidates: not exhausted → `provider_available` first → lowest `num` → name. If there are **no** candidates it returns `""`, which unpins the provider rather than failing the delete.

> [!WARNING]
> `delete` calls `list_accounts_by_provider()` internally to build the catalog, which scans every profile and opens a read-only SQLite connection per profile. Deleting one account therefore costs a full account scan. Acceptable for a UI action, not for bulk.

### 17.2 `open_account_browser` — the one-writer rule

Five sequential steps, each addressing a distinct failure that was actually hit:

1. `service.close_profile(dir)` — releases Sable's default **and** scoped browsers on this dir
2. `ProfileHub.evict(dir)` — *"The hub owns a SEPARATE browser process that close_profile does not know about; if it survives, Chromium sees the SingletonLock and forwards the new launch to the existing (often headless) instance"*
3. `GhostSession._registry` eviction for DeepSeek ghost
4. `_kill_browsers_for_profile()` — `ps -eo pid=,args=` scan
5. `_remove_singleton_locks()` — `SingletonLock`, `SingletonCookie`, `SingletonSocket`

`_kill_browsers_for_profile` matches **exact argument tokens**, both the raw and resolved path:

```
# Exact token compare — a substring test would collide acc1 with acc11.
# it covers every Chromium-family binary (helium, chrome, chromium) —
# the old `chrom.*` pgrep never matched Sable's helium build at all;
```

> [!CRITICAL]
> The launch deliberately omits `--password-store=basic` / `--use-mock-keychain`:
> *"The engine / scraper launches this SAME profile with the real OS keyring, so its Cookies DB is keyring-encrypted. Launching with the mock store made Chromium unable to decrypt those cookies -> the user got logged out on every Open."*

That is the single most valuable comment in this file. `create_account` repeats the same note for the same reason.

***

## 18. `backup_account` / `restore_account` — defined twice

> [!CRITICAL]
> **Both routes are registered twice with identical paths.**
>
> | Path | Line | Validation | `symlinks=True` | Active guard |
> |:--|--:|:--|:--|:--|
> | `/accounts/backup` | 2953 | `_validate_profile_name` (any `browser-data-*`) | ✗ | ✗ |
> | `/accounts/backup` | **3298** | `^browser-data-acc\d+$` only | ✓ | ✗ |
> | `/accounts/restore` | 2971 | `_validate_profile_name` | ✗ | **✓ refuses if active** |
> | `/accounts/restore` | **3318** | `^browser-data-acc\d+$` only | ✓ | ✗ |
>
> FastAPI keeps the **first** registration; the second pair is unreachable dead code.
>
> Practical effect: the live routes are the **permissive** ones, which support non-`acc` dirs (`browser-data-kimi`) but do **not** preserve symlinks and — for `restore` — are the ones with the active-profile guard. If someone edits the *second* copy expecting a change, nothing happens. This also means `restore` behaves asymmetrically: guarded on the live route, unguarded in the dead one.
>
> **Not fixed** — deleting a route is a behaviour change I won't make unilaterally. Say the word and the second pair goes.

***

## 19. Data export / import

`_EXPORT_DIRS = ["system", "output", "instruction", "Brain", ".sable_backups"]` → `~/.sable/backup/`.

`system/` is copied via `_copy_dir_filtered` with `_SKIP_PATTERNS`, which is why the backup doesn't include multi-gigabyte browser profiles:

```
("browser-data", "browser-scraper-data", "automation-browser-data",
 "component_crx_cache", "extensions_crx_cache", "GPUPersistentCache",
 "GraphiteDawnCache")
```

Both endpoints return `StreamingResponse(media_type="application/x-ndjson")` — one JSON object per line: `{type: "progress", dir, step, total, status}` then `{type: "done", exported, errors}`.

> [!WARNING]
> On **import**, `system/` is merged via `_copy_dir_filtered(src, dst)` with **no skip patterns** — so restoring a backup that (somehow) contains browser data would copy it in. In practice the export filters it out, so the asymmetry is latent rather than live.

***

## 20. Context-pass & consolidation settings

Two near-identical JSON-backed preference blocks:

| File | Defaults |
|:--|:--|
| `system/context_pass_settings.json` | `{summarizer_model: "", fallback_models: []}` |
| `system/consolidation_settings.json` | `{model: "", fallback_models: []}` |

Both use a `_DEFAULTS` dict → `dict(DEFAULTS)` → `.update(stored)` → save pattern, so adding a key later is backwards-compatible. `fallback_models` is guarded with `isinstance(fm, list)` **inside** the comprehension, which is awkward but correct: a non-list becomes `[]` rather than raising.

Note the key name differs: `summarizer_model` vs `model`. Easy to get wrong.

***

## 21. TTS

### 21.1 Models

Two files from `github.com/thewh1teagle/kokoro-onnx`:

| File | Size |
|:--|--:|
| `kokoro-v1.0.onnx` | 325,525,180 |
| `voices-v1.0.bin` | 28,205,511 |

"Installed" test is `actual >= expected * 0.95` — a size heuristic, no checksum.

`download_tts_models` streams 256 KB chunks, checks `request.is_disconnected()` between chunks, writes to `.part`, renames on completion, unlinks on error.

> [!WARNING]
> `tmp` is assigned **inside** the `try` (`tmp = path.with_suffix(".part")`), and the `except` block does `if tmp.exists()`. If the failure happens before that assignment, the handler raises `NameError` instead of reporting the download error. Same pattern in `download_stt_models`. Low-probability, but it turns a clean error into a 500.

### 21.2 Prefs & synthesis

`{provider: "kokoro"|"edge", voice: "af_bella", edge_voice: "en-US-AvaMultilingualNeural", speed: 1.0}` in `system/tts_prefs.json`.

`POST /api/tts/synthesize` — 400 on empty, **400 above 5,000 chars**. Returns **WAV** for Kokoro (via `soundfile`), **MP3** for Edge.

Edge rate conversion: `rate_pct = int((speed - 1.0) * 100)` → `"+20%"` / `"-10%"`.

Kokoro is a module-global lazy singleton (`_kokoro_instance`). Edge voices are cached in `_edge_voices_cache` after the first `list_voices()` call.

***

## 22. Voice filters — the interesting part

Three guards against junk transcripts.

### 22.1 `is_whisper_hallucination`

Three independent checks:
1. **Known phrase list** — 18 YouTube-outro phrases (`"thank you for watching"`, `"smash that like button"`, `"link in the description"` …). Whisper's training data bleed.
2. **Repetition** — `unique_ratio < 0.4` (i.e. `_REPEATED_WORD_THRESHOLD = 0.6` of words are the same token), minimum 4 words.
3. **No content** — fewer than 3 alphabetic characters.

### 22.2 TTS echo cancellation

Per-session `dict[str, list[str]]` of the last 5 TTS outputs, normalised (lowercase, alphanumerics only). A transcript is echo if `SequenceMatcher(...).ratio() >= 0.75` against any of them, minimum 10 chars.

> [!TIP]
> `record_tts_output` is called from `tts_synthesize` using the `X-Session-Id` request header, and `stt_transcribe` checks the same header. So the echo guard only works when the frontend sends a stable session id on **both** calls.

### 22.3 Application order

In `stt_transcribe`: hallucination check **first**, echo check **second** (echo only if a session header exists). Either one blanks `text` and `segments` and sets `result["filtered"]` to `"hallucination"` or `"tts_echo"`.

***

## 23. STT

Model: `Systran/faster-whisper-small.en` (CTranslate2), 4 files, `model.bin` at 483,545,366 bytes. Installed test uses **0.90** (looser than TTS's 0.95).

Prefs: `{model, device: "cpu", compute_type: "int8", language: "en", beam_size: 5}`. Changing prefs resets `_whisper_model = None` so the next transcription reloads.

`POST /api/stt/transcribe`:
- 100 MB upload cap → **413**
- non-`.wav` → `ffmpeg -y -i <in> -ar 16000 -ac 1 -f wav <out>` as an async subprocess; non-zero exit → 400
- returns `{text, segments: [{start, end, text}], duration, language, language_probability}`, plus `filtered` when a filter fired
- `finally` unlinks both the temp upload and the converted wav

> [!NOTE]
> `_get_whisper_model()` reads prefs **once** at construction and passes only `device` and `compute_type` — `language` and `beam_size` are read fresh per `transcribe()` call. So changing beam size takes effect immediately, changing device requires the singleton reset (which `set_stt_prefs` does).

***

## 24. Search settings

### 24.1 Providers

`_ALLOWED_SEARCH_PROVIDERS = {searxng, brave, duckduckgo, google_pse, tavily, serper, disabled}` plus `sable`, added **lazily** only when `engine.search.sable_provider.sable_available()` returns true (the Electron bridge must be live).

> [!TIP]
> `get_search_settings` downgrades a saved `sable` selection back to `searxng` when the bridge is gone:
> *"A saved `sable` selection means nothing without the desktop shell; report searxng so the UI never shows a provider that cannot run."*

`GET /search/providers` and `_allowed_search_providers()` both respect that gate, so the UI never offers an unhonourable provider.

### 24.2 Validation

- `search_result_count` must be `1..20`
- `search_safesearch` ∈ `{strict, moderate, off}`
- API keys accept **string or list**; a list is collapsed to a single string when it has exactly one entry, and the field is **removed** entirely when the list is empty

`_read_system_settings` / `_write_system_settings` live here (line ~3928) but are used ~3,500 lines earlier by the jitter and connector routes. That works because Python resolves module globals at call time and the module is fully loaded before any request. It does mean **this file's helpers are defined after their first textual use** — don't assume import order.

`_write_system_settings` is atomic: write `.json.tmp`, then `tmp.replace(path)`.

### 24.3 Test & compare

The test endpoint runs **real** queries and reports per-engine detail for fan-out providers:

- `provider_override` given → `image_search_for_provider_traced` / `_call_provider`
- no override, image → `image_search_traced`, which **returns which provider actually answered** — *"the chain may have fallen through"*
- `provider == "sable"` → `sable_multi_search_traced`, keeping the per-engine report

Response: `{success, query, type, provider_used, result_count, elapsed_s, results, engines, error}`.

> [!IMPORTANT]
> An explicit provider returning zero image results sets `error` from `image_provider_unavailable_reason()`:
> *"An explicit provider returning nothing is usually a configuration problem, and the test button exists to diagnose exactly that."*

`compare` runs two providers via `asyncio.gather` and returns `{a, b}` side by side — each with its own `elapsed_s` and `error`.

### 24.4 Multi-key pools

`_SEARCH_KEY_MAP = {tavily, serper, brave, google_pse}` → settings keys. `_get_search_keys_list` normalises legacy single-string into a list; `_save_search_keys_list` collapses back to a single string when only one key remains. Every add/remove calls `invalidate_cache()` so the new key is used immediately.

Masking is `key[:4] + "•"*(len-8) + key[-4:]`, or `key[:2] + "•"*(len-2)` for keys ≤ 8 chars.

> [!WARNING]
> `_SEARCH_KEY_MAP` includes `brave`, but `brave_api_key` is **also** in the scalar `_SEARCH_KEY_FIELDS` handled by `update_search_settings`. Two write paths for the same setting — the multi-key endpoints and the bulk settings POST. Last writer wins, and the bulk path can silently replace a list with a single string.

***

## 25. General settings

`{max_tool_output_chars, desktop_notifications, notification_categories}`.

- `max_tool_output_chars` default **100,000**, must be `>= 1000` (400 otherwise)
- `notification_categories` is merged key-by-key against `CATEGORY_META`, so an unknown category is ignored and a missing one keeps its default

> [!NOTE]
> The default here is **100,000**, while the caps raised in `connectors/common/native_tools.py` and `engine/skills/events.py` were set to **75,000** (doc 14). The general-setting default is therefore higher than what the skill layer actually enforces. Not a conflict — the setting is a ceiling, the skill caps are lower — but the two numbers should probably agree.

***

## 26. Findings summary

> [!CRITICAL]
> **1. `@router.post("/api/settings/providers/custom/models")` is attached to `_fetch_cloudflare_models` instead of `fetch_custom_endpoint_models`.** The live route takes query params (not a JSON body) and only works for Cloudflare URLs; the general handler is unreachable. §12. **Not fixed.**

> [!CRITICAL]
> **2. `/accounts/backup` and `/accounts/restore` are each registered twice.** The second pair is dead code with different validation and different symlink/guard behaviour. §18. **Not fixed.**

> [!WARNING]
> **3. `_BROWSER_PROVIDERS` is shadowed.** Imported from `server.config` at the top, then reassigned to `("qwen",)` at line ~1990. The import is dead. Meanwhile `_active_account_names()` uses a *third* name, `engine.config.BROWSER_PROVIDERS`. Three similarly-named things, three different meanings. **Not fixed.**

> [!WARNING]
> **4. `switch_api_key` for API-key providers mutates `client._key_index` in memory only.** The DeepSeek branch persists `deepseek_preferred_token` to settings; the generic branch doesn't persist anything, so the switch is lost on restart. **Not fixed.**

> [!WARNING]
> **5. `tmp` referenced in `except` before assignment** in both `download_tts_models` and `download_stt_models` — an early failure raises `NameError` instead of a clean error message. §21.1. **Not fixed.**

> [!NOTE]
> **6. `import re` appears twice** (top of file and again before the voice filters at line ~4360). Harmless shadowing.

> [!NOTE]
> **7. Two write paths for `brave_api_key`** — bulk settings POST and the multi-key endpoints. §24.4.

No code changed in this file.

***

## 27. Cross-references

- [[07-engine-config]] — `get_provider_active_account`, `BROWSER_PROVIDERS`, `PRIMARY_BROWSER_PROVIDER`, `add_custom_model`, `MODELS`
- [[11-server]] — `dependencies.service`, `server.config`, `server.database`
- [[12-connectors]] — every `connectors.*.client.get_client()`, `invalidate_jar()`
- [[15-deep-dive-corrections]] — `browser_governor` slots, `ProfileHub`, cgroups
- [[08-engine-search]] — search providers, `invalidate_cache`, `sable_provider`
- [[10-engine-subsystems]] — `engine.mcp.manager`, `engine.notifications.policy`, `engine.platform_paths`
- [[17-server-chat-route]] — the only other router this large

> [!IMPORTANT]
> This file is the single best place to learn the *conventions*: `asyncio.to_thread` for every filesystem/subprocess call, `_spawn_bg` for fire-and-forget, shaped error objects instead of 500s, persist-then-invalidate-cache, and mask every secret in a response. Follow those five and anything you add here will fit.
