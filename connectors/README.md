
# Connectors — AI Backend Client Library

The `connectors/` directory implements client libraries for all supported AI model providers. Each connector follows a common protocol interface, enabling the chat engine to stream responses from any backend interchangeably. Connectors handle authentication, streaming, token rotation, and provider-specific quirks.

***

## Architecture

### Connector Protocol (`base.py`)

All connectors implement the `ConnectorProtocol` (runtime-checkable Protocol):

```python
class ConnectorProtocol(Protocol):
    async def stream_chat(message, *, model, thinking_mode, chat_id, ...) -> AsyncGenerator[dict]
    async def chat(message, *, model, thinking_mode, chat_id, ...) -> dict
    @property
    def is_available(self) -> bool
```

**Stream events** yield dicts with a `type` key:
- `{"type": "answer", "text": "..."}` — response tokens
- `{"type": "thinking", "text": "..."}` — reasoning/thinking tokens
- `{"type": "done", "parent_id": "..."}` — stream complete
- `{"type": "error", "message": "..."}` — error occurred

### Registry (`__init__.py`)

Lazy-loaded singleton registry that maps backend names to connector instances:

- `resolve_backend(model_id)` → returns `"deepseek"`, `"gemini"`, `"groq"`, `"mistral"`, `"local"`, or `None` (for Qwen/scraper models)
- `get_connector(backend, model_id=None)` → returns or creates the connector instance
- `is_backend_available(backend)` → checks if connector has valid credentials

Connectors are instantiated on first use and cached in `_registry`. The `local` backend requires `model_id` to resolve per-model endpoints.

***

## Provider Implementations

### DeepSeek (`connectors/deepseek/`)

| File | Size | Purpose |
|:--|:--|:--|
| `client.py` | 39KB | Full DeepSeek API client with PoW challenge solving |
| `upload.py` | 4.5KB | File upload support for DeepSeek conversations |
| `pow_solver/` | — | Go binary that solves DeepSeek's Proof-of-Work challenges |

**Key features:**
- Pure HTTP connector (no browser required for API calls)
- Auth token extracted from browser `localStorage` during login
- Each request solves a PoW challenge via compiled Go binary
- Streaming SSE response parsing
- Multi-token rotation from `.deepseek_tokens.json`

### Gemini (`connectors/gemini/`)

| File | Size | Purpose |
|:--|:--|:--|
| `client.py` | 24KB | Google Gemini API client via `google-genai` SDK |

**Key features:**
- Uses official `google-genai` Python SDK
- Multi-API-key rotation with persistent state tracking
- Configurable temperature per task
- Handles up to 900K character input context
- Used by both chat and diary system

### Groq (`connectors/groq/`)

| File | Size | Purpose |
|:--|:--|:--|
| `client.py` | 19KB | Groq API client for Llama/Mixtral models |

**Key features:**
- OpenAI-compatible API interface
- Multi-key rotation
- Streaming support
- Fast inference via Groq's LPU hardware

### Mistral (`connectors/mistral/`)

| File | Size | Purpose |
|:--|:--|:--|
| `client.py` | 22KB | Mistral AI API client |

**Key features:**
- OpenAI-compatible API interface
- Multi-key rotation from `.mistral_api_keys.json`
- Streaming support
- Supports Mistral's full model lineup

### Local (`connectors/local/`)

| File | Size | Purpose |
|:--|:--|:--|
| `client.py` | 6KB | Generic OpenAI-compatible local model client |

**Key features:**
- Connects to any OpenAI-compatible local endpoint (Ollama, llama.cpp, vLLM, etc.)
- Per-model endpoint resolution from custom model config
- Default endpoint: `http://127.0.0.1:8080/v1`
- Lightweight — no auth required for most local setups

### Common Utilities (`connectors/common/`)

| File | Purpose |
|:--|:--|
| `context_summarizer.py` | Summarizes conversation context when it exceeds model limits |
| `media.py` | Media handling utilities (image encoding, file preparation) |

Shared utilities used across multiple connectors to avoid duplication.

***

## How Connectors Integrate

### Chat Flow
1. User sends message → `server/api/routes/chat.py`
2. Route calls `resolve_backend(model_id)` to determine provider
3. If backend is `None` → uses Qwen browser session (`engine/chat.py`)
4. If backend found → `get_connector(backend)` returns the client
5. `connector.stream_chat()` yields SSE events back to the frontend
6. Events are forwarded via server-sent events to the web UI

### Token Management
- **Qwen**: Browser session cookies + WAF tokens in `.session_tokens.json`
- **DeepSeek**: Auth tokens from browser localStorage in `.deepseek_tokens.json`
- **Gemini/Groq/Mistral**: API keys in respective `.json` files with rotation
- **Local**: No auth typically needed

### Error Handling
- Connectors raise standard exceptions on auth failures
- The chat route catches errors and emits `{"type": "error"}` events
- Token exhaustion tracked in `.qwen_exhaustion.json` for Qwen accounts
- Circuit breakers in multi-agent prevent repeated failures

***

## Adding New Providers

1. Create `connectors/<provider>/` directory
2. Implement `client.py` with a `get_client()` factory function
3. Ensure the client satisfies `ConnectorProtocol`
4. Register in `connectors/__init__.py` → add import branch in `get_connector()`
5. Add model entries in `engine/config.py` with `api_backend: "<provider>"`
6. Store credentials in `system/.<provider>_api_keys.json`

***

## Design Decisions

- **Protocol over ABC** — duck typing allows flexibility; runtime_checkable enables isinstance checks
- **Lazy singletons** — connectors only initialize when first used, saving startup time
- **Separate PoW solver** — Go binary keeps Python dependencies clean and solves challenges faster
- **Common utilities** — shared logic in `common/` prevents cross-provider code duplication
- **Streaming-first** — all connectors prioritize streaming; non-streaming `chat()` is a convenience wrapper
