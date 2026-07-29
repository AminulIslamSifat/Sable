
# Sable

**Sable** is a personal agentic chat platform: a local FastAPI server that wraps multiple LLM backends, persists chats in SQLite, injects semantic memory into conversations, executes local skills from the model stream, and serves a vanilla-JS PWA chat UI.

It is built for one user on one machine. The backend can route through:

- **Qwen** via a persistent Playwright-managed Chromium browser session
- **DeepSeek** via a direct HTTP API client with Proof-of-Work challenge solving
- An optional **scraper mode** that drives a live browser engine instead of the normal API path

Measured source size at the time of writing: **121 text source files** and roughly **42,600 lines** of Python, JS, HTML, CSS, Markdown, JSON, TOML, and Go, excluding `.git/`, `.venv/`, backup directories, browser profiles, uploads, and generated output.

***

## Architecture

~~~
Browser UI (web/)
    │
    ├── REST + SSE ──────────────── FastAPI server (server.py, 127.0.0.1:61770)
    │                                   │
    │                                   ├── ChatService (engine/service.py)
    │                                   │     ├── Persistent Chromium profile for Qwen
    │                                   │     ├── Session/header refresh
    │                                   │     └── DeepSeek token refresh via browser session
    │                                   │
    │                                   ├── DeepSeek connector (connectors/deepseek/)
    │                                   │     ├── HTTP chat + SSE streaming
    │                                   │     ├── PoW solver written in Go
    │                                   │     └── Vision file upload support
    │                                   │
    │                                   ├── SQLite persistence (system/sable.db)
    │                                   │     ├── chats
    │                                   │     └── messages
    │                                   │
    │                                   ├── Memory search (engine/memory_search.py)
    │                                   │     ├── Brain/Memory.json
    │                                   │     └── fastembed / sentence-transformers embeddings
    │                                   │
    │                                   ├── Skill parser + executor (engine/skills.py)
    │                                   │     ├── Native editor tags
    │                                   │     ├── execute_command
    │                                   │     ├── Background jobs
    │                                   │     └── .sable_backups/ file backups
    │                                   │
    │                                   └── Optional scraper service (engine/scraper.py)
    │                                         ├── engine/scraper_engines/qwen/
    │                                         └── engine/scraper_engines/deepseek/
    │
    └── PWA assets: manifest.json, service worker, icons
~~~

The web UI is served directly by the FastAPI app from `web/`. Chat responses stream over SSE. Skill events, memory chips, file-edit diffs, and tool output are rendered inline in the UI.

***

## Quick Start

### Prerequisites

| Requirement | Notes |
|:--|:--|
| Python | `>=3.11`, enforced by `pyproject.toml` |
| `uv` | Dependency runner and package manager |
| Playwright Chromium | Installed by the init script |
| Go | Optional unless you need to rebuild the DeepSeek PoW solver |
| Qwen account | Required for Qwen models |
| DeepSeek account | Optional, required for DeepSeek models |

### Automated setup

The repository contains an executable init script named `init`:

~~~bash
./init
~~~

The script does the following:

1. Changes into the project root
2. Runs `uv sync`
3. Installs Playwright Chromium
4. Runs `python -m engine.browser_opener` so you can log into the browser profile
5. Creates `system/`
6. Migrates `system/browser-data` to `system/browser-data-acc1` if needed
7. Creates the `system/browser-data -> browser-data-acc1` symlink if missing
8. Distributes the first account profile to scraper and automation profiles
9. Prompts for an auth token and saves it to `system/.auth_token`
10. Copies `instruction/Maria.md.example` to `instruction/Maria.md` if missing
11. Copies `Brain/Memory.json.example` to `Brain/Memory.json` if missing
12. Makes `start` executable
13. Installs and enables a systemd user service named `sable.service`

### Start the server

~~~bash
./start
~~~

If `./init` has already installed the systemd user service, `start` launches Sable through systemd:

~~~bash
systemctl --user start sable.service
~~~

If the service unit is missing, it falls back to running the server directly:

~~~bash
uv run python server.py
~~~

The server binds to:

~~~
http://127.0.0.1:61770
~~~

Login uses the token saved in `system/.auth_token`. If that file is missing, the server falls back to the environment variable `SABLE_TOKEN`, then to the default value `sable`.


***

## Run Sable Persistently

`./init` automatically installs and enables a **systemd user service** named `sable.service`.

The server always binds to:

~~~
http://127.0.0.1:61770
~~~

The service keeps Sable running in the background, restarts it if it crashes, and can start it automatically after reboot.

### Service management

Start it with either:

~~~bash
./start
~~~

or:

~~~bash
systemctl --user start sable.service
~~~

Enable or disable at login:

~~~bash
systemctl --user enable sable.service
systemctl --user disable sable.service
~~~

The generated unit file is written to:

~~~
~/.config/systemd/user/sable.service
~~~

It uses the project root detected by `./init` and the absolute path to `uv` found on your `PATH`.

Check status:

~~~bash
systemctl --user status sable.service
~~~

Follow logs:

~~~bash
journalctl --user -u sable.service -f
~~~

Restart or stop:

~~~bash
systemctl --user restart sable.service
systemctl --user stop sable.service
~~~

### Start at boot without logging in

By default, systemd user services start only after the user logs in. To make Sable start at boot even before you log in, enable lingering for your user:

~~~bash
sudo loginctl enable-linger sifat
~~~

After that, the service should come back automatically after reboot.

### Desktop autostart alternative

If you do not want systemd lingering, you can also autostart Sable when your graphical session starts. Create:

~~~bash
nano ~/.config/autostart/sable.desktop
~~~

Example:

~~~ini
[Desktop Entry]
Type=Application
Name=Sable
Exec=/home/sifat/hdd/projects/Sable/start
Path=/home/sifat/hdd/projects/Sable
Terminal=false
Hidden=false
X-GNOME-Autostart-enabled=true
~~~

This works on Cinnamon/GNOME-style desktop sessions. For Hyprland, you can instead add an exec-once rule to your Hyprland config:

~~~ini
exec-once = /home/sifat/hdd/projects/Sable/start
~~~

### Important

Do not run `./start` manually while the systemd service is already running. Both will try to bind port `61770`, and the second one will fail.

If the port is already in use, check what is holding it:

~~~bash
ss -ltnp | grep 61770
~~~


***

## Models

Model definitions live in `engine/config.py`.

| Model ID | Label | Backend | Thinking modes |
|:--|:--|:--|:--|
| `qwen3.8-max-preview` | Qwen3.8 Max Preview | Qwen browser session | Thinking |
| `qwen3.7-max` | Qwen3.7 Max | Qwen browser session | Fast, Thinking |
| `qwen3.7-plus` | Qwen3.7 Plus | Qwen browser session | Fast, Auto, Thinking |
| `deepseek-expert` | DeepSeek Expert | DeepSeek HTTP API | Fast, Thinking |
| `deepseek-instant` | DeepSeek Instant | DeepSeek HTTP API | Fast, Thinking |
| `deepseek-vision` | DeepSeek Vision | DeepSeek HTTP API | Fast, Thinking |

The default model is the first entry in `MODELS`, currently `qwen3.8-max-preview`.

`/api/models` exposes each model with its `api_backend` field. When scraper mode is enabled and the scraper engine is set to DeepSeek, `/api/models` instead returns scraper-side DeepSeek model types:

| Scraper model type | Label | Modes |
|:--|:--|:--|
| `default` | Instant | DeepThink, Fast |
| `expert` | Expert | DeepThink, Fast |
| `vision` | Vision | DeepThink, Fast |

***

## Backend Routing

### Qwen

Qwen models route through `ChatService`, which uses a persistent Chromium user data directory. The service maintains session headers and refreshes them when needed. The active API browser profile is configured in `engine/config.py`:

~~~python
BROWSER_DATA_DIR = _SYSTEM / "browser-data-acc7"
~~~

At the time of writing, the active API profile is `system/browser-data-acc7`.

### DeepSeek

DeepSeek models use the pure HTTP connector in `connectors/deepseek/client.py`.

Key properties:

- Base URL: `https://chat.deepseek.com`
- Auth token is read from the persistent browser profile's `localStorage` key `userToken`
- Token cache is stored at `connectors/deepseek/.token_cache.json`
- Each chat request solves a DeepSeek Proof-of-Work challenge using the Go binary at `connectors/deepseek/pow_solver/pow_solver`
- Session continuity is tracked per Sable chat ID
- Instruction files are prepended to the first message of a session:
  - `instruction/Maria.md`
  - `instruction/output_format.md`
  - `instruction/skills.md`

DeepSeek Vision supports file references. Files are uploaded through `/api/deepseek/upload-file`, and the returned file IDs are sent in the `/api/chat` request using `ref_file_ids`.

### Scraper mode

Scraper mode is optional and disabled by default. Settings are stored in `system/scraper_settings.json`:

~~~json
{
  "enabled": false,
  "engine_type": "deepseek",
  "port": 9333,
  "headless": false,
  "show_thoughts": true
}
~~~

Scraper engines live under:

~~~
engine/scraper_engines/qwen/
engine/scraper_engines/deepseek/
~~~

Chats are mode-locked. The `chats` table has a `mode` column, normally `api` or `scraper`. Once a chat has a mode, the server blocks sends when the current global scraper state does not match the chat's locked mode.

***

## Persistence

SQLite database:

~~~
system/sable.db
~~~

The database is gitignored.

### chats

Columns created and migrated by `server.py`:

| Column | Purpose |
|:--|:--|
| `id` | Chat ID |
| `title` | Chat title |
| `parent_id` | Upstream parent/chat reference |
| `created_at` | Creation timestamp |
| `updated_at` | Last update timestamp |
| `memory_keys` | JSON list of memory keys already injected into this chat |
| `chat_url` | Optional browser chat URL |
| `mode` | Locked chat mode, such as `api` or `scraper` |

### messages

| Column | Purpose |
|:--|:--|
| `id` | Autoincrement message ID |
| `chat_id` | Owning chat |
| `role` | `user`, `assistant`, etc. |
| `content` | Message content |
| `thinking` | Optional thinking/reasoning text |
| `skill_events` | JSON skill event log |
| `parent_id` | Message parent reference |
| `created_at` | Timestamp |
| `memory_used` | JSON list of memories surfaced for this message |

***

## Memory System

Memory files live in `Brain/`:

~~~
Brain/
├── Memory.json
├── Memory.json.example
└── Protected.json
~~~

`Memory.json` supports these categories in the server:

- `semantic`
- `episodic`
- `procedural`
- `ephemeral`

Example structure:

~~~json
{
  "semantic": [
    {
      "key": "Project Name",
      "value": "Your project name and brief description"
    }
  ],
  "episodic": [
    {
      "key": "Setup Complete",
      "value": "YYYY-MM-DD: Initial memory system configured"
    }
  ],
  "procedural": [
    {
      "key": "Dev Workflow",
      "value": "How you prefer to work"
    }
  ]
}
~~~

Memory search settings are stored in `system/memory_search_settings.json`:

~~~json
{
  "model": "jinaai/jina-embeddings-v2-small-en",
  "top_k": 5,
  "enabled": true,
  "model_thresholds": {}
}
~~~

During chat, relevant memories are searched and injected best-effort. Injected memory keys are deduplicated per chat using the `memory_keys` column in the `chats` table. The memories used for a particular message are also stored in `messages.memory_used` so the UI can display them.

There are also consolidation endpoints that use an LLM to propose memory updates from conversation content:

- `POST /api/memory/consolidate`
- `POST /api/memory/consolidate-scraper`

Protected memory is managed separately through `/api/settings/memory/protected`.

***

## Skill System

Skills are registered in:

~~~
skills/registry.json
~~~

At the time of writing, the registry contains **16 skills**.

| Category | Skills |
|:--|:--|
| Core | `code_editor`, `phone_control`, `browser_control`, `testing_debugging`, `system_repair`, `background_command`, `file_uploader` |
| Visuals | `svg_creator`, `graph_master`, `simulacra_engine`, `frontend_design` |
| Study | `study_suite` |
| Data | `document_skills`, `online_search`, `http_client`, `video_downloader` |

Skill directories usually contain an `instruction.md` file and optional scripts. The skill browser endpoint enriches registry entries with instruction content and script listings.

### Known runtime tags

`engine/skills.py` recognizes these tags in the model stream:

- `execute_command`
- `execute_background_command`
- `check_command`
- `get_file`
- `read_file`
- `search-online`
- `search_online`
- `openweb`
- `create_note`
- `save_svg`
- `view_file`
- `edit_file`
- `create_file`
- `insert_file`

### Execution rules

- Default command timeout: **15 seconds**
- Maximum command timeout: **180 seconds**
- File mutations are backed up to `.sable_backups/`
- Native editor tags call `skills/core/code_editor/scripts/editor_tools.py`
- Skill execution emits `skill_start`, `skill_output`, and `skill_end` events
- The UI renders these events as inline cards
- File edits can be reverted through `POST /api/file/revert`, restricted to the managed backup directory

***

## Browser Profiles

Runtime browser data lives under `system/`.

The init script creates and distributes profiles from `system/browser-data-acc1`:

~~~
system/
├── browser-data/                  # symlink, managed by account tooling
├── browser-data-acc1/
├── browser-data-acc2/
├── ...
├── browser-data-acc11/
├── browser-data.bak/
├── browser-scraper-data/
├── browser-scraper-data.bak/
├── automation-browser-data/
├── automation-browser-data.bak/
├── memory_search_settings.json
├── scraper_settings.json
└── sable.db
~~~

The server exposes three managed profile classes:

| Key | Label | Data directory |
|:--|:--|:--|
| `api` | API / ChatService | `system/browser-data-acc7` |
| `scraper` | Scraper | `system/browser-scraper-data` |
| `automation` | Automation / Browser Control | `system/automation-browser-data` |

Each has a `.bak` counterpart for backup and restore.

Account endpoints scan directories matching `system/browser-data-acc*`, read the logged-in email from Chromium `Preferences`, and report profile sizes.

***

## Web UI

The frontend is a no-build vanilla JS app in `web/`:

~~~
web/
├── index.html
├── app.js
├── styles.css
├── manifest.json
├── sw.js
├── favicon.svg
├── sable_icon.svg
├── icon-192.png
└── icon-512.png
~~~

Major UI features:

- SSE chat streaming
- Model switcher
- Thinking-mode selector
- Chat sidebar persisted from SQLite
- Inline skill cards
- File-edit diff cards with revert
- Memory-used chips
- File upload
- PWA manifest and service worker
- Settings panels for logs, general browser settings, accounts, backups, memory, and skills

***

## API Endpoints

### Auth and health

| Method | Path | Description |
|:--|:--|:--|
| `POST` | `/api/login` | Validate bearer token |
| `GET` | `/api/health` | Health check, auth-exempt |
| `GET` | `/api/logs` | Live log stream over SSE; supports `?token=` because EventSource cannot set headers |

### Chats and messages

| Method | Path | Description |
|:--|:--|:--|
| `GET` | `/api/chats` | List chats |
| `POST` | `/api/chat/new` | Create a new chat |
| `GET` | `/api/chats/{chat_id}/messages` | Get messages for a chat |
| `DELETE` | `/api/chats/{chat_id}` | Delete a chat and its messages |
| `POST` | `/api/chat` | Send a chat message, streaming or non-streaming |
| `POST` | `/api/sync-context` | Sync browser/session context |

`POST /api/chat` accepts:

~~~json
{
  "message": "string",
  "chat_id": "string or null",
  "parent_id": "string or null",
  "files": [],
  "model": "model id or null",
  "thinking_mode": "mode id or null",
  "stream": true,
  "ref_file_ids": []
}
~~~

### Models and skills

| Method | Path | Description |
|:--|:--|:--|
| `GET` | `/api/models` | List available models or scraper models |
| `GET` | `/api/skills` | List registered skills |
| `GET` | `/api/skills/browse` | List skills with instruction content and scripts |

### Uploads

| Method | Path | Description |
|:--|:--|:--|
| `POST` | `/api/upload` | General file upload |
| `POST` | `/api/deepseek/upload-file` | Upload a file for DeepSeek Vision and receive file IDs |

### File backups

| Method | Path | Description |
|:--|:--|:--|
| `POST` | `/api/file/revert` | Restore a file from `.sable_backups/` |

### Scraper

| Method | Path | Description |
|:--|:--|:--|
| `GET` | `/api/settings/scraper` | Get scraper settings |
| `POST` | `/api/settings/scraper` | Update scraper settings |
| `GET` | `/api/settings/scraper/engines` | List scraper engines |
| `GET` | `/api/scraper/sessions` | Inspect active scraper browser session |
| `POST` | `/api/scraper/sessions/kill` | Kill scraper browser session |
| `POST` | `/api/scraper/model` | Switch scraper model type |

### Browser profiles and accounts

| Method | Path | Description |
|:--|:--|:--|
| `GET` | `/api/settings/browser` | Get browser settings such as headless mode |
| `POST` | `/api/settings/browser` | Update browser settings and restart browser |
| `POST` | `/api/settings/deepseek/refresh-token` | Force-refresh DeepSeek token |
| `GET` | `/api/settings/accounts` | List `browser-data-acc*` profiles |
| `POST` | `/api/settings/accounts/switch` | Switch active account profile symlink |
| `GET` | `/api/settings/browser/profiles` | Show API, scraper, and automation profile status |
| `POST` | `/api/settings/browser/restore` | Restore a profile from its `.bak` snapshot |
| `POST` | `/api/settings/browser/create-backup` | Snapshot a profile to its `.bak` directory |

### Memory

| Method | Path | Description |
|:--|:--|:--|
| `GET` | `/api/settings/memory` | Read `Memory.json` |
| `POST` | `/api/settings/memory` | Update `Memory.json` |
| `GET` | `/api/settings/memory/protected` | Read protected memory |
| `POST` | `/api/settings/memory/protected` | Update protected memory |
| `GET` | `/api/settings/memory-search` | Read memory search settings |
| `POST` | `/api/settings/memory-search` | Update memory search settings |
| `POST` | `/api/memory/consolidate` | Consolidate memories from chat using API mode |
| `POST` | `/api/memory/consolidate-scraper` | Consolidate memories using scraper mode |

***

## Project Structure

~~~
Sable/
├── server.py                      # FastAPI app, routes, SQLite persistence, SSE chat
├── start                          # uv run python server.py
├── init                           # Automated setup script
├── pyproject.toml                 # Python project and dependencies
├── uv.lock                        # Locked dependencies
│
├── engine/
│   ├── config.py                  # Models, thinking modes, runtime paths
│   ├── service.py                 # ChatService: browser session, streaming, uploads
│   ├── session.py                 # Browser/session helpers
│   ├── chat.py                    # Chat pipeline helpers
│   ├── payloads.py                # Payload builders
│   ├── memory_search.py           # Semantic memory search
│   ├── skills.py                  # Skill parsing, execution, backups, background jobs
│   ├── scraper.py                 # Optional scraper service
│   ├── browser_opener.py          # Browser login/opener flow
│   └── scraper_engines/
│       ├── qwen/                  # Qwen scraper engine
│       └── deepseek/              # DeepSeek scraper engine
│
├── connectors/
│   └── deepseek/
│       ├── client.py              # DeepSeek HTTP client with PoW and SSE
│       ├── upload.py              # DeepSeek upload helpers
│       └── pow_solver/
│           ├── main.go            # Go PoW solver source
│           ├── go.mod
│           └── pow_solver         # Compiled solver binary
│
├── instruction/
│   ├── Maria.md                   # Active persona/system instructions
│   ├── Maria.md.example           # Template
│   ├── output_format.md           # Output formatting rules
│   ├── skills.md                  # Skill routing instructions
│   ├── deepseek_instructions.md
│   └── mem_cmd.py                 # Consolidation prompt templates
│
├── skills/
│   ├── registry.json              # Registered skills
│   ├── core/                      # Editor, browser, phone, debugging, repair, etc.
│   ├── visuals/                   # SVG, graphs, simulations, frontend design
│   ├── study/                     # Study suite
│   ├── data/                      # Search, docs, HTTP, video download
│   └── diary_creator/
│
├── web/
│   ├── index.html                 # SPA shell
│   ├── app.js                     # Chat UI, SSE client, settings, skill cards
│   ├── styles.css                 # Styles
│   ├── manifest.json              # PWA manifest
│   └── sw.js                      # Service worker
│
├── Brain/
│   ├── Memory.json                # Active memory
│   ├── Memory.json.example        # Memory template
│   └── Protected.json             # Protected memory
│
├── system/                        # Runtime data, gitignored
│   ├── sable.db                   # SQLite database
│   ├── .auth_token                # Login token
│   ├── .session_tokens.json       # Session token seed
│   ├── memory_search_settings.json
│   ├── scraper_settings.json
│   ├── browser-data-acc*/         # Account browser profiles
│   ├── browser-scraper-data/
│   └── automation-browser-data/
│
├── uploads/                       # Uploaded files, gitignored
├── output/                        # Generated notes/assets/sessions, gitignored
├── scraper_output/                # Scraper-generated output, gitignored
├── test/                          # Tests and demos
└── mockups/                       # UI mockups
~~~

***

## Dependencies

Runtime dependencies from `pyproject.toml`:

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
| `sentence-transformers` | Embedding model support |
| `lxml` | HTML/XML parsing |

The project is marked as a non-packaged `uv` project:

~~~toml
[tool.uv]
package = false
~~~

***

## Testing

Tests and demos live in `test/`:

~~~
test/
├── card_demo.py
├── card_demo_big.py
├── card_test.py
├── patch_benchmark.py
├── progress_demo.py
├── test_browser_control.py
├── test_consolidate_parse.py
├── test_editor_tools.py
├── test_embedding.py
├── test_hybrid_threshold.py
├── test_memory_injection.py
├── test_restore_symlinks.py
└── test_tool_pending.py
~~~

Examples:

~~~bash
# Editor tools unit tests; pytest is not in the project venv by default
uv run --with pytest python -m pytest test/test_editor_tools.py -q

# Browser control suite; long-running because of Playwright timeouts
uv run python test/test_browser_control.py
~~~

***

## Configuration and Secrets

### Auth token

Preferred:

~~~
system/.auth_token
~~~

Fallback environment variable:

~~~bash
export SABLE_TOKEN="your-token"
~~~

Default if neither exists:

~~~
sable
~~~

### DeepSeek token

DeepSeek auth is extracted from the browser profile's `localStorage` key `userToken`. A cached copy is stored at:

~~~
connectors/deepseek/.token_cache.json
~~~

Use the endpoint below to force a refresh:

~~~
POST /api/settings/deepseek/refresh-token
~~~

### Runtime settings

| File | Purpose |
|:--|:--|
| `system/memory_search_settings.json` | Memory search model, top-k, thresholds |
| `system/scraper_settings.json` | Scraper enable state, engine, port, headless mode |
| `system/.session_tokens.json` | Session token seed for Qwen |
| `engine/config.py` | Models, thinking modes, active browser profile paths |

***

## Gitignore Notes

The `.gitignore` excludes runtime and personal data, including:

- SQLite databases
- Python caches and virtual environments
- Editor and Sable backup directories
- Browser profile directories under `system/`
- Scraper output
- Uploads
- Secrets such as `system/.auth_token` and `system/.session_tokens.json`
- Logs
- Generated `output/`
- Personal instruction and memory files such as `instruction/Maria.md`, `Brain/Memory.json`, and `Brain/Protected.json`

***

## Security Model

Sable is intended to run locally.

- Server binds to `127.0.0.1`, not `0.0.0.0`
- API routes require a bearer token
- `/api/health`, `/api/login`, `/static/`, and `/uploads/` are auth-exempt
- `/api/logs` allows query-token auth because browser EventSource cannot set headers
- File revert is restricted to the managed `.sable_backups/` directory
- Browser profiles contain cookies and session tokens, so they are gitignored
- Skill execution has timeout guards and process-group killing
