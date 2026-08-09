
# Server — FastAPI Application & API Layer

The `server/` directory contains the FastAPI web application, all HTTP/WebSocket routes, database management, authentication, scheduling, and utility functions. It serves as the bridge between the frontend (`web/`) and the core engine (`engine/`), handling request routing, SSE streaming, WebSocket terminal sessions, and persistent storage.

***

## Top-Level Modules

### `app.py` (148B)
Application factory entry point. Creates and exports the FastAPI app instance used by `server.py` at the project root.

### `auth.py` (368B)
Authentication middleware/utilities for protecting API endpoints.

### `config.py` (2.1KB)
Server-specific configuration (distinct from `engine/config.py`). CORS settings, static file paths, upload limits, and runtime flags.

### `database.py` (32.1KB)
SQLite database layer using raw SQL with WAL mode. Manages all persistent data:

- **Chats & Messages** — conversation threads with metadata
- **Memory** — semantic/episodic/procedural/ephemeral entries
- **Schedules** — recurring tasks and reminders
- **Agent Operations** — multi-agent task tracking
- **TrackNotes** — notes and todo checklists
- **Library** — document and asset catalog

Uses connection pooling and prepared statements for performance. The database file lives at `system/sable.db` (~283MB).

### `logging_setup.py` (720B)
Configures Python logging for the server process.

### `models.py` (701B)
Pydantic models for request/response validation.

### `scheduler.py` (10.5KB)
Background task scheduler for recurring operations: memory consolidation, diary synthesis, schedule execution, cleanup tasks. Runs on an async event loop alongside the FastAPI server.

### `utils.py` (4.6KB)
Shared utilities: file helpers, path resolution, common response formatters.

***

## API Application (`api/`)

### `application.py` (9.4KB)
FastAPI app configuration: middleware stack, CORS, static file mounting, route registration, exception handlers, lifespan events (startup/shutdown hooks).

### `dependencies.py` (464B)
FastAPI dependency injection providers (database connections, auth checks).

***

## API Routes (`api/routes/`)

All routes are organized by domain. Each file is a FastAPI `APIRouter`.

### Chat & Conversation
| File | Size | Endpoints |
|:--|:--|:--|
| `chat.py` | 40KB | Message sending, SSE streaming, thinking modes, context injection, token refresh |
| `chats.py` | 12KB | Chat CRUD, listing, search, export, session management |

The chat route is the largest and most critical — it orchestrates the full message lifecycle from user input to streamed response.

### Agent Orchestration
| File | Size | Endpoints |
|:--|:--|:--|
| `agents.py` | 16KB | Spawn/status/kill agents, role management, todo tracking, notifications |

Handles multi-agent lifecycle, wave-based parallel execution, and progress reporting.

### Memory & Knowledge
| File | Size | Endpoints |
|:--|:--|:--|
| `memory.py` | 25KB | Memory CRUD, search, consolidation triggers, protected entry management |

Full REST API for the Brain memory system plus vector search endpoints.

### Settings & Configuration
| File | Size | Endpoints |
|:--|:--|:--|
| `settings.py` | 60KB | All settings: accounts, models, API keys, MCP servers, scraper config, themes, UI preferences |

The largest route file — handles every configurable aspect of Sable including account switching, model management, and credential storage.

### Integrations
| File | Size | Endpoints |
|:--|:--|:--|
| `email.py` | 15KB | IMAP/SMTP email client: folders, search, read, send, attachments |
| `telegram.py` | 16KB | Telegram messaging via Telethon: contacts, messages, media |
| `cookbook.py` | 18KB | Local model serving: download, serve, stop, hardware detection, presets |
| `research.py` | 4KB | Deep research session management and status |
| `deepseek.py` | 1KB | DeepSeek-specific endpoints (token extraction, PoW status) |

### Development Tools
| File | Size | Endpoints |
|:--|:--|:--|
| `filesystem.py` | 14KB | File tree browsing, folder picking, recent folders, quick access roots |
| `terminal.py` | 8KB | WebSocket PTY terminal: real shell via `os.forkpty()`, resize, session management |
| `upload.py` | 1KB | File upload handling for chat attachments |

### Content & Notes
| File | Size | Endpoints |
|:--|:--|:--|
| `library.py` | 5KB | Document/asset library browsing and search |
| `tracknote.py` | 9KB | Notes and todo CRUD, checklist toggling, schedule management |

### System
| File | Size | Endpoints |
|:--|:--|:--|
| `auth.py` | 402B | Login/password verification |
| `setup.py` | 3KB | First-run setup wizard endpoints |
| `scraper.py` | 1KB | Scraper engine status and control |
| `misc.py` | 4KB | Miscellaneous utilities (health check, version info, etc.) |

***

## How Server Connects to Engine

```
HTTP Request → FastAPI Route
    ↓
Import engine module (e.g., engine.service, engine.agents.loop)
    ↓
Call async engine function
    ↓
Stream/return result → HTTP Response / SSE / WebSocket
```

The server never implements business logic directly — it delegates to engine modules and handles only HTTP concerns (parsing, validation, streaming, error formatting).

***

## Database Schema

The SQLite database (`system/sable.db`) uses WAL mode for concurrent access. Key tables include chats, messages, memory entries, schedules, agent operations, tracknotes, and library items. Schema migrations are handled inline in `database.py` with version checks.

***

## Design Decisions

- **Raw SQL over ORM** — direct SQLite access for maximum performance and control
- **WAL mode** — concurrent reads during streaming writes without locking
- **Route-per-domain** — each file owns one feature area; no monolithic route file
- **Engine delegation** — server handles HTTP only; all logic lives in engine/
- **SSE for streaming** — server-sent events for real-time chat responses
- **WebSocket for terminal** — real PTY with bidirectional communication
