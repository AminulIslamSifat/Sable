# `tools/` + `skills/` + `telegram_bot/` + `desktop/` + `scripts/`

> [!INFO] Scope
> The registration layer and peripheral executables. `tools/` (21 dirs), `skills/` (15 manifests), `telegram_bot/` (2,659 lines), `desktop/` (Electron shell), `scripts/` (6 utilities).

***

# 1. `tools/` — Tool Registration

A tool is a directory with `tool.json` (an **array** of function schemas) plus `scripts/`.

## `tool.json` is an ARRAY, not an object

One tool directory can expose multiple callable functions:

```json
[
  { "name": "execute_command",
    "parameters": { "type": "object", "properties": {
      "command": {"type": "string"},
      "cwd": {"type": "string"},
      "env": {"type": "object", "additionalProperties": {"type": "string"}},
      "timeout": {"type": "integer", "minimum": 1, "maximum": 180},
      "bg": {"type": "boolean"}
    }, "required": ["command"] } },
  { "name": "check_command", ... },
  { "name": "kill_command", ... }
]
```

`engine/tools_loader.py`'s `discover_tools()` reads this array; `parser.py`'s `get_known_tags()` derives the live tag list from it.

## The 21 tool directories

`ask_user`, `browser`, `chat_title`, `code_editor`, `computer_use`, `create_note`, `critique`, `execute_command`, `file_uploader`, `grep_search`, `image_generator`, `mcp`, `mcp_list`, `memory_manager`, `multi_agent`, `online_search`, `study_diagnostics`, `study_document`, `study_note`, `study_pdf`, `tracknote_manager`

## Largest implementations

| Size | File |
|--:|:--|
| 59.9 KB | `computer_use/scripts/computer_use.py` |
| 38.2 KB | `online_search/scripts/online_search.py` |
| 38.1 KB | `code_editor/scripts/editor_tools.py` |
| 20.7 KB | `image_generator/scripts/dreamforge.py` |
| 18.7 KB | `image_generator/scripts/perchance_key.py` |
| 18.3 KB | `grep_search/scripts/grep_search.py` |
| 18.1 KB | `image_generator/scripts/advanced_sdxl.py` |
| 17.7 KB | `memory_manager/memory_manager.py` |
| 16.8 KB | `image_generator/scripts/image_generator.py` |
| 13.9 KB | `tracknote_manager/tracknote.py` |

> [!NOTE] `image_generator` ships four backends
> `image_generator.py`, `dreamforge.py`, `perchance_key.py`, `advanced_sdxl.py` — matching the six-provider fallback chain in `engine/skills/handlers/image_generator.py`. The handler is in `engine/`; the implementations are here.

Root-level tooling: `validate.py` (schema validation across all `tool.json`), `build_packages.py`, `test_pipeline.py`, `test_schema_check.py`, `test_update_helpers.py`, `chrome-gpu.sh`.

***

# 2. `skills/` — Capability Modules

```
skills/<name>/
  skill.json         # manifest (single object)
  instruction.md     # routing protocol, injected on demand
  scripts/           # optional
```

## Manifest

```json
{
  "name": "Frontend Design",
  "key": "frontend_design",
  "version": "1.0.0",
  "category": "visuals",
  "description": "...",
  "trigger": "...",
  "not_this_if": "A quick functional snippet is enough. Output is a diagram or simulation.",
  "tags": ["execute_command"],
  "default": false,
  "inline": false,
  "context": "reference",
  "priority": 45,
  "scope": ["*"],
  "config": {},
  "dependencies": [],
  "$schema": "skill-v1"
}
```

> [!TIP] `not_this_if` is negative routing
> Every manifest declares *when NOT to use it*. That is what stops `frontend_design` activating on "write me a quick Python snippet". Positive triggers alone cause over-routing.

## The 15 actual skills

| Key | Priority | Category | Tags |
|:--|--:|:--|:--|
| `testing_debugging` | **90** | core | execute_command |
| `phone_control` | 80 | core | execute_command |
| `system_repair` | 75 | core | execute_command |
| `document_skills` | 65 | data | execute_command, get_file |
| `text_humanizer` | 65 | data | execute_command |
| `deep_research` | 60 | data | execute_command |
| `http_client` | 55 | data | execute_command |
| `study_suite` | 55 | study | create_note, execute_command |
| `email` | 50 | data | execute_command |
| `graph_master` | 50 | visuals | — |
| `svg_creator` | 50 | visuals | — |
| `telegram` | 50 | data | execute_command |
| `frontend_design` | 45 | visuals | execute_command |
| `youtube_downloader` | 40 | data | execute_command, check_command |

**Every skill is `default: false`.** That is why `Brain/disabled_skills.json` carries entries from both `/home/sifat/hdd/projects/Sable/skills/...` and `/home/sifat/hdd/projects/test/Sable-exp/skills/...` — a previous install location's paths are still in the disabled list.

> [!WARNING] `skills/README.md` does not match the directory
> The README claims **25 skills** and lists `code_editor` (priority 100), `multi_agent` (90), `ask_user`, `file_uploader`, `mcp`, `tracknote_manager`, `online_search`, `grep_search`. **None of those exist as `skills/*/` directories** — they are `tools/` entries.
>
> There are **15** real `skill.json` files. The README conflates the two registries. Trust the directory listing.

> [!NOTE] Thirteen of fifteen claim `execute_command`
> Not a conflict: `priority` decides ownership. The tag means "I need shell access", not "I own this command". `testing_debugging` at 90 beats `frontend_design` at 45.

## Lazy instruction loading

From the README: *"Skills are lazy-loaded — their `instruction.md` is only read when explicitly requested via `<get_file>`. This keeps the base context lean. The system prompt includes a registry summary; full instructions are loaded on demand."*

`engine/skills/engine.py`'s `get_registry_prompt()` emits name/description/trigger/tags per skill every turn; the full `instruction.md` costs a tool call.

Manifests and instructions cache in `skills/_cache/` with mtime invalidation — editing a `skill.json` takes effect without a restart.

***

# 3. `telegram_bot/` — 2,659 lines

| File | Lines |
|:--|--:|
| `bot.py` | **2,654** |
| `__init__.py` | 1 |
| `__main__.py` | 4 |

A `python-telegram-bot` integration, distinct from `skills/telegram/` which uses **Telethon**. Auto-starts from `application.py`'s lifespan when `system/.telegram_bot_config.json` has a `bot_token` and is not disabled.

> [!NOTE] Two Telegram integrations, two protocols
> `skills/telegram/` uses **Telethon** (MTProto — acts as a user account, can read any chat). `telegram_bot/` uses **python-telegram-bot** (Bot API — only sees messages addressed to it). Different auth, different capabilities.

Shutdown cancels the task with a 1.0 s timeout in the lifespan teardown.

***

# 4. `desktop/` — Electron Shell

A thin native shell that **attaches to** a running server rather than reimplementing anything.

| File | Size | Role |
|:--|--:|:--|
| `main.js` | 29.7 KB | Main process: probe, spawn, window, menu, IPC |
| `search-engines.js` | 49.4 KB | Search engine bridge |
| `loader.html` | 16.9 KB | Splash / error screen + live log pane |
| `preload.js` | 1.3 KB | Narrow IPC bridge |
| `sable-desktop` | 768 B | Launcher (rebuilds `PATH`, finds `electron44`) |

## Boot sequence

```
1. probe http://127.0.0.1:<port>/api/health
2. answers  -> attach. Never take ownership; quit leaves it running
3. silent   -> spawn sable-launch.sh --no-browser --port=<port>
                poll until healthy, up to 180s
4. launcher exits non-zero -> stop polling immediately,
                              fall back to uv run python server.py
5. quit     -> kill the spawned process group, ONLY if we started it
```

> [!IMPORTANT] Step 4 is the anti-hang rule
> *"If the launcher exits non-zero, stop polling immediately (no waiting on a corpse)."* A naive implementation waits the full 180 s even after the launcher died. Checking exit status turns a 3-minute hang into a 1-second fallback.

## Port resolution

| Source | Example |
|:--|:--|
| CLI flag | `--port=9000` |
| `SABLE_DESKTOP_PORT` | 9000 |
| `SABLE_PORT` | 9000 |
| `PORT` | 9000 |
| default | **61771** |

> [!WARNING] Desktop default is 61771, not 61770
> One digit off from the server default — deliberate, so the shell can run beside a server already on 61770.

## Security posture

```
contextIsolation: true
nodeIntegration: false
sandbox: true
```

> [!TIP] All three secure defaults matter here
> `nodeIntegration: false` — the loaded Sable UI cannot `require()` Node. `contextIsolation: true` — page script cannot reach the preload bridge. With `sandbox: true`, a prompt-injected script in a rendered message cannot escape to the filesystem.

External links open in the default browser; in-app navigation stays in-app.

## Environment

| Var | Effect |
|:--|:--|
| `SABLE_ROOT` | Point at another Sable tree |
| `SABLE_SKIP_STARTER` | `1` = skip the sealed starter, run plaintext |
| `SABLE_PORT` | Default port |

> [!NOTE] The starter needs the cloud bridge
> *"The bundled `sable-starter-bin` requires a cloud token bridge (default `http://127.0.0.1:8765`). If that bridge isn't running it dies in under a second, and the shell falls back to the plaintext server automatically."*

Logs: `system/logs/desktop-server.log`.

## `search-engines.js` — 49 KB

The desktop half of `engine/search/sable_provider.py`. It writes `~/.cache/sable/search-bridge.json` (the `MARKER` constant) and serves the endpoint the Python side POSTs to. If desktop search breaks, both sides are suspects.

***

# 5. `scripts/` — One-Off Utilities

Not part of runtime.

| File | Size | Purpose |
|:--|--:|:--|
| `build_bangla_dict.py` | 3.9 KB | Builds data for `engine/bangla_dict.py` |
| `convert_prints.py` | 2.8 KB | Rewrites `print()` to logger calls |
| `probe_qwen_scraper.py` | 3.3 KB | Diagnostics for the Qwen scraper |
| `test_bare_ui_tags.py` | 6.4 KB | Bare UI tag parsing tests |
| `test_hybrid.py` | 1.6 KB | Hybrid DSML/JSON parser tests |
| `test_render_html.py` | 646 B | `render_html` handling tests |

> [!NOTE] These `test_*.py` are not the pytest suite
> `tests/` is the real suite (`.gitignore`d). These are ad-hoc scripts written while debugging the parser — the bare-UI-tag and hybrid ones correspond directly to bugs documented in `04-engine-skills.md`.

***

## Gotchas

> [!WARNING] Five things to know
> 1. **`skills/README.md` is wrong about the count.** It says 25 and lists `tools/` entries as skills. There are 15 real `skill.json` files.
> 2. **`tool.json` is an array; `skill.json` is an object.** Different schemas, different loaders.
> 3. **`Brain/disabled_skills.json` has stale paths** from `/home/sifat/hdd/projects/Sable/` — a previous install. Misleading when debugging why a skill is off.
> 4. **Desktop defaults to 61771**, one above the server.
> 5. **`search-engines.js` (49 KB) is the desktop half of `sable_provider.py`.**

> [!NOTE] `code_editor` at priority 100 is a tool, not a skill
> The README calls it "the ONLY skill that mutates files on disk". It is `tools/code_editor/`, dispatched via `HANDLER_MAP` — never through the skill registry. The handlers that actually run are `edit_file`/`create_file` in `engine/skills/handlers/file_ops.py`.
