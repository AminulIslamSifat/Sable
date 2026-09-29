# Sablem

> Release mirror for [Sable](https://github.com/AminulIslamSifat/Sable) — the zip packages live here.

This repo holds **built release packages** for Sable. It contains no application
source. The Sable server's built-in updater pulls the correct package from this
repo's GitHub Releases and installs it in place.

***

## Packages

Each release ships three zips, one per platform. Every package contains the full
runtime tree and **only** the solver binary for that platform.

| Package | Solver binary included | Platforms |
|:--|:--|:--|
| `sable-<version>-linux.zip` | `pow-solver-linux-amd64`, `pow-solver-linux-arm64` | Linux x86_64 / arm64 |
| `sable-<version>-windows.zip` | `pow-solver-windows-amd64.exe` | Windows x86_64 |
| `sable-<version>-macos.zip` | `pow-solver-darwin-arm64` | macOS (Apple Silicon) |

***

## Install

```bash
# Linux / macOS
unzip sable-<version>-linux.zip -d Sable && cd Sable && chmod +x start && ./start
```

```powershell
# Windows
Expand-Archive sable-<version>-windows.zip -DestinationPath Sable
cd Sable
.\start.bat
```

The `start` script installs Python, uv, Playwright, and everything else on first
run. The only prerequisite is `git` (for the MCP server binary).

***

## Updating

Run Sable, open **Settings → Updates**, and hit **Check for Updates**. The server:

1. Asks the GitHub API for the latest release on this repo
2. Downloads the zip matching the current OS
3. Extracts it over the install directory, preserving `system/`, `Brain/*.json`,
   `instruction/Maria.md`, and your `.venv/`
4. Runs `uv sync` and restarts

> [!NOTE]
> The updater never touches your personal data. Everything under `system/`,
> `output/`, and your local persona/memory files survives an update untouched.

***

## Building a release

The packaging script lives in the Sable source repo:

```bash
cd Sable-core
python3 tools/build_packages.py --out ../Sablem/releases
```

Then attach the three zips to a GitHub Release on this repo, tagged `v<version>`
(matching `version` in Sable's `pyproject.toml`). The updater reads that tag.

***

## Layout

```
Sablem/
├── README.md
└── releases/          # zips (gitignored — attach to GitHub Releases instead)
    ├── sable-1.9.1-linux.zip
    ├── sable-1.9.1-windows.zip
    └── sable-1.9.1-macos.zip
```

***

<p align="center"><sub>packages only — source lives in <a href="https://github.com/AminulIslamSifat/Sable">Sable</a></sub></p>
