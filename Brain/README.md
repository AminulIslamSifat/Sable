
# Brain — Persistent Knowledge Store

The `Brain/` directory is Sable's long-term memory system. It stores all persistent knowledge as human-readable JSON files that can be edited by hand or programmatically via the memory consolidation pipeline. Unlike opaque vector databases, every entry is inspectable and editable directly.

***

## File Structure

| File | Purpose |
|:--|:--|
| `Memory.json` | Primary memory store with categorized entries (semantic, episodic, procedural, ephemeral) |
| `Protected.json` | Immutable credential/security entries that survive deletion passes |
| `skills.json` | Dynamically created skill definitions extracted from conversations |
| `Memory.json.example` | Template file used during `./init` to bootstrap a fresh Memory.json |
| `Memory.json.bak.*` | Timestamped backups created automatically before memory writes |

***

## Memory Categories

### Semantic
Factual knowledge about the user, projects, tools, and environment. Examples: OS configuration, project paths, API behaviors, dependency relationships.

### Episodic
Event-based memories tied to specific sessions or interactions. Examples: bugs found in a particular session, workarounds discovered, conversation outcomes.

### Procedural
Step-by-step workflows, command patterns, and how-to knowledge. Examples: deployment procedures, debugging sequences, configuration steps.

### Ephemeral
Time-bound entries with an `expires_at` field (ISO 8601). Temporary workarounds, version-specific hacks, active debugging notes. Automatically cleaned up after expiration.

### Protected
Credentials, passwords, API keys, sudo configs, and security-sensitive paths. **Immune to deletion** — the consolidation prompt explicitly forbids removing protected entries regardless of staleness. Stored separately in `Protected.json` but also referenced in the main memory schema.

***

## How Memory Works

### Injection
At the start of each chat turn, relevant memories are injected into the system prompt context. The injection uses semantic search (via `engine/memory_search.py`) to find entries matching the current conversation topic. Injected memory keys are tracked per session to prevent duplicates.

### Consolidation
After conversations, a consolidation pass scans the chat and extracts new facts worth remembering. The consolidation prompt (`instruction/mem_cmd.py`) defines:

- **What to capture**: architecture decisions, user preferences, bugs, API quirks, file paths, dependency relationships
- **Auto-classification**: entries are sorted into semantic/episodic/procedural/protected/ephemeral categories
- **Deduplication**: skips entries already present in the store
- **Deletion**: removes outdated or superseded entries (never protected ones)
- **Skill creation**: auto-generates new skills from recurring workflows

The output is raw JSON with `add` and `delete` fields, applied atomically to the memory store.

### Search
Vector search powered by **fastembed** (`engine/memory_search.py`). Configurable top-k, similarity thresholds, and max prompt characters. Auto-disables on systems with less than 8GB RAM. Cached embeddings stored in `system/memory_cache*.npz` for fast startup.

***

## Skills Store (`skills.json`)

Beyond static memory, the Brain also stores dynamically created skills. When the consolidation process identifies a recurring workflow worth preserving, it creates a skill entry with:

- `name`: kebab-case identifier
- `description`: what the skill does
- `trigger`: when to activate it
- `prompt`: full instruction text injected into context
- `created`: timestamp

These skills supplement the static skills in `skills/` and are personalized to the user's workflows.

***

## Design Decisions

- **Human-readable JSON** over vector DB — you can open Memory.json in any text editor and fix things manually
- **Backup-on-write** — every mutation creates a timestamped `.bak` file
- **Protected category** — credentials never get accidentally pruned by automated cleanup
- **Ephemeral expiration** — temporary notes self-clean instead of accumulating forever
- **Semantic search optional** — gracefully degrades on low-memory systems
