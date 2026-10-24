# 55 — `engine/` Root: Platform, Tools, Checkpoints, Chat

16 of the 20 small `engine/*.py` files, read end to end — 2,750 lines. These are the files everything else imports and nobody documents: path resolution, tool discovery, shadow-git checkpoints, the Qwen CLI, and eleven single-purpose helpers.

Still unread from the root: `wsl_browser.py` (301), `cookie_reader.py` (275), `think_parser.py` (159), `process_utils.py` (124).

***

## 1. `platform_paths.py` — the best comment in the codebase

### 1.1 The keyring bug, stated once and definitively

```
A profile is only readable by the launcher that wrote it when the
cookie-encryption backend matches: Playwright's default is
`--password-store=basic --use-mock-keychain` (the `peanuts` key), while a raw
`chrome --user-data-dir=...` launch uses the REAL OS keyring. Cookies written
by one are undecryptable by the other, so the SPA never authenticates, no
`localStorage.token` appears, and every API call 401s. That is the bug.
```

`BROWSER_IGNORE_DEFAULT_ARGS` exists to drop Playwright's two mock-keychain flags, and every launcher is required to pass it. `shared_browser_args()` bundles it with the throttle flags and the automation flags.

> [!IMPORTANT] One launcher was never converted
> `account_login.py` builds its own `launch_kwargs` and omits `ignore_default_args`. See #201 — the two login tools write mutually unreadable cookie stores.

### 1.2 Throttle flags are about the ghost, not performance

```
Throttling flags: a ghost tab is never focused, so Chromium throttles its
timers to 1s (1min when hidden). That stretched the SPA's proof-of-work +
fetch dispatch to 60-120s BEFORE the request left the page.
```

Four flags, one purpose. A ghost tab that is never focused is throttled hard enough that the *upstream's own anti-bot computation* takes two minutes. This is the least obvious dependency in the browser stack.

### 1.3 UA derivation reads the binary, not a constant

`get_browser_chromium_version` shells out to `<binary> --version` and prefers the version following `Chromium|Chrome` rather than the first version-like string — because Helium prints `Helium 0.17.0.1 (Chromium 153.0.8010.36)` and the Helium version is not the Chromium one. Cached per path in `_UA_VERSION_CACHE`.

`derive_ua_fingerprint` then collapses to `<major>.0.0.0`, matching Chrome 110+ UA reduction. `sec-ch-ua` carries the same major plus a fixed `"Not)A;Brand";v="24"`.

> [!NOTE] The GREASE brand is frozen
> Real Chrome rotates the `Not)A;Brand` value per major. This hardcodes `24` while the Chromium major moves. A fingerprint check that reads the brand list would see an impossible pairing.

### 1.4 The fingerprint is derived but never sent as a pair

`get_account_ua_fingerprint` returns `(user_agent, sec_ch_ua)`. Grep shows `derive_ua_fingerprint` called from `get_account_ua_fingerprint` and nowhere else. Whatever consumes it must be the curl path — which is the path the ghost exists to avoid.

***

## 2. `tools_loader.py` — core/outer tiers

### 2.1 The default is the cheap direction

```
A missing manifest defaults to "outer" — the CHEAP direction — so
accidentally adding a tool can never silently bloat every prompt with a
full schema.
```

Core tools get full schemas; outer tools get `name + description` plus a stub marked `[OUTER: use load_tool to activate]`, and `load_tool`'s own schema is built dynamically with an `enum` of exactly the available outer tool names.

`_ALWAYS_CORE` overrides the manifest for six keys — `execute_command`, `code_editor`, `grep_search`, `ask_user`, `chat_title`, `file_uploader` — so a bad manifest cannot hide the escape hatches.

### 2.2 Discovery is cached twice and both must be cleared

```python
reload_tools():
    _discover_cached.cache_clear()
    get_known_tags.cache_clear()   # parser has its own name set
```

The second clear has a comment explaining the failure: without it a newly added tool stays invisible to the parser and to the resilience repair regex.

### 2.3 🔴-adjacent: a mode-gated tool is invisible by default

```python
def _mode_allows(t, mode):
    if not t.modes:
        return True
    if mode is None:
        return False
    return mode in t.modes
```

`get_all_tool_schemas(mode=None)` is the default. So any tool whose `manifest.json` declares `"modes": ["study"]` — or `["agent"]`, or anything — **vanishes** from every caller that does not pass a mode. Not an error, not a warning: the tool simply is not offered.

The comment above `modes` says *"A tool that declares ["study"] is offered only in study mode"*, which is accurate. What is not stated is that the default argument makes non-mode callers blind. `get_study_tool_schemas` passes `mode="study"` correctly; any future caller that forgets loses every gated tool silently. See #205.

### 2.4 `_read_manifest` was two passes, now one

The docstring records the fix: tier and modes used to be read by two separate helpers, each re-reading and re-parsing the same file, doubling cold-cache I/O across the whole `tools/` tree.

***

## 3. `checkpoint.py` — shadow git

### 3.1 `--bare` because non-bare nests

```python
# --bare keeps git_dir as the repo root. A non-bare `git init <path>/.git`
nests the real repo one level deeper (…/.git/.git) while the code still
passes --git-dir=…/.git, so every command fails.
```

### 3.2 `_heal_repo` is the most interesting 40 lines in the file

An interrupted git write leaves a **zero-byte loose object**. If that object is the one `HEAD` points at, every later commit fails with `could not parse HEAD` and the entire checkpoint history for that project is permanently dead. The heal does two things at construction time:

1. Sweep every zero-byte file under `objects/`.
2. If `HEAD` no longer resolves, walk the reflog back to the newest surviving commit and rewind.

> [!TIP] The comment that stops you writing the wrong check
> ```
> NOTE: `rev-parse --verify HEAD` is NOT sufficient here — it only resolves
> the ref *name* to a SHA and succeeds even when the object itself has been
> deleted. `cat-file -e HEAD^{commit}` actually verifies the object exists.
> ```
> This is the single most useful line in the file. Without it the obvious check passes and the repair never runs.

### 3.3 Deliberately no `setsid`

```
Deliberately does NOT isolate the process group (no os.setsid). git has no
children worth tree-killing, and setsid stops subprocess.run()'s timeout
from reliably reaping the child — an interrupted git write leaves a
zero-byte loose object behind, which bricks the whole shadow repo.
```

The absence of a call is documented with the reason. That is why §3.2 exists at all — the two comments are one decision.

### 3.4 `timeout=30` is uncaught

`_git` passes `timeout=30` to `subprocess.run`, which raises `TimeoutExpired` on expiry. Nothing in `_git`, `_ensure_shadow_repo` or `_heal_repo` catches it, so a hung git during construction propagates out of `CheckpointManager.__init__`. See #207.

***

## 4. `chat.py` — the Qwen CLI

### 4.1 Ghost first, curl fallback

```python
if not is_retry and not files and qwen_ghost_enabled():
    try:
        return await ghost_stream_chat(...)
    except GhostError as e:
        await GhostEngine.instance().shutdown()
        print(f"[GHOST] → falling back to raw API path: {e}")
    except Exception as e:
        print(f"[GHOST] unexpected failure …")
```

Two problems, both real:

**`not files`** — every turn with an attachment skips the ghost entirely and goes to the raw curl path. That path is the one `engine/inpage.py` and `platform_paths.py` exist to avoid, because it is the fingerprinted one that gets accounts banned. No comment explains the exclusion. Image uploads are the `bm.upload_image` route (§4.3), which is a different, also-curl flow.

**No shutdown on the generic branch** — `GhostError` shuts the engine down; any other exception does not. A ghost left in a broken state is reused next turn, fails the same way, and falls back again — forever, with only a `print` to show for it.

### 4.2 Non-200 returns success

```python
if res.status_code != 200:
    raw = (await res.content).decode(errors="replace")[:500]
    print(f"[ERROR] {raw}")
    return new_chat_id, new_parent_id
```

`stream_chat`'s contract is `(chat_id, parent_id)`. An HTTP 500 returns the *same tuple* a successful empty answer returns. The caller cannot distinguish a failure from an empty stream.

### 4.3 The incremental decoder is correct and matters

```python
# Incremental decoder so multi-byte UTF-8 chars split across chunk boundaries
# (Bengali, emoji) aren't corrupted into '?'.
decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
```

Every other SSE reader in the tree does `buffer += chunk.decode("utf-8", errors="replace")` per chunk, which corrupts a multi-byte character split across two reads. This is the only one that gets it right — and it is the oldest file.

### 4.4 Raw logging is opt-in and says why

```
These write to disk on EVERY SSE chunk when enabled, which is extremely
expensive during a live stream. They're diagnostic-only, so they are OFF
unless SABLE_RAW_LOG=1.
```

Third instance of the unbounded-log pattern (#137, #169, #184) — but the only one that gates itself. That is the difference.

***

## 5. The eleven helpers

| File | Lines | What it does | Note |
|:--|--:|:--|:--|
| `filelock.py` | 56 | `flock` POSIX / `msvcrt.locking` Windows | The primitive `browser_governor` and `study/store` rely on |
| `payloads.py` | 76 | Qwen request body | Sets both `parentId`/`parent_id` and `chatId`/`chat_id` deliberately, to match the SPA's fingerprint |
| `token_counter.py` | 105 | char/4 estimate | Honest docstring: *"accurate to ~10-15%"* |
| `service_manager.py` | 169 | systemctl / schtasks | See #206 |
| `browser_opener.py` | 149 | Interactive login, fixed CDP port 9222 | Passes the keyring flag correctly |
| `browser_launch.py` | 68 | Plain `<binary> --user-data-dir=… <urls>` | No Playwright at all, by design — *"the same command a user would type"* |
| `account_login.py` | 89 | Minimal login, WSL2-aware | **Omits the keyring flag — #201** |
| `qwen_cleanup.py` | 129 | `DELETE /api/v2/chats/` per profile | See #208 |
| `mimic.py` | 87 | Replays the SPA's send burst | Opt-in via env, default off, never raises. See #204 |
| `telegram_session.py` | 78 | Detects dead MTProto auth keys | `AUTH_KEY_DUPLICATED` is permanent — purges the `.session` file rather than retrying |
| `startup_banner.py` | 116 | Boxed readiness summary | Waits for the port to actually accept before printing |
| `bangla_dict.py` | 78 | 106k-headword offline dictionary | Lazy, lock-guarded, missing file is non-fatal |

### 5.1 `mimic.py` is the most honest file in the tree

```
This module replays that *send burst* only. It deliberately does NOT replay:
  - the page-load sequence (once per boot, not per message)
  - POST /api/v2/users/status (Alibaba monitoring SDK, needs valid logId)
  - aplus.qwen.ai telemetry (third-party signed)
Faking those is more suspicious than omitting them.
```

A partial imitation, with the boundary and the reasoning written down. Default off until measured. This is what every anti-detection hack in the tree should look like and none of the others do.

### 5.2 `browser_launch.py` and `account_login.py` disagree about what a login window is

`browser_launch.py` launches the raw binary with only `--user-data-dir` and the URLs — real OS keyring, no automation flags, deliberately. `account_login.py` uses Playwright with `--no-sandbox --disable-blink-features=AutomationControlled` and **no** `ignore_default_args` — mock keyring. Same job, opposite cookie format.

***

## 6. Findings

| # | Where | What |
|--:|:--|:--|
| 201 | `account_login.py` | **🔴** `launch_kwargs` omits `ignore_default_args=BROWSER_IGNORE_DEFAULT_ARGS`, so Playwright injects `--password-store=basic --use-mock-keychain` and every cookie is encrypted with the `peanuts` mock key. `browser_opener.py` — the other login path — and every ghost pass the flag and use the real OS keyring. Logins done through `account_login.py` are undecryptable by every other component. This is exactly the failure `platform_paths.shared_browser_args`'s comment describes; one call site was never converted. |
| 202 | `chat.py` `stream_chat` | The ghost path is skipped whenever `files` is non-empty (`not files`), so every attachment turn silently uses the raw curl path the ghost exists to avoid. No comment explains the exclusion. |
| 203 | `chat.py` `stream_chat` | Only `GhostError` triggers `GhostEngine.shutdown()`. Any other exception leaves the broken ghost alive and reused next turn, failing identically each time with only a `print` to show for it. |
| 204 | `mimic.schedule_post_send_burst` | `loop.create_task(...)` result is discarded, so the task can be garbage-collected mid-flight. Fire-and-forget is intended; losing the reference is not. |
| 205 | `tools_loader._mode_allows` | A tool that declares `modes` is invisible to every caller that omits `mode` — which is the default argument on `get_all_tool_schemas`. Silent, no warning. |
| 206 | `service_manager.stop_service` | `subprocess.Popen(["systemctl", …])` is unguarded; on a host without systemd the `FileNotFoundError` escapes the stop handler before the self-kill fallback runs. |
| 207 | `checkpoint._git` | `timeout=30` raises `TimeoutExpired`, caught nowhere in `_git`/`_ensure_shadow_repo`/`_heal_repo`, so a hung git propagates out of `CheckpointManager.__init__`. |
| 208 | `qwen_cleanup._save_accounts` | Fixed temp name `accounts.tmp`. Two concurrent writers to `accounts.json` collide on the same temp path and one `replace` loses. |

***

## 7. What the root establishes

- **`platform_paths.py` is the best-commented file in the tree.** It documents a cross-component invariant (keyring backend), the flag that enforces it, the throttle flags and *why a ghost tab needs them*, and the `pid_exists` Windows hazard. Three bugs prevented, in 442 lines.
- **Two files document a decision by explaining an absence** — `checkpoint.py` on `setsid`, `mimic.py` on what it refuses to fake. Both are more valuable than the code they annotate.
- **The keyring invariant is stated once and enforced in three of four call sites.** That is the exact shape of #132/#134/#138/#173/#180 — a documented convention with no mechanism. Here it finally produced a live 🔴 (#201) rather than a latent one.
- **`chat.py` is the oldest file and the only one with a correct incremental UTF-8 decoder.** Age and correctness are correlated here in a way they are not in `connectors/`.
