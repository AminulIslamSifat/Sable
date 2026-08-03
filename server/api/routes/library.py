
"""Library endpoints — browse generated content (agents, research, notes, gallery, skills)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter

from engine.config import (
    AGENT_OUTPUT_DIR,
    RESEARCH_DIR,
    NOTES_DIR,
    SKILLS_JSON_PATH,
)
from server.config import UPLOAD_DIR

router = APIRouter()

# ── Helpers ──────────────────────────────────────────────────────────────────

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".svg", ".webp", ".gif"}


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Extract YAML-ish frontmatter fields (simple key: value pairs)."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    fields: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            fields[key.strip().lower()] = val.strip()
    return fields


def _preview(text: str, max_len: int = 180) -> str:
    """First meaningful paragraph after frontmatter."""
    body = _FRONTMATTER_RE.sub("", text)
    for line in body.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith(("#", "---", ">")):
            return stripped[:max_len] + ("…" if len(stripped) > max_len else "")
    return ""


def _scan_md_dir(directory: Path, exclude_suffix: str | None = None) -> list[dict[str, Any]]:
    """Scan a directory of .md files, parse frontmatter, return sorted list."""
    if not directory.exists():
        return []
    items: list[dict[str, Any]] = []
    for f in directory.glob("*.md"):
        if exclude_suffix and f.stem.endswith(exclude_suffix):
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except Exception:
            continue
        fm = _parse_frontmatter(text)
        items.append({
            "id": f.stem,
            "filename": f.name,
            "title": fm.get("title", f.stem.replace("_", " ").replace("-", " ").title()),
            "date": fm.get("date", ""),
            "tags": fm.get("tags", ""),
            "preview": _preview(text),
        })
    items.sort(key=lambda x: x["date"], reverse=True)
    return items


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/api/library/agents")
def library_agents() -> list[dict[str, Any]]:
    return _scan_md_dir(AGENT_OUTPUT_DIR, exclude_suffix="_conversation")


@router.get("/api/library/research")
def library_research() -> list[dict[str, Any]]:
    return _scan_md_dir(RESEARCH_DIR)


@router.get("/api/library/notes")
def library_notes() -> list[dict[str, Any]]:
    return _scan_md_dir(NOTES_DIR)


@router.get("/api/library/gallery")
def library_gallery() -> list[dict[str, Any]]:
    if not UPLOAD_DIR.exists():
        return []
    items: list[dict[str, Any]] = []
    for f in UPLOAD_DIR.iterdir():
        if f.suffix.lower() in _IMAGE_EXTS and f.is_file():
            items.append({
                "filename": f.name,
                "url": f"/system/uploads/{f.name}",
                "type": f.suffix.lstrip("."),
                "size": f.stat().st_size,
                "date": f.stat().st_mtime,
            })
    items.sort(key=lambda x: x["date"], reverse=True)
    return items


@router.get("/api/library/skills")
def library_skills() -> list[dict[str, Any]]:
    if not SKILLS_JSON_PATH.exists():
        return []
    try:
        data = json.loads(SKILLS_JSON_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    return data.get("skills", [])


@router.get("/api/library/read/{section}/{filename}")
def library_read_file(section: str, filename: str) -> dict[str, Any]:
    """Read full content of a library item for inline viewing."""
    dir_map = {
        "agents": AGENT_OUTPUT_DIR,
        "research": RESEARCH_DIR,
        "notes": NOTES_DIR,
    }
    if section == "gallery":
        # Gallery items served via static mount already
        return {"content": "", "url": f"/system/uploads/{filename}"}
    if section not in dir_map:
        return {"error": "unknown section"}
    target = dir_map[section] / filename
    # Prevent path traversal
    if not str(target.resolve()).startswith(str(dir_map[section].resolve())):
        return {"error": "invalid path"}
    if not target.exists() or not target.name.endswith(".md"):
        return {"error": "not found"}
    try:
        return {"content": target.read_text(encoding="utf-8")}
    except Exception:
        return {"error": "read failed"}
