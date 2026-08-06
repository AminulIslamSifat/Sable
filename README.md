
# Sable

Self-hosted agentic development companion combining AI chat, a full web IDE, browser automation, multi-agent orchestration, persistent memory, and a content library into one personal platform. Proxies Qwen and DeepSeek through browser session auth (no API keys required for Qwen), supports Gemini/Groq/Mistral via API keys, and runs entirely on your machine.

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
server.py              → uvicorn entry point (host/port from engine/config.py)
server/                → FastAPI app, routes, database, config, utils
  api/routes/          → chat, settings, memory, agents, filesystem, email,
                         terminal, library, scraper, upload, misc
engine/                → Core logic
  chat.py              → Qwen SSE streaming via httpx, auto token refresh
  session.py           → Playwright BrowserManager: header sniffing, WAF capture,
                         Aliyun OSS upload, DeepSeek token extraction
  service.py           → ChatService: persistent Chromium session, streaming
  config.py            → Models, endpoints, paths, custom model registry
  memory_search.py     → fastembed vector search over Brain/Memory.json
  scraper.py           → Browser scraper orchestrator (engine loading, lifecycle)
  scraper_engines/     → CDP-based browser engines (Qwen 103KB, DeepSeek 50KB)
  skills/              → SkillEngine: registry, parser, middleware, handlers, bg jobs
  agents/              → Multi-agent: loop, runtime, teacher, auto-turn, resilience
  mcp/                 → MCP client: stdio subprocess manager, tool routing
  diary/               → Gemini-powered session summarization + synthesis
  security/            → Prompt guard, middleware
skills/                → 22 skill directories (instruction.md + skill.json + scripts/)
connectors/            → API backend clients (DeepSeek, Gemini, Groq, Mistral)
web/                   → Frontend: vanilla JS PWA with embedded IDE
  app.js               → Main application (241KB): markdown renderer, SSE, auth, chat
  js/agents.js         → Multi-agent visualization (top bar, panel, todos)
  js/filesystem.js     → File tree explorer + Monaco editor + diff review
  js/terminal.js       → xterm.js integrated terminal
  js/mode.js           → Agent ↔ IDE layout switching
  css/                 → 8 stylesheets, 11 themes, glassmorphism design system
  vendor/              → Bundled: Monaco, xterm.js, Mermaid, MathJax, Lucide, DOMPurify
Brain/                 → Memory.json + Protected.json + skills.json (persistent knowledge)
system/                → Runtime data: SQLite DB (108MB), 115+ browser profiles,
                         auth tokens, agent config, MCP config (gitignored)
output/                → Generated notes, research, agent logs, assets (gitignored)
instruction/           → System prompts (Maria.md), output format, personal context
```

***

## Core Systems

### Chat & Streaming

SSE-based streaming with typewriter animation (adaptive: low-memory devices get larger batches). Supports thinking modes (Fast/Auto/Thinking) per model. Multi-tab chat with independent scroll positions. Context injection includes timestamp, cwd, and open file metadata.

- **Qwen backend**: Browser session auth via Playwright. WAF tokens (`bx-ua`, `bx-umidtoken`) sniffed from live Chromium requests. Auto-refresh on 401/403. Aliyun OSS image upload with STS token flow.
- **DeepSeek backend**: Pure HTTP connector. Auth token extracted from browser `localStorage`. Each request solves a PoW challenge via Go binary (`connectors/deepseek/pow_solver/`).
- **API connectors**: Gemini, Groq, Mistral — all with multi-key rotation and streaming.

### Multi-Account Browser Profiles

115+ browser profile directories under `system/` for multi-account management:

- `browser-data-acc1` through `browser-data-accN` — individual sessions
- `browser-data` — symlink to active account
- `browser-scraper-data` — dedicated scraper profile
- `automation-browser-data` — automation/testing profile
- Each has a `.bak` counterpart for backup/restore

Profile stripping removes cache/GPU data while preserving cookies, localStorage, and session state. Switch via UI, API (`POST /api/settings/accounts/switch`), or `engine/browser_opener.py`.

### Scraper Engines (Browser Automation)

Two dedicated CDP-based browser engines for driving live chat interfaces:

| Engine | Size | Target |
|:--|:--|:--|
| `scraper_engines/qwen/qwen_engine.py` | 103KB | chat.qwen.ai |
| `scraper_engines/deepseek/deepseek_engine.py` | 50KB | chat.deepseek.com |

**Capabilities:**
- Chrome/Thorium/Playwright-Chromium launch with remote debugging
- WebSocket proxy that intercepts CDP messages (webview→page type rewriting)
- HTTP proxy for CDP endpoint discovery
- DOM mutation observer response capture + clipboard fallback (`wl-paste`/`xclip`)
- Instruction injection with SHA256 hash tracking (re-injects only on change)
- Thought expansion automation (clicks expand buttons periodically)
- Session markdown archival organized by date folders
- CSS injection to hide UI garbage
- Obsidian port conflict detection (diverts to alternate port)
- Headed/headless mode switching with profile lock management

The scraper orchestrator (`engine/scraper.py`) dynamically loads engine modules, manages browser lifecycle, probes existing CDP sessions before spawning duplicates, and exposes the same async interface as the HTTP ChatService.

### Multi-Agent Orchestration

Hub-and-spoke architecture with wave-based parallel execution:

- **Roles**: researcher, coder, reviewer, writer, utility
- **Concurrency**: Up to 5 simultaneous agents
- **Resilience**: Circuit breakers per backend, loop detection (consecutive + total call limits)
- **Todo tracking**: `<todo_done>` / `<todo_sub>` tags with live progress visualization
- **Auto-turn**: Agent completions signal the frontend to run a normal chat turn, rendering results identically to user-initiated messages
- **Decomposer**: Heuristic pre-filter suggesting when messages benefit from parallel agents
- **Teacher escalation**: Escalate difficult subtasks to a more capable model
- **Notifications**: Per-chat async queue; Maria drains pending events at turn start
- **Output**: Agent results saved as markdown to `output/agent/`, browsable via Library

Each agent gets isolated session context, role-specific skill scoping (`allowed_skills` + `default_skills`), and configurable model routing. Skill instructions are injected dynamically from `instruction.md` files.

### Memory System

- **Storage**: `Brain/Memory.json` (categories: semantic, episodic, procedural, ephemeral) + `Brain/Protected.json`
- **Search**: fastembed vector search with configurable top-k, thresholds, and max prompt chars (auto-disables on <8GB RAM)
- **Deduplication**: Injected memory keys tracked per chat session
- **Consolidation**: Endpoints propose memory updates from conversation content
- **Human-readable**: JSON you can edit by hand — no opaque vector DB

### Skill Engine (22 Skills)

Self-contained skill directories with auto-discovery:

```
skills/<name>/
├── instruction.md    # Routing protocol + usage docs
├── skill.json        # Manifest: name, key, version, tags, priority, scope
└── scripts/          # Optional helper scripts
```

**Architecture:**
- **Registry** (`registry.py`): Discovery, validation, priority-based tag ownership
- **Parser** (`parser.py`): Streaming XML tag extraction within `<action>` blocks, malformed tag recovery, live progress events
- **Middleware** (`middleware.py`): Validation → Permission → Execution → Logging pipeline
- **Engine** (`engine.py`): Orchestrates dispatch, emits SSE events
- **Background jobs** (`bg_jobs.py`): Namespaced process tracking with log files
- **Handlers** (`handlers/`): execute, file_ops, io, web implementations

| Category | Skills |
|:--|:--|
| Code & System | code_editor, background_command, system_repair, testing_debugging, grep_search |
| Research & Web | online_search, deep_research, http_client, browser_control |
| Documents | document_skills (pdf, docx, pptx, xlsx) |
| Visuals | graph_master, svg_creator, frontend_design, simulacra_engine |
| Study | study_suite |
| Media | youtube_downloader |
| Device | phone_control |
| Meta | multi_agent, ask_user, file_uploader, mcp, text_humanizer |

### MCP Client Integration

Full Model Context Protocol client (`engine/mcp/manager.py`):

- Spawns MCP servers as subprocesses over stdio
- Tool discovery via `list_tools()`
- Auto-routing: `call_tool_auto()` finds which server owns a tool
- Event loop isolation (separate asyncio tasks avoid anyio cancel scope conflicts)
- System prompt section generation listing all connected tools
- Config persistence in `system/mcp_servers.json`
- Handler bridges worker threads to main event loop via `run_coroutine_threadsafe`

### Diary System

Gemini-powered reflective session synthesis (`engine/diary/`):

- **Summarizer** (`summarizer.py`): Summarizes individual session logs via Gemini API
- **Synthesizer** (`synthesizer.py`): Merges per-session summaries into cohesive diary entries with structured sections (Arc & snapshot, Highlights, Technical/work, Threads & next steps, Closing note)
- **Key rotation** (`gemini_helpers.py`): Multi-API-key rotation with persistent state tracking across calls. Uses `gemini-3.1-flash-lite` with configurable temperature per task. Handles up to 900K chars input.

### Email (IMAP/SMTP Client)

Full email client (`server/api/routes/email.py`):

- IMAP connection with SSL/TLS, folder listing, message search
- MIME header decoding, multipart body extraction (plain → HTML fallback)
- Attachment listing with size/content-type metadata
- SMTP send with MIMEMultipart, HTML support, CC, attachments
- Sent folder auto-detection (Gmail, Outlook, Yahoo naming conventions)
- Configuration persistence with connection testing before save

***

## Web IDE

Sable includes a complete browser-based development environment alongside the chat.

### Monaco Editor

- Full VS Code editor loaded from `/static/vendor/monaco/`
- Language detection by file extension (Python, JS, TS, HTML, CSS, JSON, YAML, Markdown, TOML, SQL, XML, SVG, shell scripts)
- Configurable font size (persisted to localStorage)
- Dirty state tracking with auto-save on file switch
- Ctrl+S keyboard shortcut
- Binary file detection with placeholder display

### File Tree Explorer

- Recursive directory browsing with expand/collapse
- 30+ file type icon mappings (Lucide icons)
- Root picker with server-side folder selection (`/api/filesystem/pick-folder`)
- Recent folders history (localStorage, max 8)
- Quick access roots from server API
- New file/folder creation from toolbar
- File size display, active file highlighting, path bar

### Integrated Terminal

- **Real PTY** via `os.forkpty()` (same mechanism as VS Code/node-pty)
- **Fish shell** with terminal capability probe interception:
  - Kitty keyboard, XTVERSION, OSC11 background, DA1, Cursor Position Report
  - Deliberately skips XTGETTCAP to prevent fish leaking replies as typed text
- **WebSocket bridge**: JSON protocol (input/output/resize/exit)
- **Window resize**: SIGWINCH forwarding + TIOCSWINSZ ioctl
- **xterm.js frontend** with resizable panel (VS Code-style)
- **Multiple views**: Terminal, Output, Problems tabs
- **Session management**: new/clear/kill actions

### Diff Review System

- Monaco's built-in diff viewer for pending file edits
- Per-file revert button (restores from `.sable_backups/`)
- Accept/reject workflow for AI-generated changes
- Slide-in sidebar with smooth animation

### Layout Modes

| Mode | Description |
|:--|:--|
| **Agent** | Full-width chat with sidebar |
| **IDE** | Compact chat panel + Monaco editor + file tree + terminal |

IDE mode features:
- Compact chat mirrors main messages via MutationObserver (adaptive: 16ms during streaming, 80ms idle)
- Session persistence (restores last folder + file)
- Sidebar toggle opens full chat history overlay
- Independent resize handles for chat width and diff sidebar

***

## Library

Centralized content browser (`/api/library/`):

| Section | Source | Content |
|:--|:--|:--|
| Agents | `output/agent/` | Agent result markdown (excludes `_conversation` logs) |
| Research | `output/research/` | Deep research reports |
| Notes | `output/notes/` | Generated notes |
| Gallery | `system/uploads/` | Uploaded/generated images (png, jpg, svg, webp, gif) |
| Skills | `Brain/skills.json` | User-created skill definitions |

Features:
- YAML frontmatter parsing (title, date, tags)
- Preview extraction (first meaningful paragraph)
- Date-sorted display
- Inline content reading with path traversal protection

***

## Supported Models

Model definitions live in `engine/config.py` → `MODELS` list + custom models from `system/.custom_models.json`.

| Model ID | Backend | Thinking Modes |
|:--|:--|:--|
| `qwen3.8-max` | Qwen (browser) | Fast / Auto / Thinking |
| `qwen3.7-max` | Qwen (browser) | Fast / Thinking |
| `qwen3.7-plus` | Qwen (browser) | Fast / Auto / Thinking |
| `deepseek-expert` | DeepSeek API | Fast / Thinking |
| `deepseek-instant` | DeepSeek API | Fast / Thinking |
| `deepseek-vision` | DeepSeek API | Fast / Thinking |
| `gemini-2.5-flash` | Gemini API | Fast / Low / Medium / High |
| `gemini-2.5-pro` | Gemini API | Fast / Low / Medium / High |

Custom models can be added/hidden via the Providers UI. The connector registry (`connectors/__init__.py`) lazy-loads backend clients and exposes `resolve_backend()` + `get_connector()` + `is_backend_available()`.

***

## Frontend

Vanilla JS PWA — zero framework dependencies, fully offline-capable.

### Application (app.js — 241KB)

- **Custom markdown renderer**: Headers, bold/italic/strikethrough, `==highlight==`, code, tables, nested lists, Obsidian-style callouts (20+ types), links, images. HTML-escaped by construction.
- **MathJax**: Inline `$...$` and block `$$...$$` math
- **Mermaid**: Flowcharts, sequence diagrams, Gantt charts
- **Auth gate**: Token-based login, auto-inject bearer into all requests, 401 → re-login without reload
- **SSE streaming**: Real-time with typewriter animation
- **Model/thinking switchers**: Glass dropdown UI, capability-aware attachments
- **File attachment**: Drag-and-drop, preview chips, upload progress
- **Context menu**: Right-click for new/settings/sync/archive/delete
- **Chat search**: Floating animated search input

### Design System

- **11 themes**: Default, Noctalia, Ember, Ocean, Forest, Rosé, Indigo, Crimson, Mono, Teal, Blue
- **Glassmorphism**: `color-mix()` with multi-layer inset box-shadows
- **Self-hosted fonts**: Maple Mono (4 weights) + Inter (4 weights)
- **Dual icon system**: Emoji fallbacks + Lucide icons
- **Responsive**: Mobile sidebar overlay, touch targets, horizontal scroll panels
- **PWA**: Manifest + service worker + Apple mobile meta tags

### Bundled Libraries

| Library | Size | Purpose |
|:--|:--|:--|
| Mermaid | 3.4MB | Diagram rendering |
| MathJax | 1.1MB | Math rendering |
| Lucide | 404KB | Icon library |
| Monaco | (vendor dir) | Code editor |
| xterm.js | (vendor dir) | Terminal emulator |
| Marked | 34.6KB | Markdown fallback |
| DOMPurify | 28.5KB | HTML sanitization |

***

## Persistence

SQLite database at `system/sable.db` (108MB, WAL journaling). Raw sqlite3, no ORM.

| Table | Key Columns |
|:--|:--|
| `chats` | id, title, parent_id, created_at, updated_at, memory_keys, chat_url, mode, provider |
| `messages` | id, chat_id, role, content, thinking, skill_events, parent_id, created_at, memory_used |

***

## Configuration

| Setting | Location | Default |
|:--|:--|:--|
| Server port | `SABLE_PORT` env var | `61770` |
| Server host | `SABLE_HOST` env var | `0.0.0.0` |
| Auth token | `system/.auth_token` → `SABLE_TOKEN` env → `sable` | `sable` |
| Memory max prompt chars | `engine/config.py` | `20000` |
| Memory search settings | `system/memory_search_settings.json` | Runtime editable via UI |
| Scraper settings | `system/scraper_settings.json` | Runtime editable via UI |
| Agent config | `system/agent_config.json` | Role overrides, account assignments |
| MCP servers | `system/mcp_servers.json` | Server configs with enable/disable |
| Email config | `system/.email_config.json` | IMAP/SMTP credentials |
| Session tokens | `system/.session_tokens.json` | Auto-managed by Playwright |
| DeepSeek token cache | `connectors/deepseek/.token_cache.json` | Auto-managed |
| Custom models | `system/.custom_models.json` | User-added model definitions |
| Hidden models | `system/.hidden_models.json` | User-deleted static model IDs |
| Provider API keys | `system/.gemini_api_keys.json`, `.groq_api_keys.json`, `.mistral_api_keys.json` | Multi-key pools |

***

## API Endpoints

### Auth & Health

| Method | Path | Description |
|:--|:--|:--|
| `POST` | `/api/login` | Validate bearer token |
| `GET` | `/api/health` | Health check |
| `GET` | `/api/logs` | Live log stream (SSE) |
| `GET` | `/api/config/ui` | UI config (typewriter settings) |

### Chats & Messages

| Method | Path | Description |
|:--|:--|:--|
| `GET` | `/api/chats` | List chats |
| `POST` | `/api/chat/new` | Create new chat |
| `GET` | `/api/chats/{id}/messages` | Get messages |
| `DELETE` | `/api/chats/{id}` | Delete chat |
| `POST` | `/api/chat` | Send message (streaming) |
| `GET` | `/api/chats/search` | Full-text search across messages |
| `POST` | `/api/sync-context` | Sync browser/session context |

### Models & Skills

| Method | Path | Description |
|:--|:--|:--|
| `GET` | `/api/models` | List available models |
| `GET` | `/api/skills` | List registered skills |
| `GET` | `/api/skills/browse` | Skills with instruction content |

### Agents

| Method | Path | Description |
|:--|:--|:--|
| `GET` | `/api/agents/{id}/messages` | Agent conversation history |
| `GET` | `/api/agents/events` | SSE stream for agent events |
| `POST` | `/api/agents/{id}/stop` | Stop a running agent |

### Filesystem & Editor

| Method | Path | Description |
|:--|:--|:--|
| `GET` | `/api/filesystem/list` | Directory listing |
| `GET` | `/api/filesystem/read` | Read file content |
| `POST` | `/api/filesystem/write` | Write/create file |
| `POST` | `/api/filesystem/mkdir` | Create directory |
| `POST` | `/api/filesystem/pick-folder` | Server-side folder picker |
| `GET` | `/api/filesystem/roots` | Quick access root directories |

### Terminal

| Method | Path | Description |
|:--|:--|:--|
| `WS` | `/ws/terminal` | PTY WebSocket (fish/bash) |

### Library

| Method | Path | Description |
|:--|:--|:--|
| `GET` | `/api/library/agents` | Agent output markdown |
| `GET` | `/api/library/research` | Research reports |
| `GET` | `/api/library/notes` | Generated notes |
| `GET` | `/api/library/gallery` | Uploaded images |
| `GET` | `/api/library/skills` | User-created skills |
| `GET` | `/api/library/read/{section}/{file}` | Read full content inline |

### Email

| Method | Path | Description |
|:--|:--|:--|
| `GET` | `/api/email/configured` | Check if email is set up |
| `POST` | `/api/email/config` | Save credentials (tests connection) |
| `DELETE` | `/api/email/config` | Remove email config |
| `GET` | `/api/email/folders` | List IMAP folders |
| `GET` | `/api/email/messages` | Fetch headers (search, paginate) |
| `GET` | `/api/email/message/{uid}` | Read full message + attachments |
| `POST` | `/api/email/send` | Send via SMTP |

### Memory

| Method | Path | Description |
|:--|:--|:--|
| `GET` | `/api/settings/memory` | Get memory entries |
| `POST` | `/api/settings/memory` | Add memory entry |
| `PUT` | `/api/settings/memory/{key}` | Update memory entry |
| `DELETE` | `/api/settings/memory/{key}` | Delete memory entry |
| `GET/POST` | `/api/settings/memory/protected` | Protected memories CRUD |
| `POST` | `/api/memory/consolidate` | Propose memory updates from conversation |

### Settings & Browser

| Method | Path | Description |
|:--|:--|:--|
| `GET/POST` | `/api/settings/browser` | Headless toggle |
| `GET/POST` | `/api/settings/scraper` | Scraper enable/engine/port |
| `GET` | `/api/settings/scraper/engines` | List available scraper engines |
| `POST` | `/api/settings/browser/refresh-waf` | Re-sniff Qwen WAF tokens |
| `POST` | `/api/settings/deepseek/refresh-token` | Refresh DeepSeek token |
| `GET` | `/api/settings/accounts` | List account profiles |
| `POST` | `/api/settings/accounts/switch` | Switch active profile |
| `POST` | `/api/settings/accounts/strip` | Strip profiles to bare session data |

### Provider API Keys

| Method | Path | Description |
|:--|:--|:--|
| `POST/GET/DELETE` | `/api/settings/gemini/api-key` | Gemini key pool management |
| `POST/GET/DELETE` | `/api/settings/groq/api-key` | Groq key pool management |
| `POST/GET/DELETE` | `/api/settings/mistral/api-key` | Mistral key pool management |
| `GET` | `/api/settings/providers/{name}/models` | Fetch available models from provider API |

### Uploads & Files

| Method | Path | Description |
|:--|:--|:--|
| `POST` | `/api/upload` | General file upload |
| `POST` | `/api/deepseek/upload-file` | DeepSeek Vision file upload |
| `POST` | `/api/file/revert` | Restore from `.sable_backups/` |

***

## Persistent Service (systemd)

```bash
./start                              # start via systemd
systemctl --user status sable        # check status
journalctl --user -u sable -f        # tail logs
systemctl --user restart sable       # restart after code changes
```

For boot-before-login: `sudo loginctl enable-linger $USER`

> [!WARNING] Port Conflicts
> Don't run `./start` while the service is already active — both bind the same port. Check with: `ss -ltnp | grep 61770`

***

## Project Scale

| Component | Size | Notes |
|:--|:--|:--|
| Scraper engines | 153KB | Qwen (103KB) + DeepSeek (50KB) CDP orchestration |
| Web frontend | ~500KB | app.js (241KB) + JS modules (123KB) + CSS (156KB) |
| Server routes | ~170KB | 15 route modules |
| Engine core | ~130KB | Chat, session, config, memory, scraper, skills |
| Connectors | ~65KB | DeepSeek + Gemini + Groq + Mistral + base protocol |
| Agent system | ~80KB | Loop, runtime, teacher, auto-turn, resilience, registry |
| MCP client | ~16KB | Manager + handler |
| Database | 108MB | SQLite with WAL journaling |
| Browser profiles | 115+ | Multi-account session management |
| Skills | 22 dirs | Modular drop-in architecture |
| Themes | 11 | Complete CSS variable sets |
| Vendor libs | ~5MB | Mermaid + MathJax + Lucide + Monaco + xterm |
