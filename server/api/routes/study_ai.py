"""Study-mode AI — ask about a PDF, explain selections, save answers as notes.

Complements study.py (library + meta store) by layering AI on top of the
already-extracted ``page_text``:

  * configurable model / thinking mode / persona / custom instructions
  * streaming answers grounded in the document text
  * answers persisted against the exact selection (or page / whole doc)

Stored config lives in ``system/study_ai_config.json``. Saved answers live
inside the document's meta JSON under the ``ai_notes`` key (same store the
reader already loads), so the frontend needs no second fetch.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, AsyncGenerator

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from engine import library_catalog as _catalog
from engine import library_rag as _rag
from engine.config import get_all_models, get_model_config
from engine.tools_loader import get_study_tool_schemas
from engine.study.context import StudyContext, reset_study_context, set_study_context

from connectors import get_connector
from server.utils import _is_api_model, _resolve_api_backend

from ..dependencies import service, sse
from .study import _clean_rects, _load_page_text, _read_meta, _update_meta
from .study_notes import (
    _find as _find_note,
    _read_body as _read_note_body,
    _read_index as _read_notes_index,
    _write_body as _write_note_body,
)

router = APIRouter()
logger = logging.getLogger("sable.study.ai")

_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_CONFIG_PATH = _ROOT / "system" / "study_ai_config.json"
_INSTRUCTION_DIR = _ROOT / "instruction"

_MAX_CTX_CHARS = 24_000      # context budget for selection / page scope
_MAX_DOC_CTX = 60_000        # context budget for whole-document scope
_MAX_SELECTION_CHARS = 4_000
_MAX_PERSONA_CHARS = 2_500
_MAX_BODY_NOTE = 200_000  # note body budget sent to the model for a rewrite

# Backends whose connectors read a local path and base64-inline the bytes
# themselves (see server/api/routes/chat.py). Everything else needs a real
# provider-side upload before the model can see the file.
_DIRECT_READ_BACKENDS = frozenset({"gemini", "groq", "mistral", "openai"})
# Scraper transports upload through Playwright inside the connector's own
# stream_chat() loop — they consume LOCAL PATHS on the `files` kwarg and call
# engine.upload_file() per path. They are NOT direct-read (they don't inline
# bytes) and they have NO provider-side upload endpoint, so the generic
# _upload_attachment() path must never be used for them.
_SCRAPER_FILE_BACKENDS = frozenset({"deepseek_scraper", "qwen_scraper"})

_DEFAULT_CONFIG: dict[str, str] = {
    "model": "",
    "thinking_mode": "",
    "persona": "",
    "custom_prompt": "",
}

# Task instructions per action. ``ask`` uses the user's own question on top.
# Replaces the full Sable instruction registry (persona, skills, MCP, tool
# schemas — including chat_title) for study answers. Study questions must not
# drag in the chat harness, so we hand the model a self-contained directive.
_STUDY_SYSTEM = (
    "You are an expert study assistant embedded in a Sable study workspace. "
    "You are in STUDY MODE and you CONTROL study mode through your tools: "
    "you can highlight passages, take notes, edit notes, read any page of any "
    "library PDF, and inspect the user's study activity.\n"
    "Use the tools when the user asks you to change something or look something "
    "up — do not just describe what you would do. Prefer precise, surgical "
    "tools (append / replace_section) over rewriting a whole note.\n"
    "Answer in clear, well-structured markdown. Never output a chat_title "
    "block — that tool does not exist here."
)

# Safety net: strip any chat_title / stray tool tags the model emits anyway.
_TITLE_TAG_RE = re.compile(r"<chat_title>.*?</chat_title>", re.S | re.I)
_TOOL_TAG_RE = re.compile(r"</?(?:action|tool_call|chat_title)\b[^>]*>", re.I)

# System directive for the note-editing tools. Unlike the ask path (which talks
# to the user), an edit must go through study_note so the change lands in the
# live file the editor is showing — never as a chat message to approve.
_NOTE_EDIT_SYSTEM = (
    "You are a writing assistant editing a markdown note in a Sable study "
    "editor. The note lives in the study store; you change it ONLY through the "
    "study_note tool.\n"
    "Rules:\n"
    "- Apply the requested task with the study_note tool. Never paste the whole "
    "note back as chat text.\n"
    "- Prefer surgical actions: 'append' to add at the end, 'replace_section' "
    "to rewrite one heading, 'update' only when the entire body must change.\n"
    "- The note id is {note_id}. Pass it as note_id.\n"
    "- After editing, reply with one short sentence describing what you changed "
    "(or answer the user, for a question). Never output a chat_title block.\n"
    "- The note is Obsidian-flavoured markdown: headings, **bold**, "
    "==highlight==, tables, callouts ([!note]/[!tip]/[!warning]), $math$, and "
    "```mermaid fences."
)

_ACTION_PROMPTS: dict[str, str] = {
    "explain": (
        "Explain the selected text clearly and simply. Break down what it means, "
        "why it matters, and any implied steps or assumptions."
    ),
    "meaning": (
        "Give the precise meaning of the selected text. Clarify every term and "
        "call out any ambiguity."
    ),
    "summarize": "Summarize the selected text into concise key points as a bullet list.",
    "define": "Define each key term or concept in the selected text, one per line.",
    "ask": "Answer the user's question using the document context above.",
    "summary": (
        "Write a structured summary of this document: a short overview, the main "
        "sections, and the key takeaways."
    ),
    "concepts": "List the key concepts in this document, each with a one-line explanation.",
    "exam": (
        "Generate likely exam questions from this document. Follow each question "
        "with a concise model answer."
    ),
    "glossary": "Build a glossary of the important terms in this document (term — definition).",
}

# Appended only when sibling-document passages are actually present, so a
# single-doc answer is never distracted by provenance rules it cannot use.
_PROVENANCE_HINT = (
    "Task: Some context passages are tagged [CURRENT DOCUMENT: ...] and others "
    "[OTHER DOCUMENT: ...]. CURRENT DOCUMENT is the book the user has open; "
    "OTHER DOCUMENT passages come from other books on the same topic. When a "
    "fact comes from another document, name that document and its page, and "
    "say plainly that it is from a different book on the topic. Never imply "
    "an OTHER DOCUMENT passage came from the open document."
)

# Task instructions for the note editor's AI menu. Unlike the document actions
# above, these REWRITE the user's note body and must return markdown only —
# no preamble, no explanation, no code fence around the whole note.
_NOTE_SYSTEM = (
    "You are a writing assistant embedded in a markdown note editor. "
    "The user's note is written in Obsidian-flavoured markdown, which supports "
    "headings, **bold**, *italic*, ==highlight==, ~~strike~~, `code`, tables, "
    "task lists, blockquote callouts ([!note] / [!tip] / [!warning] / "
    "[!important] / [!example]), LaTeX math ($inline$ and $$block$$), and "
    "```mermaid fenced diagrams. "
    "Return ONLY the rewritten note as markdown. Never wrap the whole note in a "
    "code fence. Never add commentary, apologies, or a preamble — the response "
    "is inserted straight into the editor. "
    "Preserve the author's original meaning, facts, and any [[links]] or "
    "#tags unless the task explicitly says otherwise. "
    "Never call tools, never emit tool tags, and never output a chat_title "
    "block."
)

_NOTE_ACTIONS: dict[str, str] = {
    "ask": "Answer the user's question about this note, then return the note unchanged.",
    "refactor": (
        "Refactor this note for clarity and flow: tighten sentences, fix logical "
        "ordering, remove duplication, and improve headings. Keep every fact."
    ),
    "redesign": (
        "Redesign the visual structure of this note: introduce clear headings, "
        "callouts, tables, and bullet lists where they help. Keep the content."
    ),
    "organize": (
        "Reorganize this note into a logical outline: group related points, add "
        "headings, and move stray items under the right section."
    ),
    "enhance": (
        "Enhance this note: expand terse points into complete thoughts, add "
        "concrete examples, and sharpen the wording — without inventing facts."
    ),
    "summarize": (
        "Condense this note into its essential points as a short summary with a "
        "few key bullets. Keep it faithful and compact."
    ),
    "expand": (
        "Expand this note with more depth and detail: elaborate each point, add "
        "context, and include examples where useful."
    ),
    "clarify": (
        "Rewrite this note so it is easier to understand: simpler sentences, "
        "defined jargon, and explicit transitions."
    ),
    "fix-grammar": (
        "Correct spelling, grammar, and punctuation. Do not change wording or "
        "structure beyond what correctness requires."
    ),
    "add-examples": (
        "Keep the note as-is but add concrete examples under each major point."
    ),
    "flashcards": (
        "Convert this note into study flashcards. Use one '### Question' heading "
        "followed by the answer, repeated for each card."
    ),
    "questions": (
        "Generate likely exam or review questions from this note. Follow each "
        "question with a concise model answer."
    ),
}


# ── Config ───────────────────────────────────────────────────────────────────


def _load_config() -> dict[str, str]:
    cfg = dict(_DEFAULT_CONFIG)
    if _CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(_CONFIG_PATH.read_text(encoding="utf-8")) or {})
        except Exception:
            pass
    if not cfg.get("model"):
        models = get_all_models()
        if models:
            cfg["model"] = models[0]["id"]
    return cfg


def _save_config(cfg: dict[str, Any]) -> None:
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


# ── Meta endpoints ───────────────────────────────────────────────────────────


@router.get("/api/study/ai/models")
def study_ai_models() -> dict[str, Any]:
    """Selectable models + their thinking modes for the study AI config UI."""
    out: list[dict[str, Any]] = []
    for m in get_all_models():
        if m.get("hidden"):
            continue
        out.append({
            "id": m.get("id"),
            "label": m.get("label", m.get("id")),
            # Vision capability lets the frontend decide whether to attach a
            # rendered page image or fall back to extracted text.
            "capabilities": m.get("capabilities") or {},
            "thinking_modes": [
                {"id": t.get("id"), "label": t.get("label", t.get("id"))}
                for t in m.get("thinking_modes", [])
            ],
        })
    return {"models": out}


@router.get("/api/study/ai/personas")
def study_ai_personas() -> dict[str, Any]:
    """Persona names available for study answers (from instruction/*.md)."""
    skip = {"output_format.md", "personal.md"}
    names: list[str] = []
    if _INSTRUCTION_DIR.exists():
        for f in sorted(_INSTRUCTION_DIR.glob("*.md")):
            if f.name in skip or f.name.endswith(".example"):
                continue
            names.append(f.stem)
    return {"personas": names}


@router.get("/api/study/ai/config")
def study_ai_get_config() -> dict[str, str]:
    return _load_config()


@router.put("/api/study/ai/config")
async def study_ai_set_config(payload: dict[str, Any]) -> dict[str, str]:
    cfg = _load_config()
    for key in ("model", "thinking_mode", "persona", "custom_prompt"):
        if key in payload:
            cfg[key] = str(payload.get(key) or "")
    _save_config(cfg)
    return cfg


# ── Prompt building ──────────────────────────────────────────────────────────


def _topic_peers(meta: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    '''Every doc filed under the open document's topic, title-sorted.

    Reads the SQLite catalog rather than walking the meta store, so a topic
    lookup is one indexed SELECT instead of parsing every meta JSON.

    Topic is the primary organizing axis; semester/subject are legacy
    metadata and deliberately NOT part of the match. Returns ('', []) for the
    Others catch-all — that bucket is where unfiled PDFs land, so treating it
    as a topic would glue unrelated books together.
    '''
    topic = str((meta.get('group') or {}).get('topic') or '').strip()
    if not topic or topic == _catalog.OTHERS:
        return '', []

    # A library that predates the catalog has an empty table; build it once
    # from the meta store so the first question after deploy is still fast.
    if _catalog.count() == 0:
        try:
            _catalog.rebuild_from_meta()
        except Exception:
            logger.warning('catalog bootstrap failed', exc_info=True)

    return topic, _catalog.docs_in_topic(topic)


def _build_context(
    meta: dict[str, Any],
    scope: str,
    page: int,
    selection: str,
    query: str = "",
    topic_scope: bool = False,
    pages: list[int] | None = None,
) -> str:
    """Slice the document text to fit the requested scope within the budget.

    For whole-document scope we prefer retrieval over truncation: a long
    textbook does not fit in ``_MAX_DOC_CTX``, and silently dropping its tail
    hides exactly the passage the student is asking about. When a RAG index
    exists and we have a query, we pack the top-ranked passages instead and
    fall back to the old head-first packing only when retrieval finds nothing.
    """
    page_text = _load_page_text(meta)

    if scope == "selection":
        if not (1 <= page <= len(page_text)):
            return ""
        ctx = (page_text[page - 1] or "").strip()
        if len(ctx) > _MAX_CTX_CHARS:
            idx = ctx.find(selection[:60]) if selection else -1
            if idx > 0:
                start = max(0, idx - _MAX_CTX_CHARS // 2)
                ctx = ctx[start:start + _MAX_CTX_CHARS]
            else:
                ctx = ctx[:_MAX_CTX_CHARS]
        return f"[Page {page}]\n{ctx}"

    if scope == "page":
        # Multi-page scope: the reader may have staged several pages (e.g. a
        # question that spans pages 12–14). Slice every requested page and
        # share the budget across them, so no single page crowds out the rest.
        wanted = [p for p in (pages or []) if isinstance(p, int) and 1 <= p <= len(page_text)]
        if not wanted:
            wanted = [page] if 1 <= page <= len(page_text) else []
        if not wanted:
            return ""
        per_page = max(1, _MAX_CTX_CHARS // len(wanted))
        return "\n\n".join(
            f"[Page {p}]\n{(page_text[p - 1] or '').strip()[:per_page]}"
            for p in wanted
        )

    doc_id = str(meta.get("id") or "")

    # Topic-wide retrieval first: a question asked while reading one book gets
    # answered from every indexed book in the topic. Skipped for the Others
    # bucket and for topics holding only the open doc.
    if topic_scope and query.strip() and doc_id:
        topic, peers = _topic_peers(meta)
        indexed = [p["doc_id"] for p in peers if _rag.has_index(p["doc_id"])]
        if topic and len(indexed) > 1:
            labels = {p["doc_id"]: p["title"] for p in peers}
            try:
                retrieved, hits = _rag.context_for_docs(
                    indexed, query, labels=labels, current_doc=doc_id,
                    max_chars=_MAX_DOC_CTX,
                )
            except Exception:
                logger.warning("topic retrieval failed for %s", topic, exc_info=True)
                retrieved, hits = "", []
            if retrieved:
                others = {h["doc_id"] for h in hits if not h.get("current")}
                open_title = str(meta.get("title") or "")
                header = (
                    f'Topic "{topic}" — passages from {len({h["doc_id"] for h in hits})} '
                    f"document(s), ranked across the whole topic.\n"
                    f'CURRENT DOCUMENT is "{open_title}" — the one the user has open.\n'
                    f"OTHER DOCUMENT passages come from {len(others)} sibling "
                    "document(s) in the same topic."
                    if others else
                    f'Topic "{topic}" — passages from {len({h["doc_id"] for h in hits})} '
                    f'document(s). CURRENT DOCUMENT is "{open_title}".'
                )
                return header + "\n---\n" + retrieved

    # Whole document. If the doc fits the budget outright, send it whole —
    # retrieval can only lose context when nothing needs to be dropped.
    full_size = sum(len((t or "")) for t in page_text)
    if full_size <= _MAX_DOC_CTX:
        return _pack_pages(page_text, _MAX_DOC_CTX)

    if query.strip() and doc_id:
        try:
            retrieved, pages = _rag.context_for(doc_id, query, max_chars=_MAX_DOC_CTX)
        except Exception:
            logger.warning("rag retrieval failed for %s", doc_id, exc_info=True)
            retrieved, pages = "", []
        if retrieved:
            header = (
                f"Retrieved passages ({len(pages)} of {len(page_text)} pages) "
                "most relevant to the request:"
            )
            return header + "\n---\n" + retrieved

    # Fallback: no index or no query — pack from the front as before.
    return _pack_pages(page_text, _MAX_DOC_CTX)


def _pack_pages(page_text: list[str], budget: int) -> str:
    """Concatenate page blocks head-first until the character budget is spent."""
    parts: list[str] = []
    total = 0
    for i, text in enumerate(page_text, 1):
        text = (text or "").strip()
        if not text:
            continue
        chunk = f"[Page {i}]\n{text}\n"
        if total + len(chunk) > budget:
            parts.append(f"… ({len(page_text) - i + 1} more pages truncated)")
            break
        parts.append(chunk)
        total += len(chunk)
    return "\n".join(parts)


def _read_persona(name: str) -> str:
    if not name:
        return ""
    path = _INSTRUCTION_DIR / f"{name}.md"
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")[:_MAX_PERSONA_CHARS]
    except Exception:
        return ""


def _study_tool_sections() -> str:
    """Tool-call primer + schemas for a study turn.

    ``get_tool_format`` supplies the ``<action>`` primer and
    ``get_study_tool_schemas`` supplies the ``<tools>`` block — the SAME set
    the native tool-calling path sends, so both transports agree. The shared
    ``output_format.md`` is included last. Purely additive — study's own
    directive is left untouched.
    """
    parts: list[str] = []
    try:
        from connectors.common.instruction_builder import get_tool_format
        primer = get_tool_format("qwen")
        if primer:
            parts.append(primer)
    except Exception:
        logger.debug("study tool primer failed", exc_info=True)
    try:
        from engine.tools_loader import get_study_tool_schemas
        # Study mode exposes EXACTLY get_study_tool_schemas() - the study-gated
        # tools plus the shared allow-list (online_search / image_generator /
        # code_editor / file_uploader / grep_search). Same set
        # the native tool-calling path sends, so the prompt-injected
        # (Qwen/scraper) path and the native path can never disagree.
        #
        # Previously this prepended get_tools_prompt_section(), which dumps the
        # whole agent harness (ask_user, chat_title, critique, execute_command,
        # spawn_agent, MCP, load_tool stubs) and THEN appended study tools. Study
        # mode advertised chat_title + agent tooling it has no handlers for.
        study_tools = get_study_tool_schemas()
        if study_tools:
            lines = ["<tools>"]
            lines.extend(json.dumps(s, ensure_ascii=False) for s in study_tools)
            lines.append("</tools>")
            parts.append("\n".join(lines))
    except Exception:
        logger.debug("study tool schemas failed", exc_info=True)
    of_path = _INSTRUCTION_DIR / "output_format.md"
    if of_path.exists():
        try:
            text = of_path.read_text(encoding="utf-8").strip()
            if text:
                parts.append(text)
        except Exception:
            pass
    return "\n\n".join(p for p in parts if p)


def _build_system(custom_prompt: str, persona_text: str) -> str:
    """Legacy single-shot study directive (standalone /api/study/ai endpoint)."""
    system = _STUDY_SYSTEM
    if custom_prompt.strip():
        system += "\n\nAdditional instructions from the reader:\n" + custom_prompt.strip()
    if persona_text:
        system += "\n\nAdopt the following persona and tone:\n" + persona_text
    system += "\n\n" + _study_tool_sections()
    return system


def _build_study_system(
    provider: str | None,
    persona_text: str,
    custom_prompt: str,
) -> str:
    """Assemble the study system via the SHARED instruction builder.

    Study mode is a normal chat: persona + output format + tool-format primer +
    the study tool set + the study directive appended last. Building it here (not
    by hand) is what keeps study and normal chat on one pipeline, so a transport
    only ever has to inject this string once — exactly like any other chat.
    """
    try:
        from connectors.common.instruction_builder import build_instructions
        return build_instructions(
            provider=provider,
            layout_mode="study",
            persona_override=persona_text or None,
            custom_instructions=custom_prompt or None,
        )
    except Exception:
        logger.warning("study system build failed", exc_info=True)
        return _build_system(custom_prompt, persona_text)


def build_study_turn(
    study_context: dict[str, Any] | None,
    user_message: str,
    provider: str | None = None,
) -> tuple[str, str]:
    """Build ``(system_prompt, grounded_user_message)`` for a study chat turn.

    Called by ``/api/chat`` when ``layout_mode == "study"`` so study chats run
    through the SAME persistence + tool pipeline as normal chats (``messages``
    + ``skill_events`` tables, sidebar history, inline tool cards) instead of
    the old bespoke ``/api/study/ai`` endpoint.

    The system directive replaces Sable's chat/agent harness (no chat_title,
    no Maria persona) and the grounding block is prepended to the user message
    so every turn carries the current page / selection / document context.
    """
    ctx = study_context or {}
    # A note turn is grounded in the note body, not a document.
    _note_id = str(ctx.get("note_id") or "")
    if _note_id:
        return _build_note_turn(_note_id, ctx, user_message)
    doc_id = str(ctx.get("doc_id") or "")
    meta = _read_meta(doc_id) if doc_id else None
    scope = str(ctx.get("scope") or ("selection" if ctx.get("selection") else "doc"))
    selection = str(ctx.get("selection") or "")
    action = str(ctx.get("action") or "")
    try:
        page = int(ctx.get("page") or 1)
    except (TypeError, ValueError):
        page = 1

    cfg = _load_config()
    persona = str(ctx.get("persona") if "persona" in ctx else cfg.get("persona") or "")
    custom_prompt = str(
        ctx.get("custom_prompt") if "custom_prompt" in ctx else cfg.get("custom_prompt") or ""
    )
    system = _build_study_system(provider, _read_persona(persona), custom_prompt)

    # No document (e.g. a free-form study chat) — just the directive + message.
    if not meta:
        return system, user_message

    # Only a genuine question widens to the whole topic; page/selection/action
    # scopes stay pinned to the document the reader has open.
    topic_scope = scope == "doc" and action in ("", "ask")
    _pages = [p for p in (ctx.get("pages") or []) if isinstance(p, int)]
    context = _build_context(
        meta, scope, page, selection, query=user_message,
        topic_scope=topic_scope, pages=_pages,
    )
    _page_label = ", ".join(str(p) for p in _pages) if len(_pages) > 1 else str(page)
    blocks = [f'Document: "{meta.get("title", "")}" (Page {_page_label})']
    if context:
        blocks.append("Document context:\n---\n" + context + "\n---")
    if selection:
        blocks.append("Selected text:\n---\n" + selection[:_MAX_SELECTION_CHARS] + "\n---")
    if action and action in _ACTION_PROMPTS:
        blocks.append("Task: " + _ACTION_PROMPTS[action])
    if "OTHER DOCUMENT" in context:
        blocks.append(_PROVENANCE_HINT)
    blocks.append("User message:\n" + user_message)
    grounded = "\n\n".join(blocks)
    return system, grounded


def _build_note_turn(note_id: str, ctx: dict[str, Any], user_message: str) -> tuple[str, str]:
    """Build (system, grounded_message) for a note-AI turn.

    Mirrors the old /api/study/notes/{id}/ai endpoint but runs through the
    shared /api/chat pipeline, so note AI gets the same persistence + inline
    tool cards as the document AI. Edit actions drive the study_note tool;
    ask answers in prose.
    """
    index = _read_notes_index()
    note = _find_note(index, note_id)
    cfg = _load_config()
    persona = str(ctx.get('persona') if 'persona' in ctx else cfg.get('persona') or '')
    custom_prompt = str(ctx.get('custom_prompt') if 'custom_prompt' in ctx else cfg.get('custom_prompt') or '')
    persona_text = _read_persona(persona)
    if not note:
        return _build_system(custom_prompt, persona_text), user_message
    action = str(ctx.get('action') or 'ask')
    question = str(ctx.get('question') or '')
    content = ctx.get('note_content')
    if not isinstance(content, str) or not content.strip():
        content = _read_note_body(note_id)
    ground = bool(ctx.get('ground'))
    # Keep the file in step with the live editor body so a tool edit lands on
    # exactly what the user sees.
    try:
        if content.strip() and _read_note_body(note_id) != content:
            _write_note_body(note_id, content[:_MAX_BODY_NOTE])
    except Exception:
        logger.debug('note pre-sync failed for %s', note_id, exc_info=True)
    title = note.get('title') or 'Untitled note'
    if action == 'ask':
        system = _NOTE_SYSTEM
        if custom_prompt.strip():
            system += '\n\nAdditional instructions from the user:\n' + custom_prompt.strip()
        if persona_text:
            system += '\n\nAdopt the following persona and tone:\n' + persona_text
        blocks = ['Note title: ' + str(title)]
        if ground and note.get('refs'):
            nctx = _build_note_context(note.get('refs') or [])
            if nctx:
                blocks.append('Referenced document context:\n---\n' + nctx + '\n---')
        blocks.append('Current note:\n---\n' + content[:_MAX_BODY_NOTE] + '\n---')
        blocks.append('Task: Answer the user question about this note. Do not rewrite the note.')
        if question.strip():
            blocks.append('Question: ' + question.strip())
        else:
            blocks.append('User message: ' + user_message)
        return system, '\n\n'.join(blocks)
    instr = _NOTE_ACTIONS.get(action, _NOTE_ACTIONS['enhance'])
    system = _NOTE_EDIT_SYSTEM.format(note_id=note_id)
    if custom_prompt.strip():
        system += '\n\nAdditional instructions from the user:\n' + custom_prompt.strip()
    if persona_text:
        system += '\n\nAdopt the following persona and tone:\n' + persona_text
    blocks = ['Note id: ' + str(note_id), 'Note title: ' + str(title)]
    if ground and note.get('refs'):
        nctx = _build_note_context(note.get('refs') or [])
        if nctx:
            blocks.append('Referenced document context:\n---\n' + nctx + '\n---')
    blocks.append('Current note:\n---\n' + content[:_MAX_BODY_NOTE] + '\n---')
    blocks.append('Task: ' + instr)
    blocks.append('Apply this task to the note with the study_note tool. Do not return the note body as chat text - the user sees the live note update.')
    system += '\n\n' + _study_tool_sections()
    return system, '\n\n'.join(blocks)


# ── Model streaming (Qwen scraper path OR API connector path) ────────────────


async def _stream(
    system: str,
    prompt: str,
    model: str,
    thinking_mode: str,
    *,
    files: list[str] | None = None,
    ref_file_ids: list[str] | None = None,
    ref_files: list[dict[str, Any]] | None = None,
    chat_id: str | None = None,
) -> AsyncGenerator[dict[str, Any], None]:
    """Yield normalized events ({type: answer|thinking|error}) for any model.

    Attachment kwargs come from :func:`_resolve_attachments` — never a raw
    local path on its own. ``files`` carries paths only for direct-read
    connectors (they inline the bytes); ``ref_file_ids`` / ``ref_files`` carry
    the provider-side upload result for every other backend.

    Instruction handling is deliberate: study answers get the study directive
    instead of Sable's instruction registry, so the ``chat_title`` tool schema
    (and every other chat-harness tool) never reaches the model.
    """
    if model and _is_api_model(model):
        backend = _resolve_api_backend(model)
        conn = get_connector(backend, model_id=model)
        kwargs: dict[str, Any] = {
            "model": model,
            "thinking_mode": thinking_mode or None,
            "inject_instructions": False,
            "system_instruction": system,
            # layout_mode="study" tells the connector to suppress the
            # chat_title native handler (it must never fire in study mode).
            "layout_mode": "study",
        }
        if files:
            kwargs["files"] = files
        if ref_file_ids:
            kwargs["ref_file_ids"] = ref_file_ids
        if ref_files:
            kwargs["ref_files"] = ref_files
        try:
            agen = conn.stream_chat(prompt, **kwargs)
        except TypeError:
            # Older connector build: drop unsupported kwargs one at a time.
            kwargs.pop("layout_mode", None)
            kwargs.pop("system_instruction", None)
            agen = conn.stream_chat(prompt, **kwargs)
        async for ev in agen:
            yield ev
        return

    # Qwen/scraper path: its session instructions are global and synced ahead
    # of time, so the study directive is prefixed to the message instead.
    # Any chat_title tag the model emits anyway is stripped downstream.
    qwen_message = f"[STUDY MODE INSTRUCTIONS]\n{system}\n\n{prompt}"
    # Qwen's payload builder expects already-uploaded file objects here, not
    # local paths — an un-uploaded path is silently useless to the model.
    qwen_files = ref_files or None
    # chat_id reuse: an AI OCR run sends many pages through here. Passing the
    # same chat_id makes every page reuse ONE Qwen session instead of opening a
    # fresh browser/chat per page — otherwise page 2+ stall on the browser
    # governor's 120s acquire timeout and the whole run dies after page 1.
    async for ev in service.stream_events(
        qwen_message,
        chat_id=chat_id,
        model=model or None,
        thinking_mode=thinking_mode or None,
        files=qwen_files,
    ):
        yield ev


# ── Study tool loop ──────────────────────────────────────────────────────────

# Cap on tool-call rounds for the scraper path (API connectors cap their own
# loop internally). A model that keeps calling tools without ever answering is
# almost always stuck, so we stop feeding it and let the last text stand.
_MAX_TOOL_ROUNDS = 12


def _study_tool_schemas() -> list[dict]:
    """Schemas for the tools study mode exposes, minus anything disabled."""
    try:
        return get_study_tool_schemas()
    except Exception:
        logger.warning("study tool schema load failed", exc_info=True)
        return []


def _tool_primer() -> str:
    """The <action> JSON format instruction for the scraper path."""
    try:
        from connectors.common.instruction_builder import get_tool_format
        return get_tool_format("qwen") or ""
    except Exception:
        return ""


async def _scraper_tool_loop(
    system: str,
    prompt: str,
    model: str,
    thinking_mode: str,
    tools: list[dict],
    ctx: StudyContext,
    *,
    chat_id: str = "",
    ref_files: list[dict[str, Any]] | None = None,
) -> AsyncGenerator[dict[str, Any], None]:
    """Tool loop for the Qwen/scraper path.

    The browser scraper has no native function-calling API — tools are invoked
    by emitting an ``<action>`` JSON block the model has been primed to use.
    Each round: stream text, parse tool tags out of it, run them through the
    shared skill engine, then send the tool output back as the next turn. The
    loop ends when a round produces no tool calls or the round cap is hit.

    Session continuity is essential: ``chat_id`` pins one upstream conversation
    for every round, and the ``parent_id`` from each ``done`` event is fed back
    into the next round. Without them the service cannot resume the same
    conversation, so the model would receive the tool output with no memory of
    the question that triggered it.
    """
    import asyncio as _asyncio

    from engine.skills.parser import SkillParser
    from engine.skills.events import build_tool_feedback
    from engine.skills import get_skill_engine

    engine = get_skill_engine()
    primer = _tool_primer()
    schemas_block = "\n".join(json.dumps(s, ensure_ascii=False) for s in tools)
    base_system = (
        system
        + "\n\n" + primer
        + "\n\n<tools>\n" + schemas_block + "\n</tools>"
    )

    # Round 0 carries the full directive + tool schemas. Later rounds send ONLY
    # the tool results — the browser session already holds the system context,
    # so re-sending it each round would drown the model in repeated prose.
    message = f"[STUDY MODE INSTRUCTIONS]\n{base_system}\n\n{prompt}"
    qwen_files = ref_files or None

    # Session continuity: one stable upstream conversation for every round.
    # Round 0 passes NO id so the service creates a real upstream chat and
    # reports it via a `meta` event — we adopt that id for every later round.
    # (Fabricating an id would point at a conversation that does not exist.)
    loop_chat_id = chat_id or None
    loop_parent: str | None = None

    for _round in range(_MAX_TOOL_ROUNDS):
        parser = SkillParser(known_tags=tuple(
            set(engine.get_known_tags())
            | {"study_document", "study_note", "study_pdf", "study_diagnostics"}
        ))
        round_events: list[dict[str, Any]] = []
        round_text: list[str] = []

        # Files ride only the opening turn — the session holds them afterwards,
        # and re-uploading the same attachment every round is pure waste (and
        # on Qwen it stacks duplicate copies into the conversation).
        files_for_round = qwen_files if _round == 0 else None

        async for ev in service.stream_events(
            message,
            chat_id=loop_chat_id,
            parent_id=loop_parent,
            model=model or None,
            thinking_mode=thinking_mode or None,
            files=files_for_round,
        ):
            etype = ev.get("type")
            if etype == "answer":
                text = _strip_title_tags(str(ev.get("text", "")))
                # Emit text as it arrives, but hold back anything that might be
                # the start of a tool tag so raw JSON never flashes on screen.
                for parsed in parser.feed(text):
                    if parsed.get("type") == "text":
                        chunk = str(parsed.get("text", ""))
                        if chunk:
                            round_text.append(chunk)
                            yield {"type": "answer", "text": chunk}
                    else:
                        round_events.append(parsed)
            elif etype == "thinking":
                yield {"type": "thinking", "text": ev.get("text", "")}
            elif etype == "meta":
                # The service reports the REAL upstream chat id. Adopt it for
                # subsequent rounds — our fabricated placeholder would otherwise
                # point at a conversation that does not exist upstream.
                loop_chat_id = ev.get("chat_id") or loop_chat_id
                loop_parent = ev.get("parent_id") or loop_parent
            elif etype == "done":
                # Carry the session anchor into the next round so the service
                # resumes THIS conversation instead of starting a new one.
                loop_parent = ev.get("parent_id") or loop_parent
            elif etype == "error":
                yield {"type": "error", "message": ev.get("message", "error")}
                return

        # Flush the parser — it may be holding a trailing text tail.
        for parsed in parser.flush():
            if parsed.get("type") == "text":
                chunk = str(parsed.get("text", ""))
                if chunk:
                    round_text.append(chunk)
                    yield {"type": "answer", "text": chunk}
            else:
                round_events.append(parsed)

        tool_calls = [e for e in round_events if e.get("type") == "tag_found"]
        if not tool_calls:
            return  # no tools → this was the final answer

        skill_events: list[dict[str, Any]] = []
        attachments: list[dict[str, Any]] = []
        for call in tool_calls:
            cname = call.get("name", "")
            cid = str(uuid.uuid4())[:12]
            yield {"type": "skill_start", "id": cid, "name": cname}
            skill_events.append({"type": "skill_start", "id": cid, "name": cname})
            try:
                events = await _asyncio.to_thread(
                    lambda: list(engine.process_tag(
                        cname, call.get("attrs", {}), call.get("content", ""),
                        namespace="study",
                    ))
                )
            except Exception as exc:
                events = [{"type": "skill_end", "id": cid, "name": cname,
                           "ok": False, "error": str(exc)}]
            for sev in events:
                sev.setdefault("id", cid)
                if sev.get("type") == "study_attachment":
                    attachments.append(sev)
                if sev.get("type") in ("skill_start", "skill_output", "skill_end"):
                    skill_events.append(sev)
                yield sev

        # Surface any side-channel events handlers queued (doc_changed).
        for ev in ctx.events:
            yield ev
        ctx.events.clear()

        feedback = build_tool_feedback(skill_events)
        if not feedback:
            return
        message = (
            "[TOOL RESULTS]\n" + feedback
            + "\n\nContinue: use the results to finish answering the user, "
            "or call another tool if you still need to."
        )
        # Rendered page images ride the next turn as attachments.
        for att in attachments:
            yield att

    logger.info("study scraper tool loop hit the %d-round cap", _MAX_TOOL_ROUNDS)


async def _stream_with_tools(
    system: str,
    prompt: str,
    model: str,
    thinking_mode: str,
    tools: list[dict],
    ctx: StudyContext,
    *,
    chat_id: str = "",
    files: list[str] | None = None,
    ref_file_ids: list[str] | None = None,
    ref_files: list[dict[str, Any]] | None = None,
) -> AsyncGenerator[dict[str, Any], None]:
    """Stream a study answer, running tools for whichever backend is in use.

    API connectors already own a native tool loop, so they receive the schemas
    and hand back fully-executed turns. The scraper path needs the tag loop we
    implement here. Both yield the same normalized event shapes.
    """
    if model and _is_api_model(model):
        backend = _resolve_api_backend(model)
        conn = get_connector(backend, model_id=model)
        kwargs: dict[str, Any] = {
            "model": model,
            "thinking_mode": thinking_mode or None,
            "inject_instructions": False,
            "system_instruction": system,
            # Suppress the chat_title native handler in study mode.
            "layout_mode": "study",
        }
        if files:
            kwargs["files"] = files
        if ref_file_ids:
            kwargs["ref_file_ids"] = ref_file_ids
        if ref_files:
            kwargs["ref_files"] = ref_files
        if tools:
            kwargs["tools"] = tools
        try:
            agen = conn.stream_chat(prompt, **kwargs)
        except TypeError:
            kwargs.pop("layout_mode", None)
            kwargs.pop("system_instruction", None)
            try:
                agen = conn.stream_chat(prompt, **kwargs)
            except TypeError:
                kwargs.pop("tools", None)
                agen = conn.stream_chat(prompt, **kwargs)
        async for ev in agen:
            yield ev
        return

    async for ev in _scraper_tool_loop(
        system, prompt, model, thinking_mode, tools, ctx,
        chat_id=chat_id, ref_files=ref_files,
    ):
        yield ev


def _forward_study_event(ev: dict[str, Any], parts: list[str]) -> str | None:
    """Map a normalized stream event to an SSE frame for the study frontend.

    Returns the frame string, or None for events that carry no client payload
    (already-consumed text, internal markers). ``parts`` accumulates the answer
    so the caller can emit the final ``done`` text.
    """
    etype = ev.get("type")
    if etype == "answer":
        text = _strip_title_tags(str(ev.get("text", "")))
        if not text:
            return None
        parts.append(text)
        return sse({"type": "answer", "text": text})
    if etype == "thinking":
        return sse({"type": "thinking", "text": ev.get("text", "")})
    if etype == "error":
        return sse({"type": "error", "message": ev.get("message", "error")})
    if etype in ("skill_start", "skill_output", "skill_end"):
        frame: dict[str, Any] = {
            "type": etype,
            "id": ev.get("id"),
            "name": ev.get("name"),
        }
        if etype == "skill_output":
            frame["text"] = ev.get("text", "")
            frame["stream"] = ev.get("stream", "stdout")
        if etype == "skill_end":
            frame["ok"] = ev.get("ok", False)
            frame["duration_ms"] = ev.get("duration_ms", 0)
            if ev.get("error"):
                frame["error"] = ev.get("error")
        return sse(frame)
    if etype == "doc_changed":
        return sse({k: v for k, v in ev.items() if v is not None})
    if etype == "study_attachment":
        # Page images the model pulled in — surfaced so the UI can show them.
        return sse({
            "type": "study_attachment",
            "kind": ev.get("kind", "image"),
            "name": ev.get("name", ""),
            "page": ev.get("page", 0),
            "data": ev.get("data", ""),
        })
    return None


def _strip_title_tags(text: str) -> str:
    """Remove chat_title / stray tool tags from an answer (belt-and-braces)."""
    if not text:
        return text
    cleaned = _TITLE_TAG_RE.sub("", text)
    return _TOOL_TAG_RE.sub("", cleaned)


# ── Attachments (page image / PDF) ───────────────────────────────────────────

_MAX_ATTACH_BYTES = 20 * 1024 * 1024


def _persist_attachments(atts: list[dict[str, Any]], workdir: Path) -> list[str]:
    """Write base64 attachments to temp files and return their paths.

    Connectors take file *paths* (`files=[...]`), not raw bytes — so image and
    PDF attachments both land on disk here and are cleaned up by the caller.
    """
    paths: list[str] = []
    for i, att in enumerate(atts or []):
        data = str(att.get("data") or "")
        if not data:
            continue
        # Accept a raw base64 string or a data: URL.
        if data.startswith("data:"):
            _head, _, data = data.partition(",")
        try:
            raw = base64.b64decode(data, validate=False)
        except Exception:
            continue
        if not raw or len(raw) > _MAX_ATTACH_BYTES:
            continue
        name = str(att.get("name") or f"attachment-{i}")
        # Keep only the extension from the client name — never trust the path.
        ext = Path(name).suffix.lower() or ("" if att.get("kind") == "image" else ".pdf")
        safe = f"att-{i}{ext}"
        dest = workdir / safe
        dest.write_bytes(raw)
        paths.append(str(dest))
    return paths


def _serialize_conversation(messages: list[dict[str, Any]]) -> str:
    """Render a multi-turn thread as a single prompt string.

    Study answers reuse the plain ``stream_chat(message)`` entry point (no
    provider session), so prior turns are flattened into the prompt. That keeps
    the whole conversation — every user question AND every assistant answer —
    visible to the model on each turn.
    """
    lines: list[str] = []
    for m in messages:
        role = str(m.get("role") or "user")
        content = str(m.get("content") or "").strip()
        if not content:
            continue
        label = "User" if role == "user" else "Assistant"
        lines.append(f"{label}: {content}")
    return "\n\n".join(lines)


async def _upload_attachment(backend: str, path: str) -> dict[str, Any] | None:
    """Upload one local file to the provider, returning its file object.

    Mirrors the per-backend routing in ``server/api/routes/chat.py`` so Study
    AI attaches files exactly the way the main chat does instead of handing a
    local temp path to a provider that can't read it.
    """
    try:
        if backend == "deepseek":
            return await service.upload_deepseek_file(path)
        if backend == "zai":
            from connectors.zai.client import get_client
            return await get_client().upload_file(path)
        if backend == "gemini_web":
            from connectors.gemini_web.client import get_client
            return await get_client().upload_file(path)
        if backend == "mimo":
            from connectors.mimo.client import get_client
            return await get_client().upload_file(path)
        if backend == "mistral_web":
            from connectors.mistral_web.client import get_client
            return await get_client().upload_file(path)
        if backend == "kimi":
            from connectors.kimi.client import get_client
            return await get_client().upload_file(path)
    except Exception as exc:
        logger.warning("study attach upload failed (%s): %s", backend, exc)
        return None
    # A NAMED backend reaching here has no explicit upload path. Log it loudly:
    # silently borrowing the Qwen uploader is exactly how deepseek_scraper's
    # page images were dropped (it returned a Qwen file id the DeepSeek SPA
    # could never use, so the model saw no image at all).
    if backend:
        logger.warning(
            "study attach: backend %r has no explicit upload path — trying the "
            "Qwen uploader as a fallback, which is wrong for any non-Qwen "
            "transport. Add a branch in _upload_attachment/_resolve_attachments.",
            backend,
        )
    # Qwen / scraper models (no api_backend) go through the Qwen OSS uploader.
    try:
        return await service.upload_file(path)
    except Exception as exc:
        logger.warning("study attach Qwen upload failed: %s", exc)
        return None


async def _resolve_attachments(model: str, file_paths: list[str]) -> dict[str, Any]:
    """Route attachments the same way the main chat route does.

    Returns keyword arguments for :func:`_stream`: ``files`` for direct-read
    connectors (they inline the bytes themselves), plus ``ref_file_ids`` and
    ``ref_files`` for every backend that needs a real provider-side upload.
    """
    if not file_paths:
        return {}

    backend = _resolve_api_backend(model) if _is_api_model(model) else None

    # Direct-read connectors take local paths and base64-inline the bytes.
    if backend in _DIRECT_READ_BACKENDS:
        logger.info("study attach: direct-read backend %r -> local paths", backend)
        return {"files": list(file_paths)}

    # Scraper transports (deepseek_scraper) upload via Playwright: the
    # connector's stream_chat() iterates `files` and calls engine.upload_file()
    # on each local path. Hand them the paths directly.
    #
    # Without this branch the call fell through to _upload_attachment(), which
    # has no scraper case and therefore hit the Qwen OSS uploader as its
    # catch-all — returning a Qwen file id that DeepSeek's SPA can never use.
    # The image was silently dropped and AI OCR saw no page at all.
    if backend in _SCRAPER_FILE_BACKENDS:
        logger.info("study attach: scraper backend %r -> Playwright local paths", backend)
        return {"files": list(file_paths)}

    logger.info(
        "study attach: backend=%r routing %d file(s) via provider upload",
        backend, len(file_paths),
    )
    resolved: list[dict[str, Any]] = []
    for path in file_paths:
        meta = await _upload_attachment(backend or "", path)
        if meta:
            resolved.append(meta)
            logger.info(
                "study attach: uploaded %s -> id=%s url=%s",
                path, meta.get("file_id") or meta.get("id"),
                (meta.get("url") or "")[:60],
            )
        else:
            logger.warning(
                "study attach: upload FAILED for %s (backend=%r) — "
                "this image will NOT reach the model",
                path, backend,
            )
    if not resolved:
        logger.warning(
            "study attach: NO files resolved for backend=%r — returning empty "
            "kwargs, model gets no attachment", backend,
        )
        return {}

    out: dict[str, Any] = {}
    ref_ids = [
        str(m.get("file_id") or m.get("id"))
        for m in resolved
        if m.get("file_id") or m.get("id")
    ]
    if ref_ids:
        out["ref_file_ids"] = ref_ids

    # Some backends need the FULL uploaded object, not just an id: z.ai and
    # Gemini Web put it in their completion body, MiMo/Mistral key off `url`.
    # DeepSeek/Kimi consume the id alone, so they get nothing extra here.
    if backend in ("zai", "gemini_web"):
        full = [m for m in resolved if m.get("id") or m.get("blob")]
    elif backend == "mimo":
        full = [m for m in resolved if m.get("fileUrl")]
    elif backend == "mistral_web":
        full = [m for m in resolved if m.get("url")]
    elif backend is None:
        # Qwen rides the whole object on the payload's `files` array.
        full = resolved
    else:
        full = []
    if full:
        out["ref_files"] = full
    return out


@router.post("/api/study/doc/{doc_id}/chat")
async def study_ai_chat(doc_id: str, payload: dict[str, Any]) -> StreamingResponse:
    """Stream a multi-turn study answer.

    body: {
      messages:   [{role, content}],        # full thread, oldest first
      attachments:[{kind:'image'|'text', name, data}],  # base64
      action?, scope?, page?, selection?,   # optional document grounding
      model?, thinking_mode?, persona?, custom_prompt?
    }

    Unlike the single-shot endpoint, this keeps every turn: the client sends
    the whole conversation and the server returns one more assistant turn.
    """
    meta = _read_meta(doc_id)
    if not meta:
        raise HTTPException(status_code=404, detail="document not found")

    cfg = _load_config()
    model = str(payload.get("model") or cfg.get("model") or "")
    thinking_mode = str(payload.get("thinking_mode") or cfg.get("thinking_mode") or "")
    persona = str(payload.get("persona") if "persona" in payload else cfg.get("persona") or "")
    custom_prompt = str(
        payload.get("custom_prompt") if "custom_prompt" in payload else cfg.get("custom_prompt") or ""
    )
    scope = str(payload.get("scope") or "doc")
    selection = str(payload.get("selection") or "")
    action = str(payload.get("action") or "")
    try:
        page = int(payload.get("page") or 1)
    except (TypeError, ValueError):
        page = 1

    messages = payload.get("messages") or []
    if not isinstance(messages, list) or not messages:
        raise HTTPException(status_code=400, detail="messages required")
    messages = [m for m in messages if isinstance(m, dict)][-40:]

    system = _build_system(custom_prompt, _read_persona(persona))
    # Rank document passages against the user's latest turn, so a long chat
    # keeps grounding on whatever the current question is actually about.
    last_user = next(
        (str(m.get("content") or "") for m in reversed(messages) if m.get("role") == "user"),
        "",
    )
    topic_scope = scope == "doc" and action in ("", "ask")
    _pages = [p for p in (payload.get("pages") or []) if isinstance(p, int)]
    context = _build_context(
        meta, scope, page, selection, query=last_user,
        topic_scope=topic_scope, pages=_pages,
    )

    blocks = [f'Document: "{meta.get("title", "")}"']
    if context:
        blocks.append("Document context:\n---\n" + context + "\n---")
    if selection:
        blocks.append("Selected text:\n---\n" + selection[:_MAX_SELECTION_CHARS] + "\n---")
    blocks.append("Conversation:\n" + _serialize_conversation(messages))
    if action and action in _ACTION_PROMPTS:
        # An explicit action (explain / summarize / define / …) carries its own
        # instruction; the conversation above still supplies continuity.
        blocks.append("Task: " + _ACTION_PROMPTS[action])
    else:
        blocks.append(
            "Task: Answer the user's latest message, using the document context and "
            "the conversation so far. Keep continuity with your earlier answers."
        )
    if "OTHER DOCUMENT" in context:
        blocks.append(_PROVENANCE_HINT)
    prompt = "\n\n".join(blocks)

    atts = payload.get("attachments") or []
    if not isinstance(atts, list):
        atts = []

    # Image attachments need a model that can actually see; text attachments
    # (the fallback the client sends for non-vision models) are folded into the
    # prompt so every model still gets the page/PDF content.
    image_atts = [a for a in atts if isinstance(a, dict) and a.get("kind") != "text"]
    text_atts = [a for a in atts if isinstance(a, dict) and a.get("kind") == "text"]
    if text_atts:
        extra = []
        for a in text_atts:
            data = str(a.get("data") or "")
            if data.startswith("data:"):
                _head, _, data = data.partition(",")
                try:
                    data = base64.b64decode(data).decode("utf-8", "ignore")
                except Exception:
                    data = ""
            label = str(a.get("name") or "attachment")
            if data.strip():
                extra.append(f"[{label}]\n{data.strip()[:_MAX_CTX_CHARS]}")
        if extra:
            prompt += "\n\nAttached content:\n---\n" + "\n\n".join(extra) + "\n---"

    try:
        caps = (get_model_config(model) or {}).get("capabilities") or {}
    except Exception:
        caps = {}
    can_see_images = bool(caps.get("image"))
    can_read_docs = bool(caps.get("document"))
    # Keep only attachments this model can actually accept; anything else was
    # already downgraded to a text attachment by the client.
    sendable = [
        a for a in image_atts
        if (can_see_images and a.get("kind") != "document")
        or (can_read_docs and a.get("kind") == "document")
    ]
    # Attachments the model cannot accept, so the UI can say so instead of
    # silently sending a prompt with the page/PDF missing.
    dropped = [
        str(a.get("name") or a.get("kind") or "attachment")
        for a in image_atts if a not in sendable
    ]

    tools = _study_tool_schemas()

    async def gen() -> AsyncGenerator[str, None]:
        workdir = Path(tempfile.mkdtemp(prefix="sable-study-att-"))
        file_paths = _persist_attachments(sendable, workdir)
        # Upload to the provider (or keep paths for direct-read connectors) the
        # same way the main chat route does, so a vision model actually receives
        # the page image instead of an unreadable local temp path.
        attach_kwargs = await _resolve_attachments(model, file_paths)
        parts: list[str] = []
        study_ctx = StudyContext(doc_id=doc_id, page=page)
        token = set_study_context(study_ctx)
        yield sse({"type": "start", "model": model, "attachments": len(file_paths)})
        if dropped:
            yield sse({"type": "attachment_dropped", "names": dropped})
        try:
            async for ev in _stream_with_tools(
                system, prompt, model, thinking_mode, tools, study_ctx, **attach_kwargs,
            ):
                forwarded = _forward_study_event(ev, parts)
                if forwarded is not None:
                    yield forwarded
        except Exception as exc:  # surface any transport failure to the card
            yield sse({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
            return
        finally:
            reset_study_context(token)
            shutil.rmtree(workdir, ignore_errors=True)
        yield sse({"type": "done", "text": _strip_title_tags("".join(parts)).strip()})

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Saved AI notes (page-anchored answers) ───────────────────────────────────


@router.post("/api/study/doc/{doc_id}/notes")
def study_add_note(doc_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Persist an AI answer against a selection / page / document."""
    # Full thread (all turns), so a saved note shows the whole conversation
    # instead of only the last answer. Falls back to a single turn when the
    # client sends just question/answer.
    raw_msgs = payload.get("messages")
    messages: list[dict[str, str]] = []
    if isinstance(raw_msgs, list):
        for m in raw_msgs[-40:]:
            if not isinstance(m, dict):
                continue
            content = str(m.get("content") or "")[:60000]
            if not content:
                continue
            messages.append({
                "role": "assistant" if str(m.get("role")) == "assistant" else "user",
                "content": content,
            })

    note = {
        "id": uuid.uuid4().hex[:12],
        "page": int(payload.get("page") or 0),
        "rects": _clean_rects(payload.get("rects")),
        "text": str(payload.get("text") or "")[:_MAX_SELECTION_CHARS],
        "action": str(payload.get("action") or "ask")[:40],
        "question": str(payload.get("question") or "")[:2000],
        "answer": str(payload.get("answer") or "")[:60000],
        "messages": messages,
        "model": str(payload.get("model") or "")[:80],
        "scope": str(payload.get("scope") or "selection")[:20],
        "created": time.time(),
    }

    def mutate(meta: dict[str, Any]) -> dict[str, Any]:
        notes = meta.get("ai_notes") or []
        notes.append(note)
        meta["ai_notes"] = notes
        return note

    return _update_meta(doc_id, mutate)


@router.patch("/api/study/doc/{doc_id}/notes/{note_id}")
def study_update_note(doc_id: str, note_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    def mutate(meta: dict[str, Any]) -> dict[str, Any]:
        for note in meta.get("ai_notes") or []:
            if note.get("id") == note_id:
                if "answer" in payload:
                    note["answer"] = str(payload["answer"])[:60000]
                if "question" in payload:
                    note["question"] = str(payload["question"])[:2000]
                return note
        raise HTTPException(status_code=404, detail="note not found")

    return _update_meta(doc_id, mutate)


@router.delete("/api/study/doc/{doc_id}/notes/{note_id}")
def study_delete_note(doc_id: str, note_id: str) -> dict[str, Any]:
    def mutate(meta: dict[str, Any]) -> dict[str, Any]:
        notes = meta.get("ai_notes") or []
        kept = [n for n in notes if n.get("id") != note_id]
        if len(kept) == len(notes):
            raise HTTPException(status_code=404, detail="note not found")
        meta["ai_notes"] = kept
        return {"deleted": True, "id": note_id}

    return _update_meta(doc_id, mutate)


# ── Note-editor AI (handwritten notes) ───────────────────────────────────────


def _build_note_context(refs: list[dict[str, Any]], budget: int = _MAX_DOC_CTX) -> str:
    """Ground a note rewrite in the documents it references.

    Walks the note's refs, pulling the anchored page (or the first pages of the
    whole document) from each referenced library doc. Silent about missing
    docs — a reference can outlive the file it pointed at.
    """
    parts: list[str] = []
    total = 0
    for ref in refs or []:
        doc_id = str(ref.get("doc_id") or "")
        if not doc_id:
            continue
        meta = _read_meta(doc_id)
        if not meta:
            continue
        title = meta.get("title") or doc_id
        try:
            page = int(ref.get("page") or 0)
        except (TypeError, ValueError):
            page = 0

        page_text = _load_page_text(meta)
        chunk = ""
        if page and 1 <= page <= len(page_text):
            chunk = f"[{title} — page {page}]\n{(page_text[page - 1] or '').strip()[:_MAX_CTX_CHARS]}"
        else:
            collected: list[str] = []
            for i, text in enumerate(page_text, 1):
                text = (text or "").strip()
                if not text:
                    continue
                collected.append(f"[page {i}]\n{text}")
                if sum(len(c) for c in collected) > _MAX_CTX_CHARS:
                    break
            chunk = f"[{title}]\n" + "\n".join(collected)

        if not chunk.strip():
            continue
        if total + len(chunk) > budget:
            chunk = chunk[: max(0, budget - total)]
        parts.append(chunk)
        total += len(chunk)
        if total >= budget:
            break
    return "\n\n".join(parts)


@router.post("/api/study/notes/{note_id}/ai")
async def study_note_ai(note_id: str, payload: dict[str, Any]) -> StreamingResponse:
    """Stream an AI rewrite of a handwritten note.

    body: {
      action: ask|refactor|redesign|organize|enhance|summarize|expand|clarify|
              fix-grammar|add-examples|flashcards|questions,
      question?,            # required for action=ask
      content?,             # live editor body (falls back to the saved file)
      ground?,              # ground the rewrite in referenced documents
      model?, thinking_mode?, persona?, custom_prompt?
    }

    SSE events mirror the document endpoints: {type:start} →
{type:thinking|answer}… → {type:done,text}.
    """
    index = _read_notes_index()
    note = _find_note(index, note_id)
    if not note:
        raise HTTPException(status_code=404, detail="note not found")

    cfg = _load_config()
    model = str(payload.get("model") or cfg.get("model") or "")
    thinking_mode = str(payload.get("thinking_mode") or cfg.get("thinking_mode") or "")
    persona = str(payload.get("persona") if "persona" in payload else cfg.get("persona") or "")
    custom_prompt = str(
        payload.get("custom_prompt") if "custom_prompt" in payload else cfg.get("custom_prompt") or ""
    )
    action = str(payload.get("action") or "enhance")
    question = str(payload.get("question") or "")
    content = payload.get("content")
    if not isinstance(content, str) or not content.strip():
        content = _read_note_body(note_id)
    ground = bool(payload.get("ground"))

    action_instr = _NOTE_ACTIONS.get(action, _NOTE_ACTIONS["enhance"])

    # The editor may hold newer text than the file. Bring the file up to date so
    # a tool edit applies to exactly what the user sees, never a stale body.
    if content.strip():
        try:
            if _read_note_body(note_id) != content:
                _write_note_body(note_id, content[:_MAX_BODY_NOTE])
        except Exception:
            logger.debug("note pre-sync failed for %s", note_id, exc_info=True)

    is_ask = action == "ask"
    if is_ask:
        # A question is answered in prose; no edit, no tool.
        system = _NOTE_SYSTEM
        if custom_prompt.strip():
            system += "\n\nAdditional instructions from the user:\n" + custom_prompt.strip()
        persona_text = _read_persona(persona)
        if persona_text:
            system += "\n\nAdopt the following persona and tone:\n" + persona_text
        blocks = [f'Note title: "{note.get("title") or "Untitled note"}"']
        if ground and note.get("refs"):
            ctx = _build_note_context(note.get("refs") or [])
            if ctx:
                blocks.append("Referenced document context:\n---\n" + ctx + "\n---")
        blocks.append("Current note:\n---\n" + content[:_MAX_BODY_NOTE] + "\n---")
        blocks.append("Task: Answer the user's question about this note. Do not rewrite the note.")
        if question.strip():
            blocks.append("Question: " + question.strip())
        prompt = "\n\n".join(blocks)
    else:
        system = _NOTE_EDIT_SYSTEM.format(note_id=note_id)
        if custom_prompt.strip():
            system += "\n\nAdditional instructions from the user:\n" + custom_prompt.strip()
        persona_text = _read_persona(persona)
        if persona_text:
            system += "\n\nAdopt the following persona and tone:\n" + persona_text
        blocks = [f'Note id: {note_id}', f'Note title: "{note.get("title") or "Untitled note"}"']
        if ground and note.get("refs"):
            ctx = _build_note_context(note.get("refs") or [])
            if ctx:
                blocks.append("Referenced document context:\n---\n" + ctx + "\n---")
        blocks.append("Current note:\n---\n" + content[:_MAX_BODY_NOTE] + "\n---")
        blocks.append("Task: " + action_instr)
        blocks.append(
            "Apply this task to the note with the study_note tool. Do not return "
            "the note body as chat text — the user sees the live note update."
        )
        prompt = "\n\n".join(blocks)

    tools = [] if is_ask else [
        s for s in _study_tool_schemas()
        if s.get("function", {}).get("name") == "study_note"
    ]

    async def gen() -> AsyncGenerator[str, None]:
        parts: list[str] = []
        study_ctx = StudyContext(doc_id="", page=0)
        token = set_study_context(study_ctx)
        yield sse({"type": "start", "action": action, "model": model, "note_id": note_id})
        try:
            async for ev in _stream_with_tools(
                system, prompt, model, thinking_mode, tools, study_ctx,
            ):
                forwarded = _forward_study_event(ev, parts)
                if forwarded is not None:
                    yield forwarded
        except Exception as exc:
            yield sse({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
            return
        finally:
            reset_study_context(token)
        # For ask, the answer text is the deliverable. For edits, the note is
        # already changed — ``done`` just closes the stream.
        final = _strip_title_tags("".join(parts))
        yield sse({"type": "done", "text": final if is_ask else "", "edited": not is_ask, "note_id": note_id})

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _strip_note_fence(text: str) -> str:
    """Unwrap a whole-note ```markdown fence the model added despite instructions."""
    t = (text or "").strip()
    if not t.startswith("```"):
        return t
    first_nl = t.find("\n")
    if first_nl == -1:
        return t
    if not t.endswith("```"):
        return t
    return t[first_nl + 1 : -3].strip()

