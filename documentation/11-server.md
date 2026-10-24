# `server/` — FastAPI Application

> [!INFO] Scope
> **32,854 lines across 67 files.** This is the HTTP layer: app assembly, auth, lifespan, SQLite schema, and 40+ routers. Route *internals* are documented by endpoint surface, not line-by-line — `chat.py` alone is 4,404 lines.

***

## Import chain

```
server.py (root)
  -> from server import app
       server/__init__.py
         -> from .app import app
              server/app.py
                -> from .api.application import app
                     -> FastAPI(title="Sable API", version="0.4.0", lifespan=lifespan)
```

Three one-line files exist purely so both `from server import app` and `from server.app import app` work.

***

## The 40+ routers

`application.py` imports and mounts every router explicitly — no autodiscovery.

| Group | Routers |
|:--|:--|
| **Core chat** | `chat`, `chats`, `chat_export`, `image_stream` |
| **Memory** | `memory`, `personas`, `tracknote` |
| **Study** | `library`, `study`, `study_ai`, `study_notes`, `study_srs`, `study_dict`, `study_humanize`, `study_cover`, `rooms` |
| **Providers** | `scraper`, `deepseek`, `account`, `cookbook`, `ocr`, `ocr_local` |
| **Integrations** | `drive`, `email`, `telegram`, `telegram_bot`, `terminal`, `filesystem` |
| **System** | `settings`, `misc`, `processes`, `dashboard`, `update`, `setup`, `auth` |
| **Agents/research** | `agents`, `research`, `checkpoints` |
| **Search / upload** | `search_render`, `upload` |

Largest by line count:

| Router | Lines |
|:--|--:|
| `settings.py` | **4,742** |
| `chat.py` | **4,404** |
| `study.py` | 2,340 |
| `study_ai.py` | 1,641 |
| `memory.py` | 1,233 |
| `chats.py` | 1,196 |
| `drive.py` | 1,022 |
| `filesystem.py` | 947 |
| `misc.py` | 927 |

> [!NOTE] `settings.py` is the biggest router, not `chat.py`
> 4,742 lines of settings CRUD across every subsystem — providers, models, accounts, notifications, TTS, memory search, network. It is a single file because everything is settings; splitting it is the highest-value refactor in `server/`.

***

## `application.py` — lifespan

### Startup order

```
_raise_nofile_limit()
  -> agent runtime._loop = running loop
  -> MCP auto-connect (background task)
  -> init_db()
  -> migrate_skill_events_to_table()
  -> recover_stale_agents()
  -> telegram session preflight (background)
  -> opencode-free model sync (background)
  -> load agent_config.json -> runtime + registry
  -> load memory_search_settings.json -> searcher
  -> start_scheduler()
  -> start_qwen_history_scheduler()
  -> start_browser_reaper()
  -> auto-start Telegram Bot if configured
```

> [!IMPORTANT] Warmup is deliberately NOT in startup
> The comment: *"Browser warmup (Qwen WAF + provider token refresh + context sync) used to fire the instant the server came up. That meant a headless server with nobody at the keyboard would launch Chromium, mint tokens, and hold a browser slot — pure waste, and it competed with real chat browsers for the 2-slot cap."*
>
> It is now **lazy**: `POST /api/warmup` from the first UI load calls `ensure_warmup_started()`. `_warmup_started` is checked-and-set **synchronously** (no await between), so two tabs loading at once cannot both spawn a warmup. A tab refresh is a no-op. An API-only deployment never pays for it at all — the chat path self-heals through `_ensure_headers()`.

### `_raise_nofile_limit()`

```python
soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
target = min(65536, hard)
if soft < target:
    resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
```

> [!TIP] Why 65,536 file descriptors
> Each Playwright browser opens hundreds of sockets; agents multiply that; SSE streams hold one each. The default 1,024 soft limit is exhausted fast. `ImportError` is caught for Windows (no `resource` module).

### Shutdown — a 1-second budget

Every step is wrapped in `asyncio.wait_for` with an explicit timeout:

| Step | Timeout |
|:--|--:|
| Cancel lazy warmup | — |
| Cancel all agent tasks | — |
| `cancel_all()` scheduler | — |
| `service.close()` (main browser) | **0.5 s** |
| `scraper.stop(kill_browser=True)` | **0.5 s** |
| `get_mcp_manager().shutdown()` | **0.5 s** |
| `disconnect_client()` (Telethon) | **0.3 s** |
| Cancel Telegram Bot task | 1.0 s |

> [!IMPORTANT] `server.py` has a 0.8 s SIGTERM deadman on top of this
> If this entire shutdown still has not finished, `os._exit(1)`. Two layers of "never hang on shutdown" — because a stuck Chromium close would otherwise block a systemd restart indefinitely.

### Background tasks held on `app.state`

```python
app.state._mcp_startup_task
app.state._tg_preflight_task
app.state._oc_free_task
```

> [!NOTE] Strong references are mandatory
> *"Keep a strong reference to prevent silent GC"* — `asyncio.create_task()` returns a weak reference; a task with no other referent can be garbage-collected mid-flight. Every fire-and-forget task is stored on `app.state` for exactly this reason.

### Telegram session preflight

> *"A poisoned auth key (AUTH_KEY_DUPLICATED / revoked) can never be retried — probe once at boot and purge the dead .session so the user only has to re-sign-in, never manually delete files."*

`purge_dead_session_on_startup()` runs in the background; a failure is logged at `debug` and never blocks startup.

***

## Middleware — three layers, order matters

FastAPI applies middleware in **reverse** registration order for requests. Registered order here is CORS → cache_policy → auth, so the actual request order is:

```
request -> auth_middleware -> cache_policy -> CORSMiddleware -> route
```

### 1. CORS

```python
_LOCAL_ORIGIN_RE = (
    r"https?://(localhost|127\.0\.0\.1|0\.0\.0\.0"
    r"|10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})"
    r"(?::\d+)?"
)
allow_credentials=False
```

> [!IMPORTANT] Private LAN ranges are allowed on purpose
> The comment: *"auth is a Bearer header (not a cookie) and credentials are disabled, so a wildcard isn't session-riding. Still, we narrow it to loopback + private LAN ranges (phone access)."* A phone on the same WiFi can reach Sable at `192.168.x.x:61770` without a tunnel.
>
> `allow_credentials=False` is what makes this safe — there is no cookie to ride. If that ever flips to `True`, this regex becomes a CSRF hole.

`SABLE_CORS_ORIGINS` (comma-separated) adds origins for tunnels and reverse proxies.

### 2. `cache_policy`

```python
if path in ("/", "/index.html"):        "no-cache"
elif path.startswith("/static/"):         "no-cache, must-revalidate"
```

> [!TIP] This replaced aggressive caching, and the comment explains why
> *"Without this, browsers re-fetch ~40+ assets on every reload, causing intermittent load failures under server load (startup, MCP connect, etc.)"* — that was the old behaviour with no cache header at all. Now assets carry `no-cache, must-revalidate`, so ETag and Last-Modified still return 304 when unchanged. No redundant bytes, no stale files during development.

### 3. `auth_middleware` — the interesting one

```python
if not path.startswith("/api/") or any(path.startswith(p) for p in AUTH_EXEMPT_PREFIXES):
    return await call_next(request)
```

`AUTH_EXEMPT_PREFIXES` (from `server/config.py`):

```
/api/login  /api/health  /api/env  /api/filesystem/  /api/setup/
/static/  /system/uploads/  /assets/
/api/settings/accounts/create
/api/account/config  /api/account/register  /api/account/login  /api/account/google
/api/drive/oauth/callback
```

> [!WARNING] `/api/filesystem/` is exempt — read that carefully
> It is in the exempt list, meaning filesystem reads need no Bearer token. That is a deliberate choice for a single-user local app (the UI needs to browse files before login completes), but on a LAN-exposed instance it means anyone on the network can read files through that endpoint. Worth knowing before enabling LAN access.

#### The `?token=` escape hatch

Six paths fall back to reading the token from the query string, each with a documented reason:

| Path | Why |
|:--|:--|
| `/api/logs` | Browser `EventSource` cannot set headers |
| `*/agent-events` | Same — SSE |
| `/api/research/events/` | Same — SSE |
| `/api/drive/oauth/start` | *"a full-page browser navigation, so it can't carry a Bearer header"* |
| `*/thumb` | *"renders inside a plain `<img src>`, which can't set an Authorization header"* |
| `/api/study/cover/file/` | *"fetched by a plain `<a download>` / window navigation"* |

> [!NOTE] The OAuth callback is exempt for a different reason
> From `server/config.py`: *"Google redirects here with no Authorization header. Safe to exempt because the `state` param is a server-only single-use nonce: an attacker can only reach it with a bogus state and get 'session expired'."*

#### The 401 diagnostic

```python
_mark = f" (presented={_presented[:8]}… len={len(_presented)}"
        f" sessions={len(_auth_mod.VALID_TOKENS)}"
        f" primary={'set' if _auth_mod.AUTH_TOKEN else 'none'}"
        f" valid={_auth_mod.is_valid_token(_presented)})"
logger.warning("401 UNAUTHORIZED  %s %s  <- %s%s", request.method, path, _origin, _mark)
```

> [!TIP] This log line exists because auth failures are otherwise opaque
> It prints the first 8 chars of the presented token, its length, how many sessions exist, whether a primary token is set, and whether the token validated. That is exactly enough to distinguish "no header sent", "wrong token", and "session store empty" without ever logging the full secret.

***

## `dependencies.py` — the wiring point

```python
service = ChatService(user_data_dir=str(get_browser_data_dir()))

get_deepseek_client().set_token_refresher(service.refresh_deepseek_token)
get_zai_client().set_token_refresher(service.refresh_zai_token)
get_zai_client().set_page_provider(service.get_zai_page)
get_gemini_web_client().set_cookie_provider(service.get_gemini_cookies)
get_gemini_web_client().set_token_provider(service.get_gemini_at_token)
get_mimo_client().set_cookie_provider(service.get_mimo_cookies)
get_mistral_web_client().set_cookie_provider(service.get_mistral_web_cookies)
```

> [!IMPORTANT] This is the inversion-of-control seam
> Connectors live in `connectors/` and must not import `engine.service` (circular import). Instead they expose `set_token_refresher()` / `set_cookie_provider()` and `dependencies.py` injects `ChatService`'s methods at startup. Every provider's auth therefore routes through the same browser manager.

A second block injects an **account resolver** so error messages carry `[provider | account]` context:

```python
def _provider_account(provider: str) -> str:
    """Resolve the account a provider is really using (pin wins over global)."""
    pinned = get_provider_account(provider)
    if pinned:
        return pinned
    return _resolve_active_account()
```

Only injected on clients that expose `.ctx` — `mistral_web`, `mimo`, `gemini_web`.

```python
def sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
```

The whole SSE framing format in two lines. `ensure_ascii=False` matters — Bengali and emoji pass through unescaped.

***

## `auth.py` — multi-session tokens

```python
_SESSIONS_FILE = _AUTH_TOKEN_FILE.parent / ".sessions.json"

AUTH_TOKEN: str | None = _load_primary()
VALID_TOKENS: dict[str, dict] = _load_sessions()
```

> [!IMPORTANT] One install, many sessions
> *"One install can hold many live sessions (many browsers / many origins) at once — logging in from one browser must NEVER evict another."* That is why `VALID_TOKENS` is a dict (session id to metadata) rather than a single token, and why `is_valid_token()` checks the primary **or** any session.

`_load_primary()` reads `system/.auth_token`, falling back to `$SABLE_TOKEN`. Returns `None` when neither is set — *"the setup flow will force the user to create a password before anything else works."*

`has_any_token()` gates the setup flow: no credential at all means the app must run onboarding.

`_persist_sessions()` writes `.sessions.json` and swallows `OSError` — a read-only filesystem means in-memory sessions only, not a crash.

***

## `config.py`

Re-exports from `engine.config` (one source of truth) plus server-local paths:

```python
WEB_DIR      = BASE_DIR / "web"
INDEX_FILE   = WEB_DIR / "index.html"
DB_PATH      = BASE_DIR / "system/sable.db"
UPLOAD_DIR   = BASE_DIR / "system/uploads"
_AUTH_TOKEN_FILE = BASE_DIR / "system/.auth_token"
```

### Static mounts (in `application.py`)

| Mount | Directory |
|:--|:--|
| `/static` | `web/` |
| `/system/uploads` | `system/uploads/` |
| `/assets` | `OUTPUT_ROOT/assets/` (created if missing) |

### Frontend animation constants

```python
TYPEWRITER_CHARS_PER_TICK = 3
TYPEWRITER_TICK_MS        = 12
```

3 chars every 12 ms is about 250 chars/sec. Server-side constants for a client-side effect — the frontend reads them from `/api/env`.

### Retry / thresholds

```python
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.0
SKILL_ROUND_WARN_THRESHOLD = 15
```

> `SKILL_ROUND_WARN_THRESHOLD = 15` — after 15 skill rounds in one turn the UI warns. Pairs with `resilience.py`'s caps.

### `_BROWSER_PROFILES`

```python
_BROWSER_PROFILES = {
    "api":        (BROWSER_DATA_DIR,            ... .bak),
    "scraper":    (BROWSER_SCRAPER_DATA_DIR,    ... .bak),
    "automation": (BROWSER_AUTOMATION_DATA_DIR, ... .bak),
}
```

Three profile *classes*, each with a backup directory. Distinct from the per-account `browser-data-accN` profiles — these are per-purpose.

***

## `database/` — SQLite schema

`system/sable.db`, **243 MB** on this machine.

### Pragmas

```python
PRAGMA journal_mode=WAL
PRAGMA busy_timeout=5000
PRAGMA synchronous=NORMAL
PRAGMA cache_size=-64000        # 64 MB page cache
PRAGMA mmap_size=268435456      # 256 MB mmap
PRAGMA temp_store=MEMORY
```

> [!TIP] WAL plus `busy_timeout=5000` is what makes concurrent reads work
> The server writes while SSE streams read. WAL lets readers proceed during a write; the 5 s busy timeout absorbs write contention instead of raising `database is locked`. `synchronous=NORMAL` is the standard WAL trade-off — durable enough, much faster than `FULL`.

### Tables

| Table | Purpose | Key indexes |
|:--|:--|:--|
| `chats` | Chat metadata, model settings, project, pin, mode | `idx_chats_updated(updated_at DESC)` |
| `messages` | Message rows | `idx_messages_chat(chat_id, id)`, `idx_messages_chat_role(chat_id, role, id)` |
| `skill_events` | Per-message tool events | `idx_skill_events_msg(message_id, seq)` |
| `response_stats` | Model speed and token stats | `idx_stats_model(model, id DESC)` |
| `agent_runs` | Sub-agent lifecycle | `idx_agent_runs_status`, `idx_agent_runs_chat` |
| `agent_messages` | Sub-agent transcripts | `idx_agent_messages_agent` |
| `notes` | Notes and checklists | `idx_notes_type`, `idx_notes_archived` |
| `schedules` | Recurring schedules | `idx_schedules_type` |
| `agent_ops` | Autonomous agent operations | `idx_agent_ops_enabled`, `idx_agent_ops_next_run` |
| `checkpoints` | Shadow-git snapshots | `idx_checkpoints_chat(chat_id, message_id)` |
| `projects` | Project grouping | — |

> [!NOTE] `migrate_skill_events_to_table()` is a real migration
> Skill events used to live in a column on `messages`. They now have a dedicated table with a `seq` ordering column, and `init_db()` runs the migration once. `recover_stale_agents()` runs right after — agents left `RUNNING` by a crash get marked failed so they do not appear live forever.

### Submodules

| File | Lines | Exports |
|:--|--:|:--|
| `chats.py` | 648 | 38 functions — chats, messages, projects, checkpoints |
| `core.py` | 355 | `get_db`, `init_db`, `migrate_skill_events_to_table` |
| `notes.py` | 158 | Notes and schedules CRUD |
| `agents.py` | 148 | Agent runs, messages, ops |
| `__init__.py` | 106 | **Re-exports everything** |

> [!TIP] `__init__.py` re-exports all 60+ names
> *"All public names are re-exported here so existing imports like `from server.database import ensure_chat` continue to work unchanged."* The split into submodules was done without touching a single call site.

***

## `/api/chat` — the main endpoint

`chat.py` is 4,404 lines with surprisingly few routes:

| Endpoint | Line | Purpose |
|:--|--:|:--|
| `POST /api/chat` | **901** | The main streaming turn — roughly 3,300 lines of handler |
| `POST /api/chat/queue` | 209 | Queue a message |
| `POST /api/chat/stop` | 232 | Stop generation |
| `GET /api/skills/pending/{chat_id}` | 4198 | Pending permission requests |
| `POST /api/skills/approve/{tag_id}` | 4232 | Approve a tool call |
| `POST /api/skills/deny/{tag_id}` | 4277 | Deny |
| `POST /api/skills/cwd-approve/{tag_id}` | 4304 | Approve an out-of-cwd command |
| `POST /api/skills/batch-complete/{chat_id}` | 4349 | Bulk resolve |
| `POST /api/skills/cwd-deny/{tag_id}` | 4381 | Deny |

> [!IMPORTANT] One handler holds the whole agentic loop
> `POST /api/chat` spans lines 901 to 4198. It contains the turn loop, skill execution, memory injection, agent spawning, auto-switching, and SSE emission. That is the single largest function in the codebase and the highest-risk thing to modify.

***

## Gotchas

> [!WARNING] Six things that matter
> 1. **`/api/filesystem/` is auth-exempt.** Fine for localhost, a LAN data-exposure risk. Check before exposing.
> 2. **Middleware order is the reverse of registration.** Auth runs first even though it is registered last.
> 3. **`allow_credentials=False` is what makes the LAN CORS regex safe.** Flipping it to `True` creates a CSRF hole.
> 4. **Warmup is lazy by design.** A headless server launches zero browsers until the first UI load.
> 5. **Shutdown has a 1 s budget, and `server.py` has a 0.8 s deadman above it.** Never assume a graceful close completes.
> 6. **`POST /api/chat` is roughly 3,300 lines.** Read it in slices; do not trust a grep result to tell you what it does.

