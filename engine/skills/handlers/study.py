"""Study-mode skill handlers: document, notes, PDF access, and diagnostics.

These four tools exist only in study mode (their ``tool.json`` declares
``modes: ["study"]``). They speak to the study domain layer
(``engine.study``) rather than the filesystem, so a document highlight made
by the model lands in the same store the reader draws from and appears on the
page immediately.

Handlers follow the standard signature ``(tag_id, name, attrs, content)`` and
yield ``skill_output`` / ``skill_end`` events. Ambient state (which document is
open) arrives through the study ContextVar — see ``engine.study.context``.
"""
from __future__ import annotations

import base64
import json
import time
from typing import Any, Generator

from engine.skills.handlers.common import _end_event, _output_event
from engine.study import pdf as study_pdf
from engine.study import store
from engine.study.context import get_study_context

_MAX_IMAGE_PAGES = 10
_MAX_TEXT_PAGES = 30


# ── attrs helpers ────────────────────────────────────────────────────────────


def _attr(attrs: dict[str, str], key: str, default: str = "") -> str:
    val = attrs.get(key, default)
    return val if isinstance(val, str) else str(val)


def _attr_json(attrs: dict[str, str], key: str) -> Any:
    """Read an attrs entry that may hold JSON (arrays/objects get stringified)."""
    raw = attrs.get(key)
    if raw is None:
        return None
    if not isinstance(raw, str):
        return raw
    raw = raw.strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return raw


def _attr_int(attrs: dict[str, str], key: str, default: int = 0) -> int:
    try:
        return int(float(_attr(attrs, key) or default))
    except (TypeError, ValueError):
        return default


def _resolve_doc_id(attrs: dict[str, str], content: str = "") -> str:
    """Explicit doc_id wins; otherwise the document open in the reader."""
    explicit = _attr(attrs, "doc_id").strip()
    if explicit:
        return explicit
    ctx = get_study_context()
    if ctx and ctx.doc_id:
        return ctx.doc_id
    # Some models pack everything into a JSON content blob.
    if content.strip().startswith("{"):
        try:
            payload = json.loads(content)
            if isinstance(payload, dict) and payload.get("doc_id"):
                return str(payload["doc_id"])
        except (json.JSONDecodeError, ValueError):
            pass
    return ""


def _attr_bool(attrs: dict[str, str], key: str, default: bool = False) -> bool:
    """Parse a boolean attribute the way a model actually sends it.

    `bool("false")` is True, so the naive `bool(_attr_json(...))` turned the
    string "false" INTO a pin. Models stringify booleans constantly, so this
    accepts the real spellings and falls back to the default for anything it
    does not recognise — including an absent key.
    """
    raw = attrs.get(key)
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    s = str(raw).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off", ""):
        return False
    return default


def _current_page(attrs: dict[str, str], default: int = 1) -> int:
    page = _attr_int(attrs, "page", 0)
    if page > 0:
        return page
    ctx = get_study_context()
    if ctx and ctx.page:
        return ctx.page
    return default


def _parse_pages(spec: str, default: int, cap: int) -> list[int]:
    """Parse '3', '3-12', '1,4,9' into a bounded, ordered page list."""
    spec = (spec or "").strip()
    if not spec:
        return [default]
    pages: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            try:
                lo, hi = int(a), int(b)
            except ValueError:
                continue
            if lo > hi:
                lo, hi = hi, lo
            pages.extend(range(lo, hi + 1))
        else:
            try:
                pages.append(int(part))
            except ValueError:
                continue
    seen: list[int] = []
    for p in pages:
        if p >= 1 and p not in seen:
            seen.append(p)
    return seen[:cap] or [default]


# ── study_document ───────────────────────────────────────────────────────────


def handle_study_document(
    tag_id: str, name: str, attrs: dict[str, str], content: str
) -> Generator[dict[str, Any], None, None]:
    started = time.time()
    action = _attr(attrs, "action", "info").strip().lower()
    doc_id = _resolve_doc_id(attrs, content)

    if not doc_id:
        yield _output_event(tag_id, "No document is open. Open a PDF in the reader or pass doc_id.\n", "stderr")
        yield _end_event(tag_id, name, False, started, error="no document")
        return

    meta = store.read_meta(doc_id)
    if meta is None:
        yield _output_event(tag_id, f"Document not found: {doc_id}\n", "stderr")
        yield _end_event(tag_id, name, False, started, error="document not found")
        return

    ctx = get_study_context()

    try:
        if action == "info":
            progress = meta.get("progress") or {}
            lines = [
                f"Title: {meta.get('title') or 'Untitled'}",
                f"Document id: {doc_id}",
                f"Pages: {meta.get('pages', 0)}",
                f"Highlights: {len(meta.get('highlights') or [])}",
                f"Bookmarked: {'yes' if meta.get('bookmarked') else 'no'}",
                f"Progress: {progress.get('percent', 0)}% (page {progress.get('current_page', 1)})",
            ]
            yield _output_event(tag_id, "\n".join(lines) + "\n")
            yield _end_event(tag_id, name, True, started, {"doc_id": doc_id})
            return

        if action == "list_highlights":
            highlights = store.list_highlights(doc_id)
            if not highlights:
                yield _output_event(tag_id, "No highlights yet.\n")
                yield _end_event(tag_id, name, True, started, {"count": 0})
                return
            lines = [f"{len(highlights)} highlight(s):"]
            for h in highlights:
                snippet = (h.get("text") or "")[:80].replace("\n", " ")
                note = f" — note: {h.get('note')[:60]}" if h.get("note") else ""
                lines.append(f"  [{h.get('id')}] p{h.get('page')} {h.get('color')} \"{snippet}\"{note}")
            yield _output_event(tag_id, "\n".join(lines) + "\n")
            yield _end_event(tag_id, name, True, started, {"count": len(highlights)})
            return

        if action == "get_outline":
            outline = store.get_outline(doc_id)
            if not outline:
                yield _output_event(tag_id, "This document has no outline.\n")
                yield _end_event(tag_id, name, True, started, {"count": 0})
                return
            lines = ["Outline:"]
            for item in outline:
                indent = "  " * max(0, int(item.get("level", 1)) - 1)
                lines.append(f"{indent}- {item.get('title')} (p{item.get('page')})")
            yield _output_event(tag_id, "\n".join(lines) + "\n")
            yield _end_event(tag_id, name, True, started, {"count": len(outline)})
            return

        if action == "bookmark":
            forced = _attr_json(attrs, "bookmarked")
            result = store.set_bookmark(doc_id, forced if isinstance(forced, bool) else None)
            state = "bookmarked" if result["bookmarked"] else "un-bookmarked"
            yield _output_event(tag_id, f"Document {state}.\n")
            if ctx:
                ctx.mark_changed("bookmark", doc_id=doc_id)
            yield _end_event(tag_id, name, True, started, result)
            return

        if action == "set_progress":
            current = _attr_int(attrs, "current_page", 0) or None
            percent = _attr_int(attrs, "percent", -1)
            result = store.set_progress(
                doc_id, current_page=current,
                percent=(percent if percent >= 0 else None),
            )
            yield _output_event(tag_id, f"Progress set to {result.get('percent')}% (page {result.get('current_page')}).\n")
            if ctx:
                ctx.mark_changed("progress", doc_id=doc_id)
            yield _end_event(tag_id, name, True, started, result)
            return

        if action == "delete_highlight":
            hl_id = _attr(attrs, "highlight_id").strip()
            if not hl_id:
                yield _output_event(tag_id, "highlight_id is required.\n", "stderr")
                yield _end_event(tag_id, name, False, started, error="missing highlight_id")
                return
            result = store.delete_highlight(doc_id, hl_id)
            yield _output_event(tag_id, f"Deleted highlight {hl_id}.\n")
            if ctx:
                ctx.mark_changed("highlights", doc_id=doc_id)
            yield _end_event(tag_id, name, True, started, result)
            return

        if action in ("highlight", "highlight_text", "add_note"):
            page = _current_page(attrs, 1)
            text = _attr(attrs, "text").strip()
            note = _attr(attrs, "note").strip()
            rects = _attr_json(attrs, "rects")
            if not isinstance(rects, list):
                rects = []

            # highlight_text (or a bare highlight with only text) → locate it.
            if not rects and text:
                rects = study_pdf.find_text_rects(doc_id, page, text)
                if not rects:
                    yield _output_event(
                        tag_id,
                        f"Could not find \"{text[:60]}\" on page {page}. "
                        "Check the wording, or highlight a phrase that appears verbatim.\n",
                        "stderr",
                    )
                    yield _end_event(tag_id, name, False, started, error="text not found")
                    return

            if not rects:
                yield _output_event(tag_id, "Provide rects or text to locate.\n", "stderr")
                yield _end_event(tag_id, name, False, started, error="no rects or text")
                return

            color = _attr(attrs, "color") or "#ffd400"
            saved = store.add_highlight(doc_id, {
                "page": page,
                "rects": rects,
                "color": color,
                "text": text,
                "note": note,
            })
            what = "note" if action == "add_note" else "highlight"
            yield _output_event(
                tag_id,
                f"Added {what} on page {page} ({len(rects)} region(s))"
                + (f": \"{text[:60]}\"" if text else "")
                + ".\n",
            )
            if ctx:
                ctx.mark_changed("highlights", doc_id=doc_id)
            yield _end_event(tag_id, name, True, started, saved)
            return

        yield _output_event(tag_id, f"Unknown action: {action}\n", "stderr")
        yield _end_event(tag_id, name, False, started, error=f"unknown action {action}")

    except FileNotFoundError as exc:
        yield _output_event(tag_id, f"{exc}\n", "stderr")
        yield _end_event(tag_id, name, False, started, error=str(exc))
    except (ValueError, KeyError) as exc:
        yield _output_event(tag_id, f"{exc}\n", "stderr")
        yield _end_event(tag_id, name, False, started, error=str(exc))
    except Exception as exc:
        yield _output_event(tag_id, f"{type(exc).__name__}: {exc}\n", "stderr")
        yield _end_event(tag_id, name, False, started, error=str(exc))


# ── study_note ───────────────────────────────────────────────────────────────


def handle_study_note(
    tag_id: str, name: str, attrs: dict[str, str], content: str
) -> Generator[dict[str, Any], None, None]:
    started = time.time()
    action = _attr(attrs, "action", "list").strip().lower()
    note_id = _attr(attrs, "note_id").strip()
    body = _attr(attrs, "body")
    if not body:
        body = content
    ctx = get_study_context()

    try:
        if action == "list":
            doc_id = _attr(attrs, "doc_id").strip()
            query = _attr(attrs, "query").strip()
            notes = store.list_notes(doc_id=doc_id, query=query)
            if not notes:
                yield _output_event(tag_id, "No notes found.\n")
                yield _end_event(tag_id, name, True, started, {"count": 0})
                return
            lines = [f"{len(notes)} note(s):"]
            for n in notes:
                pin = "* " if n.get("pinned") else "  "
                lines.append(f"{pin}[{n['id']}] {n['title']} — {n['snippet'][:70].replace(chr(10), ' ')}")
            yield _output_event(tag_id, "\n".join(lines) + "\n")
            yield _end_event(tag_id, name, True, started, {"count": len(notes)})
            return

        if action == "get":
            if not note_id:
                yield _output_event(tag_id, "note_id is required.\n", "stderr")
                yield _end_event(tag_id, name, False, started, error="missing note_id")
                return
            note = store.get_note(note_id)
            if note is None:
                yield _output_event(tag_id, f"Note not found: {note_id}\n", "stderr")
                yield _end_event(tag_id, name, False, started, error="note not found")
                return
            yield _output_event(
                tag_id,
                f"# {note['title']}\n\n{note.get('content') or '(empty)'}\n",
            )
            yield _end_event(tag_id, name, True, started, {"id": note_id})
            return

        if action == "create":
            refs = _attr_json(attrs, "refs")
            result = store.create_note({
                "title": _attr(attrs, "title") or "Untitled note",
                "content": content or _attr(attrs, "content"),
                "refs": refs if isinstance(refs, list) else [],
                "tags": _attr_json(attrs, "tags") or [],
                "pinned": _attr_bool(attrs, "pinned"),
            })
            yield _output_event(tag_id, f"Created note [{result['id']}] {result['title']}.\n")
            if ctx:
                ctx.mark_changed("notes", note_id=result["id"])
            yield _end_event(tag_id, name, True, started, {"id": result["id"]})
            return

        if action == "update":
            if not note_id:
                yield _output_event(tag_id, "note_id is required.\n", "stderr")
                yield _end_event(tag_id, name, False, started, error="missing note_id")
                return
            payload: dict[str, Any] = {}
            if "content" in attrs or content:
                payload["content"] = content or _attr(attrs, "content")
            if _attr(attrs, "title"):
                payload["title"] = _attr(attrs, "title")
            refs = _attr_json(attrs, "refs")
            if isinstance(refs, list):
                payload["refs"] = refs
            result = store.update_note(note_id, payload)
            yield _output_event(tag_id, f"Updated note [{note_id}] {result['title']}.\n")
            if ctx:
                ctx.mark_changed("notes", note_id=note_id)
            yield _end_event(tag_id, name, True, started, {"id": note_id})
            return

        if action == "append":
            if not note_id:
                yield _output_event(tag_id, "note_id is required.\n", "stderr")
                yield _end_event(tag_id, name, False, started, error="missing note_id")
                return
            if not body.strip():
                yield _output_event(tag_id, "body is required to append.\n", "stderr")
                yield _end_event(tag_id, name, False, started, error="empty body")
                return
            result = store.append_note_body(note_id, body, _attr(attrs, "heading"))
            yield _output_event(tag_id, f"Appended to note [{note_id}] {result['title']}.\n")
            if ctx:
                ctx.mark_changed("notes", note_id=note_id)
            yield _end_event(tag_id, name, True, started, {"id": note_id})
            return

        if action == "replace_section":
            if not note_id:
                yield _output_event(tag_id, "note_id is required.\n", "stderr")
                yield _end_event(tag_id, name, False, started, error="missing note_id")
                return
            heading = _attr(attrs, "heading").strip()
            if not heading:
                yield _output_event(tag_id, "heading is required for replace_section.\n", "stderr")
                yield _end_event(tag_id, name, False, started, error="missing heading")
                return
            result = store.replace_note_section(note_id, heading, body)
            yield _output_event(tag_id, f"Replaced section \"{heading}\" in note [{note_id}].\n")
            if ctx:
                ctx.mark_changed("notes", note_id=note_id)
            yield _end_event(tag_id, name, True, started, {"id": note_id})
            return

        if action == "set_title":
            if not note_id:
                yield _output_event(tag_id, "note_id is required.\n", "stderr")
                yield _end_event(tag_id, name, False, started, error="missing note_id")
                return
            result = store.update_note(note_id, {"title": _attr(attrs, "title")})
            yield _output_event(tag_id, f"Renamed note [{note_id}] to {result['title']}.\n")
            if ctx:
                ctx.mark_changed("notes", note_id=note_id)
            yield _end_event(tag_id, name, True, started, {"id": note_id})
            return

        if action == "set_refs":
            if not note_id:
                yield _output_event(tag_id, "note_id is required.\n", "stderr")
                yield _end_event(tag_id, name, False, started, error="missing note_id")
                return
            refs = _attr_json(attrs, "refs")
            mode = _attr(attrs, "refs_mode") or "replace"
            result = store.update_note_refs(
                note_id, refs if isinstance(refs, list) else [], mode,
            )
            yield _output_event(tag_id, f"Updated references on note [{note_id}].\n")
            if ctx:
                ctx.mark_changed("notes", note_id=note_id)
            yield _end_event(tag_id, name, True, started, {"id": note_id})
            return

        if action == "delete":
            if not note_id:
                yield _output_event(tag_id, "note_id is required.\n", "stderr")
                yield _end_event(tag_id, name, False, started, error="missing note_id")
                return
            result = store.delete_note(note_id)
            yield _output_event(tag_id, f"Deleted note [{note_id}].\n")
            if ctx:
                ctx.mark_changed("notes", note_id=note_id)
            yield _end_event(tag_id, name, True, started, result)
            return

        yield _output_event(tag_id, f"Unknown action: {action}\n", "stderr")
        yield _end_event(tag_id, name, False, started, error=f"unknown action {action}")

    except FileNotFoundError as exc:
        yield _output_event(tag_id, f"{exc}\n", "stderr")
        yield _end_event(tag_id, name, False, started, error=str(exc))
    except ValueError as exc:
        yield _output_event(tag_id, f"{exc}\n", "stderr")
        yield _end_event(tag_id, name, False, started, error=str(exc))
    except Exception as exc:
        yield _output_event(tag_id, f"{type(exc).__name__}: {exc}\n", "stderr")
        yield _end_event(tag_id, name, False, started, error=str(exc))


# ── study_pdf ────────────────────────────────────────────────────────────────


def handle_study_pdf(
    tag_id: str, name: str, attrs: dict[str, str], content: str
) -> Generator[dict[str, Any], None, None]:
    started = time.time()
    action = _attr(attrs, "action", "page_text").strip().lower()
    doc_id = _resolve_doc_id(attrs, content)

    if not doc_id:
        yield _output_event(tag_id, "No document is open. Pass doc_id or open a PDF.\n", "stderr")
        yield _end_event(tag_id, name, False, started, error="no document")
        return

    meta = store.read_meta(doc_id)
    if meta is None:
        yield _output_event(tag_id, f"Document not found: {doc_id}\n", "stderr")
        yield _end_event(tag_id, name, False, started, error="document not found")
        return

    total_pages = int(meta.get("pages") or 0)

    try:
        if action == "info":
            outline = store.get_outline(doc_id)
            lines = [f"Document: {meta.get('title')} — {total_pages} pages"]
            if outline:
                lines.append("Outline:")
                for item in outline[:40]:
                    indent = "  " * max(0, int(item.get("level", 1)) - 1)
                    lines.append(f"{indent}- {item.get('title')} (p{item.get('page')})")
            yield _output_event(tag_id, "\n".join(lines) + "\n")
            yield _end_event(tag_id, name, True, started, {"pages": total_pages})
            return

        if action == "search":
            query = _attr(attrs, "query").strip() or _attr(attrs, "text").strip()
            if not query:
                yield _output_event(tag_id, "query is required for search.\n", "stderr")
                yield _end_event(tag_id, name, False, started, error="missing query")
                return
            hits = study_pdf.search_document(doc_id, query, max_hits=30)
            if not hits:
                yield _output_event(tag_id, f"No matches for \"{query}\".\n")
                yield _end_event(tag_id, name, True, started, {"hits": 0})
                return
            lines = [f"\"{query}\" found on {len(hits)} page(s):"]
            for h in hits[:20]:
                lines.append(f"  page {h['page']} ({len(h['rects'])} match(es))")
            yield _output_event(tag_id, "\n".join(lines) + "\n")
            yield _end_event(tag_id, name, True, started, {"hits": len(hits)})
            return

        if action in ("page_text", "page_image"):
            is_image = action == "page_image"
            cap = _attr_int(attrs, "max_pages", 0) or (_MAX_IMAGE_PAGES if is_image else _MAX_TEXT_PAGES)
            cap = min(cap, _MAX_IMAGE_PAGES if is_image else _MAX_TEXT_PAGES)
            spec = _attr(attrs, "pages").strip()
            pages = _parse_pages(spec, _current_page(attrs, 1), cap)
            pages = [p for p in pages if 1 <= p <= max(1, total_pages)] or [1]

            if is_image:
                rendered = 0
                for p in pages:
                    png = study_pdf.render_page_png(doc_id, p)
                    if not png:
                        yield _output_event(tag_id, f"Page {p}: could not render.\n", "stderr")
                        continue
                    b64 = base64.b64encode(png).decode("ascii")
                    # Frontend renders these; the study route also injects them
                    # as inline parts on the next loop iteration when it owns
                    # the loop (scraper path).
                    yield {
                        "type": "study_attachment",
                        "id": tag_id,
                        "kind": "image",
                        "name": f"page-{p}.png",
                        "page": p,
                        "data": "data:image/png;base64," + b64,
                    }
                    rendered += 1
                yield _output_event(
                    tag_id,
                    f"Rendered {rendered} page image(s) from {meta.get('title')} "
                    f"(pages {', '.join(str(p) for p in pages)}). "
                    "They are attached to the conversation.\n",
                )
                yield _end_event(tag_id, name, True, started, {"pages": pages, "images": rendered})
                return

            page_text = store.page_text_for(doc_id)
            blocks = []
            for p in pages:
                txt = page_text[p - 1] if 1 <= p <= len(page_text) else ""
                txt = (txt or "").strip()
                blocks.append(f"[Page {p}]\n{txt or '(no extractable text on this page)'}")
            yield _output_event(tag_id, "\n\n".join(blocks) + "\n")
            yield _end_event(tag_id, name, True, started, {"pages": pages})
            return

        yield _output_event(tag_id, f"Unknown action: {action}\n", "stderr")
        yield _end_event(tag_id, name, False, started, error=f"unknown action {action}")

    except Exception as exc:
        yield _output_event(tag_id, f"{type(exc).__name__}: {exc}\n", "stderr")
        yield _end_event(tag_id, name, False, started, error=str(exc))


# ── study_diagnostics ────────────────────────────────────────────────────────


def handle_study_diagnostics(
    tag_id: str, name: str, attrs: dict[str, str], content: str
) -> Generator[dict[str, Any], None, None]:
    started = time.time()
    action = _attr(attrs, "action", "overview").strip().lower()

    try:
        if action == "overview":
            data = store.study_overview()
            sessions = data.get("sessions", {})
            minutes = int(sessions.get("total_seconds", 0)) // 60
            lines = [
                "Study mode overview:",
                f"  Documents: {data['documents']} ({data['total_pages']} pages total)",
                f"  Highlights: {data['total_highlights']}",
                f"  Notes: {data['notes']} ({data['pinned_notes']} pinned)",
                f"  SRS decks: {data['decks']}",
                f"  Study time: {minutes} min across {sessions.get('sessions', 0)} session(s)",
            ]
            if data.get("recent_docs"):
                lines.append("  Recent documents:")
                for d in data["recent_docs"]:
                    lines.append(f"    - {d['title']} ({d['pages']}p, {d['progress']}% read)")
            yield _output_event(tag_id, "\n".join(lines) + "\n")
            yield _end_event(tag_id, name, True, started, data)
            return

        if action == "library":
            docs = store.list_docs()
            if not docs:
                yield _output_event(tag_id, "The library is empty.\n")
                yield _end_event(tag_id, name, True, started, {"count": 0})
                return
            lines = [f"{len(docs)} document(s) in the library:"]
            for d in docs:
                pct = (d.get("progress") or {}).get("percent", 0)
                lines.append(
                    f"  [{d['id']}] {d['title']} — {d['pages']}p, {d['highlights']} highlight(s), {pct}% read"
                )
            yield _output_event(tag_id, "\n".join(lines) + "\n")
            yield _end_event(tag_id, name, True, started, {"count": len(docs)})
            return

        if action == "notes":
            notes = store.list_notes()
            if not notes:
                yield _output_event(tag_id, "No notes yet.\n")
                yield _end_event(tag_id, name, True, started, {"count": 0})
                return
            lines = [f"{len(notes)} note(s):"]
            for n in notes:
                pin = "* " if n.get("pinned") else "  "
                lines.append(f"{pin}[{n['id']}] {n['title']}")
            yield _output_event(tag_id, "\n".join(lines) + "\n")
            yield _end_event(tag_id, name, True, started, {"count": len(notes)})
            return

        if action == "sessions":
            data = store.study_overview().get("sessions", {})
            minutes = int(data.get("total_seconds", 0)) // 60
            yield _output_event(
                tag_id,
                f"Total study time: {minutes} min across {data.get('sessions', 0)} session(s).\n",
            )
            yield _end_event(tag_id, name, True, started, data)
            return

        if action == "document":
            doc_id = _resolve_doc_id(attrs, content)
            if not doc_id:
                yield _output_event(tag_id, "No document specified.\n", "stderr")
                yield _end_event(tag_id, name, False, started, error="no document")
                return
            meta = store.read_meta(doc_id)
            if meta is None:
                yield _output_event(tag_id, f"Document not found: {doc_id}\n", "stderr")
                yield _end_event(tag_id, name, False, started, error="not found")
                return
            progress = meta.get("progress") or {}
            lines = [
                f"Document: {meta.get('title')}",
                f"  Pages: {meta.get('pages', 0)}",
                f"  Highlights: {len(meta.get('highlights') or [])}",
                f"  Progress: {progress.get('percent', 0)}% (page {progress.get('current_page', 1)})",
                f"  Bookmarked: {'yes' if meta.get('bookmarked') else 'no'}",
            ]
            yield _output_event(tag_id, "\n".join(lines) + "\n")
            yield _end_event(tag_id, name, True, started, {"id": doc_id})
            return

        yield _output_event(tag_id, f"Unknown action: {action}\n", "stderr")
        yield _end_event(tag_id, name, False, started, error=f"unknown action {action}")

    except Exception as exc:
        yield _output_event(tag_id, f"{type(exc).__name__}: {exc}\n", "stderr")
        yield _end_event(tag_id, name, False, started, error=str(exc))
