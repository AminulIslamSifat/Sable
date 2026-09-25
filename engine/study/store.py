"""Study-mode data access — documents, highlights, notes, and diagnostics.

One module owns every read/write against the study stores so both the HTTP
routes (``server/api/routes/study*.py``) and the study skill handlers can call
the same code path. This keeps the on-disk formats in a single place: the meta
JSON per document, the per-page text file, and the notes index + markdown body.

Locking mirrors the routes exactly — ``fcntl.flock`` on a sidecar lock file —
so a handler writing a highlight concurrently with a browser PATCH cannot tear
the read-modify-write cycle.
"""
from __future__ import annotations

import fcntl
import json
import re
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from engine.config import (
    LIBRARY_DIR,
    LIBRARY_META_DIR,
    LIBRARY_NOTES_DIR,
    LIBRARY_PDF_DIR,
    LIBRARY_TEXT_DIR,
)

_DOC_ID_RE = re.compile(r"[0-9a-f]{32}")
_NOTE_ID_RE = re.compile(r"[0-9a-f]{32}")

_MAX_TITLE = 200
_MAX_BODY = 2_000_000
_MAX_REFS = 50
_MAX_TAGS = 30
_HIGHLIGHT_TEXT = 2000


# ── Paths ────────────────────────────────────────────────────────────────────


def _meta_path(doc_id: str) -> Path:
    return LIBRARY_META_DIR / f"{doc_id}.json"


def _pdf_path(doc_id: str) -> Path:
    return LIBRARY_PDF_DIR / f"{doc_id}.pdf"


def _text_path(doc_id: str) -> Path:
    return LIBRARY_TEXT_DIR / f"{doc_id}.json"


def _meta_lock_path(doc_id: str) -> Path:
    return LIBRARY_META_DIR / f"{doc_id}.lock"


def _note_index_path() -> Path:
    return LIBRARY_NOTES_DIR / "notes.json"


def _note_body_path(note_id: str) -> Path:
    return LIBRARY_NOTES_DIR / f"{note_id}.md"


def _note_lock_path() -> Path:
    return LIBRARY_NOTES_DIR / ".notes.lock"


def _valid_doc_id(doc_id: str) -> bool:
    return bool(_DOC_ID_RE.fullmatch(doc_id or ""))


def _valid_note_id(note_id: str) -> bool:
    return bool(_NOTE_ID_RE.fullmatch(note_id or ""))


def _ensure_dirs() -> None:
    LIBRARY_META_DIR.mkdir(parents=True, exist_ok=True)
    LIBRARY_NOTES_DIR.mkdir(parents=True, exist_ok=True)


# ── Locks ────────────────────────────────────────────────────────────────────


@contextmanager
def _meta_lock(doc_id: str) -> Iterator[None]:
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


@contextmanager
def _note_lock() -> Iterator[None]:
    _ensure_dirs()
    fh = open(_note_lock_path(), "w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


# ── Document meta ────────────────────────────────────────────────────────────


def read_meta(doc_id: str) -> dict[str, Any] | None:
    """Load a document's meta record, or None when it does not exist."""
    if not _valid_doc_id(doc_id):
        return None
    path = _meta_path(doc_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_meta(doc_id: str, meta: dict[str, Any]) -> None:
    _ensure_dirs()
    meta["updated"] = time.time()
    target = _meta_path(doc_id)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    tmp.replace(target)


def _mutate_meta(doc_id: str, mutator: Callable[[dict[str, Any]], Any]) -> Any:
    """Atomic read-modify-write of a doc's meta under its exclusive lock."""
    with _meta_lock(doc_id):
        meta = read_meta(doc_id)
        if meta is None:
            raise FileNotFoundError(f"document not found: {doc_id}")
        result = mutator(meta)
        _write_meta(doc_id, meta)
        return result


def list_docs() -> list[dict[str, Any]]:
    """Every document in the library, newest first, with a light summary."""
    _ensure_dirs()
    out: list[dict[str, Any]] = []
    for path in LIBRARY_META_DIR.glob("*.json"):
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(meta, dict):
            continue
        out.append({
            "id": meta.get("id"),
            "title": meta.get("title") or path.stem,
            "pages": int(meta.get("pages") or 0),
            "size": int(meta.get("size") or 0),
            "added": float(meta.get("added") or 0),
            "progress": meta.get("progress") or {},
            "highlights": len(meta.get("highlights") or []),
            "bookmarked": bool(meta.get("bookmarked")),
            "source": meta.get("source") or "upload",
            "collection": meta.get("collection") or "",
        })
    out.sort(key=lambda d: -float(d.get("added") or 0))
    return out


def page_text_for(doc_id: str) -> list[str]:
    """Per-page extracted text for a document ([] when none was stored)."""
    if not _valid_doc_id(doc_id):
        return []
    path = _text_path(doc_id)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [str(t or "") for t in data]


# ── Highlights / bookmark / progress ─────────────────────────────────────────


def _clean_rects(raw: Any) -> list[dict[str, float]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, float]] = []
    for r in raw[:200]:
        if not isinstance(r, dict):
            continue
        try:
            out.append({
                "x": float(r.get("x", 0)),
                "y": float(r.get("y", 0)),
                "w": float(r.get("w", 0)),
                "h": float(r.get("h", 0)),
            })
        except (TypeError, ValueError):
            continue
    return out


def add_highlight(doc_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Append a highlight. Requires ``page`` and at least one rect."""
    page = int(payload.get("page") or 0)
    if page < 1:
        raise ValueError("page is required")
    rects = _clean_rects(payload.get("rects"))
    if not rects:
        raise ValueError("rects is required")

    highlight = {
        "id": uuid.uuid4().hex[:12],
        "page": page,
        "rects": rects,
        "color": str(payload.get("color") or "#ffd400")[:32],
        "text": str(payload.get("text") or "")[:_HIGHLIGHT_TEXT],
        "note": str(payload.get("note") or "")[:_HIGHLIGHT_TEXT],
        "created": time.time(),
    }

    def mutate(meta: dict[str, Any]) -> dict[str, Any]:
        highlights = meta.get("highlights") or []
        highlights.append(highlight)
        meta["highlights"] = highlights
        return highlight

    return _mutate_meta(doc_id, mutate)


def list_highlights(doc_id: str) -> list[dict[str, Any]]:
    meta = read_meta(doc_id)
    if meta is None:
        return []
    return list(meta.get("highlights") or [])


def update_highlight(doc_id: str, hl_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    def mutate(meta: dict[str, Any]) -> dict[str, Any]:
        for hl in meta.get("highlights") or []:
            if hl.get("id") != hl_id:
                continue
            if "note" in payload:
                hl["note"] = str(payload["note"])[:_HIGHLIGHT_TEXT]
            if "color" in payload:
                hl["color"] = str(payload["color"])[:32]
            return hl
        raise KeyError(f"highlight not found: {hl_id}")

    return _mutate_meta(doc_id, mutate)


def delete_highlight(doc_id: str, hl_id: str) -> dict[str, Any]:
    def mutate(meta: dict[str, Any]) -> dict[str, Any]:
        highlights = meta.get("highlights") or []
        kept = [h for h in highlights if h.get("id") != hl_id]
        if len(kept) == len(highlights):
            raise KeyError(f"highlight not found: {hl_id}")
        meta["highlights"] = kept
        return {"deleted": True, "id": hl_id}

    return _mutate_meta(doc_id, mutate)


def set_bookmark(doc_id: str, value: bool | None = None) -> dict[str, Any]:
    """Toggle the bookmark, or force it when ``value`` is given."""

    def mutate(meta: dict[str, Any]) -> dict[str, Any]:
        meta["bookmarked"] = (not bool(meta.get("bookmarked"))) if value is None else bool(value)
        return {"bookmarked": meta["bookmarked"]}

    return _mutate_meta(doc_id, mutate)


def set_progress(doc_id: str, current_page: int | None = None, percent: int | None = None) -> dict[str, Any]:
    def mutate(meta: dict[str, Any]) -> dict[str, Any]:
        progress = meta.get("progress") or {}
        total = max(1, int(meta.get("pages") or 1))
        if current_page is not None:
            progress["current_page"] = max(1, min(int(current_page), total))
        progress.setdefault("current_page", 1)
        if percent is not None:
            progress["percent"] = max(0, min(100, int(percent)))
        else:
            progress["percent"] = int(round((progress["current_page"] - 1) / total * 100))
        progress["last_opened"] = time.time()
        meta["progress"] = progress
        return progress

    return _mutate_meta(doc_id, mutate)


def get_outline(doc_id: str) -> list[dict[str, Any]]:
    meta = read_meta(doc_id)
    if meta is None:
        return []
    outline = meta.get("outline")
    return list(outline) if isinstance(outline, list) else []


# ── Notes ────────────────────────────────────────────────────────────────────


def _read_note_index() -> dict[str, Any]:
    path = _note_index_path()
    if not path.exists():
        return {"version": 1, "notes": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "notes": []}
    if not isinstance(data, dict) or not isinstance(data.get("notes"), list):
        return {"version": 1, "notes": []}
    return data


def _write_note_index(data: dict[str, Any]) -> None:
    _ensure_dirs()
    data["version"] = 1
    target = _note_index_path()
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(target)


def read_note_body(note_id: str) -> str:
    if not _valid_note_id(note_id):
        return ""
    path = _note_body_path(note_id)
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _write_note_body(note_id: str, content: str) -> None:
    _ensure_dirs()
    target = _note_body_path(note_id)
    tmp = target.with_suffix(".md.tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(target)


def find_note(note_id: str) -> dict[str, Any] | None:
    if not _valid_note_id(note_id):
        return None
    for note in _read_note_index().get("notes", []):
        if note.get("id") == note_id:
            return note
    return None


def _clean_refs(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw[:_MAX_REFS]:
        if not isinstance(item, dict):
            continue
        doc_id = str(item.get("doc_id") or "").strip()
        if not doc_id:
            continue
        kind = str(item.get("kind") or "doc").strip().lower()
        if kind not in ("doc", "page", "selection"):
            kind = "doc"
        try:
            page = int(item.get("page") or 0)
        except (TypeError, ValueError):
            page = 0
        ref: dict[str, Any] = {
            "doc_id": doc_id[:64],
            "title": str(item.get("title") or "")[:300],
            "kind": kind,
            "page": max(0, page),
            "text": str(item.get("text") or "")[:2000],
        }
        rects = _clean_rects(item.get("rects"))
        if rects:
            ref["rects"] = rects
        out.append(ref)
    return out


def _clean_tags(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    seen: list[str] = []
    for t in raw[:_MAX_TAGS]:
        tag = str(t or "").strip()[:40]
        if tag and tag not in seen:
            seen.append(tag)
    return seen


def _summarize_note(note: dict[str, Any], body: str = "") -> dict[str, Any]:
    snippet = (body or "").strip()
    return {
        "id": note.get("id"),
        "title": note.get("title") or "Untitled note",
        "refs": note.get("refs") or [],
        "tags": note.get("tags") or [],
        "pinned": bool(note.get("pinned")),
        "created": note.get("created", 0),
        "updated": note.get("updated", 0),
        "snippet": snippet[:400],
    }


def list_notes(doc_id: str = "", query: str = "", pinned_only: bool = False) -> list[dict[str, Any]]:
    """Note summaries, newest-updated first, with optional filters."""
    _ensure_dirs()
    needle = (query or "").strip().lower()
    out: list[dict[str, Any]] = []
    for note in _read_note_index().get("notes", []):
        nid = note.get("id")
        if not nid:
            continue
        if doc_id and not any(r.get("doc_id") == doc_id for r in (note.get("refs") or [])):
            continue
        if pinned_only and not note.get("pinned"):
            continue
        body = read_note_body(nid)
        if needle and needle not in (note.get("title") or "").lower() and needle not in body.lower():
            continue
        out.append(_summarize_note(note, body))
    out.sort(key=lambda n: (not n["pinned"], -float(n.get("updated") or 0)))
    return out


def get_note(note_id: str) -> dict[str, Any] | None:
    note = find_note(note_id)
    if note is None:
        return None
    body = read_note_body(note_id)
    return {**_summarize_note(note, body), "content": body}


def create_note(payload: dict[str, Any]) -> dict[str, Any]:
    note_id = uuid.uuid4().hex
    now = time.time()
    title = str(payload.get("title") or "").strip()[:_MAX_TITLE] or "Untitled note"
    content = str(payload.get("content") or "")[:_MAX_BODY]
    note = {
        "id": note_id,
        "title": title,
        "refs": _clean_refs(payload.get("refs")),
        "tags": _clean_tags(payload.get("tags")),
        "pinned": bool(payload.get("pinned")),
        "created": now,
        "updated": now,
    }
    with _note_lock():
        index = _read_note_index()
        index["notes"].append(note)
        _write_note_index(index)
        _write_note_body(note_id, content)
    return {**_summarize_note(note, content), "content": content}


def update_note(note_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Update any subset of a note's fields; missing keys are left untouched."""
    if not _valid_note_id(note_id):
        raise ValueError("invalid note id")
    with _note_lock():
        index = _read_note_index()
        note = next((n for n in index.get("notes", []) if n.get("id") == note_id), None)
        if note is None:
            raise FileNotFoundError(f"note not found: {note_id}")
        if "title" in payload:
            note["title"] = str(payload.get("title") or "").strip()[:_MAX_TITLE] or "Untitled note"
        if "refs" in payload:
            note["refs"] = _clean_refs(payload.get("refs"))
        if "tags" in payload:
            note["tags"] = _clean_tags(payload.get("tags"))
        if "pinned" in payload:
            note["pinned"] = bool(payload.get("pinned"))
        note["updated"] = time.time()
        if "content" in payload:
            _write_note_body(note_id, str(payload.get("content") or "")[:_MAX_BODY])
        _write_note_index(index)
    body = read_note_body(note_id)
    note = find_note(note_id) or {}
    return {**_summarize_note(note, body), "content": body}


def append_note_body(note_id: str, text: str, heading: str = "") -> dict[str, Any]:
    """Append a block to a note's body (optionally under a new heading)."""
    if not _valid_note_id(note_id):
        raise ValueError("invalid note id")
    body = read_note_body(note_id)
    block = ""
    if heading.strip():
        block += f"\n\n## {heading.strip()}\n"
    elif body.strip():
        block += "\n\n"
    block += text.strip()
    return update_note(note_id, {"content": (body.rstrip() + block).strip() + "\n"})


def replace_note_section(note_id: str, heading: str, new_text: str) -> dict[str, Any]:
    """Replace the body of the ``## <heading>`` section, or append it if absent.

    Matching is case-insensitive on the heading text. The section runs from its
    heading line up to the next heading of the same or shallower level.
    """
    if not _valid_note_id(note_id):
        raise ValueError("invalid note id")
    want = heading.strip().lower()
    lines = read_note_body(note_id).splitlines()
    start = -1
    level = 2
    for i, line in enumerate(lines):
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if not m:
            continue
        if m.group(2).strip().lower() == want:
            start = i
            level = len(m.group(1))
            break

    block = f"{'#' * level} {heading.strip()}\n\n{new_text.strip()}\n"
    if start == -1:
        body = read_note_body(note_id).rstrip()
        merged = (body + "\n\n" + block).strip() + "\n"
        return update_note(note_id, {"content": merged})

    end = len(lines)
    for j in range(start + 1, len(lines)):
        m = re.match(r"^(#{1,6})\s+", lines[j])
        if m and len(m.group(1)) <= level:
            end = j
            break
    merged = "\n".join(lines[:start]) + ("\n" if start else "") + block
    tail = "\n".join(lines[end:])
    if tail.strip():
        merged = merged.rstrip() + "\n\n" + tail
    return update_note(note_id, {"content": merged.strip() + "\n"})


def update_note_refs(note_id: str, refs: list[dict[str, Any]], mode: str = "replace") -> dict[str, Any]:
    """Set or add document references on a note."""
    if not _valid_note_id(note_id):
        raise ValueError("invalid note id")
    clean = _clean_refs(refs)
    if mode == "add":
        existing = (find_note(note_id) or {}).get("refs") or []
        merged = existing + [r for r in clean if r not in existing]
        return update_note(note_id, {"refs": merged})
    return update_note(note_id, {"refs": clean})


def delete_note(note_id: str) -> dict[str, Any]:
    if not _valid_note_id(note_id):
        raise ValueError("invalid note id")
    with _note_lock():
        index = _read_note_index()
        note = next((n for n in index.get("notes", []) if n.get("id") == note_id), None)
        if note is None:
            raise FileNotFoundError(f"note not found: {note_id}")
        index["notes"] = [n for n in index["notes"] if n.get("id") != note_id]
        _write_note_index(index)
        try:
            _note_body_path(note_id).unlink(missing_ok=True)
        except OSError:
            pass
    return {"deleted": True, "id": note_id}


# ── Diagnostics ──────────────────────────────────────────────────────────────


def _sessions_summary() -> dict[str, Any]:
    """Aggregate the study session log (time-on-doc) if it exists."""
    path = LIBRARY_DIR / "sessions.json"
    if not path.exists():
        return {"sessions": 0, "total_seconds": 0}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"sessions": 0, "total_seconds": 0}
    records = data if isinstance(data, list) else data.get("sessions", [])
    if not isinstance(records, list):
        return {"sessions": 0, "total_seconds": 0}
    total = 0.0
    for r in records:
        if isinstance(r, dict):
            try:
                total += float(r.get("seconds") or r.get("duration") or 0)
            except (TypeError, ValueError):
                continue
    return {"sessions": len(records), "total_seconds": int(total)}


def study_overview() -> dict[str, Any]:
    """A single snapshot of study-mode state for the diagnostics tool."""
    docs = list_docs()
    total_pages = sum(d["pages"] for d in docs)
    total_highlights = sum(d["highlights"] for d in docs)
    notes = list_notes()
    srs_dir = LIBRARY_DIR / "srs"
    decks = len(list(srs_dir.glob("*.json"))) if srs_dir.exists() else 0
    return {
        "documents": len(docs),
        "total_pages": total_pages,
        "total_highlights": total_highlights,
        "notes": len(notes),
        "pinned_notes": sum(1 for n in notes if n.get("pinned")),
        "decks": decks,
        "sessions": _sessions_summary(),
        "library_bytes": sum(d["size"] for d in docs),
        "recent_docs": [
            {"id": d["id"], "title": d["title"], "pages": d["pages"],
             "progress": d.get("progress", {}).get("percent", 0)}
            for d in docs[:8]
        ],
    }
