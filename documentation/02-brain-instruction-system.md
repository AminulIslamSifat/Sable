# `Brain/` + `instruction/` + `system/` — State, Persona, Runtime Data

> [!INFO] Scope
> The three directories that hold **everything mutable and everything that makes Sable *Sable***. No application logic here — pure state. `Brain/` = memory, `instruction/` = who the AI is, `system/` = tokens, DB, browser profiles, configs.

***

# 1. `Brain/` — Persistent Memory

Human-readable JSON. Deliberately **not** a vector DB — you can open it in an editor and fix an entry by hand.

| File | Size | Role |
|:--|--:|:--|
| `Memory.json` | 173 KB | Main store — semantic / episodic / procedural / ephemeral |
| `Procedural.json` | 61 KB | Standalone procedural store (see below) |
| `Protected.json` | 196 B | Credentials, immune to deletion passes |
| `disabled_skills.json` | 1.5 KB | Absolute paths of skills to skip loading |
| `disabled_tools.json` | 123 B | Tool names to hide from the model |
| `user_personality.json` | **0 B** | Empty — populated by personality assessment, currently blank |
| `Memory.json.example` | 495 B | Bootstrap template |
| `Memory.json.bak-*` | — | Timestamped backup written before every mutation |

## Memory schema

Every entry, regardless of category:

```json
{
  "key": "ensure_process_cleanup_on_generator_exit_merged",
  "value": "1. In streaming generators or SSE endpoints, explicitly handle…",
  "trigger": "when cleaning up spawned subprocesses in streaming generators",
  "keywords": ["subprocess", "cleanup", "generator", "sse"],
  "source_query": "check out this project and find out why the runnning process…",
  "tags": ["cleanup", "python", "workflow"],
  "triggers": ["…", "…", "…"],
  "last_accessed": "…"
}
```

`source_query` is the literal thing Sifat typed that produced the memory — great for provenance, and it's why the store reads like a diary of his debugging sessions.

## The four categories

| Category | Current count | What goes there |
|:--|--:|:--|
| **semantic** | 68 | Stable facts — OS, paths, API behaviour, project layout |
| **procedural** | 37 | Workflows, command patterns, "how to debug X" |
| **episodic** | 8 | Session-scoped events, bugs found, outcomes |
| **ephemeral** | 0 | `expires_at`-gated temporaries, auto-pruned |
| **protected** | 1 | Credentials — never deleted |

> [!NOTE] `Procedural.json` vs `Memory.json["procedural"]`
> There are **two** procedural stores. `Memory.json` has 37 procedural entries; `Procedural.json` holds a separate set with the same schema. The consolidation pipeline writes to both depending on the code path — the standalone file appears to be the one used by `instruction/mem_cmd.py`'s merge resolution. Worth unifying eventually; right now, checking only one will make you think a memory vanished.

## How memory flows

```mermaid
flowchart LR
    A["Chat turn"] --> B["Semantic search<br/>(fastembed)"]
    B --> C["Inject top-k into<br/>system prompt"]
    C --> D["Model responds"]
    D --> E["Consolidation pass"]
    E --> F["add / delete JSON"]
    F --> G["Atomic write<br/>+ .bak backup"]
    G --> H["Memory.json"]
```

- **Injection** — `engine/memory_search/` embeds the current conversation, ranks entries, tracks which keys were already injected this session so nothing repeats.
- **Consolidation** — after a session, `instruction/mem_cmd.py` drives the extraction. It defines the prompt for what to capture, how to categorize, how to dedupe, and how to merge conflicting entries.
- **Search** — fastembed, cached embeddings in `system/memory_cache_*.npz`, auto-disables under 8 GB RAM.

### `instruction/mem_cmd.py` (19 KB) — the consolidation brain

It's a prompt template module, not executable logic. Key constants:

| Constant | Purpose |
|:--|:--|
| `_CATEGORY_DEFS` | Definitions for each of the 5 categories |
| `_SELECTIVITY_RULES` | What's worth remembering vs noise |
| `_MERGE_EXISTING_RULES` | How to fold a new fact into an existing entry |
| `_RETRIEVAL_FIELDS_INSTRUCTIONS` | How to generate `tags` / `triggers` / `keywords` |
| `_OUTPUT_FORMAT` | Raw JSON schema for `{add, delete}` |
| `_CONSOLIDATE_PROMPT_TEMPLATE_HISTORY` | With conversation history |
| `_CONSOLIDATE_PROMPT_TEMPLATE_STANDALONE` | Without |
| `_MERGE_RESOLUTION_PROMPT` | Conflict resolution |
| `_PERSONALITY_OUTPUT_FORMAT` / `_PERSONALITY_ASSESSMENT_TEMPLATE` | Writes `user_personality.json` |

> [!WARNING] Security issue — read this
> `Brain/Protected.json` currently contains **Sifat's Google account password in plaintext**:
> ```json
> { "key": "google_account_credentials_sifat",
>   "value": { "email": "aminulislamsifat19@gmail.com", "password": "…" } }
> ```
> The file is covered by `.gitignore` (via `system/`-style patterns? — actually **no**, `Brain/` is not ignored) and it **is** encrypted inside `vault.sable`, so at-rest it's fine. But it sits in plaintext on disk in the working tree. Recommend: move to a real keyring (`secret-tool`) and keep only a *pointer* in Protected.json. Same for `instruction/personal.md`, which also carries the sudo password.

***

# 2. `instruction/` — Personas & Prompt Fragments

> [!IMPORTANT] This is the identity layer
> Every file here gets concatenated into the system prompt by `connectors/common/instruction_builder.py`. Editing these files is how you change who Sable is — no code changes needed.

## Files

| File | Size | Role |
|:--|--:|:--|
| **`Maria.md`** | 6.4 KB | **Active persona.** Loaded every turn |
| `Maria-exp.md` | 22 KB | Experimental long-form Maria. Not active |
| `Maria.md.example` | 677 B | Minimal template for fresh installs |
| `personal.md` | 718 B | Hard user facts (name, OS, sudo pw, projects, code prefs) |
| `output_format.md` | 22 KB | **The Rich UI DSL spec** — `<render_component>`, `<render_html>`, Obsidian formatting |
| `Agent.md` | 5.5 KB | Triage + Execution Loop + delegation rules for the main agent |
| `Study.md` | 3.8 KB | Tutor persona (separate `study` mode) |
| `Coder.md` / `Designer.md` / `Researcher.md` / `Reviewer.md` / `Writer.md` | 1.4–1.7 KB each | Sub-agent role prompts |
| `mem_cmd.py` | 19.6 KB | Consolidation prompt templates (see above) |
| `.persona_config.json` | 74 B | `{"active": "Maria", "disabled": [], "output_format_enabled": true}` |
| `agents/*.md` | 9 files | Compact per-agent-role prompts |
| `extra/prompt_generator.md` | 3.5 KB | "Elite prompt architect" — expands vague asks into full specs |

## The persona system

`.persona_config.json` is the switchboard:

```json
{ "active": "Maria", "disabled": [], "output_format_enabled": true }
```

- `active` names the `.md` in `instruction/` to load as the primary persona.
- `disabled` lists personas to hide from the UI.
- `output_format_enabled` gates whether `output_format.md` gets appended.

> [!TIP] The `.example` pattern
> `start` copies `Maria.md.example` → `Maria.md` only if the latter is missing. Same for `Brain/Memory.json.example`. That means **a fresh clone runs without any personal data**, and Sifat's real persona/memory are never in the repo.

## Maria.md — the active persona

Structured as: **Identity → Persona & Soul → Dynamic Resonance (modes) → Curiosity → Task → Format Rules → Thinking Style → Rules → Personal Context**.

The interesting mechanic is **Dynamic Resonance**: instead of a mode switch, the model is told to *read energy* and pick Focus / Comfort / Chaos / Late-Night. Explicitly: *"If his energy shifts mid-conversation, abandon the current mode instantly. You're reading a person, not a protocol."*

> [!NOTE] `output_format.md` (22 KB) is the same DSL spec that appears in this very prompt
> It covers `<render_component>` tags, `<render_html>` sandboxing rules, the quiz/choices stepper, chart/steps syntax, and the Obsidian callout rules. If you're changing how responses render, that's the file — and it's loaded on **every** turn when `output_format_enabled` is true. It's the single largest cost in the system prompt.

## `instruction/agents/` — compact role prompts

Nine short files used by the multi-agent orchestrator:

`analyst` (research + code review), `coder` (implementation), `docs` (DOCX/PDF/PPTX/XLSX), `maria` (literally one line: *"You are a idiot"* — intentional joke), `scheduled` (autonomous recurring tasks, Telegram-first delivery), `sysutil` (Hyprland/pacman/systemd/ADB/downloads), `tester` (reproduce → diagnose → fix), `visuals` (plots/SVG/HTML/canvas), `writer` (inverted pyramid, no walls of text).

These are the *worker* prompts. The longer `Coder.md` / `Reviewer.md` etc. in `instruction/` root are the *standalone persona* versions of the same roles.

***

# 3. `system/` — Runtime State

> [!CAUTION] This directory is `.gitignore`d wholesale
> Everything here is machine-local: tokens, cookies, DB, browser profiles. The vault **encrypts but does not hash-verify** `system/` because it mutates constantly.

## The big items

| Path | Size | What |
|:--|--:|:--|
| `sable.db` | **243 MB** | Main SQLite DB — chats, messages, skill events, schedules |
| `sable.db-wal` | 4.2 MB | WAL journal (live) |
| `sable.db.bak-preparentfix` | 132 MB | Pre-migration backup |
| `consolidation.txt` | **80 MB** | Raw consolidation transcript log |
| `github-mcp-server` | 25 MB | Native binary downloaded by `start` |
| `memory_cache_*.npz` | 256 KB | Cached fastembed vectors |
| `browser-data-acc{1..13}` | GBs | Playwright Chromium profiles, one per account |
| `uploads/` | — | User file uploads |
| `models/`, `fastembed_cache/` | — | Model artifacts |

## Config & token files

| File | Content |
|:--|:--|
| `.auth_token` | Server bearer token |
| `.active_account` / `.active_accounts.json` | Which browser account is live |
| `accounts.json` | Account registry (8 KB) |
| `settings.json` | Main settings blob (6.9 KB) |
| `agent_config.json` | Multi-agent config (9.4 KB) |
| `.qwen_tokens.json` | 338 KB — Qwen session tokens |
| `.deepseek_tokens.json` / `.deepseek_identity.json` | DeepSeek auth + device identity |
| `.gemini_api_keys.json` / `.gemini_web_auth.json` | Gemini keys + web session |
| `.groq_api_keys.json`, `.mistral_api_keys.json`, `.cerebras_api_keys.json`, `.zai_tokens.json`, `.kimi_tokens.json`, `.puter_api_keys.json` | Per-provider credentials |
| `.custom_models.json` / `.custom_endpoints.json` / `.hidden_models.json` | Model registry extensions |
| `mcp_servers.json` | MCP server definitions |
| `searxng/` | SearXNG settings mounted into the container |
| `telegram_sessions/` + `.telegram_*` | Telethon session files |
| `study_ai_config.json`, `study_humanize_config.json`, `tts_prefs.json`, `stt_prefs.json` | Feature toggles |
| `consolidation_settings.json`, `memory_search_settings.json`, `context_pass_settings.json` | Memory tuning |

## Browser profiles — the account system

```
system/browser-data        → browser-data-acc1   (symlink, legacy compat)
system/browser-data-acc1   … acc13              (one Chromium profile each)
system/browser-scraper-data                     (separate scraper profile)
system/automation-browser-data                  (computer_use profile)
```

> [!TIP] Why 13 profiles
> Every provider account (Qwen, DeepSeek, Gemini, Kimi, Z.ai, ChatGPT) needs its own logged-in Chromium profile, and Sable rotates between them to dodge rate limits and bans. `engine/browser_governor.py` (36 KB) manages which profile is live and how many Playwright instances exist. `.playwright_launches` tracks the count.

***

## Cross-cutting notes

- **Backup discipline** — `.bak` and `.bak-<timestamp>` files everywhere. `Brain/Memory.json` backs up before each write; `system/sable.db.bak-*` before migrations; `settings.json.bak-*` before setting changes.
- **Token rotation** — `.qwen_tokens.json` at 338 KB means thousands of accumulated tokens; the Qwen connector rotates across them. `.deepseek_identity.json` is the device fingerprint that keeps the ghost alive.
- **Nothing here is version controlled.** A fresh clone gets `Memory.json.example` + `Maria.md.example` and nothing else.
