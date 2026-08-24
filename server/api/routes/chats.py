from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from engine.scraper import get_settings as get_scraper_settings
from server.database import (
    ensure_chat, list_chats, get_messages, add_message, delete_chat, delete_all_chats,
    search_messages,
    get_skill_events_for_message, list_projects, create_project, update_project,
    delete_project, get_project,
)
from server.utils import retry_async, make_title, _resolve_api_backend
from server.models import NewChatRequest, ContextPassRequest, ProjectCreate, ProjectUpdate
from connectors import get_connector
from ..dependencies import service

logger = logging.getLogger("sable.context_pass")

router = APIRouter()

@router.get("/api/chats/search")
def search_chats(q: str = "") -> dict[str, Any]:
    if not q.strip():
        return {"results": []}
    return {"results": search_messages(q.strip())}


import re as _re

# ── Search Scoring ──────────────────────────────────────────────────────────
# Simple, reliable scoring: exact substring match + token overlap ratio.
# No external libraries, no IDF/BM25 that breaks on small pre-filtered corpora.

_STOP_WORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "need", "dare", "ought",
    "used", "to", "of", "in", "for", "on", "with", "at", "by", "from",
    "as", "into", "through", "during", "before", "after", "above", "below",
    "between", "out", "off", "over", "under", "again", "further", "then",
    "once", "here", "there", "when", "where", "why", "how", "all", "each",
    "every", "both", "few", "more", "most", "other", "some", "such", "no",
    "nor", "not", "only", "own", "same", "so", "than", "too", "very",
    "just", "because", "but", "and", "or", "if", "while", "about", "up",
    "it", "its", "i", "me", "my", "we", "our", "you", "your", "he", "him",
    "his", "she", "her", "they", "them", "their", "what", "which", "who",
})

_WORD_RE = _re.compile(r'[a-z0-9]+')


def _tokenize(text: str) -> list[str]:
    """Lowercase tokenize, strip stop words and short tokens."""
    return [t for t in _WORD_RE.findall(text.lower()) if t not in _STOP_WORDS and len(t) > 1]


def _score_doc(query: str, text: str) -> float:
    """Score a document against a query. Returns 0.0–1.0.

    Scoring logic:
    - Exact substring match → 1.0
    - Otherwise: (matched query tokens / total query tokens)
      with partial credit for prefix/stem matches
    """
    if not query or not text:
        return 0.0

    ql = query.lower().strip()
    tl = text.lower()

    # Exact substring → perfect match
    if ql in tl:
        return 1.0

    q_tokens = _tokenize(query)
    if not q_tokens:
        return 0.0

    doc_tokens = set(_tokenize(text))
    if not doc_tokens:
        return 0.0

    matched = 0
    for qt in q_tokens:
        if qt in doc_tokens:
            matched += 1
        else:
            # Partial credit: check if any doc token starts with query token
            # Catches "config" matching "configuration", "hypr" matching "hyprland"
            for dt in doc_tokens:
                if dt.startswith(qt) or qt.startswith(dt):
                    matched += 0.5
                    break

    return round(matched / len(q_tokens), 4)


def _rank_results(query: str, documents: list[dict], text_key: str = "text",
                  top_k: int = 10, min_score: float = 0.3) -> list[tuple[float, dict]]:
    """Rank documents by relevance to query. Returns (score, doc) tuples."""
    if not query or not documents:
        return []

    scored: list[tuple[float, dict]] = []
    for doc in documents:
        s = _score_doc(query, doc.get(text_key, ""))
        if s >= min_score:
            scored.append((s, doc))

    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:top_k]


_PER_SECTION = 10
_MIN_SCORE = 0.3


@router.get("/api/search/unified")
async def unified_search(q: str = "") -> dict[str, Any]:
    """Unified fuzzy search across all Sable data sources.
    Returns up to 10 results per section with score >= 0.75.
    """
    query = q.strip()
    sources = ("messages", "skills", "memory", "notes", "todos", "schedules", "research", "agents")
    if not query:
        return {k: [] for k in sources}

    from server.database import get_db

    results: dict[str, list] = {k: [] for k in sources}

    # Build per-term LIKE clauses so words don't need to be adjacent.
    # e.g. "dark canvas" → "%dark%" AND "%canvas%" matches both
    # "dark canvas matching" → all three terms must appear somewhere in the text.
    _q_terms = [t for t in _re.split(r'\s+', query.strip()) if len(t) > 1]
    if not _q_terms:
        return {k: [] for k in sources}
    # For DB pre-filter: require ALL terms present (AND), each anywhere in text
    _like_clauses = " AND ".join(["content LIKE ? COLLATE NOCASE"] * len(_q_terms))
    _like_params = tuple(f"%{t}%" for t in _q_terms)
    # For title/description fields that use different column names
    def _make_like(columns: list[str]) -> tuple[str, tuple]:
        """Build OR'd multi-term LIKE for multiple columns."""
        parts = []
        params: list[str] = []
        for col in columns:
            col_parts = " AND ".join([f"{col} LIKE ? COLLATE NOCASE"] * len(_q_terms))
            parts.append(f"({col_parts})")
            params.extend(f"%{t}%" for t in _q_terms)
        return " OR ".join(parts), tuple(params)

    # ── 1. Chat messages (broad fetch → fuzzy filter) ──
    _mem_re = _re.compile(r'^\[RELEVANT MEMORY CONTEXT\][\s\S]*?\n\n')
    _ts_re = _re.compile(r'^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]\n?')
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT m.id, m.chat_id, m.role, m.content, m.created_at, c.title "
                "FROM messages m JOIN chats c ON m.chat_id = c.id "
                f"WHERE {_like_clauses} ORDER BY m.id DESC LIMIT 200",
                _like_params,
            ).fetchall()
            docs = []
            for row in rows:
                d = dict(row)
                content = d.get("content") or ""
                content = _mem_re.sub("", content)
                content = _ts_re.sub("", content)
                searchable = (d.get("title") or "") + " " + content
                docs.append({
                    **d,
                    "text": searchable,
                    "preview": content[:300],
                    "source": "message",
                })
            ranked = _rank_results(query, docs, text_key="text", top_k=_PER_SECTION, min_score=_MIN_SCORE)
            for score, doc in ranked:
                doc["score"] = score
            results["messages"] = [doc for _, doc in ranked]
    except Exception as e:
        logger.warning("Search messages failed: %s", e)

    # ── 2. Skill events ──
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT se.event_data, se.message_id, m.chat_id, c.title, m.created_at "
                "FROM skill_events se "
                "JOIN messages m ON se.message_id = m.id "
                "JOIN chats c ON m.chat_id = c.id "
                f"WHERE {' AND '.join(['se.event_data LIKE ? COLLATE NOCASE'] * len(_q_terms))} ORDER BY se.id DESC LIMIT 200",
                _like_params,
            ).fetchall()
            seen: set[int] = set()
            docs = []
            for row in rows:
                d = dict(row)
                mid = d.get("message_id")
                if mid in seen:
                    continue
                seen.add(mid)
                try:
                    ev = json.loads(d["event_data"])
                except (json.JSONDecodeError, TypeError):
                    continue
                skill_name = ev.get("skill") or ev.get("tool") or ev.get("type") or "unknown"
                raw = ev.get("summary") or ev.get("result") or ev.get("output") or ""
                if isinstance(raw, dict):
                    raw = json.dumps(raw, ensure_ascii=False)
                raw_str = str(raw)
                searchable = skill_name + " " + raw_str + " " + (d.get("title") or "")
                docs.append({
                    "text": searchable,
                    "skill": skill_name,
                    "preview": raw_str[:300],
                    "chat_id": d.get("chat_id"),
                    "title": d.get("title"),
                    "created_at": d.get("created_at"),
                    "message_id": mid,
                    "source": "skill",
                })
            ranked = _rank_results(query, docs, text_key="text", top_k=_PER_SECTION, min_score=_MIN_SCORE)
            for score, doc in ranked:
                doc["score"] = score
            results["skills"] = [doc for _, doc in ranked]
    except Exception as e:
        logger.warning("Search skills failed: %s", e)

    # ── 3. Memory (vector search + fuzzy fallback) ──
    try:
        from server.api.routes.memory import get_searcher
        searcher = get_searcher()
        seen_keys: set[str] = set()

        # Primary: vector/semantic search
        for mr in searcher.search(query, top_k=_PER_SECTION, threshold=_MIN_SCORE):
            text = mr.get("value") or mr.get("text") or mr.get("content") or ""
            key = mr.get("key", "")
            seen_keys.add(key)
            results["memory"].append({
                "key": key,
                "preview": text[:300],
                "full_content": text,
                "score": round(mr.get("score", 0), 3),
                "category": mr.get("category", ""),
                "source": "memory",
            })

        # Fallback: if vector search missed results (e.g. typos), brute-force fuzzy scan
        if len(results["memory"]) < _PER_SECTION:
            searcher._ensure_loaded()
            scored_mem: list[tuple[float, dict]] = []
            for entry_str in searcher._entries:
                if not isinstance(entry_str, str) or ": " not in entry_str:
                    continue
                mkey, _, mval = entry_str.partition(": ")
                if mkey in seen_keys:
                    continue
                score = max(_score_doc(query, mkey), _score_doc(query, mval))
                if score >= _MIN_SCORE:
                    scored_mem.append((score, {
                        "key": mkey,
                        "preview": mval[:300],
                        "full_content": mval,
                        "score": round(score, 3),
                        "category": "fuzzy",
                        "source": "memory",
                    }))
            scored_mem.sort(key=lambda x: x[0], reverse=True)
            for _, item in scored_mem:
                if len(results["memory"]) >= _PER_SECTION:
                    break
                results["memory"].append(item)
    except Exception as e:
        logger.debug("Search memory skipped: %s", e)

    # ── 4. Notes (note_type != 'todo') ──
    try:
        with get_db() as conn:
            _nl, _np = _make_like(["title", "content"])
            rows = conn.execute(
                "SELECT id, title, content, note_type, created_at, updated_at FROM notes "
                f"WHERE note_type != 'todo' AND archived = 0 AND ({_nl}) "
                "ORDER BY updated_at DESC LIMIT 100",
                _np,
            ).fetchall()
            docs = []
            for row in rows:
                d = dict(row)
                searchable = (d.get("title") or "") + " " + (d.get("content") or "")
                docs.append({
                    **d,
                    "text": searchable,
                    "preview": (d.get("content") or "")[:300],
                    "source": "note",
                })
            ranked = _rank_results(query, docs, text_key="text", top_k=_PER_SECTION, min_score=_MIN_SCORE)
            for score, doc in ranked:
                doc["score"] = score
            results["notes"] = [doc for _, doc in ranked]
    except Exception as e:
        logger.warning("Search notes failed: %s", e)

    # ── 5. Todos (note_type == 'todo') ──
    try:
        with get_db() as conn:
            _tl, _tp = _make_like(["title", "content"])
            rows = conn.execute(
                "SELECT id, title, content, items, due_date, created_at, updated_at FROM notes "
                f"WHERE note_type = 'todo' AND archived = 0 AND ({_tl}) "
                "ORDER BY updated_at DESC LIMIT 100",
                _tp,
            ).fetchall()
            docs = []
            for row in rows:
                d = dict(row)
                searchable = (d.get("title") or "") + " " + (d.get("content") or "")
                docs.append({
                    **d,
                    "text": searchable,
                    "preview": (d.get("content") or "")[:300],
                    "source": "todo",
                })
            ranked = _rank_results(query, docs, text_key="text", top_k=_PER_SECTION, min_score=_MIN_SCORE)
            for score, doc in ranked:
                doc["score"] = score
            results["todos"] = [doc for _, doc in ranked]
    except Exception as e:
        logger.warning("Search todos failed: %s", e)

    # ── 6. Schedules ──
    try:
        with get_db() as conn:
            _sl, _sp = _make_like(["title", "description"])
            rows = conn.execute(
                f"SELECT * FROM schedules WHERE ({_sl}) "
                "ORDER BY start_date DESC LIMIT 100",
                _sp,
            ).fetchall()
            docs = []
            for row in rows:
                d = dict(row)
                searchable = (d.get("title") or "") + " " + (d.get("description") or "")
                docs.append({
                    **d,
                    "text": searchable,
                    "preview": (d.get("description") or d.get("title") or "")[:300],
                    "source": "schedule",
                })
            ranked = _rank_results(query, docs, text_key="text", top_k=_PER_SECTION, min_score=_MIN_SCORE)
            for score, doc in ranked:
                doc["score"] = score
            results["schedules"] = [doc for _, doc in ranked]
    except Exception as e:
        logger.warning("Search schedules failed: %s", e)

    # ── 7. Research (sable_output markdown files) ──
    try:
        from engine.config import RESEARCH_DIR
        if RESEARCH_DIR.exists():
            docs = []
            for f in sorted(RESEARCH_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
                try:
                    text = f.read_text(encoding="utf-8")
                except Exception:
                    continue
                title = f.stem.replace("_", " ").replace("-", " ")
                body = _re.sub(r'^---\s*\n.*?\n---\s*\n', '', text, count=1, flags=_re.DOTALL)
                first_para = ""
                for line in body.splitlines():
                    s = line.strip()
                    if s and not s.startswith(("#", ">", "---")):
                        first_para = s[:300]
                        break
                searchable = title + " " + text[:5000]
                docs.append({
                    "text": searchable,
                    "id": f.stem,
                    "filename": f.name,
                    "title": title.title(),
                    "preview": first_para,
                    "source": "research",
                })
            ranked = _rank_results(query, docs, text_key="text", top_k=_PER_SECTION, min_score=_MIN_SCORE)
            for score, doc in ranked:
                doc["score"] = score
            results["research"] = [doc for _, doc in ranked]
    except Exception as e:
        logger.debug("Search research skipped: %s", e)

    # ── 8. Agents (sable_output markdown files) ──
    try:
        from engine.config import AGENT_OUTPUT_DIR
        if AGENT_OUTPUT_DIR.exists():
            docs = []
            for f in sorted(AGENT_OUTPUT_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
                if f.stem.endswith("_conversation"):
                    continue
                try:
                    text = f.read_text(encoding="utf-8")
                except Exception:
                    continue
                title = f.stem.replace("_", " ").replace("-", " ")
                body = _re.sub(r'^---\s*\n.*?\n---\s*\n', '', text, count=1, flags=_re.DOTALL)
                first_para = ""
                for line in body.splitlines():
                    s = line.strip()
                    if s and not s.startswith(("#", ">", "---")):
                        first_para = s[:300]
                        break
                searchable = title + " " + text[:5000]
                docs.append({
                    "text": searchable,
                    "id": f.stem,
                    "filename": f.name,
                    "title": title.title(),
                    "preview": first_para,
                    "source": "agent",
                })
            ranked = _rank_results(query, docs, text_key="text", top_k=_PER_SECTION, min_score=_MIN_SCORE)
            for score, doc in ranked:
                doc["score"] = score
            results["agents"] = [doc for _, doc in ranked]
    except Exception as e:
        logger.debug("Search agents skipped: %s", e)

    return results


@router.get("/api/search/full-content")
async def search_full_content(source: str = "", id: str = "", chat_id: str = "", message_id: str = "") -> dict[str, Any]:
    """Return full content for a search result card expansion."""
    if source == "message":
        from server.database import get_db
        try:
            with get_db() as conn:
                row = conn.execute(
                    "SELECT m.content, m.role, m.created_at, c.title "
                    "FROM messages m JOIN chats c ON m.chat_id = c.id "
                    "WHERE m.id = ?", (int(id),),
                ).fetchone()
            if row:
                d = dict(row)
                content = d.get("content") or ""
                content = _re.sub(r'^\[RELEVANT MEMORY CONTEXT\][\s\S]*?\n\n', '', content)
                content = _re.sub(r'^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]\n?', '', content)
                return {"content": content, "role": d["role"], "title": d["title"], "created_at": d["created_at"]}
        except Exception as e:
            return {"error": str(e)}
        return {"error": "Message not found"}

    elif source == "skill":
        from server.database import get_db
        try:
            with get_db() as conn:
                rows = conn.execute(
                    "SELECT event_data FROM skill_events WHERE message_id = ? ORDER BY seq ASC",
                    (int(message_id),),
                ).fetchall()
            events = []
            for row in rows:
                try:
                    events.append(json.loads(row["event_data"]))
                except (json.JSONDecodeError, TypeError):
                    continue
            return {"events": events}
        except Exception as e:
            return {"error": str(e)}

    elif source == "note" or source == "todo":
        from server.database.notes import get_note
        note = get_note(id)
        if note:
            return {"content": note.get("content", ""), "title": note.get("title", ""), "items": note.get("items", [])}
        return {"error": "Note not found"}

    elif source == "schedule":
        from server.database import get_db
        try:
            with get_db() as conn:
                row = conn.execute("SELECT * FROM schedules WHERE id = ?", (id,)).fetchone()
            if row:
                return dict(row)
        except Exception as e:
            return {"error": str(e)}
        return {"error": "Schedule not found"}

    elif source in ("research", "agent"):
        from engine.config import RESEARCH_DIR, AGENT_OUTPUT_DIR
        dir_map = {"research": RESEARCH_DIR, "agent": AGENT_OUTPUT_DIR}
        target_dir = dir_map.get(source)
        if target_dir:
            fpath = target_dir / f"{id}.md"
            if fpath.exists():
                try:
                    return {"content": fpath.read_text(encoding="utf-8")}
                except Exception as e:
                    return {"error": str(e)}
        return {"error": "File not found"}

    elif source == "memory":
        # Memory results are self-contained; no expansion needed
        return {"error": "Memory results don't expand"}

    return {"error": f"Unknown source: {source}"}

@router.get("/api/chats")
def chats(project_id: str | None = None) -> dict[str, list[dict[str, Any]]]:
    return {"chats": list_chats(project_id=project_id)}

# --- Projects CRUD ---

@router.get("/api/projects")
def projects_list() -> dict[str, list[dict[str, Any]]]:
    return {"projects": list_projects()}

@router.post("/api/projects")
def projects_create(body: ProjectCreate) -> dict[str, Any]:
    project_id = uuid.uuid4().hex
    proj = create_project(project_id, body.name, body.path)
    return {"id": proj["id"], "project": proj}

@router.put("/api/projects/{project_id}")
def projects_update(project_id: str, body: ProjectUpdate) -> dict[str, Any]:
    existing = get_project(project_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Project not found")
    proj = update_project(project_id, **body.model_dump(exclude_none=True))
    return {"project": proj}

@router.delete("/api/projects/{project_id}")
def projects_delete(project_id: str) -> dict[str, Any]:
    deleted = delete_project(project_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Project not found")
    return {"deleted": True, "project_id": project_id}

@router.post("/api/projects/{project_id}/instruction")
async def projects_upload_instruction(project_id: str, body: dict[str, str]) -> dict[str, Any]:
    """Save instruction text to system/projects/<id>/instruction.md and update DB."""
    existing = get_project(project_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Project not found")
    text = body.get("text", "")
    if not text.strip():
        raise HTTPException(status_code=400, detail="Empty instruction text")
    # Ensure project folder exists
    proj_dir = Path(__file__).resolve().parent.parent.parent / "system" / "projects" / project_id
    proj_dir.mkdir(parents=True, exist_ok=True)
    instr_path = proj_dir / "instruction.md"
    instr_path.write_text(text, encoding="utf-8")
    # Update DB — store relative path and the text itself
    update_project(project_id, instruction_file=str(instr_path), instruction_text=text)
    return {"saved": True, "path": str(instr_path), "chars": len(text)}

# Track CWD before project activation so deactivate can restore it
_pre_project_cwd: str | None = None

@router.post("/api/projects/{project_id}/activate")
async def projects_activate(project_id: str) -> dict[str, Any]:
    """Activate a project: switch CWD and sync context with project instruction."""
    global _pre_project_cwd
    proj = get_project(project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found")
    import os
    # Remember CWD before switching so deactivate can restore it
    _pre_project_cwd = os.getcwd()
    if proj.get("path"):
        p = Path(proj["path"])
        if p.is_dir():
            os.chdir(str(p))
    # Sync context for Qwen (rebuilds instruction with project override)
    try:
        await service.sync_context(project_id=project_id)
    except Exception as exc:
        logger.warning("sync_context on activate failed: %s", exc)
    return {"activated": True, "project_id": project_id, "old_cwd": _pre_project_cwd, "new_cwd": os.getcwd()}

@router.post("/api/projects/deactivate")
async def projects_deactivate(body: dict[str, str] | None = None) -> dict[str, Any]:
    """Deactivate current project: revert CWD and sync context back to default."""
    global _pre_project_cwd
    import os
    old_cwd = os.getcwd()
    # Restore to the CWD that was active before project activation
    restore_to = _pre_project_cwd
    _pre_project_cwd = None
    if restore_to and Path(restore_to).is_dir():
        os.chdir(restore_to)
    else:
        # Fallback: Sable root
        sable_root = Path(__file__).resolve().parent.parent.parent
        os.chdir(str(sable_root))
    # Sync context back to default (no project override)
    try:
        await service.sync_context(project_id=None)
    except Exception as exc:
        logger.warning("sync_context on deactivate failed: %s", exc)
    return {"deactivated": True, "old_cwd": old_cwd, "new_cwd": os.getcwd()}

@router.post("/api/chat/new")
async def new_chat(request: NewChatRequest = NewChatRequest()) -> dict[str, str | None]:
    if get_scraper_settings().get("enabled"):
        chat_id = f"browser-{uuid.uuid4().hex}"
        ensure_chat(chat_id, "New chat", None, project_id=request.project_id)
        return {"chat_id": chat_id}
    # Always use a local Sable UUID as the chat_id.
    # Upstream Qwen session is created lazily on first message send
    # (session recovery in /api/chat handles it). This avoids blocking
    # chat creation on slow/unreachable upstream servers.
    local_chat_id = uuid.uuid4().hex
    ensure_chat(local_chat_id, "New chat", None, project_id=request.project_id)
    return {"chat_id": local_chat_id}

@router.get("/api/chats/{chat_id}/messages")
def chat_messages(
    chat_id: str,
    limit: int | None = None,
    before_id: int | None = None,
    include_skill_events: bool = False,
) -> dict[str, Any]:
    """Load messages with optional pagination.

    - limit: max messages to return (default: all)
    - before_id: load messages older than this id (for infinite scroll)
    - include_skill_events: if true, embed skill_events in response (heavy)
    """
    messages = get_messages(chat_id, limit=limit, before_id=before_id, include_skill_events=include_skill_events)
    # Compute total context chars for the full chat (not just the paginated slice)
    from server.database import get_db
    with get_db() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(LENGTH(content)) + SUM(LENGTH(COALESCE(thinking, ''))), 0) AS total "
            "FROM messages WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
        msg_chars = row["total"] if row else 0
        tool_row = conn.execute(
            "SELECT COALESCE(SUM(LENGTH(se.event_data)), 0) AS tool_total "
            "FROM skill_events se JOIN messages m ON se.message_id = m.id "
            "WHERE m.chat_id = ?",
            (chat_id,),
        ).fetchone()
        context_chars = msg_chars + (tool_row["tool_total"] if tool_row else 0)
    return {"chat_id": chat_id, "messages": messages, "context_chars": context_chars}


@router.get("/api/chats/{chat_id}/context-breakdown")
def context_breakdown(chat_id: str) -> dict[str, Any]:
    """Return context usage breakdown by role/type for the context circle popup."""
    from server.database import get_db
    with get_db() as conn:
        # Per-role content chars
        rows = conn.execute(
            "SELECT role, "
            "COALESCE(SUM(LENGTH(content)), 0) AS content_chars, "
            "COALESCE(SUM(LENGTH(COALESCE(thinking, ''))), 0) AS thinking_chars "
            "FROM messages WHERE chat_id = ? GROUP BY role",
            (chat_id,),
        ).fetchall()
        # Tool output chars from the skill_events table (joined via message_id)
        tool_rows = conn.execute(
            "SELECT se.event_data FROM skill_events se "
            "JOIN messages m ON se.message_id = m.id "
            "WHERE m.chat_id = ?",
            (chat_id,),
        ).fetchall()

    user_chars = 0
    assistant_chars = 0
    thinking_chars = 0
    for r in rows:
        if r["role"] == "user":
            user_chars = r["content_chars"]
        elif r["role"] == "assistant":
            assistant_chars = r["content_chars"]
            thinking_chars = r["thinking_chars"]

    # Sum tool chars directly from event_data
    tool_chars = sum(len(tr["event_data"] or "") for tr in tool_rows)

    total = user_chars + assistant_chars + thinking_chars + tool_chars
    return {
        "total": total,
        "user": user_chars,
        "assistant": assistant_chars,
        "thinking": thinking_chars,
        "tool": tool_chars,
    }


@router.get("/api/chats/{chat_id}/messages/{message_id}/events")
def message_skill_events(chat_id: str, message_id: int) -> dict[str, Any]:
    """Lazy-load skill events for a specific message."""
    events = get_skill_events_for_message(message_id)
    return {"message_id": message_id, "skill_events": events}

@router.delete("/api/chats")
def delete_all_chats_route() -> dict[str, Any]:
    """Bulk delete all chats (called from Settings → Delete All Chats)."""
    count = delete_all_chats()
    return {"deleted": True, "chats_removed": count}


@router.delete("/api/chats/{chat_id}")
def delete_chat_route(chat_id: str) -> dict[str, Any]:
    deleted = delete_chat(chat_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Chat not found")
    return {"deleted": True, "chat_id": chat_id}

_CONTEXT_PASS_SETTINGS_PATH = Path(__file__).resolve().parents[3] / "system" / "context_pass_settings.json"

def _load_ctx_pass_settings() -> dict[str, str]:
    defaults = {"summarizer_model": "", "browser_data_acc": ""}
    if _CONTEXT_PASS_SETTINGS_PATH.exists():
        try:
            stored = json.loads(_CONTEXT_PASS_SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                defaults.update(stored)
        except Exception:
            pass
    return defaults

@router.post("/api/context/pass")
async def context_pass(req: ContextPassRequest) -> dict[str, Any]:
    import re as _re
    _tc_re = _re.compile(r'<tool_call[\s>]', _re.IGNORECASE)
    _tr_re = _re.compile(r'<tool_result[\s>]', _re.IGNORECASE)

    messages = get_messages(req.chat_id)
    if not messages:
        return {"error": "No messages in this chat"}

    if len(messages) < 2:
        return {"error": "Not enough context to summarize"}

    # --- Extract head/tail programmatically ---
    # Find first user message
    first_user_idx = None
    for i, m in enumerate(messages):
        if m.get("role") == "user":
            first_user_idx = i
            break

    # Find last user message
    last_user_idx = None
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            last_user_idx = i
            break

    if first_user_idx is None or last_user_idx is None:
        return {"error": "No user messages found"}

    # Collect first 3 tool_call/result pairs after first user message
    head_end_idx = first_user_idx + 1
    tc_count = 0
    for i in range(first_user_idx + 1, len(messages)):
        content = (messages[i].get("content") or "")
        role = messages[i].get("role", "")
        if _tc_re.search(content) or _tr_re.search(content) or role == "tool":
            head_end_idx = i + 1
            tc_count += 1
            if tc_count >= 6:  # 3 pairs
                break
        elif role == "assistant" and not _tc_re.search(content):
            head_end_idx = i + 1

    # Collect last 5 tool_call/result pairs
    tail_start_idx = last_user_idx
    tc_count = 0
    for i in range(len(messages) - 1, last_user_idx - 1, -1):
        content = (messages[i].get("content") or "")
        role = messages[i].get("role", "")
        if _tc_re.search(content) or _tr_re.search(content) or role == "tool":
            tail_start_idx = i
            tc_count += 1
            if tc_count >= 10:  # 5 pairs
                break

    if tail_start_idx < head_end_idx:
        tail_start_idx = head_end_idx

    # --- Expand tail to meet minimum 100k char threshold ---
    _TAIL_MIN_CHARS = 100_000
    _TAIL_PRESERVE_LIMIT = 250_000
    tail_char_count = sum(
        len((messages[i].get("content") or "").strip())
        for i in range(tail_start_idx, len(messages))
    )
    while tail_char_count < _TAIL_MIN_CHARS and tail_start_idx > head_end_idx:
        tail_start_idx -= 1
        tail_char_count += len((messages[tail_start_idx].get("content") or "").strip())

    # Cap at preserve limit
    if tail_char_count > _TAIL_PRESERVE_LIMIT:
        while tail_char_count > _TAIL_PRESERVE_LIMIT and tail_start_idx < len(messages) - 1:
            tail_char_count -= len((messages[tail_start_idx].get("content") or "").strip())
            tail_start_idx += 1

    # --- Format head verbatim ---
    first_user_content = (messages[first_user_idx].get("content") or "").strip()
    head_parts = [f"## First User Message\n{first_user_content}"]
    early_tc = []
    for m in messages[first_user_idx + 1:head_end_idx]:
        c = (m.get("content") or "").strip()
        if c:
            if len(c) > 3000:
                c = c[:3000] + "… [truncated]"
            early_tc.append(c)
    if early_tc:
        head_parts.append("## Early Tool Calls\n" + "\n".join(early_tc))
    head_text = "\n\n".join(head_parts)

    # --- Format tail verbatim ---
    last_user_content = (messages[last_user_idx].get("content") or "").strip()
    tail_parts = [f"## Last User Message\n{last_user_content}"]
    recent_tc = []
    for m in messages[last_user_idx + 1:]:
        c = (m.get("content") or "").strip()
        if c:
            if len(c) > 3000:
                c = c[:3000] + "… [truncated]"
            recent_tc.append(c)
    if recent_tc:
        tail_parts.append("## Recent Tool Calls\n" + "\n".join(recent_tc))
    tail_text = "\n\n".join(tail_parts)

    # --- Build middle transcript for summarizer ---
    middle_msgs = messages[head_end_idx:tail_start_idx]
    middle_lines = []
    for m in middle_msgs:
        role = m.get("role", "unknown")
        content = (m.get("content") or "").strip()
        if not content:
            continue
        if len(content) > 3000:
            content = content[:3000] + "… [truncated]"
        middle_lines.append(f"[{role}]: {content}")

    middle_text = "\n".join(middle_lines)
    if len(middle_text) > 60000:
        middle_text = middle_text[:60000] + "\n… [transcript truncated]"

    # Only send middle to summarizer — head/tail already extracted
    prompt = (
        "You are compressing the MIDDLE portion of a conversation transcript. "
        "The first user message, early tool calls, last user message, and recent tool "
        "calls are already extracted separately — DO NOT reproduce them.\n\n"

        "YOUR ONLY JOB: Compress this transcript into labeled turn pairs.\n\n"

        "OUTPUT FORMAT:\n\n"
        "## user\n"
        "[1-2 sentence summary: what the user asked/requested]\n"
        "## model\n"
        "[Compressed actions: which files were viewed/edited/created, which tools were "
        "called, compressed tool result, any errors or problems encountered]\n\n"
        "Repeat ## user / ## model pairs as needed. Single-sided turns are allowed "
        "(e.g., only ## model if the model acted without a new user prompt).\n"
        "Be DENSE — name files, tools, and outcomes. Skip pleasantries and meta-talk.\n\n"

        "If this transcript segment contains MORE THAN 5 tool calls, also add at the end:\n\n"
        "## Intermediary Summary\n"
        "[For each tool call beyond the last 5: what tool was called, compressed result, "
        "any problems or improvements needed]\n\n"

        "HARD RULES:\n"
        "- Output ONLY the ## user / ## model pairs (and optional ## Intermediary Summary).\n"
        "- NEVER output ## First User Message, ## Early Tool Calls, ## Last User Message, "
        "## Recent Tool Calls, or any other section headers.\n"
        "- NEVER add high-level summaries, synthesis, overview, or commentary.\n"
        "- Never invent details. Write [unclear] if ambiguous.\n"
        "- Preserve code, paths, commands, errors VERBATIM in code blocks within turn pairs.\n"
        "- BUG FIX REPORTING (MANDATORY): When a bug was found AND fixed in a specific file, "
        "the ## model turn MUST include the actual code — not just a prose description. Format:\n"
        "    File: path/to/file.py:L###\n"
        "    PROBLEM: [2-5 line code snippet showing the broken code]\n"
        "    FIX: [2-5 line code snippet showing the corrected code]\n"
        "  If an issue was identified but NOT resolved, do NOT mention it at all.\n"
        "  Only include fix details for bugs actually fixed in that turn.\n\n"
        f"---\n{middle_text}"
    )

    # Load settings: primary model + fallback chain
    settings = _load_ctx_pass_settings()
    model = settings.get("summarizer_model") or req.model  # fallback to current
    browser_acc = settings.get("browser_data_acc", "").strip()
    fallback_models: list[str] = settings.get("fallback_models", [])
    browser_profiles: list[str] = settings.get("browser_profiles", [])

    logger.info(
        "[context-pass] chat_id=%s | model=%r | browser_acc=%r | fallbacks=%s/%s | transcript_len=%d",
        req.chat_id, model, browser_acc, fallback_models, browser_profiles, len(messages),
    )

    async def _try_ctx_pass_call(mdl: str, browser_acc_name: str = "") -> dict[str, Any] | None:
        """Try a single context pass call. Returns result dict or None on failure."""
        try:
            api_backend = _resolve_api_backend(mdl)
            if api_backend:
                connector = get_connector(api_backend)
                result = await connector.chat(message=prompt, model=mdl, thinking_mode="fast")
            elif browser_acc_name:
                from engine.service import ChatService
                from engine.config import _SYSTEM
                acc_dir = _SYSTEM / browser_acc_name
                if not acc_dir.exists():
                    logger.warning("[context-pass] Browser profile dir not found: %s", acc_dir)
                    return None
                temp_service = ChatService(user_data_dir=str(acc_dir))
                try:
                    result = await temp_service.chat(message=prompt, model=mdl, thinking_mode="fast")
                finally:
                    await temp_service.close()
            else:
                result = await service.chat(message=prompt, model=mdl, thinking_mode="fast")

            answer = result.get("answer", "").strip()
            if answer:
                return result
            logger.warning("[context-pass] %s returned empty: %s", mdl, result.get("error", ""))
            return None
        except Exception as exc:
            logger.warning("[context-pass] %s failed: %s: %s", mdl, type(exc).__name__, exc)
            return None

    def _assemble_summary(flow_text: str) -> str:
        """Assemble final output: head + flow + tail + continue."""
        parts = [head_text]
        if flow_text.strip():
            parts.append(f"## Conversation Flow\n{flow_text}")
        parts.append(tail_text)
        parts.append("continue")
        return "\n\n".join(parts)

    # Step 1: Primary model (with primary browser profile if Qwen)
    result = await _try_ctx_pass_call(model, browser_acc)
    if result:
        return {"summary": _assemble_summary(result.get("answer", "").strip())}

    # Step 2: Fallback models
    for fb_model in fallback_models[:2]:
        result = await _try_ctx_pass_call(fb_model)
        if result:
            logger.info("[context-pass] Fallback model succeeded: %s", fb_model)
            return {"summary": _assemble_summary(result.get("answer", "").strip())}

    # Step 3: Fallback browser profiles (using primary model)
    for fb_profile in browser_profiles[:2]:
        result = await _try_ctx_pass_call(model, fb_profile)
        if result:
            logger.info("[context-pass] Fallback browser profile succeeded: %s", fb_profile)
            return {"summary": _assemble_summary(result.get("answer", "").strip())}

    return {"error": "All summarizer fallbacks exhausted — no model/browser combination succeeded"}


# ---------------------------------------------------------------------------
# Fork endpoint — create a new chat branched from any user message
# ---------------------------------------------------------------------------

@router.post("/api/chat/fork")
async def fork_chat(payload: dict[str, str]) -> dict[str, Any]:
    """Fork a chat from a specific message.

    Creates a new chat containing all messages up to and including the specified
    message_id. For Qwen chats, creates a fresh upstream session and injects
    conversation history as the first message (parent_id=None).

    Body:
        chat_id: Source chat ID
        message_id: Message ID to fork from (inclusive)

    Returns:
        {chat_id: <new_chat_id>, message_count: <int>}
    """
    source_chat_id = payload.get("chat_id", "")
    message_id_str = payload.get("message_id", "")

    if not source_chat_id or not message_id_str:
        raise HTTPException(status_code=400, detail="chat_id and message_id required")

    try:
        message_id = int(message_id_str)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="message_id must be an integer")

    # Check if source chat uses Qwen provider
    from server.database import get_chat_provider, get_db as _get_db
    source_provider = get_chat_provider(source_chat_id)
    is_qwen = source_provider == "qwen"

    # Get the fork message first (to return its content for the input box)
    with _get_db() as conn:
        fork_msg_row = conn.execute(
            "SELECT id, role, content FROM messages WHERE chat_id = ? AND id = ?",
            (source_chat_id, message_id),
        ).fetchone()

    if not fork_msg_row:
        raise HTTPException(status_code=404, detail=f"Message {message_id} not found in chat {source_chat_id}")

    fork_message_content = fork_msg_row["content"] or ""

    # Get all messages BEFORE the fork message (exclusive)
    with _get_db() as conn:
        rows = conn.execute(
            "SELECT id, role, content, thinking, memory_used, parent_id, created_at "
            "FROM messages WHERE chat_id = ? AND id < ? ORDER BY id ASC",
            (source_chat_id, message_id),
        ).fetchall()

    # For Qwen: create fresh upstream session before creating local chat
    _new_upstream_id = None
    if is_qwen:
        try:
            _headers = await service._ensure_headers()
            from engine.session import create_new_chat as _create_qwen_chat
            _new_upstream_id = await _create_qwen_chat(_headers)
            if not _new_upstream_id:
                _headers = await service._refresh_headers()
                _new_upstream_id = await _create_qwen_chat(_headers)
        except Exception as e:
            logger.warning("[fork] Failed to create Qwen upstream session: %s", e)
            # Continue without upstream session — user can still see history

    # Create new chat
    new_chat_id = uuid.uuid4().hex
    ensure_chat(
        new_chat_id,
        title=f"Fork from {source_chat_id[:8]}",
        provider=source_provider,
        upstream_session_id=_new_upstream_id,
    )

    # Build history block for Qwen (injected as first user message)
    _history_lines = []
    if is_qwen:
        # Fetch full messages with skill events for tool call history
        _prev_msgs = get_messages(source_chat_id, include_skill_events=True)
        # Only include messages before the fork point (exclusive)
        _prev_msgs = [m for m in _prev_msgs if m["id"] < message_id]
        for _pm in _prev_msgs:
            if _pm["role"] not in ("user", "assistant"):
                continue
            if _pm["content"]:
                _history_lines.append(f"[{_pm['role']}]: {_pm['content']}")
            for _sev in (_pm.get("skill_events") or []):
                _sev_type = _sev.get("type", "")
                if _sev_type == "skill_start":
                    _tc_name = _sev.get("name", "unknown")
                    _tc_attrs = _sev.get("data", {}).get("attrs", {})
                    _history_lines.append(f"[tool_call]: {_tc_name}({_tc_attrs})")
                elif _sev_type == "skill_end":
                    _tr_name = _sev.get("name", "unknown")
                    _tr_ok = _sev.get("ok", True)
                    _tr_error = _sev.get("error")
                    _tr_result = str(_sev.get("result", ""))[:2000]
                    if _tr_error:
                        _history_lines.append(f"[tool_result]: {_tr_name} (ok={_tr_ok}): ERROR: {_tr_error}")
                    else:
                        _history_lines.append(f"[tool_result]: {_tr_name} (ok={_tr_ok}): {_tr_result}")

    # Copy messages into new chat
    msg_count = 0
    for row in rows:
        mem = None
        if row["memory_used"]:
            try:
                mem = json.loads(row["memory_used"])
            except (json.JSONDecodeError, TypeError):
                pass
        add_message(
            new_chat_id,
            row["role"],
            row["content"] or "",
            row["thinking"],
            None,  # Don't copy parent_id (new chat has its own lineage)
            None,  # Don't copy skill_events
            mem,
        )
        msg_count += 1

    # For Qwen: store history injection data so the chat endpoint can prepend
    # it to the first message sent in the forked chat (fresh upstream session).
    if is_qwen and _history_lines:
        from server.database import get_db as _db
        _history_block = "[PREVIOUS CONVERSATION]\n" + "\n".join(_history_lines) + "\n[END PREVIOUS CONVERSATION]\n\n"
        with _db() as conn:
            conn.execute(
                "UPDATE chats SET fork_history = ? WHERE id = ?",
                (_history_block, new_chat_id),
            )
        logger.info("[fork] Stored %d history lines for Qwen fork %s", len(_history_lines), new_chat_id)

    logger.info("[fork] Created %s from %s at message %d (%d messages copied, qwen=%s)",
                new_chat_id, source_chat_id, message_id, msg_count, is_qwen)
    return {
        "chat_id": new_chat_id,
        "message_count": msg_count,
        "fork_message": fork_message_content,
    }