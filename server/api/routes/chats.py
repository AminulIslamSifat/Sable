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


def _fuzzy_score(query: str, text: str) -> float:
    """True fuzzy score (0.0–1.0) using SequenceMatcher + token overlap.
    Handles typos like 'contex' → 'context'. Case-insensitive.
    """
    if not query or not text:
        return 0.0
    from difflib import SequenceMatcher
    ql = query.lower().strip()
    tl = text.lower()

    # 1. Exact substring → instant high score
    if ql in tl:
        return 1.0

    # 2. Best SequenceMatcher ratio against sliding windows of text
    #    This catches typos, partial matches, transpositions
    best = 0.0
    q_len = len(ql)
    # Check full-text ratio first
    best = SequenceMatcher(None, ql, tl[:max(q_len * 3, 200)]).ratio()

    # Slide through words for better local matches
    words = tl.split()
    for i, word in enumerate(words):
        # Single word comparison
        r = SequenceMatcher(None, ql, word).ratio()
        if r > best:
            best = r
        # Multi-word window (up to 4 words)
        if i < len(words) - 1:
            window = " ".join(words[i : i + min(4, len(words) - i)])
            r = SequenceMatcher(None, ql, window).ratio()
            if r > best:
                best = r
        if best >= 0.95:
            break  # Good enough

    # 3. Token overlap bonus — if query tokens partially match text tokens
    q_tokens = ql.split()
    if len(q_tokens) > 1:
        matched = 0
        for qt in q_tokens:
            for tw in words:
                if SequenceMatcher(None, qt, tw).ratio() >= 0.7:
                    matched += 1
                    break
        token_ratio = matched / len(q_tokens)
        # Blend: sequence matcher (70%) + token overlap (30%)
        best = best * 0.7 + token_ratio * 0.3

    return round(min(1.0, best), 4)


_PER_SECTION = 10
_MIN_SCORE = 0.75


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
    import re

    results: dict[str, list] = {k: [] for k in sources}
    like_ci = f"%{query}%"  # SQLite LIKE is case-insensitive for ASCII

    # ── 1. Chat messages (broad fetch → fuzzy filter) ──
    _mem_re = re.compile(r'^\[RELEVANT MEMORY CONTEXT\][\s\S]*?\n\n')
    _ts_re = re.compile(r'^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]\n?')
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT m.id, m.chat_id, m.role, m.content, m.created_at, c.title "
                "FROM messages m JOIN chats c ON m.chat_id = c.id "
                "WHERE m.content LIKE ? COLLATE NOCASE ORDER BY m.id DESC LIMIT 200",
                (like_ci,),
            ).fetchall()
            scored = []
            for row in rows:
                d = dict(row)
                content = d.get("content") or ""
                content = _mem_re.sub("", content)
                content = _ts_re.sub("", content)
                # Score against chat title + content
                score = max(_fuzzy_score(query, d.get("title") or ""), _fuzzy_score(query, content))
                if score >= _MIN_SCORE:
                    scored.append((score, {
                        **d,
                        "preview": content[:300],
                        "score": round(score, 3),
                        "source": "message",
                    }))
            scored.sort(key=lambda x: x[0], reverse=True)
            results["messages"] = [item for _, item in scored[:_PER_SECTION]]
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
                "WHERE se.event_data LIKE ? COLLATE NOCASE ORDER BY se.id DESC LIMIT 200",
                (like_ci,),
            ).fetchall()
            seen: set[int] = set()
            scored = []
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
                score = max(_fuzzy_score(query, skill_name), _fuzzy_score(query, raw_str), _fuzzy_score(query, d.get("title") or ""))
                if score >= _MIN_SCORE:
                    scored.append((score, {
                        "skill": skill_name,
                        "preview": raw_str[:300],
                        "chat_id": d.get("chat_id"),
                        "title": d.get("title"),
                        "created_at": d.get("created_at"),
                        "message_id": mid,
                        "score": round(score, 3),
                        "source": "skill",
                    }))
            scored.sort(key=lambda x: x[0], reverse=True)
            results["skills"] = [item for _, item in scored[:_PER_SECTION]]
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
                score = max(_fuzzy_score(query, mkey), _fuzzy_score(query, mval))
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
            rows = conn.execute(
                "SELECT id, title, content, note_type, created_at, updated_at FROM notes "
                "WHERE note_type != 'todo' AND archived = 0 "
                "AND (title LIKE ? COLLATE NOCASE OR content LIKE ? COLLATE NOCASE) "
                "ORDER BY updated_at DESC LIMIT 100",
                (like_ci, like_ci),
            ).fetchall()
            scored = []
            for row in rows:
                d = dict(row)
                score = max(_fuzzy_score(query, d.get("title") or ""), _fuzzy_score(query, d.get("content") or ""))
                if score >= _MIN_SCORE:
                    scored.append((score, {
                        **d,
                        "preview": (d.get("content") or "")[:300],
                        "score": round(score, 3),
                        "source": "note",
                    }))
            scored.sort(key=lambda x: x[0], reverse=True)
            results["notes"] = [item for _, item in scored[:_PER_SECTION]]
    except Exception as e:
        logger.warning("Search notes failed: %s", e)

    # ── 5. Todos (note_type == 'todo') ──
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT id, title, content, items, due_date, created_at, updated_at FROM notes "
                "WHERE note_type = 'todo' AND archived = 0 "
                "AND (title LIKE ? COLLATE NOCASE OR content LIKE ? COLLATE NOCASE) "
                "ORDER BY updated_at DESC LIMIT 100",
                (like_ci, like_ci),
            ).fetchall()
            scored = []
            for row in rows:
                d = dict(row)
                score = max(_fuzzy_score(query, d.get("title") or ""), _fuzzy_score(query, d.get("content") or ""))
                if score >= _MIN_SCORE:
                    scored.append((score, {
                        **d,
                        "preview": (d.get("content") or "")[:300],
                        "score": round(score, 3),
                        "source": "todo",
                    }))
            scored.sort(key=lambda x: x[0], reverse=True)
            results["todos"] = [item for _, item in scored[:_PER_SECTION]]
    except Exception as e:
        logger.warning("Search todos failed: %s", e)

    # ── 6. Schedules ──
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM schedules "
                "WHERE title LIKE ? COLLATE NOCASE OR description LIKE ? COLLATE NOCASE "
                "ORDER BY start_date DESC LIMIT 100",
                (like_ci, like_ci),
            ).fetchall()
            scored = []
            for row in rows:
                d = dict(row)
                score = max(_fuzzy_score(query, d.get("title") or ""), _fuzzy_score(query, d.get("description") or ""))
                if score >= _MIN_SCORE:
                    scored.append((score, {
                        **d,
                        "preview": (d.get("description") or d.get("title") or "")[:300],
                        "score": round(score, 3),
                        "source": "schedule",
                    }))
            scored.sort(key=lambda x: x[0], reverse=True)
            results["schedules"] = [item for _, item in scored[:_PER_SECTION]]
    except Exception as e:
        logger.warning("Search schedules failed: %s", e)

    # ── 7. Research (sable_output markdown files) ──
    try:
        from engine.config import RESEARCH_DIR
        if RESEARCH_DIR.exists():
            scored = []
            for f in sorted(RESEARCH_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
                try:
                    text = f.read_text(encoding="utf-8")
                except Exception:
                    continue
                title = f.stem.replace("_", " ").replace("-", " ")
                score = max(_fuzzy_score(query, title), _fuzzy_score(query, text[:2000]))
                if score >= _MIN_SCORE:
                    body = re.sub(r'^---\s*\n.*?\n---\s*\n', '', text, count=1, flags=re.DOTALL)
                    first_para = ""
                    for line in body.splitlines():
                        s = line.strip()
                        if s and not s.startswith(("#", ">", "---")):
                            first_para = s[:300]
                            break
                    scored.append((score, {
                        "id": f.stem,
                        "filename": f.name,
                        "title": title.title(),
                        "preview": first_para,
                        "score": round(score, 3),
                        "source": "research",
                    }))
            scored.sort(key=lambda x: x[0], reverse=True)
            results["research"] = [item for _, item in scored[:_PER_SECTION]]
    except Exception as e:
        logger.debug("Search research skipped: %s", e)

    # ── 8. Agents (sable_output markdown files) ──
    try:
        from engine.config import AGENT_OUTPUT_DIR
        if AGENT_OUTPUT_DIR.exists():
            scored = []
            for f in sorted(AGENT_OUTPUT_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
                if f.stem.endswith("_conversation"):
                    continue
                try:
                    text = f.read_text(encoding="utf-8")
                except Exception:
                    continue
                title = f.stem.replace("_", " ").replace("-", " ")
                score = max(_fuzzy_score(query, title), _fuzzy_score(query, text[:2000]))
                if score >= _MIN_SCORE:
                    body = re.sub(r'^---\s*\n.*?\n---\s*\n', '', text, count=1, flags=re.DOTALL)
                    first_para = ""
                    for line in body.splitlines():
                        s = line.strip()
                        if s and not s.startswith(("#", ">", "---")):
                            first_para = s[:300]
                            break
                    scored.append((score, {
                        "id": f.stem,
                        "filename": f.name,
                        "title": title.title(),
                        "preview": first_para,
                        "score": round(score, 3),
                        "source": "agent",
                    }))
            scored.sort(key=lambda x: x[0], reverse=True)
            results["agents"] = [item for _, item in scored[:_PER_SECTION]]
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
                import re
                content = d.get("content") or ""
                content = re.sub(r'^\[RELEVANT MEMORY CONTEXT\][\s\S]*?\n\n', '', content)
                content = re.sub(r'^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]\n?', '', content)
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
    # For Qwen models, create upstream session separately and store in DB.
    from server.utils import _is_api_model
    local_chat_id = uuid.uuid4().hex
    upstream_session_id = None
    if not _is_api_model(request.model):
        # Qwen model — create upstream session
        try:
            upstream_session_id = await retry_async(
                lambda: service.create_chat(model=request.model),
                label="create_chat",
            )
        except Exception as exc:
            return {"error": f"Session startup failed: {type(exc).__name__}: {exc}"}
        if not upstream_session_id:
            return {"error": "Could not create chat session"}
    ensure_chat(local_chat_id, "New chat", None, project_id=request.project_id, upstream_session_id=upstream_session_id)
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
    messages = get_messages(req.chat_id)
    if not messages:
        return {"error": "No messages in this chat"}

    # Build a compact transcript (skip empty/system noise)
    lines: list[str] = []
    for m in messages:
        role = m.get("role", "unknown")
        content = (m.get("content") or "").strip()
        if not content:
            continue
        if len(content) > 3000:
            content = content[:3000] + "… [truncated]"
        lines.append(f"[{role}]: {content}")

    if len(lines) < 2:
        return {"error": "Not enough context to summarize"}

    transcript = "\n".join(lines)
    if len(transcript) > 60000:
        transcript = transcript[:60000] + "\n… [transcript truncated]"

    prompt = (
        "You are a context handoff summarizer. Below is a conversation transcript from "
        "a session that is being switched to a different model. Produce a focused "
        "operational briefing so the new model can continue the work with zero loss "
        "of state. No filler, no meta-commentary, no 'here is a summary' — jump "
        "straight to substance.\n\n"

        "HARD RULES (apply to all sections):\n"
        "- Never invent, infer, or smooth over details that are not explicitly present "
        "in the transcript. If something is ambiguous, unstated, or uncertain, write "
        "[unclear] instead of guessing.\n"
        "- Preserve all code snippets, file paths, commands, config values, error "
        "messages, and stack traces VERBATIM — never paraphrase or reword these. "
        "Quote them exactly as they appear in the transcript, in code blocks.\n"
        "- Preserve exact technical details: package/library versions, OS, device/"
        "environment specifics, variable names, function signatures.\n"
        "- The word limit below applies to prose only. Verbatim code/error/config "
        "blocks are exempt from the word count and should be included in full when "
        "they represent the current working state.\n\n"

        "Structure (each section progressively more detailed than the last, except "
        "verbatim blocks which are always complete regardless of section):\n\n"

        "• Working topic — Concise but complete: what the topic is, the motive/goal, "
        "the plan, what's been done, what's pending. State clearly whether the task "
        "is finished, abandoned, or stopped mid-way — and if mid-way, the exact point "
        "of interruption.\n\n"

        "• Background — Only the context needed to understand the current task "
        "(prior decisions, constraints, why this approach was chosen over others). "
        "Skip anything not relevant to continuing the work.\n\n"

        "• Last exchange (most detailed so far) — The user's most recent prompt "
        "passed near-verbatim, plus what was actually attempted in response: what "
        "was tried, why that approach was chosen, what succeeded, what failed (with "
        "exact error output), and the precise current state of any files/code/"
        "commands at the point the session stopped.\n\n"

        "• Planned next move (MOST detailed) — Concrete next steps in order, open "
        "questions, known blockers, and every specific file/path/config/command "
        "involved. If the previous model had a next action in mind but didn't "
        "execute it, state exactly what that action was.\n\n"

        "Target ~800 words of prose across all sections combined (verbatim blocks "
        "excluded from this count). Omit anything irrelevant to resuming the work.\n\n"
        f"---\n{transcript}"
    )

    # Load settings: model + browser-data-acc
    settings = _load_ctx_pass_settings()
    model = settings.get("summarizer_model") or req.model  # fallback to current
    browser_acc = settings.get("browser_data_acc", "").strip()

    logger.info(
        "[context-pass] chat_id=%s | model=%r | browser_acc=%r | settings=%s | transcript_len=%d",
        req.chat_id, model, browser_acc, settings, len(transcript),
    )

    try:
        # Route: API connector (gemini/deepseek/groq/mistral) vs Qwen ChatService
        api_backend = _resolve_api_backend(model)

        if api_backend:
            # Non-Qwen model → use the appropriate API connector directly
            logger.info("[context-pass] routing via connector: %s", api_backend)
            connector = get_connector(api_backend)
            result = await connector.chat(
                message=prompt,
                model=model,
                thinking_mode="fast",
            )
        elif browser_acc:
            # Qwen model with dedicated browser profile
            from engine.service import ChatService
            from engine.config import _SYSTEM
            acc_dir = _SYSTEM / browser_acc
            if not acc_dir.exists():
                logger.error("[context-pass] browser profile dir not found: %s", acc_dir)
                return {"error": f"Browser profile '{browser_acc}' not found"}
            logger.info("[context-pass] using dedicated ChatService with profile: %s", acc_dir)
            temp_service = ChatService(user_data_dir=str(acc_dir))
            try:
                result = await temp_service.chat(
                    message=prompt,
                    model=model,
                    thinking_mode="fast",
                )
            finally:
                await temp_service.close()
        else:
            # Qwen model, default service
            logger.info("[context-pass] using default service, model=%r", model)
            result = await service.chat(
                message=prompt,
                model=model,
                thinking_mode="fast",
            )

        logger.info("[context-pass] result keys=%s, answer_len=%d, error=%r",
                    list(result.keys()), len(result.get("answer", "")), result.get("error"))
        answer = result.get("answer", "").strip()
        if not answer:
            err = result.get("error", "")
            logger.warning("[context-pass] empty answer. error=%r, full result: %s", err, result)
            return {"error": f"Summarization returned empty response. {err}".strip()}
        return {"summary": answer}
    except Exception as exc:
        logger.exception("[context-pass] summarization failed")
        return {"error": f"Summarization failed: {type(exc).__name__}: {exc}"}


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