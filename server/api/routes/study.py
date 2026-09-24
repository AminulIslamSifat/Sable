"""Study mode — PDF library with extracted text, progress, and highlights.

Storage layout (see engine.config):
    ~/sable_output/library/pdfs/<doc-id>.pdf
    ~/sable_output/library/meta/<doc-id>.json

The PDF is the source of truth for rendering; the meta JSON holds all derived
"connected data" — per-page text, outline, progress, and highlights — so the
frontend never has to re-parse the PDF to draw the sidebar.
"""

from __future__ import annotations

import fcntl
import json
import logging
import math
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from engine.config import (
    LIBRARY_ACADEMICS_PATH,
    LIBRARY_META_DIR,
    LIBRARY_ORIG_DIR,
    LIBRARY_PDF_DIR,
    LIBRARY_TEXT_DIR,
    LIBRARY_THUMB_DIR,
)
from engine.library_ingest import make_thumbnail, thumb_path

from engine.library_ingest import (
    convert_to_pdf as _shared_convert_to_pdf,
    extract_pdf as _shared_extract_pdf,
)

router = APIRouter()
logger = logging.getLogger("sable.study")

_MAX_PDF_BYTES = 100 * 1024 * 1024  # 100 MB
_MAX_RECTS = 200  # highlight rectangles per selection
_META_VERSION = 1
_ACADEMICS_VERSION = 1

# Formats we accept. Anything not .pdf is converted to PDF via LibreOffice
# before entering the (page-based) reader pipeline.
_NATIVE_EXT = ".pdf"
_CONVERTIBLE_EXT = {
    ".docx", ".doc", ".pptx", ".ppt", ".odt", ".odp", ".ods", ".rtf",
    ".txt", ".csv", ".md", ".epub", ".xlsx", ".xls",
}
_ACCEPTED_EXT = {_NATIVE_EXT, *_CONVERTIBLE_EXT}
_CONVERT_TIMEOUT = 120  # seconds — LibreOffice cold-start is slow the first time


# ── Helpers ──────────────────────────────────────────────────────────────────


def _ensure_dirs() -> None:
    LIBRARY_PDF_DIR.mkdir(parents=True, exist_ok=True)
    LIBRARY_META_DIR.mkdir(parents=True, exist_ok=True)
    LIBRARY_ORIG_DIR.mkdir(parents=True, exist_ok=True)
    LIBRARY_TEXT_DIR.mkdir(parents=True, exist_ok=True)


def _ext_of(filename: str) -> str:
    return Path(filename or "").suffix.lower()


def _pdf_tool() -> str | None:
    """Locate the LibreOffice CLI. Returns None when unavailable."""
    return shutil.which("soffice") or shutil.which("libreoffice")


def _kill_process_group(proc: subprocess.Popen) -> None:
    """Kill a converter and every child it spawned (LibreOffice forks)."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except Exception:
            pass
    try:
        proc.wait(timeout=5)
    except Exception:
        pass


def _convert_to_pdf(raw: bytes, ext: str) -> bytes:
    """Thin delegate — shared with the Drive importer."""
    return _shared_convert_to_pdf(raw, ext)
def _slugify(text: str, fallback: str = "document") -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return (slug or fallback)[:60]


def _meta_path(doc_id: str) -> Path:
    return LIBRARY_META_DIR / f"{doc_id}.json"


def _pdf_path(doc_id: str) -> Path:
    return LIBRARY_PDF_DIR / f"{doc_id}.pdf"


def _valid_id(doc_id: str) -> bool:
    """Doc ids are uuid4 hex — reject anything else before touching the disk."""
    return bool(re.fullmatch(r"[0-9a-f]{32}", doc_id or ""))


def _read_meta(doc_id: str) -> dict[str, Any] | None:
    if not _valid_id(doc_id):
        return None
    path = _meta_path(doc_id)
    if not path.exists():
        return None
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    # Legacy records carried the page text inline — relocate it on first read.
    inline = meta.pop("page_text", None)
    if inline and not _text_path(doc_id).exists():
        try:
            _write_page_text(doc_id, inline)
        except Exception:
            pass
    return meta


def _write_meta(doc_id: str, meta: dict[str, Any]) -> None:
    _ensure_dirs()
    meta.pop("page_text", None)  # page text lives in its own file now
    meta["updated"] = time.time()
    target = _meta_path(doc_id)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    tmp.replace(target)


def _text_path(doc_id: str) -> Path:
    return LIBRARY_TEXT_DIR / f"{doc_id}.json"


def _write_page_text(doc_id: str, pages: list[Any]) -> None:
    """Persist the per-page text array to its own file (atomic replace)."""
    _ensure_dirs()
    target = _text_path(doc_id)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(pages, ensure_ascii=False), encoding="utf-8")
    tmp.replace(target)


def _load_page_text(meta: dict[str, Any]) -> list[str]:
    """Per-page text for a doc, from its own file (legacy inline as fallback)."""
    doc_id = str(meta.get("id") or "")
    if _valid_id(doc_id):
        path = _text_path(doc_id)
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return [str(t or "") for t in data]
            except Exception:
                pass
    legacy = meta.get("page_text")
    return [str(t or "") for t in legacy] if isinstance(legacy, list) else []


def _has_text(meta: dict[str, Any]) -> bool:
    doc_id = str(meta.get("id") or "")
    if _valid_id(doc_id) and _text_path(doc_id).exists():
        return True
    return bool(meta.get("page_text"))


def _meta_lock_path(doc_id: str) -> Path:
    return LIBRARY_META_DIR / f"{doc_id}.lock"


@contextmanager
def _meta_lock(doc_id: str) -> Iterator[None]:
    """Exclusive cross-process lock guarding a doc's read-modify-write cycle."""
    _ensure_dirs()
    fh = open(_meta_lock_path(doc_id), "w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


def _update_meta(doc_id: str, mutator: Callable[[dict[str, Any]], Any]) -> Any:
    """Atomic read-modify-write of a doc's meta under an exclusive lock.

    The mutator receives the freshly-read record, mutates it in place, and
    returns the endpoint's response payload. Raises 404 when the doc is gone.
    """
    with _meta_lock(doc_id):
        meta = _read_meta(doc_id)
        if not meta:
            raise HTTPException(status_code=404, detail="document not found")
        result = mutator(meta)
        _write_meta(doc_id, meta)
        return result


def _summarize(meta: dict[str, Any]) -> dict[str, Any]:
    """Strip the heavy page-text payload for list responses."""
    return {
        "id": meta.get("id"),
        "title": meta.get("title"),
        "filename": meta.get("filename"),
        "pages": meta.get("pages", 0),
        "size": meta.get("size", 0),
        "added": meta.get("added", 0),
        "progress": meta.get("progress", {}),
        "highlights": len(meta.get("highlights", [])),
        "has_text": _has_text(meta),
        "source_ext": meta.get("source_ext", "pdf"),
        "converted": bool(meta.get("converted")),
        "group": _read_group(meta),
        # Where the doc came from — 'upload' (default) or 'drive'.
        "source": meta.get("source") or "upload",
        "source_url": meta.get("source_url") or "",
        # Drive folder this came from ('' for loose uploads).
        "collection": meta.get("collection") or "",
        "collection_id": meta.get("collection_id") or "",
        # A cached page-1 preview exists for this doc.
        "has_thumb": thumb_path(str(meta.get("id") or "")).exists(),
    }


def _read_group(meta: dict[str, Any]) -> dict[str, Any]:
    """Normalize the doc's academic grouping tags."""
    g = meta.get("group") or {}
    return {
        "semester": str(g.get("semester") or ""),
        "subject": str(g.get("subject") or ""),
        "topic": str(g.get("topic") or ""),
    }


# ── Academics store (semesters → subjects → topics) ──────────────────────────


def _empty_academics() -> dict[str, Any]:
    return {"version": _ACADEMICS_VERSION, "semesters": []}


def _read_academics() -> dict[str, Any]:
    path = LIBRARY_ACADEMICS_PATH
    if not path.exists():
        return _empty_academics()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return _empty_academics()
    if not isinstance(data, dict):
        return _empty_academics()
    data.setdefault("version", _ACADEMICS_VERSION)
    if not isinstance(data.get("semesters"), list):
        data["semesters"] = []
    return data


def _write_academics(data: dict[str, Any]) -> None:
    _ensure_dirs()
    data["version"] = _ACADEMICS_VERSION
    target = LIBRARY_ACADEMICS_PATH
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(target)


def _find_semester(data: dict[str, Any], sem_id: str) -> dict[str, Any] | None:
    for s in data.get("semesters", []):
        if s.get("id") == sem_id:
            return s
    return None


def _find_subject(sem: dict[str, Any], subj_id: str) -> dict[str, Any] | None:
    for s in sem.get("subjects", []):
        if s.get("id") == subj_id:
            return s
    return None


# ── PDF extraction ───────────────────────────────────────────────────────────


def _extract(raw: bytes) -> dict[str, Any]:
    """Thin delegate — shared with the Drive importer."""
    return _shared_extract_pdf(raw)
def _clean_rects(raw: Any) -> list[dict[str, float]]:
    """Validate/normalize highlight rectangles ({x,y,w,h} normalized 0–1).

    Rejects non-list payloads and drops malformed entries instead of trusting
    client input; caps the count so a huge selection can't bloat the meta file.
    """
    if not isinstance(raw, list):
        return []
    out: list[dict[str, float]] = []
    for item in raw[:_MAX_RECTS]:
        if not isinstance(item, dict):
            continue
        try:
            x = float(item.get("x", 0))
            y = float(item.get("y", 0))
            w = float(item.get("w", 0))
            h = float(item.get("h", 0))
        except (TypeError, ValueError):
            continue
        if not all(math.isfinite(v) for v in (x, y, w, h)):
            continue
        if w <= 0 or h <= 0:
            continue
        out.append({"x": x, "y": y, "w": w, "h": h})
    return out


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.get("/api/study/docs")
def study_list_docs() -> list[dict[str, Any]]:
    """All library documents, newest first."""
    _ensure_dirs()
    items: list[dict[str, Any]] = []
    for f in LIBRARY_META_DIR.glob("*.json"):
        try:
            meta = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if meta.get("id"):
            items.append(_summarize(meta))
    items.sort(key=lambda x: x.get("added", 0), reverse=True)
    return items


@router.post("/api/study/upload")
async def study_upload(file: UploadFile = File(...)) -> dict[str, Any]:
    """Accept a document, normalize it to PDF, extract text, persist the meta.

    PDFs go straight in. DOCX/PPTX/ODT/etc. are converted to PDF via headless
    LibreOffice first — the reader, progress, and highlights all key off pages,
    so everything downstream stays format-agnostic. The original file is kept
    under originals/ so "download source" remains possible.
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="missing filename")

    ext = _ext_of(file.filename)
    if ext not in _ACCEPTED_EXT:
        allowed = ", ".join(sorted(_ACCEPTED_EXT))
        raise HTTPException(
            status_code=400,
            detail=f"unsupported file type '{ext or '?'}' — accepted: {allowed}",
        )

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="empty file")
    if len(raw) > _MAX_PDF_BYTES:
        raise HTTPException(status_code=413, detail="file too large (max 100 MB)")

    # Normalize non-PDF inputs to PDF bytes (kept in memory; original saved later).
    converted = False
    pdf_bytes = raw
    if ext != _NATIVE_EXT:
        try:
            pdf_bytes = _convert_to_pdf(raw, ext)
            converted = True
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))

    try:
        extracted = _extract(pdf_bytes)
    except ValueError as e:
        detail = str(e)
        if converted:
            detail += " (converted from " + ext.lstrip(".") + ")"
        raise HTTPException(status_code=422, detail=detail)

    _ensure_dirs()
    doc_id = uuid.uuid4().hex

    # Prefer the document's own title, then the uploaded filename.
    title = (extracted.get("meta_title") or "").strip()
    if not title or title.lower() in {"untitled", "unknown"}:
        title = Path(file.filename).stem

    _pdf_path(doc_id).write_bytes(pdf_bytes)

    # Preserve the original only when it isn't already the stored PDF.
    if converted:
        (LIBRARY_ORIG_DIR / f"{doc_id}{ext}").write_bytes(raw)

    meta: dict[str, Any] = {
        "version": _META_VERSION,
        "id": doc_id,
        "title": title,
        "slug": _slugify(title),
        "filename": file.filename,
        "source_ext": ext.lstrip("."),
        "converted": converted,
        "pages": extracted["pages"],
        "size": len(raw),
        "added": time.time(),
        "outline": extracted["outline"],
        "progress": {"current_page": 1, "percent": 0, "last_opened": 0},
        "highlights": [],
        "bookmarked": False,
        "group": {"semester": "", "subject": "", "topic": ""},
    }
    _write_page_text(doc_id, extracted["page_text"])
    _write_meta(doc_id, meta)

    return {"uploaded": True, **_summarize(meta)}


@router.get("/api/study/doc/{doc_id}/original")
def study_get_original(doc_id: str) -> FileResponse:
    """Serve the original upload for converted docs (404 for native PDFs)."""
    meta = _read_meta(doc_id)
    if not meta:
        raise HTTPException(status_code=404, detail="document not found")
    if not meta.get("converted"):
        raise HTTPException(status_code=404, detail="no original (native PDF)")
    # source_ext comes from meta JSON, which is not trusted input — a tampered
    # value could escape originals/. Whitelist it, then confirm containment.
    raw_ext = str(meta.get("source_ext") or "").strip().lower().lstrip(".")
    if not raw_ext or f".{raw_ext}" not in _CONVERTIBLE_EXT:
        raise HTTPException(status_code=404, detail="original file missing")
    base = LIBRARY_ORIG_DIR.resolve()
    path = (base / f"{doc_id}.{raw_ext}").resolve()
    if path.parent != base or not path.exists():
        raise HTTPException(status_code=404, detail="original file missing")
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=meta.get("filename") or path.name,
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.get("/api/study/formats")
def study_formats() -> dict[str, Any]:
    """Tell the frontend what it may upload + whether conversion is available."""
    return {
        "accept": sorted(_ACCEPTED_EXT),
        "native": _NATIVE_EXT,
        "convertible": sorted(_CONVERTIBLE_EXT),
        "converter": bool(_pdf_tool()),
    }


@router.get("/api/study/doc/{doc_id}")
def study_get_doc(doc_id: str) -> dict[str, Any]:
    """Document record — structure only. Page text is served separately."""
    meta = _read_meta(doc_id)
    if not meta:
        raise HTTPException(status_code=404, detail="document not found")
    return meta


@router.get("/api/study/doc/{doc_id}/text")
def study_get_text(doc_id: str) -> dict[str, Any]:
    """Per-page extracted text, kept out of the meta record to stay cheap."""
    meta = _read_meta(doc_id)
    if not meta:
        raise HTTPException(status_code=404, detail="document not found")
    pages = _load_page_text(meta)
    return {"id": doc_id, "pages": len(pages), "page_text": pages}


@router.get("/api/study/doc/{doc_id}/thumb")
def study_get_thumb(doc_id: str) -> FileResponse:
    """Cached page-1 preview. Generated on demand for docs that predate it."""
    meta = _read_meta(doc_id)
    if not meta:
        raise HTTPException(status_code=404, detail="document not found")
    path = thumb_path(doc_id)
    if not path.exists():
        # Backfill: render from the stored PDF and cache it.
        pdf = _pdf_path(doc_id)
        if not pdf.exists():
            raise HTTPException(status_code=404, detail="no preview available")
        data = make_thumbnail(pdf.read_bytes())
        if not data:
            raise HTTPException(status_code=404, detail="preview could not be generated")
        LIBRARY_THUMB_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".jpg.tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
    return FileResponse(
        path, media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=86400"},
    )


@router.get("/api/study/collections")
def study_collections() -> list[dict[str, Any]]:
    """Drive folders that have imported docs — used to group the library."""
    _ensure_dirs()
    groups: dict[str, dict[str, Any]] = {}
    for f in LIBRARY_META_DIR.glob("*.json"):
        try:
            meta = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        cid = str(meta.get("collection_id") or "")
        if not cid:
            continue
        g = groups.setdefault(cid, {
            "id": cid,
            "name": meta.get("collection") or cid,
            "source": meta.get("source") or "upload",
            "count": 0,
        })
        g["count"] += 1
    items = sorted(groups.values(), key=lambda x: x["name"].lower())
    return items


@router.get("/api/study/doc/{doc_id}/pdf")
def study_get_pdf(doc_id: str) -> FileResponse:
    """Serve the raw PDF bytes. Auth-gated by the global middleware."""
    if not _valid_id(doc_id):
        raise HTTPException(status_code=400, detail="invalid id")
    path = _pdf_path(doc_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="pdf not found")
    return FileResponse(
        path,
        media_type="application/pdf",
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.patch("/api/study/doc/{doc_id}/progress")
def study_update_progress(doc_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Persist reading position. Body: {current_page, percent}."""
    def mutate(meta: dict[str, Any]) -> dict[str, Any]:
        progress = meta.get("progress") or {}
        page = int(payload.get("current_page", progress.get("current_page", 1)) or 1)
        total = max(1, int(meta.get("pages", 1)))
        progress["current_page"] = max(1, min(page, total))
        if "percent" in payload:
            progress["percent"] = max(0, min(100, int(payload["percent"])))
        else:
            progress["percent"] = int(round((progress["current_page"] - 1) / total * 100))
        progress["last_opened"] = time.time()
        meta["progress"] = progress
        return progress

    return _update_meta(doc_id, mutate)


@router.post("/api/study/doc/{doc_id}/highlights")
def study_add_highlight(doc_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Append a highlight. Body: {page, rects, color, text, note}."""
    page = int(payload.get("page", 0) or 0)
    if page < 1:
        raise HTTPException(status_code=400, detail="page is required")

    rects = _clean_rects(payload.get("rects"))
    if not rects:
        raise HTTPException(status_code=400, detail="rects is required")

    highlight = {
        "id": uuid.uuid4().hex[:12],
        "page": page,
        "rects": rects,
        "color": str(payload.get("color") or "#ffd400")[:32],
        "text": str(payload.get("text") or "")[:2000],
        "note": str(payload.get("note") or "")[:2000],
        "created": time.time(),
    }

    def mutate(meta: dict[str, Any]) -> dict[str, Any]:
        highlights = meta.get("highlights") or []
        highlights.append(highlight)
        meta["highlights"] = highlights
        return highlight

    return _update_meta(doc_id, mutate)


@router.patch("/api/study/doc/{doc_id}/highlights/{hl_id}")
def study_update_highlight(doc_id: str, hl_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Edit a highlight's note or colour."""
    def mutate(meta: dict[str, Any]) -> dict[str, Any]:
        for hl in meta.get("highlights") or []:
            if hl.get("id") == hl_id:
                if "note" in payload:
                    hl["note"] = str(payload["note"])[:2000]
                if "color" in payload:
                    hl["color"] = str(payload["color"])
                return hl
        raise HTTPException(status_code=404, detail="highlight not found")

    return _update_meta(doc_id, mutate)


@router.delete("/api/study/doc/{doc_id}/highlights/{hl_id}")
def study_delete_highlight(doc_id: str, hl_id: str) -> dict[str, Any]:
    def mutate(meta: dict[str, Any]) -> dict[str, Any]:
        highlights = meta.get("highlights") or []
        kept = [h for h in highlights if h.get("id") != hl_id]
        if len(kept) == len(highlights):
            raise HTTPException(status_code=404, detail="highlight not found")
        meta["highlights"] = kept
        return {"deleted": True, "id": hl_id}

    return _update_meta(doc_id, mutate)


@router.post("/api/study/doc/{doc_id}/bookmark")
def study_toggle_bookmark(doc_id: str) -> dict[str, Any]:
    def mutate(meta: dict[str, Any]) -> dict[str, Any]:
        meta["bookmarked"] = not bool(meta.get("bookmarked"))
        return {"bookmarked": meta["bookmarked"]}

    return _update_meta(doc_id, mutate)


@router.patch("/api/study/doc/{doc_id}/group")
def study_update_group(doc_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Assign academic grouping tags. Body: {semester?, subject?, topic?}.

    Values are matched against the academics tree by NAME; unknown names are
    auto-created so the assignment modal can offer free-form entry. Empty
    string clears a tag.
    """
    def mutate(meta: dict[str, Any]) -> dict[str, Any]:
        group = _read_group(meta)
        for key in ("semester", "subject", "topic"):
            if key in payload:
                group[key] = str(payload.get(key) or "").strip()[:80]

        # A subject without its parent semester is meaningless — drop it.
        if not group["semester"]:
            group["subject"] = ""
        if not group["subject"]:
            group["topic"] = ""

        if any(group.values()):
            _ensure_academic_path(group["semester"], group["subject"], group["topic"])

        meta["group"] = group
        return group

    return _update_meta(doc_id, mutate)


@router.delete("/api/study/doc/{doc_id}")
def study_delete_doc(doc_id: str) -> dict[str, Any]:
    """Remove the PDF and its meta record."""
    if not _valid_id(doc_id):
        raise HTTPException(status_code=400, detail="invalid id")
    pdf = _pdf_path(doc_id)
    meta = _meta_path(doc_id)
    if not pdf.exists() and not meta.exists():
        raise HTTPException(status_code=404, detail="document not found")
    for p in (
        pdf,
        meta,
        _text_path(doc_id),
        _meta_lock_path(doc_id),
        *LIBRARY_ORIG_DIR.glob(f"{doc_id}.*"),
    ):
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass
    return {"deleted": True, "id": doc_id}


# ── Academics: semesters → subjects → topics ─────────────────────────────────


def _ensure_academic_path(semester: str, subject: str = "", topic: str = "") -> None:
    """Idempotently create the semester/subject/topic chain in the store."""
    data = _read_academics()
    changed = False

    sem = next((s for s in data["semesters"] if s.get("name") == semester), None)
    if sem is None:
        sem = {"id": uuid.uuid4().hex[:12], "name": semester, "subjects": []}
        data["semesters"].append(sem)
        changed = True

    if subject:
        subj = next((s for s in sem["subjects"] if s.get("name") == subject), None)
        if subj is None:
            subj = {"id": uuid.uuid4().hex[:12], "name": subject, "topics": []}
            sem["subjects"].append(subj)
            changed = True

        if topic:
            topics = subj.setdefault("topics", [])
            if not any(t.get("name") == topic for t in topics):
                topics.append({"id": uuid.uuid4().hex[:12], "name": topic})
                changed = True

    if changed:
        _write_academics(data)


@router.get("/api/study/academics")
def academics_get() -> dict[str, Any]:
    """The full semester → subject → topic tree."""
    return _read_academics()


@router.post("/api/study/academics/semesters")
def academics_add_semester(payload: dict[str, Any]) -> dict[str, Any]:
    name = str(payload.get("name") or "").strip()[:80]
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    data = _read_academics()
    if any(s.get("name") == name for s in data["semesters"]):
        raise HTTPException(status_code=409, detail="semester already exists")
    sem = {"id": uuid.uuid4().hex[:12], "name": name, "subjects": []}
    data["semesters"].append(sem)
    _write_academics(data)
    return sem


@router.patch("/api/study/academics/semesters/{sem_id}")
def academics_rename_semester(sem_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    name = str(payload.get("name") or "").strip()[:80]
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    data = _read_academics()
    sem = _find_semester(data, sem_id)
    if not sem:
        raise HTTPException(status_code=404, detail="semester not found")
    old = sem["name"]
    sem["name"] = name
    _write_academics(data)
    _retag_docs("semester", old, name)
    return sem


@router.delete("/api/study/academics/semesters/{sem_id}")
def academics_delete_semester(sem_id: str) -> dict[str, Any]:
    data = _read_academics()
    sem = _find_semester(data, sem_id)
    if not sem:
        raise HTTPException(status_code=404, detail="semester not found")
    data["semesters"] = [s for s in data["semesters"] if s.get("id") != sem_id]
    _write_academics(data)
    _retag_docs("semester", sem["name"], "", cascade=True)
    return {"deleted": True, "id": sem_id}


@router.post("/api/study/academics/semesters/{sem_id}/subjects")
def academics_add_subject(sem_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    name = str(payload.get("name") or "").strip()[:80]
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    data = _read_academics()
    sem = _find_semester(data, sem_id)
    if not sem:
        raise HTTPException(status_code=404, detail="semester not found")
    if any(s.get("name") == name for s in sem["subjects"]):
        raise HTTPException(status_code=409, detail="subject already exists")
    subj = {"id": uuid.uuid4().hex[:12], "name": name, "topics": []}
    sem["subjects"].append(subj)
    _write_academics(data)
    return subj


@router.patch("/api/study/academics/subjects/{subj_id}")
def academics_rename_subject(subj_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    name = str(payload.get("name") or "").strip()[:80]
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    data = _read_academics()
    for sem in data["semesters"]:
        subj = _find_subject(sem, subj_id)
        if subj:
            old = subj["name"]
            subj["name"] = name
            _write_academics(data)
            _retag_docs("subject", old, name)
            return subj
    raise HTTPException(status_code=404, detail="subject not found")


@router.delete("/api/study/academics/subjects/{subj_id}")
def academics_delete_subject(subj_id: str) -> dict[str, Any]:
    data = _read_academics()
    for sem in data["semesters"]:
        subj = _find_subject(sem, subj_id)
        if subj:
            sem["subjects"] = [s for s in sem["subjects"] if s.get("id") != subj_id]
            _write_academics(data)
            _retag_docs("subject", subj["name"], "", cascade=True)
            return {"deleted": True, "id": subj_id}
    raise HTTPException(status_code=404, detail="subject not found")


@router.post("/api/study/academics/subjects/{subj_id}/topics")
def academics_add_topic(subj_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    name = str(payload.get("name") or "").strip()[:80]
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    data = _read_academics()
    for sem in data["semesters"]:
        subj = _find_subject(sem, subj_id)
        if subj:
            topics = subj.setdefault("topics", [])
            if any(t.get("name") == name for t in topics):
                raise HTTPException(status_code=409, detail="topic already exists")
            topic = {"id": uuid.uuid4().hex[:12], "name": name}
            topics.append(topic)
            _write_academics(data)
            return topic
    raise HTTPException(status_code=404, detail="subject not found")


@router.delete("/api/study/academics/topics/{topic_id}")
def academics_delete_topic(topic_id: str) -> dict[str, Any]:
    data = _read_academics()
    for sem in data["semesters"]:
        for subj in sem.get("subjects", []):
            topics = subj.get("topics", [])
            hit = next((t for t in topics if t.get("id") == topic_id), None)
            if hit:
                subj["topics"] = [t for t in topics if t.get("id") != topic_id]
                _write_academics(data)
                _retag_docs("topic", hit["name"], "", cascade=True)
                return {"deleted": True, "id": topic_id}
    raise HTTPException(status_code=404, detail="topic not found")


def _retag_docs(field: str, old: str, new: str, cascade: bool = False) -> None:
    """Keep doc tags consistent when a tree node is renamed/deleted.

    cascade=True also clears any deeper tags (used on delete, so a doc never
    points at a subject that no longer exists).
    """
    if not old or old == new:
        return
    for f in LIBRARY_META_DIR.glob("*.json"):
        # Read via _read_meta so legacy inline page text is migrated, not lost.
        try:
            meta = _read_meta(f.stem)
        except Exception:
            meta = None
        if not meta:
            continue
        group = meta.get("group") or {}
        if group.get(field) != old:
            continue
        group[field] = new
        if cascade:
            order = ["semester", "subject", "topic"]
            idx = order.index(field)
            for deeper in order[idx + 1:]:
                group[deeper] = ""
        meta["group"] = group
        try:
            _write_meta(f.stem, meta)
        except Exception:
            pass
