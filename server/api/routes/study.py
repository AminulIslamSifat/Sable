"""Study mode — PDF library with extracted text, progress, and highlights.

Storage layout (see engine.config):
    ~/sable_output/library/pdfs/<doc-id>.pdf
    ~/sable_output/library/meta/<doc-id>.json

The PDF is the source of truth for rendering; the meta JSON holds all derived
"connected data" — per-page text, outline, progress, and highlights — so the
frontend never has to re-parse the PDF to draw the sidebar.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import math
import re
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse

from engine.config import (
    LIBRARY_ACADEMICS_PATH,
    LIBRARY_EMBED_DIR,
    LIBRARY_META_DIR,
    LIBRARY_NOTES_DIR,
    LIBRARY_ORIG_DIR,
    LIBRARY_PDF_DIR,
    LIBRARY_SRS_DIR,
    LIBRARY_TEXT_DIR,
    LIBRARY_THUMB_DIR,
)
from engine.library_ingest import make_thumbnail, thumb_path

from engine.library_ingest import (
    convert_to_pdf as _shared_convert_to_pdf,
    extract_pdf as _shared_extract_pdf,
    pdf_tool,
)
from engine import library_catalog as _catalog
from engine import library_rag as _rag
from engine import library_search as _search
from engine import library_sessions as _sessions
from engine import library_index as _index
from engine import library_upload as _upload

router = APIRouter()
logger = logging.getLogger("sable.study")

# Fire-and-forget indexing tasks, kept referenced so they aren't GC'd mid-run.
_INDEX_TASKS: set[asyncio.Task] = set()
# Same, for the upload ingest pipelines. A bare create_task() with no strong
# reference can be garbage-collected mid-run.
_UPLOAD_TASKS: set[asyncio.Task] = set()


def _spawn_index_job(doc_id: str, *, pages: list[int] | None = None,
                     do_ocr: bool = True, do_search: bool = True,
                     do_ai: bool = False, lang: str = "eng+ben",
                     model: str = "", thinking_mode: str = "") -> None:
    """Run an index job in the background, tracked so it isn't collected."""
    async def _run() -> None:
        try:
            await _index.run_job(
                doc_id, do_ocr=do_ocr, do_search=do_search, do_ai=do_ai,
                pages=pages, lang=lang, model=model, thinking_mode=thinking_mode,
            )
        except Exception:
            logger.warning("index job failed for %s", doc_id, exc_info=True)

    task = asyncio.create_task(_run())
    _INDEX_TASKS.add(task)
    task.add_done_callback(_INDEX_TASKS.discard)

_MAX_PDF_BYTES = 100 * 1024 * 1024  # 100 MB


class _UploadCancelled(Exception):
    """Raised between pipeline phases when the user cancelled the upload."""


def _delete_doc(doc_id: str) -> None:
    """Remove every trace of a document: files, meta, and derived indexes.

    Shared by the delete endpoint and by upload cancellation, so a cancelled
    upload can never leave a half-ingested orphan behind.
    """
    if not _valid_id(doc_id):
        return
    for p in (
        _pdf_path(doc_id),
        _meta_path(doc_id),
        _text_path(doc_id),
        _meta_lock_path(doc_id),
        thumb_path(doc_id),
        *LIBRARY_ORIG_DIR.glob(f"{doc_id}.*"),
    ):
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass
    for cleanup in (_search.remove_document, _rag.remove, _catalog.remove_doc):
        try:
            cleanup(doc_id)
        except Exception:
            logger.warning("index cleanup failed for %s", doc_id, exc_info=True)
    # A cancelled doc must not keep a job file the Indexing tab would render
    # as a resumable job for a document that no longer exists.
    try:
        _index.delete_job(doc_id)
    except Exception:
        pass


async def _run_upload_pipeline(upload_id: str, *, filename: str, ext: str) -> None:
    """Ingest one staged upload, off the request thread.

    Phases: convert -> extract -> persist -> index. Every blocking call is
    pushed to a worker thread; the event loop stays free so the rest of the app
    (including the status poll that drives the sidebar bar) keeps responding.

    Cancellation is cooperative and checked between phases, then handed to the
    index job as its own should_stop. A cancelled upload is deleted outright —
    no orphan, no partial document.
    """
    staged = _upload.staging_path(upload_id, ext)
    doc_id = ""
    try:
        job = _upload.load_job(upload_id) or {}
        group = job.get("group") or {}
        index_mode = str(job.get("index_mode") or _DEFAULT_INDEX_MODE)
        ocr = str(job.get("ocr") or _DEFAULT_OCR_MODE)

        raw = await asyncio.to_thread(staged.read_bytes) if staged.exists() else b""
        if not raw:
            _upload.finish(upload_id, "error", error="staged file missing")
            return

        # ── convert (non-PDF only) ──
        if _upload.is_cancelled(upload_id):
            raise _UploadCancelled
        if ext != _NATIVE_EXT:
            _upload.set_phase(upload_id, "converting")
            try:
                pdf_bytes = await asyncio.to_thread(_convert_to_pdf, raw, ext)
            except ValueError as e:
                _upload.finish(upload_id, "error", error=str(e))
                return
            converted = True
        else:
            pdf_bytes = raw
            converted = False

        # ── extract ──
        if _upload.is_cancelled(upload_id):
            raise _UploadCancelled
        _upload.set_phase(upload_id, "extracting")
        try:
            extracted = await asyncio.to_thread(_extract, pdf_bytes)
        except ValueError:
            # No text layer — almost certainly a scanned/image-only PDF. Those
            # are exactly what OCR exists for, so ingest with blank page text
            # and let the auto-OCR pass fill it in.
            pages = await asyncio.to_thread(_pdf_page_count, pdf_bytes)
            if pages <= 0:
                _upload.finish(upload_id, "error", error="could not read PDF")
                return
            logger.info("no text layer in %s (%d pages) — queuing for OCR", filename, pages)
            extracted = {"pages": pages, "page_text": [""] * pages, "outline": [], "meta_title": ""}

        # ── persist ──
        if _upload.is_cancelled(upload_id):
            raise _UploadCancelled
        _upload.set_phase(upload_id, "persisting")

        _ensure_dirs()
        doc_id = uuid.uuid4().hex

        title = (extracted.get("meta_title") or "").strip()
        if not title or title.lower() in {"untitled", "unknown"}:
            title = Path(filename).stem

        meta: dict[str, Any] = {
            "version": _META_VERSION,
            "id": doc_id,
            "title": title,
            "slug": _slugify(title),
            "filename": filename,
            "source_ext": ext.lstrip("."),
            "converted": converted,
            "pages": extracted["pages"],
            "size": len(raw),
            "added": time.time(),
            "outline": extracted["outline"],
            "progress": {"current_page": 1, "percent": 0, "last_opened": 0},
            "highlights": [],
            "bookmarked": False,
            "group": group,
        }

        await asyncio.to_thread(_pdf_path(doc_id).write_bytes, pdf_bytes)
        if converted:
            await asyncio.to_thread((LIBRARY_ORIG_DIR / f"{doc_id}{ext}").write_bytes, raw)
        await asyncio.to_thread(_write_page_text, doc_id, extracted["page_text"])
        await asyncio.to_thread(_write_meta, doc_id, meta)
        await asyncio.to_thread(_ensure_topic, group.get("topic") or "Others")

        # Recorded before indexing so a cancel during the index pass still
        # knows which document to delete.
        _upload.set_doc_id(upload_id, doc_id, title, int(extracted.get("pages") or 0))

        try:
            await asyncio.to_thread(
                _catalog.upsert_doc, doc_id, title, group, meta["added"]
            )
        except Exception:
            logger.warning("catalog upsert failed for %s", doc_id, exc_info=True)

        if _upload.is_cancelled(upload_id):
            raise _UploadCancelled

        # ── index ──
        ocr_pages: list[int] | None = None
        if ocr == "never":
            ocr_pages = []
        elif ocr == "force":
            ocr_pages = list(range(1, int(extracted.get("pages") or 0) + 1))
        else:
            try:
                ocr_pages = await asyncio.to_thread(
                    _index.pages_needing_ocr, extracted["page_text"]
                )
            except Exception:
                logger.warning("blank-page scan failed for %s", doc_id, exc_info=True)
                ocr_pages = []

        do_ocr = ocr != "never"
        do_search = index_mode in ("search", "rag")
        do_ai = index_mode == "rag"

        if do_ocr or do_search or do_ai:
            _upload.set_phase(upload_id, "indexing")
            # Awaited, not spawned: cancelling the upload has to stop the index
            # pass too, and run_job already owns resumable checkpoints.
            await _index.run_job(
                doc_id,
                pages=ocr_pages,
                do_ocr=do_ocr,
                do_search=do_search,
                do_ai=do_ai,
                should_stop=lambda: _upload.is_cancelled(upload_id),
            )

        if _upload.is_cancelled(upload_id):
            raise _UploadCancelled

        _upload.finish(upload_id, "done")

    except _UploadCancelled:
        # Cancelled means gone. Delete the document if it got far enough to
        # have one, then settle the job.
        if doc_id:
            await asyncio.to_thread(_delete_doc, doc_id)
        _upload.finish(upload_id, "cancelled")
        logger.info("upload %s cancelled%s", upload_id, f" (doc {doc_id})" if doc_id else "")
    except Exception as exc:
        logger.warning("upload pipeline failed for %s", upload_id, exc_info=True)
        if doc_id:
            await asyncio.to_thread(_delete_doc, doc_id)
        _upload.finish(upload_id, "error", error=str(exc))
    finally:
        _upload.mark_inactive(upload_id)
        _upload.clear_cancel(upload_id)
        _upload.discard_staging(upload_id)
_MAX_RECTS = 200  # highlight rectangles per selection
_META_VERSION = 1
_ACADEMICS_VERSION = 2

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
    return pdf_tool()


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
        "session": str(g.get("session") or ""),
        "subject": str(g.get("subject") or ""),
        "topic": str(g.get("topic") or ""),
    }


# ── Academics store (semesters → subjects → topics) ──────────────────────────


def _empty_academics() -> dict[str, Any]:
    return {"version": _ACADEMICS_VERSION, "topics": [], "semesters": [], "sessions": []}


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
    # Topics are the PRIMARY organizing axis now — a flat, top-level list. A
    # document's topic is its parent; semester/subject are legacy metadata.
    if not isinstance(data.get("topics"), list):
        data["topics"] = []
    if not isinstance(data.get("semesters"), list):
        data["semesters"] = []
    # Sessions are a flat cohort list (e.g. "2019-2020") — a sibling axis to
    # semesters, not nested inside them.
    if not isinstance(data.get("sessions"), list):
        data["sessions"] = []
    # One-time migration: fold any legacy nested topics
    # (semesters → subjects → topics) into the flat top-level list. Idempotent.
    seen = {t.get("name") for t in data["topics"] if isinstance(t, dict)}
    for sem in data.get("semesters", []):
        for subj in (sem.get("subjects") or []):
            for t in (subj.get("topics") or []):
                nm = t.get("name")
                if nm and nm not in seen:
                    data["topics"].append({"id": t.get("id") or uuid.uuid4().hex[:12], "name": nm})
                    seen.add(nm)
    return data


def _ensure_topic(topic: str) -> None:
    """Idempotently create a flat top-level topic. Empty/blank is ignored."""
    topic = (topic or "").strip()[:80]
    if not topic:
        return
    data = _read_academics()
    topics = data.setdefault("topics", [])
    if not any(t.get("name") == topic for t in topics):
        topics.append({"id": uuid.uuid4().hex[:12], "name": topic})
        _write_academics(data)


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


def _pdf_page_count(raw: bytes) -> int:
    """Page count only — used for scanned PDFs that have no text layer."""
    try:
        import pymupdf  # type: ignore

        doc = pymupdf.open(stream=raw, filetype="pdf")
        try:
            return int(doc.page_count)
        finally:
            doc.close()
    except Exception:
        pass
    try:
        import io

        from pypdf import PdfReader  # type: ignore

        return len(PdfReader(io.BytesIO(raw)).pages)
    except Exception:
        return 0
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


# How much derived indexing an upload builds. Ordered by cost: each level
# includes the ones before it. "rag" is the historical default because study
# Q&A leans on retrieval, but a bulk import of slides rarely needs embeddings.
_INDEX_MODES = ("none", "search", "rag")
_DEFAULT_INDEX_MODE = "rag"

# Scanned-page handling. "auto" only touches pages whose extracted text came
# out blank; "force" re-OCRs every page (slow, for bad extractors); "never"
# leaves blank pages blank.
_OCR_MODES = ("auto", "force", "never")
_DEFAULT_OCR_MODE = "auto"


@router.post("/api/study/upload")
async def study_upload(
    file: UploadFile = File(...),
    semester: str = Form(""),
    session: str = Form(""),
    subject: str = Form(""),
    topic: str = Form(""),
    index_mode: str = Form(_DEFAULT_INDEX_MODE),
    ocr: str = Form(_DEFAULT_OCR_MODE),
) -> dict[str, Any]:
    """Accept a document, normalize it to PDF, extract text, persist the meta.

    PDFs go straight in. DOCX/PPTX/ODT/etc. are converted to PDF via headless
    LibreOffice first — the reader, progress, and highlights all key off pages,
    so everything downstream stays format-agnostic. The original file is kept
    under originals/ so "download source" remains possible.

    Optional ``semester``/``session``/``subject``/``topic`` form fields file
    the document into the academics tree on the way in, so a batch upload can
    be grouped without a second trip through the doc menu.

    ``index_mode`` picks how much derived indexing to build, because the passes
    differ hugely in cost and not every document needs all of them:

      * ``none``   -- page text only. Reader works; no search, no RAG.
      * ``search`` -- + SQLite FTS5. Fast, cheap, powers library search.
      * ``rag``    -- + chunk embeddings. Slow; needed for topic-wide and
                      semantic retrieval over the document.

    ``ocr`` controls the scanned-page pass: ``auto`` (only blank pages),
    ``force`` (every page), or ``never``.
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="missing filename")

    index_mode = (index_mode or _DEFAULT_INDEX_MODE).strip().lower()
    if index_mode not in _INDEX_MODES:
        allowed = ", ".join(sorted(_INDEX_MODES))
        raise HTTPException(
            status_code=400,
            detail=f"unknown index_mode '{index_mode}' — accepted: {allowed}",
        )
    ocr = (ocr or _DEFAULT_OCR_MODE).strip().lower()
    if ocr not in _OCR_MODES:
        allowed = ", ".join(sorted(_OCR_MODES))
        raise HTTPException(
            status_code=400,
            detail=f"unknown ocr mode '{ocr}' — accepted: {allowed}",
        )

    ext = _ext_of(file.filename)
    if ext not in _ACCEPTED_EXT:
        allowed = ", ".join(sorted(_ACCEPTED_EXT))
        raise HTTPException(
            status_code=400,
            detail=f"unsupported file type '{ext or '?'}' — accepted: {allowed}",
        )

    # The request's ONLY job is to get bytes onto disk. Conversion, extraction
    # and indexing are all slow — a LibreOffice cold start, a full PyMuPDF walk
    # of every page, then OCR — and every one of them is a SYNCHRONOUS call.
    # Running them here held the response open for minutes on a textbook, and
    # because there was no await in sight it froze the event loop with them, so
    # even /index/status stalled. Everything past staging now lives in a
    # background pipeline.
    upload_id = uuid.uuid4().hex
    staged = _upload.staging_path(upload_id, ext)

    size = 0
    try:
        with open(staged, "wb") as fh:
            while True:
                # Bounded slices keep a 100 MB upload out of RAM, and each
                # await hands control back so the loop keeps breathing.
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > _MAX_PDF_BYTES:
                    raise HTTPException(status_code=413, detail="file too large (max 100 MB)")
                fh.write(chunk)
    except HTTPException:
        _upload.discard_staging(upload_id)
        raise
    except Exception as exc:
        _upload.discard_staging(upload_id)
        logger.warning("staging write failed for %s: %s", file.filename, exc)
        raise HTTPException(status_code=500, detail="could not store upload")

    if size == 0:
        _upload.discard_staging(upload_id)
        raise HTTPException(status_code=400, detail="empty file")

    group = {
        "semester": semester.strip()[:80],
        "session": session.strip()[:80],
        "subject": subject.strip()[:80],
        # Topic is MANDATORY — every document is filed under one. Uploads that
        # don't pick a topic land in the catch-all "Others" bucket.
        "topic": (topic.strip()[:80] or "Others"),
    }

    _upload.create_job(
        upload_id,
        filename=file.filename,
        ext=ext,
        size=size,
        group=group,
        index_mode=index_mode,
        ocr=ocr,
    )
    # Register as live BEFORE the task exists. A status poll landing in the gap
    # would otherwise see "running" with nothing driving it and report an error.
    _upload.mark_active(upload_id)
    task = asyncio.create_task(
        _run_upload_pipeline(upload_id, filename=file.filename, ext=ext)
    )
    _UPLOAD_TASKS.add(task)
    task.add_done_callback(_UPLOAD_TASKS.discard)

    return {
        "upload_id": upload_id,
        "filename": file.filename,
        "size": size,
        "queued": True,
        "indexing": {
            "mode": index_mode,
            "stages": [s for s, on in (
                ("ocr", ocr != "never"),
                ("search", index_mode in ("search", "rag")),
                ("rag", index_mode == "rag"),
            ) if on],
            "ocr": ocr,
        },
    }


# ── Upload jobs (staging → convert → extract → index) ────────────────────────


@router.get("/api/study/upload/status")
def study_upload_status(upload_id: str = "") -> dict[str, Any]:
    """Progress for the sidebar's upload rows.

    Without ``upload_id``: every upload worth showing. Indexing progress for a
    row in the ``indexing`` phase is folded in from the doc's own index job, so
    one bar covers the whole upload instead of resetting at the handoff.
    """
    jobs = (
        [_upload.load_job(upload_id) or {}]
        if _valid_id(upload_id)
        else _upload.list_jobs()
    )

    out: list[dict[str, Any]] = []
    for job in jobs:
        uid = str(job.get("upload_id") or "")
        if not _valid_id(uid):
            continue
        index_percent: int | None = None
        doc_id = str(job.get("doc_id") or "")
        if doc_id and _valid_id(doc_id):
            try:
                index_percent = int(_index.status_for(doc_id).get("percent") or 0)
            except Exception:
                index_percent = None
        out.append(_upload.status_for(uid, index_percent=index_percent))

    if _valid_id(upload_id) and not out:
        raise HTTPException(status_code=404, detail="upload not found")

    return {
        "uploads": out,
        "active": sum(1 for u in out if u.get("status") == "running"),
    }


@router.post("/api/study/upload/cancel")
async def study_upload_cancel(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Cancel an upload and delete whatever it produced.

    Cooperative, like the index stop: the running pipeline finishes the phase
    it is on, then unwinds. If it already created the document, that document
    is deleted outright — a cancelled upload leaves nothing behind.
    """
    payload = payload or {}
    upload_id = str(payload.get("upload_id") or "")
    if not _valid_id(upload_id):
        raise HTTPException(status_code=400, detail="invalid upload id")

    job = _upload.load_job(upload_id)
    if not job:
        raise HTTPException(status_code=404, detail="upload not found")

    status = str(job.get("status") or "")
    if status in ("done", "error", "cancelled"):
        return {"status": status, "upload_id": upload_id, "live": False}

    was_live = _upload.request_cancel(upload_id)
    if was_live:
        return {"status": "cancelling", "upload_id": upload_id, "live": True}

    # Nothing running: the server restarted mid-upload, so there is no task to
    # signal and nothing to resume from. Delete any partial doc and settle it.
    doc_id = str(job.get("doc_id") or "")
    if doc_id and _valid_id(doc_id) and _read_meta(doc_id):
        await asyncio.to_thread(_delete_doc, doc_id)
    _upload.finish(upload_id, "cancelled")
    _upload.discard_staging(upload_id)
    return {"status": "cancelled", "upload_id": upload_id, "live": False}


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
        for key in ("semester", "session", "subject", "topic"):
            if key in payload:
                group[key] = str(payload.get(key) or "").strip()[:80]

        # Topic is MANDATORY — clear/blank resolves to the "Others" bucket so a
        # document is never left without a parent topic.
        if not group["topic"]:
            group["topic"] = "Others"
        _ensure_topic(group["topic"])

        meta["group"] = group
        return group

    result = _update_meta(doc_id, mutate)
    # Keep the topic catalog in step with the new grouping so topic-wide
    # retrieval sees the change on the very next question.
    try:
        meta = _read_meta(doc_id) or {}
        _catalog.upsert_doc(doc_id, str(meta.get("title") or ""), result, added=float(meta.get("added") or 0.0))
    except Exception:
        logger.warning("catalog upsert failed for %s", doc_id, exc_info=True)
    return result


@router.delete("/api/study/doc/{doc_id}")
def study_delete_doc(doc_id: str) -> dict[str, Any]:
    """Remove the PDF, its meta record, and every derived index."""
    if not _valid_id(doc_id):
        raise HTTPException(status_code=400, detail="invalid id")
    if not _pdf_path(doc_id).exists() and not _meta_path(doc_id).exists():
        raise HTTPException(status_code=404, detail="document not found")
    # Shared with upload cancellation so a doc can never be half-removed.
    _delete_doc(doc_id)
    return {"deleted": True, "id": doc_id}


# ── Full-text search ─────────────────────────────────────────────────────────


@router.get("/api/study/search")
def study_search(q: str = "", limit: int = 40, doc_id: str = "") -> dict[str, Any]:
    """Search extracted page text across the library (or one document).

    Returns per-page hits with highlighted snippets plus a per-document rollup
    the sidebar groups by. Titles are attached from the meta store so a hit is
    readable even when the index was built without a title.
    """
    query = (q or "").strip()
    if not query:
        return {"query": "", "engine": "none", "total": 0, "hits": [], "docs": []}
    if doc_id and not _valid_id(doc_id):
        raise HTTPException(status_code=400, detail="invalid doc id")

    result = _search.search(query, limit=limit, doc_id=doc_id)

    titles: dict[str, str] = {}
    for hit in result.get("hits", []):
        did = hit.get("doc_id") or ""
        if did in titles:
            continue
        meta = _read_meta(did)
        titles[did] = str((meta or {}).get("title") or hit.get("title") or "Untitled")
    for hit in result.get("hits", []):
        hit["title"] = titles.get(hit.get("doc_id") or "", hit.get("title") or "")
    for doc in result.get("docs", []):
        doc["title"] = titles.get(doc.get("doc_id") or "", doc.get("title") or "")

    return result


@router.post("/api/study/search/reindex")
def study_reindex(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Rebuild derived indexes from stored page text.

    body: {search?: bool, rag?: bool, doc_id?: str}

    Called on first run (existing libraries predate the index), after an
    extractor upgrade, or from the settings UI. Without ``doc_id`` the whole
    library is walked; that is slow on a big library, so it stays explicit.
    """
    payload = payload or {}
    want_search = bool(payload.get("search", True))
    want_rag = bool(payload.get("rag", False))
    only_missing = bool(payload.get("only_missing", False))
    target = str(payload.get("doc_id") or "")

    if target and not _valid_id(target):
        raise HTTPException(status_code=400, detail="invalid doc id")

    out: dict[str, Any] = {"search": None, "rag": None}

    if target:
        meta = _read_meta(target)
        if not meta:
            raise HTTPException(status_code=404, detail="document not found")
        pages = _load_page_text(meta)
        _catalog.upsert_doc(
            target, str(meta.get("title") or ""), meta.get("group") or {},
            added=float(meta.get("added") or 0.0),
        )
        if want_search:
            count = _search.index_document(target, str(meta.get("title") or ""), pages)
            out["search"] = {"doc_id": target, "pages": count}
        if want_rag:
            out["rag"] = _rag.build(target, pages)
        return out

    if not target and payload.get("catalog"):
        try:
            out["catalog"] = _catalog.rebuild_from_meta()
        except Exception:
            logger.warning("catalog rebuild failed", exc_info=True)
            out["catalog"] = None

    if want_search:
        if only_missing:
            # Walk the library but skip anything already carrying rows, so a
            # "finish the job" click doesn't redo thousands of indexed pages.
            try:
                already = set(_search.indexed_doc_ids())
            except Exception:
                already = set()
            built = 0
            skipped = 0
            for path in sorted(LIBRARY_TEXT_DIR.glob("*.json")):
                doc_id = path.stem
                if doc_id in already or not _valid_id(doc_id):
                    skipped += 1
                    continue
                try:
                    pages = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    skipped += 1
                    continue
                if not isinstance(pages, list):
                    skipped += 1
                    continue
                _search.index_document(doc_id, "", pages)
                built += 1
            out["search"] = {"indexed": built, "skipped": skipped}
        else:
            out["search"] = _search.rebuild_all()
    if want_rag:
        _ensure_dirs()
        built = 0
        for path in sorted(LIBRARY_TEXT_DIR.glob("*.json")):
            try:
                pages = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(pages, list):
                _rag.build(path.stem, pages)
                built += 1
        out["rag"] = {"indexed": built}
    return out


# ── Index jobs (OCR + AI passes) ─────────────────────────────────────────────


@router.post("/api/study/index/start")
async def study_index_start(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Start (or resume) an indexing job for one doc, or the whole library.

    body: {doc_id?, all?: bool, ocr?: bool, ai?: bool, pages?: [int],
           lang?: str, model?: str, thinking_mode?: str}

    OCR runs first, then AI. Both passes are resumable: re-invoking picks up
    from the persisted cursors, so a stop never loses work.
    """
    payload = payload or {}
    do_ocr = bool(payload.get("ocr", True))
    do_search = bool(payload.get("search", True))
    do_ai = bool(payload.get("ai", False))
    lang = str(payload.get("lang") or "eng+ben")
    model = str(payload.get("model") or "")
    thinking_mode = str(payload.get("thinking_mode") or "")
    target = str(payload.get("doc_id") or "")
    raw_pages = payload.get("pages")
    pages = [int(p) for p in raw_pages] if isinstance(raw_pages, list) else None

    if target:
        if not _valid_id(target):
            raise HTTPException(status_code=400, detail="invalid doc id")
        if not _read_meta(target):
            raise HTTPException(status_code=404, detail="document not found")
        _spawn_index_job(
            target, pages=pages, do_ocr=do_ocr, do_search=do_search,
            do_ai=do_ai, lang=lang, model=model, thinking_mode=thinking_mode,
        )
        return {
            "status": "started", "doc_id": target,
            "ocr": do_ocr, "search": do_search, "ai": do_ai,
        }

    # Whole-library kickoff — one background task per document.
    if payload.get("all"):
        # only_missing: skip docs that already have searchable text, so the
        # "index everything that isn't indexed" button doesn't re-OCR the
        # entire library every time it's pressed.
        skip_ids: set[str] = set()
        if payload.get("only_missing"):
            try:
                skip_ids = set(_search.indexed_doc_ids())
            except Exception:
                skip_ids = set()

        started = 0
        for path in sorted(LIBRARY_TEXT_DIR.glob("*.json")):
            doc_id = path.stem
            if not _valid_id(doc_id) or doc_id in skip_ids:
                continue
            _spawn_index_job(
                doc_id, do_ocr=do_ocr, do_search=do_search, do_ai=do_ai,
                lang=lang, model=model, thinking_mode=thinking_mode,
            )
            started += 1
        return {
            "status": "started", "docs": started,
            "ocr": do_ocr, "search": do_search, "ai": do_ai,
        }

    raise HTTPException(status_code=400, detail="provide doc_id or all=true")


@router.post("/api/study/index/pause")
def study_index_pause(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Pause a doc's job. The next start resumes from the saved cursors.

    This used to only write status=paused to the job file, which was a lie:
    the background task kept running and overwrote it on its next checkpoint.
    It now signals the running pass, which stops at its next page/chunk and
    sets the status itself.
    """
    payload = payload or {}
    doc_id = str(payload.get("doc_id") or "")
    if not _valid_id(doc_id):
        raise HTTPException(status_code=400, detail="invalid doc id")

    was_live = _index.request_stop(doc_id)
    if not was_live:
        # Nothing running to signal — settle the file so the UI agrees.
        job = _index.load_job(doc_id)
        if job.get("status") == "running":
            job["status"] = "paused"
            job["stage"] = ""
            _index.save_job(job)
    return {
        "status": "pausing" if was_live else "paused",
        "doc_id": doc_id,
        "live": was_live,
    }


@router.post("/api/study/index/stop")
def study_index_stop(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Ask a running index job to stop at its next checkpoint.

    Cooperative, not a hard kill: the running pass finishes the page or chunk
    it is on, commits its checkpoint, and then unwinds. That keeps the job
    resumable and never leaves a half-written text file behind.

    body: {doc_id}
    """
    payload = payload or {}
    doc_id = str(payload.get("doc_id") or "")
    if not doc_id:
        raise HTTPException(status_code=400, detail="doc_id required")
    if not _valid_id(doc_id):
        raise HTTPException(status_code=400, detail="invalid doc id")

    was_live = _index.request_stop(doc_id)
    return {
        "status": "stopping" if was_live else "not_running",
        "doc_id": doc_id,
        "live": was_live,
    }


def _library_meta_records() -> list[dict[str, Any]]:
    """Every document record on disk, newest first."""
    out: list[dict[str, Any]] = []
    for f in LIBRARY_META_DIR.glob("*.json"):
        try:
            meta = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(meta, dict) and _valid_id(str(meta.get("id") or "")):
            out.append(meta)
    out.sort(key=lambda m: float(m.get("added") or 0), reverse=True)
    return out


def _index_overview() -> dict[str, Any]:
    """Every library document joined with its index state.

    The Indexing tab is the one place that answers "what is indexed and what
    isn't", so it cannot be driven by job files alone: a document that was
    never indexed has no job file at all, while a document deleted by an older
    build can still be survived by one. Walking the library instead means the
    panel always reflects documents that actually exist.
    """
    try:
        searchable = set(_search.indexed_doc_ids())
    except Exception:
        searchable = set()

    docs = _library_meta_records()
    known = {str(m.get("id")) for m in docs}

    # Self-heal: drop job files whose document is gone. Without this, every
    # doc deleted before job cleanup existed would haunt the panel forever.
    for job in _index.list_jobs():
        jid = str(job.get("doc_id") or "")
        if jid and jid not in known:
            _index.delete_job(jid)

    jobs: list[dict[str, Any]] = []
    for meta in docs:
        doc_id = str(meta.get("id"))
        st = _index.status_for(doc_id) if _index.job_exists(doc_id) else {}
        has_rag = _index.has_embeddings(doc_id)
        is_searchable = doc_id in searchable

        # A live job state always wins — it is the only thing that can explain
        # a document that is mid-flight. Otherwise the derived indexes decide,
        # because they are the truth about what is actually queryable.
        status = str(st.get("status") or "idle")
        if status in ("running", "paused", "error"):
            state = status
        elif is_searchable and has_rag:
            state = "indexed"
        elif is_searchable:
            state = "partial"
        else:
            state = "unindexed"

        jobs.append({
            "doc_id": doc_id,
            "title": str(meta.get("title") or ""),
            "pages": int(meta.get("pages") or 0),
            "size": int(meta.get("size") or 0),
            "added": float(meta.get("added") or 0.0),
            "state": state,
            "indexed_search": is_searchable,
            "indexed_rag": has_rag,
            # Job counters, defaulted so the panel never renders "undefined".
            "status": status,
            "stage": str(st.get("stage") or ""),
            "pipeline": st.get("pipeline") or [],
            "completed": st.get("completed") or [],
            "live": bool(st.get("live")),
            "stop_requested": bool(st.get("stop_requested")),
            "ocr_done": int(st.get("ocr_done") or 0),
            "ocr_total": int(st.get("ocr_total") or 0),
            "ocr_failed": int(st.get("ocr_failed") or 0),
            "search_done": int(st.get("search_done") or 0),
            "search_total": int(st.get("search_total") or 0),
            "ai_cursor": int(st.get("ai_cursor") or 0),
            "ai_total": int(st.get("ai_total") or 0),
            "percent": int(st.get("percent") or 0),
            "updated": float(st.get("updated") or 0.0),
            "error": str(st.get("error") or ""),
        })

    return {
        "jobs": jobs,
        "active": sum(1 for j in jobs if j.get("state") == "running"),
        "total": len(jobs),
    }


@router.get("/api/study/index/status")
def study_index_status(doc_id: str = "") -> dict[str, Any]:
    """Index state for one doc, or for the whole library.

    Without ``doc_id`` every library document is returned, joined with its
    job state and its derived indexes — so the Indexing tab can list what is
    running, what is searchable, and what still needs indexing.
    """
    if doc_id:
        if not _valid_id(doc_id):
            raise HTTPException(status_code=400, detail="invalid doc id")
        return _index.status_for(doc_id)
    return _index_overview()


@router.post("/api/study/index/stream")
async def study_index_stream(payload: dict[str, Any] | None = None) -> StreamingResponse:
    """SSE: run an index job for one doc, streaming live progress.

    body: {doc_id, ocr?: bool, ai?: bool, lang?: str, model?: str, thinking_mode?: str}
    """
    import json as _json

    payload = payload or {}
    doc_id = str(payload.get("doc_id") or "")
    if not _valid_id(doc_id):
        raise HTTPException(status_code=400, detail="invalid doc id")
    if not _read_meta(doc_id):
        raise HTTPException(status_code=404, detail="document not found")

    do_ocr = bool(payload.get("ocr", True))
    do_search = bool(payload.get("search", True))
    do_ai = bool(payload.get("ai", False))
    lang = str(payload.get("lang") or "eng+ben")
    model = str(payload.get("model") or "")
    thinking_mode = str(payload.get("thinking_mode") or "")

    queue: asyncio.Queue = asyncio.Queue()
    stop_flag = {"stop": False}

    def _on_progress(event: dict[str, Any]) -> None:
        try:
            queue.put_nowait(event)
        except Exception:
            pass

    async def _worker() -> None:
        try:
            await _index.run_job(
                doc_id, do_ocr=do_ocr, do_search=do_search, do_ai=do_ai, lang=lang,
                model=model, thinking_mode=thinking_mode,
                on_progress=_on_progress,
                # Stop when the client goes away OR the job was asked to stop
                # via /index/stop — otherwise the modal's Stop button would
                # have no effect on a streamed run.
                should_stop=lambda: stop_flag["stop"] or _index.is_stop_requested(doc_id),
            )
            queue.put_nowait({"done": True})
        except Exception as exc:
            queue.put_nowait({"error": str(exc)})

    async def _gen():
        task = asyncio.create_task(_worker())
        yield f"data: {_json.dumps({'type': 'init', 'doc_id': doc_id, 'ocr': do_ocr, 'search': do_search, 'ai': do_ai})}\n\n"
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if event.get("done"):
                    yield f"data: {_json.dumps({'type': 'complete', 'doc_id': doc_id})}\n\n"
                    break
                if event.get("error"):
                    yield f"data: {_json.dumps({'type': 'error', 'message': event['error']})}\n\n"
                    break
                yield f"data: {_json.dumps({'type': 'progress', **event})}\n\n"
        finally:
            stop_flag["stop"] = True
            if not task.done():
                task.cancel()

    return StreamingResponse(_gen(), media_type="text/event-stream")


@router.get("/api/study/search/stats")
def study_search_stats() -> dict[str, Any]:
    """Index health: how many docs are searchable, and which ones aren't.

    Returns the two doc lists by name (not just counts) so the rail search
    panel can show unindexed documents and let each one be indexed on its
    own, instead of only offering an all-or-nothing rebuild.
    """
    _ensure_dirs()

    docs: list[dict[str, Any]] = []
    for f in LIBRARY_META_DIR.glob("*.json"):
        try:
            meta = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not meta.get("id"):
            continue
        docs.append({
            "id": str(meta.get("id")),
            "title": str(meta.get("title") or "Untitled"),
            "pages": int(meta.get("pages") or 0),
            "has_text": _has_text(meta),
            "added": meta.get("added", 0),
        })
    docs.sort(key=lambda d: d.get("added") or 0, reverse=True)

    try:
        indexed_ids = set(_search.indexed_doc_ids())
    except Exception:
        indexed_ids = set()

    indexed = [d for d in docs if d["id"] in indexed_ids]
    missing = [d for d in docs if d["id"] not in indexed_ids]

    rag_indexed = 0
    try:
        rag_indexed = sum(1 for _ in LIBRARY_EMBED_DIR.glob("*.json")) if LIBRARY_EMBED_DIR.exists() else 0
    except Exception:
        rag_indexed = 0

    return {
        "docs": len(docs),
        "search_indexed": len(indexed),
        "rag_indexed": rag_indexed,
        # Full lists — the panel renders these directly.
        "indexed": indexed,
        "missing": missing,
    }


# ── Study knowledge graph ────────────────────────────────────────────────────

# A chat opened from the reader gets the id ``study-<docId>-<threadId>``
# (see study-chat.js). That naming convention is the ONLY link between a
# library document and its chats -- there is no join table.
_CHAT_ID_RE = re.compile(r"^study-([0-9a-f]{32})-", re.I)


def _read_json(path: Path, default: Any) -> Any:
    """Best-effort JSON read. A corrupt file must not take the graph down."""
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("could not read %s", path, exc_info=True)
    return default


def _graph_topics(meta: dict[str, Any]) -> list[str]:
    """Topic names a doc is filed under. Always at least one ("Others")."""
    name = str(((meta.get("group") or {}).get("topic")) or "").strip()
    return [name] if name else ["Others"]


@router.get("/api/study/graph")
def study_graph() -> dict[str, Any]:
    """Topic → document → (chats, notes, decks, highlights) graph.

    Built here rather than in the browser because assembling it client-side
    would mean cross-joining five separate endpoints. Every edge is derived
    from an existing field -- nothing is inferred:

      topic → doc        doc.group.topic
      doc   → chat       chat id prefix ``study-<docId>-``
      doc   → note       note.refs[].doc_id
      doc   → deck       deck.doc_id
      doc   → highlight  meta.highlights[] (one node per doc, with a count)
    """
    _ensure_dirs()

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    topics: set[str] = set()

    # ── Documents (+ their highlights) ──
    docs: list[dict[str, Any]] = []
    for f in LIBRARY_META_DIR.glob("*.json"):
        meta = _read_json(f, None)
        if not isinstance(meta, dict) or not meta.get("id"):
            continue
        docs.append(meta)
    docs.sort(key=lambda m: m.get("added") or 0, reverse=True)

    doc_ids: set[str] = set()
    for meta in docs:
        did = str(meta["id"])
        doc_ids.add(did)
        title = str(meta.get("title") or "Untitled")
        n_hl = len(meta.get("highlights") or [])

        for topic in _graph_topics(meta):
            topics.add(topic)
            edges.append({"data": {"source": "t:" + topic, "target": "d:" + did, "kind": "contains"}})

        nodes.append({
            "data": {
                "id": "d:" + did,
                "label": title[:40],
                "kind": "doc",
                "docId": did,
                "pages": int(meta.get("pages") or 0),
                "highlights": n_hl,
            }
        })

        if n_hl:
            hid = "h:" + did
            nodes.append({
                "data": {
                    "id": hid,
                    "label": f"{n_hl} highlight{'s' if n_hl != 1 else ''}",
                    "kind": "highlight",
                    "docId": did,
                    "count": n_hl,
                }
            })
            edges.append({"data": {"source": "d:" + did, "target": hid, "kind": "highlights"}})

    for t in sorted(topics):
        nodes.append({"data": {"id": "t:" + t, "label": t, "kind": "topic"}})

    # ── Chats (study-<docId>-<thread>) ──
    try:
        from server.database.chats import list_chats as _list_chats
        chats = _list_chats()
    except Exception:
        logger.warning("study graph: could not list chats", exc_info=True)
        chats = []

    for c in chats:
        cid = str(c.get("id") or "")
        m = _CHAT_ID_RE.match(cid)
        if not m:
            continue
        did = m.group(1)
        if did not in doc_ids:
            continue
        nodes.append({
            "data": {
                "id": "c:" + cid,
                "label": str(c.get("title") or "Study chat")[:40],
                "kind": "chat",
                "docId": did,
                "chatId": cid,
                "updated": c.get("updated_at") or "",
            }
        })
        edges.append({"data": {"source": "d:" + did, "target": "c:" + cid, "kind": "chat"}})

    # ── Notes (refs[].doc_id) ──
    notes_index = _read_json(LIBRARY_NOTES_DIR / "notes.json", {}) or {}
    for note in (notes_index.get("notes") or []):
        nid = str(note.get("id") or "")
        if not nid:
            continue
        # One note can reference several documents; draw an edge to each.
        linked = {
            str(r.get("doc_id"))
            for r in (note.get("refs") or [])
            if isinstance(r, dict) and r.get("doc_id")
        }
        linked &= doc_ids
        if not linked:
            continue
        nodes.append({
            "data": {
                "id": "n:" + nid,
                "label": str(note.get("title") or "Untitled note")[:40],
                "kind": "note",
                "noteId": nid,
                "pinned": bool(note.get("pinned")),
            }
        })
        for did in linked:
            edges.append({"data": {"source": "d:" + did, "target": "n:" + nid, "kind": "note"}})

    # ── Decks (deck.doc_id) ──
    decks_index = _read_json(LIBRARY_SRS_DIR / "decks.json", {}) or {}
    for deck in (decks_index.get("decks") or []):
        kid = str(deck.get("id") or "")
        did = str(deck.get("doc_id") or "")
        if not kid or did not in doc_ids:
            continue
        nodes.append({
            "data": {
                "id": "k:" + kid,
                "label": str(deck.get("name") or "Deck")[:40],
                "kind": "deck",
                "deckId": kid,
                "docId": did,
            }
        })
        edges.append({"data": {"source": "d:" + did, "target": "k:" + kid, "kind": "deck"}})

    return {
        "nodes": nodes,
        "edges": edges,
        "counts": {
            "topics": len(topics),
            "docs": len(doc_ids),
            "nodes": len(nodes),
            "edges": len(edges),
        },
    }


# ── Study sessions / analytics ───────────────────────────────────────────────


@router.post("/api/study/doc/{doc_id}/session")
def study_record_session(doc_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Record one reading session. Body: {seconds, pages?, started?, ended?}."""
    meta = _read_meta(doc_id)
    if not meta:
        raise HTTPException(status_code=404, detail="document not found")

    try:
        seconds = float(payload.get("seconds") or 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="seconds must be a number")

    raw_pages = payload.get("pages")
    pages: list[int] = []
    if isinstance(raw_pages, list):
        for item in raw_pages[:400]:
            try:
                pages.append(int(item))
            except (TypeError, ValueError):
                continue

    entry = _sessions.record(
        doc_id,
        seconds,
        started=payload.get("started"),
        ended=payload.get("ended"),
        pages=pages,
    )
    if entry is None:
        return {"recorded": False, "reason": "session too short"}

    # Mirror the total onto the doc meta so a card can show time-studied
    # without reading the whole session store.
    def mutate(m: dict[str, Any]) -> dict[str, Any]:
        m["study_seconds"] = round(float(m.get("study_seconds") or 0) + entry["seconds"], 1)
        m["last_studied"] = entry["ended"]
        return {"study_seconds": m["study_seconds"], "last_studied": m["last_studied"]}

    totals = _update_meta(doc_id, mutate)
    return {"recorded": True, "session": entry, **totals}


@router.get("/api/study/stats")
def study_stats(days: int = 30) -> dict[str, Any]:
    """Study-time analytics over a rolling window, with document titles."""
    data = _sessions.stats(days)
    for doc in data.get("top_docs", []):
        meta = _read_meta(doc.get("doc_id") or "")
        doc["title"] = str((meta or {}).get("title") or "Deleted document")
    return data


@router.get("/api/study/doc/{doc_id}/stats")
def study_doc_stats(doc_id: str) -> dict[str, Any]:
    meta = _read_meta(doc_id)
    if not meta:
        raise HTTPException(status_code=404, detail="document not found")
    data = _sessions.doc_stats(doc_id)
    data["title"] = meta.get("title") or ""
    data["pages"] = meta.get("pages") or 0
    return data


@router.delete("/api/study/stats")
def study_clear_stats(doc_id: str = "") -> dict[str, Any]:
    """Clear analytics. Pass doc_id to clear one document, omit to clear all."""
    if doc_id and not _valid_id(doc_id):
        raise HTTPException(status_code=400, detail="invalid doc id")
    removed = _sessions.clear(doc_id)
    return {"cleared": removed, "doc_id": doc_id}


# ── Markdown export ──────────────────────────────────────────────────────────


def _export_markdown(meta: dict[str, Any]) -> str:
    """Render a document's highlights, notes, and AI answers as markdown."""
    from engine.library_sessions import doc_stats as _doc_stats

    title = str(meta.get("title") or "Untitled document")
    lines: list[str] = [f"# {title}", ""]

    progress = meta.get("progress") or {}
    stats = _doc_stats(str(meta.get("id") or ""))
    lines += [
        f"- **Pages:** {meta.get('pages') or 0}",
        f"- **Progress:** {progress.get('percent') or 0}% (page {progress.get('current_page') or 1})",
        f"- **Highlights:** {len(meta.get('highlights') or [])}",
        f"- **Time studied:** {stats.get('minutes') or 0} min across {stats.get('sessions') or 0} sessions",
    ]
    group = _read_group(meta)
    tags = [f"{k}: {v}" for k, v in group.items() if v]
    if tags:
        lines.append("- **Filed under:** " + " / ".join(tags))
    lines.append("")

    highlights = meta.get("highlights") or []
    if highlights:
        lines += ["## Highlights", ""]
        for hl in sorted(highlights, key=lambda h: (int(h.get("page") or 0), float(h.get("added") or 0))):
            text = str(hl.get("text") or "").strip()
            note = str(hl.get("note") or "").strip()
            if not text and not note:
                continue
            lines.append(f"### Page {hl.get('page') or '?'}")
            if text:
                lines.append(f"> {text}")
            if note:
                lines += ["", note]
            lines.append("")

    answers = meta.get("ai_notes") or []
    if answers:
        lines += ["## AI Answers", ""]
        for item in answers:
            question = str(item.get("question") or item.get("action") or "Question").strip()
            body = str(item.get("answer") or item.get("content") or "").strip()
            if not body:
                continue
            page = item.get("page")
            heading = f"### {question}" + (f" (page {page})" if page else "")
            lines += [heading, "", body, ""]

    return "\n".join(lines).rstrip() + "\n"


@router.get("/api/study/doc/{doc_id}/export")
def study_export_doc(doc_id: str) -> Response:
    """Download a document's highlights, notes, and AI answers as markdown."""
    meta = _read_meta(doc_id)
    if not meta:
        raise HTTPException(status_code=404, detail="document not found")
    markdown = _export_markdown(meta)
    stem = str(meta.get("slug") or meta.get("id") or "document")
    return Response(
        content=markdown,
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{stem}.md"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/api/study/export")
def study_export_all() -> Response:
    """Export the whole library's highlights and AI answers as one markdown file."""
    _ensure_dirs()
    chunks: list[str] = ["# Sable study library export", ""]
    count = 0
    for f in sorted(LIBRARY_META_DIR.glob("*.json")):
        meta = _read_meta(f.stem)
        if not meta:
            continue
        if not (meta.get("highlights") or meta.get("ai_notes")):
            continue
        chunks += [_export_markdown(meta), "", "***", ""]
        count += 1
    stamp = time.strftime("%Y-%m-%d")
    return Response(
        content="\n".join(chunks).rstrip() + "\n",
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="sable-library-{stamp}.md"',
            "Cache-Control": "no-store",
        },
    )


# ── Academics: semesters → subjects → topics ─────────────────────────────────


def _ensure_academic_path(
    semester: str, subject: str = "", topic: str = "", session: str = ""
) -> None:
    """Idempotently create the semester/session/subject/topic chain.

    ``session`` is an independent axis (a cohort), so it's created regardless
    of whether a semester was supplied.
    """
    data = _read_academics()
    changed = False

    if session:
        sessions = data.setdefault("sessions", [])
        if not any(s.get("name") == session for s in sessions):
            sessions.append({"id": uuid.uuid4().hex[:12], "name": session})
            changed = True

    sem = None
    if semester:
        sem = next((s for s in data["semesters"] if s.get("name") == semester), None)
        if sem is None:
            sem = {"id": uuid.uuid4().hex[:12], "name": semester, "subjects": []}
            data["semesters"].append(sem)
            changed = True

    if sem is not None and subject:
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
    """Topics (primary flat list) + legacy semester tree + sessions."""
    return _read_academics()


# ── Topics (flat, primary organizing axis) ───────────────────────────────────


def _find_topic(data: dict[str, Any], topic_id: str) -> dict[str, Any] | None:
    return next((t for t in data.get("topics", []) if t.get("id") == topic_id), None)


@router.post("/api/study/academics/topics")
def academics_add_flat_topic(payload: dict[str, Any]) -> dict[str, Any]:
    name = str(payload.get("name") or "").strip()[:80]
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    data = _read_academics()
    topics = data.setdefault("topics", [])
    if any(t.get("name") == name for t in topics):
        raise HTTPException(status_code=409, detail="topic already exists")
    topic = {"id": uuid.uuid4().hex[:12], "name": name}
    topics.append(topic)
    _write_academics(data)
    return topic


@router.patch("/api/study/academics/topics/{topic_id}")
def academics_rename_flat_topic(topic_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    name = str(payload.get("name") or "").strip()[:80]
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    data = _read_academics()
    topic = _find_topic(data, topic_id)
    if not topic:
        raise HTTPException(status_code=404, detail="topic not found")
    old = topic["name"]
    topic["name"] = name
    _write_academics(data)
    _retag_docs("topic", old, name)
    return topic


# ── Sessions (flat cohort axis) ─────────────────────────────────────────────


def _find_session(data: dict[str, Any], session_id: str) -> dict[str, Any] | None:
    for s in data.get("sessions", []):
        if s.get("id") == session_id:
            return s
    return None


@router.post("/api/study/academics/sessions")
def academics_add_session(payload: dict[str, Any]) -> dict[str, Any]:
    name = str(payload.get("name") or "").strip()[:80]
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    data = _read_academics()
    sessions = data.setdefault("sessions", [])
    if any(s.get("name") == name for s in sessions):
        raise HTTPException(status_code=409, detail="session already exists")
    session = {"id": uuid.uuid4().hex[:12], "name": name}
    sessions.append(session)
    _write_academics(data)
    return session


@router.patch("/api/study/academics/sessions/{session_id}")
def academics_rename_session(session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    name = str(payload.get("name") or "").strip()[:80]
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    data = _read_academics()
    session = _find_session(data, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")
    old = session["name"]
    session["name"] = name
    _write_academics(data)
    _retag_docs("session", old, name)
    return session


@router.delete("/api/study/academics/sessions/{session_id}")
def academics_delete_session(session_id: str) -> dict[str, Any]:
    data = _read_academics()
    session = _find_session(data, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")
    data["sessions"] = [s for s in data.get("sessions", []) if s.get("id") != session_id]
    _write_academics(data)
    _retag_docs("session", session["name"], "")
    return {"deleted": True, "id": session_id}


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
    """Delete a topic from the flat list (and any legacy nested copy).

    Documents filed under a deleted topic are re-bucketed into "Others" so no
    document is ever left without its mandatory parent topic.
    """
    data = _read_academics()
    hit = _find_topic(data, topic_id)
    if hit:
        name = hit["name"]
        data["topics"] = [t for t in data.get("topics", []) if t.get("id") != topic_id]
        _write_academics(data)
        if name == "Others":
            # "Others" is the catch-all — recreate it so it always exists.
            _ensure_topic("Others")
        else:
            _retag_docs("topic", name, "Others")
            _ensure_topic("Others")
        return {"deleted": True, "id": topic_id}
    # Legacy nested location
    for sem in data["semesters"]:
        for subj in sem.get("subjects", []):
            topics = subj.get("topics", [])
            leg = next((t for t in topics if t.get("id") == topic_id), None)
            if leg:
                subj["topics"] = [t for t in topics if t.get("id") != topic_id]
                _write_academics(data)
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
