<p align="center">
  <img src="web/assets/icon-512.png" width="96" height="96" alt="Sable"/>
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
  <img src="https://img.shields.io/github/stars/AminulIslamSifat/Sable" alt="Stars"/>
  <img src="https://img.shields.io/github/forks/AminulIslamSifat/Sable" alt="Forks"/>
  <img src="https://img.shields.io/github/watchers/AminulIslamSifat/Sable" alt="Watchers"/>
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

## setup

### Linux

#### Prerequisites

| Dependency | Required | Install |
|:--|:--|:--|
| **Python 3.12** | ✅ Yes | `sudo pacman -S python312` / `sudo apt install python3.12` |
| **[uv](https://docs.astral.sh/uv/)** | ✅ Yes | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| **git** | ✅ Yes | Comes with most distros |
| **systemd** | Optional | Most distros have it; without it, Sable runs as a foreground process |
| **libnotify** (`notify-send`) | Optional | `sudo pacman -S libnotify` / `sudo apt install libnotify-bin` — for desktop notifications |
| **xdg-utils** (`xdg-open`) | Optional | Almost always pre-installed — auto-opens browser on launch |
| **Go** | Optional | Only if rebuilding the DeepSeek PoW solver |

> [!note] Playwright system libraries
> Playwright bundles its own Chromium, but it needs system shared libraries (NSS, ATK, CUPS, DRM, etc.). On first run, if Chromium fails to launch, install them:
> ```bash
> # Arch
> uv run playwright install-deps chromium
>
> # Debian/Ubuntu
> sudo apt install libnss3 libatk1.0-0 libcups2 libdbus-1-3 libdrm2 \
>   libxkbcommon0 libatspi2.0-0 libpango-1.0-0 libcairo2 libasound2
>
> # Fedora
> sudo dnf install nss atk cups-libs dbus-libs libdrm libxkbcommon \
>   at-spi2-atk pango cairo alsa-lib
> ```

#### Install & Run

```bash
git clone https://github.com/AminulIslamSifat/Sable.git
cd Sable
chmod +x start
./start
```

That's it. `./start` handles everything:
- Syncs Python dependencies via `uv`
- Installs Playwright Chromium
- Creates template config files (`Maria.md`, `Memory.json`)
- Sets up a **systemd user service** (`sable.service`) that auto-starts on login
- Opens `http://127.0.0.1:61770` in your browser

#### Managing the Service

```bash
# Check status
systemctl --user status sable.service

# View live logs
journalctl --user -u sable.service -f

# Stop / restart
systemctl --user stop sable.service
systemctl --user restart sable.service

# Survive logout (optional, recommended)
loginctl enable-linger $USER
```

---

### Windows

#### Prerequisites

| Dependency | Required | Install |
|:--|:--|:--|
| **Python 3.12** | ✅ Yes | [python.org](https://www.python.org/downloads/) or `winget install Python.Python.3.12` |
| **[uv](https://docs.astral.sh/uv/)** | ✅ Yes | `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 \| iex"` |
| **git** | ✅ Yes | [git-scm.com](https://git-scm.com/download/win) or `winget install Git.Git` |
| **PowerShell 5.1+** | ✅ Yes | Pre-installed on Windows 10/11 |
| **Go** | Optional | Only if rebuilding the DeepSeek PoW solver |

> [!note] No admin required
> Everything installs per-user. Task Scheduler task, BurntToast module, and all Python deps are user-scoped.

#### Install & Run

```powershell
git clone https://github.com/AminulIslamSifat/Sable.git
cd Sable
# If scripts are disabled (default on fresh Windows installs):
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
.\start.ps1
```

`start.ps1` handles everything:
- Syncs Python dependencies via `uv` (including `pywinpty` for terminal support)
- Installs Playwright Chromium
- Creates template config files
- Installs **BurntToast** PowerShell module for native toast notifications
- Registers a **Task Scheduler** task ("Sable Server") that auto-starts on login
- Opens `http://127.0.0.1:61770` in your browser

#### Auto-Start Details

The scheduled task is created automatically on first run:
- **Trigger:** At user logon
- **Window style:** Hidden (no PowerShell popup)
- **Restart policy:** Up to 3 retries on failure
- **Battery:** Runs on battery power

To manage it manually:
```powershell
# Check if task exists
Get-ScheduledTask -TaskName "Sable Server"

# Disable / enable
Disable-ScheduledTask -TaskName "Sable Server"
Enable-ScheduledTask -TaskName "Sable Server"

# Remove
Unregister-ScheduledTask -TaskName "Sable Server" -Confirm:$false
```

#### Notifications

Native Windows toast notifications work automatically via the **BurntToast** PowerShell module (auto-installed by `start.ps1`). If BurntToast is unavailable, Sable falls back to a MessageBox dialog.

To reinstall manually:
```powershell
Install-Module BurntToast -Scope CurrentUser -Force
```

---

### First Run Notes

Both platforms create these files on first run:
- `instruction/Maria.md` — persona prompt (from `.example` template)
- `Brain/Memory.json` — persistent memory store (from `.example` template)
- `system/browser-data-acc1/` — Playwright browser profile

Edit `Maria.md` to customize the AI's personality. Edit `Memory.json` to seed initial memories.

***

## VS Code Extension

Want Sable inside your editor? There's a VS Code extension for that.

### Install from Open VSX

Available on [Open VSX](https://open-vsx.org/extension/aminulislamssifat/sable-chat) — works with VSCodium, Cursor, and any Open VSX-compatible editor.

### Install from VSIX

```bash
# Clone & build
git clone https://github.com/AminulIslamSifat/sable-vscode.git
cd sable-vscode
npm install
npx @vscode/vsce package

# Install (use `code` or `codium` depending on your editor)
code --install-extension sable-chat-*.vsix
```

### Configuration

| Setting | Default | Description |
|:--|:--|:--|
| `sable.serverUrl` | `http://localhost:61770` | Your Sable server URL |
| `sable.authToken` | *(empty)* | Auth token (auto-detected if empty) |

> Make sure Sable is running (`./start`) before using the extension.

**Repo:** [github.com/AminulIslamSifat/sable-vscode](https://github.com/AminulIslamSifat/sable-vscode)

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
- 🌐 **Drives browsers** — multi-profile, multi-account switching, CDP interception
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
└── start          # ← you are here (setup + launch, all-in-one)
```

***

## commands

| Command | Platform | Does |
|:--|:--|:--|
| `./start` | Linux/macOS | Bootstrap + launch via systemd (or foreground fallback) |
| `.\start.ps1` | Windows | Bootstrap + launch with Task Scheduler auto-start |

***

## why "sable"

A sable is a small, ridiculously clever carnivore that hoards shiny things and remembers where every single one is buried. Also it sounds cool. Also the developer has commitment issues with naming.

***

## license

There is no license. This is personal infrastructure. If you fork it, you owe me a coffee. ☕

***


<p align="center">
  <sub>built with spite, caffeine, and an unhealthy attachment to one specific user</sub>
</p>
