"""PDF access for study tools — text search, rect mapping, page rendering.

Wraps PyMuPDF so the study handlers can:
  * locate a phrase on a page and turn it into normalised highlight rects
    (the same 0–1 coordinate space the reader stores),
  * render one page to PNG for vision models,
  * slice per-page text for the on-demand reading tools.

PyMuPDF is imported lazily: a text-only tool call must not pay the import
cost, and a missing PyMuPDF degrades to the already-extracted text store.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from engine.config import LIBRARY_PDF_DIR

logger = logging.getLogger("sable.study.pdf")


def pdf_path(doc_id: str) -> Path:
    return LIBRARY_PDF_DIR / f"{doc_id}.pdf"


def _open(doc_id: str):
    import pymupdf  # type: ignore

    path = pdf_path(doc_id)
    if not path.exists():
        return None
    return pymupdf.open(str(path))


def find_text_rects(doc_id: str, page: int, needle: str) -> list[dict[str, float]]:
    """Normalised rects for every occurrence of ``needle`` on ``page``.

    Coordinates are divided by the page box so the reader can place them with
    percentage CSS, exactly like a rect a user dragged out by hand. Matching is
    case-insensitive; a blank needle matches nothing.
    """
    text = (needle or "").strip()
    if not text:
        return []
    doc = _open(doc_id)
    if doc is None:
        return []
    try:
        if page < 1 or page > doc.page_count:
            return []
        pg = doc.load_page(page - 1)
        pw, ph = pg.rect.width or 1.0, pg.rect.height or 1.0
        out: list[dict[str, float]] = []
        for quad in pg.search_for(text, quads=False):
            x0, y0, x1, y1 = quad.x0, quad.y0, quad.x1, quad.y1
            w = (x1 - x0) / pw
            h = (y1 - y0) / ph
            if w <= 0 or h <= 0:
                continue
            out.append({
                "x": round(x0 / pw, 5),
                "y": round(y0 / ph, 5),
                "w": round(w, 5),
                "h": round(h, 5),
            })
            if len(out) >= 40:
                break
        return out
    except Exception as exc:
        logger.debug("find_text_rects failed (%s p%s): %s", doc_id, page, exc)
        return []
    finally:
        try:
            doc.close()
        except Exception:
            pass


def render_page_png(doc_id: str, page: int, scale: float = 1.6) -> bytes | None:
    """Render a single page to PNG bytes, or None when unavailable."""
    doc = _open(doc_id)
    if doc is None:
        return None
    try:
        if page < 1 or page > doc.page_count:
            return None
        import pymupdf  # type: ignore

        pg = doc.load_page(page - 1)
        pix = pg.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
        return pix.tobytes("png")
    except Exception as exc:
        logger.debug("render_page_png failed (%s p%s): %s", doc_id, page, exc)
        return None
    finally:
        try:
            doc.close()
        except Exception:
            pass


def page_count(doc_id: str) -> int:
    doc = _open(doc_id)
    if doc is None:
        return 0
    try:
        return int(doc.page_count)
    finally:
        try:
            doc.close()
        except Exception:
            pass


def search_document(doc_id: str, needle: str, max_hits: int = 30) -> list[dict[str, Any]]:
    """Locate a phrase across the whole PDF. Returns page + normalised rects."""
    text = (needle or "").strip()
    if not text:
        return []
    doc = _open(doc_id)
    if doc is None:
        return []
    hits: list[dict[str, Any]] = []
    try:
        for i in range(doc.page_count):
            pg = doc.load_page(i)
            pw, ph = pg.rect.width or 1.0, pg.rect.height or 1.0
            quads = pg.search_for(text, quads=False)
            if not quads:
                continue
            rects = [
                {
                    "x": round(q.x0 / pw, 5),
                    "y": round(q.y0 / ph, 5),
                    "w": round((q.x1 - q.x0) / pw, 5),
                    "h": round((q.y1 - q.y0) / ph, 5),
                }
                for q in quads[:20]
            ]
            hits.append({"page": i + 1, "rects": rects})
            if len(hits) >= max_hits:
                break
    except Exception as exc:
        logger.debug("search_document failed (%s): %s", doc_id, exc)
    finally:
        try:
            doc.close()
        except Exception:
            pass
    return hits
