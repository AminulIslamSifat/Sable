

Sable

Sable is an agentic chat platform that wraps the Qwen API with a persistent local memory system, an extensible skill registry, and a clean web UI. It's built for one person — you — with a deeply personalized persona, browser-based auth, and real-time SSE streaming.

At ~29,000 lines of Python/JS across 129 files, Sable is a fully-featured personal AI platform, not a weekend prototype.

Architecture
text

Browser (UI) ──SSE/REST──▶ FastAPI Server ──HTTP streaming──▶ Qwen API
                                  │
                                  ├── Playwright ──▶ Persistent Chromium (sniffs WAF tokens & cookies)
                                  ├── Semantic Search ──▶ Brain (Memory.json)
                                  ├── SQLite ──▶ sable.db (chats & messages)
                                  └── Skill Registry ──▶ Editor Tools / Playwright / ADB (subprocess)

The server proxies every message through a persistent Chromium browser profile that maintains your Qwen session — no API key needed. Session cookies and WAF tokens are sniffed from the browser's network layer, refreshed automatically on 401s, and never leave your machine.

Memory entries from Brain/Memory.json are injected into each message via semantic search using fastembed, so only the most relevant facts surface in any given conversation. Skills execute as local subprocesses with sandboxed timeouts and backup guards.

Quick Start
Prerequisites
Requirement	Notes
Python ≥ 3.11	
[Now I have a thorough understanding of the project. The README is corrupted with recursive create_file text embedded in it, and the pyproject.toml looks clean. Let me write the proper README.

<create_file path="/home/sifat/hdd/projects/Sable/README.md" overwrite="true">

Sable

Sable is an agentic chat platform that wraps the Qwen API with a persistent local memory system, an extensible skill registry, and a clean web UI. It's built for one person — you — with a deeply personalized persona, browser-based auth, and real-time SSE streaming.

At ~29,000 lines of Python/JS across 129 files, Sable is a fully-featured personal AI platform, not a weekend prototype.

Architecture
text

Browser (UI) ──SSE/REST──▶ FastAPI Server ──HTTP streaming──▶ Qwen API
                                  │
                                  ├── Playwright ──▶ Persistent Chromium (sniffs WAF tokens & cookies)
                                  ├── Semantic Search ──▶ Brain (Memory.json)
                                  ├── SQLite ──▶ sable.db (chats & messages)
                                  └── Skill Registry ──▶ Editor Tools / Playwright / ADB (subprocess)

The server proxies every message through a persistent Chromium browser profile that maintains your Qwen session — no API key needed. Session cookies and WAF tokens are sniffed from the browser's network layer, refreshed automatically on 401s, and never leave your machine.

Memory entries from Brain/Memory.json are injected into each message via semantic search using fastembed, so only the most relevant facts surface in any given conversation. Skills execute as local subprocesses with sandboxed timeouts and backup guards.

Quick Start
Prerequisites
Requirement	Notes
Python ≥ 3.11	
uv	Fast Python package manager
Playwright browsers	Installed via uv run playwright install
A Qwen account	Free tier at chat.qwen.ai
Installation
bash

cd /home/sifat/hdd/projects/Sable
uv sync
uv run playwright install chromium
First-Time Setup
Step 1 — Authenticate with Qwen

Sable uses a persistent Chromium profile to maintain your Qwen session. Log in once:

bash

uv run python engine/browser_opener.py

A Chromium window opens at chat.qwen.ai. Log in manually, solve any CAPTCHAs, then press Enter in the terminal. Your session cookies and WAF tokens are saved to ./browser-data/ and auto-refreshed by the server.

[!NOTE]
If your session expires, re-run browser_opener.py. The server auto-refreshes on 401 errors, but manual re-login may be needed after long idle periods.

Step 2 — Set up your persona
bash

cp instruction/Maria.md.example instruction/Maria.md

 instruction/Maria.md with your name, OS, preferred tools, active projects, code style preferences, and sudo password (for system-level skill commands). This file is the system prompt — it defines who the agent is and what it knows about you.

Step 3 — Seed your memory (optional)
bash

cp Brain/Memory.json.example Brain/Memory.json

 Brain/Memory.json with key facts the agent should remember across sessions. Three categories are supported:

semantic — Facts and knowledge ("Tech Stack: Python + FastAPI")

episodic — Events and milestones ("Deployed v0.4 on 2026-07-28")

procedural — Workflows and preferences ("Run tests before every commit")

Memory entries are injected into the system prompt via semantic search — only the most relevant entries for the current conversation are surfaced.

Step 4 — Start the server
bash

uv run python server.py

Open **http://localhost:8000Now I have a thorough understanding of the project. The README is corrupted with recursive create_file text embedded in it, and the pyproject.toml looks clean. Let me write the proper README.

<create_file path="/home/sifat/hdd/projects/Sable/README.md" overwrite="true">

Sable

Sable is an agentic chat platform that wraps the Qwen API with a persistent local memory system, an extensible skill registry, and a clean web UI. It's built for one person — you — with a deeply personalized persona, browser-based auth, and real-time SSE streaming.

At ~29,000 lines of Python/JS across 129 files, Sable is a fully-featured personal AI platform, not a weekend prototype.

Architecture
text

Browser (UI) ──SSE/REST──▶ FastAPI Server ──HTTP streaming──▶ Qwen API
                                  │
                                  ├── Playwright ──▶ Persistent Chromium (sniffs WAF tokens & cookies)
                                  ├── Semantic Search ──▶ Brain (Memory.json)
                                  ├── SQLite ──▶ sable.db (chats & messages)
                                  └── Skill Registry ──▶ Editor Tools / Playwright / ADB (subprocess)

The server proxies every message through a persistent Chromium browser profile that maintains your Qwen session — no API key needed. Session cookies and WAF tokens are sniffed from the browser's network layer, refreshed automatically on 401s, and never leave your machine.

Memory entries from Brain/Memory.json are injected into each message via semantic search using fastembed, so only the most relevant facts surface in any given conversation. Skills execute as local subprocesses with sandboxed timeouts and backup guards.

Quick Start
Prerequisites
Requirement	Notes
Python ≥ 3.11	
uv	Fast Python package manager
Playwright browsers	Installed via uv run playwright install
A Qwen account	Free tier at chat.qwen.ai
Installation
bash

cd /home/sifat/hdd/projects/Sable
uv sync
uv run playwright install chromium
First-Time Setup
Step 1 — Authenticate with Qwen

Sable uses a persistent Chromium profile to maintain your Qwen session. Log in once:

bash

uv run python engine/browser_opener.py

A Chromium window opens at chat.qwen.ai. Log in manually, solve any CAPTCHAs, then press Enter in the terminal. Your session cookies and WAF tokens are saved to ./browser-data/ and auto-refreshed by the server.

[!NOTE]
If your session expires, re-run browser_opener.py. The server auto-refreshes on 401 errors, but manual re-login may be needed after long idle periods.

Step 2 — Set up your persona
bash

cp instruction/Maria.md.example instruction/Maria.md

 instruction/Maria.md with your name, OS, preferred tools, active projects, code style preferences, and sudo password (for system-level skill commands). This file is the system prompt — it defines who the agent is and what it knows about you.

Step 3 — Seed your memory (optional)
bash

cp Brain/Memory.json.example Brain/Memory.json

 Brain/Memory.json with key facts the agent should remember across sessions. Three categories are supported:

semantic — Facts and knowledge ("Tech Stack: Python + FastAPI")

episodic — Events and milestones ("Deployed v0.4 on 2026-07-28")

procedural — Workflows and preferences ("Run tests before every commit")

Memory entries are injected into the system prompt via semantic search — only the most relevant entries for the current conversation are surfaced.

Step 4 — Start the server
bash

uv run python server.py

Open http://localhost:8000 in your browser. The default auth token is sable (configurable via SABLE_TOKEN env var or .auth_token file).

Skill Registry

Sable's agentic power comes from 16 local skill handlers that execute commands, edit files, control browsers, and more. Skills are triggered by XML-style tags the model emits in its responses.

Skill	Category	What It Does
 Editor	Core	View, create, edit, and insert files with line-precise SEARCH/REPLACE operations
Phone Control	Core	Control an Android phone via ADB — tap, swipe, type, screenshots
Browser Control	Core	Full Playwright browser automation with persistent daemon
Testing & Debugging	Core	Structured bug investigation protocol (reproduce → isolate → fix)
System Repair	Core	Arch Linux + Hyprland crash recovery with inline diagnostic protocol
Background Commands	Core	Long-running builds, servers, and downloads with PID tracking
File Uploader	Core	Load PDFs, images, DOCX, PPTX, and other files into context
SVG Creator	Visuals	Data structure visualizations and node diagrams
Graph Master	Visuals	Mathematical function plots with labeled Cartesian axes
Simulacra Engine	Visuals	Interactive, animated HTML/JS simulations
Frontend Design	Visuals	Production-grade UI components and layouts
Study Suite	Study	Flashcards, Anki decks, practice problems, cheat sheets
Document Skills	Data	Create and edit DOCX, PDF, PPTX, XLSX files
Online Search	Data	Two-phase web search (search → fetch pages)
HTTP Client	Data	API testing with env presets and multi-step request chains
Video Downloader	Data	Download videos or extract audio from YouTube, Twitter, Instagram

Each skill has its own instruction.md protocol in skills/<category>/<key>/instruction.md. See the Skill Development section below for adding new skills.

Project Structure
text

Sable/
├── server.py                    # FastAPI server (SSE streaming, REST API, auth, SQLite)
├── pyproject.toml               # Project metadata & uv dependencies
├── uv.lock                      # Locked dependency versions
├── sable.db                     # SQLite database (chats, messages, auto-created)
│
├── engine/                      # Core chat engine
│   ├── chat.py                  # CLI chat client
│   ├── service.py               # FastAPI-friendly async service layer
│   ├── session.py               # Playwright browser session + Qwen auth
│   ├── config.py                # Endpoints, models, thinking modes, paths
│   ├── payloads.py              # Request body builder for Qwen API
│   ├── skills.py                # Skill tag parser + all 16 skill handlers
│   ├── memory_search.py         # Semantic memory search via fastembed
│   ├── scraper.py               # Web scraper engine manager
│   └── browser_opener.py        # Manual Qwen login helper
│
├── instruction/                 # System prompt & formatting rules
│   ├── Maria.md                 # Persona definition (YOU edit this)
│   ├── Maria.md.example         # Example persona template
│   ├── output_format.md         # Output formatting rules
│   └── skills.md                # Skill usage guidelines
│
├── skills/                      # Skill registry (one folder per skill)
│   ├── registry.json            # Master skill index
│   ├── core/                    # System-level skills
│   ├── data/                    # Web & document skills
│   ├── study/                   # Study & learning skills
│   └── visuals/                 # Diagram & simulation skills
│
├── Brain/                       # Persistent memory store
│   ├── Memory.json              # Semantic/episodic/procedural memories
│   ├── Memory.json.example      # Template with sample entries
│   └── Protected.json           # Protected memories (never surfaced to model)
│
├── web/                         # Frontend (vanilla JS PWA)
│   ├── index.html               # Main SPA shell
│   ├── app.js                   # Chat UI, SSE client, skill event cards
│   ├── styles.css               # All styles
│   ├── sw.js                    # Service worker for offline/PWA support
│   └── manifest.json            # PWA manifest
│
├── output/                      # Generated content (gitignored)
│   ├── notes/                   # Agent-created notes
│   ├── assets/                  # Generated SVGs, images
│   └── sessions/                # Session logs
│
├── uploads/                     # Uploaded file storage
├── browser-data/                # Persistent Chromium profile (gitignored)
├── browser-scraper-data/        # Scraper's Chromium profile (gitignored)
├── .sable_backups/              # File backups before edits (auto-created)
└── test/                        # Test suite & benchmarks
Configuration
Models & Thinking Modes

Models and their thinking modes are defined in engine/config.py:

Model ID	Label	Thinking Modes
qwen3.8-max-preview	Qwen3.8 Max Preview	
qwen3.7-max	Qwen3.7 Max	Fast, 
qwen3.7-plus	Qwen3.7 Plus	Fast, Auto, 

To add a new model, add an entry to the MODELS list in engine/config.py. It'll appear in the UI automatically.

Auth Token

The web UI requires a bearer token to access the API. Default is sable. Change it via:

File (persistent): Write your token to .auth_token in the project root

Env var (temporary): export SABLE_TOKEN=your-token

Memory Search Settings

The embedding model and similarity thresholds are stored in memory_search_settings.json and can be changed at runtime via the /api/memory-search/settings endpoint.

Web UI

Open http://localhost:8000 after starting the server. You'll see a login screen — enter your auth token (sable by default, or whatever you set).

Features

Chat interface — Real-time SSE streaming with thinking/answer phase separation

Model switcher — Change models mid-conversation (creates a new chat session)

 mode selector — Fast, Auto, or Thinking depending on the model

Chat history — All conversations persisted in SQLite, browsable in sidebar

Skill event cards — File edits, command outputs, and tool results rendered inline

File upload — Drag-and-drop images/PDFs into chat

PWA support — Install as a standalone app via manifest.json + service worker

The UI is a vanilla JS SPA — no frameworks. SSE events are parsed and rendered incrementally with DOM manipulation.

API Endpoints
Method	Path	Description
POST	/api/login	Authenticate with bearer token
GET	/api/health	Health check (no auth)
GET	/api/models	List available models & thinking modes
GET	/api/chats	List all chat sessions
POST	/api/chats	Create a new chat
DELETE	/api/chats/{id}	Delete a chat and its messages
GET	/api/messages/{chat_id}	Get all messages for a chat
POST	/api/chat	Send a message (supports stream: true/false)
GET	/api/chat/stream/{chat_id}	SSE stream for a chat
POST	/api/upload	Upload a file (image, PDF, etc.)
GET	/api/logs	Live server logs via SSE
POST	/api/file/revert	Restore a file from .sable_backups/
GET	/api/skills	List all registered skills
GET	/api/skills/browse	Skills with full instruction content
GET	/api/memory-search/settings	Get memory search config
POST	/api/memory-search/settings	Update memory search config
POST	/api/scraper/settings	Update scraper config
GET	/api/scraper/engines	List scraper engines
Skill Development

Skills live in skills/<category>/<key>/ with this structure:

text

skills/core/my_skill/
├── instruction.md     # Skill protocol (loaded into context when triggered)
└── scripts/           # Python/JS scripts the skill invokes
    └── my_tool.py

To register a new skill:

Create the folder and instruction.md

Add an entry to skills/registry.json:

json

{
  "name": "My Skill",
  "key": "my_skill",
  "category": "core",
  "path": "core/my_skill",
  "description": "What it does",
  "triggers": ["When to activate this skill"]
}
~~Now I have a thorough understanding of the project. The README is corrupted with recursive create_file text embedded in it, and the pyproject.toml looks clean. Let me write the proper README.

<create_file path="/home/sifat/hdd/projects/Sable/README.md" overwrite="true">

Sable

Sable is an agentic chat platform that wraps the Qwen API with a persistent local memory system, an extensible skill registry, and a clean web UI. It's built for one person — you — with a deeply personalized persona, browser-based auth, and real-time SSE streaming.

At ~29,000 lines of Python/JS across 129 files, Sable is a fully-featured personal AI platform, not a weekend prototype.

Architecture
text

Browser (UI) ──SSE/REST──▶ FastAPI Server ──HTTP streaming──▶ Qwen API
                                  │
                                  ├── Playwright ──▶ Persistent Chromium (sniffs WAF tokens & cookies)
                                  ├── Semantic Search ──▶ Brain (Memory.json)
                                  ├── SQLite ──▶ sable.db (chats & messages)
                                  └── Skill Registry ──▶ Editor Tools / Playwright / ADB (subprocess)

The server proxies every message through a persistent Chromium browser profile that maintains your Qwen session — no API key needed. Session cookies and WAF tokens are sniffed from the browser's network layer, refreshed automatically on 401s, and never leave your machine.

Memory entries from Brain/Memory.json are injected into each message via semantic search using fastembed, so only the most relevant facts surface in any given conversation. Skills execute as local subprocesses with sandboxed timeouts and backup guards.

Quick Start
Prerequisites
Requirement	Notes
Python ≥ 3.11	
uv	Fast Python package manager
Playwright browsers	Installed via uv run playwright install
A Qwen account	Free tier at chat.qwen.ai
Installation
bash

cd /home/sifat/hdd/projects/Sable
uv sync
uv run playwright install chromium
First-Time Setup
Step 1 — Authenticate with Qwen

Sable uses a persistent Chromium profile to maintain your Qwen session. Log in once:

bash

uv run python engine/browser_opener.py

A Chromium window opens at chat.qwen.ai. Log in manually, solve any CAPTCHAs, then press Enter in the terminal. Your session cookies and WAF tokens are saved to ./browser-data/ and auto-refreshed by the server.

[!NOTE]
If your session expires, re-run browser_opener.py. The server auto-refreshes on 401 errors, but manual re-login may be needed after long idle periods.

Step 2 — Set up your persona
bash

cp instruction/Maria.md.example instruction/Maria.md

 instruction/Maria.md with your name, OS, preferred tools, active projects, code style preferences, and sudo password (for system-level skill commands). This file is the system prompt — it defines who the agent is and what it knows about you.

Step 3 — Seed your memory (optional)
bash

cp Brain/Memory.json.example Brain/Memory.json

 Brain/Memory.json with key facts the agent should remember across sessions. Three categories are supported:

semantic — Facts and knowledge ("Tech Stack: Python + FastAPI")

episodic — Events and milestones ("Deployed v0.4 on 2026-07-28")

procedural — Workflows and preferences ("Run tests before every commit")

Memory entries are injected into the system prompt via semantic search — only the most relevant entries for the current conversation are surfaced.

Step 4 — Start the server
bash

uv run python server.py

Open http://localhost:8000 in your browser. The default auth token is sable (configurable via SABLE_TOKEN env var or .auth_token file).

Skill Registry

Sable's agentic power comes from 16 local skill handlers that execute commands, edit files, control browsers, and more. Skills are triggered by XML-style tags the model emits in its responses.

Skill	Category	What It Does
 Editor	Core	View, create, edit, and insert files with line-precise SEARCH/REPLACE operations
Phone Control	Core	Control an Android phone via ADB — tap, swipe, type, screenshots
Browser Control	Core	Full Playwright browser automation with persistent daemon
Testing & Debugging	Core	Structured bug investigation protocol (reproduce → isolate → fix)
System Repair	Core	Arch Linux + Hyprland crash recovery with inline diagnostic protocol
Background Commands	Core	Long-running builds, servers, and downloads with PID tracking
File Uploader	Core	Load PDFs, images, DOCX, PPTX, and other files into context
SVG Creator	Visuals	Data structure visualizations and node diagrams
Graph Master	Visuals	Mathematical function plots with labeled Cartesian axes
Simulacra Engine	Visuals	Interactive, animated HTML/JS simulations
Frontend Design	Visuals	Production-grade UI components and layouts
Study Suite	Study	Flashcards, Anki decks, practice problems, cheat sheets
Document Skills	Data	Create and edit DOCX, PDF, PPTX, XLSX files
Online Search	Data	Two-phase web search (search → fetch pages)
HTTP Client	Data	API testing with env presets and multi-step request chains
Video Downloader	Data	Download videos or extract audio from YouTube, Twitter, Instagram

Each skill has its own instruction.md protocol in skills/<category>/<key>/instruction.md. See the Skill Development section below for adding new skills.

Project Structure
text

Sable/
├── server.py                    # FastAPI server (SSE streaming, REST API, auth, SQLite)
├── pyproject.toml               # Project metadata & uv dependencies
├── uv.lock                      # Locked dependency versions
├── sable.db                     # SQLite database (chats, messages, auto-created)
│
├── engine/                      # Core chat engine
│   ├── chat.py                  # CLI chat client
│   ├── service.py               # FastAPI-friendly async service layer
│   ├── session.py               # Playwright browser session + Qwen auth
│   ├── config.py                # Endpoints, models, thinking modes, paths
│   ├── payloads.py              # Request body builder for Qwen API
│   ├── skills.py                # Skill tag parser + all 16 skill handlers
│   ├── memory_search.py         # Semantic memory search via fastembed
│   ├── scraper.py               # Web scraper engine manager
│   └── browser_opener.py        # Manual Qwen login helper
│
├── instruction/                 # System prompt & formatting rules
│   ├── Maria.md                 # Persona definition (YOU edit this)
│   ├── Maria.md.example         # Example persona template
│   ├── output_format.md         # Output formatting rules
│   └── skills.md                # Skill usage guidelines
│
├── skills/                      # Skill registry (one folder per skill)
│   ├── registry.json            # Master skill index
│   ├── core/                    # System-level skills
│   ├── data/                    # Web & document skills
│   ├── study/                   # Study & learning skills
│   └── visuals/                 # Diagram & simulation skills
│
├── Brain/                       # Persistent memory store
│   ├── Memory.json              # Semantic/episodic/procedural memories
│   ├── Memory.json.example      # Template with sample entries
│   └── Protected.json           # Protected memories (never surfaced to model)
│
├── web/                         # Frontend (vanilla JS PWA)
│   ├── index.html               # Main SPA shell
│   ├── app.js                   # Chat UI, SSE client, skill event cards
│   ├── styles.css               # All styles
│   ├── sw.js                    # Service worker for offline/PWA support
│   └── manifest.json            # PWA manifest
│
├── output/                      # Generated content (gitignored)
│   ├── notes/                   # Agent-created notes
│   ├── assets/                  # Generated SVGs, images
│   └── sessions/                # Session logs
│
├── uploads/                     # Uploaded file storage
├── browser-data/                # Persistent Chromium profile (gitignored)
├── browser-scraper-data/        # Scraper's Chromium profile (gitignored)
├── .sable_backups/              # File backups before edits (auto-created)
└── test/                        # Test suite & benchmarks
Configuration
Models & Thinking Modes

Models and their thinking modes are defined in engine/config.py:

Model ID	Label	Thinking Modes
qwen3.8-max-preview	Qwen3.8 Max Preview	
qwen3.7-max	Qwen3.7 Max	Fast, 
qwen3.7-plus	Qwen3.7 Plus	Fast, Auto, 

To add a new model, add an entry to the MODELS list in engine/config.py. It'll appear in the UI automatically.

Auth Token

The web UI requires a bearer token to access the API. Default is sable. Change it via:

File (persistent): Write your token to .auth_token in the project root

Env var (temporary): export SABLE_TOKEN=your-token

Memory Search Settings

The embedding model and similarity thresholds are stored in memory_search_settings.json and can be changed at runtime via the /api/memory-search/settings endpoint.

Web UI

Open http://localhost:8000 after starting the server. You'll see a login screen — enter your auth token (sable by default, or whatever you set).

Features

Chat interface — Real-time SSE streaming with thinking/answer phase separation

Model switcher — Change models mid-conversation (creates a new chat session)

 mode selector — Fast, Auto, or Thinking depending on the model

Chat history — All conversations persisted in SQLite, browsable in sidebar

Skill event cards — File edits, command outputs, and tool results rendered inline

File upload — Drag-and-drop images/PDFs into chat

PWA support — Install as a standalone app via manifest.json + service worker

The UI is a vanilla JS SPA — no frameworks. SSE events are parsed and rendered incrementally with DOM manipulation.

API Endpoints
Method	Path	Description
POST	/api/login	Authenticate with bearer token
GET	/api/health	Health check (no auth)
GET	/api/models	List available models & thinking modes
GET	/api/chats	List all chat sessions
POST	/api/chats	Create a new chat
DELETE	/api/chats/{id}	Delete a chat and its messages
GET	/api/messages/{chat_id}	Get all messages for a chat
POST	/api/chat	Send a message (supports stream: true/false)
GET	/api/chat/stream/{chat_id}	SSE stream for a chat
POST	/api/upload	Upload a file (image, PDF, etc.)
GET	/api/logs	Live server logs via SSE
POST	/api/file/revert	Restore a file from .sable_backups/
GET	/api/skills	List all registered skills
GET	/api/skills/browse	Skills with full instruction content
GET	/api/memory-search/settings	Get memory search config
POST	/api/memory-search/settings	Update memory search config
POST	/api/scraper/settings	Update scraper config
GET	/api/scraper/engines	List scraper engines
Skill Development

Skills live in skills/<category>/<key>/ with this structure:

text

skills/core/my_skill/
├── instruction.md     # Skill protocol (loaded into context when triggered)
└── scripts/           # Python/JS scripts the skill invokes
    └── my_tool.py

To register a new skill:

Create the folder and instruction.md

Add an entry to skills/registry.json:

json

{
  "name": "My Skill",
  "key": "my_skill",
  "category": "core",
  "path": "core/my_skill",
  "description": "What it does",
  "triggers": ["When to activate this skill"]
}

Add the skill's XML tag to KNOWN_TAGS in engine/skills.py if it introduces a new tag

Implement the handler function handle_<tag_name>() in engine/skills.py

Handlers are generators that yield skill_output and skill_end events — the server streams these directly to the UI.

CLI Chat Client

A standalone terminal client is available for quick testing without the web UI:

bash

uv run python engine/chat.py

Commands:

/image <path> [prompt] — Upload and chat about an image

/model <id> — Switch models (starts a new chat session)

/models — List available models

quit — Exit

Troubleshooting
Problem	Fix
Server won't start — port 8000 in use	kill $(lsof -t -i:8000) then retry
"Could not create chat session"	Re-run python engine/browser_opener.py to refresh Qwen auth
Rate limited (429 / rate_limited event)	Wait the specified hours; Qwen free tier has daily limits
Playwright browser won't launch	uv run playwright install chromium
Skills fail with timeout	Increase DEFAULT_TIMEOUT in engine/skills.py
Memory search returns nothing	Ensure Brain/Memory.json exists and has entries
UI shows "Unauthorized"	Check auth token — default is sable, or use SABLE_TOKEN env var
Development
bash

# Run the test suite
uv run python test/test_editor_tools.py
uv run python test/test_browser_control.py

# Run with debug logging
LOG_LEVEL=DEBUG uv run python server.py

# Lint & type-check (if tools are installed)
uv run ruff check .
uv run mypy engine/

