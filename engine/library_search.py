"""Full-text search across the study library (SQLite FTS5).

The library already extracts per-page text to ``library/text/<doc-id>.json``.
This mirrors that text into one FTS5 index so the whole library becomes
searchable -- "which document mentions Dijkstra, and on what page?" -- with
highlighted snippets.

Why SQLite here when the rest of study mode uses JSON + flock?
  * Search is read-heavy and needs token ranking; FTS5 gives that for free,
    and WAL mode keeps concurrent reads lock-free.
  * The index is *derived* data. If it is lost it can be rebuilt from the
    text files, so it never becomes a source of truth.

Every public function opens its own connection: FastAPI runs sync routes in a
threadpool, and a shared handle would race across requests.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from engine.config import LIBRARY_SEARCH_DB, LIBRARY_TEXT_DIR

logger = logging.getLogger("sable.study.search")

_INDEX_VERSION = 1
_SNIPPET_CHARS = 220
_DEFAULT_LIMIT = 40
_MAX_LIMIT = 200
_MAX_TOKENS = 12

# Cached capability probe. FTS5 ships with CPython's bundled SQLite on every
# mainstream build, but we degrade to a LIKE scan instead of breaking search.
_fts5_ok: bool | None = None


def _connect() -> sqlite3.Connection:
    LIBRARY_SEARCH_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(LIBRARY_SEARCH_DB), timeout=15.0)
    conn.row_factory = sqlite3.Row
    for pragma in ("PRAGMA journal_mode=WAL", "PRAGMA synchronous=NORMAL"):
        try:
            conn.execute(pragma)
        except sqlite3.DatabaseError:
            pass
    return conn


def _fts5_available(conn: sqlite3.Connection) -> bool:
    global _fts5_ok
    if _fts5_ok is None:
        try:
            conn.execute("CREATE VIRTUAL TABLE temp._fts_probe USING fts5(x)")
            conn.execute("DROP TABLE temp._fts_probe")
            _fts5_ok = True
        except sqlite3.DatabaseError:
            logger.warning("SQLite FTS5 unavailable - falling back to LIKE scan")
            _fts5_ok = False
    return _fts5_ok


def ensure_index() -> None:
    """Create the index tables when they do not exist yet. Idempotent."""
    conn = _connect()
    try:
        if _fts5_available(conn):
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS pages USING fts5("
                "  doc_id UNINDEXED, page UNINDEXED, title, body,"
                "  tokenize='unicode61 remove_diacritics 2'"
                ")"
            )
        else:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS pages ("
                "  doc_id TEXT NOT NULL, page INTEGER NOT NULL,"
                "  title TEXT, body TEXT"
                ")"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS pages_doc ON pages(doc_id)")
        conn.commit()
    finally:
        conn.close()


# ── Indexing ────────────────────────────────────────────────────────────────


def _norm(text: Any) -> str:
    """Collapse whitespace so snippets and tokenisation stay predictable."""
    return re.sub(r"\s+", " ", str(text or "")).strip()


def index_document(doc_id: str, title: str, pages: Iterable[str]) -> int:
    """(Re)index one document. Returns the number of pages written.

    Re-indexing is destructive-then-insert for this doc only, so a re-extract
    or edited page text never leaves stale rows behind.
    """
    if not doc_id:
        return 0
    ensure_index()
    clean_title = _norm(title)
    rows: list[tuple[str, int, str, str]] = []
    for number, raw in enumerate(pages, start=1):
        body = _norm(raw)
        if body:
            rows.append((doc_id, number, clean_title, body))

    conn = _connect()
    try:
        conn.execute("DELETE FROM pages WHERE doc_id = ?", (doc_id,))
        conn.executemany(
            "INSERT INTO pages(doc_id, page, title, body) VALUES (?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    logger.debug("indexed %d pages for %s", len(rows), doc_id)
    return len(rows)


def remove_document(doc_id: str) -> None:
    """Drop a document's rows (called when a doc is deleted from the library)."""
    if not doc_id:
        return
    ensure_index()
    conn = _connect()
    try:
        conn.execute("DELETE FROM pages WHERE doc_id = ?", (doc_id,))
        conn.commit()
    finally:
        conn.close()


def indexed_doc_count() -> int:
    """How many distinct documents currently have rows in the index."""
    ensure_index()
    conn = _connect()
    try:
        row = conn.execute("SELECT COUNT(DISTINCT doc_id) FROM pages").fetchone()
        return int(row[0] or 0)
    except sqlite3.DatabaseError:
        return 0
    finally:
        conn.close()


def rebuild_all() -> dict[str, int]:
    """Reindex every document from its stored page text.

    Used on first run, after a schema bump, or from the settings UI. Reads
    ``library/text/<doc-id>.json`` directly so it does not depend on the
    meta store being readable.
    """
    ensure_index()
    import json

    indexed = 0
    skipped = 0
    if not LIBRARY_TEXT_DIR.exists():
        return {"indexed": 0, "skipped": 0}
    for path in sorted(LIBRARY_TEXT_DIR.glob("*.json")):
        doc_id = path.stem
        try:
            pages = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            skipped += 1
            continue
        if not isinstance(pages, list):
            skipped += 1
            continue
        index_document(doc_id, "", pages)
        indexed += 1
    return {"indexed": indexed, "skipped": skipped}


# ── Querying ────────────────────────────────────────────────────────────────


def _fts_match(raw: str) -> str:
    """Build a safe FTS5 MATCH expression: every token quoted + prefix."""
    tokens = re.findall(r"[\w\u0080-\uffff]+", raw or "")
    if not tokens:
        return ""
    return " AND ".join(f'"{t}"*' for t in tokens[:_MAX_TOKENS])


def _like_terms(raw: str) -> list[str]:
    return [t.lower() for t in re.findall(r"[\w\u0080-\uffff]+", raw or "")[:_MAX_TOKENS]]


def _clip(text: str, needle: str) -> str:
    """A short window of text centred on the first occurrence of needle."""
    hay = text.lower()
    at = hay.find(needle.lower())
    if at < 0:
        return text[:_SNIPPET_CHARS] + ("\u2026" if len(text) > _SNIPPET_CHARS else "")
    start = max(0, at - _SNIPPET_CHARS // 2)
    end = min(len(text), start + _SNIPPET_CHARS)
    piece = text[start:end].strip()
    if start > 0:
        piece = "\u2026" + piece
    if end < len(text):
        piece += "\u2026"
    return piece


def search(
    query: str,
    *,
    limit: int = _DEFAULT_LIMIT,
    doc_id: str = "",
) -> dict[str, Any]:
    """Search indexed page text.

    Returns ``{query, engine, total, hits, docs}`` where ``hits`` are per-page
    matches and ``docs`` is a per-document rollup for grouping in the UI.
    """
    raw = str(query or "").strip()
    if not raw:
        return {"query": "", "engine": "none", "total": 0, "hits": [], "docs": []}

    limit = max(1, min(int(limit or _DEFAULT_LIMIT), _MAX_LIMIT))
    ensure_index()
    conn = _connect()
    try:
        if _fts5_available(conn):
            return _search_fts(conn, raw, limit, doc_id)
        return _search_like(conn, raw, limit, doc_id)
    finally:
        conn.close()


def _search_fts(conn: sqlite3.Connection, raw: str, limit: int, doc_id: str) -> dict[str, Any]:
    match = _fts_match(raw)
    if not match:
        return {"query": raw, "engine": "fts5", "total": 0, "hits": [], "docs": []}

    where = "pages MATCH ?"
    params: list[Any] = [match]
    if doc_id:
        where += " AND doc_id = ?"
        params.append(doc_id)

    total = int(
        conn.execute(f"SELECT COUNT(*) FROM pages WHERE {where}", params).fetchone()[0]
    )
    # Column weights: doc_id and page are UNINDEXED, so weight title above body.
    sql = (
        "SELECT doc_id, page, title,"
        "  snippet(pages, 3, '\u0002', '\u0003', '\u2026', 24) AS snip,"
        "  bm25(pages, 0.0, 0.0, 5.0, 1.0) AS score "
        f"FROM pages WHERE {where} ORDER BY score LIMIT ?"
    )
    rows = conn.execute(sql, [*params, limit]).fetchall()
    hits = [
        {
            "doc_id": r["doc_id"],
            "page": int(r["page"]),
            "title": r["title"] or "",
            "snippet": (r["snip"] or "").replace("\u0002", "<mark>").replace("\u0003", "</mark>"),
            "score": round(float(r["score"] or 0.0), 4),
        }
        for r in rows
    ]
    return {"query": raw, "engine": "fts5", "total": total, "hits": hits, "docs": _rollup(hits)}


def _search_like(conn: sqlite3.Connection, raw: str, limit: int, doc_id: str) -> dict[str, Any]:
    """Dependency-free fallback when FTS5 is missing."""
    terms = _like_terms(raw)
    if not terms:
        return {"query": raw, "engine": "like", "total": 0, "hits": [], "docs": []}

    clauses = " AND ".join("lower(body) LIKE ?" for _ in terms)
    params: list[Any] = [f"%{t}%" for t in terms]
    if doc_id:
        clauses += " AND doc_id = ?"
        params.append(doc_id)

    total = int(conn.execute(f"SELECT COUNT(*) FROM pages WHERE {clauses}", params).fetchone()[0])
    rows = conn.execute(
        f"SELECT doc_id, page, title, body FROM pages WHERE {clauses} LIMIT ?",
        [*params, limit],
    ).fetchall()
    hits = [
        {
            "doc_id": r["doc_id"],
            "page": int(r["page"]),
            "title": r["title"] or "",
            "snippet": _clip(r["body"] or "", terms[0]),
            "score": 0.0,
        }
        for r in rows
    ]
    return {"query": raw, "engine": "like", "total": total, "hits": hits, "docs": _rollup(hits)}


def _rollup(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group hits per document for the results sidebar."""
    buckets: dict[str, dict[str, Any]] = {}
    for hit in hits:
        bucket = buckets.setdefault(
            hit["doc_id"],
            {"doc_id": hit["doc_id"], "title": hit["title"], "count": 0, "pages": []},
        )
        bucket["count"] += 1
        if len(bucket["pages"]) < 8:
            bucket["pages"].append(hit["page"])
    return sorted(buckets.values(), key=lambda b: -b["count"])
