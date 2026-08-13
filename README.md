<p align="center">
  <img src="icon.svg" width="96" height="96" alt="Sable"/>
</p>

<h1 align="center">Sable</h1>

<p align="center">
  <em>An agent that makes you grow.</em> 🌱
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.12-blue?logo=python&logoColor=white" alt="Python 3.12"/>
  <img src="https://img.shields.io/badge/fastapi-0.115+-green?logo=fastapi" alt="FastAPI"/>
  <img src="https://img.shields.io/badge/frontend-vanilla_js-f7df1e?logo=javascript&logoColor=black" alt="Vanilla JS"/>
  <img src="https://img.shields.io/badge/uv-managed-purple?logo=astral" alt="uv"/>
  <img src="https://img.shields.io/badge/license-brainrot-orange" alt="License: brainrot"/>
</p>

<p align="center">
  <img src="https://img.shields.io/github/stars/AminulIslamSifat/Sable?style=social" alt="Stars"/>
  <img src="https://img.shields.io/github/forks/AminulIslamSifat/Sable?style=social" alt="Forks"/>
  <img src="https://img.shields.io/github/watchers/AminulIslamSifat/Sable?style=social" alt="Watchers"/>
  <img src="https://img.shields.io/github/commit-activity/m/AminulIslamSifat/Sable" alt="Commits/month"/>
  <img src="https://img.shields.io/github/last-commit/AminulIslamSifat/Sable" alt="Last commit"/>
  <img src="https://img.shields.io/github/repo-size/AminulIslamSifat/Sable" alt="Repo size"/>
  <img src="https://img.shields.io/github/contributors/AminulIslamSifat/Sable" alt="Contributors"/>
  <img src="https://img.shields.io/github/issues/AminulIslamSifat/Sable" alt="Issues"/>
</p>

***

## what even is this

Sable is a **self-hosted agentic platform** that lives on your machine and grows with you. Part AI companion, part dev environment, part chaos engine. It streams responses from Qwen, DeepSeek, Gemini, Groq, Mistral — no API keys needed for Qwen (it hijacks your browser session like a gremlin). It has 25 skills, multi-agent orchestration, persistent memory, a web IDE with Monaco, browser automation, and a diary system that writes about your day better than you ever could.

It remembers. It learns. It roasts your code. It *grows*.

> [!tip] the vibe
> Think: if a fox learned to code, got attached to one specific human, and refused to be just a chatbot.

***

## quick start

```bash
cd Sable
chmod +x start
./start
```

that's it. that's the whole thing. opens `http://127.0.0.1:61770` in your browser.

> [!note] first run?
> `./init` handles deps, Playwright browsers, and systemd service setup. Only needed once.

***

## prerequisites

| Thing | Why |
|:--|:--|
| **Python 3.12** | pinned exact (`3.12.13`), not sorry |
| **[uv](https://docs.astral.sh/uv/)** | package manager, fast af |
| **systemd** | optional — falls back to direct `uv run` |
| **Go** | only if rebuilding DeepSeek PoW solver |
| **Chromium** | Playwright installs it for you via `./init` |

***

## tech stack

| Layer | Tech |
|:--|:--|
| Backend | Python 3.12 · FastAPI · Uvicorn |
| Frontend | Vanilla JS PWA · Monaco Editor · xterm.js |
| AI Backends | Qwen (browser session) · DeepSeek (PoW) · Gemini · Groq · Mistral · Local |
| Browser Automation | Playwright · CDP · WebSocket proxy |
| Memory | JSON-based semantic vectors · fastembed |
| Database | SQLite (via `sable.db`) |
| Agents | Multi-agent hub-and-spoke, 7 roles, wave execution |
| Protocol | MCP (Model Context Protocol) client |
| Package Mgmt | uv + pyproject.toml |

***

## what it does (the fun list)

- 🧠 **Remembers everything** — semantic memory with vector search, auto-consolidation after every session
- 🤖 **Spawns sub-agents** — up to 5 parallel workers, 7 specialized roles
- 🌐 **Drives browsers** — 115+ profiles, multi-account switching, CDP interception
- 📝 **Writes documents** — PDF, DOCX, PPTX, XLSX generation
- 🔍 **Researches** — deep research mode with multi-source synthesis
- 📱 **Controls your phone** — ADB integration
- 📧 **Reads your email** — and your Telegram
- 🎨 **Makes visuals** — SVG, plots, physics sims, frontend designs
- 📓 **Keeps a diary** — Gemini-powered session reflection
- 🖥️ **Full web IDE** — Monaco editor, file tree, integrated terminal
- 🔧 **Repairs itself** — system repair skill, background commands
- 📥 **Downloads videos** — YouTube and friends
- 🗣️ **Talks** — TTS via Kokoro

***

## project structure

```
Sable/
├── engine/        # Core brain: chat, agents, skills, memory, MCP, security
├── server/        # FastAPI app, API routes, auth, scheduler
├── connectors/    # AI backends: DeepSeek, Gemini, Groq, Mistral, Local
├── web/           # Frontend PWA (vanilla JS, no framework, no mercy)
├── skills/        # 25 self-contained skill modules
├── Brain/         # Persistent memory (JSON you can actually read)
├── instruction/   # Persona prompts, formatting rules
├── system/        # Runtime: DB, browser profiles, configs
├── output/        # Generated content: notes, research, agent results
├── test/          # pytest suite
├── start          # ← you are here
├── init           # First-run setup
└── status         # Health check
```

***

## commands

| Command | Does |
|:--|:--|
| `./init` | Install everything, set up systemd service |
| `./start` | Launch Sable (systemd or direct) |
| `./status` | Check if it's alive |

***

## why "sable"

A sable is a small, ridiculously clever carnivore that hoards shiny things and remembers where every single one is buried. Also it sounds cool. Also the developer has commitment issues with naming.

***

## license

There is no license. This is personal infrastructure. If you fork it, you owe me a coffee. ☕

***

## star history

<p align="center">
  <a href="https://star-history.com/#AminulIslamSifat/Sable&Date">
    <img src="https://api.star-history.com/svg?repos=AminulIslamSifat/Sable&type=Date" alt="Star History Chart" width="100%"/>
  </a>
</p>

***

## activity

<p align="center">
  <img src="https://repobeats.axiom.co/api/embed/AminulIslamSifat/Sable.svg" alt="Repobeats analytics"/>
</p>

***

<p align="center">
  <sub>built with spite, caffeine, and an unhealthy attachment to one specific user</sub>
</p>
