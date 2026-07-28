
# Sable

**Sable** is a personal agentic chat platform — a FastAPI server that wraps multiple LLM backends (Qwen + DeepSeek) with persistent local memory, an extensible skill registry, browser-based session auth, and a vanilla-JS web UI with real-time SSE streaming.

Built for one person. ~29,000 lines of Python/JS across 129 files. Not a weekend prototype.

***

## Architecture

~~~
Browser (UI) ──SSE/REST──▶ FastAPI Server ──HTTP stream──▶ Qwen API
                                  │                        DeepSeek API
                                  ├── Playwright ──▶ Persistent Chromium (session cookies + WAF tokens)
                                  ├── Semantic Search ──▶ Brain (Memory.json)
                                  ├── SQLite ──▶ system/sable.db (chats & messages)
                                  └── Skill Registry ──▶ Editor / Playwright / ADB subprocesses
~~~

The server routes each message through a **persistent Chromium browser profile** that holds your Qwen session — no API key needed. Session cookies and WAF tokens are sniffed from the browser's network layer, auto-refreshed on 401s, and never leave your machine. DeepSeek models use a direct HTTP API with **Proof-of-Work challenge-based auth** (solved via a compiled Go binary, ~84ms per challenge).

**Memory** entries from `Brain/Memory.json` are injected into every message via semantic search (fastembed). Only the most relevant facts surface — no dumping the whole file into context.

**Skills** run as local subprocesses with sandboxed timeouts and automatic `.sable_backups/` guards before any file mutation.

***

## Quick Start

### Prerequisites

| Requirement | Notes |
|:---|:---|
| Python ≥ 3.11 | |
| `uv` | Fast Python package manager |
| Go ≥ 1.21 | Required for DeepSeek PoW solver (compiles on first run) |
| Playwright browsers | `uv run playwright install chromium` |
| A Qwen account | Free tier at [chat.qwen.ai](https://chat.qwen.ai) |
| (Optional) DeepSeek account | For Expert/Instant/Vision models — API key from [platform.deepseek.com](https://platform.deepseek.com) |

### Installation

~~~bash
cd /home/sifat/hdd/projects/Sable
uv sync
uv run playwright install chromium
~~~

> [!NOTE]
> The DeepSeek PoW solver (`connectors/deepseek/pow_solver/`) compiles automatically on first use. If you don't plan to use DeepSeek models, Go is optional.

### First-Time Setup

**Step 1 — Authenticate with Qwen**

~~~bash
uv run python engine/browser_opener.py
~~~

A Chromium window opens at `chat.qwen.ai`. Log in manually, solve any CAPTCHAs, press Enter in the terminal. Session cookies + WAF tokens are saved to `engine/browser-data/` and auto-refreshed.

> [!NOTE]
> If the session expires after long idle periods, re-run `browser_opener.py`. The server auto-refreshes on 401s, but manual re-login is sometimes needed.

**Step 2 — Set up DeepSeek (optional)**

~~~bash
# Store your DeepSeek API key
echo "your-deepseek-token" > .deepseek_token
# Or export it:
export DEEPSEEK_TOKEN="your-deepseek-token"
~~~

The PoW solver Go binary will compile automatically on first API call. You can also compile it manually:

~~~bash
cd connectors/deepseek/pow_solver
go build -o pow_solver main.go
~~~

**Step 3 — Set your persona**

~~~bash
cp instruction/Maria.md.example instruction/Maria.md
~~~

Edit `instruction/Maria.md` — this **is** the system prompt. Name, OS, tools, active projects, code style, sudo password, everything. The agent reads this on every message to know who you are and how you work.

**Step 4 — Seed your memory (optional)**

~~~bash
cp Brain/Memory.json.example Brain/Memory.json
~~~

Three memory categories supported:

| Type | Purpose | Example |
|:---|:---|:---|
| `semantic` | Facts & knowledge | "Tech Stack: Python + FastAPI" |
| `episodic` | Events & milestones | "Deployed v0.4 on 2026-07-28" |
| `procedural` | Workflows & preferences | "Run tests before every commit" |

**Step 5 — Start the server**

~~~bash
uv run python server.py
~~~

Open **http://localhost:8000**. Login screen asks for your auth token (default: `sable`).

***

## Models

Models and their thinking modes live in `engine/config.py`:

| Model ID | Label | Backend | Modes |
|:---|:---|:---|:---|
| `qwen3.8-max-preview` | Qwen3.8 Max Preview | Qwen (browser) | Thinking |
| `qwen3.7-max` | Qwen3.7 Max | Qwen (browser) | Fast, Thinking |
| `qwen3.7-plus` | Qwen3.7 Plus | Qwen (browser) | Fast, Auto, Thinking |
| `deepseek-expert` | DeepSeek Expert | DeepSeek (HTTP API) | Fast, Thinking |
| `deepseek-instant` | DeepSeek Instant | DeepSeek (HTTP API) | Fast, Thinking |
| `deepseek-vision` | DeepSeek Vision | DeepSeek (HTTP API) | Fast, Thinking |

Add new models by editing the `MODELS` list — they appear in the UI automatically.

### How routing works

- **Qwen models** → Route through the Playwright-managed Chromium browser profile. Cookies and WAF tokens are sniffed from the browser's network layer and auto-refreshed.
- **DeepSeek models** → Direct HTTP API calls. The server solves a Proof-of-Work challenge (difficulty ~144,000, solved in ~84ms by the Go binary) to obtain a session token, then streams responses. Session continuity is maintained per-chat via numeric parent message IDs.
- **Model switching** → Frontend auto-switches the model dropdown based on the chat's parent ID type (numeric = DeepSeek, UUID = Qwen).

***

## Skill Registry

Skills live in `skills/<category>/<key>/` with an `instruction.md` protocol file. 15 registered skills:

| Category | Skills |
|:---|:---|
| **Core** | Code Editor, Phone Control (ADB), Browser Control (Playwright), Testing & Debugging, System Repair, Background Commands, File Uploader |
| **Visuals** | SVG Creator, Graph Master, Simulacra Engine, Frontend Design |
| **Study** | Study Suite (flashcards, practice problems, cheat sheets) |
| **Data** | Document Skills, Online Search, HTTP Client, Video Downloader |

Skills are triggered by matching the user's request against trigger conditions in `skills/registry.json`. Each skill's `instruction.md` is loaded into context only when invoked — keeping the system prompt lean.

To add a skill: create the folder + `instruction.md`, then register it in `skills/registry.json`.

***

## Web UI

The UI is a **vanilla JS SPA** — no React, no build step. Open `http://localhost:8000` after starting the server.

- **SSE streaming** — thinking/answer phases separated in real-time
- **Model switcher** — change models mid-conversation (auto-creates new chat session)
- **Thinking mode selector** — Fast, Auto, or Thinking per model
- **Chat history** — persisted in SQLite, browsable via sidebar
- **Skill event cards** — file edits, command outputs, tool results rendered inline
- **Memory chips** — surfaced memories clickable with full-context popups (supports tool-result memories too)
- **File upload** — drag-and-drop images/PDFs into chat
- **PWA support** — installable as standalone app via `manifest.json` + service worker

***

## Project Structure

~~~
Sable/
├── server.py                     # FastAPI server (SSE, REST, SQLite persistence)
├── engine/
│   ├── config.py                 # Models, thinking modes, session tokens
│   ├── chat.py                   # Message pipeline + streaming logic
│   ├── service.py                # ChatService (DB ops, chat CRUD)
│   ├── session.py                # Qwen session management
│   ├── scraper.py                # Playwright browser scraper service
│   ├── scraper_engines/          # Per-backend scraper implementations
│   │   ├── deepseek/             # DeepSeek PoW challenge + SSE parsing
│   │   └── qwen/                 # Qwen WAF token + cookie sniffing
│   ├── browser_opener.py         # Manual auth flow (opens Chromium)
│   ├── browser-data/             # Persistent Chromium profile (gitignored)
│   ├── memory_search.py          # Semantic search over Memory.json
│   ├── skills.py                 # Skill parser, execution, backup guards
│   └── payloads.py               # Request payload builders
├── connectors/
│   └── deepseek/                 # DeepSeek HTTP API client
│       ├── client.py             # API client with session management
│       └── pow_solver/           # Go PoW solver (compiles on first use)
│           ├── main.go           # Challenge solver (~84ms, difficulty 144k)
│           └── pow_solver        # Compiled binary (gitignored)
├── instruction/
│   ├── Maria.md                  # System prompt (persona + core rules)
│   ├── Maria.md.example          # Persona template for new users
│   ├── output_format.md          # Output formatting rules
│   └── skills.md                 # Skill registry + routing protocol
├── skills/
│   ├── registry.json             # Skill definitions (15 registered)
│   ├── core/                     # Code editor, ADB, Playwright, etc.
│   ├── visuals/                  # SVG, graphs, simulations, frontend
│   ├── study/                    # Flashcards, practice problems
│   └── data/                     # Search, docs, HTTP, video download
├── web/
│   ├── index.html                # SPA shell
│   ├── app.js                    # Chat UI, SSE client, skill cards
│   ├── styles.css                # All styles
│   ├── sw.js                     # Service worker (PWA)
│   └── manifest.json             # PWA manifest
├── Brain/
│   ├── Memory.json               # Persistent memory (semantic search indexed)
│   └── Protected.json            # Protected memory (never auto-injected)
├── output/                       # Generated content (notes, assets, sessions)
├── uploads/                      # Uploaded file storage
├── test/                         # Test suite + benchmarks
├── browser-data/                 # Qwen Chromium profile (gitignored)
├── browser-scraper-data/         # Scraper Chromium profile (gitignored)
├── .sable_backups/               # Auto-backups before file edits
└── pyproject.toml
~~~

***

## API Endpoints

| Method | Path | Description |
|:---|:---|:---|
| `POST` | `/api/login` | Authenticate with bearer token |
| `GET` | `/api/health` | Health check (no auth) |
| `GET` | `/api/models` | List models + thinking modes + api_backend |
| `GET` | `/api/chats` | List all chat sessions |
| `POST` | `/api/chats` | Create a new chat |
| `DELETE` | `/api/chats/{id}` | Delete a chat and its messages |
| `GET` | `/api/messages/{chat_id}` | Get all messages for a chat |
| `POST` | `/api/chat` | Send message (`stream: true/false`) |
| `GET` | `/api/chat/stream/{chat_id}` | SSE stream for a chat |
| `POST` | `/api/upload` | Upload a file |
| `GET` | `/api/logs` | Live server logs via SSE |
| `POST` | `/api/file/revert` | Restore from `.sable_backups/` |
| `GET` | `/api/skills` | List registered skills |
| `GET` | `/api/skills/browse` | Skills with full instruction content |
| `GET/POST` | `/api/memory-search/settings` | Memory search config |
| `POST` | `/api/scraper/settings` | Update scraper config |
| `GET` | `/api/scraper/engines` | List scraper engines |

***

## Configuration

- **Auth token:** Write to `system/.auth_token` (persistent) or `export SABLE_TOKEN=...` (temporary). Default: `sable`.
- **DeepSeek token:** Write to `.deepseek_token` or `export DEEPSEEK_TOKEN=...`.
- **Memory search:** Thresholds and embedding model in `system/memory_search_settings.json` — editable at runtime via API.
- **Scraper:** Engine settings in `system/scraper_settings.json`.
- **Models:** Add/edit in `engine/config.py` → `MODELS` list.
 