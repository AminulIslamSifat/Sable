#!/usr/bin/env python3
"""build_packages.py — build per-platform release zips for Sable.

Each zip contains the full runtime tree, minus build/dev cruft, and ONLY the
solver binary (connectors/solver/) matching that platform.

Usage:
    python3 tools/build_packages.py                 # build all 3, version from pyproject.toml
    python3 tools/build_packages.py --version 1.9.2
    python3 tools/build_packages.py --out /path/to/Sablem/releases
"""

from __future__ import annotations

import argparse
import os
import sys
import tomllib
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# ── What ships ────────────────────────────────────────────────
# Top-level entries copied into every package.
INCLUDE_TOP = [
    "engine",
    "server",
    "connectors",
    "web",
    "skills",
    "instruction",
    "Brain",
    "tools",
    "telegram_bot",
    "tests",
    "pyproject.toml",
    "uv.lock",
    ".python-version",
    "README.md",
    "server.py",
    "start",
    "start.bat",
    "start.ps1",
    "start_shortcut.ps1",
    "sable-launch.sh",
    "render.yaml",
    ".gitignore",
]

# Directory names pruned anywhere in the tree.
EXCLUDE_DIRS = {
    ".git",
    ".venv",
    ".pytest_cache",
    ".sable_backups",
    ".editor_tools_backups",
    "__pycache__",
    "node_modules",
    "system",          # runtime: browser data, tokens, DB
    "output",          # generated content
    "test",            # scratch
    "cache",           # engine/scraper/diagnostics/cache (build stage)
}

# File names / suffixes never shipped.
EXCLUDE_SUFFIXES = (".pyc", ".pyo", ".db", ".db-shm", ".db-wal", ".sqlite", ".sqlite3", ".log", ".pid")
EXCLUDE_FILES = {
    "Maria.md",         # personal persona
    "personal.md",      # personal config
    "Memory.json",      # personal memory
    "Protected.json",
    "Procedural.json",
    "user_personality.json",
    "disabled_tools.json",
    "disabled_skills.json",
}

# solver binaries per package: platform key -> list of binary filenames
SOLVER_BY_PLATFORM = {
    "linux":   ["pow-solver-linux-amd64", "pow-solver-linux-arm64"],
    "windows": ["pow-solver-windows-amd64.exe"],
    "macos":   ["pow-solver-darwin-arm64"],
}

PLATFORM_TAG = {"linux": "linux", "windows": "windows", "macos": "macos"}


def get_version() -> str:
    with open(REPO / "pyproject.toml", "rb") as f:
        return tomllib.load(f)["project"]["version"]


def should_skip(path: Path, rel: Path) -> bool:
    if any(part in EXCLUDE_DIRS for part in rel.parts):
        return True
    if path.name in EXCLUDE_FILES:
        return True
    if path.suffix in EXCLUDE_SUFFIXES:
        return True
    # .example files are always kept (they're the templates)
    if path.name.endswith(".example"):
        return False
    return False


def build_one(platform: str, version: str, out_dir: Path) -> Path:
    solver_names = SOLVER_BY_PLATFORM[platform]
    solver_dir = REPO / "connectors" / "solver"

    for s in solver_names:
        if not (solver_dir / s).exists():
            sys.exit(f"✖ missing solver binary for {platform}: {s}")

    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"sable-{version}-{PLATFORM_TAG[platform]}.zip"

    written = 0
    total_bytes = 0

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for top in INCLUDE_TOP:
            src = REPO / top
            if not src.exists():
                continue
            if src.is_file():
                zf.write(src, top)
                written += 1
                continue

            for root, dirs, files in os.walk(src):
                root_p = Path(root)
                rel_root = root_p.relative_to(REPO)

                # prune dirs in-place so os.walk doesn't descend
                dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]

                for fn in files:
                    fp = root_p / fn
                    rel = fp.relative_to(REPO)

                    # ── solver: only this platform's binaries ──
                    if rel.parts[:2] == ("connectors", "solver"):
                        if fn not in solver_names:
                            continue
                    elif should_skip(fp, rel):
                        continue

                    zf.write(fp, str(rel))
                    written += 1
                    try:
                        total_bytes += fp.stat().st_size
                    except OSError:
                        pass

    size_mb = zip_path.stat().st_size / (1024 * 1024)
    print(f"  ✓ {zip_path.name:<34} {written:>5} files  {size_mb:>6.1f} MB (raw {total_bytes/1024/1024:.1f} MB)")
    return zip_path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default=None)
    ap.add_argument("--out", default=str(REPO / "dist"))
    ap.add_argument("--platform", choices=["linux", "windows", "macos", "all"], default="all")
    args = ap.parse_args()

    version = args.version or get_version()
    out_dir = Path(args.out).expanduser().resolve()
    platforms = list(SOLVER_BY_PLATFORM) if args.platform == "all" else [args.platform]

    print(f"📦 Sable v{version} → {out_dir}")
    built = []
    for p in platforms:
        built.append(build_one(p, version, out_dir))

    print("\n✅ done")
    for b in built:
        print(f"   {b}")


if __name__ == "__main__":
    main()
