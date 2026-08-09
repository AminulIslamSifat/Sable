
# Engine — Core Logic & Runtime

The `engine/` directory is Sable's brain. It contains all core business logic: chat streaming, browser session management, skill execution, multi-agent orchestration, memory search, scraper engines, MCP integration, diary synthesis, security, research, cookbook model serving, and configuration. Nothing in `engine/` handles HTTP directly — that's `server/`'s job. Engine modules are imported by server routes and operate as pure async Python libraries.

***

## Top-Level Modules

### `config.py` (20.7KB)
Central configuration hub. Defines all model endpoints, API URLs, file paths, host/port settings, and the custom model registry. Every other module imports from here rather than hardcoding paths or URLs. Contains `get_model_config()` for resolving model-specific settings including `api_backend` routing.

### `chat.py` (9.0KB)
Qwen SSE streaming via httpx. Handles browser-session-authenticated requests to Qwen's API, auto token refresh on 401/403, and WAF token injection (`bx-ua`, `bx-umidtoken`). This is the non-scraper path for Qwen models.

### `service.py` (22.8KB)
ChatService orchestrator. Manages the persistent Chromium browser session, coordinates between `chat.py` (HTTP) and `scraper.py` (browser automation), handles streaming lifecycle, and provides the unified async interface that server routes consume.

### `session.py` (19.1KB)
Playwright BrowserManager. Handles header sniffing from live Chromium requests, WAF capture, Aliyun OSS image upload with STS token flow, DeepSeek token extraction from localStorage, and browser profile lifecycle management.

### `scraper.py` (34.3KB)
Browser scraper orchestrator. Dynamically loads engine modules from `scraper_engines/`, manages browser lifecycle (launch, probe existing CDP sessions, avoid duplicates), and exposes the same async interface as the HTTP ChatService. The largest single file in the engine.

### `memory_search.py` (19.7KB)
Vector search over `Brain/Memory.json` using fastembed. Configurable top-k, similarity thresholds, max prompt chars. Auto-disables on systems with <8GB RAM. Manages embedding caches in `system/memory_cache*.npz`. Provides the semantic search that powers memory injection.

### `payloads.py` (2.5KB)
Request payload builders for various API formats. Constructs properly formatted message objects for different backends.

### `browser_opener.py` (4.0KB)
Utility for launching and managing browser instances. Handles profile selection, symlink switching for account management, and browser-data directory setup.

### `account_login.py` (1.6KB)
Account login flow handler. Manages the initial browser-based authentication for Qwen/DeepSeek sessions.

***

## Subdirectories

### `agents/` — Multi-Agent Orchestration
Hub-and-spoke architecture for parallel agent execution.

| File | Size | Purpose |
|:--|:--|:--|
| `loop.py` | 38KB | Main agent execution loop — message handling, tool dispatch, turn management |
| `runtime.py` | 20KB | Agent runtime environment — context isolation, skill scoping, model routing |
| `registry.py` | 15KB | Role definitions, allowed/default skills per role, model assignments |
| `resilience.py` | 12KB | Circuit breakers, loop detection, consecutive/total call limits |
| `teacher.py` | 13KB | Teacher escalation — routes difficult subtasks to more capable models |
| `auto_turn.py` | 9KB | Auto-turn signaling — agent completions trigger frontend rendering |
| `agent.py` | 7KB | Individual agent instance management |
| `notifications.py` | 2KB | Per-chat async notification queue |
| `protocol.py` | 2KB | Agent communication protocol definitions |
| `decomposer.py` | 1KB | Heuristic pre-filter suggesting when messages benefit from parallel agents |

**Roles**: analyst, coder, writer, sysutil, docs, visuals, tester (7 roles). Up to 5 concurrent agents with wave-based parallel execution.

### `skills/` — Skill Engine
Runtime skill execution framework.

| File | Size | Purpose |
|:--|:--|:--|
| `parser.py` | 12KB | Streaming XML tag extraction within `<action>` blocks, malformed tag recovery |
| `engine.py` | 10KB | Orchestrates dispatch, emits SSE events for tool execution |
| `middleware.py` | 6KB | Validation → Permission → Execution → Logging pipeline |
| `registry.py` | 6KB | Discovery, validation, priority-based tag ownership resolution |
| `bg_jobs.py` | 4KB | Namespaced background process tracking with log files |
| `events.py` | 3KB | SSE event formatting for skill execution progress |
| `handlers/` | — | Tag handler implementations (execute, file_ops, io, web, mcp) |

### `mcp/` — Model Context Protocol Client
Full MCP client implementation.

| File | Size | Purpose |
|:--|:--|:--|
| `manager.py` | 12KB | Spawns MCP servers as stdio subprocesses, tool discovery, auto-routing |
| `handler.py` | 4KB | Bridges worker threads to main event loop via `run_coroutine_threadsafe` |

Features: `call_tool_auto()` finds which server owns a tool, generates system prompt sections listing connected tools, persists config in `system/mcp_servers.json`.

### `diary/` — Session Summarization
Gemini-powered reflective session synthesis.

| File | Size | Purpose |
|:--|:--|:--|
| `gemini_helpers.py` | 4KB | Multi-API-key rotation with persistent state tracking |
| `summarizer.py` | 2KB | Summarizes individual session logs via Gemini API |
| `synthesizer.py` | 2KB | Merges per-session summaries into cohesive diary entries |

Uses `gemini-3.1-flash-lite` with configurable temperature. Handles up to 900K chars input.

### `security/` — Safety & Guardrails

| File | Size | Purpose |
|:--|:--|:--|
| `prompt_guard.py` | 8KB | Prompt injection detection and sanitization |
| `middleware.py` | 3KB | Security middleware for request/response filtering |

Enforces write-path restrictions (allowed roots: project dir, HDD, /tmp). Blocks unauthorized file operations.

### `scraper_engines/` — Browser Automation Engines
Two dedicated CDP-based browser engines for driving live chat interfaces:

- `qwen/qwen_engine.py` (103KB) — Drives chat.qwen.ai
- `deepseek/deepseek_engine.py` (50KB) — Drives chat.deepseek.com

Capabilities: Chrome/Thorium/Playwright launch, WebSocket proxy for CDP message interception, DOM mutation observer response capture, clipboard fallback, instruction injection with SHA256 hash tracking, thought expansion automation, session markdown archival, CSS injection, headless/headed mode switching.

### `search/` — Web Search Engine
Multi-provider web search with ranking and caching.

| File | Size | Purpose |
|:--|:--|:--|
| `providers.py` | 15KB | Search provider implementations (multiple engines) |
| `core.py` | 9KB | Search orchestration and result aggregation |
| `query.py` | 5KB | Query parsing and transformation |
| `ranking.py` | 5KB | Result scoring and re-ranking |
| `config.py` | 5KB | Search configuration and provider settings |
| `cache.py` | 2KB | Search result caching |

### `research/` — Deep Research Engine
Multi-step research orchestration with LLM-driven analysis.

| File | Size | Purpose |
|:--|:--|:--|
| `engine.py` | 41KB | Core research loop — query decomposition, source gathering, synthesis |
| `manager.py` | 12KB | Research session management and state tracking |
| `llm.py` | 7KB | LLM abstraction for research tasks (routes to appropriate backend) |

Routes LLM calls by backend: API models use connectors, Qwen models use browser session with account fallback.

### `cookbook/` — Local Model Serving
Model download, serving, and hardware detection.

| File | Size | Purpose |
|:--|:--|:--|
| `hardware.py` | 22KB | GPU/RAM detection, capability assessment |
| `downloader.py` | 16KB | Model download with progress tracking |
| `server.py` | 9KB | Local model server management (start/stop/status) |
| `model_settings.py` | 5KB | Model-specific serving parameters |
| `state.py` | 5KB | Persistent state for downloads and served models |
| `presets.py` | 3KB | Pre-configured serving presets |
| `diagnose.py` | 3KB | Hardware/model compatibility diagnostics |

***

## Data Flow

```
User Message → server/api/routes/chat.py
    ↓
engine/service.py (ChatService)
    ├── Qwen HTTP → engine/chat.py → Qwen API
    ├── Qwen Scraper → engine/scraper.py → scraper_engines/
    ├── API Backend → connectors/ → DeepSeek/Gemini/Groq/Mistral
    └── Local → connectors/local/ → Ollama/vLLM/etc.
    ↓
Response Stream → SSE → Frontend
    
Tool Tags Detected → engine/skills/parser.py
    ↓
engine/skills/middleware.py → engine/skills/handlers/
    ↓
Results → SSE Events → Frontend
```

***

## Design Decisions

- **Pure async library** — no HTTP handling; server routes import engine functions
- **Unified interface** — ChatService abstracts away whether responses come from HTTP, scraper, or API connector
- **Lazy loading** — scraper engines, connectors, and MCP servers load on demand
- **Resilience built-in** — circuit breakers, loop detection, token exhaustion tracking
- **Separation of concerns** — each subdirectory owns one domain; cross-cutting via imports only
