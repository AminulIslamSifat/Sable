# `connectors/` — AI Backend Clients

> [!INFO] Scope
> **18,733 lines across 59 files.** 20 provider directories plus a large `common/` shared layer. Provider internals are documented at the contract level; `base.py`, the registry, `openai_compat.py`, and `instruction_builder.py` were read directly.

***

## The contract — `base.py`

```python
@runtime_checkable
class ConnectorProtocol(Protocol):
    async def stream_chat(self, message, *, model=None, thinking_mode=None,
                          chat_id=None, ref_file_ids=None,
                          inject_instructions=True, **kwargs) -> AsyncGenerator[dict, None]
    async def chat(self, message, **kwargs) -> dict
    @property
    def is_available(self) -> bool
```

> [!IMPORTANT] Duck-typed, not inherited
> *"Duck-typing is fine — no need to inherit, but this Protocol documents the contract and enables type-checking."* Adding a connector means implementing three things, not subclassing.

### The event vocabulary

Every connector yields dicts with at minimum a `type` key:

| Event | Payload |
|:--|:--|
| `answer` | `{"text": "..."}` |
| `thinking` | `{"text": "..."}` |
| `done` | `{"parent_id": "..."}` |
| `error` | `{"message": "..."}` |

These are the raw connector events — `engine/response_normalizer.py` normalizes them into the canonical shape the skill engine consumes.

***

## The registry — `connectors/__init__.py`

```python
_registry: dict[str, Any] = {}          # api_backend name -> singleton
_remote_clients: dict[str, Any] = {}    # endpoint URL (or endpoint::model) -> client

def resolve_backend(model_id) -> str | None
def get_connector(backend, model_id=None) -> Any
def is_backend_available(backend) -> bool
```

`resolve_backend(model_id)` reads `get_model_config(model_id).get("api_backend")` — **`None` means it is a Qwen model** and the chat path uses `engine/service.py` instead.

### Lazy instantiation

Every branch in `get_connector()` is `if backend in _registry: return` then `from connectors.X.client import get_client; _registry[backend] = get_client()`. Nothing imports a provider module until that provider is actually used.

> [!TIP] Lazy imports are what keeps startup fast
> 20 provider modules, each pulling in httpx, browser helpers, or `curl_cffi`. Importing them all at boot would add seconds. The registry defers every one.

### The 18 registered backends

`deepseek`, `deepseek_scraper`, `qwen_scraper`, `kimi`, `chatgpt`, `zai`, `gemini`, `gemini_web`, `mimo`, `groq`, `mistral`, `mistral_web`, `claude_scraper`, `openai`, `opencode`, `local`, `cloudflare`, plus dynamic remote endpoints.

### Two special cases

**`local`** needs `model_id` to resolve its endpoint:

```python
endpoint = cfg.get("local_endpoint", "http://127.0.0.1:8080/v1")
if _is_remote_endpoint(endpoint):
    -> OpenAICompatClient   # cached by endpoint URL
else:
    -> connectors.local.client.get_client(endpoint, api_key)
```

> [!NOTE] `_is_remote_endpoint()` is a scheme + hostname test, not a string match
> `https` is always remote. Plain `http` is remote unless the host is `localhost`, `127.0.0.1`, `0.0.0.0`, `::1`, or empty. That is what routes a Cloudflare-hosted endpoint to `OpenAICompatClient` and a llama-server on `:8080` to the local connector.

**`cloudflare`** caches by `f"{endpoint}::{model_id}"` because *"each model needs its own client since `api_model_type` differs"* — but session history still persists per endpoint. It also sets `client.SUPPORTS_MULTIMODAL_CONTENT = False`.

> [!WARNING] `cloudflare` raises without a `model_id`
> `raise KeyError("Cloudflare backend requires model_id with local_endpoint configured")`. Unlike other backends, Cloudflare cannot be resolved from the backend name alone.

***

## `common/openai_compat.py` — 46 KB, the workhorse

By far the most reused connector. Every OpenAI-compatible provider is this one class, configured differently.

```python
_MAX_SESSION_CHARS = 100_000

class OpenAICompatClient:
    is_available        # property
    _load_keys() / _save_keys(keys)
    _current_key() / add_key(key) / remove_key(index) / list_keys() / _rotate_key()
    _load_instructions(project_id, layout_mode)
    async _get_http() -> httpx.AsyncClient
    _auth_headers(key)
    _tweak_payload(payload, model_id)
    _model_supports_thinking(model_id) -> static
    _resolve_reasoning_effort(thinking_mode) -> str | None
    _get_max_chars(chat_id) -> int
    _get_or_create_session(...)
    async _maybe_summarize(...)
    async _call_self_summarize(prompt, model_id) -> str | None
    async stream_chat(...)      # line 467
    async chat(...)             # line 947
```

### Multi-key rotation

```python
def _auto_rotate_enabled(provider: str) -> bool
```

Keys are stored per provider on disk. `_rotate_key()` advances on failure; `list_keys()` returns masked entries for the UI so the settings panel never sees a full key.

### History budgeting — the compression ladder

```python
def _msg_chars(msg) -> int
def _trim_history(...)
def _minimal_instructions() -> str
```

Three escalating stages when a conversation exceeds `_get_max_chars(chat_id)`:

| Stage | Action |
|:--|:--|
| 1 | `_trim_history()` — drop oldest turns |
| 2 | `_maybe_summarize()` — LLM-summarize the dropped span |
| 3 | `_minimal_instructions()` — strip the instruction block to essentials |

> [!IMPORTANT] `_call_self_summarize()` uses the model to summarize itself
> Stage 2 asks the *same* model to condense its own older turns. That is cheaper than a dedicated summarizer model and preserves the model's own vocabulary — but it costs one extra call before the real request.

`_MAX_SESSION_CHARS = 100_000` is the module default; `_get_max_chars(chat_id)` overrides per chat.

### Thinking-mode translation

```python
_model_supports_thinking(model_id)        # static predicate
_resolve_reasoning_effort(thinking_mode)  # -> str | None
```

> [!TIP] `reasoning_effort` is not universal
> Sable's internal `thinking_mode` ids (`fast`/`thinking`/`high`) get mapped to whatever the provider calls it — OpenAI's `reasoning_effort`, Groq's field, or omitted entirely. `_model_supports_thinking()` gates whether the field is sent at all; sending it to a model that does not accept it produces a 400.

### `_tweak_payload()`

A per-model hook for provider quirks — extra fields, removed fields, renamed keys. One method instead of a subclass per provider.

### `_get_or_create_session()`

Sessions are keyed by `chat_id` and held in memory, so `chat()` and `stream_chat()` share conversation state without the database being the source of truth.

***

## `common/instruction_builder.py` — 31 KB

> [!IMPORTANT] The single source of truth for system-prompt assembly
> *"This is the single source of truth for instruction assembly across all API connectors (DeepSeek, Gemini, Mistral), AND subagent roles. Qwen uses `session.py` directly."*\n
```python
def build_instructions(project_id=None, provider=None, agent_role=None,
                       agent_skills=None, layout_mode=None,
                       persona_override=None, custom_instructions=None) -> str
```

### The six tool formats

```python
available_tool_format_names() -> ["qwen_json", "hermes", "dsml", "mimo", "native", "none"]
```

| Format | Provider | Wrapper |
|:--|:--|:--|
| `qwen_json` | Qwen | `<action>` … `</action>` |
| `hermes` | generic | `<tool_call>` … `</tool_call>` |
| `dsml` | DeepSeek | `<｜DSML｜tool_calls>` / `<｜DSML｜invoke>` |
| `mimo` | MiMo | `<function=name><parameter=key>` |
| `native` | Gemini / Mistral | **no wrapper** — bare JSON array |
| `none` | OpenAI-style | native API function calling, no prompt format at all |

```python
def get_tool_format(provider)          -> str
def get_tool_format_name(provider)     -> str
def get_default_tool_format_name(p)    -> str    # ignores user overrides
def get_tool_format_tags(provider)     -> (open_tag, close_tag, example)
```

> [!NOTE] Empty `open_tag`/`close_tag` is meaningful
> *"Empty `open_tag`/`close_tag` means the format uses no text wrapper (bare JSON array for `native`, or no prompt format at all for `none`)."* `engine/skills/parser.py`'s bare-JSON path exists to handle exactly these two.\n
### Three instruction branches

| Branch | Trigger | Content |
|:--|:--|:--|
| **Subagent** | `agent_role` set | `instruction/agents/{role}.md` + `personal.md` + a Stuck Recovery block |
| **Persona override** | `persona_override` set | Study mode's own persona replaces the active one |
| **Main chat** | neither | Active persona from `.persona_config.json` + `personal.md` |

> [!TIP] Subagents get a Stuck Recovery block the main chat does not
> *"If you find yourself repeatedly failing the same tool call… the system will automatically request guidance from the orchestrator. You will receive a `[TEACHER GUIDANCE]` block with specific instructions — follow them exactly."* This primes the agent for `teacher.py`'s escalation, which would otherwise arrive as a surprise interruption.

### Injection toggles

```python
from connectors.common.prompt_injection import injection_enabled_map
_inj = injection_enabled_map(provider)
# _inj.get("persona", True)
```

Per-provider switches for persona, skills, memory, MCP. **Unknown or `None` provider defaults to all-on** — *"so new connectors are never silently starved of context until explicitly toggled off."*

### Caching

```python
def invalidate_cache()
def get_instruction_version() -> int
```

Instructions are cached with a version counter. `get_instruction_version()` lets a caller detect a change without re-reading every file.

### `layout_mode`

- `"chat"` — strips all tools/skills/MCP except web search and `chat_title`
- `"agent"` or `None` — full tool access

### `_filter_skills_prompt()`

Filters the skill registry block down to `agent_skills` when a subagent has a restricted skill set — so a `writer` agent does not see `phone_control` in its prompt.

`_SUBAGENT_BLOCKED_TOOLS = frozenset({"ask_user", "multi_agent"})` — subagents cannot ask the user questions or spawn further agents.\n
***

## The 20 provider directories

| Size | Provider | Transport |
|--:|:--|:--|
| 98 KB | `deepseek/` | HTTP + PoW solver (`pow_solver/` Go binary) + `ghost.py` |
| 40 KB | `zai/` | Browser-assisted (Aliyun captcha minting) |
| 36 KB | `kimi/` | Ghost tab, localStorage bearer |
| 35 KB | `chatgpt/` | Ghost tab |
| 30 KB | `gemini/` | API key |
| 30 KB | `deepseek/ghost.py` | In-page fetch |
| 25 KB | `mimo/` | Cookie-based (3 cookies) |
| 24 KB | `gemini_web/` | Cookie jar + `WIZ_global_data` |
| 24 KB | `mistral_web/` | Ory session cookies |
| 24 KB | `kimi/client.py` | |
| 19 KB | `chatgpt/client.py` | |
| 16 KB | `local/` | llama-server / vLLM |
| 16 KB | `deepseek_scraper/` | DOM scrape |
| 11 KB | `qwen_scraper/` | DOM scrape |
| 11 KB | `common/native_tools.py` | Native function-calling adapter |

Other directories: `claude_scraper/`, `cloudflare/`, `groq/`, `mistral/`, `openai/`, `opencode/`, `puter/`.\n
> [!NOTE] `deepseek/` is the biggest for a reason
> 98 KB in one client: PoW challenge solving (with a **Go binary** in `pow_solver/`), token rotation, session management, file upload, and a `ghost.py` sibling for the in-page path. It is the most battle-hardened provider because DeepSeek's anti-bot is the most aggressive.\n
***

## `common/` — the shared layer

| File | Size | Role |
|:--|--:|:--|
| `browser_scraper.py` | 65 KB | Generic Playwright scraper base |
| `openai_compat.py` | 46 KB | OpenAI-compatible client |
| `scraper_connector.py` | 38 KB | Scraper-backed connector |
| `profile_hub.py` | 35 KB | **Shared browser per profile dir** |
| `instruction_builder.py` | 31 KB | System prompt assembly |
| `native_tools.py` | 11 KB | Native function-calling adapter |
| `history_budget.py` | — | Char budgeting |
| `context_summarizer.py` | — | History compression |
| `prompt_injection.py` | — | Per-provider toggles |
| `jitter.py` | — | Anti-burst delays |
| `media.py` | — | Image/audio normalization |
| `error_ctx.py` / `browser_errors.py` | — | Error context (`[provider \| account]`) |

> [!IMPORTANT] `profile_hub.py` prevents a SingletonLock kill-war
> Multiple connectors (Kimi, DeepSeek) want a browser on the same profile directory. `ProfileHub.get_live(Path(dir))` lets them **share one Chromium and multiplex tabs**, instead of two processes fighting over `SingletonLock`. `engine/session.py`'s `_adopt_hub()` rides the same hub.

`jitter.py` is why `service.stream_events()` emits `qwen_delay_2.3s` status events — the same anti-burst delay logic shared between the HTTP and scraper paths.

***

## Gotchas

> [!WARNING] Five things that matter
> 1. **`resolve_backend()` returning `None` means Qwen**, not "broken". The chat path branches on it.
> 2. **`cloudflare` needs a `model_id`.** Every other backend resolves from its name alone.
> 3. **`_minimal_instructions()` is stage 3 of compression** — if you see a suspiciously short system prompt, history budgeting ran.
> 4. **Subagent prompts include a Stuck Recovery block** the main chat does not. That is intentional priming for teacher escalation.
> 5. **Unknown providers default to all injections on**, so a new connector is never silently context-starved.

