
---
title: Sable README
date: 2026-08-02
type: reference
tags: [sable, documentation, setup]
status: active
---

# Sable

Self-hosted agentic chat platform that proxies Qwen and DeepSeek through a local FastAPI server with persistent memory, 20 built-in skills, multi-agent orchestration, and a browser-based PWA UI. Runs entirely on your machine — no cloud API keys required for Qwen (browser session auth), DeepSeek uses PoW + localStorage token.

***

## Quick Start

```bash
git clone https://github.com/AminulIslamSifat/Sable.git && cd Sable
chmod +x init start status
./init    # one-time setup (deps, browser login, systemd service)
./start   # launch via systemd (or direct uv fallback)
```

Open `http://127.0.0.1:61770` in your browser. First run opens a Chromium window for Qwen/DeepSeek login — tokens are stored locally in `system/.session_tokens.json`.

> [!IMPORTANT] Prerequisites
> - Python ≥ 3.11
> - [uv](https://docs.astral.sh/uv/) package manager
> - Go (optional, only needed to rebuild the DeepSeek PoW solver)
> - systemd (for persistent service; optional — `./start` falls back to direct `uv run`)

***

## Architecture

```
server.py          → uvicorn entry point (host/port from engine/config.py)
server/            → FastAPI app, routes, database, config, utils
engine/            → Core logic: chat streaming, session/token mgmt, memory search, skills, scraper
skills/            → 20 skill directories (instruction.md + skill.json + optional scripts/)
connectors/        → DeepSeek HTTP client with Go PoW solver
web/               → Vanilla JS frontend (app.js + modular CSS, esbuild bundle from web/src/)
Brain/             → Memory.json + Protected.json (persistent knowledge base)
system/            → Runtime data: SQLite DB, browser profiles, uploads, auth tokens (gitignored)
output/            → Generated notes, assets, logs, session archives (gitignored)
instruction/       → System prompts (Maria.md) and output format configs
```

### Key Components

| Component | Purpose |
|:--|:--|
| `engine/service.py` | ChatService: persistent Chromium session, header refresh, streaming |
| `engine/chat.py` | Qwen SSE streaming via httpx, auto token refresh on 401/403 |
| `engine/session.py` | Playwright-based BrowserManager for header sniffing & context sync |
| `engine/memory_search.py` | fastembed vector search over Brain/Memory.json |
| `engine/skills/` | SkillEngine discovery, parsing, handler dispatch, middleware pipeline |
| `connectors/deepseek/client.py` | DeepSeek HTTP client with PoW challenge solving |
| `server/api/routes/chat.py` | Main POST /api/chat endpoint with skill loop + memory injection |
| `server/database.py` | Raw sqlite3 (no ORM), chats/messages/settings tables |
| `web/app.js` | Bundled frontend (esbuild from web/src/), streaming chat UI, settings panels |
| `web/js/agents.js` | Extracted multi-agent visualization module |

***

## Supported Models

Model definitions live in `engine/config.py` → `MODELS` list.

| Model ID | Backend | Thinking Modes |
|:--|:--|:--|
| `qwen3.8-max-preview` | Qwen | Thinking |
| `qwen3.7-max` | Qwen | Fast, Thinking |
| `qwen3.7-plus` | Qwen | Fast, Auto, Thinking |
| `deepseek-expert` | DeepSeek API | Fast, Thinking |
| `deepseek-instant` | DeepSeek API | Fast, Thinking |
| `deepseek-vision` | DeepSeek API | Fast, Thinking |

Default model is the first entry (`qwen3.8-max-preview`). `/api/models` exposes each model with its `api_backend` field.

### Backend Routing

- **Qwen**: Routes through `ChatService` using a persistent Chromium profile. Headers are sniffed from browser sessions and refreshed automatically on auth rejection. Active profile configured via `BROWSER_DATA_DIR` in `engine/config.py`.
- **DeepSeek**: Pure HTTP connector. Auth token extracted from browser `localStorage`, cached at `connectors/deepseek/.token_cache.json`. Each request solves a PoW challenge via the Go binary at `connectors/deepseek/pow_solver/pow_solver`. Instruction files prepended to first message of each session.
- **Scraper mode**: Optional, disabled by default. Drives a live browser engine instead of the API path. Settings in `system/scraper_settings.json`. Chats are mode-locked (`api` or `scraper`).

***

## Skills (20)

Each skill is a self-contained directory under `skills/` with `instruction.md`, `skill.json`, and optional `scripts/`. Auto-discovered at startup — drop a folder in, restart, done.

| Category | Skills |
|:--|:--|
| **Code & System** | code_editor, background_command, system_repair, testing_debugging |
| **Research & Web** | online_search, deep_research, http_client, browser_control |
| **Documents** | document_skills (pdf, docx, pptx, xlsx) |
| **Visuals** | graph_master, svg_creator, frontend_design, simulacra_engine |
| **Study** | study_suite |
| **Media** | youtube_downloader |
| **Device** | phone_control |
| **Meta** | multi_agent, ask_user, file_uploader |

### Skill Engine Architecture

```
engine/skills/
├── __init__.py          # Public API: SkillEngine, SkillParser, HANDLER_MAP
├── registry.py          # discover_skills(), validate_registry(), SkillMeta
├── engine.py            # SkillEngine orchestrator
├── parser.py            # SkillParser (action-gated tag extraction)
├── middleware.py        # Validation → Permission → Execution → Logging pipeline
├── events.py            # SSE event builders
├── bg_jobs.py           # BackgroundJobManager
└── handlers/            # Tag handler implementations
    ├── common.py        # Shared constants, paths, helpers
    ├── execute.py       # execute_command, background, check_command
    ├── file_ops.py      # view_file, edit_file, create_file, insert_file
    ├── io.py            # get_file, create_note, save_svg
    └── web.py           # openweb, online_search
```

### Execution Rules

- Default command timeout: **15 seconds**, max **180 seconds**
- File mutations backed up to `.sable_backups/`
- Native editor tags call `skills/code_editor/scripts/editor_tools.py` as fresh subprocess (no restart needed)
- Execution emits `skill_start`, `skill_output`, `skill_end` SSE events
- File edits revertible via `POST /api/file/revert`
- Tag conflicts resolved by priority (higher wins, warning logged)

***

## Multi-Agent Orchestration

Hub-and-spoke architecture with wave-based parallel execution:

- Spawn up to 5 concurrent background agents (researcher, coder, reviewer, writer, utility)
- Each agent has isolated session context and configurable model routing
- Results return as notifications; main thread continues chatting
- Agent config: `system/agent_config.json`
- Frontend visualization: `web/js/agents.js`

***

## Memory System

Memory files live in `Brain/`:

- `Memory.json` — categories: semantic, episodic, procedural, ephemeral
- `Protected.json` — protected memories managed via `/api/settings/memory/protected`

Memory search settings: `system/memory_search_settings.json` (model, top-k, thresholds, enabled flag). Editable via UI.

During chat, relevant memories are searched via fastembed and injected best-effort. Injected keys are deduplicated per chat using the `memory_keys` column. Consolidation endpoints propose memory updates from conversation content:

- `POST /api/memory/consolidate` (API mode)
- `POST /api/memory/consolidate-scraper` (scraper mode)

***

## Persistence

SQLite database at `system/sable.db` (gitignored). Raw sqlite3, no ORM.

### Tables

| Table | Key Columns |
|:--|:--|
| `chats` | id, title, parent_id, created_at, updated_at, memory_keys, chat_url, mode |
| `messages` | id, chat_id, role, content, thinking, skill_events, parent_id, created_at, memory_used |

***

## Configuration

| Setting | Location | Default |
|:--|:--|:--|
| Server port | `SABLE_PORT` env var | `61770` |
| Server host | `SABLE_HOST` env var | `0.0.0.0` |
| Auth token | `system/.auth_token` → `SABLE_TOKEN` env → `sable` | `sable` |
| Memory max prompt chars | `engine/config.py` | `20000` |
| Skill round warning threshold | `server/config.py` | configurable |
| Memory search settings | `system/memory_search_settings.json` | runtime editable via UI |
| Scraper settings | `system/scraper_settings.json` | runtime editable via UI |
| Session tokens | `system/.session_tokens.json` | auto-managed by Playwright |
| DeepSeek token cache | `connectors/deepseek/.token_cache.json` | auto-managed |

***

## Persistent Service (systemd)

`./init` installs `~/.config/systemd/user/sable.service` automatically.

```bash
./start                              # start via systemd
systemctl --user status sable        # check status
journalctl --user -u sable -f        # tail logs
systemctl --user restart sable       # restart after code changes
```

For boot-before-login: `sudo loginctl enable-linger $USER`

### Desktop Autostart Alternative

For Hyprland: `exec-once = ./start` in hyprland.conf. For GNOME/Cinnamon: create `~/.config/autostart/sable.desktop`.

> [!WARNING] Port Conflicts
> Don't run `./start` while the service is already active — both bind the same port. Use `systemctl --user stop sable` first if switching modes. Check with: `ss -ltnp | grep 61770`

***

## Browser Profiles

Multi-account support via numbered profile directories under `system/`:

- `browser-data-acc1` through `browser-data-accN` — individual account sessions
- `browser-data` — symlink to active account (switched via UI or API)
- `browser-scraper-data` — dedicated scraper profile
- `automation-browser-data` — automation/testing profile
- Each has a `.bak` counterpart for backup/restore

### Adding Accounts

1. Stop Sable (`systemctl --user stop sable`)
2. Copy existing profile: `cp -r system/browser-data-acc1 system/browser-data-accN`
3. Open new profile: `uv run python engine/browser_opener.py N`
4. Log in, press ENTER to save session
5. Restart Sable, switch via Account settings tab or `POST /api/settings/accounts/switch`

Profile management: `engine/browser_opener.py`, UI settings panel, or API endpoints.

***

## Web UI

Vanilla JS PWA served directly by FastAPI from `web/`. Built with esbuild from `web/src/` modules.

```bash
# Rebuild after editing web/src/:
cd web && esbuild src/app.js --bundle --outfile=app.js --format=iife --allow-overwrite
# Or just restart Sable — ExecStartPre rebuilds automatically
```

Features: SSE chat streaming, model/thinking-mode switcher, inline skill cards, file-edit diffs with revert, memory chips, file upload, PWA manifest + service worker, settings panels (logs, general, accounts, backups, brain, skills).

***

## API Endpoints

### Auth & Health

| Method | Path | Description |
|:--|:--|:--|
| `POST` | `/api/login` | Validate bearer token |
| `GET` | `/api/health` | Health check (auth-exempt) |
| `GET` | `/api/logs` | Live log stream (SSE, supports `?token=`) |

### Chats & Messages

| Method | Path | Description |
|:--|:--|:--|
| `GET` | `/api/chats` | List chats |
| `POST` | `/api/chat/new` | Create new chat |
| `GET` | `/api/chats/{chat_id}/messages` | Get messages |
| `DELETE` | `/api/chats/{chat_id}` | Delete chat |
| `POST` | `/api/chat` | Send message (streaming) |
| `POST` | `/api/sync-context` | Sync browser/session context |

### Models & Skills

| Method | Path | Description |
|:--|:--|:--|
| `GET` | `/api/models` | List available models |
| `GET` | `/api/skills` | List registered skills |
| `GET` | `/api/skills/browse` | Skills with instruction content |

### Uploads & Files

| Method | Path | Description |
|:--|:--|:--|
| `POST` | `/api/upload` | General file upload |
| `POST` | `/api/deepseek/upload-file` | DeepSeek Vision file upload |
| `POST` | `/api/file/revert` | Restore from `.sable_backups/` |

### Settings & Browser

| Method | Path | Description |
|:--|:--|:--|
| `GET/POST` | `/api/settings/browser` | Browser settings |
| `GET/POST` | `/api/settings/scraper` | Scraper settings |
| `GET` | `/api/settings/accounts` | List account profiles |
| `POST` | `/api/settings/accounts/switch` | Switch active profile |
| `GET` | `/api/settings/browser/profiles` | Profile status |
| `POST` | `/api/settings/browser/restore` | Restore from .bak |
| `POST` | `/api/settings/browser/create-backup` | Snapshot profile |
| `POST` | `/api/settings/deepseek/refresh-token` | Force-refresh DS token |

### Memory

| Method | Path | Description |
|:--|:--|:--|
| `GET/POST` | `/api/settings/memory` | Read/update Memory.json |
| `GET/POST` | `/api/settings/memory/protected` | Protected memory |
| `GET/POST` | `/api/settings/memory-search` | Search settings |
| `POST` | `/api/memory/consolidate` | Consolidate (API mode) |
| `POST` | `/api/memory/consolidate-scraper` | Consolidate (scraper mode) |

***

## Dependencies

From `pyproject.toml`:

| Package | Purpose |
|:--|:--|
| `fastapi` | Web framework |
| `uvicorn[standard]` | ASGI server |
| `httpx` | Async HTTP client |
| `python-multipart` | File upload parsing |
| `playwright` | Browser automation and persistent sessions |
| `fastembed` | Embeddings for memory search |
| `numpy` | Numerical support |
| `pydantic` | Request/response validation |
| `lxml` | HTML/XML parsing |

Managed by `uv` (`package = false`).

***

## Testing

Tests live in `test/`:

```bash
# Editor tools unit tests
uv run --with pytest python -m pytest test/test_editor_tools.py -q

# Browser control suite (long-running)
uv run python test/test_browser_control.py
```

***

## Development Notes

- **Frontend**: edit `web/src/*.js`, rebuild with esbuild or restart Sable (ExecStartPre auto-rebuilds)
- **Skill edits take effect immediately** — skills run as fresh subprocesses, no server restart needed
- **Engine/server changes require restart** — uvicorn holds imported modules in memory
- **Database**: raw sqlite3 at `system/sable.db`, no migrations framework — schema implicit in `server/database.py`
- **Gitignore**: excludes system/, output/, uploads/, .venv/, *.db, browser profiles, secrets, personal instruction/memory files

***

## Security Model

Sable is intended to run locally.

- Server binds to `127.0.0.1` by default (configurable via `SABLE_HOST`)
- API routes require bearer token (except `/api/health`, `/api/login`, `/static/`, `/uploads/`)
- `/api/logs` allows query-token auth (EventSource can't set headers)
- File revert restricted to `.sable_backups/`
- Browser profiles contain cookies/tokens — always gitignored
- Skill execution has timeout guards and process-group killing

