"""Shared document ingestion for the study library.

Both the Study upload endpoint and remote sources (Google Drive) funnel
through here, so a downloaded file becomes indistinguishable from an uploaded
one: same PDF normalization, same extracted text, same meta record. The Study
frontend therefore shows Drive downloads in the library with zero extra work.

Storage layout (mirrors engine.config):
    ~/sable_output/library/pdfs/<doc-id>.pdf
    ~/sable_output/library/meta/<doc-id>.json
    ~/sable_output/library/text/<doc-id>.json
    ~/sable_output/library/originals/<doc-id>.<ext>
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from engine.config import (
    LIBRARY_META_DIR,
    LIBRARY_ORIG_DIR,
    LIBRARY_PDF_DIR,
    LIBRARY_TEXT_DIR,
    LIBRARY_THUMB_DIR,
)

logger = logging.getLogger("sable.library")

_MAX_BYTES = 100 * 1024 * 1024
# Longest edge of a generated card preview, in pixels.
_THUMB_MAX = 320


def ensure_thumb_dir() -> None:
    LIBRARY_THUMB_DIR.mkdir(parents=True, exist_ok=True)


def thumb_path(doc_id: str) -> Path:
    return LIBRARY_THUMB_DIR / f"{doc_id}.jpg"


def make_thumbnail(pdf_bytes: bytes) -> bytes | None:
    """Render page 1 of a PDF to a small JPEG. Returns None on any failure.

    Previews are cosmetic — a document that can't be rasterized still imports.
    """
    try:
        import pymupdf  # type: ignore

        doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
        if doc.page_count < 1:
            doc.close()
            return None
        page = doc.load_page(0)
        rect = page.rect
        longest = max(rect.width, rect.height, 1.0)
        zoom = min(0.6, _THUMB_MAX / longest)
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        data = pix.tobytes("jpeg", jpg_quality=72)
        doc.close()
        return data
    except Exception as exc:
        logger.debug("thumbnail generation failed: %s", exc)
        return None
_NATIVE_EXT = ".pdf"
_CONVERTIBLE_EXT = {
    ".docx", ".doc", ".pptx", ".ppt", ".odt", ".odp", ".ods", ".rtf",
    ".txt", ".csv", ".md", ".epub", ".xlsx", ".xls",
}
_ACCEPTED_EXT = {_NATIVE_EXT, *_CONVERTIBLE_EXT}
_CONVERT_TIMEOUT = 120
_META_VERSION = 1


def ext_of(filename: str) -> str:
    return Path(filename or "").suffix.lower()


def ensure_dirs() -> None:
    for d in (LIBRARY_PDF_DIR, LIBRARY_META_DIR, LIBRARY_ORIG_DIR, LIBRARY_TEXT_DIR, LIBRARY_THUMB_DIR):
        d.mkdir(parents=True, exist_ok=True)


def slugify(text: str, fallback: str = "document") -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return (slug or fallback)[:60]


def sanitize_filename(name: str, fallback: str = "document") -> str:
    """Strip control chars and path separators; cap the length.

    The name is stored in the meta JSON and echoed back to the UI, so a
    hostile share could otherwise carry markup or newlines into the record.
    """
    cleaned = re.sub(r"[\x00-\x1f\x7f]", "", name or "")
    cleaned = cleaned.replace("/", "_").replace(chr(92), "_")
    cleaned = cleaned.strip().strip(".")
    return cleaned[:200] or fallback


def pdf_tool() -> str | None:
    """Locate the LibreOffice CLI, cross-platform.

    PATH first (Linux/macOS usually); then the well-known install locations
    Windows and macOS use, since neither reliably adds soffice to PATH.
    """
    for name in ("soffice", "libreoffice"):
        found = shutil.which(name)
        if found:
            return found
    if os.name == "nt":
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
            if base:
                cand = Path(base) / "LibreOffice" / "program" / "soffice.exe"
                if cand.exists():
                    return str(cand)
    else:
        for cand in (
            "/Applications/LibreOffice.app/Contents/MacOS/soffice",
            "/usr/bin/soffice",
            "/usr/local/bin/soffice",
            "/snap/bin/libreoffice",
        ):
            if Path(cand).exists():
                return cand
    return None


def _kill_process_group(proc: subprocess.Popen) -> None:
    """Kill the converter and every child it spawned, on any OS.

    POSIX: kill the whole process group (LibreOffice forks). Windows: taskkill
    /T walks the child tree, because os.killpg doesn't exist there.
    """
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=10,
            )
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    try:
        proc.wait(timeout=5)
    except Exception:
        pass


def convert_to_pdf(raw: bytes, ext: str) -> bytes:
    """Convert a non-PDF document to PDF via headless LibreOffice."""
    tool = pdf_tool()
    if not tool:
        raise ValueError(
            "LibreOffice is not installed — cannot convert this file. "
            "Install 'libreoffice' or use a PDF."
        )
    with tempfile.TemporaryDirectory(prefix="sable-conv-") as td:
        work = Path(td)
        src = work / f"input{ext}"
        src.write_bytes(raw)
        outdir = work / "out"
        outdir.mkdir()
        profile = work / "profile"
        # as_uri() gives the file:///C:/... form LibreOffice expects on Windows
        # (file://C:\... is malformed and silently ignored).
        cmd = [
            tool,
            "--headless", "--norestore", "--invisible",
            f"-env:UserInstallation={profile.as_uri()}",
            "--convert-to", "pdf",
            "--outdir", str(outdir),
            str(src),
        ]
        try:
            # start_new_session isolates the process group on POSIX (so we can
            # kill forks); it's a no-op on Windows, where taskkill /T handles it.
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=(os.name != "nt"),
            )
        except OSError as e:
            raise ValueError(f"could not launch the converter: {e}")
        try:
            _, err_bytes = proc.communicate(timeout=_CONVERT_TIMEOUT)
        except subprocess.TimeoutExpired:
            _kill_process_group(proc)
            raise ValueError("conversion timed out")
        produced = list(outdir.glob("*.pdf"))
        if not produced:
            stderr = (err_bytes or b"").decode("utf-8", "ignore").strip()
            raise ValueError(
                "could not convert this file to PDF"
                + (f" ({stderr[:200]})" if stderr else "")
            )
        return produced[0].read_bytes()


def extract_pdf(raw: bytes) -> dict[str, Any]:
    """Per-page text + outline. PyMuPDF first, pypdf as fallback."""
    try:
        import pymupdf  # type: ignore

        doc = pymupdf.open(stream=raw, filetype="pdf")
        page_text = [(p.get_text("text") or "") for p in doc]
        outline: list[dict[str, Any]] = []
        try:
            for item in doc.get_toc() or []:
                if len(item) >= 3:
                    outline.append({
                        "level": int(item[0]),
                        "title": str(item[1]),
                        "page": max(1, int(item[2])),
                    })
        except Exception:
            outline = []
        meta_title = ""
        try:
            meta_title = (doc.metadata or {}).get("title") or ""
        except Exception:
            meta_title = ""
        pages = doc.page_count
        doc.close()
        if any(t.strip() for t in page_text):
            return {
                "pages": pages,
                "page_text": page_text,
                "outline": outline,
                "meta_title": meta_title.strip(),
            }
        logger.info("pymupdf found no extractable text — trying pypdf")
    except Exception as exc:
        logger.warning("pymupdf extraction failed, falling back to pypdf: %s", exc)

    try:
        import io

        from pypdf import PdfReader  # type: ignore

        reader = PdfReader(io.BytesIO(raw))
        page_text = [(p.extract_text() or "") for p in reader.pages]
        if any(t.strip() for t in page_text):
            meta_title = ""
            try:
                meta_title = (reader.metadata or {}).get("/Title", "") or ""
            except Exception:
                meta_title = ""
            return {
                "pages": len(page_text),
                "page_text": page_text,
                "outline": [],
                "meta_title": str(meta_title).strip(),
            }
    except Exception as exc:
        logger.warning("pypdf extraction failed: %s", exc)

    raise ValueError("no extractable text (scanned/image-only PDF?)")


def _write_json_atomic(target: Path, data: Any) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(target)


def _write_bytes_atomic(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(target)


def ingest_bytes(
    raw: bytes,
    filename: str,
    *,
    source_url: str = "",
    source: str = "upload",
    collection: str = "",
    collection_id: str = "",
) -> dict[str, Any]:
    """Normalize + index a document into the study library.

    Returns the same summary shape as the study list endpoint, so callers can
    hand it straight to the frontend. Raises ValueError on any bad input.
    """
    if not filename:
        raise ValueError("missing filename")
    filename = sanitize_filename(filename)
    ext = ext_of(filename)
    if ext not in _ACCEPTED_EXT:
        allowed = ", ".join(sorted(_ACCEPTED_EXT))
        raise ValueError(f"unsupported file type '{ext or '?'}' — accepted: {allowed}")
    if not raw:
        raise ValueError("empty file")
    if len(raw) > _MAX_BYTES:
        raise ValueError("file too large (max 100 MB)")

    converted = False
    pdf_bytes = raw
    if ext != _NATIVE_EXT:
        pdf_bytes = convert_to_pdf(raw, ext)
        converted = True

    extracted = extract_pdf(pdf_bytes)

    ensure_dirs()
    doc_id = uuid.uuid4().hex

    title = (extracted.get("meta_title") or "").strip()
    if not title or title.lower() in {"untitled", "unknown"}:
        title = Path(filename).stem

    (LIBRARY_PDF_DIR / f"{doc_id}.pdf").write_bytes(pdf_bytes)
    if converted:
        (LIBRARY_ORIG_DIR / f"{doc_id}{ext}").write_bytes(raw)

    meta: dict[str, Any] = {
        "version": _META_VERSION,
        "id": doc_id,
        "title": title,
        "slug": slugify(title),
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
        # Topic is mandatory — docs ingested without one are filed under "Others".
        "group": {"semester": "", "subject": "", "topic": "Others"},
        "source": source,
        "source_url": source_url,
        # Drive folder (or similar) this doc was pulled from, for grouping.
        "collection": collection,
        "collection_id": collection_id,
    }
    _write_json_atomic(LIBRARY_TEXT_DIR / f"{doc_id}.json", extracted["page_text"])
    _write_json_atomic(LIBRARY_META_DIR / f"{doc_id}.json", meta)

    # Page-1 preview — cached so the grid never re-renders a PDF.
    thumb = make_thumbnail(pdf_bytes)
    if thumb:
        try:
            _write_bytes_atomic(thumb_path(doc_id), thumb)
        except Exception as exc:
            logger.debug("could not cache thumbnail: %s", exc)

    return {
        "id": doc_id,
        "title": title,
        "filename": filename,
        "pages": meta["pages"],
        "size": meta["size"],
        "added": meta["added"],
        "converted": converted,
        "source_ext": meta["source_ext"],
        "highlights": 0,
        "has_text": True,
        "group": meta["group"],
        "progress": meta["progress"],
    }
