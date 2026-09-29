"""Topic -> document catalog for the study library (SQLite).

Retrieval used to answer "which docs share this topic?" by globbing
library/meta/*.json and parsing every record on each question. That is
O(library) work per query for a fact that changes only when a document is
uploaded, re-grouped, or deleted.

This keeps a tiny derived table in the same SQLite file the full-text index
already uses, so a topic lookup becomes one indexed SELECT. Like every other
index here it is rebuildable from the meta store, so it is never a source of
truth.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

from engine.config import LIBRARY_META_DIR, LIBRARY_SEARCH_DB

logger = logging.getLogger("sable.study.catalog")

OTHERS = "Others"

_UPSERT = (
    "INSERT INTO doc_catalog"
    "(doc_id, title, topic, semester, subject, added, updated)"
    " VALUES (?, ?, ?, ?, ?, ?, ?)"
    " ON CONFLICT(doc_id) DO UPDATE SET"
    "  title=excluded.title, topic=excluded.topic,"
    "  semester=excluded.semester, subject=excluded.subject,"
    "  updated=excluded.updated"
)


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


def ensure_catalog() -> None:
    """Create the catalog table when missing. Idempotent."""
    conn = _connect()
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS doc_catalog ("
            "  doc_id TEXT PRIMARY KEY,"
            "  title TEXT NOT NULL DEFAULT '',"
            "  topic TEXT NOT NULL DEFAULT '',"
            "  semester TEXT NOT NULL DEFAULT '',"
            "  subject TEXT NOT NULL DEFAULT '',"
            "  added REAL NOT NULL DEFAULT 0,"
            "  updated REAL NOT NULL DEFAULT 0"
            ")"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS doc_catalog_topic ON doc_catalog(topic)")
        conn.commit()
    finally:
        conn.close()


def upsert_doc(
    doc_id: str,
    title: str,
    group: dict[str, Any] | None = None,
    added: float = 0.0,
) -> None:
    """Insert or update one document row. Best-effort; never raises."""
    if not doc_id:
        return
    group = group or {}
    try:
        ensure_catalog()
        conn = _connect()
        try:
            conn.execute(
                _UPSERT,
                (
                    str(doc_id),
                    str(title or ""),
                    str(group.get("topic") or "").strip(),
                    str(group.get("semester") or ""),
                    str(group.get("subject") or ""),
                    float(added or 0.0),
                    time.time(),
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        logger.warning("catalog upsert failed for %s: %s", doc_id, exc)


def remove_doc(doc_id: str) -> None:
    """Drop a document row (called when a doc leaves the library)."""
    if not doc_id:
        return
    try:
        ensure_catalog()
        conn = _connect()
        try:
            conn.execute("DELETE FROM doc_catalog WHERE doc_id = ?", (str(doc_id),))
            conn.commit()
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        logger.warning("catalog remove failed for %s: %s", doc_id, exc)


def docs_in_topic(topic: str) -> list[dict[str, Any]]:
    """Every doc filed under ``topic``, title-sorted.

    Returns [] for the ``Others`` catch-all: that bucket is where unfiled
    PDFs land, so treating it as a topic would glue unrelated books together.
    The open document is included; callers decide whether a single-doc topic
    is worth widening.
    """
    topic = str(topic or "").strip()
    if not topic or topic == OTHERS:
        return []
    try:
        ensure_catalog()
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT doc_id, title FROM doc_catalog WHERE topic = ?"
                " ORDER BY lower(title)",
                (topic,),
            ).fetchall()
            out: list[dict[str, Any]] = []
            for row in rows:
                doc_id = str(row["doc_id"] or "")
                if doc_id:
                    out.append({"doc_id": doc_id, "title": str(row["title"] or "")})
            return out
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        logger.warning("catalog lookup failed for topic %s: %s", topic, exc)
        return []


def count() -> int:
    """How many documents the catalog currently tracks."""
    try:
        ensure_catalog()
        conn = _connect()
        try:
            return int(conn.execute("SELECT COUNT(*) FROM doc_catalog").fetchone()[0])
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return 0


def rebuild_from_meta(meta_dir: Path | None = None) -> dict[str, int]:
    """Rebuild the whole catalog from the meta store in one pass.

    Used on first run, after a schema bump, or lazily when a lookup finds the
    catalog empty on a library that predates it.
    """
    directory = meta_dir or LIBRARY_META_DIR
    rows: list[tuple[Any, ...]] = []
    skipped = 0
    if not directory.exists():
        return {"indexed": 0, "skipped": 0}
    for path in sorted(directory.glob("*.json")):
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            skipped += 1
            continue
        if not isinstance(meta, dict):
            skipped += 1
            continue
        group = meta.get("group") or {}
        rows.append(
            (
                str(meta.get("id") or path.stem),
                str(meta.get("title") or path.stem),
                str(group.get("topic") or "").strip(),
                str(group.get("semester") or ""),
                str(group.get("subject") or ""),
                float(meta.get("added") or 0.0),
                time.time(),
            )
        )
    if not rows:
        return {"indexed": 0, "skipped": skipped}
    try:
        ensure_catalog()
        conn = _connect()
        try:
            conn.executemany(_UPSERT, rows)
            conn.commit()
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        logger.warning("catalog rebuild failed: %s", exc)
        return {"indexed": 0, "skipped": skipped}
    return {"indexed": len(rows), "skipped": skipped}
