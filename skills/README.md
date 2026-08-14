
# Skills — Self-Contained Capability Modules

The `skills/` directory contains 25 self-contained skill modules that extend Sable's capabilities. Each skill is an independent directory with a manifest, instruction document, and optional helper scripts. Skills are auto-discovered at startup and registered by the Skill Engine (`engine/skills/`).

***

## Skill Structure

Every skill follows this layout:

```
skills/<name>/
├── instruction.md    # Routing protocol + usage documentation (injected into context)
├── skill.json        # Manifest: name, key, version, tags, priority, scope
└── scripts/          # Optional helper scripts (Python, shell, etc.)
```

### skill.json Manifest

| Field | Description |
|:--|:--|
| `name` | Human-readable skill name |
| `key` | Unique identifier used in routing |
| `version` | Semantic version string |
| `category` | Classification: `core`, `data`, `visuals`, `study`, `media`, `device`, `meta` |
| `description` | One-line summary of what the skill does |
| `trigger` | When this skill should be activated |
| `not_this_if` | Negative routing — when NOT to use this skill |
| `tags` | XML tag names this skill owns (e.g., `view_file`, `spawn_agent`) |
| `priority` | Numeric priority for tag ownership conflicts (higher wins) |
| `scope` | Which agent roles can use this skill (`["*"]` = all) |
| `default` | Whether skill is enabled by default |
| `inline` | Whether skill instructions are injected inline vs as reference |

### instruction.md

Contains the full routing protocol, tag format specifications, examples, and rules. This file is loaded via `<get_file>` before using any skill and injected into the AI's context. It serves as both documentation and executable specification.

***

## Skill Registry

All 25 skills organized by category:

### Core Skills
| Skill | Key | Tags | Priority | Description |
|:--|:--|:--|:--|:--|
| Code Editor | `code_editor` | `view_file`, `edit_file`, `create_file`, `insert_file` | 100 | Native file I/O — the ONLY skill that mutates files on disk |
| Browser Control | `browser_control` | `execute_command` | 85 | Playwright daemon for browser automation and scraping |

### Data Skills
| Skill | Key | Tags | Priority | Description |
|:--|:--|:--|:--|:--|
| Email | `email` | — | — | IMAP/SMTP email client |
| Telegram | `telegram` | — | — | Telegram messaging via Telethon |
| Online Search | `online_search` | `execute_command` | 70 | Two-phase web search (query → fetch) |
| Deep Research | `deep_research` | — | — | Multi-part comparative research with synthesis |
| HTTP Client | `http_client` | — | — | Direct HTTP API calls without browser rendering |
| Document Skills | `document_skills` | `execute_command`, `get_file` | 65 | Create/edit DOCX, PDF, PPTX, XLSX |
| Study Suite | `study_suite` | — | — | Study material generation and organization |

### Visual Skills
| Skill | Key | Tags | Priority | Description |
|:--|:--|:--|:--|:--|
| Graph Master | `graph_master` | — | — | Mathematical plots and function graphs |
| SVG Creator | `svg_creator` | — | — | Data structure visualizations and diagrams |
| Frontend Design | `frontend_design` | — | — | UI component design and prototyping |
| Simulacra Engine | `simulacra_engine` | — | — | Physics simulations and interactive demos |

### System & Device Skills
| Skill | Key | Tags | Priority | Description |
|:--|:--|:--|:--|:--|
| System Repair | `system_repair` | — | — | OS diagnostics and repair workflows |
| Testing & Debugging | `testing_debugging` | — | — | Bug investigation and test failure analysis |
| Grep Search | `grep_search` | — | — | File content search and directory listing |
| Phone Control | `phone_control` | — | — | ADB-based Android device control |
| YouTube Downloader | `youtube_downloader` | — | — | Video/audio download from YouTube |

### Meta Skills
| Skill | Key | Tags | Priority | Description |
|:--|:--|:--|:--|:--|
| Multi-Agent | `multi_agent` | `spawn_agent`, `agent_status`, `kill_agent` | 90 | Parallel background agent orchestration |
| Ask User | `ask_user` | — | — | Structured user input during agent execution |
| File Uploader | `file_uploader` | — | — | Upload files to chat context |
| MCP | `mcp` | — | — | Model Context Protocol tool integration |
| Text Humanizer | `text_humanizer` | — | — | Convert AI-generated text to natural human style |
| TrackNote Manager | `tracknote_manager` | — | — | CRUD for notes, todos, and schedules |

***

## How Skills Work

### Discovery & Registration
At startup, `engine/skills/registry.py` scans all subdirectories for valid `skill.json` files. It validates manifests, resolves tag ownership by priority, and builds the routing table. Invalid skills are logged but don't crash the server.

### Routing
When the AI emits XML tags in an `<action>` block, the parser (`engine/skills/parser.py`) extracts them and routes to the owning skill based on tag name. Priority determines ownership when multiple skills claim the same tag.

### Execution Pipeline
Tags flow through a middleware pipeline (`engine/skills/middleware.py`):
1. **Validation** — schema check, required attributes
2. **Permission** — scope check against current agent role
3. **Execution** — dispatched to the appropriate handler
4. **Logging** — results recorded for audit

### Instruction Loading
Skills are **lazy-loaded** — their `instruction.md` is only read when explicitly requested via `<get_file>`. This keeps the base context lean. The system prompt includes a registry summary; full instructions are loaded on demand.

### Caching
Skill manifests and instructions are cached in `skills/_cache/` with mtime-based invalidation. Changes to `skill.json` or `instruction.md` are detected automatically without server restart.

***

## Creating New Skills

1. Create a new directory under `skills/`
2. Add `skill.json` with required fields (use existing skills as templates)
3. Write `instruction.md` with routing protocol and examples
4. Optionally add `scripts/` for helper utilities
5. Restart the server or wait for cache invalidation

The skill will be auto-discovered and registered on next load.

***

## Design Decisions

- **Self-contained directories** — each skill is independent, portable, and versionable
- **Manifest-driven routing** — declarative tag ownership prevents conflicts
- **Lazy instruction loading** — keeps base context small; loads full docs only when needed
- **Priority-based resolution** — higher-priority skills win tag conflicts (code_editor at 100 always wins file ops)
- **Scope filtering** — agents only see skills relevant to their role
- **Negative routing** (`not_this_if`) — prevents misrouting by explicitly stating when NOT to use a skill
