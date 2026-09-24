"""Study-mode handwritten notes — Obsidian-style markdown files.

A note is two things on disk:

  * an entry in ``library/notes/notes.json`` (the index: title, refs, tags,
    pin state, timestamps), and
  * a real ``library/notes/<id>.md`` file holding the raw markdown body.

Keeping the body as a plain ``.md`` file means notes stay hand-editable and
portable — you can open the folder in Obsidian (or vim) and the notes are just
there. The index is the only thing Sable has to parse to draw a list.

A note may reference one or more library documents (``refs``). A ref carries an
optional page / selection anchor, which is how a note taken from a highlighted
passage remembers where it came from.
"""
from __future__ import annotations

import fcntl
import json
import logging
import re
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse

from engine.config import LIBRARY_NOTES_DIR

router = APIRouter()
logger = logging.getLogger("sable.study.notes")

_INDEX_VERSION = 1
_MAX_TITLE = 200
_MAX_BODY = 2_000_000  # 2 MB of markdown is already absurd; cap it anyway
_MAX_REFS = 50
_MAX_TAGS = 30


# ── Paths & helpers ──────────────────────────────────────────────────────────


def _ensure_dirs() -> None:
    LIBRARY_NOTES_DIR.mkdir(parents=True, exist_ok=True)


def _index_path() -> Path:
    return LIBRARY_NOTES_DIR / "notes.json"


def _body_path(note_id: str) -> Path:
    return LIBRARY_NOTES_DIR / f"{note_id}.md"


def _lock_path() -> Path:
    return LIBRARY_NOTES_DIR / ".notes.lock"


def _valid_id(note_id: str) -> bool:
    """Note ids are uuid4 hex — reject traversal attempts before touching disk."""
    return bool(re.fullmatch(r"[0-9a-f]{32}", note_id or ""))


@contextmanager
def _lock() -> Iterator[None]:
    """Exclusive cross-process lock guarding index read-modify-write cycles."""
    _ensure_dirs()
    fh = open(_lock_path(), "w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


def _empty_index() -> dict[str, Any]:
    return {"version": _INDEX_VERSION, "notes": []}


def _read_index() -> dict[str, Any]:
    path = _index_path()
    if not path.exists():
        return _empty_index()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return _empty_index()
    if not isinstance(data, dict) or not isinstance(data.get("notes"), list):
        return _empty_index()
    data.setdefault("version", _INDEX_VERSION)
    return data


def _write_index(data: dict[str, Any]) -> None:
    _ensure_dirs()
    data["version"] = _INDEX_VERSION
    target = _index_path()
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(target)


def _read_body(note_id: str) -> str:
    path = _body_path(note_id)
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


def _write_body(note_id: str, content: str) -> None:
    _ensure_dirs()
    target = _body_path(note_id)
    tmp = target.with_suffix(".md.tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(target)


def _find(index: dict[str, Any], note_id: str) -> dict[str, Any] | None:
    for n in index.get("notes", []):
        if n.get("id") == note_id:
            return n
    return None


def _clean_refs(raw: Any) -> list[dict[str, Any]]:
    """Normalize a note's document references.

    Each ref is ``{doc_id, title, page?, rects?, text?, kind}`` where kind is
    one of ``doc`` (whole document), ``page`` or ``selection``.
    """
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
        rects = item.get("rects")
        if isinstance(rects, list):
            cleaned: list[dict[str, float]] = []
            for r in rects[:200]:
                if not isinstance(r, dict):
                    continue
                try:
                    cleaned.append({
                        "x": float(r.get("x", 0)),
                        "y": float(r.get("y", 0)),
                        "w": float(r.get("w", 0)),
                        "h": float(r.get("h", 0)),
                    })
                except (TypeError, ValueError):
                    continue
            if cleaned:
                ref["rects"] = cleaned
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


def _summarize(note: dict[str, Any], body: str = "") -> dict[str, Any]:
    """List-view shape: index fields + a short plain-text snippet."""
    snippet = body.strip()
    if len(snippet) > 400:
        snippet = snippet[:400]
    return {
        "id": note.get("id"),
        "title": note.get("title") or "Untitled note",
        "refs": note.get("refs") or [],
        "tags": note.get("tags") or [],
        "pinned": bool(note.get("pinned")),
        "created": note.get("created", 0),
        "updated": note.get("updated", 0),
        "snippet": snippet,
    }


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.get("/api/study/notes")
def notes_list(doc_id: str = "", q: str = "", pinned: str = "") -> list[dict[str, Any]]:
    """All notes, newest-updated first.

    Filters: ``doc_id`` (only notes referencing that document), ``q`` (title /
    body substring), ``pinned`` ('1' for pinned only).
    """
    _ensure_dirs()
    index = _read_index()
    needle = q.strip().lower()
    want_pinned = pinned in ("1", "true", "yes")

    out: list[dict[str, Any]] = []
    for note in index.get("notes", []):
        nid = note.get("id")
        if not nid:
            continue
        if doc_id and not any(
            (r.get("doc_id") == doc_id) for r in (note.get("refs") or [])
        ):
            continue
        if want_pinned and not note.get("pinned"):
            continue
        body = _read_body(nid)
        if needle and needle not in (note.get("title") or "").lower() \
                and needle not in body.lower():
            continue
        out.append(_summarize(note, body))

    out.sort(key=lambda n: (not n["pinned"], -float(n.get("updated") or 0)))
    return out


@router.post("/api/study/notes")
def notes_create(payload: dict[str, Any]) -> dict[str, Any]:
    """Create a note. Body: {title?, content?, refs?, tags?}."""
    note_id = uuid.uuid4().hex
    now = time.time()
    title = str(payload.get("title") or "").strip()[:_MAX_TITLE] or "Untitled note"
    content = str(payload.get("content") or "")[:_MAX_BODY]
    refs = _clean_refs(payload.get("refs"))
    tags = _clean_tags(payload.get("tags"))

    note = {
        "id": note_id,
        "title": title,
        "refs": refs,
        "tags": tags,
        "pinned": bool(payload.get("pinned")),
        "created": now,
        "updated": now,
    }

    with _lock():
        index = _read_index()
        index["notes"].append(note)
        _write_index(index)
        _write_body(note_id, content)

    return {**_summarize(note, content), "content": content}


@router.get("/api/study/notes/{note_id}")
def notes_get(note_id: str) -> dict[str, Any]:
    if not _valid_id(note_id):
        raise HTTPException(status_code=400, detail="invalid note id")
    index = _read_index()
    note = _find(index, note_id)
    if not note:
        raise HTTPException(status_code=404, detail="note not found")
    return {**_summarize(note, _read_body(note_id)), "content": _read_body(note_id)}


@router.put("/api/study/notes/{note_id}")
def notes_update(note_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Update title / content / refs / tags / pinned. Any subset."""
    if not _valid_id(note_id):
        raise HTTPException(status_code=400, detail="invalid note id")

    with _lock():
        index = _read_index()
        note = _find(index, note_id)
        if not note:
            raise HTTPException(status_code=404, detail="note not found")

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
            _write_body(note_id, str(payload.get("content") or "")[:_MAX_BODY])
        _write_index(index)

    body = _read_body(note_id)
    return {**_summarize(note, body), "content": body}


@router.delete("/api/study/notes/{note_id}")
def notes_delete(note_id: str) -> dict[str, Any]:
    if not _valid_id(note_id):
        raise HTTPException(status_code=400, detail="invalid note id")
    with _lock():
        index = _read_index()
        note = _find(index, note_id)
        if not note:
            raise HTTPException(status_code=404, detail="note not found")
        index["notes"] = [n for n in index["notes"] if n.get("id") != note_id]
        _write_index(index)
        try:
            _body_path(note_id).unlink(missing_ok=True)
        except Exception:
            pass
    return {"deleted": True, "id": note_id}


@router.get("/api/study/notes/{note_id}/export")
def notes_export(note_id: str) -> PlainTextResponse:
    """Download the raw markdown body as a .md file."""
    if not _valid_id(note_id):
        raise HTTPException(status_code=400, detail="invalid note id")
    index = _read_index()
    note = _find(index, note_id)
    if not note:
        raise HTTPException(status_code=404, detail="note not found")
    body = _read_body(note_id)
    title = re.sub(r"[^\w\- ]+", "", note.get("title") or "note")[:60].strip() or "note"
    return PlainTextResponse(
        body,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{title}.md"'},
    )


@router.post("/api/study/notes/capture")
def notes_capture(payload: dict[str, Any]) -> dict[str, Any]:
    """Quick-capture a note anchored to a document selection / page.

    Body: ``{doc_id, doc_title?, page?, rects?, text?, content?, title?}``.
    Used by the reader's quick-note popup; the anchor becomes the note's
    first reference.
    """
    doc_id = str(payload.get("doc_id") or "").strip()
    if not doc_id:
        raise HTTPException(status_code=400, detail="doc_id is required")

    doc_title = str(payload.get("doc_title") or "").strip()[:300]
    try:
        page = int(payload.get("page") or 0)
    except (TypeError, ValueError):
        page = 0
    text = str(payload.get("text") or "")[:2000]
    rects = payload.get("rects")

    kind = "selection" if (text or rects) else ("page" if page else "doc")
    ref: dict[str, Any] = {
        "doc_id": doc_id,
        "title": doc_title,
        "kind": kind,
        "page": max(0, page),
        "text": text,
    }
    if isinstance(rects, list) and rects:
        ref["rects"] = rects

    title = str(payload.get("title") or "").strip()[:_MAX_TITLE]
    if not title:
        # Derive from the anchor so captured notes aren't all "Untitled note".
        if text:
            title = text.strip().splitlines()[0][:80]
        elif page:
            title = f"{doc_title or 'Document'} — page {page}"
        else:
            title = doc_title or "Untitled note"

    content = str(payload.get("content") or "")
    if not content and text:
        # Quote the anchored passage so the note is self-describing later.
        quoted = "\n".join("> " + ln for ln in text.strip().splitlines())
        content = f"{quoted}\n\n"

    return notes_create({"title": title, "content": content, "refs": [ref]})
