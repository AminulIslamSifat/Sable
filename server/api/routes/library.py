
"""Library endpoints — browse generated content (agents, research, notes, gallery, skills)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from engine.config import (
    AGENT_OUTPUT_DIR,
    ASSETS_DIR,
    RESEARCH_DIR,
    NOTES_DIR,
    PROMPTS_DIR,
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
        mtime = f.stat().st_mtime
        items.append({
            "id": f.stem,
            "filename": f.name,
            "title": fm.get("title", f.stem.replace("_", " ").replace("-", " ").title()),
            "date": fm.get("date", ""),
            "tags": fm.get("tags", ""),
            "preview": _preview(text),
            "mtime": mtime,
        })
    items.sort(key=lambda x: x["mtime"], reverse=True)
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


@router.get("/api/library/prompts")
def library_prompts() -> list[dict[str, Any]]:
    return _scan_md_dir(PROMPTS_DIR)


@router.get("/api/library/gallery")
def library_gallery() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()

    # Scan uploads directory
    if UPLOAD_DIR.exists():
        for f in UPLOAD_DIR.iterdir():
            if f.suffix.lower() in _IMAGE_EXTS and f.is_file():
                items.append({
                    "filename": f.name,
                    "url": f"/system/uploads/{f.name}",
                    "type": f.suffix.lstrip("."),
                    "size": f.stat().st_size,
                    "date": f.stat().st_mtime,
                })
                seen.add(f.name)

    # Scan generated assets directory (image gen output)
    if ASSETS_DIR.exists():
        for f in ASSETS_DIR.iterdir():
            if f.suffix.lower() in _IMAGE_EXTS and f.is_file() and f.name not in seen:
                items.append({
                    "filename": f.name,
                    "url": f"/assets/{f.name}",
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
        "prompts": PROMPTS_DIR,
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


# ── PDF import ───────────────────────────────────────────────────────────────

_IMPORT_SECTION_DIRS = {
    "research": RESEARCH_DIR,
    "notes": NOTES_DIR,
}


def _slugify(text: str, fallback: str = "imported") -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return (slug or fallback)[:60]


def _extract_pdf_text(raw: bytes) -> str:
    """Extract text from a PDF. Prefers PyMuPDF (better layout), falls back to pypdf."""
    # Primary: PyMuPDF
    try:
        import fitz  # type: ignore

        doc = fitz.open(stream=raw, filetype="pdf")
        pages: list[str] = []
        for page in doc:
            pages.append(page.get_text("text") or "")
        doc.close()
        text = "\n\n".join(p for p in pages if p.strip())
        if text.strip():
            return text
    except Exception:
        pass

    # Fallback: pypdf
    try:
        import io

        from pypdf import PdfReader  # type: ignore

        reader = PdfReader(io.BytesIO(raw))
        pages = [(page.extract_text() or "") for page in reader.pages]
        return "\n\n".join(p for p in pages if p.strip())
    except Exception:
        return ""


def _dedupe_path(target: Path) -> Path:
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    for i in range(2, 1000):
        candidate = target.with_name(f"{stem}-{i}{suffix}")
        if not candidate.exists():
            return candidate
    return target


@router.post("/api/library/import-pdf")
async def library_import_pdf(
    file: UploadFile = File(...),
    section: str = Form("notes"),
    title: str = Form(""),
) -> dict[str, Any]:
    """Convert an uploaded PDF into a markdown note/research item."""
    if section not in _IMPORT_SECTION_DIRS:
        raise HTTPException(status_code=400, detail="section must be 'notes' or 'research'")
    if not file.filename:
        raise HTTPException(status_code=400, detail="missing filename")
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="only PDF files are supported")

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="empty file")
    if len(raw) > 100 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="file too large (max 100 MB)")

    text = _extract_pdf_text(raw)
    if not text.strip():
        raise HTTPException(status_code=422, detail="no extractable text (scanned/image PDF?)")

    base_title = (title or "").strip() or Path(file.filename).stem
    out_dir = _IMPORT_SECTION_DIRS[section]
    out_dir.mkdir(parents=True, exist_ok=True)
    target = _dedupe_path(out_dir / f"{_slugify(base_title)}.md")

    frontmatter = (
        "---\n"
        f"title: {base_title}\n"
        f"source: {file.filename}\n"
        "type: pdf-import\n"
        "---\n\n"
    )
    target.write_text(frontmatter + text.strip() + "\n", encoding="utf-8")

    return {
        "imported": True,
        "section": section,
        "filename": target.name,
        "title": base_title,
        "chars": len(text),
    }

