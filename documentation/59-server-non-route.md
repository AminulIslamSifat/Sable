# 59 — `server/` Non-Route Files

The process shell: app construction, auth, the SQLite layer, the PTY backends, two schedulers, and logging. Everything the 34 route files sit on top of.

| File | Lines | Role |
|:--|--:|:--|
| `api/application.py` | 470 | Lifespan, CORS, cache policy, auth middleware, 39 routers |
| `api/dependencies.py` | 60 | `ChatService` singleton + provider wiring |
| `api/pty_posix.py` / `pty_win32.py` / `pty_backend.py` | ~300 | Cross-platform PTY |
| `database/core.py` | ~330 | Thread-local connections, schema, migrations |
| `database/chats.py` | ~400 | Chats, messages, projects, checkpoints |
| `database/agents.py` | ~200 | Agent runs, messages, ops |
| `database/notes.py` | ~150 | Notes, todos, schedules |
| `auth.py` | 118 | Multi-session tokens |
| `scheduler.py` | 310 | Event-driven agent ops |
| `qwen_history_scheduler.py` | 127 | Daily 18:00 cleanup |
| `logging_setup.py` | 124 | Console + rotating file + SSE buffer |
| `bridge.py` | 239 | Identity authority client |
| `config.py` / `utils.py` / `models.py` / `app.py` / `__init__.py` | ~250 | Paths, `utcnow`, pydantic models, re-exports |

***

## 1. `application.py`

### 1.1 Lazy warmup is the best architectural decision in this batch

```
Browser warmup … used to fire the instant the server came up. That meant a
headless server with nobody at the keyboard would launch Chromium, mint
tokens, and hold a browser slot — pure waste, and it competed with real chat
browsers for the 2-slot cap.
```

The fix is `ensure_warmup_started()`, called from `POST /api/warmup` on first UI load. Check-and-set is synchronous with no `await` between the flag test and the assignment, so two tabs cannot both start it — and the comment says so. The task handle is kept on a module global *and* `app.state`, explicitly to stop GC (`"Strong reference so the task isn't silently GC'd mid-flight"`).

An API-only deployment therefore never launches a browser, and the chat path self-heals through `service._ensure_headers()`.

### 1.2 🔴 The 401 debug block logs the presented Bearer token

```python
print(f"[AUTH-DEBUG] 401 {request.method} {path} from {_origin}"
      f" header={auth_header!r} {_mark}"
      f" sessions={len(_auth_mod.VALID_TOKENS)}",
      file=_sys.stderr, flush=True)
logger.warning(
    "401 UNAUTHORIZED  %s %s  <- %s%s",
    request.method, path, _origin, _mark,
)
```

`_mark` is careful — it truncates to `_presented[:8]`. But the `print` sends the **entire** `auth_header`, including the full token, to stderr. The `logger.warning` on the next line is the sanitised version. Both fire, so every 401 writes a complete valid-or-invalid credential to the console and to systemd's journal.

A 401 is exactly the moment an attacker is probing, and it is exactly the moment the token — if it was a typo of the real one, or a rotated one still valid elsewhere — lands in a log file that `GET /api/logs` streams to the UI.

### 1.3 🔴 The env-var token cannot be revoked

`auth._load_primary()` returns the file token **or** `SABLE_TOKEN` from the environment. Every revoke path only unlinks the file:

```python
def revoke_token(token):
    VALID_TOKENS.pop(token, None)
    if token == AUTH_TOKEN:
        AUTH_TOKEN = None
        _AUTH_TOKEN_FILE.unlink(missing_ok=True)
```

When `AUTH_TOKEN` came from the environment, the unlink is a no-op and `AUTH_TOKEN = None` is in-memory only. On restart `_load_primary()` reads the env var again and the token is valid. `clear_auth_token()` has the same hole.

Anyone deploying with `SABLE_TOKEN` — the documented way to run without the setup flow — has a credential that "log out" does not revoke.

### 1.4 🟠 Bearer tokens accepted in the query string

```python
if not authorized and (
    path == "/api/logs"
    or path.endswith("/agent-events")
    or path == "/api/drive/oauth/start"
    or path.endswith("/thumb")
    or path.startswith("/api/study/cover/file/")
):
    authorized = _auth_mod.is_valid_token(request.query_params.get("token", ""))
```

Each exemption has a stated reason and each reason is real — an `<img src>` cannot set a header, and an OAuth start is a top-level navigation. But the consequence is that a **long-lived session token** travels in a URL, which is copied into uvicorn's access log, the browser history, and any `Referer` header on a subsequent navigation.

The comment acknowledges *"same read-only-GET tradeoff"*, so this is a known choice. It belongs in the ledger anyway: the mitigation for a leaked `?token=` is to revoke that session, which §1.3 says may not work.

### 1.5 Shutdown waits on a task that may be `None`

```python
_tg_bot_task = None
try:
    … if configured: _tg_bot_task = _aio.create_task(start_bot_in_background())
except …
…
if _tg_bot_task and not _tg_bot_task.done():
    _tg_bot_task.cancel()
try:
    await asyncio.wait_for(_tg_bot_task, timeout=1.0)   # None → TypeError
except Exception:
    pass
```

The `if` guards the cancel but not the await. `asyncio.wait_for(None, …)` raises `TypeError`, swallowed by the bare `except Exception`. Harmless, and it means the shutdown path takes the exception branch on every run where the bot is disabled.

### 1.6 The shutdown budget is explicit and worth keeping

Seven stages, each with its own timeout — 0.5 s for the browser, 0.5 s for the scraper, 0.5 s for MCP, 0.3 s for Telethon, 1.0 s for the bot. The header comment states the budget (`≤1s`). That is the right way to write shutdown: a bound per resource, chosen deliberately, instead of one global timeout that is always either too long or too short.

### 1.7 CORS is narrower than a wildcard and says why

```
# CORS: auth is a Bearer header (not a cookie) and credentials are disabled, so
a wildcard isn't session-riding. Still, we narrow it to loopback + private
LAN ranges (phone access) and allow extra origins via SABLE_CORS_ORIGINS
```

`allow_credentials=False` plus a Bearer header genuinely removes the session-riding risk. Narrowing anyway is defence in depth, and the regex covers RFC1918 explicitly. No finding.

***

## 2. `database/core.py`

### 2.1 The thread-local decision is documented with its failure

```
# One SQLite connection PER THREAD. A single shared handle across the FastAPI
# threadpool caused `sqlite3.OperationalError: cannot commit - no transaction
# is active`: two threads interleaved on the same connection, so one thread's
# `with conn:` __exit__ ran COMMIT while another thread had already ended the
# transaction. Thread-local handles remove the race entirely.
```

Pragmas are set once per thread: WAL, `busy_timeout=5000`, `synchronous=NORMAL`, a 64 MB page cache, 256 MB mmap, `temp_store=MEMORY`. That combination is correct for a read-heavy single-writer workload.

### 2.2 🟠 `with get_db() as conn` commits but never closes

`sqlite3.Connection` as a context manager commits or rolls back — it does **not** close. Every call site in the tree uses this pattern, so a connection lives for the lifetime of its thread and is closed only by interpreter shutdown.

For a bounded FastAPI threadpool that is fine. For `asyncio.to_thread` — which uses the default executor, whose size is `min(32, cpu+4)` — it means up to ~32 sockets plus WAL files held open indefinitely. `_raise_nofile_limit()` in `application.py` exists partly because of pressure like this.

### 2.3 The migration loop re-reads a stale column set, safely

```python
chat_cols = {row["name"] for row in conn.execute("PRAGMA table_info(chats)")}
if "memory_keys" not in chat_cols: conn.execute("ALTER TABLE chats ADD COLUMN memory_keys TEXT DEFAULT '[]'")
if "chat_url" not in chat_cols:    conn.execute("ALTER TABLE chats ADD COLUMN chat_url TEXT")
… nine more, all checked against the same stale set …
if "project_id" not in chat_cols:  conn.execute("ALTER TABLE chats ADD COLUMN project_id TEXT")
```

Every guard tests a *different* column name against a snapshot taken before any of them ran. That is correct **only because no two guards name the same column**. Add a second `ALTER` for a column already in the list and it fires a duplicate `ALTER`, which raises. The pattern is safe today and one copy-paste away from not being.

### 2.4 `migrate_skill_events_to_table` is idempotent by design

Per-message `SELECT 1 FROM skill_events WHERE message_id = ? LIMIT 1` before inserting, then a single `UPDATE messages SET skill_events = NULL` at the end if anything moved. Re-running is a no-op. Correct.

***

## 3. `auth.py` — multi-session, correctly additive

```
# Account sessions live alongside the primary install token. One install can
# hold many live sessions (many browsers / many origins) at once — logging in
# from one browser must NEVER evict another.
```

The first token becomes the primary and is written to the legacy file; every later token is purely additive to `VALID_TOKENS`. `set_auth_token` never clears. `revoke_token` removes exactly one.

The `VALID_TOKENS` entries carry `{"email": ...}` but nothing ever reads it — the value is a dict purely so the file has room to grow. Worth noting as intentional.

***

## 4. PTY backends

`pty_backend.py` is a 40-line dispatcher: `pick_shell()` honours `SABLE_TERM_SHELL` before falling back to `engine.platform_paths.pick_shell`, and the `PtySession` name binds to the right class at import time.

### 4.1 The two implementations have a real interface difference

| | POSIX | Windows |
|:--|:--|:--|
| `read(fd, n)` | `os.read` on master fd | reads the ConPTY handle, `fd` unused |
| `write(fd, data)` | `os.write` | `pty.write(text)` after UTF-8 decode |
| `send_winch()` | `killpg(SIGWINCH)` | no-op (ConPTY resizes itself) |
| `resize()` | `TIOCSWINSZ` ioctl | `set_size(cols, rows)` — **transposed** |

The Windows `resize` swaps argument order relative to its own signature, and the comment records it (`pywinpty takes (cols, rows)`). Correct, and the kind of thing that silently produces a 80×24 shell for everyone if missed.

### 4.2 🟡 `pty_posix.stop` mixes group and single-pid kills

```python
os.killpg(os.getpgid(pid), signal.SIGTERM)   # group
await asyncio.sleep(0.1)
os.kill(pid, signal.SIGKILL)                  # single pid
os.waitpid(pid, os.WNOHANG)
```

SIGTERM goes to the group, SIGKILL goes to the leader only. A shell that forked a stubborn child survives the SIGKILL. `kill_process_tree` (doc 56 §3.4) already solves this correctly, with the zombie-ordering comment — the PTY backend does not use it.

`waitpid(pid, WNOHANG)` immediately after SIGKILL almost never reaps, because the kernel has not scheduled the exit yet. The result is a zombie until the next `wait`.

***

## 5. The two schedulers

### 5.1 `qwen_history_scheduler` sleeps up to 24 hours in one `await`

```python
next_fire = _next_fire_time()
delay = max(0, (next_fire - now).total_seconds())
await asyncio.sleep(delay)
```

A single sleep of up to 86,400 s. `asyncio.sleep` on a monotonic clock survives wall-clock jumps for *elapsed* time but the target was computed from `datetime.now().astimezone()`, so a DST transition shifts the fire time by an hour for that run. Fires at 18:00 local minus the offset change. Minor for a cleanup job, and the `await asyncio.sleep(60)` buffer afterwards prevents a double-fire.

`_needs_clear` handles naive timestamps from an older `accounts.json` by assuming UTC, which is the correct assumption for what `qwen_cleanup.update_last_cleared` writes.

### 5.2 `scheduler.py` — one task per op, no polling

```
Each enabled agent op gets its own asyncio.Task that sleeps until next_run,
fires the agent, computes the next occurrence, and reschedules itself.
No polling. No wasted cycles.
```

`_scheduled_tasks: dict[str, asyncio.Task]` is module-level and keyed by op id. Nothing removes a completed entry, so deleting an op leaves its task handle in the dict forever — the same unbounded-module-dict shape as #226, though bounded here by the number of ops ever created.

**This is the file that reaches `engine/agents/loop.py`** — the deprecated shim that posts to `127.0.0.1:8080` while Sable runs on 61770 (ledger #143). Every scheduled agent op therefore fails with `ConnectError`. That finding stands; this file is the caller that makes it live.

***

## 6. `logging_setup.py`

The `sable` logger gets three handlers — console, `RotatingFileHandler(5 MB × 3)`, and `SSELogHandler` pushing into an `asyncio.Queue(maxsize=500)`. `propagate = False` prevents double-emit through root.

The SSE handler is the careful part:

```python
try:
    _log_buffer.put_nowait(self.format(record))
except asyncio.QueueFull:
    pass  # UI is slow — drop rather than block the event loop
except Exception:
    self.handleError(record)
```

Dropping under backpressure instead of blocking. A logging call must never stall the loop it is logging about.

Fourteen third-party loggers are forced to `WARNING`, including `uvicorn.access` — which is what makes §1.4's `?token=` leak land in the *app* log rather than the access log. The exemption helped.

***

## 7. Findings

| # | Where | What |
|--:|:--|:--|
| 232 | `application.py` auth middleware | **🔴** The `[AUTH-DEBUG]` `print` writes the **complete** `Authorization` header — including the full Bearer token — to stderr on every 401. The sanitised `logger.warning` on the next line is the one that should have been alone. |
| 233 | `auth.revoke_token` / `clear_auth_token` | **🔴** A token supplied via `SABLE_TOKEN` cannot be revoked: every revoke path unlinks `_AUTH_TOKEN_FILE` and clears memory, and `_load_primary()` restores it from the environment on restart. |
| 234 | `application.py` auth exemptions | Bearer tokens accepted via `?token=` for `/thumb`, `/api/study/cover/file/`, `/api/logs`, `/api/drive/oauth/start`, `/agent-events`. Each reason is valid; the cost is a session token in URLs, access logs, history and `Referer`. |
| 235 | `database/core.get_db` | `with get_db() as conn` commits but never closes. One connection per worker thread, held for process lifetime — up to ~32 in the default executor, each with WAL/SHM handles. |
| 236 | `application.py` shutdown | `await asyncio.wait_for(_tg_bot_task, timeout=1.0)` is unguarded for `None`; raises `TypeError` on every run where the bot is disabled and is swallowed by `except Exception`. |
| 237 | `api/pty_posix.PosixPtySession.stop` | SIGTERM goes to the process **group**, SIGKILL to the **leader only**, so a forked grandchild survives. `engine.process_utils.kill_process_tree` solves this correctly and is not used. |
| 238 | `scheduler._scheduled_tasks` | Module-level dict keyed by op id, entries never removed. Bounded by ops-ever-created, same shape as #226. |
| 239 | `database/core.init_db` | Eleven `ALTER TABLE` guards all test a column-name snapshot taken before any of them ran. Correct only because no two guards name the same column. |

***

## 8. What the server shell shows

- **Auth is the weakest surface in the tree.** #232 and #233 are both in `auth`, both credential-handling, and both are single lines. The multi-session design in `auth.py` is sound; the leak is in the *diagnostics* and the *env-var path*, not the model.
- **The lifespan/shutdown choreography is the strongest.** Explicit per-resource budgets, a documented lazy-warmup rationale, strong task references, and a Telegram preflight that purges a dead MTProto session rather than retrying it forever.
- **`logging_setup` and `application` were written by someone who knew a log line must not block the loop** — drop-on-full, `put_nowait`, `propagate=False`. #232 is a regression against that same instinct: the `print` bypasses the logger entirely.
- **Two of eight findings are a correct helper not being used** — `kill_process_tree` (#237) and the sanitised `_mark` already computed one line earlier (#232). That is the same shape as `pid_exists` in #210.

Python is now **~122,200 / 137,031 ≈ 89%**; overall **~63%**.

> [!WARNING] Still unread
> `server/database/chats.py` and `database/agents.py` were partially visible; `server/config.py`, `server/utils.py`, `server/browser_reaper.py`, `bridge.py`'s body and most of `scheduler.py` were not. `engine/skills/handlers/` (18 modules, 5,401 lines) was read in doc 29 at contract level only. `skills/document_skills/` remainder (~6,256) and `tools/` remainder (~2,654) are untouched.
