# Sable

An agentic chat platform with multi-engine LLM support, autonomous agent capabilities, and a persistent memory system.

## Features

- **Multi-Engine Support** — Switch between LLM providers seamlessly
- **Autonomous Agents** — Observer-based architecture with sequential command execution
- **Persistent Memory** — Categorized Brain system (semantic/episodic/procedural) that persists across sessions
- **Skill Registry** — Extensible skill system with routing protocol for visuals, study tools, system tasks, and more
- **Web Interface** — Vanilla JS frontend with real-time chat and settings panel

## Quick Start

```bash
# Clone the repo
git clone <repo-url> && cd sable

# Install dependencies
uv sync

# Set up your persona
cp instruction/Maria.md.example instruction/Maria.md
# Edit Maria.md with your preferences

# Set up memory (optional)
cp Brain/Memory.json.example Brain/Memory.json

# Run the server
uv run server.py
```

## Project Structure

```
sable/
├── engine/          # LLM engine adapters and orchestration
├── instruction/     # Persona files (Maria.md, skills.md, output_format.md)
├── skills/          # Skill registry (visuals, study, core, data)
├── web/             # Frontend assets
├── Brain/           # Persistent memory storage
├── server.py        # FastAPI backend
└── pyproject.toml   # Dependencies (managed by uv)
```

## Configuration

- **Persona**: Edit `instruction/Maria.md` to customize the AI's personality and user context
- **Memory**: `Brain/Memory.json` stores categorized memories injected into the system prompt
- **Skills**: Each skill in `skills/` has its own `instruction.md` defining triggers and behavior

## Requirements

- Python ≥ 3.11
- [uv](https://docs.astral.sh/uv/) for dependency management
- Playwright browsers (`uv run playwright install`) for web scraping skills

## License

Private — not for public distribution.
