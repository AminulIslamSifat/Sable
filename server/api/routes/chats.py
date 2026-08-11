from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from engine.scraper import get_settings as get_scraper_settings
from server.database import (
    ensure_chat, list_chats, get_messages, delete_chat, search_messages,
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
    # Always generate a local UUID instantly — no browser launch, no API call.
    # The actual model-specific session is created lazily on first message send
    # by /api/chat, which knows the correct model and provider at that point.
    prefix = "browser-" if get_scraper_settings().get("enabled") else "local-"
    chat_id = f"{prefix}{uuid.uuid4().hex}"
    ensure_chat(chat_id, "New chat", None, project_id=request.project_id)
    return {"chat_id": chat_id}

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